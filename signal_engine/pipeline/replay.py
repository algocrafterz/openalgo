"""Bar-by-bar replay through the shared runner, plus the P1/P2 parity checks.

`replay_day` drives the SAME Runner, cycle, context and gates that paper and live use, with a
ReplaySource, a SimClock and a RecordingSink swapped in. So a replayed day is the pipeline
itself run offline, not a second implementation of it.

P1 (plumbing): the fires the replay walk produces equal the strategy evaluated once over the
same window and the whole day. P2 (window sensitivity): the rolling warm-up window fires equal
a long-history evaluation, which is what a full-history backtest sees.

    uv run python -m signal_engine.pipeline.replay --day 2026-09-29 --limit 10
    uv run python -m signal_engine.pipeline.replay --parity --days 20 --limit 10
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from dataclasses import dataclass, field

import pandas as pd
from loguru import logger

from signal_engine.config import settings
from signal_engine.features.session import continuous_session
from signal_engine.pipeline import pretrade
from signal_engine.pipeline.bars import ReplaySource
from signal_engine.pipeline.clock import SimClock
from signal_engine.pipeline.context import WARMUP_SESSIONS_1M, RollingContext
from signal_engine.pipeline.cycle import CycleReport
from signal_engine.pipeline.runner import Runner
from signal_engine.pipeline.sink import RecordingSink
from signal_engine.pipeline.state import RunnerStateStore
from signal_engine.pipeline.strategies.breakout import BreakoutStrategy
from signal_engine.pipeline.strategy import Fire, Strategy
from signal_engine.timeutils import IST, ist_today

_PRICE_TOL = 1e-6


class InMemoryLoader:
    """Warm-up history sliced from frames already in memory (no Historify call)."""

    def __init__(self, frames5: dict[str, pd.DataFrame], frames1: dict[str, pd.DataFrame]) -> None:
        self._f5, self._f1 = frames5, frames1

    def load(self, symbols, day, sessions5, sessions1):
        h5, h1, failed = {}, {}, []
        for s in symbols:
            if s not in self._f5 or s not in self._f1:
                failed.append(s)
                continue
            h5[s] = _prior(self._f5[s], day, sessions5)
            h1[s] = _prior(self._f1[s], day, sessions1)
        return h5, h1, failed


def _prior(df: pd.DataFrame, day: dt.date, n: int) -> pd.DataFrame:
    before = df[df.index.date < day]
    keep = sorted(set(before.index.date))[-n:]
    return before[pd.Index(before.index.date).isin(keep)]


def _today(df: pd.DataFrame, day: dt.date) -> pd.DataFrame:
    return df[df.index.date == day]


@dataclass
class ReplayResult:
    fires: list[Fire] = field(default_factory=list)
    signals: list = field(default_factory=list)
    reports: list[CycleReport] = field(default_factory=list)


def replay_day(
    strategy: Strategy,
    day: dt.date,
    symbols: list[str],
    frames5: dict[str, pd.DataFrame],
    frames1: dict[str, pd.DataFrame],
    band: tuple[float, float] = (0.0, 0.0),
    product: str = "MIS",
) -> ReplayResult:
    """Run one session through the shared runner in shadow mode."""
    today5 = {s: _today(frames5[s], day) for s in symbols if s in frames5}
    state = RunnerStateStore(":memory:", trades_lookup=lambda _sig_id: False)
    sink = RecordingSink()
    try:
        runner = Runner(
            clock=SimClock(pd.Timestamp(f"{day} 09:00", tz=IST)),
            source=ReplaySource(today5),
            sink=sink,
            strategy=strategy,
            context=RollingContext(strategy, InMemoryLoader(frames5, frames1)),
            state=state,
            cfg=settings.runner,
            symbols=list(symbols),
            mode="replay",
            product=product,
            stale_limit_s=settings.stale_signal_seconds,
            halted=lambda: False,
            price_band=band,
        )
        asyncio.run(runner.run(day))
    finally:
        state.close()
    return ReplayResult(runner.fires, sink.signals, runner.reports)


def windowed_reference(
    strategy: Strategy, symbol: str, day: dt.date, frames5, frames1
) -> list[Fire]:
    """The strategy evaluated ONCE over warm-up plus the whole day: what the walk must equal."""
    prior5 = _prior(frames5[symbol], day, strategy.warmup_sessions)
    prior1 = continuous_session(_prior(frames1[symbol], day, WARMUP_SESSIONS_1M))
    d5 = pd.concat([prior5, _today(frames5[symbol], day)])
    fires = strategy.evaluate(symbol, d5, prior1)
    return [f for f in fires if f.bar_time.date() == day]


def _key(f: Fire) -> tuple:
    return (f.symbol, f.bar_time.value, f.direction, f.tag)


@dataclass(frozen=True)
class FireDiff:
    only_expected: list[Fire]
    only_got: list[Fire]

    @property
    def identical(self) -> bool:
        return not self.only_expected and not self.only_got


def compare_fires(expected: list[Fire], got: list[Fire]) -> FireDiff:
    """Set comparison on (symbol, bar, side, setup); a price beyond tolerance is a mismatch too."""

    def sig(f: Fire) -> tuple:
        return _key(f) + (round(f.entry_ref / _PRICE_TOL * 1e-3), round(f.sl / _PRICE_TOL * 1e-3))

    e = {sig(f): f for f in expected}
    g = {sig(f): f for f in got}
    return FireDiff(
        [f for k, f in e.items() if k not in g], [f for k, f in g.items() if k not in e]
    )


# ---- P1 / P2 report on real data ------------------------------------------------------------


@dataclass
class ParityReport:
    symbol_days: int = 0
    p1_identical: int = 0
    p2_identical: int = 0
    expected_fires: int = 0
    p2_mismatched_fires: int = 0
    p2_union_fires: int = 0
    mismatches: list[str] = field(default_factory=list)

    @property
    def p2_fire_mismatch_rate(self) -> float:
        return self.p2_mismatched_fires / self.p2_union_fires if self.p2_union_fires else 0.0


def run_parity(
    strategy: Strategy,
    symbols: list[str],
    days: list[dt.date],
    frames5: dict[str, pd.DataFrame],
    frames1: dict[str, pd.DataFrame],
    long5: dict[str, pd.DataFrame] | None = None,
    long1: dict[str, pd.DataFrame] | None = None,
) -> ParityReport:
    """P1 against the windowed reference; P2 against a long-history evaluation (long*)."""
    long5, long1 = long5 or frames5, long1 or frames1
    full: dict[str, list[Fire]] = {s: strategy.evaluate(s, long5[s], long1[s]) for s in symbols}
    rep = ParityReport()
    for day in days:
        res = replay_day(strategy, day, symbols, frames5, frames1)
        for s in symbols:
            got = [f for f in res.fires if f.symbol == s]
            p1 = compare_fires(windowed_reference(strategy, s, day, frames5, frames1), got)
            want = [f for f in full[s] if f.bar_time.date() == day]
            p2 = compare_fires(want, got)
            rep.symbol_days += 1
            rep.p1_identical += p1.identical
            rep.p2_identical += p2.identical
            rep.expected_fires += len(want)
            rep.p2_union_fires += len(want) + len(p2.only_got)
            rep.p2_mismatched_fires += len(p2.only_expected) + len(p2.only_got)
            if not p1.identical:
                rep.mismatches.append(f"P1 {s} {day}: {_names(p1)}")
            if not p2.identical:
                rep.mismatches.append(f"P2 {s} {day}: {_names(p2)}")
    return rep


def _names(diff: FireDiff) -> str:
    fmt = lambda fs: ", ".join(f"{f.bar_time:%H:%M} {f.tag}" for f in fs) or "-"  # noqa: E731
    return f"only full-history [{fmt(diff.only_expected)}] only rolling [{fmt(diff.only_got)}]"


def format_report(rep: ParityReport) -> str:
    n = rep.symbol_days or 1
    lines = [
        f"symbol-days evaluated : {rep.symbol_days}",
        f"fires (full history)  : {rep.expected_fires}",
        f"P1 identical          : {rep.p1_identical}/{rep.symbol_days} ({rep.p1_identical / n:.2%})",
        f"P2 identical          : {rep.p2_identical}/{rep.symbol_days} ({rep.p2_identical / n:.2%})",
        f"P2 fire mismatch rate : {rep.p2_mismatched_fires}/{rep.p2_union_fires} "
        f"({rep.p2_fire_mismatch_rate:.2%})",
    ]
    lines += [f"  {m}" for m in rep.mismatches]
    return "\n".join(lines)


# ---- CLI ------------------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m signal_engine.pipeline.replay")
    p.add_argument("--day", type=dt.date.fromisoformat, help="session to replay (YYYY-MM-DD)")
    p.add_argument("--symbols", type=lambda v: [x for x in v.split(",") if x])
    p.add_argument("--limit", type=int, default=10, help="symbols to use when --symbols is absent")
    p.add_argument(
        "--parity", action="store_true", help="P1/P2 parity over the last --days sessions"
    )
    p.add_argument(
        "--trade-parity",
        action="store_true",
        help="P3: replay trades (shared cycle, ExitDriver, SimSink) vs engine.simulate",
    )
    p.add_argument("--plan", default="tp1_only", help="exit plan for --trade-parity")
    p.add_argument("--days", type=int, default=20)
    p.add_argument("--long-sessions", type=int, default=250, help="history for the P2 reference")
    return p.parse_args(argv)


def _load(symbols: list[str], start: dt.date, end: dt.date):
    from signal_engine.backtest import data

    span = {
        "symbols": symbols,
        "start": start.isoformat(),
        "end": (end + dt.timedelta(days=1)).isoformat(),
        "min_sessions": 1,
    }
    return data.from_historify(interval="5m", **span), data.from_historify(interval="1m", **span)


def _pick_symbols(args: argparse.Namespace) -> list[str]:
    if args.symbols:
        return args.symbols
    from signal_engine.backtest import data

    return data.historify_symbols("1m")[: args.limit]


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    symbols = _pick_symbols(args)
    strategy = BreakoutStrategy()
    if args.parity:
        return _parity(args, strategy, symbols)
    if args.trade_parity:
        from signal_engine.pipeline import trade_parity

        return trade_parity.cli(args, strategy, symbols, _load)
    if args.day is None:
        _out("--day or --parity is required")
        return 2
    f5, f1 = _load(symbols, args.day - dt.timedelta(days=60), args.day)
    usable = [s for s in symbols if s in f5 and s in f1]
    for s in set(symbols) - set(usable):
        logger.warning(f"replay: no Historify bars for {s}")
    res = replay_day(strategy, args.day, usable, f5, f1, band=pretrade.price_band(strategy.name))
    _out(
        f"{args.day}: {len(usable)} symbols, {len(res.reports)} cycles, "
        f"{len(res.fires)} fires, {len(res.signals)} would-be signals"
    )
    for f in sorted(res.fires, key=lambda f: f.bar_time):
        _out(
            f"  {f.bar_time:%H:%M} {f.symbol:<12} {'LONG' if f.direction == 1 else 'SHORT':<5} "
            f"{f.tag:<8} entry={f.entry_ref:.2f} sl={f.sl:.2f} tp1={f.targets[0]:.2f}"
        )
    skipped = {s: r for rep in res.reports for s, r in rep.skipped.items()}
    for s, reason in sorted(skipped.items()):
        _out(f"  skipped {s}: {reason}")
    return 0


def _parity(args: argparse.Namespace, strategy: Strategy, symbols: list[str]) -> int:
    end = args.day or ist_today() - dt.timedelta(days=1)
    long_start = end - dt.timedelta(days=int(args.long_sessions * 1.5))
    l5, l1 = _load(symbols, long_start, end)
    usable = [s for s in symbols if s in l5 and s in l1]
    sessions = sorted({d for s in usable for d in l5[s].index.date if d <= end})
    days = sessions[-args.days :]
    rep = run_parity(strategy, usable, days, l5, l1)
    _out(format_report(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
