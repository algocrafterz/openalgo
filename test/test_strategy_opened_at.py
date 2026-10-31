"""Entry time bookkeeping: StrategyPosition.opened_at and StrategyClosedTrade.opened_at.

Run with: uv run pytest test/test_strategy_opened_at.py -v
"""

import uuid
from datetime import datetime, timedelta

import pytest

from database.strategy_book_db import (
    StrategyPosition,
    apply_fill,
    db_session,
    get_closed_trades,
    init_strategy_book_db,
    record_order_tag,
)

T0 = datetime(2026, 10, 30, 9, 20, 0)


@pytest.fixture(scope="module", autouse=True)
def _init_book():
    init_strategy_book_db()


def _fill(strategy, action, qty, price, at=None, mode="analyze"):
    oid = f"test-opened-{uuid.uuid4().hex}"
    assert record_order_tag(
        orderid=oid,
        user_id="",
        strategy=strategy,
        symbol="SBIN",
        exchange="NSE",
        product="MIS",
        mode=mode,
    )
    kwargs = {} if at is None else {"filled_at": at}
    return apply_fill(oid, filled_quantity=qty, average_price=price, action=action, **kwargs)


def _leg_opened_at(strategy, mode="analyze"):
    db_session.remove()
    try:
        leg = db_session.query(StrategyPosition).filter_by(strategy=strategy, mode=mode).one()
        return leg.opened_at
    finally:
        db_session.remove()


def _name():
    return f"OPENED-{uuid.uuid4().hex[:8]}"


def test_opening_fill_stamps_leg_opened_at():
    s = _name()
    _fill(s, "BUY", 10, 100.0, T0)
    assert _leg_opened_at(s) == T0


def test_adding_in_same_direction_keeps_opened_at():
    s = _name()
    _fill(s, "BUY", 10, 100.0, T0)
    _fill(s, "BUY", 5, 101.0, T0 + timedelta(minutes=30))
    assert _leg_opened_at(s) == T0


def test_partial_reduce_keeps_opened_at_and_closed_row_carries_it():
    s = _name()
    _fill(s, "BUY", 10, 100.0, T0)
    _fill(s, "SELL", 4, 102.0, T0 + timedelta(minutes=45))
    assert _leg_opened_at(s) == T0
    (row,) = get_closed_trades(strategy=s, mode="analyze")
    assert row["opened_at"] == T0.isoformat()


def test_full_close_row_carries_old_opened_at():
    s = _name()
    _fill(s, "BUY", 10, 100.0, T0)
    _fill(s, "SELL", 10, 102.0, T0 + timedelta(hours=2))
    (row,) = get_closed_trades(strategy=s, mode="analyze")
    assert row["opened_at"] == T0.isoformat()
    assert row["realized_pnl"] == 20.0


def test_flip_writes_closed_part_with_old_time_and_reopens_at_fill_time():
    s = _name()
    flip_at = T0 + timedelta(hours=1)
    _fill(s, "BUY", 10, 100.0, T0)
    _fill(s, "SELL", 15, 102.0, flip_at)
    (row,) = get_closed_trades(strategy=s, mode="analyze")
    assert row["opened_at"] == T0.isoformat()
    assert row["closed_quantity"] == 10.0
    assert _leg_opened_at(s) == flip_at


def test_reopen_after_flat_gets_new_opened_at():
    s = _name()
    _fill(s, "BUY", 10, 100.0, T0)
    _fill(s, "SELL", 10, 101.0, T0 + timedelta(minutes=10))
    again = T0 + timedelta(hours=3)
    _fill(s, "BUY", 5, 100.0, again)
    assert _leg_opened_at(s) == again


def test_default_fill_time_is_now():
    s = _name()
    before = datetime.now()
    _fill(s, "SELL", 10, 100.0)
    after = datetime.now()
    assert before <= _leg_opened_at(s) <= after


def test_pnl_and_quantities_unchanged_by_timestamps():
    s = _name()
    _fill(s, "SELL", 10, 100.0, T0)
    result = _fill(s, "BUY", 4, 98.0, T0 + timedelta(minutes=5))
    assert result["quantity"] == -6.0
    assert result["average_price"] == 100.0
    assert result["realized_pnl"] == 8.0


def test_replayed_fill_times_both_ends_with_the_fill_clock():
    """The reconciler replays a missed fill later with the order's own time. The
    closed row's closed_at must use that time too, or the hold is stretched to the
    moment of the replay."""
    s = _name()
    exit_at = T0 + timedelta(minutes=25)
    _fill(s, "BUY", 10, 100.0, T0)
    _fill(s, "SELL", 10, 101.0, exit_at)
    (row,) = get_closed_trades(strategy=s, mode="analyze")
    assert row["opened_at"] == T0.isoformat()
    assert row["closed_at"] == exit_at.isoformat()
