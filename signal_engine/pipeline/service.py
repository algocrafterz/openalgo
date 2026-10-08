"""Process wiring for the runner: pre-flight checks, one Runner per session, start and stop.

Used by startup._run_engine. Nothing here places an order by itself: a strategy whose
signal_source is python gets a DispatchSink (main.dispatch_signal, so analyze or live depending
on OpenAlgo's mode); any other configured strategy gets a RecordingSink (shadow).

Fail closed: if history is unreachable or no symbol warms up, the runner is not started and the
reason is logged at critical level. For a strategy already switched to python that means no
signals (Pine alerts are dropped by the single-source guard), which is loud and safe, unlike
running blind.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace

import pandas as pd
from loguru import logger

from signal_engine.config import RunnerSettings
from signal_engine.pipeline import pretrade, single_source
from signal_engine.pipeline.bars import (
    BarSource,
    MissingBarsError,
    OpenAlgoHistorySource,
    SharedBarSource,
)
from signal_engine.pipeline.clock import Clock, WallClock
from signal_engine.pipeline.context import HistorifyLoader, RollingContext
from signal_engine.pipeline.cycle import shared_executor, shutdown_executor
from signal_engine.pipeline.exit_driver import ExitDriver, plan_for
from signal_engine.pipeline.runner import Runner
from signal_engine.pipeline.sink import DispatchSink, ExecutionSink, RecordingSink
from signal_engine.pipeline.state import RunnerStateStore
from signal_engine.pipeline.strategies.breakout import BreakoutStrategy
from signal_engine.pipeline.strategies.ema_vwap_early import TAG as EVWAP_EARLY_TAG
from signal_engine.pipeline.strategies.ema_vwap_early import EarlyDisplacementStrategy
from signal_engine.pipeline.strategy import Strategy
from signal_engine.pipeline.trail_driver import TrailExitDriver
from signal_engine.strategy_core import strategy_spec
from signal_engine.strategy_core.breakout import BreakoutParams
from signal_engine.timeutils import IST


def _spec_strategy(tag: str) -> Strategy:
    """BREAKOUT under `tag`, its params and version stamp read from the frozen specification."""
    spec = strategy_spec.load_spec()
    logger.info(
        f"{tag}: strategy {spec.id} {spec.version} fingerprint {spec.fingerprint[:12]} "
        f"({strategy_spec.SPEC_PATH.name})"
    )
    return BreakoutStrategy(BreakoutParams.from_spec(spec.core), name=tag, spec_stamp=spec.stamp())


#: Strategy tags the pipeline can run. A configured tag not listed here refuses to start.
#: BREAKOUT-PY is the same strategy core under its own tag: a second book (capital, slots, P&L)
#: for paper-trading the Python signals beside the Pine BREAKOUT.
#: EVWAP-EARLY-PY is the EMA/VWAP/ATR early-displacement strategy with a trailing stop and no
#: take-profit (strategy_core.ema_vwap_early); its exits are owned by pipeline.trail_driver.
STRATEGY_FACTORIES: dict[str, Callable[[], Strategy]] = {
    "BREAKOUT": BreakoutStrategy,
    "BREAKOUT-PY": lambda: _spec_strategy("BREAKOUT-PY"),
    EVWAP_EARLY_TAG: EarlyDisplacementStrategy,
}

#: Tags whose rules come from the frozen BREAKOUT specification; start-up checks only these
#: against it (strategy_spec.check_runner).
SPEC_BOUND_TAGS = frozenset({"BREAKOUT-PY"})

SAFETY_BARS = 2
_LOOKBACK_WEEKDAYS = 5


class RunnerStartError(Exception):
    """The runner could not be set up; the message says why."""


def resolve_symbols(cfg: RunnerSettings, historify_symbols: Callable[[], list[str]]) -> list[str]:
    if isinstance(cfg.universe, tuple):
        return list(cfg.universe)
    symbols = list(historify_symbols())
    if not symbols:
        raise RunnerStartError("runner.universe is fno_all but Historify lists no symbols")
    return symbols


def strategy_symbols(cfg: RunnerSettings, tag: str, runner_symbols: list[str]) -> list[str]:
    """The strategy's own universe when config.yaml gives one, else the runner-wide list."""
    own = cfg.strategies[tag].universe
    return list(own) if own is not None else list(runner_symbols)


def manage_until_min(hour: int, minute: int) -> int:
    """Minute of day at which the runner stops stepping bars and the tracker's time exit takes over."""
    return hour * 60 + minute


