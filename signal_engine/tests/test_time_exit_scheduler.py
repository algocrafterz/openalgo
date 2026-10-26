"""TimeExitScheduler: fires once a day at the configured IST time, and survives a failure.

startup starts the scheduler with asyncio.create_task and never awaits it, so an exception out
of time_exit_all used to end the task silently: no log line from the scheduler, no alert, and
no time exit for the rest of the session.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import tracker as tracker_mod
from signal_engine.timeutils import IST
from signal_engine.tracker import TimeExitScheduler


def _clock(hour: int, minute: int):
    fake = MagicMock(wraps=datetime)
    fake.now = MagicMock(return_value=datetime(2026, 10, 22, hour, minute, 0, tzinfo=IST))
    return patch.object(tracker_mod, "datetime", fake)


async def _run_one_cycle(scheduler: TimeExitScheduler) -> None:
    async def _stop_after_one(_seconds):
        scheduler.stop()

    with patch("asyncio.sleep", side_effect=_stop_after_one):
        await scheduler.start()


@pytest.mark.asyncio
async def test_fires_at_the_configured_time():
    tracker = MagicMock()
    tracker.time_exit_all = AsyncMock()
    with _clock(14, 45):
        scheduler = TimeExitScheduler(tracker, 14, 45)
        await _run_one_cycle(scheduler)
    tracker.time_exit_all.assert_awaited_once()


@pytest.mark.asyncio
async def test_does_not_fire_before_the_configured_time():
    tracker = MagicMock()
    tracker.time_exit_all = AsyncMock()
    with _clock(14, 44):
        scheduler = TimeExitScheduler(tracker, 14, 45)
        await _run_one_cycle(scheduler)
    tracker.time_exit_all.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failing_time_exit_is_logged_and_alerted_and_the_loop_survives():
    tracker = MagicMock()
    tracker.time_exit_all = AsyncMock(side_effect=RuntimeError("boom"))
    with _clock(14, 45), patch.object(tracker_mod, "notifier") as notifier:
        notifier.notify = AsyncMock()
        scheduler = TimeExitScheduler(tracker, 14, 45)
        await _run_one_cycle(scheduler)  # must not raise

    tracker.time_exit_all.assert_awaited_once()  # fired once, not retried
    assert "TIME EXIT ERROR" in notifier.notify.call_args[0][0]
