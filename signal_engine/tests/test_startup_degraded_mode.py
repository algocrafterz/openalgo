"""H6 (second half): a dead listener must not take the tracker down with it.

start_listener() returning was treated by _serve_until_shutdown() as "the engine is done",
so _run_engine()'s finally block stopped the tracker AND the time-exit scheduler. On
2026-09-08 that meant every open position lost its close detection, its no-progress gates
and its 14:45 square-off, sixteen times over, during market hours.

Losing the signal feed is bad. Abandoning open risk because the signal feed died is worse.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import mode_guard, startup


class TestListenerDeathDoesNotEndTheSession:
    @pytest.mark.asyncio
    async def test_engine_keeps_serving_after_the_listener_gives_up(self, logged):
        shutdown = asyncio.Event()

        async def _dead_listener(_on_message):
            return  # gave up immediately

        async def _shutdown_soon():
            await asyncio.sleep(0.05)
            shutdown.set()

        with patch.object(startup, "start_listener", _dead_listener):
            asyncio.create_task(_shutdown_soon())
            await asyncio.wait_for(
                startup._serve_until_shutdown(AsyncMock(), shutdown), timeout=2.0
            )

        assert shutdown.is_set()
        # The feed is gone: it is logged at CRITICAL and new entries are blocked. Silence here
        # is the failure mode.
        assert logged.has("DEGRADED MODE", "CRITICAL")
        assert mode_guard.is_halted()

    @pytest.mark.asyncio
    async def test_shutdown_signal_still_ends_the_session_promptly(self):
        shutdown = asyncio.Event()

        async def _live_listener(_on_message):
            await asyncio.sleep(30)  # would outlive the test

        shutdown.set()
        with patch.object(startup, "start_listener", _live_listener):
            await asyncio.wait_for(
                startup._serve_until_shutdown(AsyncMock(), shutdown), timeout=2.0
            )
