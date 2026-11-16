"""BUG-019: duplicate-order guard and reduce-only MARKET pricing in the sandbox order manager.

2026-11-04 AXISBANK SELL 154 went out three times. The first copy held the position lock
through a 168 s quote lookup, the copies behind it queued on the lock, then all filled.
"""

from __future__ import annotations

import threading
import time
from decimal import Decimal

import pytest
from test_gthread_sandbox_support import (
    prepare_databases,
    quote,
    release_sessions,
    reset_user,
    run_in_thread,
    run_in_threads,
)

USER = "bug019_inflight"
STRATEGY = "EVWAP-EARLY-PY"


@pytest.fixture(autouse=True)
def fresh_account(monkeypatch):
    prepare_databases()
    reset_user(USER)
    release_sessions()
    from sandbox import order_manager as om
    from sandbox.order_manager import OrderManager

    getattr(om, "_inflight", {}).clear()
    yield
    getattr(om, "_inflight", {}).clear()
    release_sessions()


def _order(action="SELL", quantity=154, symbol="RELIANCE", strategy=STRATEGY, price_type="MARKET"):
    return {
        "symbol": symbol,
        "exchange": "NSE",
        "action": action,
        "quantity": quantity,
        "price_type": price_type,
        "product": "CNC",
        "strategy": strategy,
    }


def _place(order):
    from sandbox.order_manager import OrderManager

    return OrderManager(USER).place_order(order)


def _open_long(monkeypatch, quantity=200):
    """Give the user a long position through the normal REST-quote entry path."""
    from sandbox.execution_engine import ExecutionEngine

    monkeypatch.setattr(ExecutionEngine, "_fetch_quote", lambda self, s, e: quote(2500))
    ok, response, _ = run_in_thread(lambda: _place(_order("BUY", quantity, strategy="")))
    assert ok, response


def _position_qty(symbol="RELIANCE"):
    from database.sandbox_db import SandboxPositions, db_session

    db_session.remove()
    row = SandboxPositions.query.filter_by(user_id=USER, symbol=symbol, product="CNC").first()
    qty = row.quantity if row else 0
    db_session.remove()
    return qty


def _orders(symbol="RELIANCE", action="SELL"):
    from database.sandbox_db import SandboxOrders, db_session

    db_session.remove()
    rows = SandboxOrders.query.filter_by(user_id=USER, symbol=symbol, action=action).all()
    out = [(r.order_status, r.average_price) for r in rows]
    db_session.remove()
    return out


class SlowQuote:
    """A _fetch_quote that blocks until released, like the 168 s lookup."""

    def __init__(self, monkeypatch, ltp=2500):
        from sandbox.execution_engine import ExecutionEngine

        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        outer = self

        def fetch(self_, symbol, exchange):
            outer.calls += 1
            outer.entered.set()
            outer.release.wait(20)
            return quote(ltp)

        monkeypatch.setattr(ExecutionEngine, "_fetch_quote", fetch)


# -- in-flight guard -------------------------------------------------------------------


def test_identical_order_while_one_is_in_flight_is_rejected_at_once_and_not_queued(monkeypatch):
    slow = SlowQuote(monkeypatch)
    first = {}
    t = threading.Thread(
        target=lambda: first.setdefault("r", _place(_order("BUY", 154))), daemon=True
    )
    t.start()
    assert slow.entered.wait(10)
    started = time.monotonic()
    ok, response, status = run_in_thread(lambda: _place(_order("BUY", 154)), timeout=10)
    elapsed = time.monotonic() - started
    slow.release.set()
    t.join(30)
    release_sessions()

    assert not ok and status == 409
    assert response["message"].startswith("Duplicate order in flight")
    assert response["inflight"] is True
    assert elapsed < 2.0  # refused, not parked behind the position lock
    assert first["r"][0] is True
    assert slow.calls == 1  # the duplicate never reached the quote lookup
    assert len(_orders(action="BUY")) == 1


