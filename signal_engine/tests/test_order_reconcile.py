"""A timed-out order is only resent when the broker's order book was read and shows no copy.

Real data from 2026-10-13: LAURUSLABS SELL 74 and SUNPHARMA SELL 236 trailing-stop exits at
13:56:45 all timed out client-side while the order book fetch failed; the retry loop resent
and the broker filled three copies of each, opening shorts of 148 and 472.
"""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from signal_engine import order_reconcile as oc
from signal_engine.timeutils import IST

SINCE = datetime(2026, 10, 13, 13, 56, 45, tzinfo=IST)


def _row(oid="26101300000001", symbol="LAURUSLABS", action="SELL", qty=74,
         status="complete", ts="2026-10-13 13:56:46"):
    return {"orderid": oid, "symbol": symbol, "action": action, "quantity": qty,
            "order_status": status, "timestamp": ts}


async def _reconcile(fetch, attempts=3, **kw):
    args = {"symbol": "LAURUSLABS", "action": "SELL", "quantity": 74, "since": SINCE,
            "fetch_orderbook": fetch, "attempts": attempts, "delay": 0.0}
    args.update(kw)
    return await oc.reconcile_timed_out_order(**args)


@pytest.mark.asyncio
class TestReconcile:
    async def test_order_that_filled_is_adopted_not_resent(self):
        r = await _reconcile(AsyncMock(return_value=[_row()]))
        assert (r.outcome, r.order_id) == (oc.FILLED, "26101300000001")

    async def test_delayed_confirmation_is_found_on_a_later_poll(self):
        fetch = AsyncMock(side_effect=[[], [], [_row()]])
        r = await _reconcile(fetch)
        assert (r.outcome, r.order_id) == (oc.FILLED, "26101300000001")
        assert fetch.await_count == 3

    async def test_connection_loss_on_every_read_is_uncertain_never_absent(self):
        r = await _reconcile(AsyncMock(return_value=None))
        assert r.outcome == oc.UNCERTAIN

    async def test_connection_back_on_the_last_read_with_no_order_means_absent(self):
        r = await _reconcile(AsyncMock(side_effect=[None, None, []]))
        assert r.outcome == oc.ABSENT

    async def test_connection_lost_again_on_the_last_read_is_uncertain(self):
        r = await _reconcile(AsyncMock(side_effect=[[], [], None]))
        assert r.outcome == oc.UNCERTAIN

    async def test_working_or_partially_filled_order_is_in_flight_so_uncertain(self):
        for status in ("open", "pending", "trigger pending", "partially filled"):
            r = await _reconcile(AsyncMock(return_value=[_row(status=status)]))
            assert r.outcome == oc.UNCERTAIN, status

    async def test_rejected_or_cancelled_copy_is_safe_to_resend(self):
        for status in ("rejected", "cancelled"):
            r = await _reconcile(AsyncMock(return_value=[_row(status=status)]))
            assert r.outcome == oc.ABSENT, status

    async def test_duplicate_fills_are_reported_and_the_first_is_adopted(self):
        book = [_row("A1"), _row("A2"), _row("A3")]
        r = await _reconcile(AsyncMock(return_value=book))
        assert (r.outcome, r.order_id, r.duplicate_ids) == (oc.FILLED, "A1", ("A2", "A3"))

    async def test_an_earlier_fill_of_the_same_size_is_not_adopted(self):
        old = _row("OLD", ts="2026-10-13 10:00:00")
        r = await _reconcile(AsyncMock(return_value=[old]))
        assert r.outcome == oc.ABSENT

    async def test_an_already_booked_order_id_is_excluded(self):
        r = await _reconcile(AsyncMock(return_value=[_row("BOOKED")]), exclude=frozenset({"BOOKED"}))
        assert r.outcome == oc.ABSENT

    async def test_other_symbol_side_or_size_is_ignored(self):
        book = [_row(symbol="SUNPHARMA"), _row(action="BUY"), _row(qty=236)]
        r = await _reconcile(AsyncMock(return_value=book))
        assert r.outcome == oc.ABSENT


class TestRegistry:
    def test_mark_get_clear_round_trip(self):
        order = oc.UncertainOrder("ORB", "SUNPHARMA", "SELL", 236, SINCE, "EXIT")
        oc.mark_uncertain(order)
        assert oc.get_uncertain("ORB", "SUNPHARMA") is order
        assert oc.get_uncertain("ORB", "OTHER") is None
        oc.clear_uncertain("ORB", "SUNPHARMA")
        assert oc.get_uncertain("ORB", "SUNPHARMA") is None
