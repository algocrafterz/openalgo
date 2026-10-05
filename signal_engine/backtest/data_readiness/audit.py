"""Store audit: run every data-readiness rule over the whole landing store (plan Phase 1).

Read-only. Writes `audit.json` (global summary, per-rule results, per-dimension status, and a
projection of the D15 exclusion thresholds), `per_symbol.parquet`, `evidence/<interval>/<rule>.parquet`
(symbol/session rows) and `AUDIT.md`. There is no quality score: each dimension has its own status.

The expected sessions come from the official NSE calendar (`research/reference/nse_calendar.yaml`,
plan Phase 2), never from the data: the data only sets the first and last date of the window. The
Phase 1 proxy (the calendar derived from the data, weekdays only) remains available with
`--proxy-calendar` for comparison.

    uv run python -m signal_engine.backtest.data_readiness.audit
    uv run python -m signal_engine.backtest.data_readiness.audit --interval D --out /tmp/audit
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from signal_engine.backtest import data as _data
from signal_engine.backtest import session as _session
from signal_engine.backtest.data_readiness import policy as pol
from signal_engine.backtest.data_readiness import rules as R
from signal_engine.backtest.data_readiness import state as st
from signal_engine.backtest.data_readiness.calendar import NseCalendar, load_calendar
from signal_engine.research.intake.fingerprints import canonical_json, sha256_hex
from signal_engine.timeutils import IST

MAX_LISTED = 50
PROXY_SOURCE = "derived_from_market_data, union of intervals, weekdays only (Phase 1 proxy)"
#: D16: lookback-based daily strategies may not drop single sessions; 1m sessions are independent.
SESSION_EXCLUSION_ALLOWED = {"D": False, "1m": True}
PASS, WARNING, FAIL, NOT_EVALUATED = "PASS", "WARNING", "FAIL", "NOT_EVALUATED"


@dataclass(frozen=True)
class AuditCalendar:
    sessions: tuple[date, ...]
    source: str
    version: str


def _derived_days(con, interval: str) -> tuple[date, ...]:
    bars, params = R._bars(R.Scope(interval=interval))
    have = con.execute(f"WITH {bars} SELECT DISTINCT symbol, session FROM bars", params).df()
    frames = {}
    for sym, grp in have.groupby("symbol"):
        idx = pd.DatetimeIndex(pd.to_datetime(grp["session"])).tz_localize(IST)
        frames[sym] = pd.DataFrame(index=idx + pd.Timedelta(hours=9, minutes=15))
    return _session.derive_calendar(frames).trading_days


def _data_window(con, intervals: tuple[str, ...]) -> tuple[date, date] | None:
    """First and last session date in the audited intervals (the window only, not the sessions)."""
    lo = hi = None
    for iv in intervals:
        bars, params = R._bars(R.Scope(interval=iv))
        a, b = con.execute(f"WITH {bars} SELECT MIN(session), MAX(session) FROM bars",
                           params).fetchone()
        if a is not None:
            lo = a if lo is None else min(lo, a)
            hi = b if hi is None else max(hi, b)
    return None if lo is None else (lo, hi)


def official_calendar(con, cal: NseCalendar, intervals: tuple[str, ...]) -> AuditCalendar:
    """The official calendar's sessions over the data window. Raises CalendarError when the data
    leaves the calendar's coverage."""
    window = _data_window(con, intervals)
    sessions = cal.expected_sessions(*window) if window else ()
    return AuditCalendar(tuple(sessions), f"nse_calendar: {cal.source}", cal.version)


def proxy_calendar(con) -> AuditCalendar:
    """Union of the calendars derived from each interval, restricted to weekdays.

    The union matters: on some trading days the daily data holds a bar for only one symbol, so
    a calendar derived from daily bars alone drops those days and hides the gap. The 1m data
    still shows them as sessions.
    """
    days: set[date] = set()
    for iv in R.INTERVALS:
        days.update(_derived_days(con, iv))
    out = tuple(sorted(d for d in days if d.weekday() < 5))
    version = "proxy-" + sha256_hex(canonical_json([d.isoformat() for d in out]))[:8]
    return AuditCalendar(out, PROXY_SOURCE, version)


