"""SQLite persistence for trade audit trail."""

import json
import os
import sqlite3
import threading
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
    # 2026-09-09: NULL means clean/unflagged - a trade whose OUTCOME is corrupted by something
    # other than the strategy's own logic (broker session dead mid-trade, an order stuck for
    # hours then filled at a stale price, a since-fixed config bug) rather than a genuine call
    # the strategy made. Set via flag_data_quality() when such an issue is discovered - see
    # DATA_QUALITY_EXECUTION_ISSUE below and fetch_clean_trades(), the read side analysis should
    # use instead of querying `trades` directly. Deliberately NOT auto-inferred from status/PnL -
    # a losing trade is not the same thing as a corrupted one, and guessing which is which from
    # the numbers alone would silently discard real losses along with real bugs.
    ("data_quality", "TEXT"),
)

#: A trade whose recorded outcome cannot be trusted because of an infrastructure/execution
#: problem (stuck order filled late, broker session outage, orphaned position) rather than the
#: strategy's own decision-making. See fetch_clean_trades().
DATA_QUALITY_EXECUTION_ISSUE = "execution_issue"

_CREATE_STRATEGY_VERSIONS = """
CREATE TABLE IF NOT EXISTS strategy_versions (
    strategy      TEXT PRIMARY KEY,
    effective_from TEXT NOT NULL,
    reason        TEXT,
    updated_at    TEXT NOT NULL
)
"""

_INSERT = """
INSERT INTO trades (
    strategy, direction, symbol, entry, sl, tp,
    quantity, order_id, status, message,
    signal_time, received_at, executed_at,
    raw_message, context, fill_price, sig_id, trade_mode
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


#: ONE CONNECTION PER THREAD, not one per call and not one shared across threads.
#:
#: Per call was the original: every function opened a connection, re-ran CREATE TABLE,
#: PRAGMA table_info and possibly ALTER TABLE, then closed — all synchronously on the
#: asyncio event loop. The BreakingTrade poller is a SEPARATE PROCESS that also writes
#: trades.db (flip_watch.py), so a write-lock collision could stall the loop for the full
#: `timeout=10`: no position poll, no SL placement, for ten seconds of market hours.
#:
#: One shared connection fixed that and introduced a worse problem. sqlite3.threadsafety is
#: 3 (SERIALIZED) so SQLite's own structures are safe, but the Python Connection object's
#: statement cache is not — two threads executing on one handle intermittently corrupt each
#: other's cursor state, which is the same failure the root CLAUDE.md records for StaticPool
#: ("bad parameter or other API misuse"). It showed up as a flaky concurrent-write test.
#:
#: Thread-local is the shape that is both cheap and correct: no per-call open, no shared
#: cursor, and concurrent access across connections is exactly what WAL exists to handle.
#: The engine's thread count is bounded (the event loop plus asyncio.to_thread workers), so
#: this is a handful of descriptors, not a leak.
_local = threading.local()

#: Every connection handed out, so reset_connection() can close the ones belonging to other
#: threads too — a test's tmp_path connection must not outlive the test that made it.
_all_connections: list = []
_CONN_LOCK = threading.RLock()


def reset_connection() -> None:
    """Close and forget every connection this process has opened. Tests, and shutdown."""
    with _CONN_LOCK:
        for conn in _all_connections:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - a close failure must not break teardown
                pass
        _all_connections.clear()
    _local.conn = None
    _local.path = None


def _get_connection() -> sqlite3.Connection:
    """This thread's trades.db connection, building the schema on first use only.

    Re-opens if _DB_PATH has changed since the connection was made — which is what the test
    suite's autouse tmp_path fixture does, and the one case where silently reusing a stale
    handle would write to the real audit trail.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "path", None) == _DB_PATH:
        return conn

    # Creation is serialised, use is not. Two threads opening at once both run CREATE TABLE
    # / PRAGMA table_info / ALTER TABLE against the same file, and the loser gets
    # "database is locked" — which save() catches and logs, so the trade row is SILENTLY
    # LOST rather than erroring. (Found by a concurrent-write test dropping 3-7 of 8 rows,
    # with an empty error list because save() swallows by design.) The lock costs nothing:
    # it is held once per thread, for the life of the process.
    with _CONN_LOCK:
        conn = getattr(_local, "conn", None)
        if conn is not None and getattr(_local, "path", None) == _DB_PATH:
            return conn

        directory = os.path.dirname(_DB_PATH)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # isolation_level=None -> autocommit. Every function here is a single statement
        # followed by commit(); autocommit makes those commits harmless no-ops and keeps one
        # thread's write out of an open transaction while another waits on the file.
        conn = sqlite3.connect(
            _DB_PATH, timeout=10, check_same_thread=False, isolation_level=None
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_CREATE_TABLE)
        conn.execute(_CREATE_STRATEGY_VERSIONS)
        _add_missing_columns(conn)
        conn.commit()
        _local.conn, _local.path = conn, _DB_PATH
        _all_connections.append(conn)
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
        now = datetime.now(IST).replace(tzinfo=None).isoformat()  # naive IST - see models.py TradeResult.timestamp
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
    except Exception as e:
        logger.error(f"Failed to save trade: {e}")


