"""E-08: a leg whose own stop filled is booked although another strategy still holds the symbol."""

from unittest.mock import AsyncMock, patch

import pytest

from signal_engine.strategies import ORB, RSI_TP_MR
from signal_engine.tests.tracker_fixtures import _make_engine, _make_position
from signal_engine.tracker import BookEntry, PositionTracker


class _Cfg:
    tracker_guard2_timeout_minutes = 10
    tracker_min_position_age_seconds = 0
    max_trades_per_day = 8


def _two_legs():
    engine = _make_engine(trade_mode="analyze")  # one counter bucket per strategy
    engine._state(ORB).open_positions = 1
    engine._state(RSI_TP_MR).open_positions = 1
    tracker = PositionTracker(engine)
    a = _make_position(symbol="RELIANCE", strategy=ORB, sl_order_id="SLA", fill_price=2500.0)
    b = _make_position(symbol="RELIANCE", strategy=RSI_TP_MR, sl_order_id="SLB", fill_price=2500.0)
    tracker.register(a)
    tracker.register(b)
    return tracker, a, b


async def _check(tracker, pos, stop_fill):
    book = {"RELIANCE": BookEntry(50, 2490.0)}  # the shared row still holds the other leg
    key = tracker._key(pos.symbol, pos.strategy)
    with (
        patch("signal_engine.tracker.fetch_order_fill_price", AsyncMock(return_value=stop_fill)),
        patch("signal_engine.tracker.fetch_realised_pnl", AsyncMock(return_value=0.0)),
        patch("signal_engine.tracker.cancel_order", AsyncMock(return_value=True)),
    ):
        return await tracker._check_one_position(key, pos, book, _Cfg())


@pytest.mark.asyncio
async def test_a_filled_stop_on_a_shared_symbol_books_that_leg_only():
    tracker, a, b = _two_legs()
    closed = await _check(tracker, a, 2485.0)
    assert closed is True
    assert tracker._positions.get(tracker._key("RELIANCE", ORB)) is None
    assert tracker.find_position("RELIANCE", RSI_TP_MR) is b  # the other leg stays tracked
    # leg loss from its own fill: (2485 - 2500) * 50
    assert tracker._risk_engine._state(ORB).daily_realised_loss == 750.0
    assert tracker._risk_engine._state(RSI_TP_MR).daily_realised_loss == 0.0


@pytest.mark.asyncio
async def test_a_working_stop_on_a_shared_symbol_is_left_alone():
    tracker, a, b = _two_legs()
    assert await _check(tracker, a, None) is False
    assert tracker._risk_engine._state(ORB).daily_realised_loss == 0.0


@pytest.mark.asyncio
async def test_a_symbol_held_by_one_leg_is_not_checked():
    engine = _make_engine()
    tracker = PositionTracker(engine)
    a = _make_position(symbol="RELIANCE", strategy=ORB, sl_order_id="SLA")
    tracker.register(a)
    fill = AsyncMock(return_value=2485.0)
    with patch("signal_engine.tracker.fetch_order_fill_price", fill):
        out = await tracker._check_one_position(
            tracker._key("RELIANCE", ORB), a, {"RELIANCE": BookEntry(50, 2490.0)}, _Cfg()
        )
    assert out is False
    fill.assert_not_awaited()
