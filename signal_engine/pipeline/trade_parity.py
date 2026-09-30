"""P3: the trades the shared pipeline produces equal engine.simulate's trades.

The replay side runs the real Runner, cycle, RollingContext, ExitDriver and shared Ladder with a
SimSink standing in for the broker; the engine side is backtest.engine.simulate with the same
ExitPlan. Same strategy object and same bars, so a difference is a difference in the code AFTER
the strategy: fills, stops, targets, the time exit and the per-day entry rules.

Tolerance: the engine casts bars to float32 (backtest.types.Ctx), so prices agree to about 1e-5
relative; 1e-4 x price is used, and r_gross to 2e-3.

Known gate differences are reported, not hidden: the pipeline runs the validator's pre-trade
checks (minimum reward:risk, blacklist, price band) that the engine does not model, so a trade
the engine takes can be absent from the replay with a named skip reason.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field

import pandas as pd

from signal_engine.backtest.types import Ctx, RunConfig, Trade
from signal_engine.config import settings
from signal_engine.pipeline.bars import ReplaySource
from signal_engine.pipeline.clock import SimClock
from signal_engine.pipeline.context import RollingContext
from signal_engine.pipeline.cycle import CycleReport
from signal_engine.pipeline.exit_driver import ExitDriver, plan_for
from signal_engine.pipeline.replay import InMemoryLoader, _today
from signal_engine.pipeline.runner import Runner
from signal_engine.pipeline.sim_sink import SimRules, SimSink
from signal_engine.pipeline.state import RunnerStateStore
from signal_engine.pipeline.strategy import Strategy
from signal_engine.timeutils import IST

PRICE_TOL = 1e-4  # relative to price
R_TOL = 2e-3
SAFETY_BARS = 2


@dataclass
class ReplayTrades:
    trades: list[Trade] = field(default_factory=list)
    reports: list[CycleReport] = field(default_factory=list)


def replay_trades(
    strategy: Strategy,
    days: list[dt.date],
    symbols: list[str],
    frames5: dict[str, pd.DataFrame],
    frames1: dict[str, pd.DataFrame],
    run: RunConfig,
    plan_name: str,
) -> ReplayTrades:
    """Run each session through the shared runner with a simulated broker."""
    out = ReplayTrades()
    rules = SimRules.from_run_config(run, strategy.interval_min)
    for day in days:
        sink = SimSink(rules, strategy.interval_min)
        state = RunnerStateStore(":memory:", trades_lookup=lambda _sig_id: False)
        try:
            exits = ExitDriver(
                strategy=strategy.name,
                plan=plan_for(plan_name),
                positions=sink,
                broker_qty=_sim_qty(sink),
                interval_min=strategy.interval_min,
                safety_bars=SAFETY_BARS,
            )
            runner = Runner(
                clock=SimClock(pd.Timestamp(f"{day} 09:00", tz=IST)),
                source=ReplaySource({s: _today(frames5[s], day) for s in symbols if s in frames5}),
                sink=sink,
                strategy=strategy,
                context=RollingContext(strategy, InMemoryLoader(frames5, frames1)),
                state=state,
                cfg=settings.runner,
                symbols=list(symbols),
                mode="replay",
                product="MIS",
                stale_limit_s=settings.stale_signal_seconds,
                halted=lambda: False,
                price_band=(0.0, 0.0),
                exits=exits,
                # the bar that opens AT the time exit still closes the position, as in the engine
                manage_until_min=rules.time_exit_min + strategy.interval_min,
            )
            asyncio.run(runner.run(day))
            sink.flush()
        finally:
            state.close()
        out.trades.extend(sink.trades)
        out.reports.extend(runner.reports)
    return out


def _sim_qty(sink: SimSink):
    async def qty(pos) -> int:
        held = sink.find_position(pos.symbol, pos.strategy)
        return 0 if held is None else held.quantity

    return qty


def engine_trades(
    adapter,
    params,
    symbols: list[str],
    frames5: dict[str, pd.DataFrame],
    days: list[dt.date],
    run: RunConfig,
    plan_name: str,
) -> list[Trade]:
    """engine.simulate over the full history, the trades of `days` only."""
    from signal_engine.backtest.engine import simulate

    wanted = set(days)
    run = run.with_(exit_plan=plan_for(plan_name))
    out: list[Trade] = []
    for s in symbols:
        ctx = Ctx(adapter.prepare_for(s, frames5[s], params), symbol=s)
        out.extend(
            t for t in simulate(ctx, adapter, params, run) if pd.Timestamp(t.day).date() in wanted
        )
    return out


# ---- comparison ------------------------------------------------------------------------------


def _key(t: Trade) -> tuple:
    return (t.symbol, pd.Timestamp(t.entry_time), t.direction)


@dataclass
class TradeDiff:
    matched: int = 0
    engine_only: list[Trade] = field(default_factory=list)
    replay_only: list[Trade] = field(default_factory=list)
    differing: list[tuple[Trade, Trade, str]] = field(default_factory=list)

    @property
    def identical(self) -> bool:
        return not (self.engine_only or self.replay_only or self.differing)


def _why_differs(e: Trade, r: Trade) -> str | None:
    tol = PRICE_TOL * abs(e.entry)
    problems = []
    if abs(e.entry - r.entry) > tol:
        problems.append(f"entry {e.entry:.4f} vs {r.entry:.4f}")
    if abs(e.exit - r.exit) > tol:
        problems.append(f"exit {e.exit:.4f} vs {r.exit:.4f}")
    if e.reason != r.reason:
        problems.append(f"reason {e.reason} vs {r.reason}")
    if pd.Timestamp(e.exit_time) != pd.Timestamp(r.exit_time):
        problems.append(
            f"exit_time {pd.Timestamp(e.exit_time):%H:%M} vs {pd.Timestamp(r.exit_time):%H:%M}"
        )
    if abs(e.r_gross - r.r_gross) > R_TOL * max(1.0, abs(e.r_gross)):
        problems.append(f"r_gross {e.r_gross:.4f} vs {r.r_gross:.4f}")
    if e.legs != r.legs:
        problems.append(f"legs {e.legs} vs {r.legs}")
    return "; ".join(problems) or None


def compare_trades(engine: list[Trade], replay: list[Trade]) -> TradeDiff:
    eng = {_key(t): t for t in engine}
    rep = {_key(t): t for t in replay}
    diff = TradeDiff()
    for key, e in eng.items():
        r = rep.get(key)
        if r is None:
            diff.engine_only.append(e)
            continue
        why = _why_differs(e, r)
        if why:
            diff.differing.append((e, r, why))
        else:
            diff.matched += 1
    diff.replay_only = [t for k, t in rep.items() if k not in eng]
    return diff


def _describe(t: Trade) -> str:
    return (
        f"{t.symbol} {pd.Timestamp(t.entry_time):%Y-%m-%d %H:%M} dir={t.direction} "
        f"entry={t.entry:.2f} exit={t.exit:.2f} {t.reason}"
    )


def skip_reason(t: Trade, reports: list[CycleReport], interval_min: int) -> str:
    """Why the replay did not take a trade: the cycle's skip reason on the signal bar, if any."""
    close = pd.Timestamp(t.entry_time)  # signal bar closes when the fill bar opens
    for r in reports:
        if r.bar_close == close and t.symbol in r.skipped:
            return r.skipped[t.symbol]
    return "no skip recorded (sink gate or no fire)"


