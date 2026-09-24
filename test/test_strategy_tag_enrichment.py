"""
Unit tests for surfacing the strategy tag on the live order/trade book.

`database/strategy_book_db.py` already records orderid -> strategy for both
live and sandbox orders (fed by the event bus), but only Flow's internal
StrategyPnlNode ever reads it. These tests cover the new bulk lookup and the
enrichment helper that attaches it to live orderbook/tradebook rows, which
never have a `strategy` field of their own (the broker has no concept of it).

Run with: uv run pytest test/test_strategy_tag_enrichment.py -v
"""

import uuid

import pytest

from database.strategy_book_db import (
    get_strategies_for_orderids,
    init_strategy_book_db,
    record_order_tag,
)
from services.strategy_tag_enrichment import attach_strategy


@pytest.fixture(scope="module", autouse=True)
def _init_book():
    init_strategy_book_db()


def _tag(orderid, strategy="ORB"):
    assert record_order_tag(
        orderid=orderid,
        user_id="",
        strategy=strategy,
        symbol="SBIN",
        exchange="NSE",
        product="MIS",
    )


def _oid():
    return f"test-{uuid.uuid4().hex}"


def test_get_strategies_for_orderids_returns_known_tags():
    oid1, oid2 = _oid(), _oid()
    _tag(oid1, "ORB")
    _tag(oid2, "BREAKOUT")

    result = get_strategies_for_orderids([oid1, oid2, "unknown-order"])

    assert result == {oid1: "ORB", oid2: "BREAKOUT"}


def test_get_strategies_for_orderids_empty_input_returns_empty_dict():
    assert get_strategies_for_orderids([]) == {}
    assert get_strategies_for_orderids(None) == {}


def test_get_strategies_for_orderids_chunks_past_sqlite_variable_limit():
    # Chunked at 500; use 501 ids (one real tag included) to force two chunks.
    oid = _oid()
    _tag(oid, "CHUNKTEST")
    ids = [f"filler-{i}" for i in range(500)] + [oid]

    result = get_strategies_for_orderids(ids)

    assert result.get(oid) == "CHUNKTEST"
    assert len(result) == 1


def test_get_strategies_for_orderids_returns_empty_dict_on_db_error(monkeypatch):
    import database.strategy_book_db as book_db

    def _boom(*_args, **_kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(book_db.db_session, "query", _boom)

    assert get_strategies_for_orderids(["whatever"]) == {}


def test_get_strategies_for_orderids_returns_empty_dict_when_uninitialized(monkeypatch):
    import database.strategy_book_db as book_db

    monkeypatch.setattr(book_db, "_initialized", False)

    assert get_strategies_for_orderids(["whatever"]) == {}


def test_attach_strategy_fills_missing_field_and_defaults_to_empty_string():
    oid = _oid()
    _tag(oid, "ORB")
    rows = [{"orderid": oid, "symbol": "SBIN"}, {"orderid": "untagged-order", "symbol": "TCS"}]

    result = attach_strategy(rows)

    assert result[0]["strategy"] == "ORB"
    assert result[1]["strategy"] == ""


def test_attach_strategy_does_not_mutate_input():
    oid = _oid()
    _tag(oid, "ORB")
    rows = [{"orderid": oid}]

    result = attach_strategy(rows)

    assert "strategy" not in rows[0]
    assert result[0] is not rows[0]


def test_attach_strategy_leaves_existing_strategy_value_untouched():
    """Sandbox rows already carry `strategy`; enrichment must not overwrite it."""
    rows = [{"orderid": "some-sandbox-order", "strategy": "ALREADY-SET"}]

    result = attach_strategy(rows)

    assert result[0]["strategy"] == "ALREADY-SET"


def test_attach_strategy_handles_empty_list():
    assert attach_strategy([]) == []
