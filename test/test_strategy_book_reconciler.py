"""
Unit tests for services/strategy_book_reconciler.py.

The strategy book is fed only by live order.update events, so a fill that
happens while the app is down (or an order-update feed is disconnected) is
never booked. The reconciler replays such fills from the broker order book
(idempotently), verifies open legs against the broker position book, and
reports a data-health verdict for the P&L and Performance pages.

Run with: uv run pytest test/test_strategy_book_reconciler.py -v
"""

import uuid
from datetime import datetime, timedelta

import pytest

from database.strategy_book_db import (
    apply_fill,
    get_closed_trades,
    get_strategy_legs,
    init_strategy_book_db,
    record_order_tag,
)
from services import strategy_book_reconciler as rec


@pytest.fixture(scope="module", autouse=True)
def _init_book():
    init_strategy_book_db()


@pytest.fixture(autouse=True)
def _fresh_state():
    rec._reset_state_for_tests()
    yield
    rec._reset_state_for_tests()


def _ids():
    tag = uuid.uuid4().hex[:8].upper()
    return f"RECON-{tag}", "RC" + tag


def _tagged(strategy, symbol, mode="analyze", product="MIS", exchange="NSE"):
    oid = f"rc-{uuid.uuid4().hex}"
    assert record_order_tag(
        orderid=oid,
        user_id="",
        strategy=strategy,
        symbol=symbol,
        exchange=exchange,
        product=product,
        mode=mode,
    )
    return oid


def _order(oid, symbol, action, qty, price, ts, status="complete", product="MIS"):
    return {
        "orderid": oid,
        "symbol": symbol,
        "exchange": "NSE",
        "product": product,
        "action": action,
        "order_status": status,
        "filled_quantity": qty,
        "quantity": qty,
        "average_price": price,
        "timestamp": ts,
    }


def _leg(strategy, symbol, mode="analyze"):
    legs = [
        leg
        for leg in get_strategy_legs(strategy=strategy, mode=mode)
        if leg["symbol"] == symbol
    ]
    return legs[0] if legs else None


# --- replay -----------------------------------------------------------------


def test_replay_books_a_fill_that_was_never_delivered():
    strategy, symbol = _ids()
    oid = _tagged(strategy, symbol)
    orders = [_order(oid, symbol, "BUY", 10, 100.0, "29-Sep-2026 09:20:00")]

    replayed = rec.replay_missed_fills("analyze", orders)

    assert replayed == 1
    leg = _leg(strategy, symbol)
    assert leg["quantity"] == 10
    assert leg["average_price"] == 100.0


def test_replay_is_idempotent():
    strategy, symbol = _ids()
    oid = _tagged(strategy, symbol)
    orders = [_order(oid, symbol, "BUY", 10, 100.0, "29-Sep-2026 09:20:00")]

    rec.replay_missed_fills("analyze", orders)
    replayed_again = rec.replay_missed_fills("analyze", orders)

    assert replayed_again == 0
    assert _leg(strategy, symbol)["quantity"] == 10


def test_replay_does_not_double_book_a_fill_already_delivered_live():
    strategy, symbol = _ids()
    oid = _tagged(strategy, symbol)
    apply_fill(oid, 10, 100.0, "BUY")
    orders = [_order(oid, symbol, "BUY", 10, 100.0, "29-Sep-2026 09:20:00")]

    assert rec.replay_missed_fills("analyze", orders) == 0
    assert _leg(strategy, symbol)["quantity"] == 10


def test_replay_recovers_a_missed_exit_and_writes_the_ledger_row():
    strategy, symbol = _ids()
    entry = _tagged(strategy, symbol)
    apply_fill(entry, 10, 100.0, "BUY")
    exit_oid = _tagged(strategy, symbol)
    orders = [
        _order(entry, symbol, "BUY", 10, 100.0, "29-Sep-2026 09:20:00"),
        _order(exit_oid, symbol, "SELL", 10, 110.0, "29-Sep-2026 10:05:00"),
    ]

    assert rec.replay_missed_fills("analyze", orders) == 1

    assert _leg(strategy, symbol)["quantity"] == 0
    trades = get_closed_trades(strategy=strategy, mode="analyze")
    assert len(trades) == 1
    assert trades[0]["realized_pnl"] == 100.0


