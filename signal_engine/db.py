"""SQLite persistence for trade audit trail."""

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger

from signal_engine.models import Direction, Order, Signal, TradeResult

_IST = timezone(timedelta(hours=5, minutes=30))

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
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
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


def fetch_last_entry_trade(symbol: str, strategy: str) -> Optional[dict]:
    """Look up the most recent SUCCESS entry trade for symbol+strategy on today (IST).

    Used by the engine-restart recovery path to recover entry/SL/TP context
    when the in-memory tracker has been wiped. Only returns LONG/SHORT entries
    (never EXIT rows). Returns None if no matching trade is found.

    Returns a dict with: entry, sl, tp, quantity, order_id, direction, executed_at.
    """
    try:
        today = datetime.now(_IST).strftime("%Y-%m-%d")
        conn = _get_connection()
        cur = conn.execute(
            """
            SELECT entry, sl, tp, quantity, order_id, direction, executed_at
            FROM trades
            WHERE upper(symbol) = upper(?)
              AND upper(strategy) = upper(?)
              AND status = 'SUCCESS'
              AND direction IN ('LONG', 'SHORT')
              AND date(executed_at) = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (symbol, strategy, today),
        )
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        entry, sl, tp, qty, order_id, direction, executed_at = row
        return {
            "entry": float(entry or 0.0),
            "sl": float(sl or 0.0),
            "tp": float(tp or 0.0),
            "quantity": int(qty or 0),
            "order_id": str(order_id or ""),
            "direction": Direction.LONG if str(direction).upper() == "LONG" else Direction.SHORT,
            "executed_at": str(executed_at or ""),
        }
    except Exception as e:
        logger.warning(f"fetch_last_entry_trade failed for {symbol}:{strategy}: {e}")
        return None
