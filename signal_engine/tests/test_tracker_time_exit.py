"""Time exit: CNC awareness and broker close verification."""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

from signal_engine.strategies import ORB, RSI_TP_MR
from signal_engine.tracker import PositionTracker

_IST = timezone(timedelta(hours=5, minutes=30))
_AGED = datetime.now(_IST) - timedelta(minutes=5)  # past Guard 1 (30s min age)

from signal_engine.tests.tracker_fixtures import _make_engine, _make_position


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
            patch("signal_engine.tracker.fetch_open_position", new_callable=AsyncMock, return_value=0),
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=0.0),
            patch("signal_engine.tracker.notifier", new_callable=AsyncMock),
            patch("asyncio.sleep", new_callable=AsyncMock),
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
            patch("signal_engine.tracker.fetch_open_position", new_callable=AsyncMock, return_value=0),
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=0.0),
            patch("signal_engine.tracker.notifier", new_callable=AsyncMock),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await tracker.time_exit_all()

        assert tracker.tracked_count == 0


class TestTimeExitVerification:
    """time_exit_all() should verify broker closure and retry if positions remain."""

    @pytest.mark.asyncio
    async def test_confirmed_closed_first_attempt(self):
        """Position confirmed closed on first verification — no retry needed."""
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker.register(_make_position(symbol="RELIANCE", strategy=ORB, product="MIS"))

        mock_close = AsyncMock()
        mock_cancel = AsyncMock()
        with (
            patch("signal_engine.tracker.cancel_all_orders", mock_cancel),
            patch("signal_engine.tracker.close_all_positions", mock_close),
            patch("signal_engine.tracker.fetch_open_position", new_callable=AsyncMock, return_value=0),
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=0.0),
            patch("signal_engine.tracker.notifier", new_callable=AsyncMock),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await tracker.time_exit_all()

        # close_all_positions called once — no retry
        assert mock_close.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_when_position_still_open(self):
        """Close retried when broker still shows open position on first check."""
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker.register(_make_position(symbol="RELIANCE", strategy=ORB, product="MIS"))

        # First verification: still open (qty=50). Second: closed (qty=0).
        mock_open_pos = AsyncMock(side_effect=[50, 0])
        mock_close = AsyncMock()
        mock_cancel = AsyncMock()
        with (
            patch("signal_engine.tracker.cancel_all_orders", mock_cancel),
            patch("signal_engine.tracker.close_all_positions", mock_close),
            patch("signal_engine.tracker.fetch_open_position", mock_open_pos),
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=0.0),
            patch("signal_engine.tracker.notifier", new_callable=AsyncMock),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await tracker.time_exit_all()

        # close_all_positions called twice: initial + one retry
        assert mock_close.call_count == 2
        assert tracker.tracked_count == 0

    @pytest.mark.asyncio
    async def test_alert_sent_when_all_retries_fail(self):
        """Telegram alert sent when position remains open after all retry attempts."""
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker.register(_make_position(symbol="RELIANCE", strategy=ORB, product="MIS"))

        # Always still open
        mock_open_pos = AsyncMock(return_value=50)
        mock_notify = AsyncMock()
        with (
            patch("signal_engine.tracker.cancel_all_orders", new_callable=AsyncMock),
            patch("signal_engine.tracker.close_all_positions", new_callable=AsyncMock),
            patch("signal_engine.tracker.fetch_open_position", mock_open_pos),
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=0.0),
            patch("signal_engine.tracker.notifier") as mock_notifier,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_notifier.notify = mock_notify
            mock_notifier.notify_time_exit = AsyncMock()
            mock_notifier.notify_day_summary = AsyncMock()
            await tracker.time_exit_all()

        mock_notify.assert_called_once()
        assert "TIME EXIT FAILED" in mock_notify.call_args[0][0]

    @pytest.mark.asyncio
    async def test_api_error_does_not_block_cleanup(self):
        """Verification API error (-1) does not count as still-open; cleanup proceeds."""
        engine = _make_engine()
        engine.open_positions = 1
        tracker = PositionTracker(engine)
        tracker.register(_make_position(symbol="RELIANCE", strategy=ORB, product="MIS"))

        with (
            patch("signal_engine.tracker.cancel_all_orders", new_callable=AsyncMock),
            patch("signal_engine.tracker.close_all_positions", new_callable=AsyncMock),
            patch("signal_engine.tracker.fetch_open_position", new_callable=AsyncMock, return_value=-1),
            patch("signal_engine.tracker.fetch_realised_pnl", new_callable=AsyncMock, return_value=0.0),
            patch("signal_engine.tracker.notifier", new_callable=AsyncMock),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await tracker.time_exit_all()

        # Tracker still cleaned up despite unverifiable API response
        assert tracker.tracked_count == 0
