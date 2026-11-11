"""E-02 / E-04: the tracker poll books a close only on proof of flatness.

A position-book row that is absent is not proof: Flattrade maps a failed read to an empty book
and the sandbox drops a row closed at exactly zero P&L. Proof is a matching row with quantity 0,
or the closing order (the tracked stop) reporting `complete`. Anything else keeps the position
and its stop, and tells the operator if it lasts.

E-04: after an OpenAlgo mode change the poll stands down; every read would be of the other
mode's book.
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import mode_guard
from signal_engine.strategies import ORB
from signal_engine.tests.tracker_fixtures import _make_engine, _make_position
from signal_engine.tracker import PositionTracker


def _row(symbol="RELIANCE", exchange="NSE", product="MIS", quantity=50, **extra):
    row = {
        "symbol": symbol,
        "exchange": exchange,
        "product": product,
        "quantity": quantity,
        "pnl": 0,
        "ltp": 2500.0,
    }
    row.update(extra)
    return row


def _tracker(*positions):
    engine = _make_engine()
    engine._state(ORB).open_positions = len(positions)
    tracker = PositionTracker(engine)
    tracker._last_realised_pnl = 0.0
    for pos in positions:
        tracker.register(pos)
    return tracker


def _stopped(symbol="RELIANCE", sl_id="SL1", **kw):
    kw.setdefault("fill_price", 2500.0)  # fill confirmed: a close is not mistaken for an orphan
    pos = _make_position(symbol=symbol, **kw)
    pos.sl_order_id = sl_id
    return pos


class _Poll:
    """Patches the broker reads for a run of check_positions() and records the side effects."""

    def __init__(self, book, status="open"):
        self.book = book
        self.status = status
        self.stack = ExitStack()

    def __enter__(self):
        s = self.stack
        self.pb = s.enter_context(
            patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=self.book)
        )
        self.order_status = s.enter_context(
            patch("signal_engine.tracker.fetch_order_status", new_callable=AsyncMock, return_value=self.status)
        )
        s.enter_context(
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=-200.0)
        )
        self.cancel = s.enter_context(
            patch("signal_engine.tracker.cancel_order", new_callable=AsyncMock, return_value=True)
        )
        self.notify = s.enter_context(
            patch("signal_engine.tracker.notifier.notify_event", new_callable=AsyncMock)
        )
        s.enter_context(
            patch("signal_engine.tracker.notifier.notify_orphaned_position", new_callable=AsyncMock)
        )
        return self

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)

    def alerts(self):
        return [c for c in self.notify.await_args_list if c.args[0] == "position_absent_unproven"]


# --- E-02: absent row ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_book_with_two_tracked_positions_books_nothing():
    tracker = _tracker(_stopped("RELIANCE", "SL1"), _stopped("INFY", "SL2"))
    with _Poll([], status="open") as poll:
        await tracker.check_positions()
    assert tracker.tracked_count == 2
    poll.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_absent_row_with_unreadable_stop_status_is_unknown():
    tracker = _tracker(_stopped())
    with _Poll([], status="") as poll:
        await tracker.check_positions()
    assert tracker.tracked_count == 1
    poll.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_absent_row_without_a_stop_id_is_unknown():
    tracker = _tracker(_stopped(sl_id=""))
    with _Poll([], status="complete") as poll:
        await tracker.check_positions()
    assert tracker.tracked_count == 1
    poll.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_leaves_the_position_free_for_a_signal_exit():
    pos = _stopped()
    tracker = _tracker(pos)
    with _Poll([], status="open"):
        await tracker.check_positions()
    assert pos.exit_pending is False


@pytest.mark.asyncio
async def test_absent_row_with_stop_complete_is_booked():
    tracker = _tracker(_stopped())
    with _Poll([], status="complete") as poll:
        await tracker.check_positions()
    assert tracker.tracked_count == 0
    poll.cancel.assert_not_awaited()  # the stop filled; nothing to cancel


@pytest.mark.asyncio
@pytest.mark.parametrize("cnc_first", [True, False])
async def test_other_product_row_does_not_close_the_mis_position(cnc_first):
    cnc = _row(product="CNC", quantity=0)
    mis = _row(product="MIS", quantity=50)
    book = [cnc, mis] if cnc_first else [mis, cnc]
    tracker = _tracker(_stopped())
    with _Poll(book, status="open") as poll:
        await tracker.check_positions()
    assert tracker.tracked_count == 1
    poll.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_matching_row_with_quantity_zero_is_booked_as_before():
    tracker = _tracker(_stopped())
    with _Poll([_row(quantity=0)], status="open") as poll:
        await tracker.check_positions()
    assert tracker.tracked_count == 0
    poll.cancel.assert_awaited_once_with("SL1", ORB)  # unchanged: resting stop is cancelled


@pytest.mark.asyncio
async def test_only_a_row_of_another_product_means_absent():
    # A CNC row at qty 0 is not this MIS position's row.
    tracker = _tracker(_stopped())
    with _Poll([_row(product="CNC", quantity=0)], status="open") as poll:
        await tracker.check_positions()
    assert tracker.tracked_count == 1
    poll.cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_book_rows_without_exchange_and_product_still_match():
    tracker = _tracker(_stopped())
    with _Poll([{"symbol": "RELIANCE", "quantity": 0, "pnl": 0}], status="open") as poll:
        await tracker.check_positions()
    assert tracker.tracked_count == 0
    poll.cancel.assert_awaited_once()


@pytest.mark.asyncio
async def test_six_absent_polls_send_exactly_one_alert():
    tracker = _tracker(_stopped())
    with _Poll([], status="open") as poll:
        for _ in range(5):
            await tracker.check_positions()
        assert poll.alerts() == []
        for _ in range(4):
            await tracker.check_positions()
    alerts = poll.alerts()
    assert len(alerts) == 1  # the 6th poll alerts; the 7th-9th stay inside the 10 minutes
    text = alerts[0].args[1]
    assert "RELIANCE" in text and ORB in text
    assert "not in the position book" in text
    assert "its closing order has not executed" in text
    assert "The engine is keeping the position." in text
    assert tracker.tracked_count == 1


@pytest.mark.asyncio
async def test_absent_row_alert_without_a_stop_id_says_there_is_no_stop():
    tracker = _tracker(_stopped(sl_id=""))
    with _Poll([], status="open") as poll:
        for _ in range(6):
            await tracker.check_positions()
    text = poll.alerts()[0].args[1]
    assert "has no stop-loss order on record" in text
    assert "protect the position by hand if it is still open" in text
    assert "closing order has not executed" not in text


@pytest.mark.asyncio
async def test_alert_repeats_after_ten_minutes():
    tracker = _tracker(_stopped())
    with _Poll([], status="open") as poll, patch("signal_engine.tracker.time.monotonic") as clock:
        clock.return_value = 1000.0
        for _ in range(6):
            await tracker.check_positions()
        clock.return_value = 1000.0 + 601.0
        await tracker.check_positions()
    assert len(poll.alerts()) == 2


@pytest.mark.asyncio
async def test_reappearing_row_resets_the_absent_count():
    tracker = _tracker(_stopped())
    with _Poll([], status="open") as poll:
        for _ in range(5):
            await tracker.check_positions()
        poll.pb.return_value = [_row()]
        await tracker.check_positions()
        poll.pb.return_value = []
        for _ in range(5):
            await tracker.check_positions()
    assert poll.alerts() == []


@pytest.mark.asyncio
async def test_rejected_entry_with_unknown_fill_is_still_released_as_orphan():
    pos = _make_position(fill_price=0.0, entry_order_id="ENTRY_REJ")
    pos.sl_order_id = "SL_ORPHAN"
    tracker = _tracker(pos)
    with _Poll([], status="rejected") as poll:
        await tracker.check_positions()
    assert tracker.tracked_count == 0
    poll.cancel.assert_awaited_once_with("SL_ORPHAN", ORB)


# --- G2: an absent row needs the entry order's own verdict to release an orphan ---------------


def _stuck_entry(sl_id="SL_ORPHAN"):
    from datetime import datetime, timedelta

    from signal_engine.timeutils import IST

    pos = _make_position(fill_price=0.0, entry_order_id="ENTRY1")
    pos.sl_order_id = sl_id
    pos.entry_time = datetime.now(IST) - timedelta(minutes=90)  # past the guard-2 timeout
    return pos


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["", "open", "pending"])
async def test_absent_row_and_unreadable_entry_status_past_the_timeout_is_kept(status):
    pos = _stuck_entry()
    tracker = _tracker(pos)
    with _Poll([], status=status) as poll:
        await tracker.check_positions()
    assert tracker.tracked_count == 1
    poll.cancel.assert_not_awaited()
    assert pos.absent_polls == 1
    assert pos.exit_pending is False


@pytest.mark.asyncio
async def test_absent_row_and_unreadable_entry_status_counts_toward_the_alert():
    pos = _stuck_entry()
    tracker = _tracker(pos)
    with _Poll([], status="") as poll:
        for _ in range(6):
            await tracker.check_positions()
    assert len(poll.alerts()) == 1
    assert "not in the position book" in poll.alerts()[0].args[1]
    assert tracker.tracked_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["rejected", "cancelled", "cancel"])
async def test_absent_row_and_a_refused_entry_is_released_as_an_orphan(status):
    pos = _stuck_entry()
    tracker = _tracker(pos)
    with _Poll([], status=status) as poll:
        await tracker.check_positions()
    assert tracker.tracked_count == 0
    poll.cancel.assert_awaited_once_with("SL_ORPHAN", ORB)


@pytest.mark.asyncio
async def test_a_row_at_zero_and_a_stuck_entry_is_released_as_before():
    pos = _stuck_entry()
    tracker = _tracker(pos)
    with _Poll([_row(quantity=0)], status="") as poll:
        await tracker.check_positions()
    assert tracker.tracked_count == 0
    poll.cancel.assert_awaited_once_with("SL_ORPHAN", ORB)


# --- E-04: mode change stand-down ------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_stands_down_after_a_mode_change():
    tracker = _tracker(_stopped())
    # The other mode's book shows the position at qty 0, which would book a phantom close.
    with _Poll([_row(quantity=0)], status="complete") as poll, patch.object(
        mode_guard, "mode_halted", return_value=True
    ), patch.object(mode_guard, "check_for_flip", new_callable=AsyncMock, return_value=None):
        await tracker.check_positions()
    poll.pb.assert_not_awaited()
    poll.cancel.assert_not_awaited()
    assert tracker.tracked_count == 1


@pytest.mark.asyncio
async def test_stand_down_runs_no_sweep_no_stop_reconcile_no_missing_stop_retry():
    pos = _stopped(sl_id="")
    tracker = _tracker(pos)
    tracker._reconcile_stops = AsyncMock()
    tracker._retry_missing_sl = AsyncMock()
    with _Poll([_row()], status="open") as poll, patch.object(
        mode_guard, "mode_halted", return_value=True
    ), patch.object(mode_guard, "check_for_flip", new_callable=AsyncMock, return_value=None), patch(
        "signal_engine.main.sweep_unresolved_orders", new_callable=AsyncMock
    ) as sweep:
        await tracker.check_positions()
    sweep.assert_not_awaited()
    tracker._reconcile_stops.assert_not_awaited()
    tracker._retry_missing_sl.assert_not_awaited()
    poll.pb.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_flip_is_noticed_and_alerted_on_the_poll_that_sees_it():
    tracker = _tracker(_stopped())
    flipped = {"v": False}

    async def flip():
        flipped["v"] = True
        return "live"

    with _Poll([_row(quantity=0)], status="complete") as poll, patch.object(
        mode_guard, "mode_halted", side_effect=lambda: flipped["v"]
    ), patch.object(mode_guard, "check_for_flip", side_effect=flip), patch.object(
        mode_guard, "halt_reason", return_value="mode changed"
    ):
        await tracker.check_positions()
    assert any(c.args[0] == "mode_flip_halt" for c in poll.notify.await_args_list)
    poll.pb.assert_not_awaited()
    assert tracker.tracked_count == 1


@pytest.mark.asyncio
async def test_stand_down_critical_log_is_rate_limited():
    tracker = _tracker(_stopped())
    with _Poll([], status="open"), patch.object(mode_guard, "mode_halted", return_value=True), patch.object(
        mode_guard, "check_for_flip", new_callable=AsyncMock, return_value=None
    ), patch("signal_engine.tracker.logger") as log, patch("signal_engine.tracker.time.monotonic") as clock:
        clock.return_value = 5000.0
        await tracker.check_positions()
        await tracker.check_positions()
        clock.return_value = 5000.0 + 301.0
        await tracker.check_positions()
    assert log.critical.call_count == 2
