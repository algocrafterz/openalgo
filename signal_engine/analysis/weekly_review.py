"""Weekly rollup over the daily EOD reports: one pass over a Mon-Fri week, ending in a
plain-language paragraph a human can read without opening any of the underlying data.

WHY THIS EXISTS

eod.sh/eod_review.py already answer "did today go well" - this answers "did the week go
well", without anyone re-reading five markdown files and doing the arithmetic by hand. It
never re-derives parsing logic: it calls straight into ledger.py (the same trades.db join
eod.sh uses every evening) and __main__.py's snapshot loader, so a week's numbers always
agree with that week's daily reports by construction.

VERIFIED VS UNVERIFIED, NEVER MERGED

A day's P&L is only as good as its broker tradebook snapshot (see ledger.py's module
docstring: a Telegram alert cannot know what was actually filled). Whether a day's snapshot
succeeded is checked straight off disk - a `tradebook_YYYY-MM-DD.json` file exists in
`__main__.SNAP_DIR` for that day - not inferred from whether the ledger produced fill
numbers, so a day the snapshot silently failed cannot masquerade as verified. Verified and
unverified P&L are summed separately and the unverified total is always labelled as such,
including in the closing paragraph. Merging them would let one bad snapshot day quietly
corrupt an otherwise-real weekly number.

Usage:
    PYTHONPATH=. uv run python -m signal_engine.analysis.weekly_review
    PYTHONPATH=. uv run python -m signal_engine.analysis.weekly_review --since 2026-09-14 --until 2026-09-18

Wired into signal_engine/analysis/weekly.sh for a Saturday cron.
"""

from __future__ import annotations

import argparse
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from signal_engine.analysis.__main__ import SNAP_DIR, load_snapshots
from signal_engine.analysis.ledger import (
    Position,
    build_ledger,
    load_declined,
    load_engine_events,
    load_fills,
)

_REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")

#: Known, already-explained anomaly days. Not a general-purpose incident log - just enough
#: so a week containing a day this outage already explained does not read as unexplained
#: noise every single week it is looked back on. See PRD.md for the underlying fixes.
_KNOWN_ANOMALIES: dict[str, str] = {
    "2026-09-14": "OpenAlgo unreachable all session (engine events: 0) - cause not yet root-caused.",
    "2026-09-16": (
        "openalgoctl.sh lost its executable bit on 2026-09-15, silently killing the "
        "watchdog; fixed same day at 14:42 IST (commit a1d6513c0), ~45 min before square-off "
        "- too late to catch that day's session."
    ),
}


@dataclass
class DayResult:
    day: date
    is_trading_day: bool
    engine_events: int = 0
    snapshot_verified: bool = False
    positions: list[Position] = field(default_factory=list)
    declined: list[dict] = field(default_factory=list)
    anomaly: str | None = None

    @property
    def is_outage(self) -> bool:
        return self.is_trading_day and self.engine_events == 0


