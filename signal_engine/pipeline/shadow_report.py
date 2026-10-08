"""Shadow-vs-Pine comparison for one day. Informational only: nothing here trades or gates.

The shadow runner (a strategy still on signal_source pine) records every signal it would have
sent and every fire or bar it skipped, with the reason, in the runner state store. Pine's own
BREAKOUT alerts land in trades.db. This module lines the two up by symbol, day and ARMING bar
open and says, for every disagreement, why. Pine stamps a row one bar after the arming bar (the
fill bar), so Pine rows are shifted back by PINE_BAR_STAMP_OFFSET before matching; the raw,
unshifted exact-match count is reported alongside.

    uv run python -m signal_engine.pipeline.shadow_report --day 2026-09-30
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field

import pandas as pd

from signal_engine.pipeline.state import STATE_DB_PATH, RunnerStateStore, ShadowEvent
from signal_engine.timeutils import IST

_SIG_ID = re.compile(r"^(?P<symbol>.+)-(?P<day>\d{8})-(?P<hm>\d{4})$")

# Pine stamps its sig_id with the bar it FILLS on (orbEntrySignalTime is set in the pending-fill
# block, one bar after the setup arms); the runner stamps the bar the setup armed on. Verified
# 2026-10-07 on PFC and AXISBANK: same entry, stop and target, Pine stamp one 5-minute bar later.
PINE_BAR_STAMP_OFFSET = pd.Timedelta(minutes=5)


@dataclass(frozen=True)
class PineSignal:
    sig_id: str
    symbol: str
    direction: str  # LONG | SHORT
    bar_time: pd.Timestamp
    status: str  # SUCCESS | DECLINED | FAILED ...
    message: str


@dataclass
class Comparison:
    matched: list[ShadowEvent] = field(default_factory=list)
    direction_mismatch: list[tuple[ShadowEvent, PineSignal]] = field(default_factory=list)
    runner_only: list[ShadowEvent] = field(default_factory=list)
    pine_only: list[tuple[PineSignal, str]] = field(default_factory=list)
    raw_exact_matches: int = 0


def _default_trades_db() -> str:
    from signal_engine import db

    return db._DB_PATH


def load_pine_signals(
    day: dt.date, strategy: str = "BREAKOUT", trades_db: str | None = None
) -> list[PineSignal]:
    """Pine entry rows (LONG or SHORT, DECLINED included) for the day, from trades.db read-only."""
    path = trades_db or _default_trades_db()
    if not os.path.exists(path):
        raise FileNotFoundError(f"trades db not found: {path}")
    with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)) as conn:
        rows = conn.execute(
            "SELECT sig_id, symbol, direction, status, COALESCE(message, '') FROM trades "
            "WHERE strategy = ? AND direction IN ('LONG', 'SHORT') AND sig_id LIKE ? ORDER BY id",
            (strategy, f"%-{day:%Y%m%d}-%"),
        ).fetchall()
    out: list[PineSignal] = []
    for sig_id, symbol, direction, status, message in rows:
        m = _SIG_ID.match(sig_id or "")
        if m is None:
            continue
        bar = pd.Timestamp(f"{day} {m['hm'][:2]}:{m['hm'][2:]}", tz=IST)
        out.append(PineSignal(sig_id, symbol, direction, bar, status or "", message))
    return out


def _arming_bar(pine: PineSignal, offset: pd.Timedelta) -> pd.Timestamp:
    """The bar Pine's setup was armed on, i.e. the runner's decision bar."""
    return pine.bar_time - offset


def _arming_key(pine: PineSignal, offset: pd.Timedelta) -> str:
    return f"{pine.symbol}-{pine.bar_time:%Y%m%d}-{_arming_bar(pine, offset):%H%M}"


def _pine_only_reason(
    pine: PineSignal,
    universe: set[str],
    skips: dict[tuple[str, pd.Timestamp], str],
    last_bars: dict[str, pd.Timestamp],
    offset: pd.Timedelta,
) -> str:
    if universe and pine.symbol not in universe:
        return "outside the runner universe"
    arming = _arming_bar(pine, offset)
    reason = skips.get((pine.symbol, arming))
    if reason:
        return f"runner skipped it: {reason}"
    last = last_bars.get(pine.symbol)
    if last is not None and last >= arming:
        return "runner evaluated the bar and did not fire"
    return "runner did not process this bar (engine down, or symbol never evaluated)"


def _pine_shaped(sig_id: str, runner_strategy: str | None) -> str:
    """The runner's key with its strategy prefix removed, so it lines up with a Pine row."""
    prefix = f"{runner_strategy}-" if runner_strategy else ""
    return sig_id[len(prefix):] if prefix and sig_id.startswith(prefix) else sig_id


