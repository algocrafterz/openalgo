"""G1c / G1d: openposition's 0 is not proof of flatness at the two remaining decision sites.

openposition answers 0 for a missing row and for a failed Flattrade PositionBook read. A failed
exit order (c) or an exit the order book shows absent (d) with such a 0 must keep the position
protected and logged; only a position-book ROW at 0 may leave the close to the poll.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import order_reconcile as oc
from signal_engine.main import _handle_exit, sweep_unresolved_orders
from signal_engine.tests.test_exit_timeout_safety import _mark_exit, _tracked
from signal_engine.tests.test_main_characterization import (
    _exit_signal,
    _fail,
    _position,
    _real_tracker,
    _settings_stub,
    harness,
)
from signal_engine.timeutils import IST


def _row(quantity):
    return {"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "quantity": quantity, "ltp": 2490.0}


async def _failed_exit(book):
    tracker = _real_tracker()
    pos = _position()
    tracker.register(pos)
    h = harness(
        tracker=tracker,
        settings=_settings_stub(bracket_tp_exit_retries=2, bracket_retry_delay=0.0),
        send_order=AsyncMock(return_value=_fail()),
        fetch_open_position=AsyncMock(return_value=0),
    )
    with h.stack, patch(
        "signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=book
    ):
        await _handle_exit(_exit_signal(tp_level="TP1"))
    return h, tracker, pos


@pytest.mark.asyncio
@pytest.mark.parametrize("book", [[], None, [dict(_row(0), product="CNC")]])
async def test_failed_exit_with_a_false_zero_is_logged_and_reprotected(book, logged):
    h, tracker, pos = await _failed_exit(book)
    assert logged.count("after its stop was cancelled: re-protecting", "CRITICAL") == 1
    assert tracker.find_position("RELIANCE", "ORB") is pos
    assert pos.exit_pending is False
    assert pos.sl_order_id == ""  # the dead stop id is cleared so the poll places a new stop


@pytest.mark.asyncio
async def test_failed_exit_with_a_book_row_at_zero_is_left_for_the_poll(logged):
    h, tracker, pos = await _failed_exit([_row(0)])
    assert not logged.has("re-protecting", "CRITICAL")
    assert tracker.find_position("RELIANCE", "ORB") is pos
    h.risk.record_close.assert_not_called()


async def _sweep(proven):
    tracker = MagicMock()
    pos = _tracked()
    tracker.find_position = MagicMock(return_value=pos)
    tracker.fetch_own_position = AsyncMock(return_value=0)  # openposition: 0
    tracker.fetch_own_position_proven = AsyncMock(return_value=proven)
    tracker.book_close = AsyncMock()
    from dataclasses import replace
    from datetime import datetime

    _mark_exit()
    pending = oc.get_uncertain("BREAKOUT-PY", "LAURUSLABS")
    oc.mark_uncertain(replace(pending, watch_until=datetime(2000, 1, 1, tzinfo=IST)))
    with (
        patch("signal_engine.main.fetch_orderbook", AsyncMock(side_effect=[[]])),
        patch("signal_engine.main.fetch_order_status", AsyncMock(return_value="canceled")),
        patch("signal_engine.main.tracker", tracker),
    ):
        await sweep_unresolved_orders()
    return pos


@pytest.mark.asyncio
async def test_absent_exit_with_a_false_zero_keeps_the_hold_and_logs_without_a_stop(logged):
    pos = await _sweep(proven=None)
    assert pos.sl_order_id == "SL-1"  # no stop placed, none cleared
    assert oc.get_uncertain("BREAKOUT-PY", "LAURUSLABS") is not None
    assert "Stop NOT restored" in logged.text("CRITICAL")


@pytest.mark.asyncio
async def test_absent_exit_with_a_book_row_at_zero_releases_the_hold_as_before(logged):
    pos = await _sweep(proven=0)
    assert pos.sl_order_id == "SL-1"
    assert oc.get_uncertain("BREAKOUT-PY", "LAURUSLABS") is None
    assert not logged.has("Stop NOT restored", "CRITICAL")