def test_rejection_names_the_in_flight_order_once_its_id_is_known(monkeypatch):
    from sandbox import order_manager as om
    from sandbox.order_manager import OrderManager

    key = om._inflight_key(OrderManager(USER), _order("SELL", 154))
    claim, _ = om._inflight_claim(key)
    claim.orderid = "2611040001"
    ok, response, status = _place(_order("SELL", 154))
    assert not ok and status == 409
    assert response["inflight_orderid"] == "2611040001"
    assert "2611040001" in response["message"]


@pytest.mark.parametrize(
    "other",
    [
        {"quantity": 153},
        {"action": "BUY"},
        {"symbol": "ZEEL"},
        {"strategy": "OTHER-PY"},
        {"price_type": "LIMIT"},
    ],
)
def test_a_legitimately_different_order_is_never_blocked_by_the_guard(other):
    from sandbox import order_manager as om
    from sandbox.order_manager import OrderManager

    claim, _ = om._inflight_claim(om._inflight_key(OrderManager(USER), _order("SELL", 154)))
    assert claim is not None
    key = om._inflight_key(OrderManager(USER), {**_order("SELL", 154), **other, "price": 100})
    mine, holder = om._inflight_claim(key)
    assert mine is not None and holder is None


def test_two_identical_orders_without_a_strategy_are_not_guarded():
    from sandbox import order_manager as om
    from sandbox.order_manager import OrderManager

    assert om._inflight_key(OrderManager(USER), _order(strategy="")) is None
    assert om._inflight_key(OrderManager(USER), {**_order(), "gtt_leg_id": "L1"}) is None


def test_different_quantity_submitted_concurrently_both_proceed(monkeypatch):
    slow = SlowQuote(monkeypatch)
    results = {}

    def go(name, qty):
        results[name] = _place(_order("BUY", qty))
        release_sessions()

    a = threading.Thread(target=go, args=("a", 5), daemon=True)
    b = threading.Thread(target=go, args=("b", 6), daemon=True)
    a.start()
    assert slow.entered.wait(10)
    b.start()
    time.sleep(0.3)
    assert "b" not in results  # waiting its turn on the position lock, not rejected
    slow.release.set()
    a.join(30)
    b.join(30)
    assert results["a"][0] is True and results["b"][0] is True, results


def test_the_marker_is_dropped_when_placement_raises(monkeypatch):
    from sandbox import order_manager as om
    from sandbox.order_manager import OrderManager

    def boom(self, *a, **k):
        raise RuntimeError("quote service exploded")

    monkeypatch.setattr(OrderManager, "_place_order_impl", boom)
    with pytest.raises(RuntimeError):
        _place(_order("BUY", 154))
    assert om._inflight == {}
    monkeypatch.undo()


def test_the_marker_is_dropped_after_a_normal_and_a_rejected_placement(monkeypatch):
    from sandbox import order_manager as om
    from sandbox.execution_engine import ExecutionEngine
    from sandbox.order_manager import OrderManager

    monkeypatch.setattr(ExecutionEngine, "_fetch_quote", lambda self, s, e: quote(2500))
    monkeypatch.setattr("sandbox.order_manager.time.sleep", lambda *_: None)
    assert _place(_order("BUY", 10))[0] is True
    assert om._inflight == {}
    assert _place(_order("BUY", 10))[0] is True  # identical but sequential: allowed
    ok, _, _ = _place({**_order("BUY", 10), "symbol": "NOSUCH"})
    assert not ok and om._inflight == {}


def test_a_stuck_marker_expires_after_the_bound(monkeypatch):
    from sandbox import order_manager as om
    from sandbox.order_manager import OrderManager

    key = om._inflight_key(OrderManager(USER), _order("SELL", 154))
    stuck, _ = om._inflight_claim(key)
    assert om._inflight_claim(key)[0] is None
    stuck.started -= om.INFLIGHT_MAX_AGE_SECONDS + 1
    fresh, holder = om._inflight_claim(key)
    assert fresh is not None and holder is None
    om._inflight_release(stuck)  # the late finally of the stuck call must not drop the new one
    assert om._inflight[key] is fresh


# -- reduce-only MARKET pricing ----------------------------------------------------------


