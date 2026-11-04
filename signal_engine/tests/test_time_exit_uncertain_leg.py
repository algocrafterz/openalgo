"""BUG-017: an uncertain exit on one leg must not hide the other leg's shares, and a position
the broker still holds must stay tracked.

2026-11-04 CGPOWER: BREAKINGTRADE and BREAKINGTRADE-RI each held 29 shares (broker net 58). The
14:45 time exit of BREAKINGTRADE timed out and then filled (net 29) but stayed UNCERTAIN, so its
leg was still tracked. BREAKINGTRADE-RI then computed 29 (net) - 29 (the other leg) = 0 and sent
nothing; after three attempts both legs were deleted from the tracker with the broker still holding
29 shares, no stop and nothing watching it until the sandbox square-off at 15:15.
"""

import types
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import db, order_reconcile
from signal_engine.models import Direction
from signal_engine.strategies import BREAKINGTRADE
from signal_engine.tests.tracker_fixtures import _make_engine, _make_position
from signal_engine.timeutils import IST
from signal_engine.tracker import PositionTracker

RI = "BREAKINGTRADE-RI"


@pytest.fixture(autouse=True)
def _clean():
    db.set_trade_mode("analyze")
    for u in order_reconcile.all_uncertain():
        order_reconcile.clear_uncertain(u.strategy, u.symbol)
    yield
    for u in order_reconcile.all_uncertain():
        order_reconcile.clear_uncertain(u.strategy, u.symbol)


def _two_legs():
    tracker = PositionTracker(_make_engine())
    for strategy in (BREAKINGTRADE, RI):
        tracker.register(
            _make_position(symbol="CGPOWER", strategy=strategy, quantity=29, product="MIS")
        )
    return tracker


def _uncertain(strategy):
    order_reconcile.mark_uncertain(
        order_reconcile.UncertainOrder(
            strategy=strategy, symbol="CGPOWER", action="SELL", quantity=29,
            since=datetime.now(IST) - timedelta(seconds=60), kind="EXIT",
        )
    )


