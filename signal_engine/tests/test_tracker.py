"""Tests for position tracker."""

import pytest
from unittest.mock import AsyncMock, patch

from signal_engine.risk import RiskEngine
from signal_engine.strategies import ORB, RSI_TP_MR
from signal_engine.tracker import PositionTracker, TrackedPosition


def _make_engine(**overrides) -> RiskEngine:
    defaults = {
        "risk_per_trade": 0.01,
        "sizing_mode": "fixed_fractional",
        "pct_of_capital": 0.05,
        "daily_loss_limit": 0.03,
        "weekly_loss_limit": 0.06,
        "monthly_loss_limit": 0.10,
        "max_open_positions": 3,
        "max_trades_per_day": 5,
        "min_entry_price": 0,
        "max_entry_price": 0,
        "default_product": "MIS",
    }
    defaults.update(overrides)
    return RiskEngine(**defaults)


def _make_position(**overrides) -> TrackedPosition:
    defaults = {
        "symbol": "RELIANCE",
        "strategy": ORB,
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


class TestTimeExitCncAwareness:
    """time_exit_all() should only close MIS positions, not CNC swing positions."""

    @pytest.mark.asyncio
    async def test_time_exit_skips_cnc_positions(self):
        engine = _make_engine()
        engine.open_positions = 2
        tracker = PositionTracker(engine)
        tracker.register(_make_position(symbol="RELIANCE", strategy=ORB, product="MIS"))
        tracker.register(_make_position(symbol="HDFCBANK", strategy=RSI_TP_MR, product="CNC"))

        with (
            patch("signal_engine.tracker.cancel_all_orders", new_callable=AsyncMock),
            patch("signal_engine.tracker.close_all_positions", new_callable=AsyncMock),
            patch("signal_engine.tracker.notifier", new_callable=AsyncMock),
        ):
            await tracker.time_exit_all()

        # CNC position should survive time exit
        assert tracker.tracked_count == 1
        assert tracker.find_position("HDFCBANK", RSI_TP_MR) is not None

    @pytest.mark.asyncio
    async def test_time_exit_closes_all_mis_positions(self):
        engine = _make_engine()
        engine.open_positions = 2
        tracker = PositionTracker(engine)
        tracker.register(_make_position(symbol="RELIANCE", strategy=ORB, product="MIS"))
        tracker.register(_make_position(symbol="TCS", strategy=ORB, product="MIS"))

        with (
            patch("signal_engine.tracker.cancel_all_orders", new_callable=AsyncMock),
            patch("signal_engine.tracker.close_all_positions", new_callable=AsyncMock),
            patch("signal_engine.tracker.notifier", new_callable=AsyncMock),
        ):
            await tracker.time_exit_all()

        assert tracker.tracked_count == 0


class TestTrackerCheckPositions:
    @pytest.mark.asyncio
    async def test_position_still_open(self):
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker.register(_make_position())

        book = [{"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "quantity": 50, "pnl": 0}]
        with patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=book):
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
            patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=[]),
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
            patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=[]),
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=500.0),
        ):
            await tracker.check_positions()
            assert tracker.tracked_count == 0
            assert engine.open_positions == 0
            assert engine.daily_realised_loss == 500.0  # lost 500

    @pytest.mark.asyncio
    async def test_api_error_skips_cycle(self):
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker.register(_make_position())

        with patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=None):
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


class TestOCOCancellation:
    def _make_bracket_position(self, **overrides) -> TrackedPosition:
        defaults = {
            "symbol": "RELIANCE",
            "strategy": "ORB",
            "exchange": "NSE",
            "product": "MIS",
            "entry_price": 2500.0,
            "quantity": 50,
            "sl": 2485.0,
            "tp": 2540.0,
            "entry_order_id": "ENTRY001",
            "sl_order_id": "SL001",
            "tp_order_id": "TP001",
        }
        defaults.update(overrides)
        return TrackedPosition(**defaults)

    @pytest.mark.asyncio
    async def test_position_with_bracket_ids_stored(self):
        engine = _make_engine()
        tracker = PositionTracker(engine)
        pos = self._make_bracket_position()
        tracker.register(pos)
        key = "RELIANCE:ORB"
        assert tracker._positions[key].sl_order_id == "SL001"
        assert tracker._positions[key].tp_order_id == "TP001"

    @pytest.mark.asyncio
    async def test_sl_triggered_position_closed(self):
        """When position closes with loss (SL triggered), position is removed from tracker."""
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker._last_realised_pnl = 0.0
        pos = self._make_bracket_position()
        tracker.register(pos)

        with (
            patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=[]),
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=-500.0),
        ):
            await tracker.check_positions()

        assert tracker.tracked_count == 0
        assert engine.open_positions == 0

    @pytest.mark.asyncio
    async def test_tp_triggered_position_closed(self):
        """When position closes with profit (TP hit), position is removed from tracker."""
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker._last_realised_pnl = 0.0
        pos = self._make_bracket_position()
        tracker.register(pos)

        with (
            patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=[]),
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=2000.0),
        ):
            await tracker.check_positions()

        assert tracker.tracked_count == 0
        assert engine.open_positions == 0

    @pytest.mark.asyncio
    async def test_cancel_fails_gracefully(self):
        """Cancel failure must not raise; position still removed."""
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker._last_realised_pnl = 0.0
        pos = self._make_bracket_position()
        tracker.register(pos)

        with (
            patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=[]),
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=2000.0),
            patch("signal_engine.tracker.cancel_order", new_callable=AsyncMock, return_value=False),
        ):
            # Must not raise
            await tracker.check_positions()

        assert tracker.tracked_count == 0

    @pytest.mark.asyncio
    async def test_no_bracket_ids_no_cancel(self):
        """Position without bracket IDs should not attempt cancellation."""
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker._last_realised_pnl = 0.0
        pos = _make_position()  # no bracket IDs
        tracker.register(pos)

        with (
            patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=[]),
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=0.0),
            patch("signal_engine.tracker.cancel_order", new_callable=AsyncMock) as mock_cancel,
        ):
            await tracker.check_positions()

        mock_cancel.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancel_exception_does_not_propagate(self):
        """Exception in cancel_order must be caught gracefully."""
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker._last_realised_pnl = 0.0
        pos = self._make_bracket_position()
        tracker.register(pos)

        with (
            patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=[]),
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=2000.0),
            patch("signal_engine.tracker.cancel_order", new_callable=AsyncMock, side_effect=Exception("network down")),
        ):
            # Must not raise
            await tracker.check_positions()

        assert tracker.tracked_count == 0


