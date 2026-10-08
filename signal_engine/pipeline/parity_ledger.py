"""Pine versus Python parity over many sessions, against the frozen BREAKOUT v1.0 specification.

`shadow_report` compares one day. This module adds up the days, classifies every disagreement, applies
the migration gate and always keeps the raw numbers. Informational only: nothing here trades or gates a
trade. Pine is a migration oracle, not the specification: a Pine difference is classified and is never a
reason to change the spec.

Gate (owner decision 2026-10-06): observed parity at least 95 percent AND the Wilson lower bound at
least 90 percent AND at least 20 sessions AND a meaningful signal count (200). The observed rate counts
UNEXPLAINED divergences only (implementation and timing); a difference that the frozen spec or the
data explains is classified, listed and kept out of the rate, but never hidden.

    uv run python -m signal_engine.pipeline.parity_ledger --from 2026-10-01 --to 2026-10-08
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sys
from dataclasses import dataclass, field

import pandas as pd

from signal_engine.pipeline import shadow_report as sr
from signal_engine.pipeline.state import STATE_DB_PATH, RunnerStateStore
from signal_engine.timeutils import IST

MIN_SESSIONS = 20
MIN_SIGNALS = 200
MIN_OBSERVED = 0.95
MIN_WILSON_LOWER = 0.90
WILSON_Z = 1.96  # 95 percent two-sided

# Classes of a divergence. EXPLAINED ones stay out of the observed rate and are always listed.
MATCHED = "MATCHED"
UNEXPLAINED_DIRECTION = "DIRECTION_MISMATCH"
UNEXPLAINED_PINE_ONLY = "PINE_ONLY_UNEXPLAINED"
UNEXPLAINED_RUNNER_ONLY = "RUNNER_ONLY_UNEXPLAINED"
UNIVERSE = "UNIVERSE"  # a symbol outside the runner's universe
SPEC_RULE = "SPEC_RULE"  # the runner declined it under a frozen spec rule (window, policy, price band)
OUTAGE = "OUTAGE"  # the runner did not process the bar (engine down, symbol never evaluated)
UNEXPLAINED = frozenset({UNEXPLAINED_DIRECTION, UNEXPLAINED_PINE_ONLY, UNEXPLAINED_RUNNER_ONLY})

_SPEC_SKIP_MARKERS = (
    "outside the entry window",
    "sig_id already seen",
    "engine halted",
    "price",
)


def wilson_lower(successes: int, trials: int, z: float = WILSON_Z) -> float:
    """Lower end of the Wilson score interval for a proportion. 0.0 when there are no trials."""
    if trials <= 0:
        return 0.0
    p = successes / trials
    denom = 1 + z * z / trials
    centre = p + z * z / (2 * trials)
    margin = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))
    return max(0.0, (centre - margin) / denom)


def classify_pine_only(reason: str) -> str:
    """The class of a Pine row the runner did not match, from shadow_report's reason text."""
    if reason.startswith("outside the runner universe"):
        return UNIVERSE
    if reason.startswith("runner skipped it"):
        return SPEC_RULE if any(m in reason for m in _SPEC_SKIP_MARKERS) else UNEXPLAINED_PINE_ONLY
    if reason.startswith("runner did not process"):
        return OUTAGE
    return UNEXPLAINED_PINE_ONLY  # "runner evaluated the bar and did not fire"


@dataclass
class DayParity:
    day: dt.date
    counts: dict[str, int] = field(default_factory=dict)
    raw_exact_matches: int = 0
    pine_signals: int = 0
    runner_signals: int = 0
    details: list[tuple[str, str]] = field(default_factory=list)  # (class, line)


def day_parity(rep: sr.Comparison, day: dt.date, pine_signals: int, runner_signals: int) -> DayParity:
    """Classify one day's comparison."""
    out = DayParity(day, raw_exact_matches=rep.raw_exact_matches, pine_signals=pine_signals,
                    runner_signals=runner_signals)

    def add(cls: str, line: str) -> None:
        out.counts[cls] = out.counts.get(cls, 0) + 1
        out.details.append((cls, line))

    for e in rep.matched:
        add(MATCHED, f"{e.sig_id} {e.direction}")
    for ours, p in rep.direction_mismatch:
        add(UNEXPLAINED_DIRECTION, f"{p.sig_id} runner {ours.direction} vs pine {p.direction}")
    for e in rep.runner_only:
        add(UNEXPLAINED_RUNNER_ONLY, f"{e.sig_id} {e.direction} (Pine sent nothing for this bar)")
    for p, why in rep.pine_only:
        add(classify_pine_only(why), f"{p.sig_id} {p.direction} [{p.status}]: {why}")
    return out