def fetch_all_open_positions() -> list:
    """Every (strategy, symbol) pair whose latest SUCCESS trade today is an unclosed entry
    (a LONG/SHORT row with no later EXIT row) - the LOCAL, trades.db-only view of "what do we
    think is open". Used by startup.reconcile_open_positions() to find positions the broker may
    have already closed while the engine was down (e.g. mid crash-loop) - a case the existing
    reconciliation (which only walks the broker's currently-NONZERO positions) cannot see,
    since a closed position simply never appears there. HINDALCO incident, 2026-09-08: a
    stopped-out position was correctly flat at the broker but stayed 'open' in trades.db and
    in the risk engine's counters indefinitely, because nothing ever looked from this direction.
    """
    try:
        today = datetime.now(IST).strftime("%Y-%m-%d")
        conn = _get_connection()
        rows = conn.execute(
            """
            SELECT strategy, symbol, direction, entry, sl, tp, quantity, order_id, executed_at
            FROM trades
            WHERE status = 'SUCCESS' AND direction IN ('LONG', 'SHORT', 'EXIT')
              AND date(executed_at) = ?
            ORDER BY id
            """,
            (today,),
        ).fetchall()
    except Exception as e:
        logger.warning(f"fetch_all_open_positions failed: {e}")
        return []

    latest = {}
    for strategy, symbol, direction, entry, sl, tp, qty, order_id, executed_at in rows:
        latest[(strategy, symbol)] = {
            "strategy": strategy,
            "symbol": symbol,
            "direction": direction,
            "entry": float(entry or 0.0),
            "sl": float(sl or 0.0),
            "tp": float(tp or 0.0),
            "quantity": int(qty or 0),
            "order_id": str(order_id or ""),
            "executed_at": str(executed_at or ""),
        }
    return [v for v in latest.values() if v["direction"] in ("LONG", "SHORT")]


#: order_id written for a position backfilled by reconciliation rather than a real order.
RECONCILED_ORDER_ID = "-RECONCILED-"


