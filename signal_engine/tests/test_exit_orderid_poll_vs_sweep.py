"""BUG-001, second gap: the poll books a held exit's close under the cancelled stop's id.

An exit cancels the leg's stop and sends a MARKET exit. When the exit is not confirmed in about
3 s it is held (order_reconcile) and pos.sl_order_id still names the cancelled stop. The sandbox
can hold that MARKET order for minutes and then fill it. The tracker poll runs every cycle and
the sweep only every 30 s, so the poll saw the flat position first and booked the close under the
cancelled stop (10 of 56 exits on 2026-11-25, 10 of 53 on 2026-11-26, TATAPOWER 11:02:57 booked,
sweep 11:03:31 "completed at the broker"). Fill price, quantity and P&L were right; the EXIT row's
order id was the dead stop's, so the ledger could not match the real exit fill.

The poll now reads the order book for a held full exit, as the sweep does, and books under the
exit's own id. It records an id only: no order is sent, cancelled or skipped differently.

Each test names the guard it pins: removing that guard alone fails it.
"""

import dataclasses
import json
from collections.abc import Callable
from contextlib import ExitStack
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import db, main, order_reconcile
from signal_engine.models import Direction
from signal_engine.tests.test_main_single_instance import _book_row, _hold_since
from signal_engine.tests.tracker_fixtures import _make_engine, _make_position
from signal_engine.timeutils import IST
from signal_engine.tracker import PositionTracker

SYM, STRAT = "SUNPHARMA", "EVWAP-EARLY-PY"
STOP, EXIT_ID = "STOP-CANCELLED-1", "EXIT-REAL-1"
ENTRY_FILL, EXIT_FILL, QTY = 2500.0, 2510.0, 50
EXPECTED_PNL = (EXIT_FILL - ENTRY_FILL) * QTY  # LONG: +500


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    db.set_trade_mode("analyze")
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


def _rows():
    return (
        db._get_connection()
        .execute(
            "SELECT id, order_id, message, context, quantity FROM trades "
            "WHERE direction = 'EXIT' AND status = 'SUCCESS' ORDER BY id"
        )
        .fetchall()
    )


