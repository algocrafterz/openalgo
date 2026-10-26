"""Protective-stop lifecycle: failed cancels, pending duplicates, adoption.

2026-10-15 INFY (EVWAP-EARLY-PY, 48 shares): the OpenAlgo server stalled, every stop request
timed out and the verify reads found nothing, so nine SELL SL-M orders landed late on a 48-share
position. The duplicate cleanup's cancel requests also timed out, the log still said the
duplicates were "cancelled", and nothing ever looked at them again. LICI: a retry adopted one
resting stop and returned, leaving its four twins live until the exit.

Rules under test:
  * A cancel REQUEST result is not an order state. Only a fresh order-book read is.
  * A duplicate that is not proved gone stays on a pending list and is retried.
  * Cleanup never removes the last live stop of a position.
  * Stop submissions for one strategy/symbol/side are serialised.
  * Adopting a resting stop removes its twins.
"""

import asyncio
import dataclasses
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import executor
from signal_engine.config import settings as _real_settings
from signal_engine.models import Direction, OrderStatus, TradeResult

TIMEOUT = TradeResult(order_id="", status=OrderStatus.TIMEOUT, message="Request timed out")
KEY = ("EVWAP-EARLY-PY", "INFY", "SELL")
_REAL_SLEEP = asyncio.sleep  # the autouse fixture below replaces asyncio.sleep


def _ok(order_id):
    return TradeResult(order_id=order_id, status=OrderStatus.SUCCESS, message="ok")


