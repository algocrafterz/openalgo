"""A dependency that stops working must not do it silently.

Both cases below were found in production on 2026-09-11 by reading OpenAlgo's traffic log,
not the engine's own - which is the problem.

fetch_order_fill_price: 564 calls, 4 successes. `/api/v1/orderstatus` answers 404 ("Order
    not found in orderbook") and every failure was swallowed at DEBUG, so nothing reached
    errors_*.jsonl. The engine then falls back to the signal's entry price, which silently
    turns off no_progress.use_fill_price_for_progress ("Recommended true - keeps the metric
    aligned with what the trade actually risks") and makes every R-multiple and day-summary
    entry price a quote rather than a fill.

check_positions: 16 consecutive positionbook failures (403, 11:50-11:52 IST) each logged one
    WARNING and skipped the cycle. For those 2.5 minutes the tracker was blind - no close
    detection, no no-progress gate, no time-exit trigger - and nothing said so.

This is the shape of the fetch_bars UTC bug the root CLAUDE.md records: structurally
incapable of succeeding, with nothing to search the logs for.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import api_client
from signal_engine.tracker import PositionTracker


class TestFillPriceFailureIsReported:
    @pytest.mark.asyncio
    async def test_exhausting_every_attempt_warns_once(self, caplog):
        from loguru import logger

        lines = []
        sink = logger.add(lines.append, level="WARNING", format="{message}")
        try:
            with patch.object(api_client, "_post_json",
                              AsyncMock(side_effect=RuntimeError("404 NOT FOUND"))), \
                 patch.object(api_client.asyncio, "sleep", AsyncMock()):
                result = await api_client.fetch_order_fill_price("O1", "ORB")
        finally:
            logger.remove(sink)
        assert result is None
        text = " ".join(lines)
        assert "O1" in text and "fill price" in text.lower()

    @pytest.mark.asyncio
    async def test_a_successful_read_warns_about_nothing(self):
        from loguru import logger

        lines = []
        sink = logger.add(lines.append, level="WARNING", format="{message}")
        try:
            with patch.object(api_client, "_post_json", AsyncMock(return_value={
                "status": "success",
                "data": {"order_status": "complete", "average_price": "801.25"},
            })):
                price = await api_client.fetch_order_fill_price("O1", "ORB")
        finally:
            logger.remove(sink)
        assert price == 801.25
        assert lines == []

    @pytest.mark.asyncio
    async def test_a_rejected_order_is_not_reported_as_a_failure(self):
        """Rejected is an ANSWER, not a broken dependency."""
        from loguru import logger

        lines = []
        sink = logger.add(lines.append, level="WARNING", format="{message}")
        try:
            with patch.object(api_client, "_post_json", AsyncMock(return_value={
                "status": "success", "data": {"order_status": "rejected"},
            })):
                assert await api_client.fetch_order_fill_price("O1", "ORB") is None
        finally:
            logger.remove(sink)
        assert lines == []


class TestPositionbookOutageIsEscalated:
    def _tracker(self):
        return PositionTracker(MagicMock())

    @pytest.mark.asyncio
    async def test_a_single_failure_does_not_alert(self):
        """One failed poll is noise - the next cycle is 5 seconds away."""
        tracker = self._tracker()
        with patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=None)), \
             patch("signal_engine.notifier.notify_event", AsyncMock()) as notify:
            await tracker.check_positions()
        notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_sustained_outage_alerts_once(self):
        tracker = self._tracker()
        with patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=None)), \
             patch("signal_engine.notifier.notify_event", AsyncMock()) as notify:
            for _ in range(tracker._POSITIONBOOK_OUTAGE_POLLS + 3):
                await tracker.check_positions()
        assert notify.await_count == 1
        assert notify.await_args.args[0] == "positionbook_outage"

    @pytest.mark.asyncio
    async def test_recovery_is_announced_and_rearms_the_alert(self):
        tracker = self._tracker()
        fetch = AsyncMock(return_value=None)
        with patch("signal_engine.tracker.fetch_positionbook", fetch), \
             patch("signal_engine.notifier.notify_event", AsyncMock()) as notify:
            for _ in range(tracker._POSITIONBOOK_OUTAGE_POLLS):
                await tracker.check_positions()
            assert notify.await_count == 1
            fetch.return_value = []
            await tracker.check_positions()
            assert notify.await_count == 2
            assert notify.await_args.args[0] == "positionbook_recovered"
            fetch.return_value = None
            for _ in range(tracker._POSITIONBOOK_OUTAGE_POLLS):
                await tracker.check_positions()
            assert notify.await_count == 3

    @pytest.mark.asyncio
    async def test_the_counter_resets_on_every_good_poll(self):
        tracker = self._tracker()
        fetch = AsyncMock(return_value=None)
        with patch("signal_engine.tracker.fetch_positionbook", fetch), \
             patch("signal_engine.notifier.notify_event", AsyncMock()) as notify:
            for _ in range(tracker._POSITIONBOOK_OUTAGE_POLLS - 1):
                await tracker.check_positions()
            fetch.return_value = []
            await tracker.check_positions()
            fetch.return_value = None
            for _ in range(tracker._POSITIONBOOK_OUTAGE_POLLS - 1):
                await tracker.check_positions()
        notify.assert_not_awaited()
