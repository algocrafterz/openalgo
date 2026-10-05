"""Staging fetch: download broker bars into a separate DuckDB, never into the landing store.

This is the "fetch -> stage" half of the remediation pipeline (fetch -> stage -> validate ->
compare -> publish). A staging file has the landing store's `market_data` columns, so the Phase 1
audit (`audit.run_audit(db=<staging file>)`) validates it unchanged. Prices are stored exactly as
the broker returned them: no adjustment, normalization or repair happens here.

Two extra tables make a staging file self-describing:
    fetch_log   one row per request (status ok / empty / error) plus one `complete` row per
                symbol whose every request succeeded; a resumed fetch skips complete symbols.
    stage_meta  source broker, interval, requested range, fetch time (IST).

    uv run python -m signal_engine.backtest.data_readiness.staging --interval D \
        --start 2019-12-01 --end 2026-10-03 --out db/staging/angel-D-20261004.duckdb
"""

from __future__ import annotations

import argparse
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

from signal_engine.backtest import backfill as _backfill
from signal_engine.backtest import data as _data
from signal_engine.timeutils import IST
from utils.logging import get_logger

logger = get_logger(__name__)

#: Anchored to the repository root, so the guard holds whatever the working directory is.
_REPO_ROOT = Path(__file__).resolve().parents[3]
LANDING_STORE = (_REPO_ROOT / _data.HISTORIFY_DB).resolve()

#: Longest request per interval. Shared with the backfill so both stay below Angel One's own
#: chunk limits (see backfill.CHUNK_DAYS for the boundary bug this avoids).
SAFE_DAYS = {"1m": _backfill.CHUNK_DAYS, "D": _backfill.DAILY_CHUNK_DAYS}

#: Consecutive symbols with zero stored rows that mean the broker session died.
DEAD_SESSION_SYMBOLS = 3

REQUIRED = ("timestamp", "open", "high", "low", "close", "volume")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_data (
    symbol VARCHAR NOT NULL, exchange VARCHAR NOT NULL, interval VARCHAR NOT NULL,
    timestamp BIGINT NOT NULL, open DOUBLE NOT NULL, high DOUBLE NOT NULL, low DOUBLE NOT NULL,
    close DOUBLE NOT NULL, volume BIGINT NOT NULL, oi BIGINT DEFAULT 0,
    created_at TIMESTAMP NOT NULL,
    PRIMARY KEY (symbol, exchange, interval, timestamp));
