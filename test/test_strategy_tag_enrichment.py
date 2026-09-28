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
    apply_fill,
    get_strategies_for_orderids,
    init_strategy_book_db,
    record_order_tag,
)
from services.strategy_tag_enrichment import attach_strategy, attach_strategy_to_positions


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


def _open_leg(strategy, symbol, exchange, product, mode, qty, price, action):
    oid = _oid()
    assert record_order_tag(
        orderid=oid,
        user_id="",
        strategy=strategy,
        symbol=symbol,
        exchange=exchange,
        product=product,
        mode=mode,
    )
    apply_fill(oid, filled_quantity=qty, average_price=price, action=action)


def test_attach_strategy_to_positions_matches_an_open_leg(monkeypatch):
    import database.settings_db as settings_db

    monkeypatch.setattr(settings_db, "get_analyze_mode", lambda: True)

    symbol = "POS" + uuid.uuid4().hex[:8].upper()
    strategy = f"POSEN-OPEN-{uuid.uuid4().hex[:8]}"
    _open_leg(strategy, symbol, "NSE", "MIS", "analyze", qty=10, price=100.0, action="BUY")

    result = attach_strategy_to_positions(
        [{"symbol": symbol, "exchange": "NSE", "product": "MIS", "quantity": 10}]
    )

    assert result[0]["strategy"] == strategy


def test_attach_strategy_to_positions_matches_a_leg_closed_today(monkeypatch):
    """A position row with quantity=0 still appears on the Positions page
    when it was closed today - the strategy that closed it must not read as
    blank just because the leg has gone flat.
    """
    import database.settings_db as settings_db

    monkeypatch.setattr(settings_db, "get_analyze_mode", lambda: True)

    symbol = "POS" + uuid.uuid4().hex[:8].upper()
    strategy = f"POSEN-CLOSED-{uuid.uuid4().hex[:8]}"
    open_oid, close_oid = _oid(), _oid()
    for oid in (open_oid, close_oid):
        assert record_order_tag(
            orderid=oid,
            user_id="",
            strategy=strategy,
            symbol=symbol,
            exchange="NSE",
            product="MIS",
            mode="analyze",
        )
    apply_fill(open_oid, filled_quantity=10, average_price=100.0, action="BUY")
    apply_fill(close_oid, filled_quantity=10, average_price=105.0, action="SELL")

    result = attach_strategy_to_positions(
        [{"symbol": symbol, "exchange": "NSE", "product": "MIS", "quantity": 0}]
    )

    assert result[0]["strategy"] == strategy


def test_attach_strategy_to_positions_ignores_a_stale_closed_leg_from_another_day(
    monkeypatch,
):
    """A (strategy, symbol, exchange, product, mode) row persists forever
    once closed, with no per-day scoping of its own. If a DIFFERENT
    strategy closed the same symbol on a prior day, that row must not
    resurface today just because it shares the key - confirmed in
    production for NATIONALUM (a stale ORB row from three days earlier sat
    alongside today's real BREAKINGTRADE close).
    """
    import database.settings_db as settings_db
    import database.strategy_book_db as book

    monkeypatch.setattr(settings_db, "get_analyze_mode", lambda: True)

    symbol = "POS" + uuid.uuid4().hex[:8].upper()
    stale_strategy = f"POSEN-STALE-{uuid.uuid4().hex[:8]}"

    # Force this leg's close to be recorded under an earlier session date,
    # so get_strategy_legs() reads its today_realized_pnl back as 0 - the
    # same mechanism that ages out a real multi-day-old row.
    monkeypatch.setattr(book, "_session_date", lambda: "2020-01-01")
    open_oid, close_oid = _oid(), _oid()
    for oid in (open_oid, close_oid):
        assert record_order_tag(
            orderid=oid,
            user_id="",
            strategy=stale_strategy,
            symbol=symbol,
            exchange="NSE",
            product="MIS",
            mode="analyze",
        )
    apply_fill(open_oid, filled_quantity=5, average_price=50.0, action="BUY")
    apply_fill(close_oid, filled_quantity=5, average_price=55.0, action="SELL")
    monkeypatch.undo()

    result = attach_strategy_to_positions(
        [{"symbol": symbol, "exchange": "NSE", "product": "MIS", "quantity": 0}]
    )

    assert result[0]["strategy"] == ""


def test_attach_strategy_to_positions_handles_empty_list():
    assert attach_strategy_to_positions([]) == []