def _stop(order_id, status="trigger pending", **extra):
    row = {
        "orderid": order_id,
        "symbol": "INFY",
        "action": "SELL",
        "quantity": 48,
        "pricetype": "SL-M",
        "trigger_price": 1095.4,
        "order_status": status,
        "strategy": "EVWAP-EARLY-PY",
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
def _clean_state():
    executor._UNCONFIRMED_SL.clear()
    executor._PENDING_DUPLICATE_STOPS.clear()
    executor._LAST_UNKNOWN_SEND.clear()
    executor._EPISODE_START.clear()
    yield
    executor._UNCONFIRMED_SL.clear()
    executor._PENDING_DUPLICATE_STOPS.clear()
    executor._LAST_UNKNOWN_SEND.clear()
    executor._EPISODE_START.clear()


@pytest.fixture(autouse=True)
def _no_real_sleep():
    with patch.object(executor.asyncio, "sleep", AsyncMock()):
        yield


async def _sweep(book, cancel, keep_id=""):
    return await executor.cancel_live_protective_orders(
        "INFY", "EVWAP-EARLY-PY", "SELL", keep_id=keep_id, fetch_book=book, cancel=cancel
    )


async def _place(**kw):
    args = {
        "symbol": "INFY",
        "exchange": "NSE",
        "direction": Direction.LONG,
        "quantity": 48,
        "sl_price": 1095.3,
        "product": "MIS",
        "strategy_tag": "EVWAP-EARLY-PY",
    }
    args.update(kw)
    return await executor.place_sl_order(**args)


class TestCancelResultIsNotOrderState:
    @pytest.mark.asyncio
    async def test_failed_cancel_request_but_book_shows_cancelled_is_confirmed(self):
        book = AsyncMock(
            side_effect=[[_stop("A"), _stop("K")], [_stop("A", "cancelled"), _stop("K")]]
        )
        sweep = await _sweep(book, AsyncMock(return_value=False), keep_id="K")
        assert sweep.clear is True
        assert sweep.confirmed_cancelled == ("A",)
        assert sweep.unresolved == ()

    @pytest.mark.asyncio
    async def test_successful_cancel_request_but_order_still_live_is_unresolved(self):
        live = [_stop("A"), _stop("K")]
        book = AsyncMock(side_effect=[live, live, live])
        sweep = await _sweep(book, AsyncMock(return_value=True), keep_id="K")
        assert sweep.clear is False
        assert sweep.confirmed_cancelled == ()
        assert sweep.unresolved == ("A",)

    @pytest.mark.asyncio
    async def test_unreadable_book_after_cancel_leaves_every_target_unresolved(self):
        book = AsyncMock(side_effect=[[_stop("A"), _stop("B"), _stop("K")], None])
        sweep = await _sweep(book, AsyncMock(return_value=True), keep_id="K")
        assert sweep.readable is False
        assert sweep.clear is False
        assert sweep.confirmed_cancelled == ()
        assert set(sweep.unresolved) == {"A", "B"}

    @pytest.mark.asyncio
    async def test_a_filled_target_is_neither_confirmed_cancelled_nor_unresolved(self):
        book = AsyncMock(
            side_effect=[[_stop("A"), _stop("K")], [_stop("A", "complete"), _stop("K")]]
        )
        sweep = await _sweep(book, AsyncMock(return_value=False), keep_id="K")
        assert sweep.executed == ("A",)
        assert sweep.confirmed_cancelled == ()
        assert sweep.unresolved == ()


class TestDuplicateCleanupNeverClaimsWhatItCannotProve:
    @pytest.mark.asyncio
    async def test_failed_cancels_are_not_logged_as_cancelled_and_stay_pending(self):
        live = [_stop("A"), _stop("B"), _stop("K")]
        with (
            patch.object(executor, "settings", _cfg()),
            patch("signal_engine.api_client.fetch_orderbook", AsyncMock(return_value=live)),
            patch("signal_engine.api_client.cancel_order", AsyncMock(return_value=False)),
            patch.object(executor.logger, "warning") as warn,
            patch.object(executor.logger, "error") as err,
        ):
            await executor._remove_duplicate_stops("INFY", "EVWAP-EARLY-PY", "SELL", "K")
        assert not any("cancelled" in str(c.args[0]).lower() for c in warn.call_args_list)
        assert any("unresolved" in str(c.args[0]).lower() for c in err.call_args_list)
        assert executor._PENDING_DUPLICATE_STOPS.get(KEY).keep_id == "K"

    @pytest.mark.asyncio
    async def test_proved_cleanup_is_logged_as_confirmed_and_leaves_nothing_pending(self):
        book = AsyncMock(
            side_effect=[[_stop("A"), _stop("K")], [_stop("A", "cancelled"), _stop("K")]]
        )
        with (
            patch.object(executor, "settings", _cfg()),
            patch("signal_engine.api_client.fetch_orderbook", book),
            patch("signal_engine.api_client.cancel_order", AsyncMock(return_value=True)),
            patch.object(executor.logger, "warning") as warn,
        ):
            await executor._remove_duplicate_stops("INFY", "EVWAP-EARLY-PY", "SELL", "K")
        assert any("confirmed cancelled" in str(c.args[0]).lower() for c in warn.call_args_list)
        assert not executor._PENDING_DUPLICATE_STOPS

    @pytest.mark.asyncio
    async def test_a_twin_that_filled_is_reported_critically(self):
        book = AsyncMock(
            side_effect=[[_stop("A"), _stop("K")], [_stop("A", "complete"), _stop("K")]]
        )
        with (
            patch.object(executor, "settings", _cfg()),
            patch("signal_engine.api_client.fetch_orderbook", book),
            patch("signal_engine.api_client.cancel_order", AsyncMock(return_value=False)),
            patch.object(executor.logger, "error") as err,
        ):
            await executor._remove_duplicate_stops("INFY", "EVWAP-EARLY-PY", "SELL", "K")
        assert any("filled" in str(c.args[0]).lower() for c in err.call_args_list)


class TestPendingDuplicateRecovery:
    def _pend(self, keep_id="K"):
        executor._PENDING_DUPLICATE_STOPS.add(KEY, keep_id)

    @pytest.mark.asyncio
    async def test_retry_clears_the_duplicates_once_the_server_recovers(self):
        self._pend()
        book = AsyncMock(
            side_effect=[
                [_stop("A"), _stop("B"), _stop("K")],
                [_stop("A", "cancelled"), _stop("B", "cancelled"), _stop("K")],
            ]
        )
        cancel = AsyncMock(return_value=True)
        with (
            patch("signal_engine.api_client.fetch_orderbook", book),
            patch("signal_engine.api_client.cancel_order", cancel),
        ):
            done = await executor.resolve_pending_duplicate_stops(force=True)
        assert {c.args[0] for c in cancel.await_args_list} == {"A", "B"}
        assert done == [KEY]
        assert not executor._PENDING_DUPLICATE_STOPS
        assert book.await_count == 2  # the resolver's read is reused by the sweep

    @pytest.mark.asyncio
    async def test_unreadable_book_cancels_nothing_and_stays_pending(self):
        self._pend()
        cancel = AsyncMock(return_value=True)
        with (
            patch("signal_engine.api_client.fetch_orderbook", AsyncMock(return_value=None)),
            patch("signal_engine.api_client.cancel_order", cancel),
        ):
            done = await executor.resolve_pending_duplicate_stops(force=True)
        cancel.assert_not_awaited()
        assert done == []
        assert KEY in executor._PENDING_DUPLICATE_STOPS

    @pytest.mark.asyncio
    async def test_still_unproven_after_a_retry_stays_pending(self):
        self._pend()
        live = [_stop("A"), _stop("K")]
        with (
            patch("signal_engine.api_client.fetch_orderbook", AsyncMock(return_value=live)),
            patch("signal_engine.api_client.cancel_order", AsyncMock(return_value=False)),
        ):
            done = await executor.resolve_pending_duplicate_stops(force=True)
        assert done == []
        assert KEY in executor._PENDING_DUPLICATE_STOPS

    @pytest.mark.asyncio
    async def test_kept_stop_gone_promotes_a_duplicate_and_never_cancels_the_last_stop(self):
        """K was cancelled or filled meanwhile. A and B are the only live protection: keep one."""
        self._pend("K")
        book = AsyncMock(
            side_effect=[
                [_stop("K", "cancelled"), _stop("A"), _stop("B")],
                [_stop("K", "cancelled"), _stop("A"), _stop("B", "cancelled")],
            ]
        )
        cancel = AsyncMock(return_value=True)
        with (
            patch("signal_engine.api_client.fetch_orderbook", book),
            patch("signal_engine.api_client.cancel_order", cancel),
        ):
            await executor.resolve_pending_duplicate_stops(force=True)
        assert [c.args[0] for c in cancel.await_args_list] == ["B"]

    @pytest.mark.asyncio
    async def test_a_single_live_stop_is_never_cancelled(self):
        self._pend("")
        cancel = AsyncMock(return_value=True)
        with (
            patch("signal_engine.api_client.fetch_orderbook", AsyncMock(return_value=[_stop("A")])),
            patch("signal_engine.api_client.cancel_order", cancel),
        ):
            done = await executor.resolve_pending_duplicate_stops(force=True)
        cancel.assert_not_awaited()
        assert done == [KEY]

    @pytest.mark.asyncio
    async def test_another_strategys_stop_is_never_cancelled(self):
        self._pend("K")
        live = [_stop("K"), _stop("A"), _stop("P1", strategy="BREAKOUT-PY")]
        after = [_stop("K"), _stop("A", "cancelled"), _stop("P1", strategy="BREAKOUT-PY")]
        cancel = AsyncMock(return_value=True)
        with (
            patch("signal_engine.api_client.fetch_orderbook", AsyncMock(side_effect=[live, after])),
            patch("signal_engine.api_client.cancel_order", cancel),
        ):
            await executor.resolve_pending_duplicate_stops(force=True)
        assert [c.args[0] for c in cancel.await_args_list] == ["A"]

    @pytest.mark.asyncio
    async def test_retries_are_rate_limited_per_key(self):
        self._pend()
        book = AsyncMock(return_value=[_stop("A"), _stop("K")])
        with (
            patch("signal_engine.api_client.fetch_orderbook", book),
            patch("signal_engine.api_client.cancel_order", AsyncMock(return_value=False)),
        ):
            await executor.resolve_pending_duplicate_stops()
            reads_after_first = book.await_count
            await executor.resolve_pending_duplicate_stops()
        assert book.await_count == reads_after_first

    @pytest.mark.asyncio
    async def test_empty_registry_costs_no_api_call(self):
        book = AsyncMock(return_value=[])
        with patch("signal_engine.api_client.fetch_orderbook", book):
            assert await executor.resolve_pending_duplicate_stops() == []
        book.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_network_error_during_the_read_cancels_nothing_and_stays_pending(self):
        """Recovery after an interruption: an exception from the read leaves the key pending."""
        self._pend()
        cancel = AsyncMock(return_value=True)
        with (
            patch(
                "signal_engine.api_client.fetch_orderbook",
                AsyncMock(side_effect=RuntimeError("net")),
            ),
            patch("signal_engine.api_client.cancel_order", cancel),
        ):
            with pytest.raises(RuntimeError):
                await executor.resolve_pending_duplicate_stops(force=True)
        cancel.assert_not_awaited()
        assert KEY in executor._PENDING_DUPLICATE_STOPS


class TestStopsThatLandAfterTheClientGaveUp:
    @pytest.mark.asyncio
    async def test_exhausted_retries_leave_a_pending_entry_that_dedupes_late_landings(self):
        """INFY 09:45-09:47: every request timed out, none was visible, all landed late."""
        send = AsyncMock(return_value=TIMEOUT)
        with (
            patch.object(executor, "settings", _cfg()),
            patch.object(executor, "send_order", send),
            patch("signal_engine.api_client.fetch_orderbook", AsyncMock(return_value=[])),
        ):
            result = await _place()
        assert result.status != OrderStatus.SUCCESS
        assert executor._PENDING_DUPLICATE_STOPS.get(KEY).keep_id == ""

        landed = [_stop("S1"), _stop("S2"), _stop("S3")]
        book = AsyncMock(
            side_effect=[landed, [_stop("S1"), _stop("S2", "cancelled"), _stop("S3", "cancelled")]]
        )
        cancel = AsyncMock(return_value=True)
        with (
            patch("signal_engine.api_client.fetch_orderbook", book),
            patch("signal_engine.api_client.cancel_order", cancel),
        ):
            await executor.resolve_pending_duplicate_stops(force=True)
        assert [c.args[0] for c in cancel.await_args_list] == ["S2", "S3"]
        assert not executor._PENDING_DUPLICATE_STOPS


class TestAdoptionRemovesTwins:
    @pytest.mark.asyncio
    async def test_adopting_one_resting_stop_cancels_the_rest(self):
        """LICI: five stops landed together; one was adopted and four stayed live."""
        executor._UNCONFIRMED_SL.add(KEY)
        live = [_stop("A"), _stop("B"), _stop("C")]
        after = [_stop("A"), _stop("B", "cancelled"), _stop("C", "cancelled")]
        cancel = AsyncMock(return_value=True)
        send = AsyncMock(return_value=_ok("NEW"))
        with (
            patch.object(executor, "settings", _cfg()),
            patch.object(executor, "send_order", send),
            patch(
                "signal_engine.api_client.fetch_orderbook",
                AsyncMock(side_effect=[live, live, after]),
            ),
            patch("signal_engine.api_client.cancel_order", cancel),
        ):
            result = await _place()
        assert result.order_id == "A"
        send.assert_not_awaited()
        assert {c.args[0] for c in cancel.await_args_list} == {"B", "C"}
        assert KEY not in executor._UNCONFIRMED_SL

    @pytest.mark.asyncio
    async def test_adoption_with_unprovable_cleanup_keeps_the_pending_mark(self):
        executor._UNCONFIRMED_SL.add(KEY)
        live = [_stop("A"), _stop("B")]
        with (
            patch.object(executor, "settings", _cfg()),
            patch.object(executor, "send_order", AsyncMock()),
            patch("signal_engine.api_client.fetch_orderbook", AsyncMock(return_value=live)),
            patch("signal_engine.api_client.cancel_order", AsyncMock(return_value=False)),
        ):
            result = await _place()
        assert result.order_id == "A"
        assert KEY in executor._PENDING_DUPLICATE_STOPS


REJECTED = TradeResult(order_id="", status=OrderStatus.REJECTED, message="rejected by broker")


class TestUnknownOutcomeIsNotConfirmedFailure:
    """A timeout is UNKNOWN (the order may be live); a rejection is a CONFIRMED failure."""

    async def _run(self, send, book=None, **cfg):
        with (
            patch.object(executor, "settings", _cfg(**cfg)),
            patch.object(executor, "send_order", send),
            patch("signal_engine.api_client.fetch_orderbook", book or AsyncMock(return_value=[])),
        ):
            return await _place()

    @pytest.mark.asyncio
    async def test_unknown_outcomes_are_capped_per_cycle(self):
        send = AsyncMock(return_value=TIMEOUT)
        result = await self._run(send)
        assert result.status == OrderStatus.TIMEOUT
        assert send.await_count == executor._MAX_UNKNOWN_SENDS == 2
        assert KEY in executor._UNCONFIRMED_SL  # the next cycle reconciles before any resend
        assert KEY in executor._PENDING_DUPLICATE_STOPS  # and the key stays monitored

    @pytest.mark.asyncio
    async def test_the_cap_is_a_tunable_policy_value(self):
        send = AsyncMock(return_value=TIMEOUT)
        with patch.object(executor, "_MAX_UNKNOWN_SENDS", 3):
            await self._run(send)
        assert send.await_count == 3

    @pytest.mark.asyncio
    async def test_confirmed_failures_still_retry_to_exhaustion(self):
        send = AsyncMock(return_value=REJECTED)
        result = await self._run(send)
        assert result.status == OrderStatus.REJECTED
        assert send.await_count == 5
        assert KEY not in executor._UNCONFIRMED_SL
        assert KEY not in executor._PENDING_DUPLICATE_STOPS

    @pytest.mark.asyncio
    async def test_a_confirmed_failure_after_an_unknown_one_keeps_retrying_safely(self):
        send = AsyncMock(side_effect=[TIMEOUT, REJECTED, REJECTED, _ok("B")])
        book = AsyncMock(side_effect=lambda *a, **k: [_stop("B")] if send.await_count >= 4 else [])
        result = await self._run(send, book=book)
        assert result.order_id == "B"
        assert send.await_count == 4

    @pytest.mark.asyncio
    async def test_the_next_cycle_holds_while_the_last_unknown_send_is_recent(self):
        send = AsyncMock(return_value=TIMEOUT)
        await self._run(send)
        sent = send.await_count
        result = await self._run(send)
        assert result.status == OrderStatus.TIMEOUT
        assert result.message == executor.HOLD_MESSAGE
        assert send.await_count == sent

    @pytest.mark.asyncio
    async def test_after_the_grace_window_the_next_cycle_reconciles_then_sends(self):
        send = AsyncMock(side_effect=[TIMEOUT, TIMEOUT, _ok("LATE")])
        await self._run(send)
        executor._LAST_UNKNOWN_SEND[KEY] -= executor._RESEND_GRACE_S + 1
        result = await self._run(send)
        assert result.order_id == "LATE"
        assert send.await_count == 3
        assert KEY not in executor._LAST_UNKNOWN_SEND

    @pytest.mark.asyncio
    async def test_the_next_cycle_adopts_what_landed_while_it_held_and_removes_twins(self):
        send = AsyncMock(return_value=TIMEOUT)
        await self._run(send)
        sent = send.await_count
        landed = [_stop("A"), _stop("B")]
        after = [_stop("A"), _stop("B", "cancelled")]
        cancel = AsyncMock(return_value=True)
        with patch("signal_engine.api_client.cancel_order", cancel):
            result = await self._run(send, book=AsyncMock(side_effect=[landed, landed, after]))
        assert result.order_id == "A"
        assert send.await_count == sent
        assert [c.args[0] for c in cancel.await_args_list] == ["B"]

    @pytest.mark.asyncio
    async def test_an_unreadable_book_never_leads_to_a_resend(self):
        send = AsyncMock(return_value=TIMEOUT)
        await self._run(send)
        executor._LAST_UNKNOWN_SEND[KEY] -= executor._RESEND_GRACE_S + 1
        sent = send.await_count
        result = await self._run(send, book=AsyncMock(return_value=None))
        assert result.status == OrderStatus.TIMEOUT
        assert send.await_count == sent


class TestAdoptRestingStopWithoutSending:
    """Reconcile-only recovery for a position whose stop retries are exhausted."""

    async def _adopt(self, book, cancel=None):
        with (
            patch.object(executor, "settings", _cfg()),
            patch.object(
                executor, "send_order", AsyncMock(side_effect=AssertionError("must not send"))
            ),
            patch("signal_engine.api_client.cancel_order", cancel or AsyncMock(return_value=True)),
        ):
            return await executor.adopt_resting_stop(
                symbol="INFY",
                exchange="NSE",
                direction=Direction.LONG,
                quantity=48,
                sl_price=1095.3,
                product="MIS",
                strategy_tag="EVWAP-EARLY-PY",
                fetch_book=book,
            )

    @pytest.mark.asyncio
    async def test_adopts_a_stop_that_landed_late(self):
        got = await self._adopt(AsyncMock(return_value=[_stop("A")]))
        assert got.order_id == "A"

    @pytest.mark.asyncio
    async def test_returns_none_when_nothing_is_on_the_book(self):
        assert await self._adopt(AsyncMock(return_value=[])) is None

    @pytest.mark.asyncio
    async def test_returns_none_when_the_book_is_unreadable(self):
        assert await self._adopt(AsyncMock(return_value=None)) is None

    @pytest.mark.asyncio
    async def test_removes_twins_of_the_adopted_stop(self):
        cancel = AsyncMock(return_value=True)
        scan_read = AsyncMock(return_value=[_stop("A"), _stop("B")])
        cleanup_reads = AsyncMock(
            side_effect=[[_stop("A"), _stop("B")], [_stop("A"), _stop("B", "cancelled")]]
        )
        with patch("signal_engine.api_client.fetch_orderbook", cleanup_reads):
            got = await self._adopt(scan_read, cancel)
        assert got.order_id == "A"
        assert [c.args[0] for c in cancel.await_args_list] == ["B"]


class TestRestartRecoveryFindsDuplicatesWithoutMemory:
    def test_exact_twins_are_grouped(self):
        book = [_stop("A"), _stop("B"), _stop("C", symbol="LT")]
        assert executor.find_duplicate_stop_groups(book) == {KEY: ["A", "B"]}

    def test_stops_that_differ_are_not_duplicates(self):
        assert not executor.find_duplicate_stop_groups(
            [_stop("A"), _stop("B", trigger_price=1090.0)]
        )
        assert not executor.find_duplicate_stop_groups([_stop("A"), _stop("B", quantity=24)])

    def test_a_key_with_twins_and_a_different_stop_is_left_alone(self):
        book = [_stop("A"), _stop("B"), _stop("C", trigger_price=1090.0)]
        assert not executor.find_duplicate_stop_groups(book)

    def test_dead_and_foreign_rows_are_ignored(self):
        book = [
            _stop("A"),
            _stop("B", "cancelled"),
            _stop("C", strategy="BREAKOUT-PY"),
            {k: v for k, v in _stop("D").items() if k != "strategy"},
        ]
        assert not executor.find_duplicate_stop_groups(book)

    @pytest.mark.asyncio
    async def test_flagged_groups_are_cleaned_by_the_resolver_after_a_restart(self):
        assert not executor._PENDING_DUPLICATE_STOPS  # a fresh process remembers nothing
        book = [_stop("A"), _stop("B"), _stop("C")]
        executor.flag_duplicate_stop_groups(book, keep_ids={KEY: "B"})
        after = [_stop("A", "cancelled"), _stop("B"), _stop("C", "cancelled")]
        cancel = AsyncMock(return_value=True)
        with (
            patch("signal_engine.api_client.fetch_orderbook", AsyncMock(side_effect=[book, after])),
            patch("signal_engine.api_client.cancel_order", cancel),
        ):
            done = await executor.resolve_pending_duplicate_stops(force=True)
        assert done == [KEY]
        assert {c.args[0] for c in cancel.await_args_list} == {"A", "C"}

    def test_flagging_is_idempotent_and_prefers_the_tracked_stop(self):
        book = [_stop("A"), _stop("B")]
        executor.flag_duplicate_stop_groups(book, keep_ids={KEY: "B"})
        executor.flag_duplicate_stop_groups(book, keep_ids={})
        assert executor._PENDING_DUPLICATE_STOPS.get(KEY).keep_id == "B"


class TestKeepLagAndLastStop:
    @pytest.mark.asyncio
    async def test_a_lone_live_stop_with_the_kept_one_not_yet_visible_stays_pending(self):
        executor._PENDING_DUPLICATE_STOPS.add(KEY, "K")
        with patch(
            "signal_engine.api_client.fetch_orderbook", AsyncMock(return_value=[_stop("X")])
        ):
            done = await executor.resolve_pending_duplicate_stops(force=True)
        assert done == []
        assert KEY in executor._PENDING_DUPLICATE_STOPS

    @pytest.mark.asyncio
    async def test_the_kept_stop_arriving_later_leaves_two_and_one_is_removed(self):
        executor._PENDING_DUPLICATE_STOPS.add(KEY, "K")
        cancel = AsyncMock(return_value=True)
        with (
            patch("signal_engine.api_client.fetch_orderbook", AsyncMock(return_value=[_stop("X")])),
            patch("signal_engine.api_client.cancel_order", cancel),
        ):
            await executor.resolve_pending_duplicate_stops(force=True)
        landed = [_stop("X"), _stop("K")]
        after = [_stop("X", "cancelled"), _stop("K")]
        with (
            patch(
                "signal_engine.api_client.fetch_orderbook", AsyncMock(side_effect=[landed, after])
            ),
            patch("signal_engine.api_client.cancel_order", cancel),
        ):
            done = await executor.resolve_pending_duplicate_stops(force=True)
        assert done == [KEY]
        assert [c.args[0] for c in cancel.await_args_list] == ["X"]

    @pytest.mark.asyncio
    async def test_a_terminal_kept_stop_lets_the_lone_live_stop_stand_and_settles(self):
        executor._PENDING_DUPLICATE_STOPS.add(KEY, "K")
        cancel = AsyncMock(return_value=True)
        book = [_stop("K", "cancelled"), _stop("X")]
        with (
            patch("signal_engine.api_client.fetch_orderbook", AsyncMock(return_value=book)),
            patch("signal_engine.api_client.cancel_order", cancel),
        ):
            done = await executor.resolve_pending_duplicate_stops(force=True)
        assert done == [KEY]
        cancel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_watch_is_dropped_loudly_once_the_ttl_passes(self):
        executor._PENDING_DUPLICATE_STOPS.add(KEY, "K")
        executor._PENDING_DUPLICATE_STOPS.get(KEY).since -= executor._KEEP_LAG_TTL_S + 1
        with (
            patch("signal_engine.api_client.fetch_orderbook", AsyncMock(return_value=[_stop("X")])),
            patch.object(executor.logger, "error") as err,
        ):
            done = await executor.resolve_pending_duplicate_stops(force=True)
        assert done == [KEY]
        assert any("never appeared" in str(c.args[0]) for c in err.call_args_list)


class TestEscalation:
    @pytest.mark.asyncio
    async def test_repeated_failures_escalate_once_and_the_key_stays_pending(self):
        executor._PENDING_DUPLICATE_STOPS.add(KEY, "K")
        live = [_stop("A"), _stop("K")]
        with (
            patch("signal_engine.api_client.fetch_orderbook", AsyncMock(return_value=live)),
            patch("signal_engine.api_client.cancel_order", AsyncMock(return_value=False)),
        ):
            for _ in range(executor._ESCALATE_AFTER_FAILURES):
                assert executor.escalations() == []
                await executor.resolve_pending_duplicate_stops(force=True)
        assert executor.escalations() == [KEY]
        assert executor.escalations() == []  # handed out once
        assert KEY in executor._PENDING_DUPLICATE_STOPS

    @pytest.mark.asyncio
    async def test_unreadable_books_also_count_as_failures(self):
        executor._PENDING_DUPLICATE_STOPS.add(KEY, "K")
        with patch("signal_engine.api_client.fetch_orderbook", AsyncMock(return_value=None)):
            for _ in range(executor._ESCALATE_AFTER_FAILURES):
                await executor.resolve_pending_duplicate_stops(force=True)
        assert executor.escalations() == [KEY]


class TestUnconfirmedFlagIsKeptUntilCleanupIsProven:
    @pytest.mark.asyncio
    async def test_adoption_with_unprovable_cleanup_keeps_the_flag(self):
        executor._UNCONFIRMED_SL.add(KEY)
        live = [_stop("A"), _stop("B")]
        with (
            patch.object(executor, "settings", _cfg()),
            patch.object(executor, "send_order", AsyncMock()),
            patch("signal_engine.api_client.fetch_orderbook", AsyncMock(return_value=live)),
            patch("signal_engine.api_client.cancel_order", AsyncMock(return_value=False)),
        ):
            await _place()
        assert KEY in executor._UNCONFIRMED_SL

    @pytest.mark.asyncio
    async def test_a_successful_send_with_unprovable_cleanup_keeps_the_flag(self):
        send = AsyncMock(side_effect=[TIMEOUT, _ok("B")])
        live = [_stop("A"), _stop("B")]
        book = AsyncMock(side_effect=[[], [], [], live, live, live, live, live, live])
        with (
            patch.object(executor, "settings", _cfg()),
            patch.object(executor, "send_order", send),
            patch("signal_engine.api_client.fetch_orderbook", book),
            patch("signal_engine.api_client.cancel_order", AsyncMock(return_value=False)),
        ):
            result = await _place()
        assert result.order_id == "B"
        assert KEY in executor._UNCONFIRMED_SL

    @pytest.mark.asyncio
    async def test_proved_cleanup_clears_the_flag(self):
        executor._UNCONFIRMED_SL.add(KEY)
        live = [_stop("A"), _stop("B")]
        after = [_stop("A"), _stop("B", "cancelled")]
        with (
            patch.object(executor, "settings", _cfg()),
            patch.object(executor, "send_order", AsyncMock()),
            patch(
                "signal_engine.api_client.fetch_orderbook",
                AsyncMock(side_effect=[live, live, after]),
            ),
            patch("signal_engine.api_client.cancel_order", AsyncMock(return_value=True)),
        ):
            await _place()
        assert KEY not in executor._UNCONFIRMED_SL


class TestStopThatFiredDuringTheTimeout:
    """A stop that landed and was triggered before the verify read must be adopted: a new stop on
    a position the broker already closed would open a fresh one when price moves."""

    async def _run(self, send, book):
        with (
            patch.object(executor, "settings", _cfg()),
            patch.object(executor, "send_order", send),
            patch("signal_engine.api_client.fetch_orderbook", book),
            patch("signal_engine.api_client.cancel_order", AsyncMock(return_value=True)),
        ):
            return await _place()

    @pytest.mark.asyncio
    async def test_a_stop_that_already_fired_is_adopted_not_resent(self):
        send = AsyncMock(return_value=TIMEOUT)
        fired = [_stop("A", "complete", timestamp="2099-01-01 09:47:51")]
        result = await self._run(send, AsyncMock(return_value=fired))
        assert result.order_id == "A"
        assert send.await_count == 1

    @pytest.mark.asyncio
    async def test_a_completed_stop_from_before_this_episode_is_not_adopted(self):
        send = AsyncMock(side_effect=[TIMEOUT, _ok("NEW")])
        old = [_stop("OLD", "complete", timestamp="2000-01-01 09:20:00")]
        result = await self._run(send, AsyncMock(return_value=old))
        assert result.order_id == "NEW"

    @pytest.mark.asyncio
    async def test_a_completed_stop_without_a_timestamp_is_not_adopted(self):
        send = AsyncMock(side_effect=[TIMEOUT, _ok("NEW")])
        result = await self._run(send, AsyncMock(return_value=[_stop("A", "complete")]))
        assert result.order_id == "NEW"

    @pytest.mark.asyncio
    async def test_a_stop_that_fires_between_cycles_is_adopted_by_the_next_cycle(self):
        send = AsyncMock(return_value=TIMEOUT)
        await self._run(send, AsyncMock(return_value=[]))
        sent = send.await_count
        executor._LAST_UNKNOWN_SEND[KEY] -= executor._RESEND_GRACE_S + 1
        fired = [_stop("A", "complete", timestamp="2099-01-01 09:49:00")]
        result = await self._run(send, AsyncMock(return_value=fired))
        assert result.order_id == "A"
        assert send.await_count == sent


class _FakeBroker:
    """A stateful broker: stops land late, cancels may time out client-side yet still apply."""

    def __init__(self):
        self.orders = []
        self.cancel_calls = []
        self.apply_cancels_from_call = 0  # cancel requests before this many calls are ignored
        self.cancel_ok = False  # what the client sees from cancel_order

    def land(self, *ids):
        self.orders.extend(_stop(i) for i in ids)

    def live(self):
        return [o["orderid"] for o in self.orders if o["order_status"] == "trigger pending"]

    async def fetch_orderbook(self):
        return [dict(o) for o in self.orders]

    async def cancel_order(self, order_id, strategy):
        self.cancel_calls.append(order_id)
        if len(self.cancel_calls) > self.apply_cancels_from_call:
            for o in self.orders:
                if o["orderid"] == order_id:
                    o["order_status"] = "cancelled"
        return self.cancel_ok


class TestInfyScenarioAgainstAStatefulBroker:
    @pytest.mark.asyncio
    async def test_nine_late_stops_end_as_exactly_one_even_though_cancels_report_failure(self):
        broker = _FakeBroker()
        send = AsyncMock(return_value=TIMEOUT)
        with (
            patch.object(executor, "settings", _cfg()),
            patch.object(executor, "send_order", send),
            patch("signal_engine.api_client.fetch_orderbook", broker.fetch_orderbook),
            patch("signal_engine.api_client.cancel_order", broker.cancel_order),
        ):
            result = await _place()
            assert result.status != OrderStatus.SUCCESS
            broker.land(*[f"S{i}" for i in range(9)])  # the stalled server catches up
            assert len(broker.live()) == 9
            done = await executor.resolve_pending_duplicate_stops(force=True)
        assert done == [KEY]
        assert len(broker.live()) == 1  # never zero: the last stop is never cancelled
        assert not executor._PENDING_DUPLICATE_STOPS

    @pytest.mark.asyncio
    async def test_cancels_that_only_apply_on_a_later_pass_are_retried_until_proven(self):
        broker = _FakeBroker()
        broker.land("A", "B", "K")
        broker.apply_cancels_from_call = 2  # the first two cancel requests are lost
        executor._PENDING_DUPLICATE_STOPS.add(KEY, "K")
        with (
            patch("signal_engine.api_client.fetch_orderbook", broker.fetch_orderbook),
            patch("signal_engine.api_client.cancel_order", broker.cancel_order),
        ):
            first = await executor.resolve_pending_duplicate_stops(force=True)
            assert first == []
            assert KEY in executor._PENDING_DUPLICATE_STOPS
            second = await executor.resolve_pending_duplicate_stops(force=True)
        assert second == [KEY]
        assert broker.live() == ["K"]

    @pytest.mark.asyncio
    async def test_a_late_ack_after_a_second_pass_does_not_create_a_second_cleanup(self):
        broker = _FakeBroker()
        broker.land("A", "K")
        executor._PENDING_DUPLICATE_STOPS.add(KEY, "K")
        with (
            patch("signal_engine.api_client.fetch_orderbook", broker.fetch_orderbook),
            patch("signal_engine.api_client.cancel_order", broker.cancel_order),
        ):
            await executor.resolve_pending_duplicate_stops(force=True)
            calls = len(broker.cancel_calls)
            again = await executor.resolve_pending_duplicate_stops(force=True)
        assert again == []  # nothing pending any more
        assert len(broker.cancel_calls) == calls
        assert broker.live() == ["K"]


class TestStopSubmissionIsSerialised:
    @pytest.mark.asyncio
    async def test_two_concurrent_submissions_for_one_key_never_overlap(self):
        in_flight = 0
        peak = 0
        gate = asyncio.get_running_loop().create_future()

        async def slow_send(order):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await gate
            in_flight -= 1
            return _ok("X")

        with (
            patch.object(executor, "settings", _cfg()),
            patch.object(executor, "send_order", slow_send),
        ):
            first = asyncio.ensure_future(_place())
            second = asyncio.ensure_future(_place())
            for _ in range(5):
                await _REAL_SLEEP(0)
            gate.set_result(None)
            await asyncio.gather(first, second)
        assert peak == 1


class TestTrackerPollRetriesPendingDuplicates:
    @pytest.mark.asyncio
    async def test_the_poll_runs_the_retry_even_when_the_positionbook_cannot_be_read(self):
        from unittest.mock import MagicMock

        from signal_engine.tracker import PositionTracker

        t = PositionTracker(MagicMock())
        t._note_positionbook_failure = AsyncMock()
        retry = AsyncMock(return_value=[])
        with (
            patch("signal_engine.main.sweep_unresolved_orders", AsyncMock()),
            patch("signal_engine.tracker.resolve_pending_duplicate_stops", retry),
            patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=None)),
        ):
            await t.check_positions()
        retry.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_failing_retry_does_not_break_the_poll(self):
        from unittest.mock import MagicMock

        from signal_engine.tracker import PositionTracker

        t = PositionTracker(MagicMock())
        t._note_positionbook_failure = AsyncMock()
        with (
            patch("signal_engine.main.sweep_unresolved_orders", AsyncMock()),
            patch(
                "signal_engine.tracker.resolve_pending_duplicate_stops",
                AsyncMock(side_effect=RuntimeError("x")),
            ),
            patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=None)),
        ):
            await t.check_positions()
        t._note_positionbook_failure.assert_awaited_once()
