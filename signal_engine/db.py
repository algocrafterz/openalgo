"""SQLite persistence for trade audit trail."""

import os
import sqlite3

from loguru import logger

from signal_engine.models import Order, Signal, TradeResult

_DB_PATH = os.path.join(os.path.dirname(__file__), "data", "trades.db")

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT,
    direction TEXT,
    symbol TEXT,
    entry REAL,
    sl REAL,
    tp REAL,
    quantity INTEGER,
    order_id TEXT,
    status TEXT,
    message TEXT,
    signal_time TEXT,
    received_at TEXT,
    executed_at TEXT
)
"""

_INSERT = """
INSERT INTO trades (
    strategy, direction, symbol, entry, sl, tp,
    quantity, order_id, status, message,
    signal_time, received_at, executed_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(_CREATE_TABLE)
    conn.commit()
    return conn


def save(signal: Signal, order: Order, result: TradeResult) -> None:
    """Persist a completed trade to the audit trail."""
    try:
        conn = _get_connection()
        conn.execute(
            _INSERT,
            (
                signal.strategy,
                signal.direction.value,
                signal.symbol,
                signal.entry,
                signal.sl,
                signal.tp,
                order.quantity,
                result.order_id,
                result.status.value,
                result.message,
                signal.time or "",
                signal.received_at.isoformat(),
                result.timestamp.isoformat(),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to save trade: {e}")
