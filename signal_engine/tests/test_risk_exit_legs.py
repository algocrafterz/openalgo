"""R-01: every completed exit leg reaches the risk counters once, at its own P&L."""

from unittest.mock import AsyncMock, patch

import pytest

from signal_engine.strategies import ORB
from signal_engine.tests.risk_fixtures import _engine
from signal_engine.tests.test_exit_status_verification import _exit
from signal_engine.tests.tracker_fixtures import _make_engine, _make_position
from signal_engine.tracker import PositionTracker


def test_partial_leg_books_pnl_and_keeps_the_slot():
    e = _engine()
    e.record_trade("ORB", "RELIANCE")
    e.record_partial_close(500.0, "ORB")
    s = e._state("ORB")
    assert s.open_positions == 1
    assert s.daily_net_pnl == 500.0
    assert s.daily_realised_loss == 0.0


def test_profit_leg_then_loss_final_leg_counts_each_leg():
    # TP1 +500, runner stopped -300: the owner decided per-leg, so the loss counter is 300
    # and the net is +200.
    e = _engine()
    e.record_trade("ORB", "RELIANCE")
    e.record_partial_close(500.0, "ORB")
    e.record_close(-300.0, "ORB", "RELIANCE")
    s = e._state("ORB")
    assert s.open_positions == 0
    assert s.daily_net_pnl == 200.0
    assert s.daily_realised_loss == 300.0
    assert s.weekly_net_pnl == 200.0 and s.monthly_net_pnl == 200.0


def test_loss_leg_counts_toward_loss_counters_while_open():
    e = _engine()
    e.record_trade("ORB", "RELIANCE")
    e.record_partial_close(-120.0, "ORB")
    s = e._state("ORB")
    assert s.daily_realised_loss == 120.0 and s.open_positions == 1


@pytest.mark.asyncio
async def test_on_time_partial_exit_books_its_leg_and_not_the_slot():
    # 25 of 50 sold at 2520 against an entry of 2500.
    h, tracker, pos = await _exit(status="complete", own=25, exit_qty_pct=0.5, fill=2520.0)
    h.risk.record_partial_close.assert_called_once_with(500.0, strategy="ORB")
    h.risk.record_close.assert_not_called()


@pytest.mark.asyncio
async def test_time_exit_books_only_the_closing_leg_when_earlier_legs_exist():
    engine = _make_engine()
    engine._state(ORB).open_positions = 1
    tracker = PositionTracker(engine)
    pos = _make_position(symbol="RELIANCE", strategy=ORB, product="MIS")
    pos.realized_pnl = 500.0  # an earlier leg, already booked when it filled
    tracker.register(pos)
    with (
        patch("signal_engine.api_client.cancel_all_orders", new_callable=AsyncMock),
        patch.object(PositionTracker, "_close_strategy", new_callable=AsyncMock),
        patch("signal_engine.tracker.fetch_net_position", new_callable=AsyncMock, return_value=0),
        patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=-300.0),
        patch("asyncio.sleep", new_callable=AsyncMock)
    ):
        await tracker.time_exit_all()
    state = engine._state(ORB)
    assert state.daily_realised_loss == pytest.approx(300.0)
    assert state.daily_net_pnl == pytest.approx(-300.0)