def compare(
    events: list[ShadowEvent],
    pine: list[PineSignal],
    *,
    universe: set[str],
    last_bars: dict[str, pd.Timestamp],
    runner_strategy: str | None = None,
    pine_bar_offset: pd.Timedelta = PINE_BAR_STAMP_OFFSET,
) -> Comparison:
    """Line Pine rows up with runner signals on the arming bar.

    Pine stamps a row with the bar it fills on, one bar after the arming bar the runner stamps,
    so each Pine row is shifted back by `pine_bar_offset` before matching. A row that still does
    not line up after the shift is a divergence. `raw_exact_matches` counts Pine rows whose
    as-stamped key equals a runner key (no shift), so the unadjusted figure is never lost.
    """
    signals = {_pine_shaped(e.sig_id, runner_strategy): e for e in events if e.kind == "signal"}
    skips = {(e.symbol, e.bar_time): e.reason for e in events if e.kind == "skip"}
    rep = Comparison()
    matched_keys: set[str] = set()
    for p in pine:
        raw = signals.get(p.sig_id)
        if raw is not None and raw.direction == p.direction:
            rep.raw_exact_matches += 1
        key = _arming_key(p, pine_bar_offset)
        ours = signals.get(key)
        if ours is None:
            reason = _pine_only_reason(p, universe, skips, last_bars, pine_bar_offset)
            rep.pine_only.append((p, reason))
            continue
        matched_keys.add(key)
        if ours.direction == p.direction:
            rep.matched.append(ours)
        else:
            rep.direction_mismatch.append((ours, p))
    rep.runner_only = [e for sid, e in signals.items() if sid not in matched_keys]
    return rep


def format_report(rep: Comparison, day: dt.date, strategy: str) -> str:
    lines = [
        f"Shadow vs Pine, {strategy} {day}: matched {len(rep.matched)}, "
        f"direction-mismatch {len(rep.direction_mismatch)}, runner-only {len(rep.runner_only)}, "
        f"pine-only {len(rep.pine_only)}; raw exact (no bar shift) {rep.raw_exact_matches}"
    ]
    for e in rep.matched:
        lines.append(f"  matched     {e.sig_id} {e.direction}")
    for ours, p in rep.direction_mismatch:
        lines.append(f"  direction   {p.sig_id} runner {ours.direction} vs pine {p.direction}")
    for e in rep.runner_only:
        lines.append(f"  runner-only {e.sig_id} {e.direction} (Pine sent nothing for this bar)")
    for p, why in rep.pine_only:
        lines.append(f"  pine-only   {p.sig_id} {p.direction} [{p.status}]: {why}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Shadow runner vs Pine BREAKOUT for one day.")
    ap.add_argument("--day", type=dt.date.fromisoformat, default=dt.datetime.now(IST).date())
    ap.add_argument("--strategy", default="BREAKOUT", help="the Pine strategy tag in trades.db")
    ap.add_argument(
        "--runner-strategy",
        default=None,
        help="the runner tag to read events for (default: same as --strategy), e.g. BREAKOUT-PY",
    )
    ap.add_argument(
        "--pine-bar-offset-minutes",
        type=int,
        default=int(PINE_BAR_STAMP_OFFSET.total_seconds() // 60),
        help="minutes Pine's sig_id bar sits after the runner's arming bar (default 5, 0 = none)",
    )
    ap.add_argument("--state-db", default=STATE_DB_PATH)
    ap.add_argument("--trades-db", default=None)
    ap.add_argument("--symbols", nargs="*", help="override the runner universe from config")
    return ap.parse_args(argv)


def _universe(args: argparse.Namespace) -> set[str]:
    if args.symbols:
        return set(args.symbols)
    from signal_engine.config import settings

    universe = settings.runner.universe
    return set(universe) if isinstance(universe, tuple) else set()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    runner = args.runner_strategy or args.strategy
    pine = load_pine_signals(args.day, args.strategy, args.trades_db)
    with RunnerStateStore(args.state_db) as store:
        events = store.shadow_events(args.day, runner)
        last_bars = store.last_bars(args.day, runner)
    rep = compare(
        events, pine, universe=_universe(args), last_bars=last_bars, runner_strategy=runner,
        pine_bar_offset=pd.Timedelta(minutes=args.pine_bar_offset_minutes),
    )
    label = args.strategy if runner == args.strategy else f"{runner} vs {args.strategy}"
    sys.stdout.write(format_report(rep, args.day, label) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