def save_reconciled_exit(
    strategy: str,
    symbol: str,
    entry: float,
    sl: float,
    tp: float,
    quantity: int,
    fill_price: float,
    pnl: float,
    note: str,
) -> None:
    """Backfill an EXIT row for a position the broker had already closed by the time the engine
    restarted - see startup.reconcile_open_positions(). The exact fill time, and whether it
    closed in one shot or several partial exits, are not knowable from a single flat-position
    snapshot - this records ONE full-quantity exit with what IS known (realized P&L from the
    broker's own position book, current LTP as a fill-price estimate) and says so plainly in
    the message. An approximate record beats a silently missing one, but must never be read as
    a precisely-timed fill. Never raises: this is bookkeeping and must not break startup.
    """
    try:
        conn = _get_connection()
        now = datetime.now(IST).replace(tzinfo=None).isoformat()  # naive IST - see models.py TradeResult.timestamp
        conn.execute(
            _INSERT,
            (
                strategy,
                "EXIT",
                symbol,
                entry,
                sl,
                tp,
                quantity,
                RECONCILED_ORDER_ID,
                "SUCCESS",
                note,
                "",
                now,
                now,
                note,
                json.dumps({"reconciled": True, "realized_pnl": pnl}),
                fill_price,
                None,
                _TRADE_MODE,
            ),
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to save reconciled exit for {symbol}: {e}")


def fetch_last_entry_trade(symbol: str, strategy: str) -> dict | None:
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


def flag_data_quality(order_id: str, quality: str, reason: str = "") -> int:
    """Mark every trades.db row for `order_id` (entry and any partial/full exits share it) as
    not trustworthy for performance analysis, without deleting or altering the row itself - the
    record of what actually happened stays intact, only a read-side filter changes.

    Called from tracker.py's orphan-release path the moment an execution problem is confirmed,
    so the flag lands within the same session the problem was found rather than requiring a
    human to remember to backfill it later. Never raises: this is bookkeeping, not something
    that should be able to break position tracking.

    Returns the number of rows flagged (0 if order_id is blank or matched nothing).
    """
    if not order_id:
        return 0
    try:
        conn = _get_connection()
        cur = conn.execute(
            "UPDATE trades SET data_quality = ? WHERE order_id = ?",
            (quality, order_id),
        )
        conn.commit()
        if cur.rowcount:
            logger.warning(
                f"data_quality={quality!r} flagged on {cur.rowcount} row(s) for order_id="
                f"{order_id!r}: {reason}"
            )
        return cur.rowcount
    except Exception as e:
        logger.error(f"flag_data_quality failed for order_id={order_id!r}: {e}")
        return 0


def fetch_clean_trades(strategy: str = None, since: str = None) -> list:
    """Trades safe to use for performance analysis - excludes anything flag_data_quality()
    marked, and (when `since` is given) anything before a strategy-logic change.

    This is the function analysis should call instead of querying `trades` directly - see
    set_strategy_version()'s docstring for why `since` matters independently of data_quality.

    Args:
        strategy: restrict to one strategy tag (case-insensitive), or None for all.
        since: ISO date/datetime string - only rows with executed_at >= this. Pass
            get_strategy_version(strategy)["effective_from"] to apply the current version
            cutoff automatically.

    Returns a list of dicts, newest first.
    """
    try:
        conn = _get_connection()
        clauses = ["data_quality IS NULL"]
        params: list = []
        if strategy:
            clauses.append("upper(strategy) = upper(?)")
            params.append(strategy)
        if since:
            clauses.append("executed_at >= ?")
            params.append(since)
        cur = conn.execute(
            f"SELECT * FROM trades WHERE {' AND '.join(clauses)} ORDER BY id DESC",
            params,
        )
        columns = [d[0] for d in cur.description]
        rows = [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]
        return rows
    except Exception as e:
        logger.error(f"fetch_clean_trades failed: {e}")
        return []


def set_strategy_version(strategy: str, effective_from: str, reason: str = "") -> None:
    """Record that trades before `effective_from` were produced by a DIFFERENT version of this
    strategy's logic and should not be pooled with trades after it.

    This is distinct from flag_data_quality(): that marks individual trades corrupted by an
    infrastructure problem; this marks a whole PERIOD as belonging to a superseded ruleset (an
    entry-trigger change, a new confirmation window, a relaxed/tightened filter - anything that
    changes what counts as a signal in the first place). Bump this in the same change as the
    corresponding STRATEGY-LOG.md entry - this is the machine-readable half of that same
    discipline, not a replacement for it.

    One row per strategy - calling this again for the same strategy overwrites the previous
    cutover, it does not keep history (STRATEGY-LOG.md is the durable history; this is only
    "what does 'current' mean right now").
    """
    try:
        conn = _get_connection()
        now = datetime.now(IST).replace(tzinfo=None).isoformat()  # naive IST - see models.py TradeResult.timestamp
        conn.execute(
            "INSERT INTO strategy_versions (strategy, effective_from, reason, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(strategy) DO UPDATE SET "
            "effective_from = excluded.effective_from, reason = excluded.reason, "
            "updated_at = excluded.updated_at",
            (strategy.upper(), effective_from, reason, now),
        )
        conn.commit()
    except Exception as e:
        logger.error(f"set_strategy_version failed for {strategy}: {e}")


def get_strategy_version(strategy: str) -> dict | None:
    """The current effective_from/reason for `strategy`, or None if never set (meaning: no
    known cutover, all history for this strategy is comparable)."""
    try:
        conn = _get_connection()
        cur = conn.execute(
            "SELECT effective_from, reason, updated_at FROM strategy_versions "
            "WHERE upper(strategy) = upper(?)",
            (strategy,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {"effective_from": row[0], "reason": row[1], "updated_at": row[2]}
    except Exception as e:
        logger.error(f"get_strategy_version failed for {strategy}: {e}")
        return None
