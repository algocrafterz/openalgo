"""No-progress action must not sell into, or stack a stop on, a stop it failed to cancel.

A tracked stop that already filled cannot be cancelled; a market exit then opens a naked short.
A stop that is still working and refuses the cancel would make the exit a new short at the
broker, and a break-even stop placed beside it leaves two live stops on one position.
"""

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine.models import Direction, OrderStatus, TradeResult
from signal_engine.tests.tracker_fixtures import _make_engine, _make_position
from signal_engine.tracker import BookEntry, PositionTracker

_IST = timezone(timedelta(hours=5, minutes=30))


def _settings(mock_settings, market_exit: bool) -> None:
    mock_settings.no_progress_enabled = True
    mock_settings.no_progress_check_after_minutes = 90
    mock_settings.no_progress_min_progress_pct = 0.33
    mock_settings.no_progress_profit_lock_ratio = 0.0
    mock_settings.no_progress_early_check_enabled = False
    mock_settings.no_progress_early_check_after_minutes = 45
    mock_settings.no_progress_early_min_progress_pct = 0.05
    mock_settings.no_progress_use_fill_price = True
    mock_settings.no_progress_chop_tightener_enabled = False
    mock_settings.no_progress_chop_tightener_trigger_count = 2
    mock_settings.no_progress_chop_tightener_early_check_after_minutes = 30
    mock_settings.no_progress_loss_cut_enabled = False
    mock_settings.time_exit_enabled = market_exit
    exit_time = datetime.now(_IST) + timedelta(minutes=60)
    mock_settings.time_exit_hour = exit_time.hour
    mock_settings.time_exit_minute = exit_time.minute


def _live_twin():
    return {
        "orderid": "TWIN",
        "symbol": "RELIANCE",
        "action": "SELL",
        "quantity": 1,
        "pricetype": "SL-M",
        "order_status": "trigger pending",
        "strategy": "ORB",
    }


#: main's exit sender: one retry, reconcile reads without waiting, a short late-fill watch.
_MAIN_SETTINGS = SimpleNamespace(
    bracket_tp_exit_retries=2, bracket_retry_delay=0.0,
    entry_timeout_recovery_attempts=1, entry_timeout_recovery_delay_seconds=0.0,
)


async def _run(*, market_exit, cancel_ok, stop_status="", book=None, sends=None, exit_book=None, own_qty=0):
    """sends: results of main.send_order in turn (default one SUCCESS). exit_book: what the
    exit reconcile reads from the order book (None = unreadable). own_qty: this strategy's broker
    position after the exit, which an adopted exit must show (default 0: the full exit filled)."""
    from signal_engine import main as main_module

    tracker = PositionTracker(_make_engine())
    pos = _make_position(
        entry_price=541.80,
        fill_price=541.80,
        sl=528.01,
        tp=551.10,
        sl_order_id="OLD_SL",
        direction=Direction.LONG,
        entry_time=datetime.now(_IST) - timedelta(minutes=150),
    )
    tracker.register(pos)
    ok = TradeResult(status=OrderStatus.SUCCESS, order_id="NEW", message="")
    with ExitStack() as stack:
        enter = stack.enter_context
        enter(patch("signal_engine.tracker.cancel_order", new_callable=AsyncMock, return_value=cancel_ok))
        enter(patch("signal_engine.tracker.fetch_order_status", new_callable=AsyncMock, return_value=stop_status))
        enter(patch("signal_engine.tracker.fetch_orderbook", new_callable=AsyncMock, return_value=book))
        enter(patch("signal_engine.executor.asyncio.sleep", new_callable=AsyncMock))
        send = enter(patch("signal_engine.main.send_order", AsyncMock(side_effect=list(sends or [ok]))))
        enter(patch("signal_engine.main.fetch_orderbook", AsyncMock(return_value=exit_book)))
        enter(patch("signal_engine.main.settings", _MAIN_SETTINGS))
        enter(patch("signal_engine.main.notifier", AsyncMock()))
        enter(patch("signal_engine.main.fetch_order_ids_today", MagicMock(return_value=set())))
        enter(patch.object(main_module.tracker, "fetch_own_position", AsyncMock(return_value=own_qty)))
        enter(patch("signal_engine.main._LATE_FILL_WATCH_SECONDS", 0.0))
        enter(patch("signal_engine.order_reconcile.asyncio.sleep", AsyncMock()))
        place = enter(patch("signal_engine.tracker.place_sl_order", new_callable=AsyncMock, return_value=ok))
        enter(patch("signal_engine.tracker.fetch_order_fill_price", new_callable=AsyncMock, return_value=None))
        enter(patch("signal_engine.tracker.notifier.notify_no_progress_exit", new_callable=AsyncMock))
        enter(patch("signal_engine.tracker.notifier.notify_be_stop_applied", new_callable=AsyncMock))
        enter(patch("signal_engine.tracker.notifier.notify_sl_failed", new_callable=AsyncMock))
        _settings(enter(patch("signal_engine.config.settings")), market_exit)
        await tracker._check_no_progress({"RELIANCE": BookEntry(1, 544.70)})
    return pos, send, place, tracker