def _forbid_rest(monkeypatch):
    from sandbox.execution_engine import ExecutionEngine

    calls = []

    def fetch(self, symbol, exchange):
        calls.append(symbol)
        raise AssertionError("a reduce-only MARKET order must not look up a live quote")

    monkeypatch.setattr(ExecutionEngine, "_fetch_quote", fetch)
    return calls


def _feed(monkeypatch, quote_or_none):
    seen = []

    def fake(symbol, exchange, max_age):
        seen.append(max_age)
        return quote_or_none

    monkeypatch.setattr("sandbox.feed_price.feed_quote", fake)
    return seen


def test_reduce_only_exit_fills_from_a_fresh_feed_price_without_a_rest_quote(monkeypatch):
    _open_long(monkeypatch, 200)
    rest = _forbid_rest(monkeypatch)
    seen = _feed(monkeypatch, {"ltp": 2490.0, "bid": 0, "ask": 0, "source": "feed", "age": 1.0})
    ok, response, _ = run_in_thread(lambda: _place(_order("SELL", 154)))
    assert ok, response
    assert rest == [] and seen == [10.0]
    assert _orders() == [("complete", Decimal("2490.00"))]
    assert _position_qty() == 46


def _set_position_ltp(value):
    from database.sandbox_db import SandboxPositions, db_session

    db_session.remove()
    row = SandboxPositions.query.filter_by(user_id=USER, symbol="RELIANCE", product="CNC").first()
    row.ltp = value
    db_session.commit()
    db_session.remove()


def _fresh_feed(monkeypatch):
    return _feed(monkeypatch, {"ltp": 2490.0, "bid": 0, "ask": 0, "source": "feed", "age": 1.0})


@pytest.mark.parametrize("why", ["feed missing", "feed stale"])
def test_reduce_only_exit_without_a_fresh_feed_tick_is_accepted_open_at_last_price_with_no_rest(
    monkeypatch, why, caplog
):
    """feed_quote returns None both for a symbol not on the feed and for a stale tick."""
    import logging

    _open_long(monkeypatch, 200)
    rest = _forbid_rest(monkeypatch)
    _feed(monkeypatch, None)
    with caplog.at_level(logging.INFO):
        ok, response, status = run_in_thread(lambda: _place(_order("SELL", 154)))
    assert ok, response
    assert rest == []
    assert _orders() == [("open", Decimal("0.00"))] or _orders()[0][0] == "open"
    assert _position_qty() == 200  # not filled yet: the execution engine fills it on a tick
    assert "ladder=fallback-open" in caplog.text


def test_reduce_only_exit_with_a_fresh_feed_tick_fills_at_last_traded_price(monkeypatch, caplog):
    import logging

    _open_long(monkeypatch, 200)
    rest = _forbid_rest(monkeypatch)
    _fresh_feed(monkeypatch)
    with caplog.at_level(logging.INFO):
        ok, response, _ = run_in_thread(lambda: _place(_order("SELL", 154)))
    assert ok, response
    assert rest == []
    assert _orders() == [("complete", Decimal("2490.00"))]
    assert "price_source=feed" in caplog.text


def test_reduce_only_exit_with_no_price_at_all_is_refused_400(monkeypatch, caplog):
    import logging

    _open_long(monkeypatch, 200)
    rest = _forbid_rest(monkeypatch)
    _feed(monkeypatch, None)
    _set_position_ltp(Decimal("0"))
    with caplog.at_level(logging.INFO):
        ok, response, status = run_in_thread(lambda: _place(_order("SELL", 154)))
    assert not ok and status == 400
    assert "no live-feed price and no last known price" in response["message"]
    assert rest == [] and _orders() == [] and _position_qty() == 200
    assert "ladder=refused" in caplog.text


