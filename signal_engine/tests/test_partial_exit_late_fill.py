"""G3: a partial exit's order id is never "the order that closes the position".

A held partial exit that the sweeper later finds FILLED used to be filed as the position's
closing order (`sl_order_id`), so the runner shares got no stop, the quantity stayed at the old
figure, and a later empty book plus that `complete` order booked the whole position closed.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import order_reconcile as oc
from signal_engine.main import sweep_unresolved_orders
from signal_engine.models import OrderStatus, TradeResult
from signal_engine.tests.test_exit_status_verification import _exit, _row
from signal_engine.tests.test_flatness_proof import _Poll
from signal_engine.tests.test_main_characterization import harness
from signal_engine.timeutils import IST


async def _held_partial():
    """Partial exit (25 of 50) accepted, status unreadable, no proof: held. Then the sweeper finds
    the exit FILLED on the order book."""
    h, tracker, pos = await _exit(status="", own=50, exit_qty_pct=0.5)
    pending = oc.get_uncertain("ORB", "RELIANCE")
    assert pending is not None and pending.quantity == 25
    book = [
        {
            "orderid": "EX1", "symbol": "RELIANCE", "action": "SELL", "quantity": 25,
            "order_status": "complete", "strategy": "ORB",
            "timestamp": pending.since.strftime("%Y-%m-%d %H:%M:%S"),
        }
    ]
    h2 = harness(tracker=tracker, fetch_orderbook=AsyncMock(return_value=book))
    with h2.stack:
        await sweep_unresolved_orders()
    return h2, tracker, pos


@pytest.mark.asyncio
async def test_a_late_filled_partial_exit_reduces_the_quantity_and_clears_the_stop_id():
    h2, tracker, pos = await _held_partial()
    assert oc.get_uncertain("ORB", "RELIANCE") is None
    assert pos.quantity == 25
    assert pos.sl_order_id == ""  # not the partial exit's id
    assert pos.sl_retry_count == 0 and pos.last_sl_retry_at is None
    assert tracker.find_position("RELIANCE", "ORB") is pos
    h2.risk.record_close.assert_not_called()  # no P&L booked here
    texts = [c.args[0] for c in h2.notifier.notify.await_args_list]
    assert len([t for t in texts if "partial exit of 25 shares" in t]) == 1
    text = next(t for t in texts if "partial exit of 25 shares" in t)
    assert "RELIANCE [ORB]" in text and "tracks 25 remaining shares" in text
    assert "check it in the trade book" in text


@pytest.mark.asyncio
async def test_after_a_late_partial_an_empty_book_does_not_book_the_position_closed():
    _, tracker, pos = await _held_partial()
    pos.entry_time = datetime.now(IST) - timedelta(minutes=90)  # past the young-position guard
    # every order reads `complete`, as the old code needed to prove the whole position flat
    with _Poll([], status="complete") as poll:
        await tracker.check_positions()
    assert tracker.find_position("RELIANCE", "ORB") is pos
    poll.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_after_a_late_partial_the_stop_retry_places_a_stop_for_the_remaining_shares():
    _, tracker, pos = await _held_partial()
    placed = TradeResult(order_id="SLNEW", status=OrderStatus.SUCCESS, message="ok")
    with _Poll([_row(25)], status="open"), patch(
        "signal_engine.tracker.place_sl_order", new_callable=AsyncMock, return_value=placed
    ) as place:
        await tracker.check_positions()
    place.assert_awaited_once()
    assert place.await_args.kwargs["quantity"] == 25
    assert pos.sl_order_id == "SLNEW"


@pytest.mark.asyncio
async def test_a_late_filled_full_exit_is_still_filed_as_the_closing_order():
    h, tracker, pos = await _exit(status="", own=50)
    pending = oc.get_uncertain("ORB", "RELIANCE")
    book = [
        {
            "orderid": "EX1", "symbol": "RELIANCE", "action": "SELL", "quantity": 50,
            "order_status": "complete", "strategy": "ORB",
            "timestamp": pending.since.strftime("%Y-%m-%d %H:%M:%S"),
        }
    ]
    h2 = harness(tracker=tracker, fetch_orderbook=AsyncMock(return_value=book))
    with h2.stack:
        await sweep_unresolved_orders()
    assert pos.sl_order_id == "EX1"
    assert pos.quantity == 50
