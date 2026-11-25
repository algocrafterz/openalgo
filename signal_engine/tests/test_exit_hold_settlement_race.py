"""One unconfirmed order, two paths that can settle it: the sweep and a signal path.

The sweep (tracker poll) and a signal exit both reconcile the same hold. A signal exit holds the
leg's exit lock while it settles; the sweep leaves a hold alone while that lock is held and looks
again next cycle. A path that holds no lock (the no-progress exit, a new entry) can still lose the
hold to the sweep, so every path also re-reads the hold after each await and acts only while it is
still the registered one.

Each test names the guard it pins: removing that guard alone fails it.
"""

import asyncio
import dataclasses
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import main, order_reconcile
from signal_engine.analysis import execution_health
from signal_engine.models import Action, Direction, Order, OrderStatus, Signal, TradeResult
from signal_engine.tests.test_main_single_instance import _book_row, _GatedBook, _hold_since
from signal_engine.tests.tracker_fixtures import _make_engine, _make_position
from signal_engine.timeutils import IST
from signal_engine.tracker import PositionTracker

SYM, STRAT = "SUNPHARMA", "EVWAP-EARLY-PY"
STOP = "STOP-CANCELLED-1"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    tables = (
        order_reconcile._uncertain,
        order_reconcile._last_checked,
        order_reconcile._last_alerted,
    )
    for t in tables:
        t.clear()
    monkeypatch.setattr(main, "_exit_locks", {})
    yield
    for t in tables:
        t.clear()


class _Env:
    """A tracked 50-share long with an unconfirmed SELL 50 hold, and every broker edge stubbed."""

    def __init__(self, rows, *, own=-50, hold_qty=50, slow=None, own_gate=None):
        self.tracker = PositionTracker(_make_engine())
        self.pos = _make_position(symbol=SYM, strategy=STRAT, quantity=50, sl_order_id=STOP)
        self.pos.exit_pending = True
        self.pos.claimed_exit = "TIME_EXIT"
        self.tracker.register(self.pos)
        self.pending = order_reconcile.UncertainOrder(
            STRAT, SYM, "SELL", hold_qty, _hold_since(), "EXIT"
        )
        order_reconcile.mark_uncertain(self.pending)
        self.book = _GatedBook(rows, slow)
        self.send = AsyncMock(
            return_value=TradeResult(order_id="SENT-1", status=OrderStatus.SUCCESS)
        )
        self.status = AsyncMock(return_value="cancelled")
        self.own_calls = 0
        self.own_entered, self.own_release = asyncio.Event(), asyncio.Event()
        self._own_gate, self._own = own_gate, own
        self.settings = dataclasses.replace(
            main.settings,
            entry_timeout_recovery_attempts=1,
            entry_timeout_recovery_delay_seconds=0.0,
        )

    async def _fetch_own(self, *_a, **_k):
        if (
            self._own_gate
            and asyncio.current_task().get_name() == self._own_gate
            and not self.own_calls
        ):
            self.own_calls += 1
            self.own_entered.set()
            await self.own_release.wait()
        return self._own

    def __enter__(self):
        self._patches = (
            patch.object(main, "tracker", self.tracker),
            patch.object(main, "settings", self.settings),
            patch.object(main, "fetch_orderbook", self.book),
            patch.object(main, "fetch_order_ids_today", MagicMock(return_value=set())),
            patch.object(main, "fetch_order_status", self.status),
            patch.object(main, "send_order", self.send),
            patch.object(PositionTracker, "fetch_own_position", self._fetch_own),
            patch.object(PositionTracker, "fetch_own_position_proven", self._fetch_own),
        )
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *_exc):
        for p in self._patches:
            p.stop()

    def exit_order(self):
        return main.build_exit_order(
            symbol=SYM, exchange=self.pos.exchange, quantity=50, product=self.pos.product,
            strategy_tag=STRAT, direction=Direction.SHORT,
        )  # fmt: skip

    async def signal_exit(self, *, locked):
        """What a signal exit does: take the leg's lock, then settle and send."""
        if not locked:
            return await main._send_exit_with_retries(self.exit_order(), self.pos)
        async with main._get_exit_lock(SYM, STRAT):
            return await main._send_exit_with_retries(self.exit_order(), self.pos)


def _two_copies():
    return [_book_row("EXIT-A", "SELL"), _book_row("EXIT-B", "SELL")]


def _pos_untouched(env):
    return (
        env.pos.sl_order_id == STOP and env.pos.claimed_exit == "TIME_EXIT" and env.pos.exit_pending
    )


