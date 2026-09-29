"""
Keeps the per-strategy book honest after a crash, restart or feed outage.

`database/strategy_book_db.py` is fed only by live `order.update` events. A fill
that happens while the app is down, or while an order-update feed is
disconnected, is never delivered again - the websocket adapters do not replay on
reconnect and the polling adapter seeds its baseline silently - so the book
would keep an open leg that is really flat, or miss a closed trade entirely.

This module closes that gap without inventing data:

* `replay_missed_fills` re-applies fills from the broker order book. Booking is
  idempotent (per-order applied-quantity watermark), so replaying an already
  booked fill is a no-op and only the genuinely missing delta is booked.
* `find_mismatches` compares open legs with the broker position book. A leg
  that cannot be explained by a real fill is reported, never force-closed at a
  guessed price.
* `get_data_health` turns the last check into a verdict the P&L and
  Performance pages show as a warning when their figures may be stale.

Verification runs off the request path on one shared worker thread, rate
limited per mode, and is triggered at boot and whenever a page reads.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

import pytz

from database.strategy_book_db import apply_fill, get_order_tag, get_strategy_legs
from utils.env_config import env_int
from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# Minimum gap between two reconciles of one mode. Pages poll about once a
# minute, so this keeps broker order/position-book calls to one pair per minute.
MIN_INTERVAL_SEC = env_int("STRATEGY_BOOK_RECONCILE_MIN_INTERVAL_SEC", 60, minimum=10)

# With no live order feed, fills are only picked up when a reconcile runs, so a
# check older than this can no longer vouch for the figures.
FEED_DOWN_MAX_AGE_SEC = env_int("STRATEGY_BOOK_FEED_DOWN_MAX_AGE_SEC", 600, minimum=60)

_FILLED_STATUSES = {"complete", "completed", "filled", "partially filled", "partial"}
_TIMESTAMP_FORMATS = ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")
_QTY_EPSILON = 1e-6
_MAX_SYMBOLS_IN_WARNING = 5


class BooksUnavailable(RuntimeError):
    """The broker order book or position book could not be read."""


_lock = threading.Lock()
_state: dict[str, dict[str, Any]] = {}
_inflight: set[str] = set()
_last_started: dict[str, float] = {}
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="strategy-book-reconcile")


def _monotonic() -> float:
    return time.monotonic()


def _now() -> datetime:
    return datetime.now(IST)


def _reset_state_for_tests() -> None:
    with _lock:
        _state.clear()
        _inflight.clear()
        _last_started.clear()


def _to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _parse_timestamp(value: Any) -> datetime:
    text = str(value or "").strip()
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return datetime.min


def replay_missed_fills(mode: str, orders: list[dict]) -> int:
    """Book fills the event feed never delivered. Returns how many orders
    contributed a new quantity.

    Applied oldest first: an exit applied before its entry would book a flipped
    position instead of a closed trade, and the order book's list order is not
    guaranteed chronological.
    """
    filled_orders = [
        o
        for o in orders
        if isinstance(o, dict)
        and str(o.get("order_status", "")).strip().lower().replace("_", " ") in _FILLED_STATUSES
        and _to_float(o.get("filled_quantity")) > 0
        and o.get("orderid")
    ]
    filled_orders.sort(key=lambda o: _parse_timestamp(o.get("timestamp")))

    replayed = 0
    for order in filled_orders:
        tag = get_order_tag(str(order["orderid"]))
        if tag is None or (tag.mode or "unknown") != mode:
            continue
        filled = _to_float(order.get("filled_quantity"))
        if filled - float(tag.applied_quantity or 0) <= 0:
            continue
        result = apply_fill(
            orderid=str(order["orderid"]),
            filled_quantity=filled,
            average_price=_to_float(order.get("average_price")),
            action=str(order.get("action", "")).upper(),
        )
        if result:
            replayed += 1
            logger.info(
                f"Strategy book reconcile: recovered missed fill for order "
                f"{order['orderid']} ({result['strategy']} {result['symbol']} "
                f"booked {result['booked_quantity']})"
            )
    return replayed


def find_mismatches(mode: str, positions: list[dict]) -> list[dict]:
    """Open legs whose net quantity disagrees with the broker position book.

    Legs sharing one (symbol, exchange, product) are summed, because the broker
    nets them into one row. A CNC leg missing from positions is not flagged: T+1
    settlement moves it into holdings, so its absence proves nothing.
    """
    broker_qty: dict[tuple, float] = {}
    for p in positions:
        if not isinstance(p, dict):
            continue
        key = (p.get("symbol"), p.get("exchange"), p.get("product"))
        broker_qty[key] = broker_qty.get(key, 0.0) + _to_float(p.get("quantity"))

    book: dict[tuple, dict[str, Any]] = {}
    for leg in get_strategy_legs(mode=mode):
        qty = _to_float(leg.get("quantity"))
        if abs(qty) <= _QTY_EPSILON:
            continue
        key = (leg.get("symbol"), leg.get("exchange"), leg.get("product"))
        entry = book.setdefault(key, {"quantity": 0.0, "strategies": set()})
        entry["quantity"] += qty
        entry["strategies"].add(leg.get("strategy"))

    mismatches = []
    for key, entry in book.items():
        symbol, exchange, product = key
        if key not in broker_qty and product == "CNC":
            continue
        broker = broker_qty.get(key, 0.0)
        if abs(entry["quantity"] - broker) > _QTY_EPSILON:
            mismatches.append(
                {
                    "symbol": symbol,
                    "exchange": exchange,
                    "product": product,
                    "book_quantity": entry["quantity"],
                    "broker_quantity": broker,
                    "strategies": sorted(s for s in entry["strategies"] if s),
                }
            )
    return sorted(mismatches, key=lambda m: (m["symbol"] or "", m["product"] or ""))


def derive_health(state: dict | None, feed_connected: bool, now: datetime) -> dict:
    """The verdict a page shows: ok, stale (warn) or unverified (not yet checked)."""
    if state is None:
        return {
            "status": "unverified",
            "warnings": [
                "Figures have not yet been verified against the broker since the app started."
            ],
            "mismatches": [],
            "recovered_fills": 0,
            "last_reconciled_at": None,
            "feed_connected": feed_connected,
        }

    warnings: list[str] = []
    if state["error"]:
        warnings.append(
            f"Could not verify figures against the broker: {state['error']}. "
            "P&L and performance may be out of date."
        )
    mismatches = state["mismatches"]
    if mismatches:
        shown = ", ".join(
            f"{m['symbol']} (book {m['book_quantity']:g}, broker {m['broker_quantity']:g})"
            for m in mismatches[:_MAX_SYMBOLS_IN_WARNING]
        )
        extra = len(mismatches) - _MAX_SYMBOLS_IN_WARNING
        if extra > 0:
            shown += f" and {extra} more"
        warnings.append(
            f"{len(mismatches)} open position(s) in the strategy book do not match the broker: "
            f"{shown}. Fills may have been missed (for example while the app was down), so "
            "open quantity, P&L and performance for these may be wrong."
        )
    age = (now - state["checked_at"]).total_seconds()
    if not feed_connected and age > FEED_DOWN_MAX_AGE_SEC:
        warnings.append(
            "Live order updates are not connected and the last broker check is over "
            f"{FEED_DOWN_MAX_AGE_SEC // 60} minutes old, so recent fills may be missing."
        )

    return {
        "status": "stale" if warnings else "ok",
        "warnings": warnings,
        "mismatches": mismatches,
        "recovered_fills": state["recovered_fills"],
        "last_reconciled_at": state["checked_at"].isoformat(),
        "feed_connected": feed_connected,
    }


def _fetch_books(mode: str, credentials: dict) -> tuple[list[dict], list[dict]]:
    from services.orderbook_service import get_orderbook
    from services.positionbook_service import get_positionbook

    if mode == "analyze":
        api_key = credentials.get("api_key")
        if not api_key:
            raise BooksUnavailable("API key required for analyze mode")
        ok_o, orders_resp, _ = get_orderbook(api_key=api_key)
        ok_p, positions_resp, _ = get_positionbook(api_key=api_key)
    else:
        auth_token, broker = credentials.get("auth_token"), credentials.get("broker")
        if not auth_token or not broker:
            raise BooksUnavailable("no active broker session")
        ok_o, orders_resp, _ = get_orderbook(auth_token=auth_token, broker=broker)
        ok_p, positions_resp, _ = get_positionbook(auth_token=auth_token, broker=broker)

    if not ok_o:
        raise BooksUnavailable(f"order book unavailable: {orders_resp.get('message', 'error')}")
    if not ok_p:
        raise BooksUnavailable(
            f"position book unavailable: {positions_resp.get('message', 'error')}"
        )

    orders = (orders_resp.get("data") or {}).get("orders") or []
    positions = positions_resp.get("data") or []
    return (
        orders if isinstance(orders, list) else [],
        positions if isinstance(positions, list) else [],
    )


def _record(mode: str, **fields: Any) -> None:
    with _lock:
        previous = _state.get(mode) or {}
        _state[mode] = {
            "checked_at": _now(),
            "error": None,
            "mismatches": [],
            "recovered_fills": previous.get("recovered_fills", 0),
            **fields,
        }


def run_reconcile(mode: str, credentials: dict) -> None:
    """Replay missed fills, verify open legs, and record the outcome. Never raises."""
    try:
        orders, positions = _fetch_books(mode, credentials)
    except BooksUnavailable as exc:
        logger.warning(f"Strategy book reconcile ({mode}) skipped: {exc}")
        _record(mode, error=str(exc))
        return
    except Exception:
        logger.exception(f"Strategy book reconcile ({mode}) could not read broker books")
        _record(mode, error="unexpected error reading broker books")
        return

    try:
        replayed = replay_missed_fills(mode, orders)
        mismatches = find_mismatches(mode, positions)
    except Exception:
        logger.exception(f"Strategy book reconcile ({mode}) failed")
        _record(mode, error="reconciliation failed")
        return

    with _lock:
        recovered = (_state.get(mode) or {}).get("recovered_fills", 0) + replayed
    _record(mode, mismatches=mismatches, recovered_fills=recovered)
    if mismatches:
        logger.warning(
            f"Strategy book reconcile ({mode}): {len(mismatches)} open leg(s) disagree "
            f"with the broker: {mismatches[:5]}"
        )


def _feed_connected(mode: str) -> bool:
    if mode == "analyze":
        return True  # sandbox fills are published in-process by the execution engine
    try:
        from services.order_update_service import get_order_update_status

        return any(a.get("connected") for a in get_order_update_status().values())
    except Exception:
        logger.exception("Could not read order-update feed status")
        return False


def get_data_health(mode: str, feed_connected: bool | None = None) -> dict:
    """Current data-health verdict for `mode`, from the last reconcile."""
    if feed_connected is None:
        feed_connected = _feed_connected(mode)
    with _lock:
        state = _state.get(mode)
    return derive_health(state, feed_connected, _now())


def _claim(mode: str) -> bool:
    now = _monotonic()
    with _lock:
        if mode in _inflight:
            return False
        last = _last_started.get(mode)
        if last is not None and now - last < MIN_INTERVAL_SEC:
            return False
        _inflight.add(mode)
        _last_started[mode] = now
        return True


def _mark_finished(mode: str) -> None:
    with _lock:
        _inflight.discard(mode)


def _worker(mode: str, credentials: dict) -> None:
    from utils.db_sessions import remove_all_scoped_sessions

    try:
        run_reconcile(mode, credentials)
    finally:
        _mark_finished(mode)
        remove_all_scoped_sessions()


def _submit(fn, *args) -> None:
    _executor.submit(fn, *args)


def schedule_reconcile(mode: str, credentials: dict) -> bool:
    """Queue a background reconcile unless one ran recently or is in flight."""
    if not _claim(mode):
        return False
    _submit(_worker, mode, credentials)
    return True


def schedule_boot_reconcile() -> None:
    """Verify the book once at startup, for whoever is logged in this session."""
    _submit(_boot_task)


def _boot_task() -> None:
    from database.auth_db import Auth, get_api_key_for_tradingview, get_auth_token
    from database.settings_db import get_analyze_mode
    from utils.db_sessions import remove_all_scoped_sessions
    from utils.session import has_login_this_trading_session

    try:
        mode = "analyze" if get_analyze_mode() else "live"
        for auth_obj in Auth.query.filter_by(is_revoked=False).all():
            if not (auth_obj.name and auth_obj.broker):
                continue
            if not has_login_this_trading_session(auth_obj.name):
                continue
            credentials = {
                "auth_token": get_auth_token(auth_obj.name),
                "broker": auth_obj.broker,
                "api_key": get_api_key_for_tradingview(auth_obj.name),
            }
            if _claim(mode):
                try:
                    run_reconcile(mode, credentials)
                finally:
                    _mark_finished(mode)
            return
    except Exception:
        logger.exception("Strategy book boot reconcile failed")
    finally:
        remove_all_scoped_sessions()