def build_sink(
    cfg: RunnerSettings, tag: str, dispatch: Callable[[object], Awaitable[None]] | None
) -> ExecutionSink:
    entry = cfg.strategies[tag]
    if entry.signal_source != "python":
        return RecordingSink()
    if dispatch is None:
        raise single_source.SingleSourceError(
            f"{tag}: signal_source is python but no dispatcher was provided"
        )
    return DispatchSink(dispatch, paper_only=entry.paper_only)


def build_exits(
    cfg: RunnerSettings,
    tag: str,
    positions,
    broker_qty,
    strategy: Strategy | None = None,
    context: RollingContext | None = None,
    manage_until_min: int | None = None,
) -> ExitDriver | TrailExitDriver | None:
    entry = cfg.strategies[tag]
    if entry.signal_source != "python":
        return None
    if entry.exit_owner == "strategy":
        if strategy is None or context is None:
            raise RunnerStartError(f"{tag}: exit_owner strategy needs the strategy and its context")
        return TrailExitDriver(
            strategy=strategy,
            context=context,
            positions=positions,
            interval_min=cfg.bar_interval_minutes,
            manage_until_min=manage_until_min,
        )
    if entry.exit_owner != "ladder":
        return None
    return ExitDriver(
        strategy=tag,
        plan=plan_for(entry.exit_plan),
        positions=positions,
        broker_qty=broker_qty,
        interval_min=cfg.bar_interval_minutes,
        safety_bars=SAFETY_BARS,
    )


# ---- health checks ---------------------------------------------------------------------------


def _weekdays_back(now: pd.Timestamp, n: int):
    d = now.date()
    while n > 0:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
            yield d


async def history_reachable(source: BarSource, probe: str, now: pd.Timestamp) -> bool:
    """True when the history endpoint returns bars for one liquid symbol on a recent weekday.

    Walks back over holidays and weekends; a transport error is unreachable, never raised.
    """
    for day in _weekdays_back(now, _LOOKBACK_WEEKDAYS):
        try:
            frames = await source.fetch([probe], 5, day, now)
        except MissingBarsError:
            continue
        except Exception:  # noqa: BLE001 - reported as unreachable
            logger.exception(f"history probe for {probe} failed")
            return False
        if any(not df.empty for df in frames.values()):
            return True
    return False


async def preflight(
    cfg: RunnerSettings,
    source: BarSource,
    context: RollingContext,
    symbols: list[str],
    now: pd.Timestamp,
    day: dt.date,
) -> tuple[bool, str]:
    if not cfg.enabled:
        return False, "runner disabled"
    if not await history_reachable(source, symbols[0], now):
        return False, f"history not reachable (probe {symbols[0]})"
    logger.info(f"runner health: history reachable (probe {symbols[0]})")
    loop = asyncio.get_running_loop()
    failed = await loop.run_in_executor(shared_executor(), context.warm, symbols, day)
    ok = len(context.warmed)
    logger.info(f"runner warm-up finished: {ok} ok, {len(failed)} failed")
    if ok == 0:
        return False, "warm-up: no symbol warmed up"
    return True, ""


# ---- session loop ----------------------------------------------------------------------------


async def run_days(make_runner: Callable[[dt.date], object], clock: Clock) -> None:
    """One Runner per weekday, forever. A failing session is logged and the loop continues."""
    while True:
        now = clock.now()
        day = now.date()
        if day.weekday() < 5:
            try:
                await make_runner(day).run(day)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad session must not end the process
                logger.exception(f"runner session {day} failed")
        await asyncio.sleep(0)
        tomorrow = pd.Timestamp(f"{day + dt.timedelta(days=1)} 09:00", tz=IST)
        await clock.sleep_until(tomorrow)


# ---- process lifecycle -----------------------------------------------------------------------


@dataclass
class RunnerService:
    tasks: list[asyncio.Task] = field(default_factory=list)
    state: RunnerStateStore | None = None

    def cancel(self) -> None:
        """Cancel the session loops (first step of shutdown)."""
        for task in self.tasks:
            if not task.done():
                task.cancel()

    def close(self) -> None:
        """Release the state store and the evaluation pool (last step of shutdown). Idempotent."""
        if self.state is not None:
            self.state.close()
            self.state = None
        shutdown_executor()

    def stop(self) -> None:
        self.cancel()
        self.close()


