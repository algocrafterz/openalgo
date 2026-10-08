"""One actual close produces one ledger close event and one risk-counter update.

2026-10-05 BREAKOUT-PY KOTAKBANK (SHORT 723 @ 418.50, closed @ 415.85). The signal-driven TP1
exit booked the close at 11:15:11. One second later the tracker poll saw the position at qty 0,
still registered, and booked the SAME close again as a stop hit: two EXIT rows with the same order
id and the same +1,915.95, a second risk_engine.record_close(), and day counters 22 -> 23.

The cause was ordering. The signal exit unregistered the position only AFTER its booking awaits,
and the poll never read pos.exit_pending, the flag that says an exit handler owns the position.
The reverse race was worse: a signal exit arriving while the poll was booking would place a SELL
on a flat position.

These tests make the other path fire exactly inside the booking window, which is where the
real race happened. The invariant is I2 in docs/ACCOUNTING-INVARIANTS.md.
"""

import json
from contextlib import ExitStack
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import db, main
from signal_engine.models import Direction, OrderStatus, TradeResult
from signal_engine.tests.tracker_fixtures import _make_engine, _make_position
from signal_engine.tracker import PositionTracker

STRATEGY = "BREAKOUT-PY"
SYMBOL = "KOTAKBANK"
SIG_ID = "BREAKOUT-PY-KOTAKBANK-20261005-1020"
CLOSE_FILL = 415.85
EXPECTED_PNL = (418.5 - CLOSE_FILL) * 723  # SHORT: 1,915.95


@pytest.fixture(autouse=True)
def _analyze_mode():
    db.set_trade_mode("analyze")
    yield


def _position():
    return _make_position(
        symbol=SYMBOL, strategy=STRATEGY, product="MIS", quantity=723, entry_price=418.5,
        fill_price=418.5, sl=420.0, tp=415.0, direction=Direction.SHORT,
        entry_order_id="E-1", sl_order_id="SL-1", sig_id=SIG_ID, ever_seen_nonzero_qty=True,
    )


def _closed_book():
    return [{"symbol": SYMBOL, "quantity": 0, "ltp": CLOSE_FILL, "today_realized_pnl": 0.0}]


def _exit_signal():
    return SimpleNamespace(
        symbol=SYMBOL, strategy=STRATEGY, tp_level="TP1", tp=CLOSE_FILL, exit_qty_pct=1.0,
        direction=Direction.EXIT, sig_id=SIG_ID, exchange="", product="", entry=0.0, sl=0.0,
    )


def _rows():
    return db._get_connection().execute(
        "SELECT id, direction, order_id, message, context FROM trades "
        "WHERE direction = 'EXIT' AND status = 'SUCCESS' ORDER BY id"
    ).fetchall()


class _Harness:
    """A real tracker, risk engine and ledger with the broker edges mocked."""

    def __init__(self):
        self.engine = _make_engine()
        self.engine._state(STRATEGY).open_positions = 1
        self.tracker = PositionTracker(self.engine)
        self.pos = _position()
        self.tracker.register(self.pos)
        self.send_order = AsyncMock(
            return_value=TradeResult(status=OrderStatus.SUCCESS, order_id="EXIT-1")
        )
        self.tracker_notifier = AsyncMock()

    def patches(self):
        return [
            patch("signal_engine.main.tracker", self.tracker),
            patch("signal_engine.main.risk_engine", self.engine),
            patch("signal_engine.main.send_order", self.send_order),
            patch("signal_engine.main.cancel_order", AsyncMock(return_value=True)),
            patch("signal_engine.main.fetch_order_fill_price", AsyncMock(return_value=CLOSE_FILL)),
            patch("signal_engine.main._book_realised_pnl_delta", AsyncMock(return_value=(0.0, 0.0))),
            patch("signal_engine.main.notifier", AsyncMock()),
            patch("signal_engine.tracker.notifier", self.tracker_notifier),
            patch("signal_engine.tracker.cancel_order", AsyncMock(return_value=False)),
            patch("signal_engine.tracker.fetch_order_fill_price", AsyncMock(return_value=CLOSE_FILL)),
            patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=_closed_book())),
            patch("signal_engine.tracker.fetch_realised_pnl", AsyncMock(return_value=0.0)),
            patch("signal_engine.tracker.fetch_orderbook", AsyncMock(return_value=[])),
            patch.object(self.tracker, "_check_mode_flip", AsyncMock()),
            patch.object(self.tracker, "_maybe_send_day_summary", AsyncMock()),
            patch.object(self.tracker, "maybe_send_day_summary", AsyncMock()),
            # BREAKOUT-PY's exits belong to the Ladder, which makes _check_no_progress skip it
            # before any guard is reached. Turn that off so the guards are what is tested.
            patch("signal_engine.tracker._ladder_owns_exits", return_value=False),
        ]

    def run_inside_close_notification(self, coro_factory):
        """Run `coro_factory()` once, from inside the close notification: the booking window."""
        fired = []

        async def hook(*_a, **_k):
            if not fired:
                fired.append(True)
                await coro_factory()

        self.tracker_notifier.notify_position_closed.side_effect = hook