# --- the sweep leaves a hold alone while a signal exit holds the leg's lock ----------------------


@pytest.mark.asyncio
async def test_a_skipped_leg_is_settled_by_a_later_sweep_once_the_lock_is_free():
    """Pins: the sweep's lock check (a held leg is skipped) and the next cycle's pickup."""
    with _Env([], own=50) as env:
        lock = main._get_exit_lock(SYM, STRAT)
        await lock.acquire()
        await main.sweep_unresolved_orders()
        assert order_reconcile.get_uncertain(STRAT, SYM) is env.pending  # hold untouched
        assert _pos_untouched(env)
        env.status.assert_not_awaited()
        lock.release()
        order_reconcile._last_checked.clear()
        await main.sweep_unresolved_orders()
        assert order_reconcile.get_uncertain(STRAT, SYM) is None
        assert env.pos.sl_order_id == "" and env.pos.exit_pending is False  # stop restored


@pytest.mark.asyncio
async def test_an_exit_absent_for_the_sweep_is_still_sent_once_by_the_signal_exit_that_holds_the_lock():
    """The dropped-exit case: the signal exit reads the book slowly, the sweep reads it first."""
    with _Env([], slow="signal", own=50) as env:
        signal = asyncio.create_task(env.signal_exit(locked=True), name="signal")
        await asyncio.wait_for(env.book.entered.wait(), 5)
        await main.sweep_unresolved_orders()
        assert order_reconcile.get_uncertain(STRAT, SYM) is env.pending  # the sweep did not take it
        assert _pos_untouched(env)
        env.book.release.set()
        result = await asyncio.wait_for(signal, 5)
    assert result.status == OrderStatus.SUCCESS and not result.settled
    assert env.send.await_count == 1  # one fresh exit, not dropped and not doubled
    assert order_reconcile.get_uncertain(STRAT, SYM) is None


@pytest.mark.asyncio
async def test_a_filled_exit_with_a_duplicate_is_corrected_and_adopted_once_by_the_locked_signal_exit():
    with _Env(_two_copies(), slow="signal") as env:
        signal = asyncio.create_task(env.signal_exit(locked=True), name="signal")
        await asyncio.wait_for(env.book.entered.wait(), 5)
        await main.sweep_unresolved_orders()
        assert _pos_untouched(env)  # the sweep wrote no close id and sent no corrective order
        env.book.release.set()
        result = await asyncio.wait_for(signal, 5)
    assert result.status == OrderStatus.SUCCESS and result.order_id == "EXIT-A"  # booked once
    assert env.send.await_count == 1  # the one corrective order


@pytest.mark.asyncio
async def test_a_signal_path_without_the_lock_that_loses_a_filled_hold_reports_it_settled():
    """The no-progress exit holds no lock: the sweep may settle its hold during its read."""
    with _Env(_two_copies(), slow="signal") as env:
        signal = asyncio.create_task(env.signal_exit(locked=False), name="signal")
        await asyncio.wait_for(env.book.entered.wait(), 5)
        await main.sweep_unresolved_orders()
        env.book.release.set()
        result = await asyncio.wait_for(signal, 5)
    assert result.settled is True
    assert (
        env.send.await_count == 1
    )  # the sweep's one corrective order, nothing from the signal path
    assert env.pos.sl_order_id == "EXIT-A"  # the sweep recorded the real close id for the poll


# --- each guard alone ------------------------------------------------------------------------------


async def _sweep_while(env, *, gate, event):
    """Run the sweep, pause it at `gate` (book | decision), apply `event`, let it finish."""
    if gate == "book":
        env.book.slow = "sweep"
    else:
        env._own_gate = "sweep"
    lock = main._get_exit_lock(SYM, STRAT)
    sweep = asyncio.create_task(main.sweep_unresolved_orders(), name="sweep")
    await asyncio.wait_for((env.book.entered if gate == "book" else env.own_entered).wait(), 5)
    if event == "cleared":
        order_reconcile.clear_uncertain(STRAT, SYM)
    else:
        await lock.acquire()
    (env.book.release if gate == "book" else env.own_release).set()
    await asyncio.wait_for(sweep, 5)
    if event == "locked":
        lock.release()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "gate,event,rows",
    [
        ("book", "cleared", "filled"),  # pins the identity check after the book read
        ("book", "locked", "filled"),  # pins the lock check after the book read
        ("decision", "cleared", "absent"),  # pins the identity check after the position read
        ("decision", "locked", "absent"),  # pins the lock check after the position read
    ],
)
async def test_the_sweep_acts_on_nothing_once_another_path_takes_the_hold(gate, event, rows):
    with _Env(
        _two_copies() if rows == "filled" else [], own=-50 if rows == "filled" else 50
    ) as env:
        await _sweep_while(env, gate=gate, event=event)
    assert _pos_untouched(env)  # no close id written, no claim cleared, no stop restored
    assert env.send.await_count == 0  # no corrective order
    if event == "locked":
        assert order_reconcile.get_uncertain(STRAT, SYM) is env.pending