def format_diff(
    diff: TradeDiff, reports: list[CycleReport] | None = None, interval_min: int = 5
) -> str:
    lines = [
        f"matched {diff.matched}, engine only {len(diff.engine_only)}, "
        f"replay only {len(diff.replay_only)}, differing {len(diff.differing)}"
    ]
    for t in diff.engine_only:
        why = f" [{skip_reason(t, reports, interval_min)}]" if reports is not None else ""
        lines.append(f"  engine only: {_describe(t)}{why}")
    for t in diff.replay_only:
        lines.append(f"  replay only: {_describe(t)}")
    for e, _r, why in diff.differing:
        lines.append(f"  differs: {_describe(e)}: {why}")
    return "\n".join(lines)


def cli(args, strategy: Strategy, symbols: list[str], load) -> int:
    """`replay --trade-parity`: P3 on real Historify bars, report printed to stdout."""
    import sys

    from signal_engine.backtest.strategies.breakout import Breakout
    from signal_engine.strategy_core.breakout import BreakoutParams

    end = args.day or dt.date.today() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=int(args.long_sessions * 1.5))
    f5, f1 = load(symbols, start, end)
    usable = [s for s in symbols if s in f5 and s in f1]
    sessions = sorted({d for s in usable for d in f5[s].index.date if d <= end})
    days = sessions[-args.days :]
    adapter = Breakout(minute_source=lambda s, _d: f1[s].astype("float64"))
    run = RunConfig().with_(**adapter.run_overrides)
    eng = engine_trades(adapter, BreakoutParams(), usable, f5, days, run, args.plan)
    rep = replay_trades(strategy, days, usable, f5, f1, run, args.plan)
    diff = compare_trades(eng, rep.trades)
    sys.stdout.write(
        f"P3 {args.plan}: {len(usable)} symbols, {len(days)} sessions, engine {len(eng)} trades, "
        f"replay {len(rep.trades)} trades\n{format_diff(diff, rep.reports, strategy.interval_min)}\n"
    )
    return 0 if diff.identical else 1
