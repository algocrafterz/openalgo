"""Tracker bookkeeping: register, find, unregister, record_exit, stop."""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

from signal_engine.strategies import ORB, RSI_TP_MR
from signal_engine.tracker import PositionTracker

_IST = timezone(timedelta(hours=5, minutes=30))
_AGED = datetime.now(_IST) - timedelta(minutes=5)  # past Guard 1 (30s min age)

from signal_engine.tests.tracker_fixtures import _make_engine, _make_position


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
        tracker.register(_make_position(symbol="RELIANCE", strategy=ORB))
        tracker.register(_make_position(symbol="RELIANCE", strategy=ORB, quantity=100))
        assert tracker.tracked_count == 1


class TestTrackerFindPosition:
    def test_find_existing_position(self):
        engine = _make_engine()
        tracker = PositionTracker(engine)
        pos = _make_position(symbol="RELIANCE", strategy=RSI_TP_MR)
        tracker.register(pos)
        found = tracker.find_position("RELIANCE", RSI_TP_MR)
        assert found is not None
        assert found.symbol == "RELIANCE"
        assert found.strategy == RSI_TP_MR

    def test_find_nonexistent_returns_none(self):
        engine = _make_engine()
        tracker = PositionTracker(engine)
        assert tracker.find_position("TCS", "ORB") is None

    def test_find_wrong_strategy_returns_none(self):
        engine = _make_engine()
        tracker = PositionTracker(engine)
        tracker.register(_make_position(symbol="RELIANCE", strategy=ORB))
        assert tracker.find_position("RELIANCE", RSI_TP_MR) is None


class TestTrackerUnregister:
    def test_unregister_removes_and_returns(self):
        engine = _make_engine()
        tracker = PositionTracker(engine)
        tracker.register(_make_position(symbol="RELIANCE", strategy=RSI_TP_MR))
        assert tracker.tracked_count == 1
        removed = tracker.unregister("RELIANCE", RSI_TP_MR)
        assert removed is not None
        assert removed.symbol == "RELIANCE"
        assert tracker.tracked_count == 0

    def test_unregister_nonexistent_returns_none(self):
        engine = _make_engine()
        tracker = PositionTracker(engine)
        assert tracker.unregister("TCS", "ORB") is None

    def test_unregister_does_not_affect_other_positions(self):
        engine = _make_engine()
        tracker = PositionTracker(engine)
        tracker.register(_make_position(symbol="RELIANCE", strategy=ORB))
        tracker.register(_make_position(symbol="TCS", strategy=ORB))
        tracker.unregister("RELIANCE", "ORB")
        assert tracker.tracked_count == 1
        assert tracker.find_position("TCS", "ORB") is not None


class TestTPMonitoringRemoved:
    """TP monitoring via LTP polling is removed — exits driven by TP HIT signal instead."""

    def test_tracked_position_has_no_tp_monitoring_field(self):
        pos = _make_position()
        assert not hasattr(pos, "tp_monitoring")

    def test_tracked_position_has_no_tp_triggered_field(self):
        pos = _make_position()
        assert not hasattr(pos, "tp_triggered")

    def test_tracker_has_no_exit_at_tp_method(self):
        engine = _make_engine()
        tracker = PositionTracker(engine)
        assert not hasattr(tracker, "_exit_at_tp")

    @pytest.mark.asyncio
    async def test_check_positions_does_not_exit_when_ltp_crosses_tp(self):
        """Tracker must NOT exit a position when LTP > TP — that's TP HIT signal's job."""
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        pos = _make_position(symbol="RELIANCE", strategy=ORB, tp=2540.0)
        tracker.register(pos)

        book = [{"symbol": "RELIANCE", "quantity": 50, "ltp": 2600.0}]
        with patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=book):
            await tracker.check_positions()

        assert tracker.tracked_count == 1


class TestTrackerStop:
    def test_stop_sets_flag(self):
        engine = _make_engine()
        tracker = PositionTracker(engine)
        tracker._running = True
        tracker.stop()
        assert tracker._running is False


class TestRecordExit:
    """record_exit() increments day counters for exits driven by TradingView signals."""

    def test_record_exit_profit_increments_wins(self):
        engine = _make_engine()
        tracker = PositionTracker(engine)
        tracker.record_exit(pnl=500.0)
        assert tracker._day_trades == 1
        assert tracker._day_wins == 1
        assert tracker._day_losses == 0
        assert tracker._day_pnl == 500.0

    def test_record_exit_loss_increments_losses(self):
        engine = _make_engine()
        tracker = PositionTracker(engine)
        tracker.record_exit(pnl=-200.0)
        assert tracker._day_trades == 1
        assert tracker._day_wins == 0
        assert tracker._day_losses == 1
        assert tracker._day_pnl == -200.0

    def test_record_exit_zero_pnl_counts_as_win(self):
        engine = _make_engine()
        tracker = PositionTracker(engine)
        tracker.record_exit(pnl=0.0)
        assert tracker._day_trades == 1
        assert tracker._day_wins == 1

    def test_record_exit_accumulates_across_trades(self):
        engine = _make_engine()
        tracker = PositionTracker(engine)
        tracker.record_exit(pnl=500.0)
        tracker.record_exit(pnl=-200.0)
        tracker.record_exit(pnl=300.0)
        assert tracker._day_trades == 3
        assert tracker._day_wins == 2
        assert tracker._day_losses == 1
        assert tracker._day_pnl == 600.0

    def test_record_exit_updates_realised_pnl_snapshot(self):
        """record_exit takes a new realised_pnl snapshot to keep delta tracking accurate."""
        engine = _make_engine()
        tracker = PositionTracker(engine)
        tracker._last_realised_pnl = 1000.0
        tracker.record_exit(pnl=500.0, new_realised_pnl=1500.0)
        assert tracker._last_realised_pnl == 1500.0

    def test_record_exit_without_snapshot_preserves_last(self):
        engine = _make_engine()
        tracker = PositionTracker(engine)
        tracker._last_realised_pnl = 1000.0
        tracker.record_exit(pnl=500.0)
        assert tracker._last_realised_pnl == 1000.0