class TestBatchPositionCheck:
    """Polling optimization: one positionbook call replaces N individual openposition calls."""

    @pytest.mark.asyncio
    async def test_batch_check_detects_closed_position(self):
        """Positionbook returns qty=0 for a tracked symbol -> position closed."""
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker._last_realised_pnl = 0.0
        tracker.register(_make_position(symbol="RELIANCE", strategy=ORB))

        # Positionbook returns empty list (RELIANCE not present = closed)
        positionbook = []
        with (
            patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=positionbook),
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=-200.0),
        ):
            await tracker.check_positions()

        assert tracker.tracked_count == 0
        assert engine.open_positions == 0

    @pytest.mark.asyncio
    async def test_batch_check_position_still_open(self):
        """Positionbook returns qty>0 for tracked symbol -> still open."""
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker.register(_make_position(symbol="RELIANCE", strategy=ORB))

        positionbook = [
            {"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "quantity": 50, "pnl": 100.0},
        ]
        with patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=positionbook):
            await tracker.check_positions()

        assert tracker.tracked_count == 1
        assert engine.open_positions == 1

    @pytest.mark.asyncio
    async def test_batch_check_multiple_positions_mixed(self):
        """Mix of open and closed positions in a single positionbook call."""
        engine = _make_engine()
        engine.open_positions = 2
        tracker = PositionTracker(engine)
        tracker._last_realised_pnl = 0.0
        tracker.register(_make_position(symbol="RELIANCE", strategy=ORB))
        tracker.register(_make_position(symbol="TCS", strategy=ORB))

        # TCS still open (qty=10), RELIANCE not in positionbook (closed)
        positionbook = [
            {"symbol": "TCS", "exchange": "NSE", "product": "MIS", "quantity": 10, "pnl": 50.0},
        ]
        with (
            patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=positionbook),
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=-100.0),
        ):
            await tracker.check_positions()

        assert tracker.tracked_count == 1  # only TCS remains
        assert engine.open_positions == 1

    @pytest.mark.asyncio
    async def test_batch_check_api_error_skips_cycle(self):
        """If positionbook returns None (API error), skip entire cycle."""
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker.register(_make_position())

        with patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=None):
            await tracker.check_positions()

        assert tracker.tracked_count == 1  # nothing removed
        assert engine.open_positions == 1

    @pytest.mark.asyncio
    async def test_batch_check_zero_qty_in_book_means_closed(self):
        """Positionbook may return the symbol with qty=0 (explicitly closed)."""
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker._last_realised_pnl = 0.0
        tracker.register(_make_position(symbol="RELIANCE", strategy=ORB))

        positionbook = [
            {"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "quantity": 0, "pnl": -150.0},
        ]
        with (
            patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=positionbook),
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=-150.0),
        ):
            await tracker.check_positions()

        assert tracker.tracked_count == 0

    @pytest.mark.asyncio
    async def test_single_api_call_for_all_positions(self):
        """Verify only 1 positionbook call is made regardless of position count."""
        engine = _make_engine()
        engine.open_positions = 3
        tracker = PositionTracker(engine)
        tracker.register(_make_position(symbol="RELIANCE", strategy=ORB))
        tracker.register(_make_position(symbol="TCS", strategy=ORB))
        tracker.register(_make_position(symbol="INFY", strategy=ORB))

        positionbook = [
            {"symbol": "RELIANCE", "exchange": "NSE", "product": "MIS", "quantity": 50, "pnl": 0},
            {"symbol": "TCS", "exchange": "NSE", "product": "MIS", "quantity": 10, "pnl": 0},
            {"symbol": "INFY", "exchange": "NSE", "product": "MIS", "quantity": 20, "pnl": 0},
        ]
        mock_pb = AsyncMock(return_value=positionbook)
        with patch("signal_engine.tracker.fetch_positionbook", mock_pb):
            await tracker.check_positions()

        # Exactly 1 API call, not 3
        mock_pb.assert_called_once()