class _Env:
    """A real tracker, risk engine, ledger, hold registry and sweep; the broker edges stubbed.

    `rows` is the order book both the poll and the sweep read. `held` is the positionbook
    quantity. By default the leg is a 50-share long whose SELL 50 exit is held unconfirmed.
    """

    def __init__(
        self,
        *,
        hold=True,
        hold_qty=QTY,
        hold_kind="EXIT",
        since=None,
        rows=None,
        held=QTY,
        claimed="EXIT",
        time_exit_left_open=False,
    ):
        self.engine = _make_engine()
        self.engine._state(STRAT).open_positions = 1
        self.tracker = PositionTracker(self.engine)
        self.pos = _make_position(
            symbol=SYM, strategy=STRAT, product="MIS", quantity=QTY, entry_price=ENTRY_FILL,
            fill_price=ENTRY_FILL, sl=2485.0, tp=2540.0, direction=Direction.LONG,
            entry_order_id="E-1", sl_order_id=STOP, ever_seen_nonzero_qty=True,
        )  # fmt: skip
        self.pos.exit_pending = False
        # main.py:404/1305 set claimed_exit to the signal's tp_level, or "EXIT"; the time exit's
        # timeout hold (main.py:858-870) and the bad-fill close hold (main.py:3131) leave it "".
        self.pos.claimed_exit = claimed
        self.pos.time_exit_left_open = time_exit_left_open
        if time_exit_left_open:
            self.tracker._time_exit_started = datetime.now(IST) - timedelta(minutes=1)
        self.tracker.register(self.pos)
        self.pending = None
        if hold:
            self.pending = order_reconcile.UncertainOrder(
                STRAT, SYM, "SELL", hold_qty, since or _hold_since(), hold_kind
            )
            order_reconcile.mark_uncertain(self.pending)
        self.rows = rows if rows is not None else [_book_row(EXIT_ID, "SELL", status="open")]
        self.held = held
        self.poll_book_hook: Callable | None = None  # called during the poll's own order-book read
        self.poll_book_raises = False
        self.fill_hook: Callable | None = None  # called when the poll reads a fill price
        self.cancel = AsyncMock(
            return_value=False
        )  # HTTP 400: "Cannot cancel order in cancelled status"
        self.place_sl = AsyncMock()
        self.record_close = None

    # -- broker edges --------------------------------------------------------------------------
    async def _main_book(self, *_a, **_k):
        return list(self.rows)

    async def _poll_book(self, *_a, **_k):
        if self.poll_book_raises:
            raise RuntimeError("order book unreadable")
        rows = list(self.rows)
        if self.poll_book_hook:
            self.poll_book_hook()
        return rows

    async def _status(self, order_id, *_a, **_k):
        return {STOP: "cancelled", EXIT_ID: "complete"}.get(order_id, "")

    async def _fill(self, order_id, *_a, **_k):
        if self.fill_hook:
            self.fill_hook(order_id)
        return EXIT_FILL if order_id == EXIT_ID else None  # a cancelled stop never filled

    async def _positionbook(self):
        return [{"symbol": SYM, "quantity": self.held, "ltp": EXIT_FILL, "today_realized_pnl": 0.0}]

    async def _own(self, *_a, **_k):
        return self.held

    def make_flat_and_filled(self):
        self.rows[:] = [_book_row(EXIT_ID, "SELL")]
        self.held = 0

    def throttle_sweep(self):
        """The sweep looked a moment ago (its 30 s throttle), so only the poll acts this cycle."""
        order_reconcile._last_checked[(STRAT, SYM)] = datetime.now(IST)

    async def poll(self):
        await self.tracker.check_positions()

    async def sweep(self):
        order_reconcile._last_checked.clear()  # the 30 s throttle: let this look run now
        await main.sweep_unresolved_orders()

    def __enter__(self):
        self._stack = ExitStack()
        s = self._stack
        self.record_close = s.enter_context(
            patch.object(self.engine, "record_close", wraps=self.engine.record_close)
        )
        for p in (
            patch.object(main, "tracker", self.tracker),
            patch.object(main, "risk_engine", self.engine),
            patch.object(main, "fetch_orderbook", self._main_book),
            patch.object(main, "fetch_order_status", self._status),
            patch.object(main, "place_sl_order", self.place_sl),
            patch("signal_engine.tracker.fetch_orderbook", self._poll_book),
            patch("signal_engine.tracker.fetch_order_status", self._status),
            patch("signal_engine.tracker.fetch_order_fill_price", self._fill),
            patch("signal_engine.tracker.fetch_positionbook", self._positionbook),
            patch("signal_engine.tracker.fetch_realised_pnl", AsyncMock(return_value=0.0)),
            patch("signal_engine.tracker.cancel_order", self.cancel),
            patch("signal_engine.tracker._ladder_owns_exits", return_value=False),
            patch.object(PositionTracker, "fetch_own_position", self._own),
            patch.object(PositionTracker, "fetch_own_position_proven", self._own),
            patch.object(self.tracker, "_check_mode_flip", AsyncMock()),
            patch.object(self.tracker, "_reconcile_stops", AsyncMock()),
            patch.object(self.tracker, "_retry_missing_sl", AsyncMock()),
            patch.object(self.tracker, "_check_no_progress", AsyncMock()),
            patch.object(self.tracker, "_maybe_send_day_summary", AsyncMock()),
            patch.object(self.tracker, "maybe_send_day_summary", AsyncMock()),
        ):
            s.enter_context(p)
        return self

    def __exit__(self, *exc):
        self._stack.close()


def _assert_one_correct_close(env, order_id, label="EXIT"):
    rows = _rows()
    assert len(rows) == 1, [r[1:3] for r in rows]  # one EXIT row, no duplicate
    assert rows[0][1] == order_id
    ctx = json.loads(rows[0][3])
    assert ctx["pnl"] == pytest.approx(EXPECTED_PNL)  # fill price, qty and P&L unchanged
    assert ctx["exit_types"][-1] == label  # the label HEAD's poll gives this close
    assert rows[0][4] == QTY
    assert env.record_close.call_count == 1  # one risk-counter update
    assert env.engine._state(STRAT).open_positions == 0
    assert env.tracker.find_position(SYM, STRAT) is None


# --- (a) the poll sees the flat position first -----------------------------------------------


