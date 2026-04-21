"""Tests for position tracker."""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

from signal_engine.risk import RiskEngine
from signal_engine.strategies import ORB, RSI_TP_MR
from signal_engine.tracker import PositionTracker, TrackedPosition

_IST = timezone(timedelta(hours=5, minutes=30))
_AGED = datetime.now(_IST) - timedelta(minutes=5)  # past Guard 1 (30s min age)


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
        "entry_time": _AGED,  # bypass Guard 1 (min_position_age_seconds) in close-detection tests
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
            "fill_price": 2500.0,  # confirmed fill bypasses Guard 2 (order status check)
            "quantity": 50,
            "sl": 2485.0,
            "tp": 2540.0,
            "entry_order_id": "ENTRY001",
            "sl_order_id": "SL001",
            "entry_time": _AGED,
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


class TestOrphanSlCancel:
    """Orphaned SL orders must be cancelled when a position is released as orphan."""

    @pytest.mark.asyncio
    async def test_guard2_immediate_rejection_cancels_sl(self):
        """Guard 2: broker returns 'rejected' -> slot released AND orphaned SL cancelled."""
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        pos = _make_position(fill_price=0.0, entry_order_id="ENTRY_REJ")
        pos.sl_order_id = "SL_ORPHAN_REJ"
        tracker.register(pos)

        with (
            patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=[]),
            patch("signal_engine.tracker.fetch_order_status", new_callable=AsyncMock, return_value="rejected"),
            patch("signal_engine.tracker.cancel_order", new_callable=AsyncMock, return_value=True) as mock_cancel,
            patch("signal_engine.tracker.notifier.notify_orphaned_position", new_callable=AsyncMock),
        ):
            await tracker.check_positions()

        assert tracker.tracked_count == 0
        mock_cancel.assert_awaited_once_with("SL_ORPHAN_REJ", pos.strategy)

    @pytest.mark.asyncio
    async def test_guard3_zero_pnl_cancels_sl(self):
        """Guard 3: zero PnL with unconfirmed fill -> orphan released AND orphaned SL cancelled."""
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker._last_realised_pnl = 0.0
        pos = _make_position(fill_price=0.0, entry_order_id="ENTRY_G3")
        pos.sl_order_id = "SL_ORPHAN_G3"
        tracker.register(pos)

        with (
            patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=[]),
            patch("signal_engine.tracker.fetch_order_status", new_callable=AsyncMock, return_value="complete"),
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=0.0),
            patch("signal_engine.tracker.cancel_order", new_callable=AsyncMock, return_value=True) as mock_cancel,
            patch("signal_engine.tracker.notifier.notify_orphaned_position", new_callable=AsyncMock),
        ):
            await tracker.check_positions()

        assert tracker.tracked_count == 0
        mock_cancel.assert_awaited_once_with("SL_ORPHAN_G3", pos.strategy)

    @pytest.mark.asyncio
    async def test_no_sl_order_id_no_cancel_on_orphan(self):
        """Orphan release with no sl_order_id should not attempt cancel."""
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        pos = _make_position(fill_price=0.0, entry_order_id="ENTRY_NOSL")
        pos.sl_order_id = ""
        tracker.register(pos)

        with (
            patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=[]),
            patch("signal_engine.tracker.fetch_order_status", new_callable=AsyncMock, return_value="rejected"),
            patch("signal_engine.tracker.cancel_order", new_callable=AsyncMock) as mock_cancel,
            patch("signal_engine.tracker.notifier.notify_orphaned_position", new_callable=AsyncMock),
        ):
            await tracker.check_positions()

        assert tracker.tracked_count == 0
        mock_cancel.assert_not_called()


class TestGuard2Timeout:
    """Guard 2 — ambiguous order status — bypasses wait after guard2_timeout_minutes."""

    @pytest.mark.asyncio
    async def test_guard2_waits_when_young_and_status_unknown(self):
        """Position younger than guard2_timeout should keep waiting on unknown order status."""
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker._last_realised_pnl = 0.0
        # entry_time = 5 min ago (passes Guard 1, but < 30 min guard2_timeout)
        pos = _make_position(fill_price=0.0, entry_order_id="ORDER123")
        tracker.register(pos)

        with (
            patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=[]),
            patch("signal_engine.tracker.fetch_order_status", new_callable=AsyncMock, return_value=""),
        ):
            await tracker.check_positions()

        assert tracker.tracked_count == 1  # still waiting

    @pytest.mark.asyncio
    async def test_guard2_releases_as_orphan_when_old_and_status_unknown(self):
        """Position older than guard2_timeout with still-unknown orderstatus is treated as
        orphaned rejection (not 'assumed complete'). Slot released, SL cancelled."""
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker._last_realised_pnl = 1000.0
        old_entry = datetime.now(_IST) - timedelta(minutes=35)  # > 30 min guard2_timeout
        pos = _make_position(fill_price=0.0, entry_order_id="ORDER456", entry_time=old_entry)
        pos.sl_order_id = "SL_ORPHAN"
        tracker.register(pos)

        with (
            patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=[]),
            patch("signal_engine.tracker.fetch_order_status", new_callable=AsyncMock, return_value=""),
            patch("signal_engine.tracker.cancel_order", new_callable=AsyncMock, return_value=True) as mock_cancel,
            patch("signal_engine.tracker.notifier.notify_orphaned_position", new_callable=AsyncMock),
        ):
            await tracker.check_positions()

        assert tracker.tracked_count == 0  # orphan released
        mock_cancel.assert_awaited_once_with("SL_ORPHAN", pos.strategy)  # orphaned SL cancelled


