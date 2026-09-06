"""SQLite persistence for trade audit trail."""

import json
import os
import sqlite3
from datetime import datetime
from typing import Optional

from loguru import logger

from signal_engine.models import Direction, Order, Signal, TradeResult
from signal_engine.timeutils import IST


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
    executed_at TEXT,
    raw_message TEXT,
    context TEXT,
    fill_price REAL,
    sig_id TEXT,
    trade_mode TEXT
)
"""

# Columns added after the table shipped. SQLite has no "ADD COLUMN IF NOT EXISTS", so the
# existing set is read once per connection and only the gaps are filled — cheap, and it keeps
# a live trades.db working across an upgrade without a manual migration step.
_ADDED_COLUMNS = (
    ("raw_message", "TEXT"),
    ("context", "TEXT"),
    ("fill_price", "REAL"),
    ("sig_id", "TEXT"),
    # Deliberately NOT backfilled. The engine has an analyze mode and an off-hours testing
    # switch, so some pre-existing rows may not be live trades, and nothing in the data
    # distinguishes them. A NULL that analysis can see and exclude beats a guess it cannot.
    ("trade_mode", "TEXT"),
)

_INSERT = """
INSERT INTO trades (
    strategy, direction, symbol, entry, sl, tp,
    quantity, order_id, status, message,
    signal_time, received_at, executed_at,
    raw_message, context, fill_price, sig_id, trade_mode
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _get_connection() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_CREATE_TABLE)
    _add_missing_columns(conn)
    conn.commit()
    return conn


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Bring a pre-existing trades table up to the current column set."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
    for name, coltype in _ADDED_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {name} {coltype}")


#: Order id written for a signal that never reached the broker. Not empty string: the ledger
#: uses a falsy order_id to mean "sent but unmatched by any fill", which is a reconciliation
#: error. A decline is not an error, so it needs an id that is present and unmistakable.
DECLINED_ORDER_ID = "-DECLINED-"

#: Status for a signal the engine refused before sending anything. Distinct from REJECTED,
#: which means the BROKER refused an order that was actually placed.
DECLINED_STATUS = "DECLINED"

#: Written when a row is saved before startup has learned the mode from OpenAlgo. The engine
#: builds its objects at import, so that window is real; "unknown" is honest where "live"
#: would be a guess about real money.
UNKNOWN_TRADE_MODE = "unknown"

#: Which mode the engine is running in, set once by startup after fetch_trading_mode().
#: A module-level value rather than a lookup into risk_engine so db.py stays free of any
#: import back into main/runtime.
_TRADE_MODE = UNKNOWN_TRADE_MODE


def set_trade_mode(mode: str) -> None:
    """Record which mode subsequent rows were produced in.

    risk.db has keyed its counters on (mode, date) from the start, because mixing paper losses
    into live totals would be wrong. trades.db — the audit trail every performance report and
    the ledger actually read — had no equivalent, so a paper week would land in the same table
    as months of real trades with only the date to separate them. That breaks the moment one
    strategy is paper-tested while another runs live, or the mode changes mid-session.
    """
    global _TRADE_MODE
    _TRADE_MODE = (mode or UNKNOWN_TRADE_MODE).strip().lower() or UNKNOWN_TRADE_MODE


def save_declined(signal: Signal, stage: str, reason: str) -> None:
    """Persist a signal the engine declined before any order was sent.

    Every early return in main._handle_entry -- blacklist, T2T, exposure limits, price filter,
    capital, qty=0 -- happens before save(), so without this a decline exists only as a log
    line. For a forward test that is the wrong half of the record to lose: with tight slot and
    capital limits a real share of signals never becomes an order, and whether the DECLINED
    ones would have been the winners is exactly what the review has to be able to ask.

    Writes the signal's prices, context and sig_id, so the outcome can be scored after the
    fact against the same chart data as a taken trade. Never raises: this is bookkeeping and
    must not be able to break signal handling.
    """
    try:
        conn = _get_connection()
        now = datetime.now(IST).isoformat()
        conn.execute(
            _INSERT,
            (
                signal.strategy,
                signal.direction.value,
                signal.symbol,
                signal.entry,
                signal.sl,
                signal.tp,
                0,
                DECLINED_ORDER_ID,
                DECLINED_STATUS,
                f"[{stage}] {reason}",
                signal.time or "",
                signal.received_at.isoformat(),
                now,
                signal.raw_message,
                json.dumps(signal.context or {}),
                None,
                signal.sig_id,
                _TRADE_MODE,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to save declined signal for {signal.symbol}: {e}")


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
                signal.raw_message,
                json.dumps(signal.context or {}),
                result.fill_price,
                signal.sig_id,
                _TRADE_MODE,
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
        today = datetime.now(IST).strftime("%Y-%m-%d")
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
