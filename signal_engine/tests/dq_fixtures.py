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

_COLUMN_NAMES = [c.split()[0] for c in COLUMNS.split(", ")]


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


def bulk_insert(con, table: str, columns: list[str], rows: list[tuple], chunk: int = 500) -> None:
    """Insert `rows` with multi-row `VALUES` statements, `chunk` rows per statement.

    `executemany` costs 3 to 9 ms per row in DuckDB; this loads the same rows far faster.
    Python None stays NULL and float nan/inf are kept, which a DataFrame insert would not do.
    Columns are named, so the order is exactly `columns`. Stays far below the parameter limit.
    """
    if not rows:
        return
    names = ", ".join(columns)
    one = "(" + ",".join("?" * len(columns)) + ")"
    for i in range(0, len(rows), chunk):
        part = rows[i : i + chunk]
        flat = [v for row in part for v in row]
        con.execute(f"INSERT INTO {table} ({names}) VALUES " + ",".join([one] * len(part)), flat)


def make_store(path: Path, rows: list[tuple]) -> Path:
    """Create a DuckDB file with a `market_data` table holding `rows`."""
    con = duckdb.connect(str(path))
    try:
        con.execute(f"CREATE TABLE market_data ({COLUMNS})")
        bulk_insert(con, "market_data", _COLUMN_NAMES, rows)
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