class TestNoProgressProfitLock:
    """no_progress break-even with profit_lock_ratio locks partial unrealized profit."""

    @pytest.mark.asyncio
    async def test_profit_lock_raises_sl_above_entry(self):
        """With profit_lock_ratio=0.4 and ltp > entry, new SL should be above entry."""
        from signal_engine.models import Direction

        engine = _make_engine()
        tracker = PositionTracker(engine)
        pos = _make_position(
            entry_price=541.80,
            fill_price=541.80,
            sl=528.01,
            tp=551.10,
            direction=Direction.LONG,
            entry_time=datetime.now(_IST) - timedelta(minutes=150),
        )
        tracker.register(pos)

        book_data = {"RELIANCE": (1, 544.70)}

        with (
            patch("signal_engine.tracker.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.tracker.place_sl_order") as mock_place,
            patch("signal_engine.tracker.notifier.notify_be_stop_applied", new_callable=AsyncMock),
        ):
            from signal_engine.models import TradeResult, OrderStatus
            mock_place.return_value = TradeResult(
                status=OrderStatus.SUCCESS, order_id="NEW_SL", message=""
            )
            with patch("signal_engine.config.settings") as mock_settings:
                mock_settings.no_progress_enabled = True
                mock_settings.no_progress_check_after_minutes = 90
                mock_settings.no_progress_min_progress_pct = 0.33
                mock_settings.no_progress_profit_lock_ratio = 0.40
                mock_settings.time_exit_enabled = False  # disable market-exit path
                await tracker._check_no_progress(book_data)

        mock_place.assert_called_once()
        _, kwargs = mock_place.call_args
        sl_placed = kwargs.get("sl_price", mock_place.call_args[0][3] if mock_place.call_args[0] else None)
        # With lock 0.4 and ltp=544.70, entry=541.80: new SL = 541.80 + (544.70-541.80)*0.4 = 542.96
        assert sl_placed is not None and sl_placed > 541.80

    @pytest.mark.asyncio
    async def test_strict_breakeven_when_lock_ratio_zero(self):
        """With profit_lock_ratio=0.0, SL should move to exact entry price."""
        from signal_engine.models import Direction

        engine = _make_engine()
        tracker = PositionTracker(engine)
        pos = _make_position(
            entry_price=541.80,
            fill_price=541.80,
            sl=528.01,
            tp=551.10,
            direction=Direction.LONG,
            entry_time=datetime.now(_IST) - timedelta(minutes=150),
        )
        tracker.register(pos)

        book_data = {"RELIANCE": (1, 544.70)}

        with (
            patch("signal_engine.tracker.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.tracker.place_sl_order") as mock_place,
            patch("signal_engine.tracker.notifier.notify_be_stop_applied", new_callable=AsyncMock),
        ):
            from signal_engine.models import TradeResult, OrderStatus
            mock_place.return_value = TradeResult(
                status=OrderStatus.SUCCESS, order_id="NEW_SL", message=""
            )
            with patch("signal_engine.config.settings") as mock_settings:
                mock_settings.no_progress_enabled = True
                mock_settings.no_progress_check_after_minutes = 90
                mock_settings.no_progress_min_progress_pct = 0.33
                mock_settings.no_progress_profit_lock_ratio = 0.0
                mock_settings.time_exit_enabled = False  # disable market-exit path
                await tracker._check_no_progress(book_data)

        mock_place.assert_called_once()
        _, kwargs = mock_place.call_args
        sl_placed = kwargs.get("sl_price", mock_place.call_args[0][3] if mock_place.call_args[0] else None)
        assert sl_placed == 541.80

    @pytest.mark.asyncio
    async def test_market_exit_when_rate_too_slow(self):
        """Market exit when projected time-to-TP1 at current rate exceeds time remaining to exit.

        Setup: entry=541.80 TP=551.10 (9.30pt), after 150min at ltp=544.70 (31.2% progress).
        Rate = 0.312/150 = 0.00208/min. Minutes needed = 0.688/0.00208 = 331min.
        With time exit 60min away: 331 > 60 → market exit fires.
        """
        from signal_engine.models import Direction, TradeResult, OrderStatus

        engine = _make_engine()
        tracker = PositionTracker(engine)
        pos = _make_position(
            entry_price=541.80,
            fill_price=541.80,
            sl=528.01,
            tp=551.10,
            sl_order_id="OLD_SL",
            direction=Direction.LONG,
            entry_time=datetime.now(_IST) - timedelta(minutes=150),
        )
        tracker.register(pos)

        book_data = {"RELIANCE": (1, 544.70)}  # progress = (544.70-541.80)/(551.10-541.80) = 31.2%
        now = datetime.now(_IST)
        exit_time = now + timedelta(minutes=60)  # 331min needed > 60min remaining → market exit

        with (
            patch("signal_engine.tracker.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.tracker.send_order", new_callable=AsyncMock,
                  return_value=TradeResult(status=OrderStatus.SUCCESS, order_id="MKT_EXIT", message="")) as mock_send,
            patch("signal_engine.tracker.place_sl_order") as mock_sl,
            patch("signal_engine.tracker.notifier.notify_no_progress_exit", new_callable=AsyncMock),
        ):
            with patch("signal_engine.config.settings") as mock_settings:
                mock_settings.no_progress_enabled = True
                mock_settings.no_progress_check_after_minutes = 90
                mock_settings.no_progress_min_progress_pct = 0.33
                mock_settings.no_progress_profit_lock_ratio = 0.0
                mock_settings.time_exit_enabled = True
                mock_settings.time_exit_hour = exit_time.hour
                mock_settings.time_exit_minute = exit_time.minute
                await tracker._check_no_progress(book_data)

        mock_send.assert_called_once()   # market exit placed
        mock_sl.assert_not_called()      # no break-even SL
