"""The shared 'find the complete order we just sent' matcher.

Used by the timed-out exit recovery (main._find_recent_market_exit_fill), the bad-fill guard and
the time exit's ledger row. A filled MARKET order shows 'complete' at once, so recency is the only
guard against adopting an earlier fill of the same symbol, action and size.
"""

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine.api_client import find_recent_complete_order_id
from signal_engine.models import Action, Order, OrderStatus
from signal_engine.timeutils import IST

SINCE = datetime(2026, 10, 7, 14, 55, 3, tzinfo=IST)


def _order(**overrides):
    base = {
        "symbol": "PFC", "action": "SELL", "quantity": 930, "order_status": "complete",
        "timestamp": "2026-10-07 14:55:04", "orderid": "OID-1",
    }
    base.update(overrides)
    return base


def _find(book, **kw):
    args = {"symbol": "PFC", "action": "SELL", "quantity": 930, "since": SINCE}
    args.update(kw)
    return find_recent_complete_order_id(
        book, args["symbol"], args["action"], args["quantity"], args["since"]
    )


class TestMatching:
    def test_a_complete_matching_order_is_found(self):
        assert _find([_order()]) == "OID-1"

    def test_a_fill_in_the_same_second_as_since_is_not_skipped(self):
        assert _find([_order(timestamp="2026-10-07 14:55:03")]) == "OID-1"

    def test_an_older_fill_of_the_same_size_is_not_adopted(self):
        assert _find([_order(timestamp="2026-10-07 14:55:02")]) == ""

    @pytest.mark.parametrize(
        "override",
        [
            {"symbol": "ONGC"},
            {"action": "BUY"},
            {"quantity": 100},
            {"order_status": "open"},
            {"orderid": ""},
        ],
    )
    def test_a_row_that_differs_is_not_adopted(self, override):
        assert _find([_order(**override)]) == ""

    def test_symbol_and_status_compare_case_insensitively(self):
        assert _find([_order(symbol="pfc", order_status="COMPLETE")]) == "OID-1"

    def test_an_unparseable_timestamp_does_not_discard_a_real_match(self):
        assert _find([_order(timestamp="not a time")]) == "OID-1"

    def test_the_first_row_with_an_id_wins_and_junk_rows_are_ignored(self):
        book = ["junk", None, _order(orderid=""), _order(orderid="OID-2")]
        assert _find(book) == "OID-2"

    def test_an_empty_book_finds_nothing(self):
        assert _find([]) == ""


def _sell_order():
    return Order(
        symbol="PFC", exchange="NSE", action=Action.SELL, quantity=930, price=0.0,
        order_type="MARKET", product="MIS", strategy_tag="BREAKOUT",
    )


@pytest.mark.asyncio
class TestTheMainHelperStillBehavesTheSame:
    async def test_it_returns_a_success_result_carrying_the_order_id(self):
        from signal_engine.main import _find_recent_market_exit_fill

        with patch("signal_engine.api_client.fetch_orderbook", AsyncMock(return_value=[_order()])):
            result = await _find_recent_market_exit_fill(_sell_order(), SINCE)
        assert result is not None
        assert (result.status, result.order_id) == (OrderStatus.SUCCESS, "OID-1")
        assert result.message == "recovered after timeout"

    async def test_it_returns_none_when_the_book_is_empty_or_unreadable(self):
        from signal_engine.main import _find_recent_market_exit_fill

        for book in (None, []):
            with patch("signal_engine.api_client.fetch_orderbook", AsyncMock(return_value=book)):
                assert await _find_recent_market_exit_fill(_sell_order(), SINCE) is None
