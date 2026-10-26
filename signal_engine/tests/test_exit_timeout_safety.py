"""Exit and entry timeouts must never be blindly resent (2026-10-13 LAURUSLABS / SUNPHARMA / KALYANKJIL).

At 13:56 every exit for LAURUSLABS (SELL 74) and SUNPHARMA (SELL 236) timed out while the order book
could not be read. The retry loop resent; the broker filled three copies of each and the extra two
opened shorts of 148 and 472. KALYANKJIL's entry (BUY 188) timed out, was treated as a failure, and
sat on the broker untracked until the 14:45 close-out.
"""

from datetime import datetime, timedelta
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
    # A broker stamps a fill after the send. Stamping "now" at whole seconds while the row is
    # built made it read as older than the attempt's `since` whenever a second boundary fell in
    # between, so the row was filtered out and the test flaked.
    stamp = (datetime.now(IST) + timedelta(seconds=2)).strftime("%Y-%m-%d %H:%M:%S")
    return {"orderid": oid, "symbol": symbol, "action": action, "quantity": qty,
            "order_status": status, "timestamp": stamp, "strategy": "BREAKOUT-PY"}


#: Test watch: 10 s window read every 5 s = 2 reads after the 3 normal ones (the sleeps are stubbed).
WATCH_SECONDS, WATCH_POLL_SECONDS = 10.0, 5.0


#: own_qty default: the broker shows exactly one correct exit of this order (the position proof
#: an adoption needs since 2026-10-25). Tests of an unproven or reversed position pass their own.
EXIT_SHOWN = object()


async def _run_exit(send_results, books, *, order=None, pos=None, own_qty=EXIT_SHOWN):
    order, pos = order or _exit_order(), pos or _pos()
    if own_qty is EXIT_SHOWN:
        sign = 1 if pos.direction == Direction.LONG else -1
        own_qty = sign * (pos.quantity - order.quantity)
    send = AsyncMock(side_effect=send_results)
    fetch = AsyncMock(side_effect=books)
    notify = AsyncMock()
    tracker = MagicMock(fetch_own_position=AsyncMock(return_value=own_qty))
    with (
        patch("signal_engine.main.settings", SETTINGS),
        patch("signal_engine.main.send_order", send),
        patch("signal_engine.main.fetch_orderbook", fetch),
        patch("signal_engine.main.notifier", MagicMock(notify=notify)),
        patch("signal_engine.main.tracker", tracker),
        patch("signal_engine.main._LATE_FILL_WATCH_SECONDS", WATCH_SECONDS),
        patch("signal_engine.main._EXIT_WATCH_POLL_SECONDS", WATCH_POLL_SECONDS),
        patch("signal_engine.order_reconcile.asyncio", SimpleNamespace(sleep=AsyncMock())),
    ):
        result = await _send_exit_with_retries(order, pos)
    return result, send, notify


def _expire_watch(symbol="LAURUSLABS"):
    """Move a held order's late-fill watch into the past, as if its window had run out."""
    from dataclasses import replace

    pending = oc.get_uncertain("BREAKOUT-PY", symbol)
    oc.mark_uncertain(replace(pending, watch_until=datetime(2000, 1, 1, tzinfo=IST)))


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

    async def test_duplicate_fills_with_an_unknown_position_hold_and_name_every_copy(self):
        """Policy updated 2026-10-25 (was: adopt F1 and alert, whatever the broker position).
        An order is adopted as the confirmed exit only when the broker position shows it."""
        book = [_filled_row("F1"), _filled_row("F2"), _filled_row("F3")]
        result, send, notify = await _run_exit([TIMEOUT], [book, book], own_qty=None)
        assert send.await_count == 1 and result.status == OrderStatus.TIMEOUT
        alerts = str(notify.await_args_list)
        assert all(oid in alerts for oid in ("F1", "F2", "F3")) and "UNPROTECTED" in alerts

    async def test_partial_fill_in_flight_is_not_resent(self):
        book = [_filled_row("P1", status="partially filled")]
        result, send, _ = await _run_exit([TIMEOUT], [book, book, book])
        assert send.await_count == 1 and "UNCERTAIN" in result.message

    async def test_book_clean_for_the_whole_watch_then_resends_once(self):
        """Policy updated 2026-10-25 (was: resend straight after three clean reads, ~6 s).

        2026-10-23 SUNPHARMA: three clean reads, a resend, and the first copy filled anyway
        ~14 s after it was sent; the extra BUY 174 opened a long nothing tracked. A clean book
        now means "keep watching" until the late-fill window has run out.
        """
        ok = TradeResult(status=OrderStatus.SUCCESS, order_id="N1")
        result, send, _ = await _run_exit([TIMEOUT, ok], [[]] * 5)
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
        _expire_watch()  # an exit still inside its late-fill window is held: TestExitLateFillWatch
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