async def _run(harness, scenario):
    with ExitStack() as stack:
        record_close = stack.enter_context(
            patch.object(harness.engine, "record_close", wraps=harness.engine.record_close)
        )
        for p in harness.patches():
            stack.enter_context(p)
        await scenario()
    return record_close


@pytest.mark.asyncio
class TestSignalExitAndPollBookTheSameCloseOnce:
    async def test_the_poll_firing_during_a_signal_exits_booking_does_not_book_again(self):
        h = _Harness()
        h.run_inside_close_notification(h.tracker.check_positions)

        async def scenario():
            await main._handle_exit_locked(_exit_signal())

        record_close = await _run(h, scenario)

        rows = _rows()
        assert len(rows) == 1, [r[3] for r in rows]  # one ledger close
        ctx = json.loads(rows[0][4])
        assert ctx["pnl"] == pytest.approx(EXPECTED_PNL)  # one P&L realization
        assert rows[0][2] == "EXIT-1"  # the closing order's own id (B1)
        assert "TP1" in rows[0][3]  # booked by the signal exit, not as a stop hit
        assert record_close.call_count == 1  # one risk-counter update
        assert h.engine._state(STRATEGY).open_positions == 0  # decremented once
        assert h.send_order.await_count == 1  # one broker close
        assert h.tracker.find_position(SYMBOL, STRATEGY) is None  # correct final state
        trades = db.fetch_day_trades("analyze")
        assert len(trades) == 1 and trades[0]["total_pnl"] == pytest.approx(EXPECTED_PNL)

    async def test_a_signal_exit_arriving_during_the_polls_booking_places_no_order(self):
        """The reverse race: without the guard this would SELL a position that is already flat."""
        h = _Harness()
        h.run_inside_close_notification(lambda: main._handle_exit_locked(_exit_signal()))

        async def scenario():
            await h.tracker.check_positions()

        record_close = await _run(h, scenario)

        assert h.send_order.await_count == 0  # no exit order on a flat position
        assert len(_rows()) == 1
        assert record_close.call_count == 1
        assert h.tracker.find_position(SYMBOL, STRATEGY) is None
        assert len(db.fetch_day_trades("analyze")) == 1

    async def test_an_sl_hit_reconcile_alert_racing_the_poll_books_once(self):
        h = _Harness()
        h.run_inside_close_notification(h.tracker.check_positions)
        signal = _exit_signal()
        signal.tp_level = "SL"

        async def scenario():
            await main._handle_exit_locked(signal)

        record_close = await _run(h, scenario)

        assert len(_rows()) == 1
        assert record_close.call_count == 1
        assert h.send_order.await_count == 0  # an SL-hit reconcile never places an order
        assert h.tracker.find_position(SYMBOL, STRATEGY) is None


