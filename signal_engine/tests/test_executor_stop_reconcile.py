"""2026-10-14 HINDALCO duplicate protective stop.

EVWAP-EARLY-PY entered HINDALCO at 10:35:59. The stop request timed out while the OpenAlgo
server was stalled. The verify reads found nothing, the retry was sent, then the stalled first
request landed 6 ms before the retry: two live SELL 319 SL-M orders. The 11:00 exit cancelled
only the tracked id, the other stop fired at 11:16 on a flat position and opened a 319 short.

The fix: reconcile the order book before and after a retry, remove duplicate stops, and cancel
every live protective order of the strategy on exit.
"""

import dataclasses
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import executor
from signal_engine.config import settings as _real_settings
from signal_engine.models import Direction, OrderStatus, TradeResult

TIMEOUT = TradeResult(order_id="", status=OrderStatus.TIMEOUT, message="Request timed out")


def _ok(order_id):
    return TradeResult(order_id=order_id, status=OrderStatus.SUCCESS, message="ok")


def _stop(order_id, status="trigger pending", strategy="EVWAP-EARLY-PY", **extra):
    row = {
        "orderid": order_id,
        "symbol": "HINDALCO",
        "action": "SELL",
        "quantity": 319,
        "pricetype": "SL-M",
        "trigger_price": 893.7,
        "order_status": status,
        "strategy": strategy,
        "product": "MIS",
    }
    row.update(extra)
    return row


def _cfg(**kw):
    base = {
        "bracket_max_sl_retries": 5,
        "bracket_retry_delay": 0.0,
        "sl_timeout_verify_attempts": 3,
        "sl_timeout_settle_seconds": 0.0,
    }
    base.update(kw)
    return dataclasses.replace(_real_settings, **base)


@pytest.fixture(autouse=True)
def _clean_unconfirmed():
    executor._UNCONFIRMED_SL.clear()
    yield
    executor._UNCONFIRMED_SL.clear()


async def _place(**kw):
    args = {
        "symbol": "HINDALCO", "exchange": "NSE", "direction": Direction.LONG, "quantity": 319,
        "sl_price": 893.6, "product": "MIS", "strategy_tag": "EVWAP-EARLY-PY",
    }
    args.update(kw)
    return await executor.place_sl_order(**args)


class TestProtectiveOrderSelection:
    def test_selects_only_live_stops_of_this_strategy_symbol_and_side(self):
        book = [
            _stop("A"),
            _stop("B", status="trigger pending"),
            _stop("DONE", status="complete"),
            _stop("GONE", status="cancelled"),
            _stop("OTHER-STRAT", strategy="BREAKOUT-PY"),
            _stop("OTHER-SIDE", action="BUY"),
            _stop("OTHER-SYM", symbol="LT"),
            _stop("ENTRY", pricetype="MARKET"),
            {k: v for k, v in _stop("UNTAGGED").items() if k != "strategy"},
        ]
        got = executor.protective_orders(book, "HINDALCO", "EVWAP-EARLY-PY", "SELL")
        assert [o["orderid"] for o in got] == ["A", "B"]

    def test_status_spelling_variants_count_as_live(self):
        book = [_stop("A", status="TRIGGER_PENDING"), _stop("B", status="open"),
                _stop("C", status="partially filled")]
        got = executor.protective_orders(book, "HINDALCO", "EVWAP-EARLY-PY", "SELL")
        assert {o["orderid"] for o in got} == {"A", "B", "C"}