@pytest.mark.asyncio
class TestExitLateFillWatch:
    """2026-10-22 TVSMOTOR and 2026-10-23 SUNPHARMA: an exit that read clean ~6 s after its
    timeout filled anyway (14 s, server overloaded); the resend filled too and opened a reverse
    position. A clean book inside the late-fill window is not proof the order never lands."""

    async def test_a_fill_that_lands_during_the_watch_is_adopted_without_a_resend(self):
        books = [[], [], [], [], [_filled_row("F1")]]
        result, send, _ = await _run_exit([TIMEOUT], books)
        assert send.await_count == 1
        assert (result.status, result.order_id) == (OrderStatus.SUCCESS, "F1")
        assert oc.get_uncertain("BREAKOUT-PY", "LAURUSLABS") is None

    async def test_book_unreadable_at_the_end_of_the_watch_holds_and_never_resends(self):
        result, send, _ = await _run_exit([TIMEOUT], [[], [], [], [], None])
        assert send.await_count == 1 and "UNCERTAIN" in result.message
        pending = oc.get_uncertain("BREAKOUT-PY", "LAURUSLABS")
        assert pending is not None and pending.watch_until is not None

    async def test_a_held_exit_inside_its_window_is_not_resent_on_a_clean_read(self):
        await _run_exit([TIMEOUT], [None] * 3)  # held, window open
        result, send, _ = await _run_exit([], [[], [], []])
        assert send.await_count == 0 and result.status == OrderStatus.TIMEOUT
        assert oc.get_uncertain("BREAKOUT-PY", "LAURUSLABS") is not None

    async def test_sweeper_keeps_a_clean_exit_inside_its_window_and_restores_no_stop(self):
        pos = _tracked()
        oc.mark_uncertain(oc.UncertainOrder(
            "BREAKOUT-PY", "LAURUSLABS", "SELL", 74, datetime.now(IST).replace(microsecond=0),
            "EXIT", watch_until=datetime(2100, 1, 1, tzinfo=IST)))
        await _sweep([[]], position=pos)
        assert oc.get_uncertain("BREAKOUT-PY", "LAURUSLABS") is not None
        assert pos.sl_order_id == "SL-1"


