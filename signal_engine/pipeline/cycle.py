"""One shared cycle: fetch closed bars, gate, evaluate, dedupe, submit.

The cycle is the same in replay, shadow, paper and live; only the injected source, clock and
sink differ. Every skipped symbol is logged by name with a reason and returned in the report.
A symbol that raises is isolated: the rest of the batch still runs.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import pandas as pd
from loguru import logger

from signal_engine.config import RunnerSettings
from signal_engine.pipeline import pretrade
from signal_engine.pipeline.bars import BarSource, fetch_current
from signal_engine.pipeline.clock import Clock
from signal_engine.pipeline.context import RollingContext
from signal_engine.pipeline.exit_driver import ExitDriver
from signal_engine.pipeline.signals import fire_to_signal, sig_id
from signal_engine.pipeline.sink import ExecutionSink
from signal_engine.pipeline.state import RunnerStateStore
from signal_engine.pipeline.strategy import Fire, Strategy

#: Threads evaluating strategy windows. One shared pool for the process, never per call.
_MAX_WORKERS = 4
_executor: ThreadPoolExecutor | None = None


def shared_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="pipeline")
    return _executor


def shutdown_executor() -> None:
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=False)
        _executor = None


@dataclass
class CycleDeps:
    strategy: Strategy
    source: BarSource
    context: RollingContext
    sink: ExecutionSink
    state: RunnerStateStore
    clock: Clock
    symbols: list[str]
    cfg: RunnerSettings
    mode: str  # "analyze" or "live"
    product: str
    stale_limit_s: float
    halted: Callable[[], bool]
    price_band: tuple[float, float]
    executor: ThreadPoolExecutor | None = None
    exits: ExitDriver | None = (
        None  # steps the shared Ladder for open positions; None = entries only
    )


@dataclass
class CycleReport:
    bar_close: pd.Timestamp
    evaluated: int = 0
    submitted: int = 0
    fires: list[Fire] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    late: list[str] = field(default_factory=list)
    stale: bool = False
    halted: bool = False
    duration_s: float = 0.0


def _skip(
    report: CycleReport,
    bar_open: pd.Timestamp,
    symbol: str,
    reason: str,
    deps: CycleDeps | None = None,
) -> None:
    report.skipped[symbol] = reason
    logger.info(f"pipeline skip {symbol} bar {bar_open:%H:%M}: {reason}")
    if deps is not None and _records_events(deps):
        deps.state.record_shadow(
            bar_open.date(), deps.strategy.name, symbol, bar_open, "skip", reason=reason
        )


def _shadow(deps: CycleDeps) -> bool:
    """True when this cycle feeds the shadow-vs-Pine comparison (a sink that only records)."""
    return bool(getattr(deps.sink, "records_shadow", False))


def _records_events(deps: CycleDeps) -> bool:
    """True when the runner's own signal and skip events are written for the comparison report.

    A recording (shadow) sink always does. A dispatching sink does too, so a strategy that
    trades in paper (BREAKOUT-PY) can be lined up against Pine the same way. Only the shadow
    sink ignores trades.db for dedupe (see _shadow); a dispatching one must respect it.
    """
    return _shadow(deps) or bool(getattr(deps.sink, "dispatches", False))


async def run_cycle(bar_close: pd.Timestamp, deps: CycleDeps) -> CycleReport:
    started = time.monotonic()
    report = CycleReport(bar_close=bar_close)
    bar_open = bar_close - pd.Timedelta(minutes=deps.strategy.interval_min)
    day = bar_open.date()

    if deps.halted():
        report.halted = True
        logger.warning(f"pipeline cycle {bar_close:%H:%M} skipped: engine halted")
        return report

    loop = asyncio.get_running_loop()
    pool = deps.executor or shared_executor()

    await loop.run_in_executor(pool, deps.context.warm, deps.symbols, day)
    for s in deps.symbols:
        if s in deps.context.failed:
            _skip(report, bar_open, s, "warm-up failed", deps)
    active = [s for s in deps.symbols if s in deps.context.warmed]
    if (
        deps.exits is not None
        and bar_open.hour * 60 + bar_open.minute >= deps.strategy.entry_window_min[1]
    ):
        # Entries have closed; only open positions and orders still to fill need bars.
        active = [s for s in active if deps.exits.holds(s) or deps.sink.has_pending(s)]
        if not active:
            return report

    frames, late = await fetch_current(
        deps.source,
        active,
        deps.strategy.interval_min,
        day,
        deps.clock,
        bar_close,
        deps.cfg.cycle_deadline_seconds,
        deps.cfg.retry_backoff_seconds,
    )
    report.late = late
    for s in late:
        _skip(report, bar_open, s, "bar late (not published by the cycle deadline)", deps)

    lag = (deps.clock.now() - bar_close).total_seconds()
    if pretrade.is_stale(bar_close, deps.clock.now(), deps.stale_limit_s):
        report.stale = True
        logger.warning(
            f"pipeline cycle {bar_close:%H:%M} stale: data {lag:.0f}s after the bar closed "
            f"(limit {deps.stale_limit_s:.0f}s); {len(frames)} symbol(s) not evaluated"
        )
        if _records_events(deps):
            for s in frames:
                deps.state.record_shadow(
                    day, deps.strategy.name, s, bar_open, "skip",
                    reason=f"cycle stale ({lag:.0f}s after the bar closed)",
                )
        return report

    async def one(symbol: str) -> None:
        try:
            await _process(symbol, frames[symbol], bar_open, day, deps, pool, loop, report)
        except Exception:  # noqa: BLE001 - isolate one symbol; the batch must continue
            logger.exception(f"pipeline {symbol} bar {bar_open:%H:%M}: evaluate failed")
            report.skipped[symbol] = "evaluate error"

    await asyncio.gather(*(one(s) for s in frames))
    report.duration_s = time.monotonic() - started
    logger.info(
        f"pipeline cycle {bar_close:%H:%M}: evaluated {report.evaluated}, fires "
        f"{len(report.fires)}, submitted {report.submitted}, skipped {len(report.skipped)}, "
        f"late {len(report.late)} in {report.duration_s:.2f}s"
    )
    return report


async def _process(symbol, df, bar_open, day, deps, pool, loop, report) -> None:
    strategy = deps.strategy.name
    last = deps.state.last_bar(deps.mode, day, strategy, symbol)
    if last is not None and last >= bar_open:
        _skip(report, bar_open, symbol, "bar already processed")
        return

    if not await _manage_open_position(symbol, df, bar_open, deps, report):
        return

    fires = await loop.run_in_executor(pool, deps.context.evaluate, symbol, df, None)
    report.evaluated += 1
    current = [f for f in fires if f.bar_time == bar_open]
    report.fires.extend(current)
    for fire in current:
        await _dispatch(fire, bar_open, deps, report)
    deps.state.mark(deps.mode, day, strategy, symbol, bar_open)


async def _dispatch(fire: Fire, bar_open, deps: CycleDeps, report: CycleReport) -> None:
    symbol = fire.symbol
    if not pretrade.in_entry_window(bar_open, deps.strategy.entry_window_min):
        _skip(report, bar_open, symbol, "outside the entry window", deps)
        return
    why = pretrade.ineligible_reason(fire.entry_ref, *deps.price_band)
    if why:
        _skip(report, bar_open, symbol, why, deps)
        return
    signal = fire_to_signal(fire, deps.product, strategy=deps.strategy.name)
    verdict = pretrade.check(signal)
    if verdict is not None:
        _skip(report, bar_open, symbol, verdict.reason, deps)
        return
    key = sig_id(symbol, fire.bar_time, deps.strategy.name)
    if deps.state.seen_sig_id(key, include_trades=not _shadow(deps)):
        _skip(report, bar_open, symbol, f"sig_id already seen ({key})", deps)
        return
    if deps.halted():
        _skip(report, bar_open, symbol, "engine halted", deps)
        return
    try:
        await deps.sink.submit(signal)
    except Exception:  # noqa: BLE001 - a sink failure is reported, never swallowed
        logger.exception(f"pipeline {symbol}: submit failed for {key}")
        report.skipped[symbol] = "submit error"
        return
    deps.state.record_sig_id(key)
    if _records_events(deps):
        deps.state.record_shadow(
            bar_open.date(), deps.strategy.name, symbol, fire.bar_time, "signal",
            direction=signal.direction.value, sig_id=key,
            entry=signal.entry, sl=signal.sl, tp=signal.tp,
        )
    report.submitted += 1


async def _manage_open_position(symbol, df, bar_open, deps: CycleDeps, report: CycleReport) -> bool:
    """Tell the sink about the closed bar, then step the exit driver. False = stop this symbol."""
    if bar_open not in df.index:
        _skip(report, bar_open, symbol, "closed bar missing from the frame")
        return False
    row = df.loc[bar_open]
    o, h, low, c = (float(row[k]) for k in ("Open", "High", "Low", "Close"))
    await deps.sink.on_bar(symbol, bar_open, o, h, low, c)
    if deps.exits is not None:
        for exit_signal in await deps.exits.on_bar(symbol, bar_open, o, h, low, c):
            try:
                await deps.sink.submit(exit_signal)
            except Exception:  # noqa: BLE001 - a sink failure is reported, never swallowed
                logger.exception(f"pipeline {symbol}: exit submit failed ({exit_signal.tp_level})")
                report.skipped[symbol] = "exit submit error"
                return False
    await deps.sink.after_bar(symbol, bar_open, o, h, low, c)
    return True
