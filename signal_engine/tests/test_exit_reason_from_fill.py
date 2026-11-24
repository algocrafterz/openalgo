"""BUG-034: the exit label of a close the poll finds comes from the order that filled.

2026-11-23: APLAPOLLO and TATASTEEL time exits (14:46) were cancelled, and the positions were
closed by the 14:55:07 failsafe square-off (orders 26112353710401 and 26112353710719, untagged,
closeposition). The poll found them flat and booked "Closed by tracker (SL)" with the original stop
id, because it labelled every close it found as the stop unless another path had claimed it.

Rule: SL only when the tracker's own stop is complete; TIME when the tracker's own time-exit order
is complete; SQUAREOFF when the tracker placed neither filling order (a failsafe or broker
square-off); an unidentifiable close keeps the old SL default (residual risk, documented below).
Only the label changes: P&L, quantity and exit price are computed as before.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine.models import Direction
from signal_engine.strategies import ORB
from signal_engine.tests.tracker_fixtures import _make_engine, _make_position
from signal_engine.tracker import PositionTracker, _poll_close_exit_types

_IST = timezone(timedelta(hours=5, minutes=30))


async def _close(pos, statuses, *, pnl_delta, square_off_order=""):
    """Run the poll's close booking; return the kwargs of the row written to trades.db."""
    engine = _make_engine()
    tracker = PositionTracker(engine)
    tracker.register(pos)
    if square_off_order:
        tracker._square_off_orders[tracker._key(pos.symbol, pos.strategy)] = square_off_order

    async def status(order_id, _strategy):
        return statuses.get(order_id, "")

    with (
        patch("signal_engine.tracker.fetch_order_status", new=AsyncMock(side_effect=status)),
        patch("signal_engine.tracker.cancel_order", new=AsyncMock(return_value=True)),
        patch.object(tracker, "_cancel_untracked_stops", new=AsyncMock(return_value=None)),
        patch("signal_engine.tracker.db.save_tracker_exit") as save,
    ):
        await tracker._record_closed_trade(
            tracker._key(pos.symbol, pos.strategy), pos, timedelta(minutes=20), pnl_delta,
            _Settings(),
        )
    assert save.call_count == 1
    return save.call_args.kwargs


class _Settings:
    max_trades_per_day = 5


def _pos(**kw):
    return _make_position(**kw)


def _message(types):
    """The text db.save_tracker_exit builds from the labels."""
    return f"Closed by tracker ({'+'.join(types)})"


class TestStopFilled:
    @pytest.mark.asyncio
    async def test_stop_fill_is_sl(self):
        pos = _pos(symbol="SBIN", fill_price=800.0, sl_order_id="STOP-1")
        row = await _close(pos, {"STOP-1": "complete"}, pnl_delta=-100.0)
        assert row["exit_types"] == ["SL"]
        assert row["order_id"] == "STOP-1"

    @pytest.mark.asyncio
    async def test_stop_fill_after_tp1_is_sl(self):
        pos = _pos(symbol="SBIN", fill_price=800.0, sl_order_id="STOP-1", exit_types=["TP1"])
        row = await _close(pos, {"STOP-1": "complete"}, pnl_delta=-100.0)
        assert row["exit_types"] == ["TP1", "SL"]

    @pytest.mark.asyncio
    async def test_stop_filled_and_late_retry_cancelled_follows_the_stop(self):
        pos = _pos(symbol="SBIN", fill_price=800.0, sl_order_id="STOP-1", time_exit_left_open=True)
        row = await _close(
            pos, {"STOP-1": "complete", "RETRY-1": "cancelled"}, pnl_delta=-100.0,
            square_off_order="RETRY-1",
        )
        assert row["exit_types"] == ["SL"]


class TestTargetFilled:
    @pytest.mark.asyncio
    async def test_target_leg_label_is_kept_when_the_target_closed_it(self):
        # A target fill books its label before the poll sees the close (main._finalize_partial_exit
        # and the full-exit paths): the poll must not add a stop label on top of a non-TP last entry.
        pos = _pos(symbol="SBIN", fill_price=800.0, sl_order_id="STOP-1", exit_types=["EXIT"])
        row = await _close(pos, {"STOP-1": "cancelled"}, pnl_delta=100.0)
        assert row["exit_types"] == ["EXIT"]


class TestTimeExitFilled:
    @pytest.mark.asyncio
    async def test_late_time_exit_fill_is_time(self):
        pos = _pos(symbol="SBIN", fill_price=800.0, sl_order_id="STOP-1", time_exit_left_open=True)
        row = await _close(
            pos, {"STOP-1": "cancelled", "RETRY-1": "complete"}, pnl_delta=50.0,
            square_off_order="RETRY-1",
        )
        assert row["exit_types"] == ["TIME"]