@pytest.mark.asyncio
async def test_poll_books_a_held_exit_under_its_own_order_id_not_the_cancelled_stop():
    """Pins: _adopt_held_exit_id and its hand-off to pos.sl_order_id (the BUG-001 race itself)."""
    with _Env() as env:
        await env.poll()  # the sweep looks first (exit still working), the leg is still open
        assert _rows() == [] and env.tracker.find_position(SYM, STRAT) is env.pos
        env.make_flat_and_filled()
        await env.poll()  # the 30 s sweep throttle is still running: only the poll acts
        _assert_one_correct_close(env, EXIT_ID)


@pytest.mark.asyncio
async def test_the_sweep_after_the_poll_booked_does_not_book_again_and_the_hold_is_gone():
    """Pins: the post-booking release of the hold (clear_uncertain) and one booking per close."""
    with _Env() as env:
        await env.poll()
        env.make_flat_and_filled()
        await env.poll()
        assert order_reconcile.get_uncertain(STRAT, SYM) is None  # nothing left to settle
        await env.sweep()
        await env.poll()
        _assert_one_correct_close(env, EXIT_ID)


# --- the label stays what the poll gave it; only the row's order id changes (review blocker) ---


@pytest.mark.asyncio
async def test_an_unclaimed_time_exit_hold_is_still_labelled_time_on_the_exit_id():
    """Pins: the id goes to pos.exit_order_id, not sl_order_id (which would read as a filled stop).

    HEAD's poll labels this close TIME (_closing_label: the time exit was left open and the book
    shows this strategy's own completed exit). Writing the exit's id into sl_order_id turned it
    into SL.
    """
    with _Env(claimed="", time_exit_left_open=True) as env:
        await env.poll()
        env.make_flat_and_filled()
        await env.poll()
        _assert_one_correct_close(env, EXIT_ID, label="TIME")
        assert env.pos.sl_order_id == STOP  # still names the stop


@pytest.mark.asyncio
async def test_an_unclaimed_bad_fill_hold_with_a_cancelled_stop_is_still_labelled_squareoff():
    """Pins: the same. HEAD's poll: a stop cancelled before the poll touched it is SQUAREOFF."""
    with _Env(claimed="") as env:
        await env.poll()
        env.make_flat_and_filled()
        await env.poll()
        _assert_one_correct_close(env, EXIT_ID, label="SQUAREOFF")
        assert env.pos.sl_order_id == STOP


@pytest.mark.asyncio
async def test_a_claimed_signal_exit_keeps_its_tp_label_on_the_exit_id():
    """A signal exit's claim wins the label: TP1 here, and the exit's id on the row."""
    with _Env(claimed="TP1") as env:
        await env.poll()
        env.make_flat_and_filled()
        await env.poll()
        _assert_one_correct_close(env, EXIT_ID, label="TP1")


@pytest.mark.asyncio
async def test_without_the_stop_cancel_the_label_decision_reads_the_stop_not_the_exit():
    """Pins: the stop-status read. The stop is read once (before the poll's own cancel)."""
    with _Env(claimed="") as env:
        await env.poll()
        env.make_flat_and_filled()
        reads = []
        orig = env._status

        async def spy(order_id, *a, **k):
            reads.append(order_id)
            return await orig(order_id, *a, **k)

        with patch("signal_engine.tracker.fetch_order_status", spy):
            await env.poll()
        assert STOP in reads and EXIT_ID not in reads[: reads.index(STOP) + 1]


# --- (b) the sweep gets there first ----------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_first_books_under_the_exit_id_and_the_poll_finds_nothing_to_book():
    """Control: the sweep-wins ordering was already right; the fix must not disturb it."""
    with _Env() as env:
        env.make_flat_and_filled()
        await env.poll()  # sweep (due: first look) hands the id over, then the poll books once
        _assert_one_correct_close(env, EXIT_ID)
        await env.poll()
        assert len(_rows()) == 1


# --- (c) no hold: an ordinary stop hit -------------------------------------------------------


@pytest.mark.asyncio
async def test_without_a_hold_the_close_is_booked_under_the_stop_as_before():
    """Control: behaviour unchanged when no exit is held."""
    with _Env(hold=False) as env:
        env.pos.claimed_exit = ""
        env.make_flat_and_filled()
        await env.poll()
        rows = _rows()
        assert len(rows) == 1 and rows[0][1] == STOP
        assert env.pos.exit_order_id == ""
        assert env.record_close.call_count == 1
        assert env.cancel.await_count >= 1  # the poll still tries the (already cancelled) stop