def test_an_order_larger_than_the_position_is_not_reduce_only_and_keeps_the_rest_path(monkeypatch):
    from sandbox.execution_engine import ExecutionEngine

    _open_long(monkeypatch, 100)
    calls = []

    def fetch(self, symbol, exchange):
        calls.append(symbol)
        return quote(2500)

    monkeypatch.setattr(ExecutionEngine, "_fetch_quote", fetch)
    seen = _feed(monkeypatch, None)
    ok, response, _ = run_in_thread(lambda: _place(_order("SELL", 154, strategy="")))
    assert calls == ["RELIANCE"] and seen == []
    # CNC cannot oversell holdings, so it is refused by the CNC check, after the REST price.
    assert not ok


def test_an_entry_is_unchanged_and_still_uses_the_rest_quote(monkeypatch):
    from sandbox.execution_engine import ExecutionEngine

    calls = []
    monkeypatch.setattr(
        ExecutionEngine, "_fetch_quote", lambda self, s, e: calls.append(s) or quote(2500)
    )
    seen = _feed(monkeypatch, None)
    ok, response, _ = run_in_thread(lambda: _place(_order("BUY", 154, strategy="")))
    assert ok, response
    assert calls == ["RELIANCE"] and seen == []


def test_two_identical_reduce_only_exits_exactly_one_fills_and_the_other_is_rejected_fast(
    monkeypatch,
):
    """The AXISBANK SELL 154 incident, with the slow step now inside the guard window."""
    _open_long(monkeypatch, 200)
    _forbid_rest(monkeypatch)
    entered = threading.Event()

    def slow_feed(symbol, exchange, max_age):
        entered.set()
        time.sleep(0.8)  # the slow step, holding the position lock
        return {"ltp": 2490.0, "bid": 0, "ask": 0, "source": "feed", "age": 0.5}

    monkeypatch.setattr("sandbox.feed_price.feed_quote", slow_feed)
    done = {}

    def go(name):
        started = time.monotonic()
        done[name] = (_place(_order("SELL", 154)), time.monotonic() - started)
        release_sessions()

    first = threading.Thread(target=go, args=("first",), daemon=True)
    first.start()
    assert entered.wait(10)
    second = threading.Thread(target=go, args=("second",), daemon=True)
    second.start()
    first.join(30)
    second.join(30)

    (ok1, _, _), _ = done["first"]
    (ok2, resp2, status2), took2 = done["second"]
    assert ok1 is True
    assert ok2 is False and status2 == 409 and resp2["message"].startswith("Duplicate order")
    assert took2 < 0.5
    assert _orders() == [("complete", Decimal("2490.00"))]
    assert _position_qty() == 46  # one exit, not three: 200 - 154


def test_lock_ordering_unchanged_concurrent_different_symbols_do_not_wait_on_each_other(
    monkeypatch,
):
    slow = SlowQuote(monkeypatch)
    out = {}

    def go(name, symbol):
        out[name] = _place(_order("BUY", 5, symbol=symbol))
        release_sessions()

    a = threading.Thread(target=go, args=("a", "RELIANCE"), daemon=True)
    b = threading.Thread(target=go, args=("b", "ZEEL"), daemon=True)
    a.start()
    b.start()
    assert slow.entered.wait(10)
    time.sleep(0.2)
    slow.release.set()
    a.join(30)
    b.join(30)
    assert out["a"][0] is True and out["b"][0] is True


# -- in-flight key normalisation ---------------------------------------------------------


def _key(**over):
    from sandbox.order_manager import OrderManager, _inflight_key

    order = _order(price_type="LIMIT")
    order.update({"price": "2500", "trigger_price": 0})
    order.update(over)
    return _inflight_key(OrderManager(USER), order)


def test_inflight_key_matches_across_decimal_spellings_for_limit_and_sl():
    assert _key(price="2500") == _key(price="2500.00") == _key(price=2500.0)
    assert _key(price="2500") != _key(price="2500.05")
    assert _key(price_type="SL", price="2500", trigger_price="2490") == _key(
        price_type="SL", price="2500.00", trigger_price="2490.0"
    )


def test_inflight_key_ignores_price_and_trigger_for_market():
    assert _key(price_type="MARKET", price=0) == _key(price_type="MARKET", price="2500.5")
    assert _key(price_type="MARKET", trigger_price="9") == _key(price_type="MARKET")
