"""
Unit tests: the strategy book must keep paper (analyze) and live positions
separate, not merge them into one row.

Before this change, `StrategyPosition` was keyed on
`(user_id, strategy, symbol, exchange, product)` with no mode. A paper BUY
and a live BUY of the same symbol under the same strategy name booked into
the SAME row, so a strategy promoted from paper to live silently inherited
its paper P&L, and a strategy traded in both modes at once (routine while
validating a promotion) reported one blended number that answered neither
question ("is the live version working?" / "is the paper version working?").

`record_order_tag`/`apply_fill`/`get_strategy_legs` are now mode-aware:
mode is captured at tag time (the only place `OrderEvent.mode` is available)
and carried onto the position row it books against.

Run with: uv run pytest test/test_strategy_book_mode_separation.py -v
"""

import uuid

import pytest

from database.strategy_book_db import (
    apply_fill,
    get_strategy_legs,
    init_strategy_book_db,
    record_order_tag,
)


@pytest.fixture(scope="module", autouse=True)
def _init_book():
    init_strategy_book_db()


def _oid():
    return f"test-mode-{uuid.uuid4().hex}"


def _tag(orderid, strategy, mode, symbol="SBIN", exchange="NSE", product="MIS"):
    assert record_order_tag(
        orderid=orderid,
        user_id="",
        strategy=strategy,
        symbol=symbol,
        exchange=exchange,
        product=product,
        mode=mode,
    )


def test_live_and_analyze_positions_stay_separate_for_same_leg():
    strategy = f"MODESEP-{uuid.uuid4().hex[:8]}"
    live_oid, analyze_oid = _oid(), _oid()

    _tag(live_oid, strategy, mode="live")
    _tag(analyze_oid, strategy, mode="analyze")

    apply_fill(live_oid, filled_quantity=10, average_price=100.0, action="BUY")
    apply_fill(analyze_oid, filled_quantity=25, average_price=200.0, action="BUY")

    live_legs = get_strategy_legs(strategy=strategy, mode="live")
    analyze_legs = get_strategy_legs(strategy=strategy, mode="analyze")

    assert len(live_legs) == 1
    assert live_legs[0]["quantity"] == 10
    assert live_legs[0]["average_price"] == 100.0

    assert len(analyze_legs) == 1
    assert analyze_legs[0]["quantity"] == 25
    assert analyze_legs[0]["average_price"] == 200.0


def test_get_strategy_legs_with_no_mode_filter_returns_every_mode():
    """Flow's own reader (`strategy_pnl_service.get_strategy_pnl`) never passes
    `mode` and must keep seeing every leg regardless of mode - this is the
    backward-compatibility contract for the untouched Flow integration."""
    strategy = f"MODESEP-ALL-{uuid.uuid4().hex[:8]}"
    live_oid, analyze_oid = _oid(), _oid()

    _tag(live_oid, strategy, mode="live")
    _tag(analyze_oid, strategy, mode="analyze")
    apply_fill(live_oid, filled_quantity=5, average_price=50.0, action="BUY")
    apply_fill(analyze_oid, filled_quantity=7, average_price=60.0, action="BUY")

    all_legs = get_strategy_legs(strategy=strategy)

    assert len(all_legs) == 2
    assert {leg["mode"] for leg in all_legs} == {"live", "analyze"}


def test_missing_mode_backfills_to_unknown():
    """A tag recorded without an explicit mode (empty string, e.g. an event
    whose `mode` attribute was blank) must not be stored as NULL/empty - it
    must be bucketed as 'unknown' so it never silently joins current-mode
    figures."""
    strategy = f"MODESEP-UNKNOWN-{uuid.uuid4().hex[:8]}"
    oid = _oid()

    _tag(oid, strategy, mode="")
    apply_fill(oid, filled_quantity=3, average_price=30.0, action="BUY")

    unknown_legs = get_strategy_legs(strategy=strategy, mode="unknown")
    live_legs = get_strategy_legs(strategy=strategy, mode="live")

    assert len(unknown_legs) == 1
    assert len(live_legs) == 0


def test_leg_dict_includes_mode():
    strategy = f"MODESEP-SHAPE-{uuid.uuid4().hex[:8]}"
    oid = _oid()
    _tag(oid, strategy, mode="live")
    apply_fill(oid, filled_quantity=1, average_price=10.0, action="BUY")

    legs = get_strategy_legs(strategy=strategy, mode="live")

    assert legs[0]["mode"] == "live"