@pytest.mark.asyncio
class TestDuplicateExitIsClosed:
    """The broker filled one exit more than once: flatten exactly what the extras opened, and
    only when the broker position proves that size."""

    async def test_one_extra_copy_is_closed_with_one_opposite_order(self):
        close = TradeResult(status=OrderStatus.SUCCESS, order_id="C1")
        book = [_filled_row("F1", "SUNPHARMA", 174), _filled_row("F2", "SUNPHARMA", 174)]
        result, send, notify = await _run_exit(
            [TIMEOUT, close], [book, book], order=_exit_order("SUNPHARMA", 174),
            pos=_pos("SUNPHARMA", 174), own_qty=-174)
        assert result.order_id == "F1" and send.await_count == 2
        sent = send.await_args_list[1].args[0]
        assert (sent.action, sent.quantity, sent.strategy_tag) == (Action.BUY, 174, "BREAKOUT-PY")
        assert "DUPLICATE CLOSED" in str(notify.await_args_list)

    async def test_two_extra_copies_after_a_partial_exit_close_their_total(self):
        close = TradeResult(status=OrderStatus.SUCCESS, order_id="C1")
        book = [_filled_row("F1"), _filled_row("F2"), _filled_row("F3")]
        # long 100, exit 74: a correct exit leaves +26; two extras leave 26 - 148 = -122
        result, send, _ = await _run_exit(
            [TIMEOUT, close], [book, book], pos=_pos(qty=100), own_qty=-122)
        sent = send.await_args_list[1].args[0]
        assert (sent.action, sent.quantity) == (Action.BUY, 148)

    async def test_an_untagged_extra_copy_is_alerted_not_traded(self):
        """A live broker order book carries no strategy tag, so an 'extra' same-size order could be
        another strategy's own exit (BREAKINGTRADE and -WATCHLIST shared 49 positions)."""
        book = [_filled_row("F1"), _filled_row("F2")]
        untagged = [dict(r, strategy="") for r in book]
        result, send, notify = await _run_exit([TIMEOUT], [book, untagged], own_qty=-74)
        assert send.await_count == 1
        assert "DUPLICATE FILLS" in str(notify.await_args_list)

    async def test_another_strategys_same_size_order_is_neither_adopted_nor_a_duplicate(self):
        theirs = _filled_row("W1")
        theirs["strategy"] = "BREAKINGTRADE-WATCHLIST"
        mine = _filled_row("F1")
        # one correct exit of this strategy's 74 leaves its share at 0 (W1 is the other strategy's)
        result, send, notify = await _run_exit([TIMEOUT], [[theirs, mine]])
        assert (result.status, result.order_id) == (OrderStatus.SUCCESS, "F1")
        assert send.await_count == 1 and "DUPLICATE" not in str(notify.await_args_list)

    async def test_another_strategys_order_alone_is_not_adopted(self):
        theirs = _filled_row("W1")
        theirs["strategy"] = "BREAKINGTRADE-WATCHLIST"
        result, send, _ = await _run_exit([TIMEOUT], [[theirs]] * 3 + [None, None])
        assert result.order_id != "W1" and send.await_count == 1

    @pytest.mark.parametrize("own_qty", [None, 0, -100])
    async def test_a_position_that_does_not_prove_the_excess_is_alerted_not_traded(self, own_qty):
        """Policy updated 2026-10-25: a position that does not show the fills also means the exit is
        not adopted - it is held and alerted, and nothing more is sent."""
        book = [_filled_row("F1"), _filled_row("F2")]
        result, send, notify = await _run_exit([TIMEOUT], [book, book], own_qty=own_qty)
        assert send.await_count == 1 and result.status == OrderStatus.TIMEOUT
        assert "F2" in str(notify.await_args_list)

    async def test_a_close_that_times_out_is_never_resent(self):
        book = [_filled_row("F1"), _filled_row("F2")]
        result, send, notify = await _run_exit([TIMEOUT, TIMEOUT], [book, book], own_qty=-74)
        assert send.await_count == 2
        assert "DUPLICATE NOT CLOSED" in str(notify.await_args_list)

    async def test_sweeper_closes_the_excess_of_an_exit_it_finds_filled_twice(self):
        from signal_engine.main import sweep_unresolved_orders

        pos = _tracked()
        _mark_exit()
        send = AsyncMock(return_value=TradeResult(status=OrderStatus.SUCCESS, order_id="C1"))
        tracker = MagicMock(find_position=MagicMock(return_value=pos),
                            fetch_own_position=AsyncMock(return_value=-74))
        with (
            patch("signal_engine.main.fetch_orderbook",
                  AsyncMock(return_value=[_filled_row("F1"), _filled_row("F2")])),  # read twice
            patch("signal_engine.main.send_order", send),
            patch("signal_engine.main.notifier", MagicMock(notify=AsyncMock())),
            patch("signal_engine.main.tracker", tracker),
        ):
            await sweep_unresolved_orders()
        sent = send.await_args.args[0]
        assert (sent.action, sent.quantity) == (Action.BUY, 74)


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

    async def test_clean_book_without_the_order_is_watched_not_written_off(self):
        """Policy updated 2026-10-17 (was: a clean empty book is "a genuine failure", nothing kept).

        NTPC, AXISBANK and KOTAKBANK 2026-10-15 filled seconds after three clean reads and sat
        unprotected for an hour. The send still counts as failed (result None) but the entry is
        watched for a bounded window; adoption, the entry hold, the release after the window and
        the unreadable-book rule are pinned in test_late_entry_recovery.py.
        """
        result, _ = await _run_entry_recovery([[], [], []])
        assert result is None
        pending = oc.get_uncertain("BREAKOUT-PY", "KALYANKJIL")
        assert pending is not None and pending.kind == "ENTRY"
        assert pending.watch_until is not None

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