# --- (d) the exit is later proven absent: nothing was closed ----------------------------------


@pytest.mark.asyncio
async def test_an_exit_proven_absent_with_the_position_open_books_nothing_and_restores_the_stop():
    """Pins: the fix adds no booking while the broker still holds the position."""
    with _Env(rows=[], held=QTY) as env:
        await env.poll()  # the sweep reads the book clean, the broker shows the full position
        assert _rows() == [] and env.record_close.call_count == 0
        assert env.tracker.find_position(SYM, STRAT) is env.pos
        assert order_reconcile.get_uncertain(STRAT, SYM) is None  # hold released by the sweep
        assert env.pos.sl_order_id == ""  # the poll's missing-stop retry places a fresh one
        assert env.pos.claimed_exit == ""  # the exit never reached the broker
        assert env.pos.exit_order_id == ""
        await env.poll()
        assert _rows() == [] and env.record_close.call_count == 0


@pytest.mark.asyncio
async def test_a_flat_position_with_no_copy_of_the_exit_on_the_book_books_under_the_stop_as_before():
    """Fallback: with nothing proven the poll books as it always did, and leaves the hold."""
    window = _hold_since(5) + timedelta(minutes=5)  # inside the late-fill watch: sweep keeps it
    with _Env(rows=[], held=0) as env:
        env.pending = dataclasses.replace(env.pending, watch_until=window)
        order_reconcile.mark_uncertain(env.pending)
        await env.poll()
        rows = _rows()
        assert len(rows) == 1 and rows[0][1] == STOP
        assert env.pos.exit_order_id == ""
        assert order_reconcile.get_uncertain(STRAT, SYM) is env.pending  # left to the sweep
        assert env.pos.exit_order_id == ""


# --- (e) idempotency -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_polls_and_sweeps_leave_one_row_and_one_risk_record():
    """Pins: one close, one booking (poll twice, sweep twice)."""
    with _Env() as env:
        await env.poll()
        env.make_flat_and_filled()
        for _ in range(2):
            await env.poll()
            await env.sweep()
        _assert_one_correct_close(env, EXIT_ID)


# --- (f) the cancel of the already cancelled stop ---------------------------------------------


@pytest.mark.asyncio
async def test_a_cancel_that_raises_does_not_stop_the_close_being_booked_on_the_exit_id():
    with _Env() as env:
        env.cancel.side_effect = RuntimeError("HTTP 400 Cannot cancel order in cancelled status")
        env.make_flat_and_filled()
        await env.poll()
        _assert_one_correct_close(env, EXIT_ID)


@pytest.mark.asyncio
async def test_the_fallback_still_books_when_the_stop_cancel_returns_400():
    with _Env(rows=[], held=0) as env:
        env.cancel.return_value = False
        await env.poll()
        rows = _rows()
        assert len(rows) == 1 and rows[0][1] == STOP
        assert env.pos.exit_order_id == ""
        assert env.cancel.await_count >= 1
        assert env.record_close.call_count == 1


# --- guards on what may be adopted -----------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unreadable_order_book_never_blocks_the_booking():
    """Pins: the read's exception guard. The close is booked as before, under the stop."""
    with _Env() as env:
        env.make_flat_and_filled()
        env.poll_book_raises = True
        env.throttle_sweep()  # the sweep is not what settles it here
        await env.poll()
        rows = _rows()
        assert len(rows) == 1 and rows[0][1] == STOP and env.record_close.call_count == 1
        assert env.pos.exit_order_id == ""


@pytest.mark.asyncio
async def test_a_partial_exit_hold_is_never_adopted_as_the_closing_order():
    """Pins: pending.quantity < pos.quantity. A partial's id never closes the position."""
    with _Env(hold_qty=20, rows=[_book_row("PARTIAL-1", "SELL", qty=20)]) as env:
        env.held = 0
        order = await env.tracker._adopt_held_exit_id(f"{SYM}:{STRAT}", env.pos)
        assert order is None and env.pos.sl_order_id == STOP
        assert env.pos.exit_order_id == ""


