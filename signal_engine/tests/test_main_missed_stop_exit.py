"""main: the missed-stop safety exit never sends a second close while the stop may be live.

The trail driver sends tp_level INITIAL_STOP_SAFETY when the bar data touched the initial stop but
the position is still open. main cancels the SL-M first; if that cancel fails (it can fail because
the stop just filled) main must not place the market close. Every other exit is unchanged.
"""

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import main
from signal_engine.models import Direction, OrderStatus, Signal, TradeResult
from signal_engine.strategy_core.trade_policy import MISSED_STOP_SAFETY
from signal_engine.tests.pipeline_fixtures import tracker_mock
from signal_engine.timeutils import IST
from signal_engine.tracker import TrackedPosition

TAG = "EVWAP-EARLY-PY"


def _pos() -> TrackedPosition:
    return TrackedPosition(
        symbol="PFC",
        strategy=TAG,
        exchange="NSE",
        product="MIS",
        entry_price=326.2,
        quantity=436,
        sl=328.28,
        tp=0.0,
        direction=Direction.SHORT,
        fill_price=326.2,
        original_quantity=436,
        entry_time=dt.datetime(2026, 10, 9, 10, 55, tzinfo=IST),
        sl_order_id="SL1",
        sig_id=f"{TAG}-PFC",
    )


def _exit(tp_level: str) -> Signal:
    return Signal(
        strategy=TAG,
        direction=Direction.EXIT,
        symbol="PFC",
        entry=0.0,
        sl=328.28,
        tp=0.0,
        exchange="NSE",
        product="MIS",
        sig_id=f"{TAG}-PFC",
        tp_level=tp_level,
        exit_qty_pct=1.0,
    )


async def _run(tp_level: str, cancel_ok: bool, broker_own: int | None):
    pos = _pos()
    placed = AsyncMock(
        return_value=(MagicMock(), TradeResult(status=OrderStatus.SUCCESS, order_id="X"))
    )
    with (
        patch("signal_engine.main.tracker", new_callable=tracker_mock) as tr,
        patch("signal_engine.main.cancel_order", new_callable=AsyncMock, return_value=cancel_ok),
        patch("signal_engine.main._place_exit_order", placed),
        patch("signal_engine.main._book_exit_result", new_callable=AsyncMock, return_value=True),
        patch("signal_engine.main._reconcile_sl_hit", new_callable=AsyncMock) as reconcile,
        patch(
            "signal_engine.main._abort_exit_on_rejected_entry",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch("signal_engine.main.save"),
    ):
        tr._time_exit_active = False
        tr.find_position.return_value = pos
        tr.fetch_own_position = AsyncMock(return_value=broker_own)
        await main._handle_exit_locked(_exit(tp_level))
    return pos, placed, reconcile


@pytest.mark.asyncio
class TestMissedStopSafetyExit:
    async def test_cancel_failed_and_broker_flat_books_the_stop_without_an_order(self):
        _, placed, reconcile = await _run(MISSED_STOP_SAFETY, cancel_ok=False, broker_own=0)
        placed.assert_not_awaited()
        reconcile.assert_awaited_once()

    @pytest.mark.parametrize("own", [-436, None])
    async def test_cancel_failed_and_broker_holds_or_unknown_sends_nothing(self, own):
        pos, placed, reconcile = await _run(MISSED_STOP_SAFETY, cancel_ok=False, broker_own=own)
        placed.assert_not_awaited()
        reconcile.assert_not_awaited()
        assert pos.exit_pending is False  # released, so the runner's next attempt is not blocked

    async def test_cancel_succeeded_closes_at_market(self):
        _, placed, reconcile = await _run(MISSED_STOP_SAFETY, cancel_ok=True, broker_own=-436)
        placed.assert_awaited_once()
        reconcile.assert_not_awaited()

    @pytest.mark.parametrize("label", ["TRAILING_STOP", None, "TP1"])
    async def test_other_exits_are_unchanged_when_the_cancel_fails(self, label):
        _, placed, reconcile = await _run(label, cancel_ok=False, broker_own=0)
        placed.assert_awaited_once()  # existing behaviour: the exit is still sent
        reconcile.assert_not_awaited()