@dataclass
class ParityTally:
    days: list[DayParity] = field(default_factory=list)

    def total(self, cls: str) -> int:
        return sum(d.counts.get(cls, 0) for d in self.days)

    @property
    def sessions(self) -> int:
        return len(self.days)

    @property
    def matched(self) -> int:
        return self.total(MATCHED)

    @property
    def unexplained(self) -> int:
        return sum(self.total(c) for c in UNEXPLAINED)

    @property
    def trials(self) -> int:
        """Signals the rate is measured on: matches plus unexplained divergences."""
        return self.matched + self.unexplained

    @property
    def observed(self) -> float | None:
        return self.matched / self.trials if self.trials else None

    @property
    def wilson_lower(self) -> float:
        return wilson_lower(self.matched, self.trials)

    def gate(self) -> tuple[str, list[str]]:
        """("MET" | "NOT_MET", reasons). Never a pass on thin evidence."""
        reasons = []
        if self.sessions < MIN_SESSIONS:
            reasons.append(f"sessions {self.sessions} < {MIN_SESSIONS}")
        if self.trials < MIN_SIGNALS:
            reasons.append(f"signals {self.trials} < {MIN_SIGNALS}")
        if self.observed is None or self.observed < MIN_OBSERVED:
            shown = "n/a" if self.observed is None else f"{self.observed:.3f}"
            reasons.append(f"observed {shown} < {MIN_OBSERVED}")
        if self.wilson_lower < MIN_WILSON_LOWER:
            reasons.append(f"Wilson lower {self.wilson_lower:.3f} < {MIN_WILSON_LOWER}")
        return ("NOT_MET" if reasons else "MET"), reasons


def format_ledger(t: ParityTally, runner: str, pine: str) -> str:
    verdict, reasons = t.gate()
    lines = [
        f"Parity ledger, {runner} vs {pine}: {t.sessions} session(s)",
        f"  raw: pine signals {sum(d.pine_signals for d in t.days)}, "
        f"runner signals {sum(d.runner_signals for d in t.days)}, "
        f"raw exact (no bar shift) {sum(d.raw_exact_matches for d in t.days)}",
        f"  matched {t.matched}, unexplained {t.unexplained} "
        f"(direction {t.total(UNEXPLAINED_DIRECTION)}, pine-only {t.total(UNEXPLAINED_PINE_ONLY)}, "
        f"runner-only {t.total(UNEXPLAINED_RUNNER_ONLY)})",
        f"  explained, kept out of the rate: universe {t.total(UNIVERSE)}, spec rule "
        f"{t.total(SPEC_RULE)}, outage {t.total(OUTAGE)}",
        "  observed "
        + ("n/a" if t.observed is None else f"{t.observed:.3f} ({t.matched}/{t.trials})")
        + f", Wilson lower {t.wilson_lower:.3f}",
        f"  gate (>= {MIN_OBSERVED:.0%}, Wilson >= {MIN_WILSON_LOWER:.0%}, >= {MIN_SESSIONS} sessions, "
        f">= {MIN_SIGNALS} signals): {verdict}" + (" - " + "; ".join(reasons) if reasons else ""),
        "  Retiring Pine is not live authorization; the raw numbers above are kept either way.",
    ]
    for d in t.days:
        lines.append(f"  {d.day}: " + ", ".join(f"{k} {v}" for k, v in sorted(d.counts.items())))
    return "\n".join(lines)


def _weekdays(start: dt.date, end: dt.date):
    day = start
    while day <= end:
        if day.weekday() < 5:
            yield day
        day += dt.timedelta(days=1)


def build_tally(
    start: dt.date,
    end: dt.date,
    *,
    pine_strategy: str,
    runner_strategy: str,
    state_db: str,
    trades_db: str | None,
    universe: set[str],
    pine_bar_offset: pd.Timedelta = sr.PINE_BAR_STAMP_OFFSET,
) -> ParityTally:
    """One DayParity per weekday on which the runner recorded anything."""
    tally = ParityTally()
    with RunnerStateStore(state_db) as store:
        for day in _weekdays(start, end):
            events = store.shadow_events(day, runner_strategy)
            if not events:
                continue  # the runner did not run that day: no session to count
            last_bars = store.last_bars(day, runner_strategy)
            pine = sr.load_pine_signals(day, pine_strategy, trades_db)
            rep = sr.compare(
                events, pine, universe=universe, last_bars=last_bars,
                runner_strategy=runner_strategy, pine_bar_offset=pine_bar_offset,
            )
            runner_signals = sum(1 for e in events if e.kind == "signal")
            tally.days.append(day_parity(rep, day, len(pine), runner_signals))
    return tally


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Pine versus Python parity ledger over many sessions.")
    ap.add_argument("--from", dest="start", type=dt.date.fromisoformat, required=True)
    ap.add_argument("--to", dest="end", type=dt.date.fromisoformat, default=dt.datetime.now(IST).date())
    ap.add_argument("--strategy", default="BREAKOUT", help="the Pine strategy tag in trades.db")
    ap.add_argument("--runner-strategy", default="BREAKOUT-PY")
    ap.add_argument("--state-db", default=STATE_DB_PATH)
    ap.add_argument("--trades-db", default=None)
    ap.add_argument("--symbols", nargs="*", help="override the runner universe from config")
    ap.add_argument("--details", action="store_true", help="list every divergence")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.symbols:
        universe = set(args.symbols)
    else:
        from signal_engine.config import settings

        configured = settings.runner.universe
        universe = set(configured) if isinstance(configured, tuple) else set()
    tally = build_tally(
        args.start, args.end, pine_strategy=args.strategy, runner_strategy=args.runner_strategy,
        state_db=args.state_db, trades_db=args.trades_db, universe=universe,
    )
    sys.stdout.write(format_ledger(tally, args.runner_strategy, args.strategy) + "\n")
    if args.details:
        for d in tally.days:
            for cls, line in d.details:
                if cls != MATCHED:
                    sys.stdout.write(f"  {d.day} {cls}: {line}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
