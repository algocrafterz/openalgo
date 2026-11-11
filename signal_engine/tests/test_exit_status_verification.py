"""E-03: an accepted exit order is not a filled exit order.

After send_order returned an order id, a fill price of None meant "rejected", "cancelled", "not
complete yet" and "unreadable" alike, and the close was booked from the portfolio realised delta
and the position unregistered (or its quantity reduced) while the broker still held it.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import order_reconcile as oc
from signal_engine.main import _handle_exit
from signal_engine.tests.test_main_characterization import (
    _exit_signal,
    _position,
    _real_tracker,
    harness,
)
from signal_engine.tracker import PositionTracker


async def _exit(*, status, own, tp_level="TP1", exit_qty_pct=None, fill=None, orderbook=None, book="derive"):
    """Run one exit whose order is accepted (id EX1) with no readable fill price.

    `own` is what openposition answers. `book` is the position book (None = unreadable); by
    default it is the one consistent with `own` (a row holding `own`, none when own is None).
    """
    if book == "derive":
        book = None if own is None else [
            {"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "quantity": own, "ltp": 2490.0}
        ]
    tracker = _real_tracker()
    pos = _position()
    tracker.register(pos)
    tracker._last_realised_pnl = 0.0
    tracker.fetch_own_position = AsyncMock(return_value=own)
    stubs = {
        "fetch_order_status": AsyncMock(return_value=status),
        "fetch_order_fill_price": AsyncMock(return_value=fill),
        "fetch_realised_pnl": AsyncMock(return_value=-750.0),
    }
    if orderbook is not None:
        stubs["fetch_orderbook"] = AsyncMock(return_value=orderbook)
    h = harness(tracker=tracker, **stubs)
    extra = {} if exit_qty_pct is None else {"exit_qty_pct": exit_qty_pct}
    with h.stack, patch(
        "signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=book
    ):
        await _handle_exit(_exit_signal(tp_level=tp_level, **extra))
    return h, tracker, pos


def _exit_failed_alerts(h):
    return h.notifier.notify_exit_failed.await_args_list


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["rejected", "cancelled", "REJECTED"])
async def test_full_exit_rejected_after_acceptance_books_nothing(status):
    h, tracker, pos = await _exit(status=status, own=50)
    assert tracker.find_position("RELIANCE", "ORB") is pos
    assert pos.quantity == 50
    assert pos.exit_pending is False
    h.risk.record_close.assert_not_called()
    assert len(tracker._completed_trades) == 0
    assert pos.sl_order_id == ""  # the cancelled stop's id is cleared so the poll re-protects
    assert pos.sl_order_id != "EX1"  # and the dead exit id is not filed as the closing order
    assert len(_exit_failed_alerts(h)) == 1
    assert order_reconcile_get() is None  # a refused exit is not held: the stop may come back


def order_reconcile_get():
    return oc.get_uncertain("ORB", "RELIANCE")


@pytest.mark.asyncio
async def test_partial_exit_rejected_after_acceptance_changes_no_quantity_and_records_no_pnl():
    h, tracker, pos = await _exit(status="rejected", own=50, exit_qty_pct=0.5)
    assert pos.quantity == 50
    assert tracker.find_position("RELIANCE", "ORB") is pos
    assert pos.realized_pnl == 0.0
    assert pos.exit_types == []
    h.risk.record_close.assert_not_called()
    h.notifier.notify_partial_exit.assert_not_awaited()
    assert len(_exit_failed_alerts(h)) == 1


@pytest.mark.asyncio
async def test_status_complete_with_no_price_is_booked_as_before():
    h, tracker, _ = await _exit(status="complete", own=50)
    assert tracker.find_position("RELIANCE", "ORB") is None
    h.risk.record_close.assert_called_once_with(pnl=-750.0, strategy="ORB", symbol="RELIANCE")
    assert order_reconcile_get() is None


@pytest.mark.asyncio
async def test_status_open_and_a_book_row_at_zero_is_booked():
    h, tracker, _ = await _exit(status="open", own=0, book=[_row(0)])
    assert tracker.find_position("RELIANCE", "ORB") is None
    h.risk.record_close.assert_called_once()


def _row(quantity):
    return {"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "quantity": quantity, "ltp": 2490.0}


@pytest.mark.asyncio
@pytest.mark.parametrize("book", [[], [dict(_row(0), product="CNC")], None])
async def test_status_open_and_an_empty_book_is_held_not_booked_although_openposition_says_zero(book):
    """openposition answers 0 for a missing row and for a failed read: not proof of a fill."""
    h, tracker, pos = await _exit(status="open", own=0, book=book)
    assert tracker.find_position("RELIANCE", "ORB") is pos
    assert pos.quantity == 50
    h.risk.record_close.assert_not_called()
    assert order_reconcile_get() is not None


@pytest.mark.asyncio
async def test_short_partial_exit_is_accepted_on_the_signed_remainder():
    from signal_engine.models import Direction

    h, tracker, pos = await _exit_short(book=[_row(-25)])
    assert pos.quantity == 25
    h.notifier.notify_partial_exit.assert_awaited_once()
    assert Direction.SHORT == pos.direction


@pytest.mark.asyncio
async def test_a_reversed_position_is_not_accepted_as_the_partial_remainder():
    """A long of 25 where a short of 25 was expected has the right size and the wrong side."""
    h, tracker, pos = await _exit_short(book=[_row(25)])
    assert pos.quantity == 50
    h.notifier.notify_partial_exit.assert_not_awaited()
    assert order_reconcile_get() is not None


async def _exit_short(*, book):
    from signal_engine.models import Direction

    tracker = _real_tracker()
    pos = _position(direction=Direction.SHORT, sl=2515.0, tp=2460.0)
    tracker.register(pos)
    tracker._last_realised_pnl = 0.0
    h = harness(
        tracker=tracker,
        fetch_order_status=AsyncMock(return_value=""),
        fetch_order_fill_price=AsyncMock(return_value=None),
        fetch_realised_pnl=AsyncMock(return_value=750.0),
    )
    with h.stack, patch(
        "signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=book
    ):
        await _handle_exit(_exit_signal(tp_level="TP1", exit_qty_pct=0.5))
    return h, tracker, pos


@pytest.mark.asyncio
async def test_partial_exit_status_unreadable_and_broker_shows_the_remainder_is_booked():
    h, tracker, pos = await _exit(status="", own=25, exit_qty_pct=0.5)
    assert pos.quantity == 25
    h.notifier.notify_partial_exit.assert_awaited_once()


@pytest.mark.asyncio
async def test_status_unreadable_and_broker_still_holds_is_held_uncertain():
    h, tracker, pos = await _exit(status="", own=50)
    assert tracker.find_position("RELIANCE", "ORB") is pos
    assert pos.quantity == 50
    assert pos.exit_pending is False
    h.risk.record_close.assert_not_called()
    assert len(tracker._completed_trades) == 0
    pending = order_reconcile_get()
    assert pending is not None
    assert (pending.kind, pending.action, pending.quantity) == ("EXIT", "SELL", 50)
    h.place_sl_order.assert_not_awaited()  # no stop is placed beside an exit that may still land
    assert pos.sl_order_id != "EX1"
    # the operator is told the order is still working and must not be doubled by hand
    texts = [c.args[0] for c in h.notifier.notify.await_args_list]
    assert any("Do not exit by hand" in t for t in texts)
    assert not any("exit or place a stop by hand" in t for t in texts)
    assert not any("ORDER UNCONFIRMED" in t for t in texts)
    assert any("stop-loss was cancelled for this exit" in t for t in texts)
    assert _exit_failed_alerts(h) == []


@pytest.mark.asyncio
async def test_the_accepted_exit_alert_names_the_stop_only_when_it_was_cancelled():
    from datetime import datetime

    from signal_engine.main import _hold_accepted_exit
    from signal_engine.models import OrderStatus, TradeResult
    from signal_engine.timeutils import IST

    tracker = _real_tracker()
    pos = _position()
    tracker.register(pos)
    order = SimpleNamespace(action=SimpleNamespace(value="SELL"), quantity=50)
    accepted = TradeResult(order_id="EX1", status=OrderStatus.SUCCESS, message="ok")
    texts = []
    for cancelled in (False, True):
        h = harness(tracker=tracker)
        with h.stack:
            await _hold_accepted_exit(
                pos, 50, True, "EXIT", accepted, order, datetime.now(IST), "status open",
                sl_cancelled=cancelled,
            )
        texts.append(h.notifier.notify.await_args_list[0].args[0])
    assert "RELIANCE [ORB]: the exit order EX1 was accepted" in texts[0]
    assert "Do not exit by hand while that order is still working" in texts[0]
    assert "stop-loss was cancelled" not in texts[0]
    assert "The position's stop-loss was cancelled for this exit." in texts[1]


@pytest.mark.asyncio
async def test_status_open_and_broker_position_unreadable_is_held_uncertain():
    h, tracker, pos = await _exit(status="open", own=None)
    assert tracker.find_position("RELIANCE", "ORB") is pos
    assert order_reconcile_get() is not None
    h.risk.record_close.assert_not_called()


@pytest.mark.asyncio
async def test_a_partial_exit_that_the_broker_does_not_show_is_held_not_booked():
    h, tracker, pos = await _exit(status="open", own=50, exit_qty_pct=0.5)
    assert pos.quantity == 50
    assert order_reconcile_get() is not None
    h.notifier.notify_partial_exit.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_known_fill_price_skips_the_status_read_and_books_as_before():
    h, tracker, _ = await _exit(status="rejected", own=50, fill=2485.0)
    h.fetch_order_status.assert_not_awaited()
    assert tracker.find_position("RELIANCE", "ORB") is None


@pytest.mark.asyncio
async def test_a_held_exit_is_settled_by_the_existing_sweeper_when_the_book_shows_it_complete():
    from signal_engine.main import sweep_unresolved_orders

    h, tracker, pos = await _exit(status="", own=50)
    pending = order_reconcile_get()
    assert pending is not None
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
    assert order_reconcile_get() is None
    assert pos.sl_order_id == "EX1"  # the poll now books the close under the exit's own id


# --- the no-progress market exit -----------------------------------------------------------


def _np_tracker():
    from signal_engine.tests.tracker_fixtures import _make_engine, _make_position

    engine = _make_engine()
    engine._state("ORB").open_positions = 1
    tracker = PositionTracker(engine)
    tracker._last_realised_pnl = 0.0
    pos = _make_position(fill_price=2500.0)
    pos.sl_order_id = "SL_OLD"
    tracker.register(pos)
    return tracker, pos


async def _np_exit(status):
    from datetime import timedelta

    from signal_engine.models import OrderStatus, TradeResult

    tracker, pos = _np_tracker()
    ok = TradeResult(order_id="EXNP", status=OrderStatus.SUCCESS, message="ok")
    stop = TradeResult(order_id="SL_NEW", status=OrderStatus.SUCCESS, message="ok")
    with (
        patch("signal_engine.main._send_exit_with_retries", new_callable=AsyncMock, return_value=ok),
        patch("signal_engine.tracker.fetch_order_fill_price", new_callable=AsyncMock, return_value=None),
        patch("signal_engine.tracker.fetch_order_status", new_callable=AsyncMock, return_value=status),
        patch("signal_engine.tracker.place_sl_order", new_callable=AsyncMock, return_value=stop) as place,
        patch("signal_engine.tracker.notifier") as notifier,
    ):
        notifier.notify_sl_failed = AsyncMock()
        notifier.notify_no_progress_exit = AsyncMock()
        await tracker._no_progress_market_exit(pos, timedelta(minutes=40), 2490.0, 2500.0, 0.1)
    return tracker, pos, place, notifier


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["rejected", "cancelled"])
async def test_no_progress_exit_rejected_after_acceptance_restores_the_stop(status):
    tracker, pos, place, notifier = await _np_exit(status)
    place.assert_awaited_once()
    assert pos.sl_order_id == "SL_NEW"  # the dead exit id is not left in place
    assert tracker.find_position("RELIANCE", "ORB") is pos
    notifier.notify_sl_failed.assert_awaited_once()
    assert "Stop restored" in notifier.notify_sl_failed.await_args.args[1]
    notifier.notify_no_progress_exit.assert_not_awaited()  # nothing was exited


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["open", "", "complete"])
async def test_no_progress_exit_with_any_other_status_is_as_today(status):
    tracker, pos, place, notifier = await _np_exit(status)
    place.assert_not_awaited()
    assert pos.sl_order_id == "EXNP"  # left for the poll to book, as before
    notifier.notify_no_progress_exit.assert_awaited_once()