def _result_json(res: R.RuleResult) -> dict[str, Any]:
    symbols = dict(sorted(res.symbols.items(), key=lambda kv: (-kv[1], kv[0])))
    sessions = res.sessions
    rule = R.RULES[res.rule_id]
    return {
        "version": res.version,
        "dimension": res.dimension,
        "action": res.action.value,
        "status": res.status,
        "reason": res.reason,
        "rows": res.rows,
        "symbol_count": len(symbols),
        "symbols": dict(list(symbols.items())[:MAX_LISTED]),
        "session_count": len(sessions),
        "sessions": sessions[:MAX_LISTED],
        "rationale": rule.rationale,
    }


def _skipped(results: list[R.RuleResult], interval: str) -> list[R.RuleResult]:
    """Rules that apply to the interval but could not run. They are never a pass."""
    return [r for r in results if r.status == R.NOT_EVALUATED
            and R.RULES[r.rule_id].applies_to(interval)]


def _dimension_status(results: list[R.RuleResult], interval: str) -> dict[str, str]:
    skipped = {r.dimension for r in _skipped(results, interval)}
    out = {}
    for dim in R.DIMENSIONS:
        mine = [r for r in results if r.dimension == dim and r.status == R.EVALUATED]
        if any(r.rows and r.action is R.Action.BLOCK for r in mine):
            out[dim] = FAIL
        elif not mine or dim in skipped:
            out[dim] = NOT_EVALUATED
        elif any(r.rows and r.action is not R.Action.INFO for r in mine):
            out[dim] = WARNING
        else:
            out[dim] = PASS
    return out


def _exclusions(results: list[R.RuleResult], allowed: bool) -> list[st.Exclusion]:
    defect_rules = R.DEFECT_RULES if allowed else R.DEFECT_RULES | R.MISSING_SESSION_RULES
    out = []
    for res in results:
        if res.status != R.EVALUATED or res.rule_id not in defect_rules:
            continue
        symbol_level = res.action is R.Action.EXCLUDE_SYMBOL
        for row in res.evidence.dropna(subset=["symbol"]).itertuples(index=False):
            session = None if symbol_level else row.session
            out.append(st.Exclusion(row.symbol, session, res.rule_id, res.dimension, st.DEFECT))
    return out


def _projection(interval, results, per_symbol, invalid, total, policy) -> dict[str, Any]:
    """What preflight would conclude for a run over every symbol of the interval."""
    allowed = SESSION_EXCLUSION_ALLOWED[interval]
    # A session on a date that already blocks the run (a panel-wide calendar error) is part of
    # that one dataset-level problem, not a separate defect of each symbol.
    blocked_dates = {s for r in results if r.rule_id == "CAL-UNEXPECTED-DATE"
                     for s in r.evidence["session"]}
    raw = _exclusions(results, allowed)
    kept = [e for e in raw if e.session is None or e.session not in blocked_dates]
    sessions = dict(zip(per_symbol["symbol"], per_symbol["sessions"], strict=True))
    ex = st.apply_session_policy(kept, session_exclusion_allowed=allowed,
                                 sessions_per_symbol=sessions,
                                 max_session_share=policy.max_missing_session_share)
    symbols = list(per_symbol["symbol"])
    blocks = [r.rule_id for r in results
              if r.status == R.EVALUATED and r.rows and r.action is R.Action.BLOCK]
    warnings = [r.rule_id for r in results if r.status == R.EVALUATED and r.rows
                and r.action is R.Action.WARNING]
    for r in _skipped(results, interval):
        target = blocks if r.action is R.Action.BLOCK else warnings
        target.append(f"not_evaluated:{r.rule_id}")
    if total == 0:
        blocks.append("no_rows")
    share = invalid / total if total else 0.0
    if share > policy.bad_bar_share:
        blocks.append("VAL-WIDESPREAD")
    defect = sorted({e.symbol for e in ex if e.session is None})
    a = st.assess(requested=len(symbols), blocks=blocks, warnings=warnings, exclusions=ex,
                  eligible=len(symbols) - len(defect), min_symbols=1, policy=policy)
    by_rule: dict[str, list[str]] = {}
    for e in ex:
        if e.session is None:
            by_rule.setdefault(e.rule_id, []).append(e.symbol)
    return {
        "session_exclusion_allowed": allowed,
        "requested_symbols": len(symbols),
        "policy": pol.exclusion_thresholds(policy),
        "invalid_bar_rows": invalid,
        "total_rows": total,
        "invalid_bar_share": share,
        "blocks": blocks,
        "warnings": warnings,
        "defect_excluded_symbols": defect,
        "defect_excluded_by_rule": {k: sorted(set(v)) for k, v in sorted(by_rule.items())},
        "excluded_sessions": a.excluded_sessions,
        "exclusions_on_blocked_dates": len(raw) - len(kept),
        "state": a.state,
        "reasons": list(a.reasons),
    }