@pytest.mark.asyncio
class TestTheFlagNeverStrandsAPosition:
    async def test_a_poll_booking_that_fails_releases_the_flag(self):
        h = _Harness()

        async def scenario():
            with patch.object(
                h.tracker, "_book_broker_close", AsyncMock(side_effect=RuntimeError("boom"))
            ):
                with pytest.raises(RuntimeError):
                    await h.tracker.check_positions()

        await _run(h, scenario)
        assert h.pos.exit_pending is False  # a later poll or exit can still act

    async def test_a_poll_booking_that_decides_not_to_close_releases_the_flag(self):
        h = _Harness()

        async def scenario():
            with patch.object(h.tracker, "_book_broker_close", AsyncMock(return_value=False)):
                await h.tracker.check_positions()

        await _run(h, scenario)
        assert h.pos.exit_pending is False

    async def test_the_poll_marks_the_position_while_it_books(self):
        h = _Harness()
        seen = {}

        async def booking(*_a, **_k):
            seen["pending"] = h.pos.exit_pending
            return True

        async def scenario():
            with patch.object(h.tracker, "_book_broker_close", booking):
                await h.tracker.check_positions()

        await _run(h, scenario)
        assert seen["pending"] is True

    async def test_the_poll_skips_a_position_whose_exit_is_in_progress(self):
        h = _Harness()
        h.pos.exit_pending = True

        async def scenario():
            await h.tracker.check_positions()

        record_close = await _run(h, scenario)
        assert _rows() == [] and record_close.call_count == 0
        assert h.tracker.find_position(SYMBOL, STRATEGY) is h.pos  # left for the exit handler


@pytest.mark.asyncio
class TestNoProgressExitAndSignalExitDoNotCollide:
    async def test_no_progress_skips_a_position_whose_exit_is_in_progress(self):
        h = _Harness()
        h.pos.exit_pending = True
        applied = AsyncMock()

        async def scenario():
            with (
                patch.object(h.tracker, "_evaluate_no_progress", return_value=("fired",)),
                patch.object(h.tracker, "_apply_no_progress_action", applied),
            ):
                await h.tracker._check_no_progress({})

        await _run(h, scenario)
        applied.assert_not_awaited()

    async def test_no_progress_acts_on_a_position_with_no_exit_in_progress(self):
        """Control: the same setup does act when nothing owns the position."""
        h = _Harness()
        applied = AsyncMock()

        async def scenario():
            with (
                patch.object(h.tracker, "_evaluate_no_progress", return_value=("fired",)),
                patch.object(h.tracker, "_apply_no_progress_action", applied),
            ):
                await h.tracker._check_no_progress({})

        await _run(h, scenario)
        applied.assert_awaited_once()

    async def test_no_progress_skips_a_position_another_path_already_removed(self):
        h = _Harness()
        applied = AsyncMock()

        async def scenario():
            with (
                patch.object(h.tracker, "_positions", {"stale-key": h.pos}),
                patch.object(h.tracker, "_evaluate_no_progress", return_value=("fired",)),
                patch.object(h.tracker, "_apply_no_progress_action", applied),
            ):
                await h.tracker._check_no_progress({})

        await _run(h, scenario)
        applied.assert_not_awaited()

    async def test_a_no_progress_exit_marks_the_position_while_it_acts(self):
        h = _Harness()
        seen = {}

        async def send(_order):
            seen["pending"] = h.pos.exit_pending
            return TradeResult(status=OrderStatus.REJECTED, message="no")

        fired = (timedelta(minutes=90), 416.0, 0.1, 418.5, 0.2, "main")

        async def scenario():
            from datetime import datetime

            from signal_engine.timeutils import IST

            with (
                patch("signal_engine.tracker.send_order", send),
                patch.object(PositionTracker, "_should_market_exit", return_value=True),
            ):
                await h.tracker._apply_no_progress_action(
                    h.pos, datetime.now(IST), fired, __import__("signal_engine.config", fromlist=["settings"]).settings
                )

        await _run(h, scenario)
        assert seen["pending"] is True  # claimed before the first await, held while it acts
        assert h.pos.exit_pending is False  # the order failed, so the position is released

    async def test_a_break_even_move_releases_the_position_afterwards(self):
        h = _Harness()
        fired = (timedelta(minutes=90), 416.0, 0.1, 418.5, 0.2, "main")

        async def scenario():
            from datetime import datetime

            from signal_engine.config import settings
            from signal_engine.timeutils import IST

            with (
                patch.object(PositionTracker, "_should_market_exit", return_value=False),
                patch.object(h.tracker, "_no_progress_break_even", AsyncMock()),
            ):
                await h.tracker._apply_no_progress_action(h.pos, datetime.now(IST), fired, settings)

        await _run(h, scenario)
        assert h.pos.exit_pending is False