class TestTrackedStopCancelFails:
    @pytest.mark.asyncio
    async def test_filled_stop_gets_no_market_exit(self):
        pos, send, place, _ = await _run(market_exit=True, cancel_ok=False, stop_status="complete")
        send.assert_not_awaited()
        place.assert_not_awaited()
        assert pos.be_stop_applied is True  # the action does not fire again every poll

    @pytest.mark.asyncio
    async def test_working_stop_gets_no_market_exit(self):
        pos, send, place, _ = await _run(market_exit=True, cancel_ok=False, stop_status="trigger pending")
        send.assert_not_awaited()
        place.assert_not_awaited()
        assert pos.sl_order_id == "OLD_SL"  # the stop that still protects stays tracked

    @pytest.mark.asyncio
    async def test_working_stop_gets_no_second_break_even_stop(self):
        pos, send, place, _ = await _run(market_exit=False, cancel_ok=False, stop_status="trigger pending")
        place.assert_not_awaited()
        assert pos.sl_order_id == "OLD_SL"

    @pytest.mark.asyncio
    async def test_stop_already_cancelled_still_gets_the_action(self):
        """A cancel that failed because the stop is already gone must not block the action."""
        _, send, _, _ = await _run(market_exit=True, cancel_ok=False, stop_status="cancelled")
        send.assert_awaited_once()


class TestTwinStopStillLive:
    @pytest.mark.asyncio
    async def test_no_break_even_stop_beside_a_live_twin(self):
        _, _, place, _ = await _run(market_exit=False, cancel_ok=True, book=[_live_twin()])
        place.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unreadable_book_still_moves_the_stop(self):
        """The tracked stop is gone; with no proof of a twin the position needs its new stop."""
        _, _, place, _ = await _run(market_exit=False, cancel_ok=True, book=None)
        place.assert_awaited_once()


def _exit_fill_row(order_id, stamp, quantity):
    return {"orderid": order_id, "symbol": "RELIANCE", "action": "SELL", "quantity": quantity,
            "order_status": "complete", "timestamp": stamp, "strategy": "ORB"}


class TestMarketExitTimeout:
    """The no-progress market exit used to log a timeout and return: the stop was already
    cancelled and nothing reconciled the order, so the stop retry could add a stop beside an exit
    that was still landing (double sell), or the position sat with no protection."""

    TIMEOUT = TradeResult(status=OrderStatus.TIMEOUT, message="Request timed out")

    @pytest.mark.asyncio
    async def test_a_timed_out_exit_that_filled_is_adopted_not_resent(self):
        from signal_engine import order_reconcile

        stamp = datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S")
        pos, send, place, _ = await _run(
            market_exit=True, cancel_ok=True, sends=[self.TIMEOUT],
            exit_book=[_exit_fill_row("EXIT1", stamp, _make_position().quantity)],
        )
        assert send.await_count == 1
        place.assert_not_awaited()
        assert pos.sl_order_id == "EXIT1"  # the closing order is the adopted one
        assert order_reconcile.get_uncertain("ORB", "RELIANCE") is None

    @pytest.mark.asyncio
    async def test_an_unresolved_timeout_holds_and_places_no_stop(self):
        from signal_engine import order_reconcile

        try:
            pos, send, place, tracker = await _run(
                market_exit=True, cancel_ok=True, sends=[self.TIMEOUT], exit_book=None,
            )
            assert send.await_count == 1  # never resent blind
            place.assert_not_awaited()  # no stop beside an exit that may still land
            assert order_reconcile.get_uncertain("ORB", "RELIANCE") is not None
            assert tracker.find_position("RELIANCE", "ORB") is pos  # still tracked
        finally:
            order_reconcile.clear_uncertain("ORB", "RELIANCE")

    @pytest.mark.asyncio
    async def test_a_rejected_exit_restores_the_stop_at_once(self):
        rejected = TradeResult(status=OrderStatus.REJECTED, message="RMS block")
        pos, send, place, _ = await _run(market_exit=True, cancel_ok=True, sends=[rejected, rejected])
        assert send.await_count == 2
        place.assert_awaited_once()
        assert place.await_args.kwargs["sl_price"] == pytest.approx(528.01)  # the stop just cancelled
        assert place.await_args.kwargs["quantity"] == pos.quantity
        assert pos.sl_order_id == "NEW"