def test_replay_applies_in_time_order_not_list_order():
    """An exit applied before its entry would book a flipped short instead of
    a closed trade, so the orderbook's list order must not be trusted."""
    strategy, symbol = _ids()
    entry = _tagged(strategy, symbol)
    exit_oid = _tagged(strategy, symbol)
    orders = [
        _order(exit_oid, symbol, "SELL", 10, 110.0, "29-Sep-2026 10:05:00"),
        _order(entry, symbol, "BUY", 10, 100.0, "29-Sep-2026 09:20:00"),
    ]

    assert rec.replay_missed_fills("analyze", orders) == 2

    assert _leg(strategy, symbol)["quantity"] == 0
    assert get_closed_trades(strategy=strategy, mode="analyze")[0]["realized_pnl"] == 100.0


def test_replay_skips_orders_tagged_for_the_other_mode():
    strategy, symbol = _ids()
    oid = _tagged(strategy, symbol, mode="live")
    orders = [_order(oid, symbol, "BUY", 10, 100.0, "29-Sep-2026 09:20:00")]

    assert rec.replay_missed_fills("analyze", orders) == 0
    assert _leg(strategy, symbol, mode="live") is None


def test_replay_skips_unfilled_and_untagged_orders():
    strategy, symbol = _ids()
    oid = _tagged(strategy, symbol)
    orders = [
        _order(oid, symbol, "BUY", 0, 0.0, "29-Sep-2026 09:20:00", status="open"),
        _order("untagged-order", symbol, "BUY", 5, 50.0, "29-Sep-2026 09:21:00"),
    ]

    assert rec.replay_missed_fills("analyze", orders) == 0
    assert _leg(strategy, symbol) is None


# --- position-book verification ---------------------------------------------


def _open_leg(strategy, symbol, qty, price, action, product="MIS"):
    oid = _tagged(strategy, symbol, product=product)
    apply_fill(oid, qty, price, action)


def _pos(symbol, qty, product="MIS"):
    return {"symbol": symbol, "exchange": "NSE", "product": product, "quantity": str(qty)}


def test_open_leg_matching_broker_position_is_not_flagged():
    strategy, symbol = _ids()
    _open_leg(strategy, symbol, 10, 100.0, "BUY")

    result = rec.find_mismatches("analyze", [_pos(symbol, 10)])

    assert [m for m in result if m["symbol"] == symbol] == []


def test_open_leg_with_flat_broker_position_is_flagged():
    strategy, symbol = _ids()
    _open_leg(strategy, symbol, 45, 100.0, "SELL")

    result = [m for m in rec.find_mismatches("analyze", [_pos(symbol, 0)]) if m["symbol"] == symbol]

    assert len(result) == 1
    assert result[0]["book_quantity"] == -45
    assert result[0]["broker_quantity"] == 0
    assert strategy in result[0]["strategies"]


def test_open_leg_absent_from_broker_positions_is_flagged():
    strategy, symbol = _ids()
    _open_leg(strategy, symbol, 5, 100.0, "BUY")

    result = [m for m in rec.find_mismatches("analyze", []) if m["symbol"] == symbol]

    assert len(result) == 1
    assert result[0]["broker_quantity"] == 0


def test_cnc_leg_absent_from_positions_is_not_flagged_it_may_be_in_holdings():
    strategy, symbol = _ids()
    _open_leg(strategy, symbol, 5, 100.0, "BUY", product="CNC")

    result = [m for m in rec.find_mismatches("analyze", []) if m["symbol"] == symbol]

    assert result == []


def test_legs_of_two_strategies_are_summed_against_one_broker_row():
    symbol = "RC" + uuid.uuid4().hex[:8].upper()
    _open_leg("RECON-A-" + symbol, symbol, 10, 100.0, "BUY")
    _open_leg("RECON-B-" + symbol, symbol, 5, 100.0, "BUY")

    result = [m for m in rec.find_mismatches("analyze", [_pos(symbol, 15)]) if m["symbol"] == symbol]

    assert result == []


# --- health derivation -------------------------------------------------------

NOW = datetime(2026, 9, 29, 12, 0, 0)


def _state(**overrides):
    base = {
        "checked_at": NOW - timedelta(seconds=30),
        "error": None,
        "mismatches": [],
        "recovered_fills": 0,
    }
    base.update(overrides)
    return base


