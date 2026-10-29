"""SQLite file for one session of recorded quote ticks. A plain file under signal_engine/data,
separate from every platform database, so the recorder can never lock or fill one."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from signal_engine.shadow.tick_bars import Tick, parse_exchange_time
from signal_engine.timeutils import IST

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "shadow"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks (
    symbol TEXT NOT NULL, arrival_ms INTEGER NOT NULL, exch_time TEXT,
    ltp REAL NOT NULL, volume INTEGER NOT NULL, day_high REAL, day_low REAL
);
CREATE INDEX IF NOT EXISTS idx_ticks_symbol_time ON ticks (symbol, arrival_ms);
CREATE TABLE IF NOT EXISTS gaps (start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, reason TEXT);
"""


def db_path(day: str) -> Path:
    return DATA_DIR / f"ticks-{day}.db"


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    return conn


def tick_row(message: dict, arrival_ms: int) -> tuple | None:
    """The ticks row for one proxy `market_data` message, or None when it carries no usable price."""
    if message.get("type") != "market_data":
        return None
    data = message.get("data") or {}
    ltp = data.get("ltp")
    volume = data.get("volume")
    symbol = message.get("symbol")
    if not symbol or not ltp or ltp <= 0 or volume is None:
        return None
    return (
        symbol,
        arrival_ms,
        str(data["last_trade_time"]) if data.get("last_trade_time") else None,
        float(ltp),
        int(volume),
        float(data["high"]) if data.get("high") else None,
        float(data["low"]) if data.get("low") else None,
    )


def load_ticks(conn: sqlite3.Connection) -> dict[str, list[Tick]]:
    out: dict[str, list[Tick]] = {}
    for symbol, ms, exch, ltp, vol, hi, lo in conn.execute(
        "SELECT symbol, arrival_ms, exch_time, ltp, volume, day_high, day_low "
        "FROM ticks ORDER BY arrival_ms"
    ):
        arrival = pd.Timestamp(ms, unit="ms", tz="UTC").tz_convert(IST)
        out.setdefault(symbol, []).append(
            Tick(symbol, arrival, parse_exchange_time(exch, arrival), ltp, vol, hi, lo)
        )
    return out


def load_gaps(conn: sqlite3.Connection) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    return [
        (
            pd.Timestamp(s, unit="ms", tz="UTC").tz_convert(IST),
            pd.Timestamp(e, unit="ms", tz="UTC").tz_convert(IST),
        )
        for s, e in conn.execute("SELECT start_ms, end_ms FROM gaps ORDER BY start_ms")
    ]