@pytest.mark.asyncio
class TestACrashNeverLeavesThePositionOwned:
    """The poll now skips a position whose exit is in progress. If an exit handler crashed after
    setting the flag and never cleared it, a real stop-hit close would never be booked and the
    risk slot would never be released."""

    async def test_a_crash_in_the_exit_handler_releases_the_flag_and_the_poll_can_book(self):
        h = _Harness()

        async def scenario():
            with patch.object(main, "_book_exit_result", AsyncMock(side_effect=RuntimeError("db"))):
                with pytest.raises(RuntimeError):
                    await main._handle_exit(_exit_signal())
            assert h.pos.exit_pending is False  # released, so the poll is not blocked
            await h.tracker.check_positions()  # and the poll books the real close

        record_close = await _run(h, scenario)
        assert len(_rows()) == 1
        assert record_close.call_count == 1
        assert h.tracker.find_position(SYMBOL, STRATEGY) is None

    async def test_cancellation_also_releases_the_flag(self):
        import asyncio

        h = _Harness()

        async def scenario():
            with patch.object(
                main, "_book_exit_result", AsyncMock(side_effect=asyncio.CancelledError())
            ):
                with pytest.raises(asyncio.CancelledError):
                    await main._handle_exit(_exit_signal())

        await _run(h, scenario)
        assert h.pos.exit_pending is False

    async def test_a_normal_full_exit_still_leaves_nothing_registered(self):
        h = _Harness()

        async def scenario():
            await main._handle_exit(_exit_signal())

        await _run(h, scenario)
        assert h.tracker.find_position(SYMBOL, STRATEGY) is None
        assert len(_rows()) == 1


@pytest.mark.asyncio
class TestAFailedExitWhileTheStopFiresStillBooksTheStop:
    """Review finding. The exit order fails and the broker stop fires in the same seconds. The
    handler used to unregister the position and record a close of 0.0 with no ledger row. Before
    the poll honored exit_pending that loss was usually booked by the poll with its real P&L;
    afterwards it would never have been. A realized loss must reach the ledger and the risk
    counters (I4)."""

    async def test_the_stop_hit_is_booked_by_the_poll_with_its_real_pnl(self):
        from datetime import timedelta as _td  # noqa: F401

        h = _Harness()
        h.send_order.return_value = TradeResult(status=OrderStatus.REJECTED, message="no")
        state = {}

        async def scenario():
            with (
                patch.object(h.tracker, "fetch_own_position", AsyncMock(return_value=0)),
                patch("signal_engine.main.asyncio.sleep", AsyncMock()),
            ):
                await main._handle_exit_locked(_exit_signal())
            # the handler books nothing and leaves the position for the poll
            state["registered"] = h.tracker.find_position(SYMBOL, STRATEGY) is h.pos
            state["pending"] = h.pos.exit_pending
            state["rows_after_handler"] = len(_rows())
            state["record_close_after_handler"] = h.engine._state(STRATEGY).daily_net_pnl
            await h.tracker.check_positions()

        record_close = await _run(h, scenario)

        assert state["registered"] is True
        assert state["pending"] is False
        assert state["rows_after_handler"] == 0
        rows = _rows()
        assert len(rows) == 1
        assert json.loads(rows[0][4])["pnl"] == pytest.approx(EXPECTED_PNL)
        assert record_close.call_count == 1  # once, with the real P&L
        assert record_close.call_args.args[0] == pytest.approx(EXPECTED_PNL)
        assert h.tracker.find_position(SYMBOL, STRATEGY) is None

    async def test_a_failed_exit_with_the_position_still_open_is_unchanged(self):
        h = _Harness()
        h.send_order.return_value = TradeResult(status=OrderStatus.REJECTED, message="no")

        async def scenario():
            with (
                patch.object(h.tracker, "fetch_own_position", AsyncMock(return_value=723)),
                patch("signal_engine.main.asyncio.sleep", AsyncMock()),
            ):
                await main._handle_exit_locked(_exit_signal())

        record_close = await _run(h, scenario)
        assert h.tracker.find_position(SYMBOL, STRATEGY) is h.pos  # still tracked
        assert h.pos.exit_pending is False  # a later TP or EXIT signal can retry
        assert record_close.call_count == 0 and _rows() == []