async def start(
    cfg: RunnerSettings,
    *,
    dispatch,
    tracker,
    trade_mode: str,
    time_exit: tuple[int, int],
    halted: Callable[[], bool],
    stale_limit_s: float,
    product: str,
    clock: Clock | None = None,
    source: BarSource | None = None,
    loader=None,
    historify_symbols: Callable[[], list[str]] | None = None,
    state: RunnerStateStore | None = None,
    broker_qty=None,
) -> RunnerService | None:
    """Build and launch the runner for this process. None when disabled or a check fails."""
    if not cfg.enabled:
        logger.info("Python signal runner: disabled")
        return None
    unknown = [t for t in cfg.strategies if t not in STRATEGY_FACTORIES]
    if unknown:
        raise RunnerStartError(f"no pipeline strategy for: {', '.join(unknown)}")
    cfg = _without_live_paper_only(cfg, trade_mode)
    if not cfg.strategies:
        logger.critical(
            "Python signal runner NOT started: every configured strategy is paper-only and "
            f"OpenAlgo is in {trade_mode} mode"
        )
        return None
    clock = clock or WallClock()
    # One shared source: sessions on the same symbols fetch each bar once between them.
    source = SharedBarSource(
        source
        or OpenAlgoHistorySource(
            timeout_s=cfg.history_timeout_seconds, max_concurrent=cfg.max_concurrent_requests
        )
    )
    if historify_symbols is None:
        from signal_engine.backtest import data

        def historify_symbols() -> list[str]:
            return data.historify_symbols("1m")

    symbols = resolve_symbols(cfg, historify_symbols)
    logger.info(
        f"Python signal runner: {len(symbols)} symbol(s), strategies {list(cfg.strategies)}"
    )
    loader = loader or HistorifyLoader()
    now = clock.now()

    first_tag = next(iter(cfg.strategies))
    probe_ctx = RollingContext(STRATEGY_FACTORIES[first_tag](), loader)
    ok, why = await preflight(cfg, source, probe_ctx, symbols, now, now.date())
    if not ok:
        logger.critical(f"Python signal runner NOT started: {why}")
        return None

    state = state or RunnerStateStore()
    service = RunnerService(state=state)
    exit_qty = broker_qty or make_broker_qty(tracker)
    for tag in cfg.strategies:
        make = _session_factory(
            cfg,
            tag,
            symbols,
            source,
            loader,
            state,
            dispatch,
            tracker,
            exit_qty,
            trade_mode,
            time_exit,
            halted,
            stale_limit_s,
            product,
            clock,
        )
        service.tasks.append(asyncio.create_task(run_days(make, clock), name=f"runner-{tag}"))
        logger.info(f"Python signal runner started for {tag} ({cfg.strategies[tag].signal_source})")
    return service


def _without_live_paper_only(cfg: RunnerSettings, trade_mode: str) -> RunnerSettings:
    """Drop every paper-only strategy when the engine is not in analyze mode, naming each one.

    The sink refuses per signal as well; this stops the session loop and its history traffic
    from running for a strategy that can never dispatch.
    """
    if trade_mode == "analyze":
        return cfg
    kept = {}
    for tag, entry in cfg.strategies.items():
        if entry.paper_only:
            logger.critical(
                f"Python signal runner: {tag} is paper-only and OpenAlgo is in {trade_mode} "
                f"mode, so it is NOT started"
            )
            continue
        kept[tag] = entry
    return replace(cfg, strategies=kept)


def make_broker_qty(tracker):
    """Exit-driver broker check: how many shares of `pos` the broker still holds.

    Counts only this strategy's share of the symbol's net broker position, as a magnitude
    (the driver reads a negative as "unknown", and a short is a real negative). -1 = unknown.
    """

    async def _broker_qty(pos) -> int:
        own = await tracker.fetch_own_position(pos.symbol, pos.strategy, pos.exchange, pos.product)
        return -1 if own is None else abs(own)

    return _broker_qty


def _session_factory(
    cfg,
    tag,
    symbols,
    source,
    loader,
    state,
    dispatch,
    tracker,
    exit_qty,
    trade_mode,
    time_exit,
    halted,
    stale_limit_s,
    product,
    clock,
):
    def make(day: dt.date) -> Runner:
        strategy = STRATEGY_FACTORIES[tag]()
        context = RollingContext(strategy, loader)
        return Runner(
            clock=clock,
            source=source,
            sink=build_sink(cfg, tag, dispatch),
            strategy=strategy,
            context=context,
            state=state,
            cfg=cfg,
            symbols=strategy_symbols(cfg, tag, symbols),
            mode=trade_mode,
            product=product,
            stale_limit_s=stale_limit_s,
            halted=halted,
            price_band=pretrade.price_band(tag),
            exits=build_exits(
                cfg, tag, tracker, exit_qty, strategy, context, manage_until_min(*time_exit)
            ),
            manage_until_min=manage_until_min(*time_exit),
        )

    return make
