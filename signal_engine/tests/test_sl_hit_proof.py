"""E-05: a chart "SL HIT" alert is not proof that the position is flat.

_reconcile_sl_hit() cancelled the tracked stop, booked a close and unregistered, never reading the
broker. A chart alert that fires while the broker still holds the position (a late or wrong alert)
left the position held with no stop and nothing watching it.
"""

from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import main as main_module
from signal_engine.main import _handle_exit
from signal_engine.tests.test_main_characterization import (
    _exit_signal,
    _position,
    _real_tracker,
    harness,
)


def _alerts(h):
    return [c for c in h.notifier.notify_event.await_args_list if c.args[0] == "sl_hit_unproven"]


@pytest.fixture(autouse=True)
def _fresh_alert_state():
    main_module._sl_hit_unproven_alerted.clear()
    yield
    main_module._sl_hit_unproven_alerted.clear()


def _book_row(quantity, product="MIS"):
    return {"symbol": "RELIANCE", "exchange": "NSE", "product": product, "quantity": quantity, "ltp": 2490.0}


async def _run(stop_status, own, *, sl_order_id="SL1", book=None):
    """`own` is what OpenAlgo's openposition answers; `book` is the position book (None = unreadable).

    openposition answers 0 for a missing row and for a failed Flattrade read, so it is never proof.
    """
    tracker = _real_tracker()
    pos = _position(sl_order_id=sl_order_id)
    tracker.register(pos)
    tracker._last_realised_pnl = 0.0
    tracker.fetch_own_position = AsyncMock(return_value=own)
    h = harness(
        tracker=tracker,
        fetch_order_status=AsyncMock(return_value=stop_status),
        fetch_realised_pnl=AsyncMock(return_value=-750.0),
        fetch_order_fill_price=AsyncMock(return_value=2485.0),
    )
    with h.stack, patch(
        "signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=book
    ):
        await _handle_exit(_exit_signal(tp_level="SL"))
    return h, tracker, pos


@pytest.mark.asyncio
async def test_sl_hit_while_the_broker_holds_keeps_the_stop_and_the_position():
    h, tracker, pos = await _run("open", own=50)
    h.cancel_order.assert_not_awaited()
    h.send_order.assert_not_awaited()
    assert tracker.find_position("RELIANCE", "ORB") is pos
    assert pos.exit_pending is False
    h.risk.record_close.assert_not_called()
    assert len(tracker._completed_trades) == 0
    alerts = _alerts(h)
    assert len(alerts) == 1
    text = alerts[0].args[1]
    assert "RELIANCE [ORB]" in text
    assert "broker still shows the position open" in text
    assert "Nothing was cancelled or closed" in text


@pytest.mark.asyncio
async def test_sl_hit_with_no_stop_id_while_the_broker_holds_is_kept_too():
    h, tracker, pos = await _run("", own=50, sl_order_id="")
    h.cancel_order.assert_not_awaited()
    h.send_order.assert_not_awaited()
    assert tracker.find_position("RELIANCE", "ORB") is pos
    assert pos.exit_pending is False
    assert len(_alerts(h)) == 1


@pytest.mark.asyncio
async def test_sl_hit_with_an_executed_stop_is_booked_as_before():
    h, tracker, _ = await _run("complete", own=50)
    h.send_order.assert_not_awaited()
    assert tracker.find_position("RELIANCE", "ORB") is None
    h.risk.record_close.assert_called_once_with(pnl=-750.0, strategy="ORB", symbol="RELIANCE")
    assert _alerts(h) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("book", [[], [_book_row(0)], None])
async def test_a_working_stop_means_the_stop_did_not_fire_whatever_openposition_says(book):
    """Stop `open` + openposition 0 used to be booked as flat: the stop was cancelled and the
    position (still held when openposition's 0 was a missing row or a failed read) went naked."""
    h, tracker, pos = await _run("open", own=0, book=book)
    h.cancel_order.assert_not_awaited()
    h.send_order.assert_not_awaited()
    assert tracker.find_position("RELIANCE", "ORB") is pos
    assert pos.exit_pending is False
    h.risk.record_close.assert_not_called()
    assert len(_alerts(h)) == 1