@pytest.mark.asyncio
class TestTimedOutExitIgnoresOtherFills:
    """Same symbol, side and size as the timed-out exit, but not ITS copy: never adopted.

    Adopting one books a close that was never sent and leaves the position open with no stop.
    The send then has to be retried once the book is proven clean.
    """

    @staticmethod
    def _flattrade_stamp(dt):
        return dt.strftime("%H:%M:%S %d-%m-%Y")

    async def _run(self, book, booked=frozenset()):
        ok = TradeResult(status=OrderStatus.SUCCESS, order_id="RESENT")
        with patch("signal_engine.main.fetch_order_ids_today", MagicMock(return_value=set(booked))):
            return await _run_exit([TIMEOUT, ok], [book] * 6)

    async def test_an_earlier_day_first_fill_is_not_adopted(self):
        from datetime import timedelta

        earlier = {**_filled_row("OLD"), "timestamp": self._flattrade_stamp(datetime.now(IST) - timedelta(hours=1))}
        result, send, _ = await self._run([earlier])
        assert send.await_count == 2 and result.order_id == "RESENT"

    async def test_a_fill_already_booked_today_is_not_adopted(self):
        result, send, _ = await self._run([_filled_row("BOOKED")], booked={"BOOKED"})
        assert send.await_count == 2 and result.order_id == "RESENT"

    async def test_another_strategys_fill_is_not_adopted(self):
        theirs = {**_filled_row("THEIRS"), "strategy": "ORB"}
        result, send, _ = await self._run([theirs])
        assert send.await_count == 2 and result.order_id == "RESENT"

    async def test_its_own_day_first_fill_is_still_adopted(self):
        mine = {**_filled_row("MINE"), "timestamp": self._flattrade_stamp(datetime.now(IST))}
        result, send, _ = await self._run([mine])
        assert send.await_count == 1 and result.order_id == "MINE"


@pytest.fixture(autouse=True)
def _clean_late_duplicate_watches():
    getattr(oc, "_late_duplicates", {}).clear()
    yield
    getattr(oc, "_late_duplicates", {}).clear()


@pytest.mark.asyncio
class TestAdoptionNeedsThePositionToShowTheExit:
    """2026-10-25 owner rule: before an order is adopted as the confirmed exit, the broker position
    (this strategy's share) must show it. Otherwise the exit is held, nothing is resent, and the
    alert says the position is unprotected and names the candidate orders."""

    async def test_a_filled_copy_with_the_position_unchanged_is_held(self):
        result, send, notify = await _run_exit([TIMEOUT], [[_filled_row("F1")]], own_qty=74)
        assert send.await_count == 1 and result.status == OrderStatus.TIMEOUT
        assert oc.get_uncertain("BREAKOUT-PY", "LAURUSLABS") is not None
        alerts = str(notify.await_args_list)
        assert "F1" in alerts and "UNPROTECTED" in alerts

    async def test_an_unreachable_broker_position_is_held(self):
        result, send, _ = await _run_exit([TIMEOUT], [[_filled_row("F1")]], own_qty=None)
        assert send.await_count == 1 and result.status == OrderStatus.TIMEOUT

    async def test_one_ambiguous_fill_is_adopted_when_the_position_shows_the_exit(self):
        odd = {**_filled_row("ODD"), "timestamp": "garbage"}
        result, send, notify = await _run_exit([TIMEOUT], [[odd]] * 3)
        assert (result.status, result.order_id) == (OrderStatus.SUCCESS, "ODD")
        assert send.await_count == 1

    async def test_an_ambiguous_fill_with_an_unchanged_position_is_held_never_resent(self):
        odd = {**_filled_row("ODD"), "timestamp": "garbage"}
        result, send, notify = await _run_exit([TIMEOUT], [[odd]] * 3, own_qty=74)
        assert send.await_count == 1 and result.status == OrderStatus.TIMEOUT
        alerts = str(notify.await_args_list)
        assert "ODD" in alerts and "UNPROTECTED" in alerts

    async def test_two_ambiguous_fills_are_never_adopted(self):
        odd = [{**_filled_row("ODD1"), "timestamp": "garbage"}, {**_filled_row("ODD2"), "timestamp": "x"}]
        result, send, _ = await _run_exit([TIMEOUT], [odd] * 3)
        assert send.await_count == 1 and result.status == OrderStatus.TIMEOUT

    async def test_a_held_exit_adopts_on_a_later_attempt_only_with_the_position(self):
        await _run_exit([TIMEOUT], [None] * 3)
        result, send, _ = await _run_exit([], [[_filled_row("F1")]] * 3, own_qty=74)
        assert send.await_count == 0 and result.status == OrderStatus.TIMEOUT
        result, send, _ = await _run_exit([], [[_filled_row("F1")]] * 3)
        assert (result.status, result.order_id) == (OrderStatus.SUCCESS, "F1")


