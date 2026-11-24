"""Tracker side of the protective-stop lifecycle (executor side: test_stop_pending_duplicates.py).

* Restart recovery: the pending list is in memory, so the poll also audits the order book for
  exact-twin stops, which finds duplicates a restarted process knows nothing about.
* Repeated reconcile failure is escalated once (one error log line), and never breaks the poll.
* A position whose stop retries are exhausted is not abandoned: it is reconciled (no send) and
  re-attempted on a slow cycle, with an error log line each time it is still unprotected.
* A "held" result (an earlier submission is unresolved) does not use up a re-attempt.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import executor
from signal_engine.config import settings
from signal_engine.models import OrderStatus, TradeResult
from signal_engine.strategies import ORB
from signal_engine.tests.log_capture import capturing
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
    async def test_repeated_failures_are_logged_once_as_an_error(self):
        t = _tracker()
        executor._PENDING_DUPLICATE_STOPS.add(KEY, "K")
        live = [_stop("A"), _stop("K")]
        with (
            patch("signal_engine.main.sweep_unresolved_orders", AsyncMock()),
            patch("signal_engine.tracker.fetch_orderbook", AsyncMock(return_value=live)),
            patch("signal_engine.api_client.fetch_orderbook", AsyncMock(return_value=live)),
            patch("signal_engine.api_client.cancel_order", AsyncMock(return_value=False)),
            patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=None)),
            patch.object(executor, "_DUPLICATE_RETRY_INTERVAL_S", 0.0),
            capturing() as logs,
        ):
            for _ in range(executor._ESCALATE_AFTER_FAILURES + 3):
                await t.check_positions()
        # Escalated once, in the log (no message goes out), however many polls follow it.
        failed = [m for m in logs.messages("ERROR") if "Duplicate stop cleanup" in m]
        assert len(failed) == 1
        assert "failed repeatedly" in failed[0] and "RELIANCE" in failed[0]
        assert KEY in executor._PENDING_DUPLICATE_STOPS  # still monitored after the escalation


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
        ):
            await t.check_positions()
        assert pos.sl_order_id == "LATE"
        place.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_stop_found_leads_to_a_slow_cycle_send_and_an_error_line(self):
        t = _tracker()
        pos = self._exhausted(t)
        failure = TradeResult(status=OrderStatus.TIMEOUT, message="Request timed out")
        place = AsyncMock(return_value=failure)
        with (
            patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=_book())),
            patch("signal_engine.tracker.place_sl_order", place),
            patch("signal_engine.tracker.adopt_resting_stop", AsyncMock(return_value=None)),
            capturing() as logs,
        ):
            await t.check_positions()
        place.assert_awaited_once()
        failed = [m for m in logs.messages("ERROR") if m.startswith("SL FAILED |")]
        assert len(failed) == 1
        assert "still unprotected" in failed[0].lower()
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
    async def test_a_hold_leaves_the_retry_count_unchanged_and_logs_no_failure(self):
        t = _tracker()
        pos = _make_position(
            sl_order_id="", sl_retry_count=2, last_sl_retry_at=_AGED - timedelta(hours=1)
        )
        t.register(pos)
        held = TradeResult(status=OrderStatus.TIMEOUT, message=executor.HOLD_MESSAGE)
        with (
            patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=_book())),
            patch("signal_engine.tracker.place_sl_order", AsyncMock(return_value=held)),
            capturing() as logs,
        ):
            await t.check_positions()
        assert pos.sl_retry_count == 2
        assert not [m for m in logs.messages("ERROR") if m.startswith("SL FAILED |")]