@pytest.mark.asyncio
async def test_a_cancelled_stop_and_a_book_row_at_zero_is_booked():
    h, tracker, _ = await _run("cancelled", own=0, book=[_book_row(0)])
    h.send_order.assert_not_awaited()
    assert tracker.find_position("RELIANCE", "ORB") is None
    h.risk.record_close.assert_called_once()
    assert _alerts(h) == []


@pytest.mark.asyncio
async def test_a_cancelled_stop_and_an_empty_book_is_kept_although_openposition_says_zero():
    h, tracker, pos = await _run("cancelled", own=0, book=[])
    h.cancel_order.assert_not_awaited()
    assert tracker.find_position("RELIANCE", "ORB") is pos
    h.risk.record_close.assert_not_called()
    assert len(_alerts(h)) == 1


@pytest.mark.asyncio
async def test_an_unreadable_stop_and_no_book_row_is_kept():
    h, tracker, pos = await _run("", own=0, book=[_book_row(0, product="CNC")])
    h.cancel_order.assert_not_awaited()
    assert tracker.find_position("RELIANCE", "ORB") is pos
    assert pos.exit_pending is False
    assert len(_alerts(h)) == 1


@pytest.mark.asyncio
async def test_an_unreadable_stop_and_a_book_row_at_zero_is_booked():
    h, tracker, _ = await _run("", own=0, book=[_book_row(0)])
    h.send_order.assert_not_awaited()
    assert tracker.find_position("RELIANCE", "ORB") is None
    h.risk.record_close.assert_called_once()
    assert _alerts(h) == []


@pytest.mark.asyncio
async def test_an_unreadable_stop_and_an_unreadable_book_is_kept():
    h, tracker, pos = await _run("", own=None, book=None)
    h.cancel_order.assert_not_awaited()
    assert tracker.find_position("RELIANCE", "ORB") is pos
    assert pos.exit_pending is False
    assert len(_alerts(h)) == 1


@pytest.mark.asyncio
async def test_the_unproven_alert_is_sent_once_per_ten_minutes():
    tracker = _real_tracker()
    tracker.register(_position())
    tracker.fetch_own_position = AsyncMock(return_value=50)
    h = harness(tracker=tracker, fetch_order_status=AsyncMock(return_value="open"))
    with h.stack:
        await _handle_exit(_exit_signal(tp_level="SL"))
        await _handle_exit(_exit_signal(tp_level="SL"))
        assert len(_alerts(h)) == 1
        key = next(iter(main_module._sl_hit_unproven_alerted))
        main_module._sl_hit_unproven_alerted[key] -= 601.0  # ten minutes later
        await _handle_exit(_exit_signal(tp_level="SL"))
    assert len(_alerts(h)) == 2


@pytest.mark.asyncio
async def test_a_stop_that_filled_during_the_exit_cancel_still_books_without_a_second_read():
    """The exit path that has just read own == 0 passes the proof on: no extra position read."""
    tracker = _real_tracker()
    pos = _position()
    tracker.register(pos)
    tracker._last_realised_pnl = 0.0
    pos.stop_filled_in_cancel = False
    h = harness(
        tracker=tracker,
        fetch_order_status=AsyncMock(return_value="open"),
        fetch_realised_pnl=AsyncMock(return_value=-750.0),
        fetch_order_fill_price=AsyncMock(return_value=2485.0),
    )
    calls = []

    async def own(*a, **k):
        calls.append(1)
        return 0

    tracker.fetch_own_position = own

    async def cancel_marks_filled(pos_):
        pos_.stop_filled_in_cancel = True
        return True

    h.stack.enter_context(patch("signal_engine.main._cancel_sl_before_exit", cancel_marks_filled))
    with h.stack:
        await _handle_exit(_exit_signal(tp_level="TP1"))
    assert calls == [1]  # read once by the exit path, not again by the reconcile
    assert tracker.find_position("RELIANCE", "ORB") is None
    h.send_order.assert_not_awaited()
