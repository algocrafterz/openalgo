"""Close detection: the three guards, batch polling, orphan handling, OCO."""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

from signal_engine.strategies import ORB
from signal_engine.tracker import PositionTracker, TrackedPosition

_IST = timezone(timedelta(hours=5, minutes=30))
_AGED = datetime.now(_IST) - timedelta(minutes=5)  # past Guard 1 (30s min age)

from signal_engine.tests.tracker_fixtures import _make_engine, _make_position


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

    @pytest.mark.asyncio
    async def test_ghost_close_mid_morning_does_not_send_day_summary(self):
        """Ghost-close before the 30-min EOD window must NOT trigger send_day_summary.

        Positionbook momentarily returns qty=0 mid-morning (10:46 scenario).
        The time-exit scheduler will send the real summary at 3 PM.
        """
        _IST = timezone(timedelta(hours=5, minutes=30))
        fake_now = datetime.now(_IST).replace(hour=10, minute=46, second=0, microsecond=0)
        entry_time = fake_now - timedelta(hours=1)  # entered at 09:46

        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker._last_realised_pnl = 1000.0
        tracker.register(_make_position(entry_time=entry_time))
        tracker.send_day_summary = AsyncMock()

        with (
            patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=[]),
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=1500.0),
            patch("signal_engine.tracker.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = fake_now
            await tracker.check_positions()

        tracker.send_day_summary.assert_not_called()

    @pytest.mark.asyncio
    async def test_legitimate_close_near_eod_sends_day_summary(self):
        """Last position closing after 14:30 (within 30 min of 15:00) triggers summary."""
        _IST = timezone(timedelta(hours=5, minutes=30))
        fake_now = datetime.now(_IST).replace(hour=14, minute=45, second=0, microsecond=0)
        entry_time = fake_now - timedelta(hours=4)  # entered at ~10:45

        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker._last_realised_pnl = 1000.0
        tracker.register(_make_position(entry_time=entry_time))
        tracker.send_day_summary = AsyncMock()

        with (
            patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=[]),
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=1500.0),
            patch("signal_engine.tracker.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = fake_now
            await tracker.check_positions()

        tracker.send_day_summary.assert_called_once()


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

    @pytest.mark.asyncio
    async def test_guard2_processes_real_close_when_position_was_seen_filled(self):
        """Guard 2 timeout with ever_seen_nonzero_qty=True must NOT orphan the position.

        Reproduces the Apr 22 bug: orderstatus API returns '' (500 errors) but the position
        DID appear in positionbook with qty > 0 earlier. The tracker was wrongly releasing
        the slot as an orphan rejection, leaving live unmanaged broker positions.
        """
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker._last_realised_pnl = 1000.0  # baseline
        old_entry = datetime.now(_IST) - timedelta(minutes=35)  # > 30 min guard2_timeout
        pos = _make_position(fill_price=0.0, entry_order_id="ORDER_REAL", entry_time=old_entry)
        pos.sl_order_id = "SL_REAL"
        pos.ever_seen_nonzero_qty = True  # position WAS confirmed in positionbook
        tracker.register(pos)

        with (
            patch("signal_engine.tracker.fetch_positionbook", new_callable=AsyncMock, return_value=[]),
            patch("signal_engine.tracker.fetch_order_status", new_callable=AsyncMock, return_value=""),
            patch("signal_engine.tracker.cancel_order", new_callable=AsyncMock) as mock_cancel,
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=1200.0),
            patch("signal_engine.tracker.notifier.notify_orphaned_position", new_callable=AsyncMock) as mock_orphan,
            patch("signal_engine.tracker.notifier.notify_position_closed", new_callable=AsyncMock),
        ):
            await tracker.check_positions()

        # Must NOT be treated as orphan — slot released via record_close, not record_rejection
        mock_orphan.assert_not_awaited()
        mock_cancel.assert_not_awaited()  # SL not cancelled (already gone with the close)
        assert tracker.tracked_count == 0  # position recorded as closed
