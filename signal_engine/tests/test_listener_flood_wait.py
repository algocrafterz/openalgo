"""H6: honour Telegram's FLOOD_WAIT instead of retrying inside the ban window.

Confirmed incident, logs/errors_2026-09-08.jsonl, 11:15:50 -> 11:39:53 IST — 96 records:
  64x  "A wait of N seconds is required (caused by GetUsersRequest)"
  16x  "A wait of N seconds is required (caused by InvokeWithLayerRequest(...))"
  16x  "Max retries exceeded, listener shutting down"

The GetUsersRequest ones were _keepalive()'s own get_me() calls (see _KEEPALIVE_INTERVAL).
The retry loop then backed off 2/4/8/16/32s against a stated 349-second wait, so every retry
landed inside the ban and extended it, and after five the listener gave up.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import listener


class _FloodWaitError(Exception):
    def __init__(self, seconds):
        self.seconds = seconds
        super().__init__(f"A wait of {seconds} seconds is required")


@pytest.fixture(autouse=True)
def _patch_flood_error(monkeypatch):
    monkeypatch.setattr(listener, "_flood_wait_error", lambda: _FloodWaitError)


class TestFloodWaitBackoff:
    def test_sleeps_for_the_stated_wait_not_the_exponential_backoff(self):
        wait = listener._flood_wait_seconds(_FloodWaitError(349))
        assert 349 <= wait <= 349 + listener._FLOOD_WAIT_JITTER_SECONDS

    def test_wait_is_capped(self):
        wait = listener._flood_wait_seconds(_FloodWaitError(99_999))
        assert wait == listener._MAX_FLOOD_WAIT_SECONDS

    def test_a_missing_seconds_attribute_falls_back_to_the_cap(self):
        class _Bare(Exception):
            pass

        assert listener._flood_wait_seconds(_Bare()) == listener._MAX_FLOOD_WAIT_SECONDS


class TestFloodWaitDoesNotBurnRetries:
    @pytest.mark.asyncio
    async def test_flood_waits_use_their_own_budget(self):
        """A flood wait is not a connection failure — the wait IS the remedy, so it must not
        consume the reconnect budget meant for genuine failures. It gets its own, bounded
        budget so a permanent throttle still terminates."""
        slept = []

        async def _sleep(secs):
            slept.append(secs)

        connect = AsyncMock(side_effect=_FloodWaitError(60))
        with patch.object(listener, "_connect", connect), \
             patch.object(listener.asyncio, "sleep", _sleep), \
             patch.object(listener, "_build_client", MagicMock()), \
             patch.object(listener, "_serve", AsyncMock()):
            await listener.start_listener(AsyncMock())

        assert connect.await_count == listener._MAX_CONSECUTIVE_FLOOD_WAITS
        assert all(s >= 60 for s in slept)
