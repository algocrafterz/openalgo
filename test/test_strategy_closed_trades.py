"""
Unit tests: closing fills append a row to the `strategy_closed_trades` ledger,
which day-by-day performance metrics (win rate, profit factor, streaks) are
computed from. See docs/strategy-daily-performance.md for why this ledger
exists instead of an EOD snapshot job.

Run with: uv run pytest test/test_strategy_closed_trades.py -v
"""

import uuid

import pytest

from database.strategy_book_db import (
    apply_fill,
    get_closed_trades,
    init_strategy_book_db,
    record_order_tag,
)


@pytest.fixture(scope="module", autouse=True)
def _init_book():
    init_strategy_book_db()


def _oid():
    return f"test-closed-{uuid.uuid4().hex}"


def _tag(orderid, strategy, mode="live", symbol="SBIN", exchange="NSE", product="MIS"):
    assert record_order_tag(
        orderid=orderid,
        user_id="",
        strategy=strategy,
        symbol=symbol,
        exchange=exchange,
        product=product,
        mode=mode,
    )


def test_opening_fill_writes_no_closed_trade_row():
    strategy = f"CLOSED-OPEN-{uuid.uuid4().hex[:8]}"
    oid = _oid()
    _tag(oid, strategy)

    apply_fill(oid, filled_quantity=10, average_price=100.0, action="BUY")

    trades = get_closed_trades(strategy=strategy, mode="live")
    assert trades == []


def test_full_close_writes_one_closed_trade_row_with_correct_pnl():
    strategy = f"CLOSED-FULL-{uuid.uuid4().hex[:8]}"
    open_oid, close_oid = _oid(), _oid()
    _tag(open_oid, strategy)
    _tag(close_oid, strategy)

    apply_fill(open_oid, filled_quantity=10, average_price=100.0, action="BUY")
    apply_fill(close_oid, filled_quantity=10, average_price=110.0, action="SELL")

    trades = get_closed_trades(strategy=strategy, mode="live")
    assert len(trades) == 1
    trade = trades[0]
    assert trade["direction"] == "LONG"
    assert trade["closed_quantity"] == 10
    assert trade["entry_price"] == 100.0
    assert trade["exit_price"] == 110.0
    assert trade["realized_pnl"] == pytest.approx(100.0)  # 10 * (110 - 100)
    assert trade["mode"] == "live"


def test_short_close_reports_short_direction_and_correct_pnl():
    strategy = f"CLOSED-SHORT-{uuid.uuid4().hex[:8]}"
    open_oid, close_oid = _oid(), _oid()
    _tag(open_oid, strategy)
    _tag(close_oid, strategy)

    apply_fill(open_oid, filled_quantity=5, average_price=200.0, action="SELL")
    apply_fill(close_oid, filled_quantity=5, average_price=180.0, action="BUY")

    trades = get_closed_trades(strategy=strategy, mode="live")
    assert len(trades) == 1
    trade = trades[0]
    assert trade["direction"] == "SHORT"
    assert trade["realized_pnl"] == pytest.approx(100.0)  # 5 * (200 - 180)


def test_partial_close_writes_only_the_closed_portion():
    strategy = f"CLOSED-PARTIAL-{uuid.uuid4().hex[:8]}"
    open_oid, close_oid = _oid(), _oid()
    _tag(open_oid, strategy)
    _tag(close_oid, strategy)

    apply_fill(open_oid, filled_quantity=10, average_price=100.0, action="BUY")
    apply_fill(close_oid, filled_quantity=4, average_price=120.0, action="SELL")

    trades = get_closed_trades(strategy=strategy, mode="live")
    assert len(trades) == 1
    assert trades[0]["closed_quantity"] == 4
    assert trades[0]["realized_pnl"] == pytest.approx(80.0)  # 4 * (120 - 100)


def test_get_closed_trades_filters_by_strategy_and_mode():
    strategy_a = f"CLOSED-FILT-A-{uuid.uuid4().hex[:8]}"
    strategy_b = f"CLOSED-FILT-B-{uuid.uuid4().hex[:8]}"

    for strategy, mode in ((strategy_a, "live"), (strategy_a, "analyze"), (strategy_b, "live")):
        open_oid, close_oid = _oid(), _oid()
        _tag(open_oid, strategy, mode=mode)
        _tag(close_oid, strategy, mode=mode)
        apply_fill(open_oid, filled_quantity=1, average_price=50.0, action="BUY")
        apply_fill(close_oid, filled_quantity=1, average_price=55.0, action="SELL")

    assert len(get_closed_trades(strategy=strategy_a, mode="live")) == 1
    assert len(get_closed_trades(strategy=strategy_a, mode="analyze")) == 1
    assert len(get_closed_trades(strategy=strategy_a)) == 2  # no mode filter
    assert len(get_closed_trades(strategy=strategy_b, mode="live")) == 1


def test_get_closed_trades_filters_by_date_range():
    strategy = f"CLOSED-DATE-{uuid.uuid4().hex[:8]}"
    open_oid, close_oid = _oid(), _oid()
    _tag(open_oid, strategy)
    _tag(close_oid, strategy)
    apply_fill(open_oid, filled_quantity=1, average_price=50.0, action="BUY")
    apply_fill(close_oid, filled_quantity=1, average_price=55.0, action="SELL")

    # Today's trade is excluded by a range that ends yesterday, and included
    # by one that starts today.
    trades = get_closed_trades(strategy=strategy, mode="live")
    today = trades[0]["trade_date"]

    assert get_closed_trades(strategy=strategy, mode="live", end_date="2000-01-01") == []
    assert len(get_closed_trades(strategy=strategy, mode="live", start_date=today)) == 1