class TestDuplicateStopAfterAmbiguousTimeout:
    @pytest.mark.asyncio
    async def test_late_landing_first_request_is_cancelled_and_the_retry_kept(self):
        """The incident: verify reads are empty, the retry succeeds, then BOTH are on the book."""
        send = AsyncMock(side_effect=[TIMEOUT, _ok("B")])
        book = AsyncMock(side_effect=[[], [], [], [_stop("A"), _stop("B")], [_stop("B")]])
        cancel = AsyncMock(return_value=True)
        with (
            patch.object(executor, "settings", _cfg()),
            patch.object(executor, "send_order", send),
            patch("signal_engine.api_client.fetch_orderbook", book),
            patch("signal_engine.api_client.cancel_order", cancel),
        ):
            result = await _place()
        assert result.status == OrderStatus.SUCCESS
        assert result.order_id == "B"
        cancel.assert_awaited_once_with("A", "EVWAP-EARLY-PY")
        assert not executor._UNCONFIRMED_SL

    @pytest.mark.asyncio
    async def test_other_strategys_stop_on_the_same_symbol_is_never_cancelled(self):
        send = AsyncMock(side_effect=[TIMEOUT, _ok("B")])
        book = AsyncMock(side_effect=[
            [], [], [],
            [_stop("A"), _stop("B"), _stop("P1", strategy="BREAKOUT-PY")],
            [_stop("B"), _stop("P1", strategy="BREAKOUT-PY")],
        ])
        cancel = AsyncMock(return_value=True)
        with (
            patch.object(executor, "settings", _cfg()),
            patch.object(executor, "send_order", send),
            patch("signal_engine.api_client.fetch_orderbook", book),
            patch("signal_engine.api_client.cancel_order", cancel),
        ):
            await _place()
        assert [c.args[0] for c in cancel.await_args_list] == ["A"]

    @pytest.mark.asyncio
    async def test_unreadable_book_after_timeout_stops_retrying(self):
        """Fail closed: a timeout is not a rejection, and with no book there is no proof."""
        send = AsyncMock(return_value=TIMEOUT)
        with (
            patch.object(executor, "settings", _cfg()),
            patch.object(executor, "send_order", send),
            patch("signal_engine.api_client.fetch_orderbook", AsyncMock(return_value=None)),
        ):
            result = await _place()
        assert result.status == OrderStatus.TIMEOUT
        assert send.await_count == 1
        assert ("EVWAP-EARLY-PY", "HINDALCO", "SELL") in executor._UNCONFIRMED_SL

    @pytest.mark.asyncio
    async def test_next_call_adopts_the_order_that_landed_instead_of_resending(self):
        """The tracker's missing-stop retry calls place_sl_order again later."""
        executor._UNCONFIRMED_SL.add(("EVWAP-EARLY-PY", "HINDALCO", "SELL"))
        send = AsyncMock(return_value=_ok("NEW"))
        with (
            patch.object(executor, "settings", _cfg()),
            patch.object(executor, "send_order", send),
            patch("signal_engine.api_client.fetch_orderbook",
                  AsyncMock(return_value=[_stop("A")])),
        ):
            result = await _place()
        assert result.order_id == "A"
        send.assert_not_awaited()
        assert not executor._UNCONFIRMED_SL

    @pytest.mark.asyncio
    async def test_next_call_does_not_send_while_the_book_is_unreadable(self):
        executor._UNCONFIRMED_SL.add(("EVWAP-EARLY-PY", "HINDALCO", "SELL"))
        send = AsyncMock(return_value=_ok("NEW"))
        with (
            patch.object(executor, "settings", _cfg()),
            patch.object(executor, "send_order", send),
            patch("signal_engine.api_client.fetch_orderbook", AsyncMock(return_value=None)),
        ):
            result = await _place()
        assert result.status == OrderStatus.TIMEOUT
        send.assert_not_awaited()
        assert executor._UNCONFIRMED_SL

    @pytest.mark.asyncio
    async def test_next_call_sends_when_the_book_proves_nothing_landed(self):
        executor._UNCONFIRMED_SL.add(("EVWAP-EARLY-PY", "HINDALCO", "SELL"))
        send = AsyncMock(return_value=_ok("NEW"))
        with (
            patch.object(executor, "settings", _cfg()),
            patch.object(executor, "send_order", send),
            patch("signal_engine.api_client.fetch_orderbook",
                  AsyncMock(side_effect=[[], [_stop("NEW")]])),
        ):
            result = await _place()
        assert result.order_id == "NEW"
        send.assert_awaited_once()
        assert not executor._UNCONFIRMED_SL

    @pytest.mark.asyncio
    async def test_clean_first_attempt_makes_no_extra_orderbook_reads(self):
        send = AsyncMock(return_value=_ok("SL1"))
        book = AsyncMock(return_value=[])
        with (
            patch.object(executor, "settings", _cfg()),
            patch.object(executor, "send_order", send),
            patch("signal_engine.api_client.fetch_orderbook", book),
        ):
            result = await _place()
        assert result.order_id == "SL1"
        book.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_duplicate_that_cannot_be_cancelled_is_reported_not_hidden(self):
        send = AsyncMock(side_effect=[TIMEOUT, _ok("B")])
        book = AsyncMock(side_effect=[
            [], [], [], [_stop("A"), _stop("B")], [_stop("A"), _stop("B")], [_stop("A"), _stop("B")],
        ])
        cancel = AsyncMock(return_value=False)
        with (
            patch.object(executor, "settings", _cfg()),
            patch.object(executor, "send_order", send),
            patch("signal_engine.api_client.fetch_orderbook", book),
            patch("signal_engine.api_client.cancel_order", cancel),
            patch.object(executor.asyncio, "sleep", AsyncMock()),
            patch.object(executor.logger, "error") as err,
        ):
            result = await _place()
        assert result.order_id == "B"
        assert any("duplicate" in str(c.args[0]).lower() for c in err.call_args_list)


