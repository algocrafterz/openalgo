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


#: The table carried no indexes at all, which was survivable while it was read twice a day at
#: startup. fetch_day_trades() now backs both the EOD summary and the day-context line on
#: EVERY close notification, and the audit trail is deliberately never pruned - so the scans
#: only get longer. Both are EXPRESSION indexes: the queries filter on date(executed_at) and
#: upper(symbol), which a plain column index cannot serve.
_CREATE_INDEXES = (
    # fetch_day_trades() and fetch_all_open_positions(): mode + day, then status/direction.
    "CREATE INDEX IF NOT EXISTS idx_trades_day ON trades "
    "(trade_mode, date(executed_at), status, direction)",
    # fetch_last_entry_trade(): case-insensitive symbol+strategy on a given day.
    "CREATE INDEX IF NOT EXISTS idx_trades_symbol_lookup ON trades "
    "(upper(symbol), upper(strategy), date(executed_at))",
)


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
        for statement in _CREATE_INDEXES:
            conn.execute(statement)
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


def sig_id_exists(sig_id: str) -> bool:
    """True when any trades row (taken or DECLINED) already carries this signal id."""
    row = _get_connection().execute(
        "SELECT 1 FROM trades WHERE sig_id = ? LIMIT 1", (sig_id,)
    ).fetchone()
    return row is not None