def _book(status):
    stamp = (datetime.now(IST) + timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S")
    return [
        {
            "symbol": "CGPOWER", "action": "SELL", "quantity": 29, "order_status": status,
            "timestamp": stamp, "orderid": "26110453174657",
        }
    ]


async def _own_qty(tracker, *, net, book, strategy=RI):
    pos = tracker.find_position("CGPOWER", strategy)
    with (
        patch("signal_engine.tracker.fetch_net_position", AsyncMock(return_value=net)),
        patch("signal_engine.tracker.fetch_orderbook", AsyncMock(return_value=book)),
    ):
        return await tracker._own_close_qty(tracker._key("CGPOWER", strategy), pos)


@pytest.mark.asyncio
class TestOwnCloseQuantityWithAnUncertainOtherLeg:
    async def test_the_real_cgpower_case_sells_the_second_leg(self):
        """The other leg's exit shows COMPLETE on the book: net 29 is all this leg's."""
        tracker = _two_legs()
        _uncertain(BREAKINGTRADE)
        assert await _own_qty(tracker, net=29, book=_book("complete")) == 29

    async def test_an_uncertain_exit_still_working_keeps_the_old_count(self):
        """Its shares are still in the net. This leg's stop may have filled, so selling 29
        now would be selling the other leg's shares and reverse the position."""
        tracker = _two_legs()
        _uncertain(BREAKINGTRADE)
        assert await _own_qty(tracker, net=29, book=_book("open")) == 0

    async def test_an_uncertain_exit_not_on_the_book_keeps_the_old_count(self):
        tracker = _two_legs()
        _uncertain(BREAKINGTRADE)
        assert await _own_qty(tracker, net=29, book=[]) == 0

    async def test_without_an_uncertain_exit_the_other_leg_is_still_taken_out(self):
        tracker = _two_legs()
        assert await _own_qty(tracker, net=29, book=[]) == 0

    async def test_an_unfilled_uncertain_exit_with_both_legs_held_sells_only_its_own(self):
        tracker = _two_legs()
        _uncertain(BREAKINGTRADE)
        assert await _own_qty(tracker, net=58, book=_book("open")) == 29

    async def test_the_cap_still_stops_a_sale_into_a_flat_position(self):
        tracker = _two_legs()
        _uncertain(BREAKINGTRADE)
        assert await _own_qty(tracker, net=0, book=_book("complete")) == 0

    async def test_opposite_directions_on_one_symbol_are_still_closed(self):
        """BT short 29 (uncertain BUY, not on the book), RI long 29, net 0: RI is closed as
        before; the other leg's shares were never going to be excluded without proof."""
        tracker = PositionTracker(_make_engine())
        tracker.register(
            _make_position(symbol="CGPOWER", strategy=BREAKINGTRADE, quantity=29, product="MIS",
                           direction=Direction.SHORT)
        )
        tracker.register(_make_position(symbol="CGPOWER", strategy=RI, quantity=29, product="MIS"))
        order_reconcile.mark_uncertain(
            order_reconcile.UncertainOrder(
                strategy=BREAKINGTRADE, symbol="CGPOWER", action="BUY", quantity=29,
                since=datetime.now(IST) - timedelta(seconds=60), kind="EXIT",
            )
        )
        assert await _own_qty(tracker, net=0, book=[]) == 29


@pytest.mark.asyncio
class TestAPositionTheBrokerStillHoldsStaysTracked:
    async def test_booking_an_unconfirmed_close_does_not_delete_the_leg(self):
        tracker = _two_legs()
        key = tracker._key("CGPOWER", BREAKINGTRADE)
        pos = tracker.find_position("CGPOWER", BREAKINGTRADE)
        with patch("signal_engine.tracker.notifier", new_callable=AsyncMock):
            await tracker._book_one_time_exit(key, pos, 0.0, None, confirmed_closed=False)
        assert tracker.find_position("CGPOWER", BREAKINGTRADE) is pos
        assert tracker.tracked_count == 2

    async def test_no_close_is_recorded_for_it(self):
        engine = _make_engine()
        tracker = PositionTracker(engine)
        pos = _make_position(symbol="CGPOWER", strategy=BREAKINGTRADE, quantity=29, product="MIS")
        tracker.register(pos)
        key = tracker._key("CGPOWER", BREAKINGTRADE)
        with (
            patch("signal_engine.tracker.notifier", new_callable=AsyncMock) as notifier,
            patch.object(engine, "record_close") as record_close,
        ):
            await tracker._book_one_time_exit(key, pos, 0.0, None, confirmed_closed=False)
        record_close.assert_not_called()
        notifier.notify_time_exit.assert_not_called()
        assert tracker._day_trades == 0 and tracker._completed_trades == []

    async def test_a_confirmed_close_is_still_booked_and_removed(self):
        tracker = _two_legs()
        key = tracker._key("CGPOWER", BREAKINGTRADE)
        pos = tracker.find_position("CGPOWER", BREAKINGTRADE)
        with patch("signal_engine.tracker.notifier", new_callable=AsyncMock):
            await tracker._book_one_time_exit(
                key, pos, 0.0, SimpleNamespace(max_trades_per_day=5), confirmed_closed=True
            )
        assert tracker.find_position("CGPOWER", BREAKINGTRADE) is None


@pytest.mark.asyncio
class TestALegLeftOpenIsNotExitedAgainOnItsOwn:
    async def _left_open(self):
        tracker = _two_legs()
        key = tracker._key("CGPOWER", BREAKINGTRADE)
        pos = tracker.find_position("CGPOWER", BREAKINGTRADE)
        with patch("signal_engine.tracker.notifier", new_callable=AsyncMock):
            await tracker._book_one_time_exit(key, pos, 0.0, None, confirmed_closed=False)
        return tracker, pos

    async def test_the_leg_is_flagged(self):
        _, pos = await self._left_open()
        assert pos.time_exit_left_open is True

    async def test_the_no_progress_exit_skips_it(self):
        tracker, pos = await self._left_open()
        fired = []
        tracker._evaluate_no_progress = lambda *a, **k: fired.append(1) or None
        with patch("signal_engine.tracker._ladder_owns_exits", return_value=False):
            await tracker._check_no_progress({})
        # the unflagged sibling is evaluated, the flagged leg is not
        assert len(fired) == 1

    async def test_a_signal_exit_for_it_is_declined(self):
        from signal_engine import main

        tracker, pos = await self._left_open()
        signal = types.SimpleNamespace(symbol="CGPOWER", strategy=BREAKINGTRADE, tp_level="TP1")
        with (
            patch.object(main, "tracker", tracker),
            patch.object(main, "_abort_exit_on_rejected_entry", AsyncMock()) as abort,
        ):
            await main._handle_exit_locked(signal)
        abort.assert_not_called()
        assert pos.exit_pending is False
