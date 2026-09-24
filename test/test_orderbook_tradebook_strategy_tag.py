"""
Live orderbook/tradebook responses gain a `strategy` field.

Sandbox already returns `strategy` (database/sandbox_db.py), so only the live
path - a pure broker passthrough with no concept of OpenAlgo's strategy tag -
needs enrichment from the strategy book. These tests mock the broker module
boundary and assert the enrichment is applied without needing a real broker.

Run with: uv run pytest test/test_orderbook_tradebook_strategy_tag.py -v
"""

import uuid

import pytest

import services.orderbook_service as orderbook_service
import services.tradebook_service as tradebook_service
from database.settings_db import init_db as init_settings_db
from database.strategy_book_db import init_strategy_book_db, record_order_tag


@pytest.fixture(scope="module", autouse=True)
def _init_book():
    init_strategy_book_db()
    # _prune_old_tags() can leave its scoped session's transaction open when
    # there is nothing to prune (no commit on the no-op path), which holds a
    # brief write lock on the shared SQLite file. Release it before another
    # engine (settings_db) tries to CREATE TABLE against the same file.
    from database.strategy_book_db import db_session as strategy_book_session

    strategy_book_session.remove()
    # get_orderbook_with_auth/get_tradebook_with_auth check get_analyze_mode(),
    # which queries the settings table.
    init_settings_db()


def _oid():
    return f"live-test-{uuid.uuid4().hex}"


def test_live_orderbook_gets_tagged_from_strategy_book(monkeypatch):
    oid = _oid()
    record_order_tag(
        orderid=oid, user_id="", strategy="ORB", symbol="SBIN", exchange="NSE", product="MIS"
    )

    def fake_import_broker_module(_broker_name):
        return {
            "get_order_book": lambda _auth: {"data": []},
            "map_order_data": lambda order_data: [{"orderid": oid, "symbol": "SBIN"}],
            "calculate_order_statistics": lambda _orders: {},
            "transform_order_data": lambda orders: orders,
        }

    monkeypatch.setattr(orderbook_service, "import_broker_module", fake_import_broker_module)

    success, response, status = orderbook_service.get_orderbook_with_auth(
        "fake-token", "fakebroker", original_data=None
    )

    assert success is True
    assert status == 200
    orders = response["data"]["orders"]
    assert orders[0]["strategy"] == "ORB"


def test_live_orderbook_untagged_order_gets_empty_strategy(monkeypatch):
    def fake_import_broker_module(_broker_name):
        return {
            "get_order_book": lambda _auth: {"data": []},
            "map_order_data": lambda order_data: [{"orderid": "never-tagged", "symbol": "TCS"}],
            "calculate_order_statistics": lambda _orders: {},
            "transform_order_data": lambda orders: orders,
        }

    monkeypatch.setattr(orderbook_service, "import_broker_module", fake_import_broker_module)

    success, response, _status = orderbook_service.get_orderbook_with_auth(
        "fake-token", "fakebroker", original_data=None
    )

    assert success is True
    assert response["data"]["orders"][0]["strategy"] == ""


def test_live_tradebook_gets_tagged_from_strategy_book(monkeypatch):
    oid = _oid()
    record_order_tag(
        orderid=oid, user_id="", strategy="BREAKOUT", symbol="INFY", exchange="NSE", product="MIS"
    )

    def fake_import_broker_module(_broker_name):
        return {
            "get_trade_book": lambda _auth: {"data": []},
            "map_trade_data": lambda trade_data: [{"orderid": oid, "symbol": "INFY"}],
            "transform_tradebook_data": lambda trades: trades,
        }

    monkeypatch.setattr(tradebook_service, "import_broker_module", fake_import_broker_module)

    success, response, status = tradebook_service.get_tradebook_with_auth(
        "fake-token", "fakebroker", original_data=None
    )

    assert success is True
    assert status == 200
    assert response["data"][0]["strategy"] == "BREAKOUT"
