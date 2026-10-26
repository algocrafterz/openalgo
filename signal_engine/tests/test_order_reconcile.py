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


# --------------------------------------------------------------------------
# Same-day, same-size earlier fills (Flattrade stamps are day-first)
# --------------------------------------------------------------------------
#
# The time filter parsed only the sandbox stamp. Flattrade's day-first stamp failed to parse,
# and an unparseable stamp counted as new, so on a live book every same-side, same-size order of
# the day matched: an earlier fill could be adopted as the timed-out exit, booking a close that
# was never sent and leaving the position open with no stop.


class TestParseBrokerStamp:
    def test_sandbox_format(self):
        assert oc.parse_broker_stamp("2026-10-13 13:56:46") == datetime(2026, 10, 13, 13, 56, 46, tzinfo=IST)

    def test_flattrade_time_first_format(self):
        assert oc.parse_broker_stamp("13:56:46 13-10-2026") == datetime(2026, 10, 13, 13, 56, 46, tzinfo=IST)

    def test_flattrade_day_first_date_format(self):
        assert oc.parse_broker_stamp("13-10-2026 13:56:46") == datetime(2026, 10, 13, 13, 56, 46, tzinfo=IST)

    def test_unknown_or_missing_stamp_is_none(self):
        for raw in ("yesterday", "", None):
            assert oc.parse_broker_stamp(raw) is None


class TestEarlierSameSizeFill:
    def test_flattrade_earlier_fill_is_not_matched(self):
        book = [_row(oid="OLD", ts="10:15:02 13-10-2026")]
        assert oc.classify_book(book, "LAURUSLABS", "SELL", 74, SINCE) == ([], 0)

    def test_sandbox_earlier_fill_is_not_matched(self):
        book = [_row(oid="OLD", ts="2026-10-13 10:15:02")]
        assert oc.classify_book(book, "LAURUSLABS", "SELL", 74, SINCE) == ([], 0)

    def test_only_the_current_fill_matches_beside_an_earlier_one(self):
        book = [_row(oid="OLD", ts="10:15:02 13-10-2026"), _row(oid="NEW", ts="13:56:47 13-10-2026")]
        assert oc.classify_book(book, "LAURUSLABS", "SELL", 74, SINCE) == (["NEW"], 0)

    def test_an_order_stamped_in_the_send_second_matches(self):
        book = [_row(oid="NEW", ts="13:56:45 13-10-2026")]
        assert oc.classify_book(book, "LAURUSLABS", "SELL", 74, SINCE) == (["NEW"], 0)


class TestStrategyTag:
    def test_another_strategys_order_is_not_matched(self):
        book = [{**_row(oid="THEIRS"), "strategy": "BREAKOUT-PY"}]
        assert oc.classify_book(book, "LAURUSLABS", "SELL", 74, SINCE, strategy="ORB") == ([], 0)

    def test_an_untagged_order_still_matches(self):
        book = [{**_row(oid="NOTAG"), "strategy": ""}]
        assert oc.classify_book(book, "LAURUSLABS", "SELL", 74, SINCE, strategy="ORB") == (["NOTAG"], 0)

    def test_the_tag_compare_ignores_case(self):
        book = [{**_row(oid="MINE"), "strategy": "orb"}]
        assert oc.classify_book(book, "LAURUSLABS", "SELL", 74, SINCE, strategy="ORB") == (["MINE"], 0)


@pytest.mark.asyncio
class TestReconcileExcludesBookedFills:
    async def test_a_booked_id_is_never_adopted(self):
        r = await _reconcile(AsyncMock(return_value=[_row(oid="BOOKED")]), attempts=1,
                             exclude=frozenset({"BOOKED"}))
        assert r.outcome == oc.ABSENT

    async def test_the_new_fill_is_adopted_beside_a_booked_one(self):
        book = [_row(oid="BOOKED"), _row(oid="NEW", ts="2026-10-13 13:56:47")]
        r = await _reconcile(AsyncMock(return_value=book), attempts=1, exclude=frozenset({"BOOKED"}))
        assert (r.outcome, r.order_id) == (oc.FILLED, "NEW")

    async def test_the_strategy_reaches_the_book_filter(self):
        book = [{**_row(oid="THEIRS"), "strategy": "BREAKOUT-PY"}]
        r = await _reconcile(AsyncMock(return_value=book), attempts=1, strategy="ORB")
        assert r.outcome == oc.ABSENT


class TestUnknownStampFormat:
    def test_an_unknown_stamp_still_matches_and_is_logged(self):
        from loguru import logger

        lines = []
        hid = logger.add(lambda m: lines.append(str(m)), level="WARNING")
        try:
            book = [_row(oid="ODD", ts="13/10/2026 1:56pm")]
            assert oc.classify_book(book, "LAURUSLABS", "SELL", 74, SINCE) == (["ODD"], 0)
        finally:
            logger.remove(hid)
        assert any("unknown format" in ln and "ODD" in ln for ln in lines)