class TestCancelLiveProtectiveOrders:
    @pytest.mark.asyncio
    async def test_cancels_every_live_stop_not_only_the_tracked_one(self):
        """HINDALCO 11:00: tracked B was cancelled, untracked A stayed live."""
        book = AsyncMock(side_effect=[[_stop("A"), _stop("B")], []])
        cancel = AsyncMock(return_value=True)
        sweep = await executor.cancel_live_protective_orders(
            "HINDALCO", "EVWAP-EARLY-PY", "SELL", fetch_book=book, cancel=cancel
        )
        assert sweep.clear is True
        assert sweep.cancelled == ("A", "B")
        assert [c.args[0] for c in cancel.await_args_list] == ["A", "B"]

    @pytest.mark.asyncio
    async def test_nothing_to_cancel_is_clear_without_a_second_read(self):
        book = AsyncMock(return_value=[_stop("DONE", status="complete")])
        cancel = AsyncMock()
        sweep = await executor.cancel_live_protective_orders(
            "HINDALCO", "EVWAP-EARLY-PY", "SELL", fetch_book=book, cancel=cancel
        )
        assert sweep.clear is True and sweep.cancelled == ()
        cancel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unreadable_book_is_not_clear_and_cancels_nothing(self):
        cancel = AsyncMock()
        sweep = await executor.cancel_live_protective_orders(
            "HINDALCO", "EVWAP-EARLY-PY", "SELL",
            fetch_book=AsyncMock(return_value=None), cancel=cancel,
        )
        assert sweep.clear is False
        cancel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_cancel_that_leaves_the_order_live_is_not_clear(self):
        sweep = await executor.cancel_live_protective_orders(
            "HINDALCO", "EVWAP-EARLY-PY", "SELL",
            fetch_book=AsyncMock(side_effect=[[_stop("A")], [_stop("A")], [_stop("A")]]),
            cancel=AsyncMock(return_value=False), settle_s=0,
        )
        assert sweep.clear is False

    @pytest.mark.asyncio
    async def test_unreadable_verification_read_is_not_clear(self):
        sweep = await executor.cancel_live_protective_orders(
            "HINDALCO", "EVWAP-EARLY-PY", "SELL",
            fetch_book=AsyncMock(side_effect=[[_stop("A")], None]),
            cancel=AsyncMock(return_value=True), settle_s=0,
        )
        assert sweep.clear is False
        assert sweep.readable is False

    @pytest.mark.asyncio
    async def test_stop_that_filled_during_the_cancel_is_reported_as_executed(self):
        """The cancel loses a race with a trigger: the position has changed."""
        sweep = await executor.cancel_live_protective_orders(
            "HINDALCO", "EVWAP-EARLY-PY", "SELL",
            fetch_book=AsyncMock(side_effect=[[_stop("A")], [_stop("A", status="complete")]]),
            cancel=AsyncMock(return_value=False),
        )
        assert sweep.clear is True
        assert sweep.executed == ("A",)

    @pytest.mark.asyncio
    async def test_partially_filled_stop_is_cancelled_and_reported(self):
        sweep = await executor.cancel_live_protective_orders(
            "HINDALCO", "EVWAP-EARLY-PY", "SELL",
            fetch_book=AsyncMock(side_effect=[
                [_stop("A", status="partially filled")], [_stop("A", status="complete")],
            ]),
            cancel=AsyncMock(return_value=True),
        )
        assert sweep.executed == ("A",)

    @pytest.mark.asyncio
    async def test_keep_id_is_left_alone(self):
        cancel = AsyncMock(return_value=True)
        sweep = await executor.cancel_live_protective_orders(
            "HINDALCO", "EVWAP-EARLY-PY", "SELL", keep_id="B",
            fetch_book=AsyncMock(side_effect=[[_stop("A"), _stop("B")], [_stop("B")]]),
            cancel=cancel,
        )
        assert sweep.clear is True
        assert [c.args[0] for c in cancel.await_args_list] == ["A"]