def _per_symbol(con, interval: str) -> pd.DataFrame:
    bars, params = R._bars(R.Scope(interval=interval))
    df = con.execute(
        f"WITH {bars} SELECT ? AS interval, symbol, COUNT(*) AS rows, "
        "COUNT(DISTINCT session) AS sessions, MIN(session) AS first_session, "
        "MAX(session) AS last_session, string_agg(DISTINCT exchange, ',') AS exchanges "
        "FROM bars GROUP BY symbol ORDER BY symbol", [*params, interval]).df()
    for col in ("first_session", "last_session"):
        df[col] = pd.to_datetime(df[col]).dt.date
    return df


def _audit_interval(con, interval, policy, out: Path,
                    cal: AuditCalendar) -> tuple[dict[str, Any], pd.DataFrame]:
    ctx = R.Context(policy=policy, calendar=cal.sessions)
    scope = R.Scope(interval=interval)
    results = [R.evaluate(con, scope, rid, ctx) for rid in R.RULES]
    ev_dir = out / "evidence" / interval
    ev_dir.mkdir(parents=True, exist_ok=True)
    for res in results:
        if res.status == R.EVALUATED:
            res.evidence.to_parquet(ev_dir / f"{res.rule_id}.parquet", index=False)
    per_symbol = _per_symbol(con, interval)
    invalid, total = R.invalid_bar_counts(con, scope)
    block = {
        "calendar": {"source": cal.source, "version": cal.version, "sessions": len(cal.sessions),
                     "first": str(min(cal.sessions)) if cal.sessions else None,
                     "last": str(max(cal.sessions)) if cal.sessions else None},
        "symbols": len(per_symbol),
        "rows": total,
        "first_session": str(per_symbol["first_session"].min()) if len(per_symbol) else None,
        "last_session": str(per_symbol["last_session"].max()) if len(per_symbol) else None,
        "dimensions": _dimension_status(results, interval),
        "rules": {r.rule_id: _result_json(r) for r in results},
        "projection": _projection(interval, results, per_symbol, invalid, total, policy),
    }
    return block, per_symbol


