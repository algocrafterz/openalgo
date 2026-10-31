"""Stored daily value areas (VAH / POC / VAL) for every Historify symbol and session.

WHY A STORE
    Value areas used to exist only while something computed them: the BREAKOUT runner and the
    breakout backtest rebuilt every session's profile from 1-minute bars on each run, and nothing
    tracked whether the 1-minute data behind them was complete. Volume- and market-profile
    research needs them as a dataset: one row per symbol per session, built the same way every
    time, with the parameters stored beside the numbers.

HOW EACH ROW IS BUILT
    `signal_engine.features.value_area.session_value_areas`, the Pine-parity implementation
    (volume spread over each bar's range, 70% from the POC outward, continuous session only,
    rows of 5 ticks). Not `backtest/volume_profile.py`, which bins each bar's close only and gives
    different levels. Tick is the symbol's current broker tick (SymToken), stored per row.

UPDATED BY
    The close-day `value_areas` step (signal_engine/ops/value_areas.py) after the 1-minute
    backfill; `build --full` rebuilds history. Rows are keyed (symbol, session_date, method), so a
    re-run replaces a session instead of duplicating it.

    uv run python -m signal_engine.features.value_area_store build [--symbols A,B] [--full]
    uv run python -m signal_engine.features.value_area_store status [--day YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta

import pandas as pd

from signal_engine.features.value_area import session_value_areas
from signal_engine.timeutils import IST

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "value_areas.db")
METHOD = "pine-range-spread-v1"
ROWS_PER_TICK = 5
VA_PCT = 70.0
CONTINUOUS_ONLY = True

_SCHEMA = """
CREATE TABLE IF NOT EXISTS value_areas (
    symbol TEXT NOT NULL,
    session_date TEXT NOT NULL,
    method TEXT NOT NULL,
    vah REAL,
    poc REAL,
    val REAL,
    volume REAL NOT NULL,
    bars INTEGER NOT NULL,
    tick REAL NOT NULL,
    rows_per_tick INTEGER NOT NULL,
    va_pct REAL NOT NULL,
    continuous_only INTEGER NOT NULL,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (symbol, session_date, method)
)
"""


def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(_SCHEMA)
    return conn


def _tick(symbol: str, last_close: float) -> float:
    from signal_engine.features.instruments import tick_for_price, tick_size

    try:
        return tick_size(symbol)
    except LookupError:
        return tick_for_price(last_close)


def rows_for(symbol: str, df1m: pd.DataFrame, tick: float) -> list[tuple]:
    """One row per session in `df1m` (in-session 1-minute bars, IST index)."""
    if df1m.empty:
        return []
    va = session_value_areas(df1m, tick, ROWS_PER_TICK, VA_PCT, CONTINUOUS_ONLY)
    by_day = df1m.groupby(df1m.index.date)
    stamp = datetime.now(IST).replace(microsecond=0).isoformat()
    out = []
    for day, row in va.iterrows():
        chunk = by_day.get_group(day)
        out.append(
            (
                symbol,
                str(day),
                METHOD,
                None if pd.isna(row["vah"]) else float(row["vah"]),
                None if pd.isna(row["poc"]) else float(row["poc"]),
                None if pd.isna(row["val"]) else float(row["val"]),
                float(chunk["Volume"].sum()),
                int(len(chunk)),
                float(tick),
                ROWS_PER_TICK,
                VA_PCT,
                int(CONTINUOUS_ONLY),
                stamp,
            )
        )
    return out


def _last_stored(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(
        conn.execute(
            "SELECT symbol, max(session_date) FROM value_areas WHERE method=? GROUP BY symbol",
            (METHOD,),
        )
    )


def _bars_since(loader, symbol: str, start: str | None, end: str | None, historify_db):
    """The symbol's 1-minute bars from `start`, or None when there are none yet.

    The Historify loader raises "no symbol ... has 0+ sessions" for an empty window, which on a
    daily run only means the session is not in the store yet, not that anything failed.
    """
    try:
        return loader(
            [symbol],
            interval="1m",
            start=start,
            end=end,
            min_sessions=0,
            db_path=historify_db,
            drop_split_days=False,
        ).get(symbol)
    except RuntimeError as exc:
        if "sessions at 1m" in str(exc):
            return None
        raise


def build(
    symbols: list[str] | None = None,
    *,
    full: bool = False,
    end: str | None = None,
    db_path: str = DB_PATH,
    historify_db=None,
) -> dict:
    """Compute and store every session not yet stored (all of history with `full`)."""
    from signal_engine.backtest.data import from_historify, historify_symbols

    conn = connect(db_path)
    try:
        last = {} if full else _last_stored(conn)
        symbols = symbols or historify_symbols("1m", historify_db)
        written, failed = 0, []
        for symbol in symbols:
            start = None
            if symbol in last:
                start = (date.fromisoformat(last[symbol]) + timedelta(days=1)).isoformat()
            try:
                bars = _bars_since(from_historify, symbol, start, end, historify_db)
                if bars is None or bars.empty:
                    continue  # no session after the last stored one: nothing to add
                rows = rows_for(symbol, bars, _tick(symbol, float(bars["Close"].iloc[-1])))
            except Exception as exc:  # noqa: BLE001 - one bad symbol must not stop the rest
                failed.append(f"{symbol}: {type(exc).__name__}: {exc}"[:200])
                continue
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO value_areas VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
                )
            written += len(rows)
        return {"symbols": len(symbols), "rows_written": written, "failed": failed}
    finally:
        conn.close()


def coverage(day: str, db_path: str = DB_PATH, historify_db=None) -> dict:
    """Symbols with 1-minute bars on `day` against symbols with a stored value area for it."""
    import duckdb

    from signal_engine.backtest.data import _DUCKDB_CONFIG, HISTORIFY_DB

    lo = int(pd.Timestamp(day, tz="Asia/Kolkata").timestamp())
    con = duckdb.connect(str(historify_db or HISTORIFY_DB), read_only=True, config=_DUCKDB_CONFIG)
    try:
        # Traded volume, not just bars: an index (NIFTY) has bars and no volume, so no profile.
        with_bars = {
            r[0]
            for r in con.execute(
                "SELECT symbol FROM market_data WHERE interval='1m' AND timestamp >= ? AND timestamp < ? "
                "GROUP BY symbol HAVING sum(volume) > 0",
                [lo, lo + 86400],
            ).fetchall()
        }
    finally:
        con.close()
    conn = connect(db_path)
    try:
        stored = {
            r[0]
            for r in conn.execute(
                "SELECT symbol FROM value_areas WHERE session_date=? AND method=? AND poc IS NOT NULL",
                (day, METHOD),
            )
        }
        total, first, latest = conn.execute(
            "SELECT count(*), min(session_date), max(session_date) FROM value_areas WHERE method=?",
            (METHOD,),
        ).fetchone()
    finally:
        conn.close()
    return {
        "day": day,
        "with_bars": len(with_bars),
        "stored": len(stored & with_bars),
        "missing": sorted(with_bars - stored),
        "rows": total,
        "first": first,
        "latest": latest,
    }


def load(
    symbols: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    db_path: str = DB_PATH,
) -> pd.DataFrame:
    """Stored value areas as a frame (symbol, session_date, vah, poc, val, volume, bars, tick)."""
    q = "SELECT symbol, session_date, vah, poc, val, volume, bars, tick FROM value_areas WHERE method=?"
    args: list = [METHOD]
    if symbols:
        q += f" AND symbol IN ({','.join('?' * len(symbols))})"
        args += list(symbols)
    if start:
        q += " AND session_date >= ?"
        args.append(start)
    if end:
        q += " AND session_date <= ?"
        args.append(end)
    conn = connect(db_path)
    try:
        return pd.read_sql_query(q + " ORDER BY symbol, session_date", conn, params=args)
    finally:
        conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="signal_engine.features.value_area_store")
    parser.add_argument("command", choices=("build", "status"))
    parser.add_argument("--symbols", help="comma-separated (default: every Historify 1m symbol)")
    parser.add_argument("--full", action="store_true", help="rebuild all of history")
    parser.add_argument("--day", default=datetime.now(IST).strftime("%Y-%m-%d"))
    args = parser.parse_args(argv)
    if args.command == "build":
        result = build(args.symbols.split(",") if args.symbols else None, full=args.full)
        print(
            f"{result['rows_written']} rows written for {result['symbols']} symbols; "
            f"{len(result['failed'])} failed"
        )
        for line in result["failed"]:
            print(f"  failed {line}")
        return 1 if result["failed"] else 0
    cov = coverage(args.day)
    print(
        f"{cov['day']}: {cov['stored']}/{cov['with_bars']} symbols stored; store {cov['rows']} rows "
        f"{cov['first']}..{cov['latest']}; missing {cov['missing'][:10]}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
