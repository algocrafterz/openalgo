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

    def test_an_unparseable_timestamp_never_identifies_the_order(self):
        """Owner rule 2026-10-26 (was: an unparseable stamp still matched). An unreadable stamp
        cannot show the order was placed after the send: it may be an earlier same-size fill."""
        assert _find([_order(timestamp="not a time")]) == ""

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


class TestFlattradeDayFirstStamps:
    """Flattrade stamps are day-first; a sandbox-only parse let every earlier same-size fill match."""

    def test_an_earlier_day_first_fill_is_skipped(self):
        assert _find([_order(timestamp="14:40:00 07-10-2026")]) == ""

    def test_a_day_first_fill_after_the_send_is_found(self):
        assert _find([_order(timestamp="14:55:04 07-10-2026")]) == "OID-1"

    def test_only_the_current_fill_matches_beside_an_earlier_one(self):
        book = [_order(orderid="OLD", timestamp="07-10-2026 14:40:00"),
                _order(orderid="NEW", timestamp="07-10-2026 14:55:05")]
        assert _find(book) == "NEW"


# --------------------------------------------------------------------------
# Fail-safe matching (owner rule 2026-10-26): only a DEFINITE match identifies the order
# --------------------------------------------------------------------------


def _match(book, **kw):
    from signal_engine.api_client import match_recent_complete_order

    args = {"symbol": "PFC", "action": "SELL", "quantity": 930, "since": SINCE}
    args.update(kw)
    return match_recent_complete_order(book, **args)


class TestMatchRecentCompleteOrder:
    def test_a_sandbox_stamp_after_the_send_is_definite(self):
        m = _match([_order(timestamp="2026-10-07 14:55:04")])
        assert (m.complete, m.ambiguous) == (["OID-1"], [])

    def test_a_flattrade_stamp_after_the_send_is_definite(self):
        m = _match([_order(timestamp="14:55:04 07-10-2026")])
        assert (m.complete, m.ambiguous) == (["OID-1"], [])

    @pytest.mark.parametrize("stamp", ["not a time", "07/10/2026 14:55", "", None])
    def test_a_malformed_or_missing_stamp_is_ambiguous_never_a_match(self, stamp):
        m = _match([_order(timestamp=stamp)])
        assert m.complete == [] and m.ambiguous == ["OID-1"]

    def test_a_stamp_just_before_the_send_is_ambiguous(self):
        m = _match([_order(timestamp="14:55:00 07-10-2026")])  # 3 s before since: clock skew
        assert m.complete == [] and m.ambiguous == ["OID-1"]

    def test_an_earlier_fill_well_before_the_send_is_not_ours(self):
        m = _match([_order(timestamp="14:40:00 07-10-2026")])
        assert (m.complete, m.ambiguous) == ([], [])

    def test_a_previously_booked_id_is_ignored_even_with_a_bad_stamp(self):
        m = _match([_order(timestamp="not a time")], exclude=frozenset({"OID-1"}))
        assert (m.complete, m.ambiguous) == ([], [])

    def test_another_strategys_order_on_the_symbol_is_ignored(self):
        m = _match([_order(strategy="BREAKOUT-PY")], strategy="BREAKOUT")
        assert (m.complete, m.ambiguous) == ([], [])

    def test_a_delayed_fill_stamped_well_after_the_send_is_definite(self):
        m = _match([_order(timestamp="14:57:30 07-10-2026")])  # filled 2.5 min after the send
        assert m.complete == ["OID-1"]

    def test_the_finder_returns_nothing_while_only_an_ambiguous_copy_exists(self):
        assert _find([_order(timestamp="14:55:00 07-10-2026")]) == ""


@pytest.mark.asyncio
class TestTheMainHelperHoldsAnAmbiguousMatch:
    async def test_an_ambiguous_copy_is_reported_not_adopted(self):
        from signal_engine.main import _find_recent_market_exit_fill

        book = [_order(timestamp="not a time")]
        with (
            patch("signal_engine.api_client.fetch_orderbook", AsyncMock(return_value=book)),
            patch("signal_engine.main.fetch_order_ids_today", lambda symbol: set()),
        ):
            result = await _find_recent_market_exit_fill(_sell_order(), SINCE)
        assert result is not None
        assert result.status == OrderStatus.TIMEOUT and result.order_id == ""
        assert "OID-1" in result.message and "ambiguous" in result.message.lower()

    async def test_a_booked_copy_is_not_adopted(self):
        from signal_engine.main import _find_recent_market_exit_fill

        with (
            patch("signal_engine.api_client.fetch_orderbook", AsyncMock(return_value=[_order()])),
            patch("signal_engine.main.fetch_order_ids_today", lambda symbol: {"OID-1"}),
        ):
            assert await _find_recent_market_exit_fill(_sell_order(), SINCE) is None
