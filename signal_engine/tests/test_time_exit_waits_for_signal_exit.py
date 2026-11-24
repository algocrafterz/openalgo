"""E-06: the time exit does not sell a leg that a signal exit is already closing."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import tracker as tracker_module
from signal_engine.main import _get_exit_lock, _release_exit_lock
from signal_engine.models import OrderStatus, TradeResult
from signal_engine.strategies import ORB
from signal_engine.tests.log_capture import capturing
from signal_engine.tests.tracker_fixtures import _make_engine, _make_position
from signal_engine.tracker import PositionTracker


@pytest.fixture(autouse=True)
def _fresh_exit_locks(monkeypatch):
    """Exit locks are keyed by symbol and strategy and bound to the loop that first used them."""
    monkeypatch.setattr("signal_engine.main._exit_locks", {})


def _tracker_with_leg():
    tracker = PositionTracker(_make_engine())
    pos = _make_position(symbol="RELIANCE", strategy=ORB, product="MIS")
    tracker.register(pos)
    return tracker, pos


def _ok():
    return TradeResult(order_id="TX1", status=OrderStatus.SUCCESS, message="ok")


@pytest.mark.asyncio
async def test_a_leg_closed_by_the_running_signal_exit_gets_no_second_order():
    tracker, pos = _tracker_with_leg()
    lock = _get_exit_lock("RELIANCE", ORB)
    await lock.acquire()  # the signal exit is mid-flight
    send = AsyncMock(return_value=_ok())
    with (
        patch("signal_engine.main._send_exit_with_retries", send),
        patch.object(PositionTracker, "_own_close_qty", AsyncMock(return_value=pos.quantity)),
    ):
        task = asyncio.create_task(tracker._close_strategy(ORB, {"RELIANCE"}))
        await asyncio.sleep(0.05)
        assert not task.done()  # waiting, nothing sent yet
        send.assert_not_awaited()
        tracker.unregister("RELIANCE", ORB)  # the signal exit finished and booked the close
        lock.release()
        await asyncio.wait_for(task, 5)
    send.assert_not_awaited()
    _release_exit_lock("RELIANCE", ORB)


@pytest.mark.asyncio
async def test_a_leg_still_open_after_the_signal_exit_is_closed_once():
    tracker, pos = _tracker_with_leg()
    lock = _get_exit_lock("RELIANCE", ORB)
    await lock.acquire()
    send = AsyncMock(return_value=_ok())
    with (
        patch("signal_engine.main._send_exit_with_retries", send),
        patch.object(PositionTracker, "_own_close_qty", AsyncMock(return_value=pos.quantity)),
    ):
        task = asyncio.create_task(tracker._close_strategy(ORB, {"RELIANCE"}))
        await asyncio.sleep(0.05)
        send.assert_not_awaited()
        lock.release()  # the signal exit gave up without closing it
        await asyncio.wait_for(task, 5)
    send.assert_awaited_once()
    assert not lock.locked()  # the time exit gives the lock back


@pytest.mark.asyncio
async def test_no_order_is_sent_when_the_signal_exit_outlasts_the_wait():
    tracker, pos = _tracker_with_leg()
    lock = _get_exit_lock("RELIANCE", ORB)
    await lock.acquire()
    send = AsyncMock(return_value=_ok())
    with (
        patch("signal_engine.main._send_exit_with_retries", send),
        patch.object(tracker_module, "_EXIT_LOCK_WAIT_SECONDS", 0.05),
        capturing() as logs,
    ):
        await tracker._close_strategy(ORB, {"RELIANCE"})
    lock.release()
    send.assert_not_awaited()
    # The trader is no longer messaged: the skipped close is a critical log line naming the leg.
    assert logs.has("RELIANCE", "CRITICAL")
    assert logs.has("an exit is still in progress", "CRITICAL")
    assert logs.has("no second order sent", "CRITICAL")
    assert tracker.find_position("RELIANCE", ORB) is pos  # stays tracked, poll books it