@dataclass
class WeeklyReport:
    since: date
    until: date
    days: list[DayResult] = field(default_factory=list)

    @property
    def trading_days(self) -> list[DayResult]:
        return [d for d in self.days if d.is_trading_day]

    @property
    def outage_days(self) -> list[DayResult]:
        return [d for d in self.trading_days if d.is_outage]

    @property
    def unverified_days(self) -> list[DayResult]:
        return [d for d in self.trading_days if not d.is_outage and not d.snapshot_verified]

    def strategy_table(self) -> dict[tuple[str, bool], dict]:
        """(strategy, verified) -> {trades, declined, scored, wins, sum_r, gross_pnl}."""
        buckets: dict[tuple[str, bool], dict] = defaultdict(
            lambda: {
                "trades": 0,
                "declined": 0,
                "scored": 0,
                "wins": 0,
                "sum_r": 0.0,
                "gross_pnl": 0.0,
            }
        )
        for d in self.trading_days:
            for p in d.positions:
                b = buckets[(p.strategy or "?", d.snapshot_verified)]
                b["trades"] += 1
                r = p.realised_r
                if r is not None:
                    b["scored"] += 1
                    b["sum_r"] += r
                    if r > 0:
                        b["wins"] += 1
                    if d.snapshot_verified and p.gross_pnl is not None:
                        b["gross_pnl"] += p.gross_pnl
            for row in d.declined:
                buckets[(row.get("strategy") or "?", d.snapshot_verified)]["declined"] += 1
        return buckets

    def decline_reasons(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for d in self.trading_days:
            for row in d.declined:
                stage = (row.get("message") or "").split("]")[0].lstrip("[") or "unknown"
                counts[stage] += 1
        return counts

    def total_trades(self) -> int:
        return sum(len(d.positions) for d in self.trading_days)

    def total_declined(self) -> int:
        return sum(len(d.declined) for d in self.trading_days)

    def verified_gross_pnl(self) -> float:
        return sum(
            p.gross_pnl or 0.0
            for d in self.trading_days
            if d.snapshot_verified
            for p in d.positions
            if p.gross_pnl is not None
        )

    def unverified_gross_pnl(self) -> float:
        return sum(
            p.gross_pnl or 0.0
            for d in self.trading_days
            if not d.snapshot_verified
            for p in d.positions
            if p.gross_pnl is not None
        )


def _daterange(since: date, until: date):
    d = since
    while d <= until:
        yield d
        d += timedelta(days=1)


def _last_completed_week(today: date) -> tuple[date, date]:
    """The most recently completed Mon-Fri week strictly before this week."""
    this_monday = today - timedelta(days=today.weekday())
    last_monday = this_monday - timedelta(days=7)
    return last_monday, last_monday + timedelta(days=4)


def _snapshot_exists(day: date) -> bool:
    return os.path.exists(os.path.join(SNAP_DIR, f"tradebook_{day.isoformat()}.json"))


def build_weekly_report(since: date, until: date) -> WeeklyReport:
    report = WeeklyReport(since=since, until=until)

    try:
        events = load_engine_events(since=since)
    except FileNotFoundError:
        events = []
    fills = load_fills(load_snapshots())
    all_positions = build_ledger(events, fills)
    all_declined = load_declined(since=since)

    for day in _daterange(since, until):
        is_trading_day = day.weekday() < 5
        day_events = [e for e in events if e["_ts"].date() == day]
        day_positions = [p for p in all_positions if p.day == day]
        day_declined = [
            d for d in all_declined if d["_ts"].date() == day and d["_ts"].date() <= until
        ]
        report.days.append(
            DayResult(
                day=day,
                is_trading_day=is_trading_day,
                engine_events=len(day_events),
                snapshot_verified=_snapshot_exists(day),
                positions=day_positions,
                declined=day_declined,
                anomaly=_KNOWN_ANOMALIES.get(day.isoformat()),
            )
        )
    return report


# ---------------------------------------------------------------------------
# Plain-language summary
# ---------------------------------------------------------------------------


def plain_language_summary(report: WeeklyReport) -> str:
    total_trades = report.total_trades()
    total_declined = report.total_declined()
    verified_pnl = report.verified_gross_pnl()
    unverified_pnl = report.unverified_gross_pnl()
    verified_days = [d for d in report.trading_days if d.snapshot_verified and d.positions]
    unverified_days = [d for d in report.unverified_days if d.positions]

    reasons = report.decline_reasons()
    top_reason = max(reasons.items(), key=lambda kv: kv[1]) if reasons else None

    lines: list[str] = []

    if total_trades == 0:
        lines.append(
            f"No trades were taken anywhere from {report.since} to {report.until} - either "
            "nothing set up all week, or the engine was not running (see the problem days below)."
        )
    else:
        lines.append(
            f"Over {len(report.trading_days)} trading days ({report.since} to {report.until}), "
            f"the strategies took {total_trades} trade(s) and skipped {total_declined} signal(s) "
            "that did not meet the risk rules."
        )
        if verified_days and not unverified_days:
            sign = "made" if verified_pnl >= 0 else "lost"
            lines.append(
                f"Money-wise, the week {sign} about INR {abs(verified_pnl):,.0f}, and this number is checked against the broker's own record so it can be trusted."
            )
        elif verified_days and unverified_days:
            v_sign = "made" if verified_pnl >= 0 else "lost"
            u_sign = "made" if unverified_pnl >= 0 else "lost"
            lines.append(
                f"On the {len(verified_days)} day(s) checked against the broker, the strategies "
                f"{v_sign} about INR {abs(verified_pnl):,.0f}. On the other "
                f"{len(unverified_days)} day(s) the broker record could not be captured, so those "
                f"numbers are only the system's own estimate (roughly {u_sign} INR "
                f"{abs(unverified_pnl):,.0f}) and should not be treated as real money until "
                "re-verified."
            )
        else:
            sign = "made" if unverified_pnl >= 0 else "lost"
            lines.append(
                f"The broker record could not be captured on any day this week, so the "
                f"P&L below (roughly {sign} INR {abs(unverified_pnl):,.0f}) is only the "
                "system's own estimate, not a verified number - treat it as a plumbing "
                "check, not a trading result."
            )

    if top_reason:
        lines.append(
            f"The most common reason a signal was skipped was '{top_reason[0]}', "
            f"accounting for {top_reason[1]} of the {total_declined} skipped signal(s)."
        )

    problem_days = [d for d in report.trading_days if d.anomaly or d.is_outage]
    if problem_days:
        lines.append("Problem days this week:")
        for d in problem_days:
            cause = d.anomaly or "no engine activity recorded and no known explanation logged yet."
            lines.append(f"  - {d.day}: {cause}")
    else:
        lines.append("No unexplained outage days this week.")

    if unverified_days:
        lines.append(
            "The broker-verification step (tradebook snapshot) did not run on "
            f"{len(unverified_days)} day(s) with trades this week - fill prices and slippage "
            "for those days are the system's own estimate only, not confirmed by the broker."
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def render_report(report: WeeklyReport) -> str:
    out: list[str] = []
    out.append(f"# Weekly review — {report.since} to {report.until}")
    out.append("")
    out.append("## Daily overview")
    out.append("")
    out.append(
        f"{'day':<12}{'weekday':<11}{'events':>8}{'trades':>8}{'declined':>10}{'snapshot':>12}"
    )
    for d in report.trading_days:
        snap = "verified" if d.snapshot_verified else "unverified"
        out.append(
            f"{d.day.isoformat():<12}{d.day.strftime('%A'):<11}{d.engine_events:>8}"
            f"{len(d.positions):>8}{len(d.declined):>10}{snap:>12}"
        )
    out.append("")
    out.append("## By strategy (verified vs unverified kept separate)")
    out.append("")
    out.append(
        f"{'strategy':<16}{'verified':<10}{'trades':>7}{'declined':>10}{'scored':>8}{'win%':>7}{'sum R':>9}{'gross P&L':>14}"
    )
    for (strategy, verified), b in sorted(report.strategy_table().items()):
        win = f"{b['wins'] / b['scored'] * 100:.0f}%" if b["scored"] else "-"
        sum_r = f"{b['sum_r']:+.2f}" if b["scored"] else "-"
        pnl = (
            f"{b['gross_pnl']:+,.0f}"
            if verified and b["scored"]
            else ("unverified" if b["scored"] else "-")
        )
        out.append(
            f"{strategy:<16}{'yes' if verified else 'no':<10}{b['trades']:>7}{b['declined']:>10}"
            f"{b['scored']:>8}{win:>7}{sum_r:>9}{pnl:>14}"
        )
    out.append("")
    out.append("## Declined signals, by reason")
    out.append("")
    for reason, count in sorted(report.decline_reasons().items(), key=lambda kv: -kv[1]):
        out.append(f"  {count:3}  {reason}")
    if not report.decline_reasons():
        out.append("  none")
    out.append("")
    out.append("## Plain-language summary")
    out.append("")
    out.append(plain_language_summary(report))
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(prog="signal_engine.analysis.weekly_review")
    ap.add_argument(
        "--since", default=None, help="YYYY-MM-DD, start of the week (default: last Monday)"
    )
    ap.add_argument(
        "--until", default=None, help="YYYY-MM-DD, end of the week (default: last Friday)"
    )
    args = ap.parse_args()

    if args.since and args.until:
        since = date.fromisoformat(args.since)
        until = date.fromisoformat(args.until)
    else:
        since, until = _last_completed_week(date.today())

    report = build_weekly_report(since, until)
    text = render_report(report)
    print(text)

    os.makedirs(_REPORTS_DIR, exist_ok=True)
    out_path = os.path.join(_REPORTS_DIR, f"weekly-{since}-to-{until}.md")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"\nreport -> {out_path}")


if __name__ == "__main__":
    main()