def save_tracker_exit(*, strategy: str, symbol: str, entry: float, sl: float, tp: float,
                      quantity: int, exit_price: "float | None", pnl: float,
                      exit_types: list, order_id: str = "", sig_id: "str | None" = None,
                      includes_leg_pnl: "float | None" = None,
                      extra_context: "dict | None" = None) -> None:
    """Write the EXIT row for a close the TRACKER detected (broker SL-M fill, no-progress
    market exit, time exit).

    These closes used to leave no row at all. book_close() filed an in-memory TradeRecord,
    advanced the day counters and sent the Telegram message - but trades.db, which every
    performance report and the ledger actually read, got nothing. The next restart's
    reconciliation then found "no EXIT recorded", booked the close a SECOND time from the
    broker's own figures, and called record_close() again. On 2026-09-11 all four positions
    closed at 13:16/13:35 and were re-booked at 15:05 with different P&L.

    Never raises: the position IS closed at the broker, and a bookkeeping failure must not
    leave the tracker believing otherwise.

    sig_id: the entry's SigID (TrackedPosition.sig_id), so the ledger's sig_id-keyed
    reconciliation can pair this close back to its entry. Left None only for entries that
    never carried one (Python-sourced strategies) — see tracker.py's TrackedPosition.sig_id
    docstring for the 2026-09-17 incident this closes.

    includes_leg_pnl: how much of `pnl` is partial-leg P&L the position had already realised
    (TrackedPosition.realized_pnl at close). fetch_day_trades() uses it to decide whether a
    partial leg's own row is already inside this row or still has to be added: a close booked
    before a leg finished, or by a restarted engine that lost the position's history, includes
    less than the legs' rows hold.

    extra_context: further keys for the row's context, for closes that carry lineage the plain
    tracker close does not (the time exit's exit_class, entry and exit order ids and fill).
    """
    try:
        conn = _get_connection()
        now = datetime.now(IST).replace(tzinfo=None).isoformat()
        context = {"pnl": pnl, "exit_types": list(exit_types or [])}
        if includes_leg_pnl is not None:
            context["includes_leg_pnl"] = includes_leg_pnl
        context.update(extra_context or {})
        conn.execute(
            _INSERT,
            (
                strategy, Direction.EXIT.value, symbol, entry, sl, tp, quantity,
                order_id or "-TRACKER-", "SUCCESS",
                f"Closed by tracker ({'+'.join(exit_types) if exit_types else 'UNKNOWN'}): "
                f"P&L {pnl:+,.2f}",
                "", now, now,
                f"tracker close: {'+'.join(exit_types) if exit_types else 'UNKNOWN'}",
                json.dumps(context),
                exit_price, sig_id or None, _TRADE_MODE,
            ),
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to save tracker exit for {symbol}: {e}")


#: The classification of an entry the execution guard closed at once
#: (main._auto_close_on_bad_fill), and the two causes kept in the row's context["reason"].
#: These are real round trips with a real P&L, but they are NOT the strategy's own exits, so
#: the row's exit type is BAD_FILL_CLOSE and it carries an `execution_exit` flag. Consumers
#: that measure money count them; consumers that measure strategy quality can split them out.
EXIT_BAD_FILL_CLOSE = "BAD_FILL_CLOSE"

#: Exit types that are a trade and real money but NOT a decided win or loss: the clock (TIME) or
#: the execution guard (BAD_FILL_CLOSE) closed the position, which is no evidence about the entry
#: rule. Win rate and average R leave them out; trade counts and net P&L keep them. One set, so
#: the headline summary and the per-strategy lines cannot disagree.
NOT_DECIDED_EXIT_TYPES = frozenset({"TIME", EXIT_BAD_FILL_CLOSE})
EXIT_ENTRY_REJECTED_SLIPPAGE = "ENTRY_REJECTED_SLIPPAGE"
EXIT_ENTRY_REJECTED_TP_OVERSHOT = "ENTRY_REJECTED_TP_OVERSHOT"
EXIT_ENTRY_REJECTED_STOP_BREACHED = "ENTRY_REJECTED_STOP_BREACHED"


def save_execution_exit(
    *,
    strategy: str,
    symbol: str,
    signal_entry: float,
    sl: float,
    tp: float,
    quantity: int,
    entry_order_id: str,
    exit_order_id: str,
    entry_fill: "float | None",
    exit_fill: "float | None",
    pnl: float,
    exit_type: str,
    reason: str,
    slippage_r: float,
    slippage_rs: float,
    sig_id: "str | None" = None,
) -> None:
    """Write the EXIT row for an entry the execution guard closed immediately.

    Before this the round trip left no row at all (the entry save was skipped and the close
    order's result was dropped), so the broker's two fills reached the EOD ledger as "no
    matching engine order" and the realised loss was in no report. The context keeps the
    whole lineage: the signal price, the actual fill, why it was closed, the realised P&L,
    the slippage in R and in rupees, and both order ids. Never raises: the position IS
    closed at the broker and a bookkeeping failure must not undo that.
    """
    try:
        conn = _get_connection()
        now = datetime.now(IST).replace(tzinfo=None).isoformat()
        conn.execute(
            _INSERT,
            (
                strategy, Direction.EXIT.value, symbol, signal_entry, sl, tp, quantity,
                exit_order_id or "-EXECUTION-", "SUCCESS",
                f"Closed by execution guard ({EXIT_BAD_FILL_CLOSE}, {exit_type}): "
                f"P&L {pnl:+,.2f} - {reason}",
                "", now, now,
                f"execution guard: {EXIT_BAD_FILL_CLOSE} {exit_type}",
                json.dumps({
                    "pnl": pnl,
                    "exit_types": [EXIT_BAD_FILL_CLOSE],
                    "exit_class": EXIT_BAD_FILL_CLOSE,
                    "execution_exit": True,
                    "reason": exit_type,  # the cause; the ledger reads this as a short label
                    "detail": reason,
                    "signal_entry": signal_entry,
                    "entry_fill": entry_fill,
                    "exit_fill": exit_fill,
                    "slippage_r": slippage_r,
                    "slippage_rs": slippage_rs,
                    "entry_order_id": entry_order_id,
                    "exit_order_id": exit_order_id,
                }),
                exit_fill, sig_id or None, _TRADE_MODE,
            ),
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to save execution exit for {symbol}: {e}")


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
            SELECT strategy, symbol, direction, entry, sl, tp, quantity, order_id, executed_at, sig_id
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
    for strategy, symbol, direction, entry, sl, tp, qty, order_id, executed_at, sig_id in rows:
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
            "sig_id": str(sig_id or ""),
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
    sig_id: "str | None" = None,
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
                # Canonical keys, same as save_tracker_exit(): one shape for one fact.
                # This used to write only {"reconciled", "realized_pnl"}, so the EOD summary
                # reading `pnl` saw 0.00 for every reconciled close - two writers, two shapes,
                # and the reader silently agreed with neither.
                json.dumps({
                    "pnl": pnl, "exit_types": ["RECONCILED"],
                    "reconciled": True, "realized_pnl": pnl,
                }),
                fill_price,
                sig_id or None,
                _TRADE_MODE,
            ),
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to save reconciled exit for {symbol}: {e}")


#: The message the generic save() stores for an order result. Partial-exit rows written before
#: the explicit "partial" marker existed (2026-09-22 onward) carry a pnl but no marker; this
#: message is what tells them apart, since every final-close writer (save_tracker_exit,
#: save_reconciled_exit, the startup backfill) writes its own message.
_GENERIC_SAVE_MESSAGE = "success"


def _is_partial_leg(payload: dict, message: "str | None") -> bool:
    """True for an EXIT row that is one leg of a position, not its closing row."""
    if payload.get("partial") is True:
        return True
    return (message or "") == _GENERIC_SAVE_MESSAGE and not payload.get("reconciled")


def _is_reconciled_close(payload: dict, message: "str | None") -> bool:
    """A close booked from the broker's own figure after a restart (reconcile or backfill)."""
    return payload.get("reconciled") is True or (message or "").startswith(
        ("Backfilled", "Reconciled")
    )


def _leg_pnl_inside_final(final: dict, legs: list[dict]) -> float:
    """How much of the legs' P&L the position's closing row already contains.

    A closing row's pnl is cumulative only to the extent the position still knew its earlier
    legs when it closed. Newer rows say so exactly (includes_leg_pnl, written by book_close).
    For older rows the evidence is in the row: a close labelled with the leg's own exit type
    ("TP1") was booked after that leg; one labelled only "SL" may have been booked first (SBIN
    2026-09-30, final row id below its own leg's). A broker-reconciled close is the whole
    position by construction.
    """
    marker = final["payload"].get("includes_leg_pnl")
    if marker is not None:
        return float(marker)
    if _is_reconciled_close(final["payload"], final["message"]):
        return sum(leg["pnl"] for leg in legs)
    labels = set(final["payload"].get("exit_types") or [])
    return sum(leg["pnl"] for leg in legs if leg["label"] in labels)


def _exit_types_of(payload: dict) -> list:
    return list(payload.get("exit_types") or (["RECONCILED"] if payload.get("reconciled") else []))


def _load_day_entries(conn, trade_mode: str, day: str) -> tuple[dict, dict, dict]:
    """The day's entry rows, indexed three ways: by id, by (strategy, sig_id), by (symbol, strategy).

    One query for the lot, not a sub-query per exit row: this runs on every close notification
    and the table never shrinks.
    """
    by_id: dict[int, tuple] = {}
    ids_by_sig: dict[tuple[str, str], int] = {}
    ids_by_key: dict[tuple[str, str], list[int]] = {}
    for entry_id, sym, strat, e_price, e_sl, e_qty, e_sig in conn.execute(
        """
        SELECT id, upper(symbol), upper(strategy), entry, sl, quantity, sig_id FROM trades
        WHERE status = 'SUCCESS' AND direction IN ('LONG', 'SHORT')
          AND trade_mode = ? AND date(executed_at) = ?
        ORDER BY id
        """,
        (trade_mode, day),
    ):
        by_id[entry_id] = (e_price, e_sl, e_qty)
        if e_sig:
            ids_by_sig[(strat, e_sig)] = entry_id
        ids_by_key.setdefault((sym, strat), []).append(entry_id)
    return by_id, ids_by_sig, ids_by_key


def _resolve_entry_id(exit_id, strategy, symbol, sig_id, ids_by_sig, ids_by_key):
    """The entry an exit belongs to: by sig_id, else the nearest PRECEDING entry, else None."""
    key = (strategy.upper(), sig_id)
    if sig_id and key in ids_by_sig:
        return ids_by_sig[key]
    for candidate in reversed(ids_by_key.get((symbol.upper(), strategy.upper()), [])):
        if candidate < exit_id:
            return candidate
    return None


def _group_exits_by_position(exit_rows, ids_by_sig, ids_by_key) -> dict[tuple, dict]:
    """Group the day's EXIT rows into positions: {(strategy, symbol, entry_id): legs and finals}."""
    positions: dict[tuple, dict] = {}
    for exit_id, strategy, symbol, entry, sl, qty, fill, context, message, sig_id in exit_rows:
        try:
            payload = json.loads(context) if context else {}
        except (TypeError, ValueError):
            payload = {}
        if "pnl" not in payload and "realized_pnl" not in payload:
            continue
        types = payload.get("exit_types") or []
        # Tolerant of both historical shapes: rows written before the two writers were
        # unified carry "realized_pnl" instead of "pnl".
        row = {
            "id": exit_id, "strategy": strategy, "symbol": symbol, "entry": entry, "sl": sl,
            "qty": qty, "fill": fill, "message": message, "payload": payload,
            "pnl": float(payload.get("pnl", payload.get("realized_pnl", 0.0)) or 0.0),
            "label": types[0] if types else None,
        }
        anchor = _resolve_entry_id(exit_id, strategy, symbol, sig_id, ids_by_sig, ids_by_key)
        group = positions.setdefault(
            (strategy.upper(), symbol.upper(), anchor), {"legs": [], "finals": []}
        )
        group["legs" if _is_partial_leg(payload, message) else "finals"].append(row)
    return positions


def _exit_class(payload: dict, types: list) -> str:
    """What kind of close a row is, kept apart from what it earned.

    Money (total_pnl) always counts. The class says whether the close is evidence about the
    strategy's own entry and exit rules (STRATEGY_EXIT) or an event the clock (TIME_EXIT), the
    execution guard (BAD_FILL_CLOSE) or a restart (RECONCILED) caused.
    """
    explicit = payload.get("exit_class")
    if explicit:
        return explicit
    if payload.get("execution_exit") is True:
        return EXIT_BAD_FILL_CLOSE
    if "TIME" in types:
        return "TIME_EXIT"
    if payload.get("reconciled") is True:
        return "RECONCILED"
    return "STRATEGY_EXIT"


def pnl_by_exit_class(trades: list) -> dict:
    """Net P&L per exit class, so strategy P&L and execution P&L can be read separately."""
    out: dict[str, float] = {}
    for trade in trades:
        out[trade["exit_class"]] = out.get(trade["exit_class"], 0.0) + trade["total_pnl"]
    return out


def _trade_row(row: dict, entry_row, total: float, types: list, fill) -> dict:
    return {
        "strategy": row["strategy"],
        "symbol": row["symbol"],
        "entry": entry_row[0] if entry_row else row["entry"],
        "sl": entry_row[1] if entry_row else row["sl"],
        "quantity": (entry_row[2] if entry_row else None) or row["qty"],
        "exit_price": fill,
        "total_pnl": total,
        "exit_types": list(types),
        "execution_exit": row["payload"].get("execution_exit") is True,
        "exit_class": _exit_class(row["payload"], list(types)),
    }


def _position_trades(group: dict, entry_row) -> list[tuple[int, dict]]:
    """The closed trades of one position, each with the row id it sorts by.

    Earlier closing rows (a double-booked close) stand as they are; the last closing row also
    takes whatever leg P&L it does not already contain. No closing row at all (a runner closed
    by the time exit) makes one trade out of the legs.
    """
    legs, finals = group["legs"], group["finals"]
    trades = [
        (f["id"], _trade_row(f, entry_row, f["pnl"], _exit_types_of(f["payload"]), f["fill"]))
        for f in finals[:-1]
    ]
    if finals:
        last = finals[-1]
        uncounted = sum(leg["pnl"] for leg in legs) - _leg_pnl_inside_final(last, legs)
        total = last["pnl"] + uncounted
        trades.append(
            (last["id"], _trade_row(last, entry_row, total, _exit_types_of(last["payload"]), last["fill"]))
        )
    elif legs:
        labels = [leg["label"] for leg in legs if leg["label"]]
        total = sum(leg["pnl"] for leg in legs)
        trades.append((legs[-1]["id"], _trade_row(legs[0], entry_row, total, labels, legs[-1]["fill"])))
    return trades


def fetch_day_trades(trade_mode: str, day: str = None) -> list:
    """Every CLOSED trade for one mode and day, as the EOD summary's source of truth.

    Each position is the group of EXIT rows that share one entry row: the entry is found by
    sig_id (the lineage key, correct under re-entry), else the nearest PRECEDING entry for the
    same symbol and strategy. A position's trade is its closing row plus whatever leg P&L that
    closing row does not already contain (see _leg_pnl_inside_final), so a partial leg is
    neither counted twice (2026-10-06: engine P&L 1,164.3 over the broker fills) nor dropped
    (a runner closed by the time exit writes no closing row at all; PRESTIGE and ONGC
    2026-09-30). A position with legs and no closing row is one trade made of its legs.
    DECLINED and REJECTED rows are excluded: they are signals that never became trades.

    The day summary used to be built from PositionTracker's in-memory counters, which
    __init__ sets to zero - so a restart silently reset the day. On 2026-09-11 the engine
    restarted at 15:05 and again at 15:49, and any summary after that would have reported
    only what happened since. trades.db is what every performance report and the ledger
    already read, and since tracker-detected closes began writing EXIT rows it is finally
    complete enough to be the one place the day is read from.
    """
    day = day or datetime.now(IST).strftime("%Y-%m-%d")
    conn = _get_connection()
    exit_rows = conn.execute(
        """
        SELECT id, strategy, symbol, entry, sl, quantity, fill_price, context, message, sig_id
        FROM trades
        WHERE status = 'SUCCESS' AND direction = 'EXIT'
          AND trade_mode = ? AND date(executed_at) = ?
        ORDER BY id
        """,
        (trade_mode, day),
    ).fetchall()
    entries_by_id, ids_by_sig, ids_by_key = _load_day_entries(conn, trade_mode, day)
    positions = _group_exits_by_position(exit_rows, ids_by_sig, ids_by_key)
    ordered = [
        pair
        for (_s, _y, anchor), group in positions.items()
        for pair in _position_trades(group, entries_by_id.get(anchor))
    ]
    return [trade for _order, trade in sorted(ordered, key=lambda pair: pair[0])]


def fetch_order_ids_today(symbol: str) -> set[str]:
    """Every broker order id this engine recorded today for `symbol` (entries and exits).

    Lets entry-timeout recovery tell a fill it has already booked (an earlier entry that
    closed and left the tracker) from the new one it is looking for. Empty on any error.
    """
    try:
        today = datetime.now(IST).strftime("%Y-%m-%d")
        conn = _get_connection()
        cur = conn.execute(
            """
            SELECT order_id FROM trades
            WHERE upper(symbol) = upper(?)
              AND date(executed_at) = ?
              AND order_id IS NOT NULL AND order_id != ''
            """,
            (symbol, today),
        )
        return {str(r[0]) for r in cur.fetchall()}
    except Exception:
        logger.exception(f"fetch_order_ids_today failed for {symbol}")
        return set()


def fetch_entry_fill_directions(strategy: str, symbol: str, day: str | None = None) -> list[int]:
    """+1 (LONG) or -1 (SHORT) for every FILLED entry of one strategy and symbol on a day.

    Source of the trade-policy counters (strategy_core.trade_policy): the daily per-direction cap
    counts entries that actually filled, in the ledger of the current trade mode. A declined,
    rejected or unfilled signal has no fill price and is not counted. A bad-fill entry that was
    closed at once has a filled entry row and counts as used. Raises on a database error: the
    caller decides what an unreadable ledger means for a new entry.
    """
    day = day or datetime.now(IST).strftime("%Y-%m-%d")
    conn = _get_connection()
    rows = conn.execute(
        """
        SELECT direction FROM trades
        WHERE upper(symbol) = upper(?) AND upper(strategy) = upper(?)
          AND status = 'SUCCESS' AND direction IN ('LONG', 'SHORT')
          AND fill_price IS NOT NULL AND fill_price > 0
          AND trade_mode = ? AND date(executed_at) = ?
        ORDER BY id
        """,
        (symbol, strategy, _TRADE_MODE, day),
    ).fetchall()
    return [1 if str(r[0]).upper() == "LONG" else -1 for r in rows]


def fetch_strategy_closes(strategy: str, trade_mode: str, day: str) -> dict[str, dict]:
    """sig_id -> {pnl, exit_fill, exit_types, quantity, closed_at} for one strategy's closing EXIT rows.

    Read-only, for per-trade research reports that join on the lineage key. Partial-leg rows are
    skipped: a closing row's pnl already contains its legs (see fetch_day_trades). Rows without a
    sig_id cannot be joined and are skipped.
    """
    conn = _get_connection()
    rows = conn.execute(
        """
        SELECT sig_id, fill_price, context, quantity, executed_at FROM trades
        WHERE upper(strategy) = upper(?) AND direction = 'EXIT' AND status = 'SUCCESS'
          AND trade_mode = ? AND date(executed_at) = ? AND sig_id IS NOT NULL AND sig_id != ''
        ORDER BY id
        """,
        (strategy, trade_mode, day),
    ).fetchall()
    out: dict[str, dict] = {}
    for sig_id, fill, context, quantity, executed_at in rows:
        try:
            payload = json.loads(context) if context else {}
        except (TypeError, ValueError):
            payload = {}
        if payload.get("partial"):
            continue
        out[sig_id] = {
            "pnl": float(payload.get("pnl", payload.get("realized_pnl", 0.0)) or 0.0),
            "exit_fill": fill,
            "exit_types": list(payload.get("exit_types") or []),
            "quantity": quantity,
            "closed_at": executed_at,
        }
    return out


def fetch_last_entry_trade(symbol: str, strategy: str) -> dict | None:
    """Look up the most recent SUCCESS entry trade for symbol+strategy on today (IST).

    Used by the engine-restart recovery path to recover entry/SL/TP context
    when the in-memory tracker has been wiped. Only returns LONG/SHORT entries
    (never EXIT rows). Returns None if no matching trade is found.

    Returns a dict with: entry, sl, tp, quantity, order_id, direction, executed_at, sig_id.
    """
    try:
        today = datetime.now(IST).strftime("%Y-%m-%d")
        conn = _get_connection()
        cur = conn.execute(
            """
            SELECT entry, sl, tp, quantity, order_id, direction, executed_at, sig_id
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
        entry, sl, tp, qty, order_id, direction, executed_at, sig_id = row
        return {
            "entry": float(entry or 0.0),
            "sl": float(sl or 0.0),
            "tp": float(tp or 0.0),
            "quantity": int(qty or 0),
            "order_id": str(order_id or ""),
            "direction": Direction.LONG if str(direction).upper() == "LONG" else Direction.SHORT,
            "executed_at": str(executed_at or ""),
            "sig_id": str(sig_id or ""),
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
