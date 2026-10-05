"""Synthetic Historify landing stores for the data-readiness tests.

The table has the same columns as the real `market_data` table. Rows are built with IST wall-clock
times, so a fixture states the session it means without epoch arithmetic.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

IST = "Asia/Kolkata"

COLUMNS = (
    "symbol VARCHAR, exchange VARCHAR, interval VARCHAR, timestamp BIGINT, open DOUBLE, "
    "high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT, oi BIGINT, created_at TIMESTAMP"
)


def ts(day: str, hhmm: str) -> int:
    """Epoch seconds of an IST wall-clock time."""
    return int(pd.Timestamp(f"{day} {hhmm}", tz=IST).timestamp())


def daily(symbol, day, o=100.0, h=105.0, lo=99.0, c=101.0, v=1000, hhmm="05:30",
          exchange="NSE", created=None):
    """One daily row. `created` defaults to 16:00 IST on the session date (after the close)."""
    made = created or datetime.fromisoformat(f"{day} 16:00")
    return (symbol, exchange, "D", ts(day, hhmm), o, h, lo, c, v, 0, made)


def minute(symbol, day, hhmm, o=100.0, h=100.5, lo=99.5, c=100.2, v=10, exchange="NSE"):
    """One 1m row, written after the close."""
    return (symbol, exchange, "1m", ts(day, hhmm), o, h, lo, c, v, 0,
            datetime.fromisoformat(f"{day} 16:00"))


def make_store(path: Path, rows: list[tuple]) -> Path:
    """Create a DuckDB file with a `market_data` table holding `rows`."""
    con = duckdb.connect(str(path))
    try:
        con.execute(f"CREATE TABLE market_data ({COLUMNS})")
        if rows:
            con.executemany("INSERT INTO market_data VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    finally:
        con.close()
    return path


def weekdays(start: str, end: str) -> list[str]:
    """ISO dates of every Monday-Friday from `start` to `end` inclusive."""
    days = pd.bdate_range(start, end)
    return [d.date().isoformat() for d in days]


def clean_daily(symbol: str, days: list[str], **kw) -> list[tuple]:
    """A well-formed daily series for `symbol` on `days`."""
    return [daily(symbol, d, **kw) for d in days]