@pytest.mark.asyncio
async def test_an_entry_hold_is_not_an_exit_and_is_never_adopted():
    """Pins: pending.kind == EXIT."""
    with _Env(hold_kind="ENTRY") as env:
        env.make_flat_and_filled()
        order = await env.tracker._adopt_held_exit_id(f"{SYM}:{STRAT}", env.pos)
        assert order is None and env.pos.sl_order_id == STOP
        assert env.pos.exit_order_id == ""


@pytest.mark.asyncio
async def test_a_hold_from_an_earlier_day_is_not_adopted():
    """Pins: the same-day check."""
    with _Env(since=_hold_since() - timedelta(days=1)) as env:
        env.make_flat_and_filled()
        order = await env.tracker._adopt_held_exit_id(f"{SYM}:{STRAT}", env.pos)
        assert order is None and env.pos.sl_order_id == STOP
        assert env.pos.exit_order_id == ""


@pytest.mark.asyncio
async def test_duplicate_copies_of_the_exit_are_left_to_the_sweep():
    """Pins: duplicate_ids. Two completed copies are a duplicate-exit case, not a plain close."""
    rows = [_book_row("EXIT-A", "SELL"), _book_row("EXIT-B", "SELL")]
    with _Env(rows=rows) as env:
        order = await env.tracker._adopt_held_exit_id(f"{SYM}:{STRAT}", env.pos)
        assert order is None and env.pos.sl_order_id == STOP
        assert env.pos.exit_order_id == ""


@pytest.mark.asyncio
async def test_an_order_this_engine_already_booked_today_is_not_taken_for_the_exit():
    """Pins: exclude=db.fetch_order_ids_today (an earlier booked fill is not this exit)."""
    db.save_tracker_exit(
        strategy=STRAT, symbol=SYM, entry=ENTRY_FILL, sl=2485.0, tp=2540.0, quantity=QTY,
        exit_price=EXIT_FILL, pnl=EXPECTED_PNL, exit_types=["TP1"], order_id="OLD-BOOKED",
        sig_id="", includes_leg_pnl=0.0,
    )  # fmt: skip
    with _Env(rows=[_book_row("OLD-BOOKED", "SELL")]) as env:
        order = await env.tracker._adopt_held_exit_id(f"{SYM}:{STRAT}", env.pos)
        assert order is None and env.pos.sl_order_id == STOP
        assert env.pos.exit_order_id == ""


# --- another path settles or replaces the hold while the poll works ---------------------------


@pytest.mark.asyncio
async def test_a_hold_settled_during_the_poll_book_read_is_not_adopted_over_the_other_path():
    """Pins: the identity re-check after the read (the pattern the sweep and signal paths use)."""
    with _Env() as env:
        env.make_flat_and_filled()
        newer = order_reconcile.UncertainOrder(STRAT, SYM, "SELL", QTY, _hold_since(5), "EXIT")

        def replaced():
            order_reconcile.mark_uncertain(newer)

        env.poll_book_hook = replaced
        order = await env.tracker._adopt_held_exit_id(f"{SYM}:{STRAT}", env.pos)
        assert order is None and env.pos.sl_order_id == STOP
        assert env.pos.exit_order_id == ""
        assert order_reconcile.get_uncertain(STRAT, SYM) is newer


@pytest.mark.asyncio
async def test_a_hold_replaced_during_the_booking_survives_the_polls_release():
    """Pins: the identity check before clear_uncertain."""
    with _Env() as env:
        env.make_flat_and_filled()
        newer = order_reconcile.UncertainOrder(STRAT, SYM, "SELL", QTY, _hold_since(5), "EXIT")
        env.fill_hook = lambda _oid: order_reconcile.mark_uncertain(newer)
        env.throttle_sweep()  # the sweep is not what settles it here
        await env.poll()
        assert len(_rows()) == 1 and _rows()[0][1] == EXIT_ID
        assert order_reconcile.get_uncertain(STRAT, SYM) is newer


@pytest.mark.asyncio
async def test_the_leg_unregistered_during_the_read_is_not_touched():
    """Pins: the registered-position re-check after the read."""
    with _Env() as env:
        env.make_flat_and_filled()
        env.poll_book_hook = lambda: env.tracker.unregister(SYM, STRAT)
        order = await env.tracker._adopt_held_exit_id(f"{SYM}:{STRAT}", env.pos)
        assert order is None and env.pos.sl_order_id == STOP
        assert env.pos.exit_order_id == ""
