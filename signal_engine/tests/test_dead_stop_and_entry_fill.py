"""E-14: a stop the broker rejected or cancelled after placement is replaced.
E-20: an entry confirmed complete records its fill price."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine.strategies import ORB
from signal_engine.tests.tracker_fixtures import _make_engine, _make_position
from signal_engine.tracker import _FILL_PROCEED, PositionTracker


def _tracker(**pos_kw):
    tracker = PositionTracker(_make_engine())
    pos = _make_position(symbol="RELIANCE", strategy=ORB, sl_order_id="SL1", **pos_kw)
    tracker.register(pos)
    return tracker, pos


async def _audit(tracker, rows):
    with (
        patch("signal_engine.tracker.fetch_orderbook", AsyncMock(return_value=rows)),
        patch("signal_engine.tracker.notifier.notify", new_callable=AsyncMock) as notify,
    ):
        await tracker._audit_duplicate_stops()
    return notify


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["cancelled", "rejected", "CANCELLED"])
async def test_a_dead_stop_is_cleared_and_the_operator_told(status):
    tracker, pos = _tracker()
    notify = await _audit(tracker, [{"orderid": "SL1", "order_status": status}])
    assert pos.sl_order_id == ""
    assert pos.sl_retry_count == 0 and pos.last_sl_retry_at is None
    notify.assert_awaited_once()
    assert "no stop" in notify.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [
    [{"orderid": "SL1", "order_status": "trigger pending"}],
    [{"orderid": "SL1", "order_status": "open"}],
    [{"orderid": "SL1", "order_status": "complete"}],  # filled: close detection owns it
    [{"orderid": "OTHER", "order_status": "cancelled"}],
    [],  # absent from the book proves nothing
])
async def test_a_working_filled_or_unlisted_stop_is_left_alone(rows):
    tracker, pos = _tracker()
    notify = await _audit(tracker, rows)
    assert pos.sl_order_id == "SL1"
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_stop_cancelled_by_a_running_exit_is_left_alone():
    tracker, pos = _tracker()
    pos.exit_pending = True
    notify = await _audit(tracker, [{"orderid": "SL1", "order_status": "cancelled"}])
    assert pos.sl_order_id == "SL1"
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_entry_confirmed_complete_records_its_fill_price():
    tracker = PositionTracker(_make_engine())
    pos = _make_position(symbol="RELIANCE", strategy=ORB, fill_price=0.0, entry_order_id="E1")
    tracker.register(pos)
    pos.fill_price = 0.0
    with (
        patch("signal_engine.tracker.fetch_order_status", AsyncMock(return_value="complete")),
        patch("signal_engine.tracker.fetch_order_fill_price", AsyncMock(return_value=2501.5)),
    ):
        out = await tracker._verify_unconfirmed_fill("k", pos, timedelta(minutes=5), _Cfg())
    assert out == _FILL_PROCEED and pos.fill_price == 2501.5


@pytest.mark.asyncio
async def test_an_unreadable_entry_fill_falls_back_to_the_entry_price():
    tracker = PositionTracker(_make_engine())
    pos = _make_position(symbol="RELIANCE", strategy=ORB, fill_price=0.0, entry_order_id="E1")
    tracker.register(pos)
    pos.fill_price = 0.0
    with (
        patch("signal_engine.tracker.fetch_order_status", AsyncMock(return_value="complete")),
        patch("signal_engine.tracker.fetch_order_fill_price", AsyncMock(return_value=None)),
    ):
        await tracker._verify_unconfirmed_fill("k", pos, timedelta(minutes=5), _Cfg())
    assert pos.fill_price == pos.entry_price


class _Cfg:
    tracker_guard2_timeout_minutes = 10


@pytest.mark.asyncio
async def test_a_stop_cancelled_by_the_time_exit_is_not_replaced():
    tracker, pos = _tracker()
    pos.time_exit_left_open = True  # the close could not be confirmed and may still land
    notify = await _audit(tracker, [{"orderid": "SL1", "order_status": "cancelled"}])
    assert pos.sl_order_id == "SL1"
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_stop_of_a_leg_the_time_exit_closed_is_not_replaced():
    tracker, pos = _tracker()
    tracker._square_off_orders = {tracker._key(pos.symbol, pos.strategy): "TX1"}
    notify = await _audit(tracker, [{"orderid": "SL1", "order_status": "cancelled"}])
    assert pos.sl_order_id == "SL1"
    notify.assert_not_awaited()
