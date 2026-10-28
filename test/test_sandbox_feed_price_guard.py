"""The live-feed price helper, the guarantee that sandbox orders never use it, and the priority
tags on sandbox quote calls.

The feed carries a last price only. Sandbox MARKET orders fill a BUY at the ask and a SELL at
the bid from a REST quote, and must keep doing so; when the REST quote cannot be had they are
rejected, not priced from the feed.
"""

from __future__ import annotations

import time
from decimal import Decimal

import pytest
from test_gthread_sandbox_support import (
    prepare_databases,
    quote,
    release_sessions,
    reset_user,
    run_in_thread,
)

from sandbox import feed_price
from utils import broker_priority

USER = "feed_price_guard"


class FakeService:
    """Stands in for MarketDataService: one cache entry and a health verdict."""

    def __init__(self, ltp=2500.0, age=0.5, safe=True, reason=""):
        self.entry = {"ltp": {"value": ltp, "timestamp": 0}, "last_update": time.time() - age}
        self.safe = safe
        self.reason = reason
        self.reads = 0

    def is_trade_management_safe(self):
        return self.safe, self.reason

    def get_all_data(self, symbol, exchange):
        self.reads += 1
        return self.entry


@pytest.fixture
def service(monkeypatch):
    fake = FakeService()
    monkeypatch.setattr(feed_price, "get_market_data_service", lambda: fake)
    return fake


# -- feed_quote ----------------------------------------------------------------


def test_fresh_tick_on_a_healthy_feed_is_returned(service):
    result = feed_price.feed_quote("RELIANCE", "NSE", max_age=2.0)
    assert result["ltp"] == 2500.0
    assert result["bid"] == 0 and result["ask"] == 0  # the feed carries no book
    assert result["source"] == "feed"
    assert 0 <= result["age"] < 2.0


def test_tick_older_than_the_bound_is_refused(service):
    service.entry["last_update"] = time.time() - 2.6
    assert feed_price.feed_quote("RELIANCE", "NSE", max_age=2.0) is None


def test_tick_exactly_inside_the_bound_is_accepted(service):
    service.entry["last_update"] = time.time() - 1.9
    assert feed_price.feed_quote("RELIANCE", "NSE", max_age=2.0) is not None


def test_unhealthy_feed_is_refused_even_with_a_fresh_tick(service):
    service.safe, service.reason = False, "Data flow inactive for 40s"
    assert feed_price.feed_quote("RELIANCE", "NSE", max_age=2.0) is None


def test_symbol_not_on_the_feed_is_refused(service):
    service.entry = {}
    assert feed_price.feed_quote("RELIANCE", "NSE", max_age=2.0) is None


@pytest.mark.parametrize("bad", [0, 0.0, None, -5.0])
def test_non_positive_or_missing_price_is_refused(service, bad):
    service.entry["ltp"] = {"value": bad}
    assert feed_price.feed_quote("RELIANCE", "NSE", max_age=2.0) is None


def test_timestamp_from_the_future_is_refused(service):
    service.entry["last_update"] = time.time() + 30
    assert feed_price.feed_quote("RELIANCE", "NSE", max_age=2.0) is None


def test_disabled_bound_never_touches_the_feed(service):
    assert feed_price.feed_quote("RELIANCE", "NSE", max_age=0) is None
    assert service.reads == 0


def test_a_failing_feed_read_is_a_refusal_not_an_error(monkeypatch):
    def boom():
        raise RuntimeError("cache gone")

    monkeypatch.setattr(feed_price, "get_market_data_service", boom)
    assert feed_price.feed_quote("RELIANCE", "NSE", max_age=2.0) is None


# -- the order path ---------------------------------------------------------------


@pytest.fixture
def fresh_account(monkeypatch):
    prepare_databases()
    reset_user(USER)
    release_sessions()
    yield
    release_sessions()


def _place_market(action="BUY"):
    from sandbox.order_manager import OrderManager

    order = {
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "action": action,
        "quantity": 10,
        "price_type": "MARKET",
        "product": "CNC",
    }
    ok, response, _ = run_in_thread(lambda: OrderManager(USER).place_order(order))
    return ok, response


def _average_price(orderid):
    from database.sandbox_db import SandboxOrders, db_session

    db_session.remove()
    row = SandboxOrders.query.filter_by(orderid=orderid).first()
    return Decimal(str(row.average_price)), row.order_status


@pytest.fixture
def rest_calls(monkeypatch):
    from sandbox.execution_engine import ExecutionEngine

    calls = []

    def fake(self, symbol, exchange):
        calls.append((symbol, exchange))
        return quote(2500, spread=0.5)

    monkeypatch.setattr(ExecutionEngine, "_fetch_quote", fake)
    return calls


def test_a_market_order_prices_from_the_rest_quote_and_fills_at_the_ask(
    fresh_account, service, rest_calls
):
    ok, response = _place_market()
    assert ok, response
    assert len(rest_calls) == 1
    assert service.reads == 0  # the feed is not consulted when an order is priced
    price, status = _average_price(response["orderid"])
    assert status == "complete"
    assert price == Decimal("2500.5")  # the ask, not the last price


def test_a_market_order_is_rejected_when_the_rest_quote_fails_even_with_a_fresh_feed(
    fresh_account, service, monkeypatch
):
    from sandbox.execution_engine import ExecutionEngine

    monkeypatch.setattr(ExecutionEngine, "_fetch_quote", lambda self, s, e: None)
    monkeypatch.setattr("sandbox.order_manager.time.sleep", lambda *_: None)
    ok, response = _place_market()
    assert not ok
    assert "unable to fetch current price" in response["message"]
    assert service.reads == 0


# -- priority tags on sandbox quote calls -------------------------------------------


@pytest.fixture
def api_key(monkeypatch):
    import database.auth_db as auth_db

    class Row:
        api_key_encrypted = "enc"

    class Keys:
        class query:
            @staticmethod
            def first():
                return Row()

    monkeypatch.setattr(auth_db, "ApiKeys", Keys)
    monkeypatch.setattr(auth_db, "decrypt_token", lambda value: "plain")


def test_order_pricing_quotes_are_tagged_critical(monkeypatch, api_key):
    from sandbox import execution_engine

    seen = []

    def fake_get_quotes(symbol, exchange, api_key):
        seen.append(broker_priority.current())
        return True, {"data": {"ltp": 1.0}}, 200

    monkeypatch.setattr(execution_engine, "get_quotes", fake_get_quotes)
    execution_engine.ExecutionEngine._fetch_quote(object(), "RELIANCE", "NSE")
    assert seen == [broker_priority.CRITICAL]
    assert broker_priority.current() == broker_priority.NORMAL


def test_pending_order_batch_quotes_are_tagged_critical(monkeypatch, api_key):
    from sandbox import execution_engine

    seen = []

    def fake_multi(symbols, api_key):
        seen.append(broker_priority.current())
        return True, {"results": []}, 200

    monkeypatch.setattr(execution_engine, "get_multiquotes", fake_multi)
    execution_engine.ExecutionEngine._fetch_quotes_batch(object(), [("RELIANCE", "NSE")])
    assert seen == [broker_priority.CRITICAL]


def test_position_valuation_fallback_quotes_are_tagged_background(monkeypatch, api_key):
    from sandbox import position_manager

    seen = []

    def fake_multi(symbols, api_key):
        seen.append(broker_priority.current())
        return True, {"results": []}, 200

    monkeypatch.setattr(position_manager, "get_multiquotes", fake_multi)
    position_manager.PositionManager._fetch_quotes_batch(object(), [("RELIANCE", "NSE")])
    assert seen == [broker_priority.BACKGROUND]
