"""A failed realised-P&L read must not be booked as a real 0.0.

fetch_realised_pnl() used to return 0.0 on any error. Every snapshot then set the baseline to
0, so the next good read booked the whole day's realised P&L as one close, and the failed read
itself booked a fake delta of -baseline into the loss-limit counters.
"""

from unittest.mock import AsyncMock, patch

import pytest

from signal_engine.tests.tracker_fixtures import _make_engine
from signal_engine.tracker import PositionTracker


def _tracker(baseline: float) -> PositionTracker:
    tracker = PositionTracker(_make_engine())
    tracker._last_realised_pnl = baseline
    return tracker


@pytest.mark.asyncio
class TestTrackerSnapshot:
    async def test_good_read_books_the_delta_and_moves_the_baseline(self):
        tracker = _tracker(1000.0)
        with patch("signal_engine.tracker.fetch_realised_pnl", AsyncMock(return_value=1250.0)):
            assert await tracker._advance_realised_baseline() == (250.0, 1250.0)
        assert tracker._last_realised_pnl == 1250.0

    async def test_failed_read_books_nothing_and_keeps_the_baseline(self):
        tracker = _tracker(1000.0)
        with patch("signal_engine.tracker.fetch_realised_pnl", AsyncMock(return_value=None)):
            assert await tracker._advance_realised_baseline() == (0.0, 1000.0)
        assert tracker._last_realised_pnl == 1000.0

    async def test_time_exit_booking_survives_a_failed_read(self):
        tracker = _tracker(1000.0)
        with (
            patch("signal_engine.tracker.fetch_realised_pnl", AsyncMock(return_value=None)),
            patch.object(tracker, "_time_exit_leg_pnls", AsyncMock(return_value={})),
        ):
            await tracker._book_time_exit_trades({}, None, None)
        assert tracker._last_realised_pnl == 1000.0
        assert tracker._day_pnl == 0.0


@pytest.mark.asyncio
class TestStartupBaseline:
    async def test_startup_retries_a_failed_first_read(self):
        tracker = _tracker(0.0)
        reads = AsyncMock(side_effect=[None, None, 730.0])
        with (
            patch("signal_engine.tracker.fetch_realised_pnl", reads),
            patch("signal_engine.tracker.asyncio.sleep", AsyncMock()),
        ):
            await tracker._load_realised_baseline()
        assert tracker._last_realised_pnl == 730.0


@pytest.mark.asyncio
class TestMainSnapshot:
    async def test_failed_read_books_nothing_and_keeps_the_baseline(self):
        from signal_engine import main

        tracker = _tracker(1000.0)
        with (
            patch("signal_engine.main.tracker", tracker),
            patch("signal_engine.main.fetch_realised_pnl", AsyncMock(return_value=None)),
        ):
            assert await main._book_realised_pnl_delta() == (0.0, 1000.0)
        assert tracker._last_realised_pnl == 1000.0
