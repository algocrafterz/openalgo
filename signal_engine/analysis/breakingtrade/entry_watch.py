"""Re-checks entry confirmation for every still-unconfirmed scan pick across polls, not just
the single poll it first appeared on.

WHY THIS EXISTS

`__main__.py`'s `_emit_trade_signals()` only ever runs against `new_by_scan` - symbols that
appeared FOR THE FIRST TIME on this poll - and is only ever called once, on that same poll.
`trigger.entry_trigger()` waits for a LATER 5-minute bar to CLOSE beyond the signal bar's
extreme (see trigger.py's docstring for why: a touch-triggered entry buys every false break).
That confirmation can only appear on a bar that has not happened yet at the moment a symbol is
first flagged - so a single same-poll check almost never has anything to find, and the design
had no second chance built in: once a symbol drops out of "new" on the next poll, nothing ever
looks at it again, confirmed or not.

Measured against 2026-09-09's real scan output: 31 symbols were flagged that day, and replaying
`entry_trigger()` against the FULL day's bars (i.e. giving every symbol every poll's worth of
extra data, not just the one it was born on) found 22 of them had genuinely closed beyond their
signal-bar level - 22 real, checkable trade setups the running system never got a chance to see,
for a purely mechanical reason unrelated to signal quality. Confirmation delay in that sample
ranged 3-94 minutes, median ~14.

WHY A TIME WINDOW, NOT AN OPEN-ENDED RECHECK

Re-checking forever would eventually confirm almost anything by pure drift, which is not the
same claim `trigger.py` is making ("this IS the immediate continuation of the level the scan
just called out"). CONFIRMATION_WINDOW bounds how stale a scan pick is allowed to be before it
stops being an entry candidate - chosen from the 2026-09-09 sample (slowest genuine confirmation
was 94 minutes) with headroom, not tuned to a specific pass rate.

CADENCE

Called from `__main__.py`'s `_watch()` on every regular poll (same cadence as the scan itself,
5-15 minutes depending on the time of day), independent of whether that particular poll produced
any NEW scan hits - unlike `_emit_trade_signals`, which only runs when there are new hits to
attach the transition alert to.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from signal_engine.analysis.breakingtrade import alerts, store, trigger, validate

#: See module docstring - bounds staleness of a scan pick that has not yet confirmed. The
#: slowest genuine confirmation found in the 2026-09-09 sample was 94 minutes.
CONFIRMATION_WINDOW = timedelta(hours=2)


#: (alert kind, strategy tag, planner) per version the scanner trades. BREAKINGTRADE-RI is
#: added by check() only while its channel is enabled (alerts.ri_enabled()). Each version keeps
#: its own "already confirmed" state, so one confirming never stops the other from confirming.
_BASELINE = ("trade_signal", "BREAKINGTRADE", "plan_trade")
_RANGE_INTEGRITY = ("trade_signal_ri", "BREAKINGTRADE-RI", "plan_trade_range_integrity")


def _already_confirmed(symbol: str, since: str, kind: str = "trade_signal") -> bool:
    """Whether a signal of `kind` has already fired for this symbol from this pick onward.

    Mirrors tp_watch.py's `_last_level_hit` - a direct query against alerts' own connection
    rather than a new helper, matching that module's existing convention.
    """
    with alerts._connect() as conn:
        row = conn.execute(
            f"SELECT 1 FROM alerts WHERE kind = ? AND symbol = ? "
            f"AND {alerts.SINCE_CLAUSE} LIMIT 1",
            (kind, symbol, since),
        ).fetchone()
    return row is not None


def _pending_picks(captured_at: datetime, kind: str = "trade_signal") -> list[tuple]:
    """(symbol, direction, scan, first_seen) for today's scan hits still inside the
    confirmation window that have not yet produced a signal of `kind`.

    Reads scan_hits (already recorded for every hit, new or not, by store.record_hits) rather
    than adding a new state table - the "is this pick still worth checking" question is fully
    answered by joining what store.py already persisted against what alerts.py already
    persisted, so there is no separate lifecycle to keep in sync.
    """
    today_start = captured_at.strftime("%Y-%m-%d 00:00:00")
    now_stamp = captured_at.strftime("%Y-%m-%d %H:%M:%S")
    cutoff = captured_at - CONFIRMATION_WINDOW

    with store._connect() as conn:
        rows = conn.execute(
            "SELECT symbol, direction, scan, MIN(captured_at) FROM scan_hits "
            "WHERE is_new = 1 AND captured_at >= ? AND captured_at <= ? "
            "GROUP BY symbol, scan",
            (today_start, now_stamp),
        ).fetchall()

    picks = []
    for symbol, direction, scan, first_seen in rows:
        first_seen_dt = datetime.strptime(first_seen, "%Y-%m-%d %H:%M:%S")
        if first_seen_dt < cutoff:
            continue  # expired - not chasing a call from hours ago
        if _already_confirmed(symbol, first_seen, kind):
            continue
        picks.append((symbol, direction, scan, first_seen_dt))
    return picks


def check(captured_at: datetime, snapshot=None) -> int:
    """Re-run entry confirmation for every still-pending scan pick, per version. Returns how
    many new trade signals were emitted this poll."""
    versions = [_BASELINE] + ([_RANGE_INTEGRITY] if alerts.ri_enabled() else [])
    work = [(v, pick) for v in versions for pick in _pending_picks(captured_at, v[0])]
    if not work:
        return 0

    day_types = {}
    if snapshot is not None and "day_type" in snapshot.columns:
        day_types = dict(zip(snapshot["symbol"], snapshot["day_type"], strict=False))

    bars_cache: dict = {}  # one history fetch per symbol per poll, whatever the versions
    emitted = 0
    for (kind, strategy, planner), (symbol, direction, scan, first_seen) in work:
        try:
            if symbol not in bars_cache:
                bars_cache[symbol] = validate.fetch_bars(symbol, captured_at)
            bars = bars_cache[symbol]
            if bars.empty:
                continue
            before = bars[pd.to_datetime(bars["timestamp"]) <= captured_at]
            atr = validate.average_true_range(before) if not before.empty else None
            plan = getattr(trigger, planner)(
                symbol, direction, day_types.get(symbol), bars, first_seen, atr
            )
            if plan is None:
                continue
            alerts.alert_trade_signal(plan, strategy=strategy, scan_name=scan, kind=kind)
            emitted += 1
        except Exception as exc:
            bars_cache[symbol] = pd.DataFrame()  # a failed fetch is not retried this poll
            print(f"  entry recheck for {symbol} skipped: {type(exc).__name__}: {exc}")
    return emitted
