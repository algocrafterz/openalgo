"""Per-run data-quality report, built from the frames a run actually used.

`build_data_quality` turns the frames plus the loader's `DataCollector` into the
content of `data_quality.json`. It applies a stated policy on top of the facts
`dataquality.audit_frame` returns:

  * WARNING with `affects_validity` when impossible bars, duplicate timestamps or
    out-of-hours bars exceed `bad_bar_share` of all bars seen (retained plus dropped
    by the loader), or when session completeness is below the minimum for more than
    `incomplete_symbol_session_share` of symbol-sessions.
  * WARNING without `affects_validity` for lower-level findings (a symbol whose last
    bar lags the panel, split days dropped, symbols dropped for too few sessions,
    zero-volume bars).
  * PASS otherwise. Findings within their thresholds are listed as `info`.

Deterministic: nothing here reads the clock, so the same inputs give the same output.
Staleness is measured against the panel's own latest bar, not today's date.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from signal_engine.backtest import dataquality
from signal_engine.backtest.data_collect import DataCollector

SCHEMA = 1
MAX_LISTED_SYMBOLS = 10
_EPS = 1e-9
_DAILY = {"1d", "d"}

DEFAULT_THRESHOLDS: dict = {
    "bad_bar_share": 0.001,
    "incomplete_symbol_session_share": 0.02,
    "min_session_completeness": dataquality.MIN_SESSION_COMPLETENESS,
    "stale_days": 10,
    "zero_volume_pct": 5.0,
}

POLICY = {
    "corporate_actions": (
        "intraday: sessions whose open versus previous close ratio lies outside 0.75 to 1.33 "
        "are excluded as probable splits; daily: back-adjusted for detected splits; "
        "dividends, bonuses and rights issues are NOT detected"
    ),
    "closing_auction": (
        "only the 09:15-15:29 continuous session is kept; bars outside it, including any "
        "closing-auction prints, are dropped by the loader"
    ),
    "timestamps": "stored as epoch seconds, converted to IST at load",
}


def select_symbols(names: list[str], max_symbols: int | None) -> list[str]:
    """Sorted names, or a deterministic evenly spaced sample of `max_symbols` of them."""
    ordered = sorted(names)
    if max_symbols is None or max_symbols >= len(ordered):
        return ordered
    if max_symbols <= 0:
        return []
    pos = np.unique(np.linspace(0, len(ordered) - 1, max_symbols).round().astype(int))
    return [ordered[i] for i in pos]


def _item(check: str, severity: str, detail: str, count: int, symbols) -> dict:
    names = sorted(set(symbols))
    return {
        "check": check,
        "severity": severity,
        "detail": detail,
        "count": int(count),
        "symbols": names[:MAX_LISTED_SYMBOLS],
        "_all": names,
    }


def _exceeds(count: int, share: float, total: int) -> bool:
    return count - share * total > _EPS


def build_data_quality(
    frames: dict[str, pd.DataFrame],
    collector: DataCollector,
    *,
    calendar=None,
    short_sessions=(),
    thresholds: dict | None = None,
    max_symbols: int | None = None,
    expected_bars_for=None,
) -> dict:
    """The `data_quality.json` content for one run. See the module docstring.

    `expected_bars_for(day) -> int` is the dataset's own session shape. When given, session
    completeness is measured against it instead of the fixed 09:15-15:29 bar count, which
    predates the closing auction.
    """
    th = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        unknown = set(thresholds) - set(th)
        if unknown:
            raise ValueError(f"unknown data-quality thresholds: {sorted(unknown)}")
        th.update(thresholds)

    interval = collector.interval
    audited = select_symbols(list(frames), max_symbols)
    summary: dict = {
        "symbols": len(frames),
        "symbols_with_issues": 0,
        "sessions_checked": 0,
        "complete_pct_overall": None,
        "audited_symbols": audited,
    }
    if calendar is not None:
        summary["calendar_sessions"] = len(list(calendar))

    base = {
        "schema": SCHEMA,
        "thresholds": th,
        "summary": summary,
        "dropped": collector.to_dict(),
        "policy": dict(POLICY),
    }
    if not audited:
        return {**base, "status": "NOT_RUN", "affects_validity": False, "items": []}
    if not interval:
        raise ValueError("collector.interval is required to audit frames")

    daily = interval.strip().lower() in _DAILY
    rows = {s: dataquality.audit_frame(frames[s], interval, s) for s in audited}
    if daily:  # daily bars are stamped at midnight; the session-hours rule is intraday only
        for r in rows.values():
            r["outside_hours"] = 0

    items: list[dict] = []
    affects = False
    dropped = collector.clean_dropped
    keys = ("impossible", "duplicate_ts", "outside_hours")
    audit_key = {
        "impossible": "impossible",
        "duplicate_ts": "dup_ts",
        "outside_hours": "outside_hours",
    }
    names = {
        "impossible": "impossible_bars",
        "duplicate_ts": "duplicate_timestamps",
        "outside_hours": "outside_hours_bars",
    }
    # One population per symbol. Where the loader recorded rows seen before cleaning, the
    # share is its dropped rows over those rows (all loaded symbols). Otherwise it is the
    # audited frame's own bad bars over its bars plus whatever the loader dropped.
    per_class = {k: (0, set()) for k in keys}
    total_bars = 0
    for s in sorted(set(collector.rows_seen) | set(dropped) | set(rows)):
        drop = dropped.get(s, {})
        if s in collector.rows_seen:
            total_bars += collector.rows_seen[s]
            counts = {k: int(drop.get(k, 0)) for k in keys}
        elif s in rows:
            total_bars += int(rows[s]["bars"]) + sum(drop.values())
            counts = {k: int(rows[s][audit_key[k]]) + int(drop.get(k, 0)) for k in keys}
        else:
            continue
        for k in keys:
            if counts[k]:
                n, syms = per_class[k]
                per_class[k] = (n + counts[k], syms | {s})
    bad_total = sum(n for n, _ in per_class.values())
    bad_validity = _exceeds(bad_total, th["bad_bar_share"], total_bars)
    for k, (n, syms) in per_class.items():
        if n:
            sev = "warning" if bad_validity else "info"
            share = 100 * n / total_bars
            items.append(
                _item(
                    names[k],
                    sev,
                    f"{n} of {total_bars} rows ({share:.3f}%); policy limit for all "
                    f"bad-bar classes together is {100 * th['bad_bar_share']:.3f}%",
                    n,
                    syms,
                )
            )
    affects = affects or bad_validity

    # Session completeness.
    if not daily:
        floor = th["min_session_completeness"]
        short = set(short_sessions)
        sess, incomplete = {}, {}
        for s in rows:
            counts = frames[s].groupby(frames[s].index.date).size()
            if short:  # half-day and special sessions are short by design, not incomplete
                counts = counts[[d not in short for d in counts.index]]
            if expected_bars_for is None:
                exp = dataquality.expected_bars(interval)
            else:
                exp = np.array([expected_bars_for(d) for d in counts.index], dtype=float)
            sess[s] = int(len(counts))
            incomplete[s] = int((counts.to_numpy() < exp * floor).sum())
        n_sess, n_inc = sum(sess.values()), sum(incomplete.values())
        summary["sessions_checked"] = n_sess
        if n_sess:
            summary["complete_pct_overall"] = round(100 * (n_sess - n_inc) / n_sess, 1)
        if n_inc:
            inc_validity = _exceeds(n_inc, th["incomplete_symbol_session_share"], n_sess)
            items.append(
                _item(
                    "incomplete_sessions",
                    "warning" if inc_validity else "info",
                    f"{n_inc} of {n_sess} symbol-sessions below {floor:.0%} of expected bars"
                    + (
                        f" (sample of {len(rows)} of {len(frames)} symbols)"
                        if len(rows) < len(frames)
                        else ""
                    ),
                    n_inc,
                    [s for s, v in incomplete.items() if v],
                )
            )
            affects = affects or inc_validity
    else:
        summary["sessions_checked"] = sum(int(r["sessions"]) for r in rows.values())

    # Lower-level findings: warnings that do not affect validity.
    lasts = {s: frames[s].index[-1].date() for s in frames if len(frames[s])}
    if lasts:
        newest = max(lasts.values())
        stale = [s for s, d in lasts.items() if (newest - d).days > th["stale_days"]]
        if stale:
            items.append(
                _item(
                    "stale_last_bar",
                    "warning",
                    f"last bar more than {th['stale_days']} days before the panel's "
                    f"latest bar ({newest.isoformat()})",
                    len(stale),
                    stale,
                )
            )
    if collector.split_days_dropped:
        n = sum(len(v) for v in collector.split_days_dropped.values())
        items.append(
            _item(
                "split_days_dropped",
                "warning",
                f"{n} sessions excluded as probable splits",
                n,
                collector.split_days_dropped,
            )
        )
    if collector.symbols_dropped_min_sessions:
        d = collector.symbols_dropped_min_sessions
        items.append(
            _item(
                "symbols_dropped_min_sessions",
                "warning",
                f"{len(d)} symbols dropped for too few sessions",
                len(d),
                d,
            )
        )
    zero = [s for s, r in rows.items() if r["zero_vol_pct"] > th["zero_volume_pct"]]
    if zero:
        items.append(
            _item(
                "zero_volume",
                "warning",
                f"more than {th['zero_volume_pct']}% of bars have zero volume",
                len(zero),
                zero,
            )
        )

    flagged = set()
    for it in items:
        flagged |= set(it["_all"])
        del it["_all"]
    summary["symbols_with_issues"] = len(flagged)
    warn = affects or any(i["severity"] == "warning" for i in items)
    return {
        **base,
        "status": "WARNING" if warn else "PASS",
        "affects_validity": affects,
        "items": items,
    }
