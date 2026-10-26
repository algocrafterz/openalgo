"""The recency check on a timed-out order must read the broker's own timestamp format.

The sandbox stamps orders "%Y-%m-%d %H:%M:%S". Flattrade stamps them day-first:
"%H:%M:%S %d-%m-%Y" in the order book (norentm) and "%d-%m-%Y %H:%M:%S" in history. A parser
that knows only the sandbox form cannot read a live stamp, and if an unreadable stamp counts as
a match the recency check is off: a timed-out order adopts an unrelated earlier fill of the
same symbol, side and size, and the position it was sent for is left unprotected.

Both outcomes are unsafe in different ways, so each test names the one it rules out:
  * FILLED on an older fill  -> adopts somebody else's fill
  * ABSENT on a real fill     -> the caller resends, and the broker holds two copies
An unreadable stamp is neither proof of a fill nor proof of absence, so it must stay UNCERTAIN.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from signal_engine import order_reconcile as oc
from signal_engine.api_client import find_recent_complete_order_id
from signal_engine.timeutils import IST

# A client-side timeout at 13:56:45 IST on 2026-10-26.
SINCE = datetime(2026, 10, 26, 13, 56, 45, tzinfo=IST)
EARLIER = SINCE - timedelta(minutes=30)  # an unrelated fill of the same symbol, side and size
LATER = SINCE + timedelta(seconds=40)  # the timed-out order filling after the client gave up


def sandbox(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S")


def flattrade_book(t: datetime) -> str:
    return t.strftime("%H:%M:%S %d-%m-%Y")


def flattrade_history(t: datetime) -> str:
    return t.strftime("%d-%m-%Y %H:%M:%S")


def _row(ts, oid="26102600000001", status="complete"):
    return {
        "orderid": oid,
        "symbol": "PFC",
        "action": "SELL",
        "quantity": 930,
        "order_status": status,
        "timestamp": ts,
    }


async def _reconcile(book):
    return await oc.reconcile_timed_out_order(
        symbol="PFC",
        action="SELL",
        quantity=930,
        since=SINCE,
        fetch_orderbook=AsyncMock(return_value=book),
        attempts=1,
        delay=0.0,
    )


def _recent(book):
    return find_recent_complete_order_id(book, "PFC", "SELL", 930, SINCE)


# ---------------------------------------------------------------------------
# Timed-out order reconciliation
# ---------------------------------------------------------------------------


class TestReconcileReadsEveryBrokerFormat:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("fmt", [sandbox, flattrade_book, flattrade_history])
    async def test_a_fill_after_the_timeout_is_adopted_not_resent(self, fmt):
        out = await _reconcile([_row(fmt(LATER))])
        assert out.outcome == oc.FILLED, "ABSENT here makes the caller resend a filled order"
        assert out.order_id == "26102600000001"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fmt", [sandbox, flattrade_book, flattrade_history])
    async def test_an_older_fill_of_the_same_order_shape_is_never_adopted(self, fmt):
        out = await _reconcile([_row(fmt(EARLIER), oid="26102600000000")])
        assert out.outcome != oc.FILLED, (
            "adopting an unrelated earlier fill leaves this order unprotected"
        )

    @pytest.mark.asyncio
    async def test_the_new_fill_is_picked_when_an_older_one_sits_beside_it(self):
        book = [
            _row(flattrade_book(EARLIER), oid="26102600000000"),
            _row(flattrade_book(LATER), oid="26102600000001"),
        ]
        out = await _reconcile(book)
        assert out.outcome == oc.FILLED
        assert out.order_id == "26102600000001"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "raw", ["not a time", "26/10/2026 13:57:25", "13:57", "2026-13-40 99:99:99"]
    )
    async def test_an_unreadable_stamp_is_neither_adopted_nor_treated_as_absent(self, raw):
        out = await _reconcile([_row(raw)])
        assert out.outcome == oc.UNCERTAIN, (
            "an unreadable stamp proves neither a fill nor its absence"
        )


# ---------------------------------------------------------------------------
# The fill lookup used to adopt an entry after a timeout
# ---------------------------------------------------------------------------


class TestRecentCompleteOrderReadsEveryBrokerFormat:
    @pytest.mark.parametrize("fmt", [sandbox, flattrade_book, flattrade_history])
    def test_a_fill_after_since_is_found(self, fmt):
        assert _recent([_row(fmt(LATER))]) == "26102600000001"

    @pytest.mark.parametrize("fmt", [sandbox, flattrade_book, flattrade_history])
    def test_an_older_fill_is_not_returned(self, fmt):
        assert _recent([_row(fmt(EARLIER), oid="26102600000000")]) == ""

    def test_the_later_fill_wins_over_an_older_one_in_day_first_form(self):
        book = [
            _row(flattrade_book(EARLIER), oid="26102600000000"),
            _row(flattrade_book(LATER), oid="26102600000001"),
        ]
        assert _recent(book) == "26102600000001"

    @pytest.mark.parametrize("raw", ["not a time", "26/10/2026 13:57:25"])
    def test_an_unreadable_stamp_is_not_a_definite_match(self, raw):
        assert _recent([_row(raw)]) == ""