@pytest.mark.asyncio
async def test_a_hold_the_sweep_cleared_during_a_filled_adoption_check_is_not_corrected_twice():
    """Pins `_adopt_if_proven(hold=)`: the signal path lost the hold while reading the position."""
    with _Env(_two_copies(), own_gate="signal") as env:
        signal = asyncio.create_task(env.signal_exit(locked=False), name="signal")
        await asyncio.wait_for(env.own_entered.wait(), 5)
        await (
            main.sweep_unresolved_orders()
        )  # settles the filled hold and sends the corrective order
        env.own_release.set()
        result = await asyncio.wait_for(signal, 5)
    assert env.send.await_count == 1
    assert result.settled is True  # pins the re-check after the adoption attempt


@pytest.mark.asyncio
async def test_a_smaller_filled_hold_settled_elsewhere_is_not_reported_as_unconfirmed(logged):
    """Pins the re-check after the book read: without it a size mismatch logs an unconfirmed exit."""
    with _Env([_book_row("EXIT-P", "SELL", qty=20)], hold_qty=20, slow="signal") as env:
        signal = asyncio.create_task(env.signal_exit(locked=False), name="signal")
        await asyncio.wait_for(env.book.entered.wait(), 5)
        order_reconcile.clear_uncertain(STRAT, SYM)  # another path settled the 20-share exit
        env.book.release.set()
        result = await asyncio.wait_for(signal, 5)
    assert result.settled is True
    assert not logged.has("UNCERTAIN", "WARNING")
    assert env.send.await_count == 0


# --- the stop restore's write-back -------------------------------------------------------------------


async def _restore_with_status_read_interrupted(env, interrupt):
    entered, release = asyncio.Event(), asyncio.Event()

    async def status(*_a, **_k):
        entered.set()
        await release.wait()
        return "cancelled"

    order_reconcile.clear_uncertain(STRAT, SYM)
    restored = asyncio.create_task(main._restore_stop_after_absent_exit(env.pending))
    with patch.object(main, "fetch_order_status", status):
        await asyncio.wait_for(entered.wait(), 5)
        interrupt()
        release.set()
        await asyncio.wait_for(restored, 5)


@pytest.mark.asyncio
async def test_the_restore_leaves_a_position_alone_when_a_new_exit_was_left_unconfirmed_meanwhile():
    with _Env([]) as env:

        def new_hold():
            order_reconcile.mark_uncertain(
                order_reconcile.UncertainOrder(STRAT, SYM, "SELL", 50, datetime.now(IST), "EXIT")
            )

        await _restore_with_status_read_interrupted(env, new_hold)
    assert _pos_untouched(env) is True
    assert order_reconcile.get_uncertain(STRAT, SYM) is not None


@pytest.mark.asyncio
async def test_the_restore_writes_nothing_to_a_position_that_was_replaced_during_the_read():
    with _Env([]) as env:

        def replace():
            env.tracker.unregister(SYM, STRAT)

        await _restore_with_status_read_interrupted(env, replace)
    assert _pos_untouched(env) is True


# --- the entry path: declined visibly, never silently --------------------------------------------------


