"""Exit and entry timeouts must never be blindly resent (2026-10-13 LAURUSLABS / SUNPHARMA / KALYANKJIL).

At 13:56 every exit for LAURUSLABS (SELL 74) and SUNPHARMA (SELL 236) timed out while the order book
could not be read. The retry loop resent; the broker filled three copies of each and the extra two
opened shorts of 148 and 472. KALYANKJIL's entry (BUY 188) timed out, was treated as a failure, and
sat on the broker untracked until the 14:45 close-out.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import order_reconcile as oc
from signal_engine.main import _recover_ambiguous_entry_timeout, _send_exit_with_retries
from signal_engine.models import Action, Direction, Order, OrderStatus, Signal, TradeResult
from signal_engine.timeutils import IST

SETTINGS = SimpleNamespace(
    bracket_tp_exit_retries=3, bracket_retry_delay=0.0,
    entry_timeout_recovery_attempts=3, entry_timeout_recovery_delay_seconds=0.0,
)
TIMEOUT = TradeResult(status=OrderStatus.TIMEOUT, message="Request timed out")


@pytest.fixture(autouse=True)
def _clean_registry():
    oc._uncertain.clear()
    yield
    oc._uncertain.clear()


def _exit_order(symbol="LAURUSLABS", qty=74):
    return Order(symbol=symbol, exchange="NSE", action=Action.SELL, quantity=qty, price=0.0,
                 order_type="MARKET", product="MIS", strategy_tag="BREAKOUT-PY")


def _pos(symbol="LAURUSLABS", qty=74):
    return SimpleNamespace(symbol=symbol, strategy="BREAKOUT-PY", quantity=qty,
                           exchange="NSE", product="MIS", direction=Direction.LONG)


def _filled_row(oid, symbol="LAURUSLABS", qty=74, status="complete", action="SELL"):
    stamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    return {"orderid": oid, "symbol": symbol, "action": action, "quantity": qty,
            "order_status": status, "timestamp": stamp, "strategy": "BREAKOUT-PY"}


async def _run_exit(send_results, books, *, order=None, pos=None):
    send = AsyncMock(side_effect=send_results)
    fetch = AsyncMock(side_effect=books)
    notify = AsyncMock()
    with (
        patch("signal_engine.main.settings", SETTINGS),
        patch("signal_engine.main.send_order", send),
        patch("signal_engine.main.fetch_orderbook", fetch),
        patch("signal_engine.main.notifier", MagicMock(notify=notify)),
    ):
        result = await _send_exit_with_retries(order or _exit_order(), pos or _pos())
    return result, send, notify


@pytest.mark.asyncio
class TestExitTimeouts:
    async def test_book_unreadable_after_timeout_sends_exactly_one_order(self):
        result, send, notify = await _run_exit([TIMEOUT, TIMEOUT, TIMEOUT], [None] * 9)
        assert send.await_count == 1
        assert result.status == OrderStatus.TIMEOUT
        assert "UNCERTAIN" in result.message
        assert oc.get_uncertain("BREAKOUT-PY", "LAURUSLABS") is not None
        notify.assert_awaited()  # CRITICAL alert

    async def test_timeout_that_actually_filled_is_adopted_without_a_resend(self):
        result, send, _ = await _run_exit([TIMEOUT], [[_filled_row("F1")]])
        assert send.await_count == 1
        assert (result.status, result.order_id) == (OrderStatus.SUCCESS, "F1")
        assert oc.get_uncertain("BREAKOUT-PY", "LAURUSLABS") is None

    async def test_delayed_confirmation_is_adopted(self):
        result, send, _ = await _run_exit([TIMEOUT], [[], [], [_filled_row("F1")]])
        assert send.await_count == 1 and result.order_id == "F1"

    async def test_duplicate_fills_are_adopted_once_and_alerted(self):
        book = [_filled_row("F1"), _filled_row("F2"), _filled_row("F3")]
        result, send, notify = await _run_exit([TIMEOUT], [book])
        assert send.await_count == 1 and result.order_id == "F1"
        assert "F2" in str(notify.await_args_list) and "F3" in str(notify.await_args_list)

    async def test_partial_fill_in_flight_is_not_resent(self):
        book = [_filled_row("P1", status="partially filled")]
        result, send, _ = await _run_exit([TIMEOUT], [book, book, book])
        assert send.await_count == 1 and "UNCERTAIN" in result.message

    async def test_book_read_and_clean_still_retries_as_before(self):
        ok = TradeResult(status=OrderStatus.SUCCESS, order_id="N1")
        result, send, _ = await _run_exit([TIMEOUT, ok], [[], [], []])
        assert send.await_count == 2 and result.order_id == "N1"
        assert oc.get_uncertain("BREAKOUT-PY", "LAURUSLABS") is None

    async def test_non_timeout_failure_retries_unchanged(self):
        err = TradeResult(status=OrderStatus.ERROR, message="rejected")
        ok = TradeResult(status=OrderStatus.SUCCESS, order_id="N1")
        result, send, _ = await _run_exit([err, ok], [])
        assert send.await_count == 2 and result.order_id == "N1"


@pytest.mark.asyncio
class TestRecovery:
    async def test_next_exit_attempt_is_held_while_the_book_stays_unreadable(self):
        await _run_exit([TIMEOUT], [None] * 3)  # marks the position uncertain
        result, send, _ = await _run_exit([], [None] * 3)
        assert send.await_count == 0 and "UNCERTAIN" in result.message

    async def test_connection_back_and_order_found_is_adopted_not_resent(self):
        await _run_exit([TIMEOUT], [None] * 3)
        result, send, _ = await _run_exit([], [[_filled_row("F1")]])
        assert send.await_count == 0 and result.order_id == "F1"
        assert oc.get_uncertain("BREAKOUT-PY", "LAURUSLABS") is None

    async def test_connection_back_and_no_order_releases_the_hold_and_exits_normally(self):
        await _run_exit([TIMEOUT], [None] * 3)
        ok = TradeResult(status=OrderStatus.SUCCESS, order_id="N1")
        result, send, _ = await _run_exit([ok], [[], [], []])
        assert send.await_count == 1 and result.order_id == "N1"
        assert oc.get_uncertain("BREAKOUT-PY", "LAURUSLABS") is None

    async def test_other_positions_are_not_blocked(self):
        await _run_exit([TIMEOUT], [None] * 3)
        ok = TradeResult(status=OrderStatus.SUCCESS, order_id="S1")
        result, send, _ = await _run_exit(
            [ok], [], order=_exit_order("SUNPHARMA", 236), pos=_pos("SUNPHARMA", 236)
        )
        assert send.await_count == 1 and result.order_id == "S1"


def _entry_signal():
    return Signal(strategy="BREAKOUT-PY", direction=Direction.LONG, symbol="KALYANKJIL",
                  entry=566.0, sl=560.0, tp=0.0, raw_message="x")


def _entry_order():
    return Order(symbol="KALYANKJIL", exchange="NSE", action=Action.BUY, quantity=188, price=0.0,
                 order_type="MARKET", product="MIS", strategy_tag="BREAKOUT-PY")


async def _run_entry_recovery(books):
    notify = AsyncMock()
    tracker = MagicMock(_positions={})
    with (
        patch("signal_engine.main.settings", SETTINGS),
        patch("signal_engine.main.fetch_orderbook", AsyncMock(side_effect=books)),
        patch("signal_engine.main.fetch_order_ids_today", MagicMock(return_value=set())),
        patch("signal_engine.main.notifier", MagicMock(notify=notify)),
        patch("signal_engine.main.tracker", tracker),
    ):
        return await _recover_ambiguous_entry_timeout(_entry_signal(), _entry_order()), notify


@pytest.mark.asyncio
class TestEntryTimeouts:
    async def test_unreadable_book_leaves_the_entry_uncertain_and_alerts(self):
        result, notify = await _run_entry_recovery([None] * 3)
        assert result is not None and "UNCERTAIN" in result.message
        assert oc.get_uncertain("BREAKOUT-PY", "KALYANKJIL") is not None
        notify.assert_awaited()

    async def test_clean_book_without_the_order_is_still_a_genuine_failure(self):
        result, _ = await _run_entry_recovery([[], [], []])
        assert result is None
        assert oc.get_uncertain("BREAKOUT-PY", "KALYANKJIL") is None

    async def test_filled_entry_is_still_recovered(self):
        row = _filled_row("E1", "KALYANKJIL", 188, action="BUY")
        result, _ = await _run_entry_recovery([[row]])
        assert (result.status, result.order_id) == (OrderStatus.SUCCESS, "E1")

    async def test_entry_still_working_is_uncertain_not_a_failure(self):
        row = _filled_row("E1", "KALYANKJIL", 188, status="open", action="BUY")
        result, _ = await _run_entry_recovery([[row]] * 3)
        assert result is not None and "UNCERTAIN" in result.message


@pytest.mark.asyncio
class TestUnresolvedEntryBlocksNewOrders:
    def _mark(self):
        oc.mark_uncertain(oc.UncertainOrder(
            "BREAKOUT-PY", "KALYANKJIL", "BUY", 188, datetime.now(IST).replace(microsecond=0), "ENTRY"))

    async def _held(self, books):
        from signal_engine.main import _entry_held_by_unresolved_order

        notify = AsyncMock()
        with (
            patch("signal_engine.main.settings", SETTINGS),
            patch("signal_engine.main.fetch_orderbook", AsyncMock(side_effect=books)),
            patch("signal_engine.main.notifier", MagicMock(notify=notify)),
        ):
            return await _entry_held_by_unresolved_order(_entry_signal()), notify

    async def test_no_pending_order_means_no_hold(self):
        held, _ = await self._held([])
        assert held is False

    async def test_still_unreadable_keeps_the_hold(self):
        self._mark()
        held, _ = await self._held([None] * 3)
        assert held is True and oc.get_uncertain("BREAKOUT-PY", "KALYANKJIL") is not None

    async def test_order_found_complete_releases_the_hold_but_blocks_and_alerts(self):
        self._mark()
        row = _filled_row("E1", "KALYANKJIL", 188, action="BUY")
        held, notify = await self._held([[row]])
        assert held is True and oc.get_uncertain("BREAKOUT-PY", "KALYANKJIL") is None
        assert "UNTRACKED FILL" in str(notify.await_args_list)

    async def test_order_confirmed_absent_releases_the_hold_and_allows_entry(self):
        self._mark()
        held, _ = await self._held([[], [], []])
        assert held is False and oc.get_uncertain("BREAKOUT-PY", "KALYANKJIL") is None


# ---- sweeper and stop protection ----------------------------------------------------------

def _tracked(symbol="LAURUSLABS", qty=74, sl_id="SL-1", exit_pending=False):
    from signal_engine.tracker import TrackedPosition

    return TrackedPosition(symbol=symbol, strategy="BREAKOUT-PY", exchange="NSE", product="MIS",
                           entry_price=2050.0, quantity=qty, sl=2040.0, tp=0.0,
                           direction=Direction.LONG, sl_order_id=sl_id, exit_pending=exit_pending)


def _mark_exit(symbol="LAURUSLABS", qty=74, since=None):
    oc.mark_uncertain(oc.UncertainOrder(
        "BREAKOUT-PY", symbol, "SELL", qty, since or datetime.now(IST).replace(microsecond=0), "EXIT"))


async def _sweep(books, *, position=None, sl_status="canceled"):
    from signal_engine.main import sweep_unresolved_orders

    notify = AsyncMock()
    tracker = MagicMock()
    tracker.find_position = MagicMock(return_value=position)
    with (
        patch("signal_engine.main.fetch_orderbook", AsyncMock(side_effect=books)),
        patch("signal_engine.main.fetch_order_status", AsyncMock(return_value=sl_status)),
        patch("signal_engine.main.notifier", MagicMock(notify=notify)),
        patch("signal_engine.main.tracker", tracker),
    ):
        await sweep_unresolved_orders()
    return notify


@pytest.mark.asyncio
class TestSweeper:
    async def test_exit_absent_and_stop_not_working_restores_protection(self):
        pos = _tracked()
        _mark_exit()
        notify = await _sweep([[]], position=pos)
        assert pos.sl_order_id == "" and pos.sl_retry_count == 0
        assert oc.get_uncertain("BREAKOUT-PY", "LAURUSLABS") is None
        assert "STOP RESTORING" in str(notify.await_args_list)

    async def test_a_stop_still_working_is_never_stacked(self):
        pos = _tracked()
        _mark_exit()
        await _sweep([[]], position=pos, sl_status="trigger pending")
        assert pos.sl_order_id == "SL-1"

    async def test_unknown_stop_status_changes_nothing(self):
        pos = _tracked()
        _mark_exit()
        await _sweep([[]], position=pos, sl_status="")
        assert pos.sl_order_id == "SL-1"

    async def test_exit_found_filled_releases_the_hold_and_places_no_stop(self):
        pos = _tracked()
        _mark_exit()
        await _sweep([[_filled_row("F1")]], position=pos)
        assert oc.get_uncertain("BREAKOUT-PY", "LAURUSLABS") is None
        assert pos.sl_order_id == "SL-1"

    async def test_still_unreadable_keeps_the_hold_and_alerts_once_per_window(self):
        pos = _tracked()
        _mark_exit()
        notify = await _sweep([None], position=pos)
        assert oc.get_uncertain("BREAKOUT-PY", "LAURUSLABS") is not None
        assert notify.await_count == 1
        # an immediate second sweep is throttled: no second look, no second alert
        notify2 = await _sweep([None], position=pos)
        assert notify2.await_count == 0

    async def test_entry_found_filled_is_adopted_with_its_stop(self):
        from signal_engine.main import sweep_unresolved_orders

        oc.mark_uncertain(oc.UncertainOrder(
            "BREAKOUT-PY", "KALYANKJIL", "BUY", 188, datetime.now(IST).replace(microsecond=0),
            "ENTRY", signal=_entry_signal(), order=_entry_order()))
        establish, save = AsyncMock(return_value=True), MagicMock()
        row = _filled_row("E1", "KALYANKJIL", 188, action="BUY")
        with (
            patch("signal_engine.main.fetch_orderbook", AsyncMock(return_value=[row])),
            patch("signal_engine.main._establish_position", establish),
            patch("signal_engine.main.save", save),
            patch("signal_engine.main.notifier", MagicMock(notify=AsyncMock())),
        ):
            await sweep_unresolved_orders()
        establish.assert_awaited_once()
        assert establish.await_args.args[3].order_id == "E1"
        save.assert_called_once()
        assert oc.get_uncertain("BREAKOUT-PY", "KALYANKJIL") is None

    async def test_entry_not_on_the_broker_just_releases_the_hold(self):
        oc.mark_uncertain(oc.UncertainOrder(
            "BREAKOUT-PY", "KALYANKJIL", "BUY", 188, datetime.now(IST).replace(microsecond=0),
            "ENTRY", signal=_entry_signal(), order=_entry_order()))
        establish = AsyncMock()
        with (
            patch("signal_engine.main.fetch_orderbook", AsyncMock(return_value=[])),
            patch("signal_engine.main._establish_position", establish),
            patch("signal_engine.main.notifier", MagicMock(notify=AsyncMock())),
        ):
            from signal_engine.main import sweep_unresolved_orders

            await sweep_unresolved_orders()
        establish.assert_not_awaited()
        assert oc.get_uncertain("BREAKOUT-PY", "KALYANKJIL") is None

    async def test_a_hold_from_an_earlier_day_is_dropped_with_a_critical_log(self):
        _mark_exit(since=datetime(2026, 10, 12, 14, 0, 0, tzinfo=IST))
        await _sweep([], position=_tracked())
        assert oc.get_uncertain("BREAKOUT-PY", "LAURUSLABS") is None


@pytest.mark.asyncio
class TestFailedExitReprotects:
    async def _fail(self, *, message="rejected", stop_cancelled=True, held=False):
        from signal_engine.main import _handle_exit_order_failure

        pos = _tracked(exit_pending=True)
        if held:
            _mark_exit()
        tracker = MagicMock(fetch_own_position=AsyncMock(return_value=74))
        with (
            patch("signal_engine.main.tracker", tracker),
            patch("signal_engine.main.notifier", MagicMock(notify_exit_failed=AsyncMock())),
        ):
            await _handle_exit_order_failure(
                pos, TradeResult(status=OrderStatus.ERROR, message=message), stop_cancelled=stop_cancelled)
        return pos

    async def test_exit_failed_after_the_stop_was_cancelled_clears_the_dead_stop_id(self):
        pos = await self._fail()
        assert pos.sl_order_id == "" and pos.exit_pending is False

    async def test_stop_that_was_not_cancelled_keeps_its_id(self):
        pos = await self._fail(stop_cancelled=False)
        assert pos.sl_order_id == "SL-1"

    async def test_an_unresolved_order_defers_to_the_sweeper(self):
        pos = await self._fail(held=True)
        assert pos.sl_order_id == "SL-1"


@pytest.mark.asyncio
class TestPollDoesNotPlaceAStopWhileAnOrderIsUnresolved:
    async def _poll(self, *, held):
        from signal_engine.tracker import PositionTracker

        t = PositionTracker(MagicMock())
        t.register(_tracked(sl_id=""))
        if held:
            _mark_exit()
        settings = SimpleNamespace(bracket_enabled=True, bracket_max_sl_reretries=5,
                                   bracket_sl_reretry_interval_seconds=0)
        place = AsyncMock(return_value=TradeResult(status=OrderStatus.SUCCESS, order_id="SL-NEW"))
        book = {"LAURUSLABS": SimpleNamespace(quantity=74)}
        with (
            patch("signal_engine.tracker.place_sl_order", place),
            patch("signal_engine.tracker.notifier", MagicMock(notify_sl_placed=AsyncMock())),
        ):
            await t._retry_missing_sl(book, settings)
        return place

    async def test_unresolved_order_blocks_the_stop(self):
        place = await self._poll(held=True)
        place.assert_not_awaited()

    async def test_resolved_position_gets_its_stop(self):
        place = await self._poll(held=False)
        place.assert_awaited_once()


@pytest.mark.asyncio
class TestSweepRunsDuringAPositionbookOutage:
    async def test_poll_sweeps_even_when_the_positionbook_cannot_be_read(self):
        from signal_engine.tracker import PositionTracker

        t = PositionTracker(MagicMock())
        t._note_positionbook_failure = AsyncMock()
        sweep = AsyncMock()
        with (
            patch("signal_engine.main.sweep_unresolved_orders", sweep),
            patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=None)),
        ):
            await t.check_positions()
        sweep.assert_awaited_once()
        t._note_positionbook_failure.assert_awaited_once()

    async def test_a_failing_sweep_does_not_break_the_poll(self):
        from signal_engine.tracker import PositionTracker

        t = PositionTracker(MagicMock())
        t._note_positionbook_failure = AsyncMock()
        with (
            patch("signal_engine.main.sweep_unresolved_orders", AsyncMock(side_effect=RuntimeError("x"))),
            patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=None)),
        ):
            await t.check_positions()
        t._note_positionbook_failure.assert_awaited_once()
