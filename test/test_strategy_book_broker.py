"""The broker an order went through travels from the order tag to the closed-trade ledger, so
charges can be priced per broker (live and paper alike).

Run with: uv run pytest test/test_strategy_book_broker.py -v
"""

import uuid
from types import SimpleNamespace

import pytest

from database.strategy_book_db import (
    apply_fill,
    get_closed_trades,
    init_strategy_book_db,
    record_order_tag,
)
from subscribers import strategy_book_subscriber as sub


@pytest.fixture(scope="module", autouse=True)
def _init_book():
    init_strategy_book_db()


def _oid():
    return f"test-broker-{uuid.uuid4().hex}"


def _tag(orderid, strategy, broker="", mode="analyze", symbol="SBIN"):
    assert record_order_tag(
        orderid=orderid,
        user_id="",
        strategy=strategy,
        symbol=symbol,
        exchange="NSE",
        product="MIS",
        mode=mode,
        broker=broker,
    )


def _round_trip(strategy, open_broker, close_broker, mode="analyze"):
    open_oid, close_oid = _oid(), _oid()
    _tag(open_oid, strategy, open_broker, mode)
    _tag(close_oid, strategy, close_broker, mode)
    apply_fill(open_oid, filled_quantity=10, average_price=100.0, action="BUY")
    apply_fill(close_oid, filled_quantity=10, average_price=101.0, action="SELL")
    return get_closed_trades(strategy=strategy, mode=mode)


def test_closed_trade_carries_the_broker_of_the_closing_order():
    strategy = f"BRK-{uuid.uuid4().hex[:8]}"
    (trade,) = _round_trip(strategy, "flattrade", "flattrade")
    assert trade["broker"] == "flattrade"


def test_broker_names_are_normalised():
    strategy = f"BRK-{uuid.uuid4().hex[:8]}"
    (trade,) = _round_trip(strategy, " Flattrade ", " FLATTRADE ")
    assert trade["broker"] == "flattrade"


def test_missing_broker_is_recorded_as_unknown_not_blank():
    strategy = f"BRK-{uuid.uuid4().hex[:8]}"
    (trade,) = _round_trip(strategy, "", "")
    assert trade["broker"] == "unknown"


def test_paper_and_live_trades_both_carry_a_broker():
    for mode in ("analyze", "live"):
        strategy = f"BRK-{mode}-{uuid.uuid4().hex[:8]}"
        (trade,) = _round_trip(strategy, "flattrade", "flattrade", mode=mode)
        assert trade["broker"] == "flattrade"


def test_a_position_closed_at_a_different_broker_is_filed_under_the_closing_broker():
    strategy = f"BRK-{uuid.uuid4().hex[:8]}"
    (trade,) = _round_trip(strategy, "oldbroker", "flattrade")
    assert trade["broker"] == "flattrade"


class TestSubscriberResolvesTheBroker:
    def _event(self, api_key="KEY", **kw):
        base = {
            "strategy": "S",
            "orderid": _oid(),
            "symbol": "SBIN",
            "exchange": "NSE",
            "product": "MIS",
            "mode": "analyze",
            "api_key": api_key,
            "request_data": {},
        }
        base.update(kw)
        return SimpleNamespace(**base)

    def test_broker_comes_from_the_auth_record_of_the_events_api_key(self, monkeypatch):
        monkeypatch.setattr(
            "database.auth_db.get_auth_token_broker", lambda key: ("token", "flattrade")
        )
        assert sub._broker(self._event()) == "flattrade"

    def test_api_key_in_the_request_body_is_used_when_the_event_has_none(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            "database.auth_db.get_auth_token_broker",
            lambda key: seen.append(key) or ("t", "flattrade"),
        )
        sub._broker(self._event(api_key="", request_data={"apikey": "BODYKEY"}))
        assert seen == ["BODYKEY"]

    def test_lookup_failure_never_blocks_tagging(self, monkeypatch):
        def boom(key):
            raise RuntimeError("auth db down")

        monkeypatch.setattr("database.auth_db.get_auth_token_broker", boom)
        assert sub._broker(self._event()) == ""

    def test_no_api_key_means_no_broker(self):
        assert sub._broker(self._event(api_key="")) == ""

    def test_order_placed_tags_the_order_with_the_broker(self, monkeypatch):
        monkeypatch.setattr(
            "database.auth_db.get_auth_token_broker", lambda key: ("t", "flattrade")
        )
        strategy = f"BRK-{uuid.uuid4().hex[:8]}"
        opening = self._event(strategy=strategy)
        closing = self._event(strategy=strategy, orderid=_oid())
        sub.on_order_placed(opening)
        sub.on_order_placed(closing)
        apply_fill(opening.orderid, filled_quantity=4, average_price=10.0, action="BUY")
        apply_fill(closing.orderid, filled_quantity=4, average_price=11.0, action="SELL")
        (trade,) = get_closed_trades(strategy=strategy, mode="analyze")
        assert trade["broker"] == "flattrade"
