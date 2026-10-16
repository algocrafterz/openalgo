"""Tracker side of the protective-stop lifecycle (executor side: test_stop_pending_duplicates.py).

* Restart recovery: the pending list is in memory, so the poll also audits the order book for
  exact-twin stops, which finds duplicates a restarted process knows nothing about.
* Repeated reconcile failure is escalated to the operator once, and never breaks the poll.
* A position whose stop retries are exhausted is not abandoned: it is reconciled (no send) and
  re-attempted on a slow cycle, with an alert each time it is still unprotected.
* A "held" result (an earlier submission is unresolved) does not use up a re-attempt.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import executor
from signal_engine.config import settings
from signal_engine.models import OrderStatus, TradeResult
from signal_engine.strategies import ORB
from signal_engine.tests.tracker_fixtures import _AGED, _make_engine, _make_position
from signal_engine.timeutils import IST
from signal_engine.tracker import PositionTracker


def _stop(order_id, status="trigger pending", **extra):
    row = {
        "orderid": order_id,
        "symbol": "RELIANCE",
        "action": "SELL",
        "quantity": 50,
        "pricetype": "SL-M",
        "trigger_price": 2480.0,
        "order_status": status,
        "strategy": "ORB",
        "product": "MIS",
    }
    row.update(extra)
    return row


KEY = ("ORB", "RELIANCE", "SELL")


def _book(qty: int = 50) -> list:
    return [{"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "quantity": qty, "pnl": 0}]


@pytest.fixture(autouse=True)
def _clean():
    executor._PENDING_DUPLICATE_STOPS.clear()
    executor._LAST_UNKNOWN_SEND.clear()
    yield
    executor._PENDING_DUPLICATE_STOPS.clear()
    executor._LAST_UNKNOWN_SEND.clear()


def _tracker():
    engine = _make_engine()
    engine._state(ORB).open_positions = 1
    t = PositionTracker(engine)
    t._note_positionbook_failure = AsyncMock()
    return t


class TestRestartAudit:
    @pytest.mark.asyncio
    async def test_twins_on_the_book_are_removed_with_no_prior_in_memory_state(self):
        t = _tracker()
        twins = [_stop("A"), _stop("B")]
        after = [_stop("A"), _stop("B", "cancelled")]
        cancel = AsyncMock(return_value=True)
        with (
            patch("signal_engine.main.sweep_unresolved_orders", AsyncMock()),
            patch("signal_engine.tracker.fetch_orderbook", AsyncMock(return_value=twins)),
            patch(
                "signal_engine.api_client.fetch_orderbook", AsyncMock(side_effect=[twins, after])
            ),
            patch("signal_engine.api_client.cancel_order", cancel),
            patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=None)),
        ):
            await t.check_positions()
        assert len(cancel.await_args_list) == 1
        assert not executor._PENDING_DUPLICATE_STOPS

    @pytest.mark.asyncio
    async def test_the_tracked_stop_is_the_one_kept(self):
        t = _tracker()
        t.register(_make_position(sl_order_id="B"))
        twins = [_stop("A"), _stop("B")]
        after = [_stop("A", "cancelled"), _stop("B")]
        cancel = AsyncMock(return_value=True)
        with (
            patch("signal_engine.main.sweep_unresolved_orders", AsyncMock()),
            patch("signal_engine.tracker.fetch_orderbook", AsyncMock(return_value=twins)),
            patch(
                "signal_engine.api_client.fetch_orderbook", AsyncMock(side_effect=[twins, after])
            ),
            patch("signal_engine.api_client.cancel_order", cancel),
            patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=None)),
        ):
            await t.check_positions()
        assert [c.args[0] for c in cancel.await_args_list] == ["A"]

    @pytest.mark.asyncio
    async def test_the_audit_reads_the_book_at_most_once_per_interval(self):
        t = _tracker()
        read = AsyncMock(return_value=[])
        with (
            patch("signal_engine.main.sweep_unresolved_orders", AsyncMock()),
            patch("signal_engine.tracker.fetch_orderbook", read),
            patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=None)),
        ):
            await t.check_positions()
            await t.check_positions()
        assert read.await_count == 1

    @pytest.mark.asyncio
    async def test_an_unreadable_book_cancels_nothing_and_does_not_break_the_poll(self):
        t = _tracker()
        cancel = AsyncMock(return_value=True)
        with (
            patch("signal_engine.main.sweep_unresolved_orders", AsyncMock()),
            patch("signal_engine.tracker.fetch_orderbook", AsyncMock(return_value=None)),
            patch("signal_engine.api_client.cancel_order", cancel),
            patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=None)),
        ):
            await t.check_positions()
        cancel.assert_not_awaited()
        t._note_positionbook_failure.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_raising_audit_does_not_break_the_poll(self):
        t = _tracker()
        with (
            patch("signal_engine.main.sweep_unresolved_orders", AsyncMock()),
            patch(
                "signal_engine.tracker.fetch_orderbook", AsyncMock(side_effect=RuntimeError("net"))
            ),
            patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=None)),
        ):
            await t.check_positions()
        t._note_positionbook_failure.assert_awaited_once()


class TestRepeatedReconcileFailureEscalates:
    @pytest.mark.asyncio
    async def test_the_operator_is_alerted_once_after_repeated_failures(self):
        t = _tracker()
        executor._PENDING_DUPLICATE_STOPS.add(KEY, "K")
        live = [_stop("A"), _stop("K")]
        alert = AsyncMock()
        with (
            patch("signal_engine.main.sweep_unresolved_orders", AsyncMock()),
            patch("signal_engine.tracker.fetch_orderbook", AsyncMock(return_value=live)),
            patch("signal_engine.api_client.fetch_orderbook", AsyncMock(return_value=live)),
            patch("signal_engine.api_client.cancel_order", AsyncMock(return_value=False)),
            patch("signal_engine.tracker.notifier.notify_sl_failed", alert),
            patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=None)),
            patch.object(executor, "_DUPLICATE_RETRY_INTERVAL_S", 0.0),
        ):
            for _ in range(executor._ESCALATE_AFTER_FAILURES + 3):
                await t.check_positions()
        alert.assert_awaited_once()
        assert "duplicate" in alert.await_args.args[1].lower()
        assert KEY in executor._PENDING_DUPLICATE_STOPS  # still monitored after the alert


class TestExhaustedPositionIsNotAbandoned:
    def _exhausted(self, tracker, last_try=None):
        pos = _make_position(
            sl_order_id="",
            sl_retry_count=settings.bracket_max_sl_reretries,
            last_sl_retry_at=last_try or (_AGED - timedelta(hours=1)),
        )
        tracker.register(pos)
        return pos

    @pytest.mark.asyncio
    async def test_a_stop_that_landed_late_is_adopted_without_sending(self):
        t = _tracker()
        pos = self._exhausted(t)
        place = AsyncMock(side_effect=AssertionError("must reconcile before sending"))
        adopt = AsyncMock(return_value=TradeResult(order_id="LATE", status=OrderStatus.SUCCESS))
        with (
            patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=_book())),
            patch("signal_engine.tracker.place_sl_order", place),
            patch("signal_engine.tracker.adopt_resting_stop", adopt),
            patch("signal_engine.tracker.notifier.notify_sl_placed", AsyncMock()),
        ):
            await t.check_positions()
        assert pos.sl_order_id == "LATE"
        place.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_stop_found_leads_to_a_slow_cycle_send_and_an_alert(self):
        t = _tracker()
        pos = self._exhausted(t)
        failure = TradeResult(status=OrderStatus.TIMEOUT, message="Request timed out")
        place = AsyncMock(return_value=failure)
        alert = AsyncMock()
        with (
            patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=_book())),
            patch("signal_engine.tracker.place_sl_order", place),
            patch("signal_engine.tracker.adopt_resting_stop", AsyncMock(return_value=None)),
            patch("signal_engine.tracker.notifier.notify_sl_failed", alert),
        ):
            await t.check_positions()
        place.assert_awaited_once()
        alert.assert_awaited_once()
        assert "still unprotected" in alert.await_args.args[1].lower()
        assert pos.sl_retry_count == settings.bracket_max_sl_reretries  # exhaustion is not reset

    @pytest.mark.asyncio
    async def test_the_slow_cycle_waits_for_its_interval(self):
        t = _tracker()
        self._exhausted(t, last_try=datetime.now(IST) - timedelta(seconds=60))
        place = AsyncMock()
        adopt = AsyncMock(return_value=None)
        with (
            patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=_book())),
            patch("signal_engine.tracker.place_sl_order", place),
            patch("signal_engine.tracker.adopt_resting_stop", adopt),
        ):
            await t.check_positions()
        place.assert_not_awaited()
        adopt.assert_not_awaited()


class TestHeldResultDoesNotBurnAReattempt:
    @pytest.mark.asyncio
    async def test_a_hold_leaves_the_retry_count_unchanged_and_sends_no_alert(self):
        t = _tracker()
        pos = _make_position(
            sl_order_id="", sl_retry_count=2, last_sl_retry_at=_AGED - timedelta(hours=1)
        )
        t.register(pos)
        held = TradeResult(status=OrderStatus.TIMEOUT, message=executor.HOLD_MESSAGE)
        alert = AsyncMock()
        with (
            patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=_book())),
            patch("signal_engine.tracker.place_sl_order", AsyncMock(return_value=held)),
            patch("signal_engine.tracker.notifier.notify_sl_failed", alert),
        ):
            await t.check_positions()
        assert pos.sl_retry_count == 2
        alert.assert_not_awaited()