def _markdown(report: dict[str, Any]) -> str:
    lines = [f"# Data-store audit {report['generated_ist']}", "",
             f"Store: `{report['db']}`. Policy version `{report['policy_version'][:12]}`.",
             f"Calendar: `{report['calendar']['source']}` version "
             f"`{str(report['calendar']['version'])[:12]}`.", ""]
    for iv, blk in report["intervals"].items():
        p = blk["projection"]
        mode = "allowed" if p["session_exclusion_allowed"] else "not allowed"
        lines += [f"## Interval {iv}", "",
                  f"{blk['symbols']} symbols, {blk['rows']:,} rows, "
                  f"{blk['first_session']} to {blk['last_session']}.", "",
                  "| Dimension | Status |", "|---|---|"]
        lines += [f"| {d} | {s} |" for d, s in blk["dimensions"].items()]
        lines += ["", "| Rule | Action | Status | Rows | Symbols | Sessions |",
                  "|---|---|---|---|---|---|"]
        for rid, r in blk["rules"].items():
            lines.append(f"| {rid} | {r['action']} | {r['status']} | {r['rows']:,} | "
                         f"{r['symbol_count']} | {r['session_count']} |")
        lines += ["", f"Projection for a run over all {p['requested_symbols']} symbols "
                  f"(session exclusion {mode}):",
                  f"- state: {p['state']} ({', '.join(p['reasons']) or 'no reasons'})",
                  f"- blocks: {', '.join(p['blocks']) or 'none'}",
                  f"- defect-excluded symbols: {len(p['defect_excluded_symbols'])} "
                  f"(limit {p['policy']['max_defect_excluded_symbols']} or "
                  f"{p['policy']['max_defect_excluded_symbol_share']:.0%})",
                  f"- invalid-bar share: {p['invalid_bar_share']:.5%} "
                  f"({p['invalid_bar_rows']:,} of {p['total_rows']:,})", ""]
    return "\n".join(lines) + "\n"


def run_audit(db: Path | str, out: Path | str, policy: pol.Policy,
              intervals: tuple[str, ...] = R.INTERVALS, *, calendar: NseCalendar | None = None,
              proxy: bool = False) -> dict[str, Any]:
    """Audit the store at `db` and write the outputs under `out`.

    `calendar` defaults to the repository NSE calendar. `proxy=True` uses the Phase 1
    data-derived calendar instead (comparison only). DuckDB and calendar errors propagate.
    """
    if not proxy and calendar is None:
        calendar = load_calendar()
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "generated_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
        "db": str(db),
        "policy_version": policy.version,
        "policy_source": policy.source,
        "rule_versions": {rid: r.version for rid, r in R.RULES.items()},
        "intervals": {},
    }
    frames = []
    with R.connect_readonly(db) as con:
        cal = proxy_calendar(con) if proxy else official_calendar(con, calendar, intervals)
        report["calendar"] = {"source": cal.source, "version": cal.version}
        for iv in intervals:
            block, per_symbol = _audit_interval(con, iv, policy, out, cal)
            report["intervals"][iv] = block
            frames.append(per_symbol)
    pd.concat(frames, ignore_index=True).to_parquet(out / "per_symbol.parquet", index=False)
    report = json.loads(json.dumps(report, default=str))
    (out / "audit.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out / "AUDIT.md").write_text(_markdown(report), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=str(_data.HISTORIFY_DB))
    ap.add_argument("--interval", choices=["D", "1m", "all"], default="all")
    ap.add_argument("--policy", default=str(pol.DEFAULT_PATH))
    ap.add_argument("--out", default=None,
                    help="default: signal_engine/reports/data-audit/<IST date>")
    ap.add_argument("--proxy-calendar", action="store_true",
                    help="use the Phase 1 data-derived calendar instead of nse_calendar.yaml")
    ap.add_argument("--fail-on-not-ready", action="store_true",
                    help="exit 1 when any interval projects NOT_READY (for cron or CI)")
    args = ap.parse_args(argv)
    out = args.out or (f"signal_engine/reports/data-audit/"
                       f"{datetime.now(IST).strftime('%Y-%m-%d')}")
    intervals = R.INTERVALS if args.interval == "all" else (args.interval,)
    report = run_audit(args.db, out, pol.load_policy(args.policy), intervals,
                       proxy=args.proxy_calendar)
    for iv, blk in report["intervals"].items():
        p = blk["projection"]
        print(f"{iv}: {p['state']} blocks={p['blocks']} "
              f"defect_excluded={len(p['defect_excluded_symbols'])}")
    print(f"written to {out}")
    not_ready = any(b["projection"]["state"] == st.NOT_READY
                    for b in report["intervals"].values())
    return 1 if args.fail_on_not_ready and not_ready else 0


if __name__ == "__main__":
    raise SystemExit(main())