async def _sweep_late(book, *, own_qty, position=None, booked=frozenset(), send_result=None):
    from signal_engine.main import sweep_unresolved_orders

    send = AsyncMock(return_value=send_result or TradeResult(status=OrderStatus.SUCCESS, order_id="C1"))
    notify = AsyncMock()
    tracker = MagicMock(find_position=MagicMock(return_value=position),
                        fetch_own_position=AsyncMock(return_value=own_qty))
    with (
        patch("signal_engine.main.fetch_orderbook", AsyncMock(return_value=book)),
        patch("signal_engine.main.fetch_order_ids_today", MagicMock(return_value=set(booked))),
        patch("signal_engine.main.send_order", send),
        patch("signal_engine.main.notifier", MagicMock(notify=notify)),
        patch("signal_engine.main.tracker", tracker),
    ):
        await sweep_unresolved_orders()
    return send, notify


@pytest.mark.asyncio
class TestLateDuplicateAfterAResend:
    """The original lands AFTER the 120 s watch and after the resend (a sandbox MARKET order can
    keep the server busy for ~6 minutes: three quote attempts on a 120 s client timeout). The exit
    path has already returned, so the sweeper watches for it and treats it as a duplicate."""

    async def _resend(self):
        ok = TradeResult(status=OrderStatus.SUCCESS, order_id="RESENT")
        return await _run_exit([TIMEOUT, ok], [[]] * 5)

    async def test_a_resend_after_a_clean_watch_starts_a_late_duplicate_watch(self):
        result, send, _ = await self._resend()
        assert send.await_count == 2 and result.order_id == "RESENT"
        watch = oc.get_late_duplicate_watch("BREAKOUT-PY", "LAURUSLABS")
        assert watch is not None and "RESENT" in watch.known_ids
        assert (watch.action, watch.quantity, watch.pre_exit_quantity) == ("SELL", 74, 74)

    async def test_an_adopted_exit_starts_no_late_duplicate_watch(self):
        await _run_exit([TIMEOUT], [[_filled_row("F1")]])
        assert oc.get_late_duplicate_watch("BREAKOUT-PY", "LAURUSLABS") is None

    async def test_the_late_original_is_closed_when_tagged_and_the_position_shows_it(self):
        await self._resend()
        # long 74 exited twice: the strategy's share is -74
        send, notify = await _sweep_late([_filled_row("RESENT"), _filled_row("LATE")], own_qty=-74)
        sent = send.await_args.args[0]
        assert (sent.action, sent.quantity, sent.strategy_tag) == (Action.BUY, 74, "BREAKOUT-PY")
        assert oc.get_late_duplicate_watch("BREAKOUT-PY", "LAURUSLABS") is None
        assert "LATE" in str(notify.await_args_list)

    async def test_only_the_resent_order_on_the_book_sends_nothing_and_keeps_watching(self):
        await self._resend()
        send, _ = await _sweep_late([_filled_row("RESENT")], own_qty=0)
        send.assert_not_awaited()
        assert oc.get_late_duplicate_watch("BREAKOUT-PY", "LAURUSLABS") is not None

    async def test_an_untagged_late_original_is_alerted_not_traded(self):
        await self._resend()
        late = {**_filled_row("LATE"), "strategy": ""}
        send, notify = await _sweep_late([_filled_row("RESENT"), late], own_qty=-74)
        send.assert_not_awaited()
        assert "LATE" in str(notify.await_args_list)

    async def test_an_ambiguous_late_copy_is_alerted_and_still_watched(self):
        await self._resend()
        odd = {**_filled_row("ODD"), "timestamp": "garbage"}
        send, notify = await _sweep_late([_filled_row("RESENT"), odd], own_qty=-74)
        send.assert_not_awaited()
        assert "ODD" in str(notify.await_args_list)
        assert oc.get_late_duplicate_watch("BREAKOUT-PY", "LAURUSLABS") is not None

    async def test_an_expired_watch_is_dropped_without_a_book_read(self):
        from dataclasses import replace

        await self._resend()
        w = oc.get_late_duplicate_watch("BREAKOUT-PY", "LAURUSLABS")
        oc.watch_late_duplicate(replace(w, until=datetime(2000, 1, 1, tzinfo=IST)))
        send, _ = await _sweep_late([_filled_row("RESENT"), _filled_row("LATE")], own_qty=-74)
        send.assert_not_awaited()
        assert oc.get_late_duplicate_watch("BREAKOUT-PY", "LAURUSLABS") is None

    async def test_the_watch_never_blocks_a_stop_or_a_new_entry(self):
        """A held order blocks the tracker's missing-stop retry and new entries; this watch must
        not, or a partial exit's remaining shares would sit without a stop for ten minutes."""
        await self._resend()
        assert oc.get_uncertain("BREAKOUT-PY", "LAURUSLABS") is None
