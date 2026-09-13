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

import logging
import sqlite3
import time
from dataclasses import dataclass

import pandas as pd

from utils.logging import get_logger

logger = get_logger(__name__)

#: One request per calendar month. ~7,500 one-minute bars against a cap near 58,000 -
#: deliberately conservative, because the failure mode is silent.
CHUNK_DAYS = 30

#: Daily bars are small enough that the whole history fits one request.
DAILY_CHUNK_DAYS = 4000


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
    """What the store holds per symbol: bars, span, sessions, and how stale it is."""
    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    try:
        df = con.execute(
            "SELECT symbol, count(*) AS bars, min(timestamp) AS t0, max(timestamp) AS t1 "
            "FROM market_data WHERE interval = ? GROUP BY symbol ORDER BY symbol",
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
    return df[["symbol", "bars", "first", "last", "stale_days"]]


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
             compare_existing: bool = False) -> list[SymbolResult]:
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
    """
    from database.historify_db import upsert_market_data
    from services.history_service import get_history_with_auth

    auth, feed, broker = session or _broker_session(db_path)
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
        # ever sees the difference (see PRD.md, 2026-09-14: a token died at
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
                    historify_db: str = "db/historify.duckdb", **kw) -> list[SymbolResult]:
    """Bring every symbol already in the store up to today.

    Starts each symbol one day before its own last bar, so a partially-downloaded final
    session is completed rather than left with a hole in the middle of it.
    """
    cov = coverage(interval, historify_db)
    todo = cov[cov.stale_days >= min_stale]
    out = []
    for row in todo.itertuples():
        start = (pd.Timestamp(row.last) - pd.Timedelta(days=1)).date().isoformat()
        out += backfill([row.symbol], start, interval=interval, **kw)
    return out


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
    args = ap.parse_args()

    logging.getLogger().setLevel(logging.WARNING)
    pd.set_option("display.max_rows", 300)

    if args.status:
        print(coverage(args.interval).to_string(index=False))
        return

    if args.fno:
        syms = [s.replace(".NS", "") for s in data.refresh_fno()]
    elif args.symbols:
        syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        res = extend_existing(args.interval, dry_run=args.dry_run)
        print(pd.DataFrame([vars(r) for r in res]).to_string(index=False))
        return

    if not args.start:
        ap.error("--start is required with --symbols or --fno")
    res = backfill(syms, args.start, args.end, interval=args.interval, dry_run=args.dry_run)
    print(pd.DataFrame([vars(r) for r in res]).to_string(index=False))


if __name__ == "__main__":
    main()
