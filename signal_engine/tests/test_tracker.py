"""Tests for position tracker."""

import pytest
from unittest.mock import AsyncMock, patch

from signal_engine.risk import RiskEngine
from signal_engine.tracker import PositionTracker, TrackedPosition


def _make_engine(**overrides) -> RiskEngine:
    defaults = {
        "risk_per_trade": 0.01,
        "sizing_mode": "fixed_fractional",
        "pct_of_capital": 0.05,
        "max_position_size": 0.20,
        "daily_loss_limit": 0.03,
        "weekly_loss_limit": 0.06,
        "monthly_loss_limit": 0.10,
        "max_open_positions": 3,
        "max_trades_per_day": 5,
        "min_entry_price": 0,
        "max_entry_price": 0,
        "max_portfolio_heat": 0.06,
    }
    defaults.update(overrides)
    return RiskEngine(**defaults)


def _make_position(**overrides) -> TrackedPosition:
    defaults = {
        "symbol": "RELIANCE",
        "strategy": "ORB",
        "exchange": "NSE",
        "product": "MIS",
        "entry_price": 2500.0,
        "quantity": 50,
        "sl": 2485.0,
        "tp": 2540.0,
    }
    defaults.update(overrides)
    return TrackedPosition(**defaults)


class TestTrackerRegister:
    def test_register_increments_count(self):
        engine = _make_engine()
        tracker = PositionTracker(engine)
        assert tracker.tracked_count == 0
        tracker.register(_make_position())
        assert tracker.tracked_count == 1

    def test_register_multiple_positions(self):
        engine = _make_engine()
        tracker = PositionTracker(engine)
        tracker.register(_make_position(symbol="RELIANCE"))
        tracker.register(_make_position(symbol="TCS"))
        assert tracker.tracked_count == 2

    def test_same_key_overwrites(self):
        engine = _make_engine()
        tracker = PositionTracker(engine)
        tracker.register(_make_position(symbol="RELIANCE", strategy="ORB"))
        tracker.register(_make_position(symbol="RELIANCE", strategy="ORB", quantity=100))
        assert tracker.tracked_count == 1


class TestTrackerCheckPositions:
    @pytest.mark.asyncio
    async def test_position_still_open(self):
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker.register(_make_position())

        with patch("signal_engine.tracker.fetch_open_position", new_callable=AsyncMock, return_value=50):
            await tracker.check_positions()
            assert tracker.tracked_count == 1
            assert engine.open_positions == 1

    @pytest.mark.asyncio
    async def test_position_closed_with_profit(self):
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker._last_realised_pnl = 1000.0
        tracker.register(_make_position())

        with (
            patch("signal_engine.tracker.fetch_open_position", new_callable=AsyncMock, return_value=0),
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=1500.0),
        ):
            await tracker.check_positions()
            assert tracker.tracked_count == 0
            assert engine.open_positions == 0
            assert engine.daily_realised_loss == 0.0  # profit, no loss recorded

    @pytest.mark.asyncio
    async def test_position_closed_with_loss(self):
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker._last_realised_pnl = 1000.0
        tracker.register(_make_position())

        with (
            patch("signal_engine.tracker.fetch_open_position", new_callable=AsyncMock, return_value=0),
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=500.0),
        ):
            await tracker.check_positions()
            assert tracker.tracked_count == 0
            assert engine.open_positions == 0
            assert engine.daily_realised_loss == 500.0  # lost 500

    @pytest.mark.asyncio
    async def test_api_error_skips_position(self):
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker.register(_make_position())

        with patch("signal_engine.tracker.fetch_open_position", new_callable=AsyncMock, return_value=-1):
            await tracker.check_positions()
            assert tracker.tracked_count == 1  # not removed
            assert engine.open_positions == 1  # unchanged


class TestTrackerStop:
    def test_stop_sets_flag(self):
        engine = _make_engine()
        tracker = PositionTracker(engine)
        tracker._running = True
        tracker.stop()
        assert tracker._running is False