@pytest.mark.asyncio
async def test_an_entry_that_loses_its_hold_to_the_sweep_is_declined_with_a_warning_and_a_record(
    logged,
):
    signal = Signal(
        strategy=STRAT,
        direction=Direction.LONG,
        symbol=SYM,
        entry=1800.0,
        sl=1780.0,
        tp=0.0,
        sig_id="e-1",
    )
    order = Order(
        symbol=SYM, exchange="NSE", action=Action.BUY, quantity=50, price=0.0,
        order_type="MARKET", product="MIS", strategy_tag=STRAT,
    )  # fmt: skip
    pending = order_reconcile.UncertainOrder(
        STRAT, SYM, "BUY", 50, _hold_since(), "ENTRY", signal=signal, order=order
    )  # fmt: skip
    order_reconcile.mark_uncertain(pending)
    book = _GatedBook([_book_row("ENTRY-LATE-7", "BUY")], "signal")
    declined = MagicMock()
    tracker = PositionTracker(_make_engine())
    patches = (
        patch.object(main, "tracker", tracker),
        patch.object(main, "risk_engine", tracker._risk_engine),
        patch.object(main, "fetch_orderbook", book),
        patch.object(main, "fetch_order_ids_today", MagicMock(return_value=set())),
        patch.object(main, "save_declined", declined),
        patch.object(main, "send_bracket_legs", AsyncMock(return_value=(TradeResult(order_id="S1", status=OrderStatus.SUCCESS), None))),
        patch.object(main, "fetch_order_fill_price", AsyncMock(return_value=1800.0)),
        patch.object(main, "save", MagicMock(return_value=1)),
        patch.object(main, "update_entry_fill", MagicMock()),
    )  # fmt: skip
    for p in patches:
        p.start()
    try:
        held = asyncio.create_task(main._entry_held_by_unresolved_order(signal), name="signal")
        await asyncio.wait_for(book.entered.wait(), 5)
        await main.sweep_unresolved_orders()  # adopts the late entry
        book.release.set()
        assert await asyncio.wait_for(held, 5) is True
    finally:
        for p in patches:
            p.stop()
    assert logged.has("declined", "WARNING") and logged.has(SYM, "WARNING")
    declined.assert_called_once()
    assert declined.call_args.kwargs["stage"] == "unconfirmed_order"
    reason = declined.call_args.kwargs["reason"]
    assert "settled while this entry was checking it" in reason
    assert "potentially doubling the position" in reason
    # The entry path did not see the broker confirm anything: the text must not say so.
    assert "confirmed at the broker" not in reason
    assert not logged.has("confirmed at the broker", "DEBUG")
    assert tracker.tracked_count == 1  # adopted once, by the sweep


# --- a settled exit is neither a failure nor an unconfirmed exit -----------------------------------


@pytest.mark.asyncio
async def test_an_exit_settled_by_another_path_logs_no_failure_and_saves_no_row(logged):
    from signal_engine.tests.test_main_characterization import (
        _exit_signal,
        _position,
        _real_tracker,
        harness,
    )

    tracker = _real_tracker()
    pos = _position()
    tracker.register(pos)
    settled = main._exit_settled(pos, "EXIT-A")
    h = harness(tracker=tracker, send_order=AsyncMock(return_value=settled))
    with h.stack, patch.object(main, "_send_exit_with_retries", AsyncMock(return_value=settled)):
        await main._handle_exit(_exit_signal(tp_level="TP1"))
    h.save.assert_not_called()  # no TIMEOUT or UNCERTAIN row for execution health to count
    assert not logged.has("EXIT order failed", "DEBUG")
    assert pos.exit_pending is False and tracker.find_position(pos.symbol, pos.strategy) is pos
    # The wording is plain, and none of it is what execution health counts as an unconfirmed exit.
    lines = [f"EXIT {m}" for m in logged.messages("DEBUG")] + logged.messages("DEBUG")
    assert not any(execution_health._UNCERTAIN.search(line) for line in lines)
    assert not any(execution_health._TIMEOUT.search(line) for line in lines)
    assert not any(
        "sweep" in m.lower() for m in logged.messages("DEBUG") if "confirmed filled" in m
    )
    assert settled.status == OrderStatus.TIMEOUT and "UNCERTAIN" not in settled.message


@pytest.mark.asyncio
async def test_a_no_progress_exit_settled_by_another_path_is_not_logged_as_unconfirmed(logged):
    """Pins the settled branch in the tracker's no-progress exit."""
    from signal_engine.tests.test_close_accounting import _pos

    tracker = PositionTracker(MagicMock())
    pos = _pos("INFY", qty=40, fill=1021.80)
    tracker.register(pos)
    settled = main._exit_settled(pos, "EXIT-A")
    with (
        patch("signal_engine.tracker.cancel_order", AsyncMock(return_value=True)),
        patch.object(main, "_send_exit_with_retries", AsyncMock(return_value=settled)),
        patch("signal_engine.tracker.db.save_tracker_exit", MagicMock()) as save,
    ):
        await tracker._no_progress_market_exit(pos, timedelta(minutes=95), 1024.40, 1021.80, 0.10)
    assert not logged.has("unconfirmed", "WARNING")
    save.assert_not_called()  # the poll books it from the recorded fill
    assert tracker.find_position("INFY", pos.strategy) is pos