def test_health_is_unverified_before_the_first_reconcile():
    health = rec.derive_health(None, feed_connected=True, now=NOW)

    assert health["status"] == "unverified"


def test_health_ok_when_clean_and_feed_connected():
    health = rec.derive_health(_state(), feed_connected=True, now=NOW)

    assert health["status"] == "ok"
    assert health["warnings"] == []


def test_health_stale_when_book_disagrees_with_broker():
    mismatch = {
        "symbol": "ORB",
        "exchange": "NSE",
        "product": "MIS",
        "book_quantity": -45,
        "broker_quantity": 0,
        "strategies": ["ORB"],
    }
    health = rec.derive_health(_state(mismatches=[mismatch]), feed_connected=True, now=NOW)

    assert health["status"] == "stale"
    assert "ORB" in health["warnings"][0]
    assert health["mismatches"] == [mismatch]


def test_health_stale_when_last_reconcile_failed():
    health = rec.derive_health(_state(error="broker down"), feed_connected=True, now=NOW)

    assert health["status"] == "stale"
    assert "broker down" in health["warnings"][0]


def test_health_stale_when_feed_down_and_check_is_old():
    old = _state(checked_at=NOW - timedelta(seconds=rec.FEED_DOWN_MAX_AGE_SEC + 60))

    health = rec.derive_health(old, feed_connected=False, now=NOW)

    assert health["status"] == "stale"


def test_health_ok_when_feed_down_but_check_is_recent():
    health = rec.derive_health(_state(), feed_connected=False, now=NOW)

    assert health["status"] == "ok"


def test_health_reports_recovered_fills_without_marking_stale():
    health = rec.derive_health(_state(recovered_fills=3), feed_connected=True, now=NOW)

    assert health["status"] == "ok"
    assert health["recovered_fills"] == 3


# --- run + scheduling --------------------------------------------------------


def test_run_reconcile_replays_then_verifies_and_records_state(monkeypatch):
    strategy, symbol = _ids()
    oid = _tagged(strategy, symbol)
    orders = [_order(oid, symbol, "BUY", 10, 100.0, "29-Sep-2026 09:20:00")]
    monkeypatch.setattr(rec, "_fetch_books", lambda *a, **k: (orders, [_pos(symbol, 10)]))

    rec.run_reconcile("analyze", credentials={})

    health = rec.get_data_health("analyze", feed_connected=True)
    assert health["recovered_fills"] >= 1
    assert [m for m in health["mismatches"] if m["symbol"] == symbol] == []
    assert _leg(strategy, symbol)["quantity"] == 10


def test_run_reconcile_records_error_when_books_unavailable(monkeypatch):
    def boom(*a, **k):
        raise rec.BooksUnavailable("orderbook down")

    monkeypatch.setattr(rec, "_fetch_books", boom)

    rec.run_reconcile("analyze", credentials={})

    health = rec.get_data_health("analyze", feed_connected=True)
    assert health["status"] == "stale"
    assert "orderbook down" in health["warnings"][0]


def test_state_is_kept_per_mode(monkeypatch):
    monkeypatch.setattr(rec, "_fetch_books", lambda *a, **k: ([], []))
    monkeypatch.setattr(rec, "find_mismatches", lambda mode, positions: [])
    rec.run_reconcile("analyze", credentials={})

    assert rec.get_data_health("analyze", feed_connected=True)["status"] == "ok"
    assert rec.get_data_health("live", feed_connected=True)["status"] == "unverified"


def test_schedule_is_rate_limited_and_single_flight(monkeypatch):
    submitted = []
    monkeypatch.setattr(rec, "_submit", lambda fn, *args: submitted.append(args))

    assert rec.schedule_reconcile("analyze", {"api_key": "k"}) is True
    assert rec.schedule_reconcile("analyze", {"api_key": "k"}) is False
    assert len(submitted) == 1


def test_schedule_runs_again_after_the_interval(monkeypatch):
    submitted = []
    monkeypatch.setattr(rec, "_submit", lambda fn, *args: submitted.append(args))
    clock = {"t": 1000.0}
    monkeypatch.setattr(rec, "_monotonic", lambda: clock["t"])

    rec.schedule_reconcile("analyze", {})
    rec._mark_finished("analyze")
    clock["t"] += rec.MIN_INTERVAL_SEC + 1
    assert rec.schedule_reconcile("analyze", {}) is True
    assert len(submitted) == 2