CREATE TABLE IF NOT EXISTS fetch_log (
    symbol VARCHAR, exchange VARCHAR, interval VARCHAR, req_start VARCHAR, req_end VARCHAR,
    status VARCHAR, rows BIGINT, detail VARCHAR, fetched_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS stage_meta (key VARCHAR PRIMARY KEY, value VARCHAR);
"""

Fetch = Callable[[str, str, str, str, str], tuple[bool, dict, int]]


class StagingError(RuntimeError):
    """The staging fetch cannot continue safely."""


@dataclass
class FetchSummary:
    symbol: str
    rows: int = 0
    chunks_ok: int = 0
    chunks_empty: int = 0
    chunks_failed: int = 0
    first: str | None = None
    last: str | None = None
    errors: list[str] = field(default_factory=list)


def _now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None, microsecond=0)


def _is_landing_store(target: Path) -> bool:
    if target == LANDING_STORE:
        return True
    # samefile also catches a hard link or a second path to the same file.
    return target.exists() and LANDING_STORE.exists() and os.path.samefile(target, LANDING_STORE)


def open_staging(path: str | Path) -> duckdb.DuckDBPyConnection:
    """Open (or create) a staging DuckDB.

    Refuses the landing store, and any existing DuckDB file that is not already a staging file,
    so the schema is never added to an unrelated database.
    """
    target = Path(path).resolve()
    if _is_landing_store(target):
        raise StagingError(f"refusing to stage into the landing store {target}")
    existed = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(target))
    if existed and not con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'stage_meta'").fetchone():
        con.close()
        raise StagingError(f"{target} exists and is not a staging file")
    con.execute(_SCHEMA)
    return con


def _spans(start: str, end: str, interval: str) -> list[tuple[str, str]]:
    if interval not in SAFE_DAYS:
        raise StagingError(f"unsupported interval {interval!r}")
    return list(_backfill._chunks(pd.Timestamp(start), pd.Timestamp(end), SAFE_DAYS[interval]))


def _frame(resp: dict) -> pd.DataFrame:
    """The response rows as a frame, or raise ValueError naming the defect."""
    df = pd.DataFrame(resp.get("data") or [])
    if df.empty:
        return df
    if "time" in df.columns and "timestamp" not in df.columns:
        df = df.rename(columns={"time": "timestamp"})
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ValueError(f"missing columns {missing}")
    if not pd.api.types.is_integer_dtype(df["timestamp"]):
        raise ValueError(f"non-epoch timestamp dtype {df['timestamp'].dtype}")
    vol = pd.to_numeric(df["volume"], errors="coerce")
    if vol.isna().any() or (vol != vol.round()).any():
        raise ValueError("non-integral or missing volume")
    dups = int(df["timestamp"].duplicated().sum())
    if dups:
        raise ValueError(f"{dups} duplicate timestamps in one response")
    return df


def _insert(con, df: pd.DataFrame, symbol: str, exchange: str, interval: str) -> int:
    rows = pd.DataFrame({
        "symbol": symbol, "exchange": exchange, "interval": interval,
        "timestamp": df["timestamp"].astype("int64"),
        "open": df["open"], "high": df["high"], "low": df["low"], "close": df["close"],
        "volume": df["volume"], "oi": df["oi"] if "oi" in df.columns else 0,
        "created_at": _now_ist(),
    })
    con.register("_incoming", rows)
    try:
        con.execute("INSERT INTO market_data SELECT * FROM _incoming")
    finally:
        con.unregister("_incoming")
    return len(rows)


def _log(con, symbol, exchange, interval, a, b, status, rows, detail=""):
    con.execute("INSERT INTO fetch_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [symbol, exchange, interval, a, b, status, rows, detail, _now_ist()])


def _complete(con, exchange: str, interval: str) -> set[str]:
    return {r[0] for r in con.execute(
        "SELECT symbol FROM fetch_log WHERE status = 'complete' AND exchange = ? AND interval = ?",
        [exchange, interval]).fetchall()}


#: DuckDB errors that mean the response itself is bad. Anything else (disk, IO, a closed
#: connection) stops the run instead of being logged as a bad chunk.
_DATA_ERRORS = (duckdb.ConstraintException, duckdb.ConversionException,
                duckdb.InvalidInputException)


def _check_resume(con, meta: dict[str, str]) -> None:
    """A staging file holds exactly one source, interval, exchange and range."""
    have = dict(con.execute("SELECT key, value FROM stage_meta").fetchall())
    for key in ("source", "interval", "exchange", "requested_start", "requested_end"):
        if key in have and have[key] != meta[key]:
            raise StagingError(f"staging file was started with {key}={have[key]!r}, not "
                               f"{meta[key]!r}: use a new file for a different range or source")


def _fetch_chunk(con, fetch: Fetch, res: FetchSummary, exchange, interval, a, b) -> None:
    try:
        ok, resp, code = fetch(res.symbol, exchange, interval, a, b)
        if not ok or not isinstance(resp, dict):
            raise ValueError(f"broker error {code}: {str(resp)[:160]}")
        df = _frame(resp)
        if df.empty:
            res.chunks_empty += 1
            _log(con, res.symbol, exchange, interval, a, b, "empty", 0)
            return
        n = _insert(con, df, res.symbol, exchange, interval)
    except (ValueError, *_DATA_ERRORS) as exc:
        res.chunks_failed += 1
        res.errors.append(f"{a}..{b}: {exc}")
        _log(con, res.symbol, exchange, interval, a, b, "error", 0, str(exc)[:500])
        logger.error(f"staging chunk failed: {res.symbol} {interval} {a}..{b}: {exc}")
        return
    res.rows += n
    res.chunks_ok += 1
    _log(con, res.symbol, exchange, interval, a, b, "ok", n)
    days = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_convert(IST).dt.date
    res.first = min(filter(None, [res.first, str(days.min())]))
    res.last = max(filter(None, [res.last, str(days.max())]))


def fetch_into_staging(con, symbols: Sequence[str], start: str, end: str, *, interval: str,
                       exchange: str, source: str, fetch: Fetch,
                       pause: float = 0.0) -> list[FetchSummary]:
    """Fetch `symbols` over [start, end] into the staging connection.

    Every request is logged. A symbol is marked complete only when none of its requests failed,
    so a resumed fetch retries it. Raises StagingError when DEAD_SESSION_SYMBOLS symbols in a row
    store nothing: that is a dead broker session, not a data gap.
    """
    spans = _spans(start, end, interval)
    meta = {"source": source, "interval": interval, "exchange": exchange,
            "requested_start": start, "requested_end": end,
            "fetched_at_ist": _now_ist().isoformat(sep=" ")}
    _check_resume(con, meta)
    con.executemany("INSERT OR IGNORE INTO stage_meta VALUES (?, ?)", list(meta.items()))
    done = _complete(con, exchange, interval)
    results: list[FetchSummary] = []
    zero_run = 0
    for sym in symbols:
        if sym in done:
            continue
        res = FetchSummary(symbol=sym)
        for a, b in spans:
            _fetch_chunk(con, fetch, res, exchange, interval, a, b)
            if pause:
                time.sleep(pause)
        # Empty everywhere is not complete: a dying session answers "ok, no rows".
        if not res.chunks_failed and res.rows:
            _log(con, sym, exchange, interval, start, end, "complete", res.rows)
        results.append(res)
        logger.info(f"staged {sym} {interval}: rows={res.rows} ok={res.chunks_ok} "
                    f"empty={res.chunks_empty} failed={res.chunks_failed}")
        zero_run = zero_run + 1 if res.rows == 0 else 0
        if zero_run >= DEAD_SESSION_SYMBOLS:
            names = ", ".join(r.symbol for r in results[-DEAD_SESSION_SYMBOLS:])
            raise StagingError(
                f"{zero_run} consecutive symbols ({names}) stored zero rows: dead broker "
                f"session, not a data gap. Log in again and resume from {sym!r}.")
    return results


def _landing_symbols(interval: str, exchange: str) -> list[str]:
    con = duckdb.connect(str(LANDING_STORE), read_only=True, config=_data._DUCKDB_CONFIG)
    try:
        return [r[0] for r in con.execute(
            "SELECT DISTINCT symbol FROM market_data WHERE interval = ? AND exchange = ? "
            "ORDER BY symbol", [interval, exchange]).fetchall()]
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="signal_engine.backtest.data_readiness.staging")
    ap.add_argument("--interval", required=True, choices=sorted(SAFE_DAYS))
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--exchange", default="NSE")
    ap.add_argument("--symbols", help="comma-separated; default: the landing store's symbols")
    ap.add_argument("--pause", type=float, default=0.35)
    ap.add_argument("--session", choices=("secondary", "live"), default="secondary",
                    help="secondary: broker_login (.secondary_broker.env); live: the logged-in "
                         "OpenAlgo session, read from db/openalgo.db without writing to it")
    args = ap.parse_args(argv)

    from services.history_service import get_history_with_auth
    from signal_engine.backtest import broker_login

    symbols = (args.symbols.split(",") if args.symbols
               else _landing_symbols(args.interval, args.exchange))
    if args.session == "live":
        auth, feed, broker = _backfill._broker_session()
        if not broker:
            raise StagingError("no live OpenAlgo broker session: log in to OpenAlgo first")
    else:
        auth, feed, broker = broker_login.login()

    def fetch(sym, exch, iv, a, b):
        return get_history_with_auth(auth, feed, broker, sym, exch, iv, a, b)

    con = open_staging(args.out)
    try:
        out = fetch_into_staging(con, symbols, args.start, args.end, interval=args.interval,
                                 exchange=args.exchange, source=broker, fetch=fetch,
                                 pause=args.pause)
    finally:
        con.close()
    failed = [r.symbol for r in out if r.chunks_failed]
    print(f"staged {len(out)} symbols into {args.out}; failed: {failed or 'none'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