@pytest.mark.asyncio
class TestTheTimeExitAndThePollInFlight:
    async def test_the_time_exit_skips_a_position_whose_close_is_being_booked(self):
        from datetime import datetime

        from signal_engine.config import settings
        from signal_engine.timeutils import IST

        h = _Harness()
        h.pos.exit_pending = True  # the poll (or a signal exit) is booking it right now
        snapshot = dict(h.tracker._positions)

        async def scenario():
            await h.tracker._book_time_exit_trades(
                snapshot, settings, datetime.now(IST).replace(microsecond=0)
            )

        record_close = await _run(h, scenario)
        assert _rows() == [] and record_close.call_count == 0

    async def test_the_poll_removes_a_closed_position_at_once(self):
        """A time exit starting right after the poll booked must not find it still registered."""
        from signal_engine.config import settings
        from signal_engine.tracker import _index_positionbook

        h = _Harness()
        key = h.tracker._key(SYMBOL, STRATEGY)

        async def scenario():
            closed = await h.tracker._check_one_position(
                key, h.pos, _index_positionbook(_closed_book()), settings
            )
            assert closed is True
            assert h.tracker._positions.get(key) is None

        await _run(h, scenario)


@pytest.mark.asyncio
class TestThePollClaimsBeforeItVerifies:
    async def test_the_claim_is_held_through_the_fill_verification(self):
        from signal_engine.config import settings
        from signal_engine.tracker import _FILL_WAIT, _index_positionbook

        h = _Harness()
        seen = {}
        key = h.tracker._key(SYMBOL, STRATEGY)

        async def verify(*_a, **_k):
            seen["pending"] = h.pos.exit_pending
            return _FILL_WAIT

        async def scenario():
            with patch.object(h.tracker, "_verify_unconfirmed_fill", verify):
                closed = await h.tracker._check_one_position(
                    key, h.pos, _index_positionbook(_closed_book()), settings
                )
            assert closed is False

        await _run(h, scenario)
        assert seen["pending"] is True  # a signal exit cannot slip in during the verification
        assert h.pos.exit_pending is False  # released: nothing was booked


@pytest.mark.asyncio
class TestANotificationFailureCannotUnwindABookedClose:
    async def test_the_close_stays_booked_when_telegram_is_down(self):
        h = _Harness()
        h.tracker_notifier.notify_position_closed.side_effect = RuntimeError("telegram down")

        async def scenario():
            await main._handle_exit_locked(_exit_signal())  # must not raise

        record_close = await _run(h, scenario)
        assert len(_rows()) == 1
        assert record_close.call_count == 1
        assert h.tracker.find_position(SYMBOL, STRATEGY) is None


@pytest.mark.asyncio
class TestARecoveredPositionStillReleasesItsRiskSlot:
    """After an engine restart the exit handler rebuilds the position from the broker. That object
    is never registered, so the poll cannot book its close. When its exit fails and the broker is
    flat, the handler must still free the risk slot the persisted risk store counts."""

    async def test_an_unregistered_position_releases_the_slot_with_a_zero_close(self):
        h = _Harness()
        h.tracker.unregister(SYMBOL, STRATEGY)  # as after a restart: nothing is tracked
        recovered = _position()
        failed = TradeResult(status=OrderStatus.REJECTED, message="no")

        async def scenario():
            with patch.object(h.tracker, "fetch_own_position", AsyncMock(return_value=0)):
                await main._handle_exit_order_failure(recovered, failed)

        record_close = await _run(h, scenario)
        record_close.assert_called_once()
        assert record_close.call_args.kwargs["pnl"] == 0.0
        assert recovered.exit_pending is False

    async def test_a_registered_position_is_left_for_the_poll(self):
        h = _Harness()
        failed = TradeResult(status=OrderStatus.REJECTED, message="no")

        async def scenario():
            with patch.object(h.tracker, "fetch_own_position", AsyncMock(return_value=0)):
                await main._handle_exit_order_failure(h.pos, failed)

        record_close = await _run(h, scenario)
        assert record_close.call_count == 0
        assert h.tracker.find_position(SYMBOL, STRATEGY) is h.pos
