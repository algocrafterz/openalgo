"""Fill OpenAlgo's Historify store with verified bar data, in chunks.

WHY THIS EXISTS RATHER THAN THE HISTORIFY UI

`services.historify_service.download_data` issues ONE broker request for the whole
date range it is given. Flattrade answers a single history call with at most roughly
58,000 one-minute bars - about seven months - and returns them without any indication
that it truncated. Ask the UI for five years and it reports success, stores what came
back, and the missing years look exactly like a stock that did not trade. Every
backtest run against that store is then quietly measuring a different period than the
one it printed.

So this module chunks. A request covers one calendar month of 1-minute bars (~7,500),
which sits far enough under the cap that a silent truncation cannot happen, and each
chunk is upserted independently so an interrupted run resumes instead of restarting.

WHAT THE BROKER ACTUALLY HAS (Flattrade, measured 2026-09-12)

    1m   about 12 months rolling. Older 1-minute history is GONE and cannot be
         re-fetched - which makes the 2020-2026 bars already in the store an asset
         that must be preserved, never rebuilt.
    D    back to roughly December 2019.

The practical consequence: extend existing symbols forward, and accept that a newly
added symbol starts with about a year of intraday history.
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import pandas as pd

from utils.logging import get_logger

logger = get_logger(__name__)

#: About one calendar month per request. ~7,500 one-minute bars against a cap near 58,000 -
#: deliberately conservative, because the failure mode is silent.
#:
#: Must stay BELOW Angel One's own 30-day 1m chunk. Angel's plugin
#: (broker/angel/api/data.py) ends every internal chunk but the last at 00:00 of its final
#: day, so a request of exactly 30 days silently drops that day's bars. 30 here produced 55
#: missing 1m sessions spaced ~30 days apart (verified 2026-10-04: 2019-12-11..2020-01-09
#: returned no 2020-01-09 bars; 2019-12-12..2020-01-09 did). 29 keeps every request
#: inside one Angel chunk, whose end is the caller's 23:59.
CHUNK_DAYS = 29

#: Daily bars are small enough to fetch in a few requests. Kept below Angel's 2000-day
#: daily chunk for the same reason as CHUNK_DAYS.
DAILY_CHUNK_DAYS = 1999

#: How far back Flattrade still serves 1-minute bars (measured 2026-09-12: about 12 months,
#: rolling). A 1m gap older than this cannot be refetched from the live broker, so gap repair
#: does not try. Kept under 365 so a boundary day is never requested.
INTRADAY_DEPTH_DAYS = 350

#: Exit status of --verify / --repair-gaps when the NSE calendar does not cover today. Distinct
#: from 1 (the store is behind) so the log shows a calendar to update, not a data failure.
EXIT_CALENDAR_ENDED = 3
CALENDAR_ENDED_MESSAGE = (
    "calendar: {detail}.\n"
    "The NSE calendar (signal_engine/research/reference/nse_calendar.yaml) must be extended from "
    "the official NSE trading-holiday circular for the new year; an unofficial list is not "
    "accepted. Until then verify and gap repair stay off and no sessions were guessed: a guessed "
    "calendar would invent sessions and send false gap-repair requests. Extending the store "
    "forward is unaffected.")

#: A day's bars are complete only after the cash close plus the cron's buffer. Before this time
#: of day the latest expected session is still the previous one.
SESSION_COMPLETE_IST = dt.time(15, 40)

#: --daily: from the cash close the scheduled run fetches TODAY's session. A bar that lands after
#: the run (the closing-auction print) is rewritten by the next run, which starts one day before
#: each symbol's last bar. The runner's warm-up needs the previous session every morning, so the
#: 15:35 run must not wait for SESSION_COMPLETE_IST and leave today to the day after tomorrow.
MARKET_CLOSE_IST = dt.time(15, 30)


def _now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="Asia/Kolkata")


@dataclass
class SymbolResult:
    symbol: str
    stored: int = 0
    chunks_ok: int = 0
    chunks_empty: int = 0
    chunks_failed: int = 0
    first: str | None = None
    last: str | None = None
    error: str | None = None
    #: Only populated when `compare_existing=True`: how many bars in the new source
    #: overlapped a bar already in the store, how many of those disagreed beyond
    #: tolerance, and the worst single disagreement seen.
    compared: int = 0
    mismatches: int = 0
    max_diff_pct: float = 0.0
    #: Bars the broker returned with a non-positive price, which are never stored.
    rejected: int = 0


#: Two independent broker feeds should agree almost exactly on a closed bar. This is
#: NOT a corporate-action threshold like SPLIT_LO/SPLIT_HI in data.py - it is a "did
#: these two vendors report the same trade" threshold, so it stays tight.
COMPARE_TOLERANCE_PCT = 0.5


def _broker_session(db_path: str = "db/openalgo.db") -> tuple[str, str | None, str]:
    """Decrypted broker token straight from the auth row.

    Read in-process and never written anywhere: the alternative is passing an OpenAlgo
    API key on a command line, where it lands in shell history and in the process list.
    """
    from database.auth_db import decrypt_token

    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT auth, feed_token, broker FROM auth WHERE is_revoked = 0 LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    if not row:
        raise RuntimeError("no active broker session - log in to OpenAlgo first")
    return decrypt_token(row[0]), (decrypt_token(row[1]) if row[1] else None), row[2]


def coverage(interval: str = "1m", db_path: str = "db/historify.duckdb") -> pd.DataFrame:
    """What the store holds per symbol and exchange: bars, span, and how stale it is.

    Keyed on the exchange too: NIFTY is stored under NSE_INDEX, and a symbol-only key would
    request it under NSE, where the broker does not know it."""
    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    try:
        df = con.execute(
            "SELECT symbol, exchange, count(*) AS bars, min(timestamp) AS t0, "
            "max(timestamp) AS t1 FROM market_data WHERE interval = ? "
            "GROUP BY symbol, exchange ORDER BY symbol, exchange",
            [interval],
        ).df()
    finally:
        con.close()
    if df.empty:
        return df
    def to_ist(epoch_seconds):
        return pd.to_datetime(epoch_seconds, unit="s", utc=True).dt.tz_convert("Asia/Kolkata")

    df["first"] = to_ist(df.t0).dt.date
    df["last"] = to_ist(df.t1).dt.date
    df["stale_days"] = (pd.Timestamp.now(tz="Asia/Kolkata").normalize().date() - df["last"]).apply(
        lambda d: d.days)
    return df[["symbol", "exchange", "bars", "first", "last", "stale_days"]]


def _compare_to_existing(df: pd.DataFrame, symbol: str, exchange: str, interval: str,
                         db_path: str) -> tuple[int, int, float]:
    """How many of `df`'s bars already exist in Historify, and do they agree?

    Read-only, best-effort: a lock conflict here must never block the write that
    follows, so any read failure is treated as "nothing to compare against" rather
    than raised. Compares CLOSE only - the field every strategy in this repo actually
    prices trades and stops off - within COMPARE_TOLERANCE_PCT.

    Returns (compared, mismatches, max_diff_pct).
    """
    import duckdb

    lo, hi = int(df["timestamp"].min()), int(df["timestamp"].max())
    try:
        con = duckdb.connect(db_path, read_only=True)
        try:
            existing = con.execute(
                "SELECT timestamp, close FROM market_data WHERE symbol = ? AND "
                "exchange = ? AND interval = ? AND timestamp BETWEEN ? AND ?",
                [symbol, exchange, interval, lo, hi]).df()
        finally:
            con.close()
    except Exception:
        return 0, 0, 0.0
    if existing.empty:
        return 0, 0, 0.0

    merged = df[["timestamp", "close"]].merge(
        existing, on="timestamp", suffixes=("_new", "_old"))
    if merged.empty:
        return 0, 0, 0.0
    diff_pct = ((merged["close_new"] - merged["close_old"]).abs()
                / merged["close_old"].replace(0, pd.NA) * 100.0).fillna(0.0)
    mismatches = int((diff_pct > COMPARE_TOLERANCE_PCT).sum())
    return len(merged), mismatches, float(diff_pct.max())


def _chunks(start: pd.Timestamp, end: pd.Timestamp, days: int):
    cur = start
    while cur <= end:
        stop = min(cur + pd.Timedelta(days=days - 1), end)
        yield cur.date().isoformat(), stop.date().isoformat()
        cur = stop + pd.Timedelta(days=1)


def backfill(symbols: list[str], start: str, end: str | None = None, *,
             interval: str = "1m", exchange: str = "NSE",
             db_path: str = "db/openalgo.db", pause: float = 0.4,
             dry_run: bool = False,
             session: tuple[str, str | None, str] | None = None,
             compare_existing: bool = False,
             keep_dates: set[dt.date] | None = None) -> list[SymbolResult]:
    """Download `symbols` over [start, end] in chunks and upsert into Historify.

    Idempotent: `upsert_market_data` keys on (symbol, exchange, interval, timestamp),
    so re-running a range that is already present rewrites the same rows rather than
    duplicating them. Safe to interrupt and re-run.

    `session` overrides where the (auth_token, feed_token, broker_name) triple comes
    from. Default is `_broker_session(db_path)` - the ONE broker this OpenAlgo
    instance is logged into for live trading. Pass an explicit triple to pull from a
    SECOND broker instead (see `broker_login.py`): this never touches the `auth`
    table, so it cannot disturb the live session other code depends on.

    `compare_existing=True` reads whatever is already stored for each chunk's range
    BEFORE writing over it, and tallies how many overlapping bars disagreed beyond
    `COMPARE_TOLERANCE_PCT` on close - the audit trail behind "trust the new source,
    but confirm the old one wasn't already right". The write proceeds unconditionally
    either way: this makes the new source authoritative for the whole range while
    still reporting where the two disagreed, rather than silently overwriting.

    `keep_dates` (IST session dates) drops every returned bar outside those dates before
    anything is written. Gap repair passes the missing sessions so that a broker answering
    with more than was asked (Flattrade daily appends today's bar) cannot rewrite a session
    that is already stored.
    """
    from database.historify_db import upsert_market_data
    from services.history_service import get_history_with_auth

    auth, feed, broker = session or (("", None, "") if dry_run else _broker_session(db_path))
    lo = pd.Timestamp(start)
    hi = pd.Timestamp(end) if end else pd.Timestamp.now(tz="Asia/Kolkata").tz_localize(None).normalize()
    step = CHUNK_DAYS if interval == "1m" else DAILY_CHUNK_DAYS
    hist_db_path = "db/historify.duckdb"
    results: list[SymbolResult] = []
    consecutive_zero = 0

    for sym in symbols:
        res = SymbolResult(symbol=sym)
        for c0, c1 in _chunks(lo, hi, step):
            if dry_run:
                res.chunks_ok += 1
                continue
            try:
                ok, resp, _ = get_history_with_auth(
                    auth, feed, broker, sym, exchange, interval, c0, c1)
                rows = resp.get("data") if ok and isinstance(resp, dict) else None
                if not rows:
                    res.chunks_empty += 1
                else:
                    df = pd.DataFrame(rows)
                    if "time" in df.columns and "timestamp" not in df.columns:
                        df["timestamp"] = df["time"]
                    if keep_dates is not None:
                        day = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(
                            "Asia/Kolkata").dt.date
                        df = df[day.isin(keep_dates)]
                    # An instrument the broker cannot price comes back as a zero-filled bar
                    # (Flattrade daily, COCHINSHIP: 9 all-zero rows were stored). Not data.
                    priced = (df[["open", "high", "low", "close"]].apply(
                        pd.to_numeric, errors="coerce") > 0).all(axis=1)
                    res.rejected += int((~priced).sum())
                    df = df[priced]
                    if df.empty:
                        res.chunks_empty += 1
                        time.sleep(pause)
                        continue
                    if compare_existing:
                        n, mism, worst = _compare_to_existing(
                            df, sym, exchange, interval, hist_db_path)
                        res.compared += n
                        res.mismatches += mism
                        res.max_diff_pct = max(res.max_diff_pct, worst)
                        if mism:
                            logger.warning(
                                f"{sym} {c0}..{c1}: {mism}/{n} overlapping bars disagree "
                                f"with the existing store by >{COMPARE_TOLERANCE_PCT:.1f}% "
                                f"(worst {worst:.2f}%) - new source is replacing them")
                    res.stored += upsert_market_data(df, sym, exchange, interval)
                    res.chunks_ok += 1
                    ts = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(
                        "Asia/Kolkata")
                    res.first = min(filter(None, [res.first, str(ts.min().date())]))
                    res.last = max(filter(None, [res.last, str(ts.max().date())]))
            except Exception as exc:                       # one bad chunk must not end the run
                res.chunks_failed += 1
                res.error = str(exc)[-120:]
                logger.exception(f"backfill chunk failed: {sym} {c0}..{c1}")
            time.sleep(pause)
        results.append(res)
        logger.info(f"backfill {sym}: stored={res.stored} ok={res.chunks_ok} "
                   f"empty={res.chunks_empty} failed={res.chunks_failed} "
                   f"compared={res.compared} mismatches={res.mismatches}")

        # A dead session token (or a revoked key) makes every chunk for every
        # remaining symbol come back looking exactly like "no data for this
        # range" - Angel's own plugin collapses "auth failed" and "genuinely
        # nothing traded" into the same empty DataFrame before this function
        # ever sees the difference (see docs/PRD.md, 2026-09-14: a token died at
        # midnight IST mid-run and burned 30+ minutes silently "backfilling"
        # thousands of empty chunks before anyone noticed). Three consecutive
        # symbols with ZERO bars stored across their entire requested range is
        # not a plausible coincidence - even a very recent listing should have
        # picked up its live months - so treat it as a dead session and stop
        # immediately instead of grinding through the rest of `symbols` the
        # same way.
        consecutive_zero = 0 if (res.stored or dry_run) else consecutive_zero + 1
        if consecutive_zero >= 3:
            raise RuntimeError(
                f"backfill stopped: {consecutive_zero} consecutive symbols "
                f"(...,{results[-3].symbol},{results[-2].symbol},{results[-1].symbol}) "
                f"stored zero bars each across the full {start}..{end or 'now'} range. "
                f"This is the signature of a dead/expired session token, not a real "
                f"data gap - re-run login() for a fresh session and resume from "
                f"{sym!r} onward rather than continuing.")
    return results


def extend_existing(interval: str = "1m", *, min_stale: int = 2,
                    historify_db: str = "db/historify.duckdb",
                    through: dt.date | None = None, skip: Iterable[str] = (),
                    **kw) -> list[SymbolResult]:
    """Bring every symbol already in the store up to today.

    Starts each symbol one day before its own last bar, so a partially-downloaded final
    session is completed rather than left with a hole in the middle of it.

    `through` (a calendar session, see daily_target) fetches every symbol whose last bar is older
    than it. Without it, `min_stale` calendar days decide, which skips a symbol one day behind:
    run daily, that fetched the bulk of the store only every other trading day.

    A symbol is also retried when it lags the store-wide latest bar, even if it is under
    `min_stale`. Without this, a symbol whose write failed today (e.g. a DuckDB lock
    conflict) sits one day behind and is skipped by `min_stale` until it is 2 days stale.
    On a market holiday nothing lags, so nothing is fetched.

    `skip` names symbols the broker is known not to serve on this interval; they are not fetched.
    """
    cov = coverage(interval, historify_db)
    if cov.empty:
        return []
    behind = cov["last"] < through if through is not None else cov.stale_days >= min_stale
    todo = cov[(behind | (cov["last"] < cov["last"].max())) & ~cov["symbol"].isin(set(skip))]
    out = []
    for row in todo.itertuples():
        start = (pd.Timestamp(row.last) - pd.Timedelta(days=1)).date().isoformat()
        out += backfill([row.symbol], start, interval=interval,
                        **{**kw, "exchange": getattr(row, "exchange", "NSE")})
    return out


def _expected_sessions() -> Callable[[dt.date, dt.date], Iterable[dt.date]]:
    """The NSE calendar's `expected_sessions` (holidays out, special sessions in).

    The calendar covers 2016-2026 and raises past its end, which makes gap repair and
    verification fail loudly on 2027-01-01 instead of guessing at holidays."""
    from signal_engine.backtest.data_readiness.calendar import load_calendar

    return load_calendar().expected_sessions


def expected_last_session(expected: Callable[[dt.date, dt.date], Iterable[dt.date]],
                          now: pd.Timestamp,
                          complete_at: dt.time = SESSION_COMPLETE_IST) -> dt.date | None:
    """The latest session whose bars should be in the store at `now` (an IST timestamp)."""
    day = now.date()
    if now.time() < complete_at:
        day -= dt.timedelta(days=1)
    sessions = list(expected(day - dt.timedelta(days=15), day))
    return sessions[-1] if sessions else None


def daily_target(expected: Callable[[dt.date, dt.date], Iterable[dt.date]],
                 now: pd.Timestamp) -> dt.date | None:
    """The session a --daily run brings the store up to: today once the cash market has closed,
    otherwise the last session before today. Holidays and weekends resolve to the last session."""
    return expected_last_session(expected, now, complete_at=MARKET_CLOSE_IST)


def _split_run(run: list[dt.date], max_days: int) -> list[tuple[dt.date, dt.date]]:
    """Cut a run of consecutive missing sessions into ranges of at most `max_days` calendar days."""
    out, start, prev = [], run[0], run[0]
    for day in run[1:]:
        if (day - start).days + 1 > max_days:
            out.append((start, prev))
            start = day
        prev = day
    out.append((start, prev))
    return out


def find_gaps(interval: str, *, expected: Callable[[dt.date, dt.date], Iterable[dt.date]],
              historify_db: str = "db/historify.duckdb", depth_days: int | None = None,
              today: dt.date | None = None) -> dict[tuple[str, str], list[tuple[dt.date, dt.date]]]:
    """Expected sessions with no bar INSIDE each symbol's own first..last span.

    `extend_existing` only moves forward from a symbol's last bar, so a session missing in the
    middle is never looked at again. Sessions before a symbol's first bar (a later listing) and
    after its last bar (staleness, which `extend_existing` owns) are not gaps. `depth_days`
    drops sessions older than that many days: the broker no longer serves them.

    Returns {(symbol, exchange): [(first_missing, last_missing), ...]}, each range a run of
    consecutive expected sessions no longer than one request chunk."""
    import duckdb

    today = today or pd.Timestamp.now(tz="Asia/Kolkata").date()
    horizon = today - dt.timedelta(days=depth_days) if depth_days else None
    max_days = CHUNK_DAYS if interval == "1m" else DAILY_CHUNK_DAYS
    con = duckdb.connect(historify_db, read_only=True)
    try:
        # Integer day arithmetic on the IST-shifted epoch: no dependence on the session time zone.
        df = con.execute(
            "SELECT symbol, exchange, DATE '1970-01-01' + CAST((timestamp + 19800) // 86400 "
            "AS INTEGER) AS d FROM market_data WHERE interval = ? GROUP BY 1, 2, 3",
            [interval]).df()
    finally:
        con.close()
    gaps: dict[tuple[str, str], list[tuple[dt.date, dt.date]]] = {}
    for (sym, exch), grp in df.groupby(["symbol", "exchange"]):
        have = set(pd.to_datetime(grp["d"]).dt.date)
        first, last = min(have), max(have)
        lo = max(first, horizon) if horizon else first
        runs: list[list[dt.date]] = []
        run: list[dt.date] = []
        for session in expected(lo, last):
            if session in have:
                if run:
                    runs.append(run)
                    run = []
            else:
                run.append(session)
        if run:
            runs.append(run)
        ranges = [rng for r in runs for rng in _split_run(r, max_days)]
        if ranges:
            gaps[(sym, exch)] = ranges
    return gaps


def _canary_alive(session: tuple[str, str | None, str], interval: str, day: dt.date) -> bool:
    """Does a liquid instrument on a recent session still answer? An empty reply means the
    session token is dead; the broker plugin reports both as the same empty result."""
    from services.history_service import get_history_with_auth

    ok, resp, _ = get_history_with_auth(*session, "SBIN", "NSE", interval, day.isoformat(),
                                        day.isoformat())
    return bool(ok and isinstance(resp, dict) and resp.get("data"))


def repair_gaps(interval: str = "1m", *, depth_days: int | None = None, dry_run: bool = False,
                max_empty_streak: int = 60, max_requests: int | None = None,
                canary: Callable[[], bool] | None = None,
                historify_db: str = "db/historify.duckdb", **kw) -> list[SymbolResult]:
    """Refetch the sessions `find_gaps` reports, and write only those sessions.

    The broker does not serve every old 1m session (Flattrade's depth is patchy: measured
    2026-10-19, the 60 oldest gap requests all came back empty on a valid token), so an empty
    reply is not an error. A run of `max_empty_streak` empties is checked against `canary`
    (default: SBIN on the latest session). If the canary also fails the token is dead and the
    repair stops; if it answers, the ranges are unservable and the repair carries on.

    The oldest gaps go first: they are the ones about to fall out of the broker's depth. With
    `max_requests` the run stops after that many requests, so a run can end before the session
    token dies at the 16:00 AutoStop; re-running continues with whatever is still missing."""
    if depth_days is None and interval == "1m":
        depth_days = INTRADAY_DEPTH_DAYS
    expected = _expected_sessions()
    gaps = find_gaps(interval, expected=expected, historify_db=historify_db, depth_days=depth_days)
    if canary is None and not dry_run:
        session = kw.get("session") or _broker_session()
        kw = {**kw, "session": session}
        latest = expected_last_session(expected, pd.Timestamp.now(tz="Asia/Kolkata"))

        def canary() -> bool:
            return _canary_alive(session, interval, latest)
    queue = sorted((first, last, sym, exch) for (sym, exch), ranges in gaps.items()
                   for first, last in ranges)
    if max_requests is not None:
        queue = queue[:max_requests]
    out: list[SymbolResult] = []
    empty = 0
    for first, last, sym, exch in queue:
        res = backfill([sym], first.isoformat(), last.isoformat(), interval=interval,
                       exchange=exch, keep_dates=set(expected(first, last)),
                       dry_run=dry_run, **kw)
        out += res
        empty = 0 if (dry_run or any(r.stored for r in res)) else empty + 1
        if empty >= max_empty_streak:
            if canary is not None and canary():
                logger.warning(f"gap repair: {empty} consecutive requests returned nothing "
                               f"(last {sym} {first}..{last}) but the session answers - "
                               "those ranges are not served by the broker")
                empty = 0
                continue
            raise RuntimeError(
                f"gap repair stopped: {empty} consecutive gap requests returned nothing "
                f"(last {sym} {first}..{last}). This is the signature of a dead/expired "
                f"session token, not of {empty} real gaps.")
    return out


def verify_current(interval: str = "1m", *, expected_last: dt.date,
                   historify_db: str = "db/historify.duckdb",
                   tolerate: Iterable[str] = ()) -> pd.DataFrame:
    """Symbols whose latest bar is older than `expected_last`, minus the `tolerate`d ones.

    A run that exits 0 proves only that nothing raised: an empty broker answer is stored as
    nothing and reported as success. This is the check that the store is actually current.

    A symbol stored under another interval but never under this one is reported too, with no
    `last` and no `behind_days`: coverage() only sees what this interval holds, so M&M (1m
    history, no daily bars at all) was invisible here."""
    tolerate = set(tolerate)
    cov = coverage(interval, historify_db)
    bad = cov[(cov["last"] < expected_last) & ~cov["symbol"].isin(tolerate)] if not cov.empty else cov
    bad = bad.assign(behind_days=[(expected_last - d).days for d in bad["last"]])
    held = set(zip(cov["symbol"], cov["exchange"], strict=True)) if not cov.empty else set()
    missing = sorted(p for p in _stored_pairs(historify_db) - held if p[0] not in tolerate)
    if missing:
        absent = pd.DataFrame({"symbol": [s for s, _ in missing],
                               "exchange": [e for _, e in missing],
                               "last": None, "behind_days": None})
        bad = absent if bad.empty else pd.concat([bad, absent], ignore_index=True)
    return bad.reset_index(drop=True)


def _stored_pairs(db_path: str) -> set[tuple[str, str]]:
    """Every (symbol, exchange) the store holds under any interval."""
    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    try:
        rows = con.execute("SELECT DISTINCT symbol, exchange FROM market_data").fetchall()
    finally:
        con.close()
    return {(s, e) for s, e in rows}


def main() -> None:
    import argparse

    from signal_engine.backtest import data

    ap = argparse.ArgumentParser(prog="signal_engine.backtest.backfill")
    ap.add_argument("--symbols", help="comma-separated; default: extend what is already stored")
    ap.add_argument("--fno", action="store_true", help="the whole F&O underlying list")
    ap.add_argument("--start", default=None, help="YYYY-MM-DD (required with --symbols/--fno)")
    ap.add_argument("--end", default=None)
    ap.add_argument("--interval", default="1m", choices=["1m", "D"])
    ap.add_argument("--status", action="store_true", help="print coverage and exit")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--repair-gaps", action="store_true",
                    help="refetch sessions missing inside each symbol's span (1m: only within "
                         "the broker's depth); stores only the missing sessions")
    ap.add_argument("--depth-days", type=int, default=None,
                    help="with --repair-gaps: ignore gaps older than this many days")
    ap.add_argument("--max-requests", type=int, default=None,
                    help="with --repair-gaps: stop after this many gap requests (oldest first)")
    ap.add_argument("--verify", action="store_true",
                    help="exit 1 if any symbol's latest bar is older than the last expected "
                         "session; also counts interior gaps")
    ap.add_argument("--tolerate", default="",
                    help="with --verify: comma-separated symbols already known to be unservable")
    ap.add_argument("--skip", default="",
                    help="extend: comma-separated symbols not to fetch (the broker never serves them "
                         "on this interval)")
    ap.add_argument("--daily", action="store_true",
                    help="the scheduled run: extend every symbol behind the calendar's latest "
                         "session (today once the cash market has closed), and verify against it")
    args = ap.parse_args()

    logging.getLogger().setLevel(logging.WARNING)
    pd.set_option("display.max_rows", 300)

    if args.status:
        print(coverage(args.interval).to_string(index=False))
        return

    import sys

    from signal_engine.backtest.data_readiness.calendar import CalendarError

    try:
        if _main_calendar_modes(args):
            return
    except CalendarError as exc:
        print(CALENDAR_ENDED_MESSAGE.format(detail=exc))
        sys.exit(EXIT_CALENDAR_ENDED)

    if args.fno:
        syms = [s.replace(".NS", "") for s in data.refresh_fno()]
    elif args.symbols:
        syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        skip = {s.strip().upper() for s in args.skip.split(",") if s.strip()}
        res = extend_existing(args.interval, dry_run=args.dry_run, skip=skip, **_extend_gate(args))
        print(pd.DataFrame([vars(r) for r in res]).to_string(index=False))
        return

    if not args.start:
        ap.error("--start is required with --symbols or --fno")
    res = backfill(syms, args.start, args.end, interval=args.interval, dry_run=args.dry_run)
    print(pd.DataFrame([vars(r) for r in res]).to_string(index=False))


def _extend_gate(args) -> dict:
    """extend_existing's gate for this run. --daily targets the calendar session; when the
    calendar cannot answer it falls back to every symbol not updated today, so the store keeps
    moving and verify (exit 3) reports the calendar."""
    if not args.daily:
        return {}
    from signal_engine.backtest.data_readiness.calendar import CalendarError

    try:
        return {"through": daily_target(_expected_sessions(), _now())}
    except CalendarError as exc:
        print(f"WARNING: calendar cannot give today's target ({exc}); extending every symbol "
              "not updated today instead")
        return {"min_stale": 1}


def _main_calendar_modes(args) -> bool:
    """--verify and --repair-gaps: the modes that need the NSE calendar. True when one ran (verify
    exits through `sys.exit`); `main` turns a `CalendarError` into the calendar-ended message."""
    import sys

    if args.verify:
        expected = _expected_sessions()
        last = (daily_target if args.daily else expected_last_session)(expected, _now())
        tolerate = {s.strip().upper() for s in args.tolerate.split(",") if s.strip()}
        bad = verify_current(args.interval, expected_last=last, tolerate=tolerate)
        depth = INTRADAY_DEPTH_DAYS if args.interval == "1m" else None
        gaps = find_gaps(args.interval, expected=expected, depth_days=depth)
        n_sessions = sum(len(list(expected(a, b))) for rs in gaps.values() for a, b in rs)
        print(f"verify {args.interval}: expected last session {last}; symbols behind: {len(bad)}; "
              f"interior gaps within depth: {n_sessions} sessions in {len(gaps)} symbols")
        if len(bad):
            print(bad.to_string(index=False))
        sys.exit(1 if len(bad) else 0)

    if args.repair_gaps:
        res = repair_gaps(args.interval, depth_days=args.depth_days, dry_run=args.dry_run,
                          max_requests=args.max_requests)
        got = [r for r in res if r.stored]
        print(f"repair {args.interval}: {len(res)} gap requests, {len(got)} stored "
              f"{sum(r.stored for r in got)} bars, {len(res) - len(got)} returned nothing")
        print(pd.DataFrame([vars(r) for r in res if not r.stored or r.chunks_failed]
                           ).to_string(index=False))
        return True
    return False


if __name__ == "__main__":
    main()