class TestFailsafeFill:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("history", [[], ["TP1"]])
    async def test_failsafe_after_cancelled_time_exit_is_not_sl(self, history):
        pos = _pos(
            symbol="SBIN", fill_price=800.0, sl_order_id="STOP-1", exit_types=list(history),
            time_exit_left_open=True,
        )
        row = await _close(
            pos, {"STOP-1": "cancelled", "TE-1": "cancelled"}, pnl_delta=50.0,
            square_off_order="TE-1",
        )
        assert row["exit_types"] == history + ["SQUAREOFF"]
        assert "SL" not in row["exit_types"]

    @pytest.mark.asyncio
    async def test_failsafe_with_no_time_exit_order_recorded(self):
        pos = _pos(symbol="SBIN", fill_price=800.0, sl_order_id="STOP-1", time_exit_left_open=True)
        row = await _close(pos, {"STOP-1": "cancelled"}, pnl_delta=50.0)
        assert row["exit_types"] == ["SQUAREOFF"]

    @pytest.mark.asyncio
    async def test_stop_cancelled_and_nothing_of_ours_filled_is_not_sl(self):
        pos = _pos(symbol="SBIN", fill_price=800.0, sl_order_id="STOP-1")
        row = await _close(pos, {"STOP-1": "cancelled"}, pnl_delta=50.0)
        assert row["exit_types"] == ["SQUAREOFF"]


class TestUnidentifiable:
    @pytest.mark.asyncio
    async def test_unreadable_status_keeps_the_old_sl_default(self):
        """Residual risk: with no readable order status and no time exit in flight there is no
        evidence either way, so the close is still labelled SL as before this fix."""
        pos = _pos(symbol="SBIN", fill_price=800.0, sl_order_id="STOP-1")
        row = await _close(pos, {}, pnl_delta=-100.0)
        assert row["exit_types"] == ["SL"]

    @pytest.mark.asyncio
    async def test_no_stop_id_and_no_time_exit_keeps_the_old_sl_default(self):
        pos = _pos(symbol="SBIN", fill_price=800.0)
        row = await _close(pos, {}, pnl_delta=-100.0)
        assert row["exit_types"] == ["SL"]


class TestPureLabelFunction:
    def test_old_signature_is_unchanged(self):
        assert _poll_close_exit_types([]) == ["SL"]
        assert _poll_close_exit_types(["TP1"]) == ["TP1", "SL"]
        assert _poll_close_exit_types([], "TRAILING_STOP") == ["TRAILING_STOP"]
        assert _poll_close_exit_types(["NO-PROGRESS"]) == ["NO-PROGRESS"]

    def test_closer_replaces_the_default_only_when_nothing_else_labelled(self):
        assert _poll_close_exit_types([], "", "SQUAREOFF") == ["SQUAREOFF"]
        assert _poll_close_exit_types(["TP2"], "", "TIME") == ["TP2", "TIME"]
        assert _poll_close_exit_types(["NO-PROGRESS"], "", "SQUAREOFF") == ["NO-PROGRESS"]
        assert _poll_close_exit_types([], "TRAILING_STOP", "SQUAREOFF") == ["TRAILING_STOP"]


class TestNovember23Case:
    """APLAPOLLO short 3 and TATASTEEL (two strategies, 127 each), closed by the 14:55:07 failsafe."""

    CASES = [
        ("APLAPOLLO", "STRAT-A", Direction.SHORT, 3, 1680.0, 1673.9, "26112337849391",
         "26112353710401"),
        ("TATASTEEL", "STRAT-A", Direction.LONG, 127, 163.00, 163.06, "26112337856371",
         "26112353710719"),
        ("TATASTEEL", "STRAT-B", Direction.LONG, 127, 163.00, 163.06, "26112337857089",
         "26112353710719"),
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("sym,strat,direction,qty,base,fill,stop_id,failsafe_id", CASES)
    async def test_failsafe_close_is_not_booked_as_sl(
        self, sym, strat, direction, qty, base, fill, stop_id, failsafe_id
    ):
        sign = 1 if direction == Direction.LONG else -1
        pnl = round(sign * (fill - base) * qty, 2)
        pos = _pos(
            symbol=sym, strategy=strat, direction=direction, quantity=qty, fill_price=base,
            sl_order_id=stop_id, time_exit_left_open=True,
        )
        row = await _close(
            pos, {stop_id: "cancelled", "TIME-EXIT-1446": "cancelled"}, pnl_delta=pnl,
            square_off_order="TIME-EXIT-1446",
        )
        assert _message(row["exit_types"]) != "Closed by tracker (SL)"
        assert row["exit_types"] == ["SQUAREOFF"]
        # Money and size are exactly what the close path computed before: same inputs, same row.
        assert row["quantity"] == qty
        assert row["pnl"] == pytest.approx(pnl)
        assert row["exit_price"] == pytest.approx(fill)
        assert row["order_id"] == stop_id  # recorded id unchanged: the failsafe id is not known


class TestPnlAndQuantityUnchangedByLabel:
    @pytest.mark.asyncio
    async def test_same_inputs_same_money_whatever_the_label(self):
        results = []
        for statuses, left_open in (({"STOP-1": "complete"}, False), ({"STOP-1": "cancelled"}, True)):
            pos = _pos(
                symbol="SBIN", fill_price=800.0, quantity=40, sl_order_id="STOP-1",
                realized_pnl=25.0, time_exit_left_open=left_open,
            )
            row = await _close(pos, statuses, pnl_delta=-120.0)
            results.append(
                (row["pnl"], row["quantity"], row["exit_price"], row["order_id"], row["includes_leg_pnl"])
            )
        assert results[0] == results[1] == (-95.0, 40, 797.0, "STOP-1", 25.0)
