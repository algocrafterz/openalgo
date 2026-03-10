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
        "margin_multiplier": {"MIS": 0.20, "NRML": 0.25, "CNC": 1.0},
        "max_capital_utilization": 0.80,
        "default_product": "MIS",
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
    async def test_sl_triggered_cancels_tp(self):
        """When position closes with loss (SL triggered), cancel the pending TP."""
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker._last_realised_pnl = 0.0
        pos = self._make_bracket_position()
        tracker.register(pos)

        with (
            patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=[]),
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=-500.0),
            patch("signal_engine.tracker.cancel_order", new_callable=AsyncMock, return_value=True) as mock_cancel,
        ):
            await tracker.check_positions()

        # TP should be cancelled because SL was triggered (loss realized)
        mock_cancel.assert_called_once_with("TP001", "ORB")

    @pytest.mark.asyncio
    async def test_tp_triggered_cancels_sl(self):
        """When position closes with profit (TP hit), cancel the pending SL."""
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker._last_realised_pnl = 0.0
        pos = self._make_bracket_position()
        tracker.register(pos)

        with (
            patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=[]),
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=2000.0),
            patch("signal_engine.tracker.cancel_order", new_callable=AsyncMock, return_value=True) as mock_cancel,
        ):
            await tracker.check_positions()

        # SL should be cancelled because TP was triggered (profit realized)
        mock_cancel.assert_called_once_with("SL001", "ORB")

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


class TestMarginRelease:
    """When a position closes, remove_margin must be called on the risk engine."""

    @pytest.mark.asyncio
    async def test_position_closed_removes_margin(self):
        """Closing a position calls remove_margin on the risk engine."""
        from unittest.mock import MagicMock
        engine = _make_engine()
        engine.open_positions = 1
        # Spy on remove_margin
        engine.remove_margin = MagicMock(wraps=engine.remove_margin)
        tracker = PositionTracker(engine)
        tracker._last_realised_pnl = 0.0

        pos = _make_position(symbol="RELIANCE", product="MIS", entry_price=2500.0, quantity=10)
        tracker.register(pos)

        with (
            patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=[]),
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=1000.0),
        ):
            await tracker.check_positions()

        engine.remove_margin.assert_called_once_with(
            qty=10,
            entry_price=2500.0,
            product="MIS",
        )


class TestBatchPositionCheck:
    """Polling optimization: one positionbook call replaces N individual openposition calls."""

    @pytest.mark.asyncio
    async def test_batch_check_detects_closed_position(self):
        """Positionbook returns qty=0 for a tracked symbol -> position closed."""
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker._last_realised_pnl = 0.0
        tracker.register(_make_position(symbol="RELIANCE", strategy="ORB"))

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
        tracker.register(_make_position(symbol="RELIANCE", strategy="ORB"))

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
        tracker.register(_make_position(symbol="RELIANCE", strategy="ORB"))
        tracker.register(_make_position(symbol="TCS", strategy="ORB"))

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
        tracker.register(_make_position(symbol="RELIANCE", strategy="ORB"))

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
        tracker.register(_make_position(symbol="RELIANCE", strategy="ORB"))
        tracker.register(_make_position(symbol="TCS", strategy="ORB"))
        tracker.register(_make_position(symbol="INFY", strategy="ORB"))

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