class TestReviewFixes:
    @pytest.mark.asyncio
    async def test_scan_never_adopts_another_strategys_stop(self):
        """ORB and BREAKOUT-PY with the same symbol, side, size and trigger."""
        executor._UNCONFIRMED_SL.add(("EVWAP-EARLY-PY", "HINDALCO", "SELL"))
        send = AsyncMock(return_value=_ok("MINE"))
        book = AsyncMock(side_effect=[
            [_stop("THEIRS", strategy="BREAKOUT-PY")],
            [_stop("THEIRS", strategy="BREAKOUT-PY"), _stop("MINE")],
        ])
        cancel = AsyncMock(return_value=True)
        with (
            patch.object(executor, "settings", _cfg()),
            patch.object(executor, "send_order", send),
            patch("signal_engine.api_client.fetch_orderbook", book),
            patch("signal_engine.api_client.cancel_order", cancel),
        ):
            result = await _place()
        assert result.order_id == "MINE"
        send.assert_awaited_once()
        cancel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_scan_ignores_non_stop_orders_of_the_same_strategy(self):
        executor._UNCONFIRMED_SL.add(("EVWAP-EARLY-PY", "HINDALCO", "SELL"))
        send = AsyncMock(return_value=_ok("MINE"))
        book = AsyncMock(side_effect=[[_stop("MKT", pricetype="MARKET")], [_stop("MINE")]])
        with (
            patch.object(executor, "settings", _cfg()),
            patch.object(executor, "send_order", send),
            patch("signal_engine.api_client.fetch_orderbook", book),
        ):
            result = await _place()
        assert result.order_id == "MINE"
        send.assert_awaited_once()

    def test_unconfirmed_flag_expires(self):
        reg = executor._UnconfirmedRegistry(ttl_s=0.0)
        reg.add(("S", "SYM", "SELL"))
        import time as _t
        _t.sleep(0.01)
        assert ("S", "SYM", "SELL") not in reg
        assert not reg

    def test_unconfirmed_flag_is_kept_inside_its_ttl(self):
        reg = executor._UnconfirmedRegistry(ttl_s=60.0)
        reg.add(("S", "SYM", "SELL"))
        assert ("S", "SYM", "SELL") in reg and reg

    @pytest.mark.asyncio
    async def test_lagging_book_that_shows_a_cancelled_stop_live_clears_on_the_reread(self):
        sweep = await executor.cancel_live_protective_orders(
            "HINDALCO", "EVWAP-EARLY-PY", "SELL",
            fetch_book=AsyncMock(side_effect=[[_stop("A")], [_stop("A")], []]),
            cancel=AsyncMock(return_value=True), settle_s=0,
        )
        assert sweep.clear is True
