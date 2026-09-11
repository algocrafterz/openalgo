"""fd-audit follow-ups: two resources that outlived their owner.

1. The Telethon client was never disconnected when the listener gave up. Harmless while
   start_listener() returning ended the process - degraded mode (H6) deliberately keeps the
   process alive for the rest of the session, so it now holds a dead TCP socket plus the
   session's SQLite handle until shutdown. A fix meant to make failure safer must not leak.

2. _exit_locks grew without bound: _get_exit_lock() writes one asyncio.Lock per
   "SYMBOL:STRATEGY" and nothing removed it. Small per entry, but the key space is "any NSE
   name" (BreakingTrade scans the whole F&O list) and sessions now survive failures they used
   to die from.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import listener, main


class _FloodWaitError(Exception):
    def __init__(self, seconds):
        self.seconds = seconds
        super().__init__(f"A wait of {seconds} seconds is required")


class TestListenerReleasesItsClient:
    @pytest.mark.asyncio
    async def test_giving_up_after_flood_waits_disconnects(self, monkeypatch):
        monkeypatch.setattr(listener, "_flood_wait_error", lambda: _FloodWaitError)
        client = MagicMock()
        client.disconnect = AsyncMock()
        with patch.object(listener, "_build_client", return_value=client), \
             patch.object(listener, "_connect", AsyncMock(side_effect=_FloodWaitError(60))), \
             patch.object(listener.asyncio, "sleep", AsyncMock()), \
             patch.object(listener, "_serve", AsyncMock()):
            await listener.start_listener(AsyncMock())
        client.disconnect.assert_awaited()

    @pytest.mark.asyncio
    async def test_giving_up_after_ordinary_failures_disconnects(self, monkeypatch):
        monkeypatch.setattr(listener, "_flood_wait_error", lambda: _FloodWaitError)
        client = MagicMock()
        client.disconnect = AsyncMock()
        with patch.object(listener, "_build_client", return_value=client), \
             patch.object(listener, "_connect", AsyncMock(side_effect=RuntimeError("nope"))), \
             patch.object(listener.asyncio, "sleep", AsyncMock()), \
             patch.object(listener, "_serve", AsyncMock()):
            await listener.start_listener(AsyncMock())
        client.disconnect.assert_awaited()

    @pytest.mark.asyncio
    async def test_a_failing_disconnect_does_not_raise(self, monkeypatch):
        """Teardown must not become the failure."""
        monkeypatch.setattr(listener, "_flood_wait_error", lambda: _FloodWaitError)
        client = MagicMock()
        client.disconnect = AsyncMock(side_effect=RuntimeError("already gone"))
        with patch.object(listener, "_build_client", return_value=client), \
             patch.object(listener, "_connect", AsyncMock(side_effect=RuntimeError("nope"))), \
             patch.object(listener.asyncio, "sleep", AsyncMock()), \
             patch.object(listener, "_serve", AsyncMock()):
            await listener.start_listener(AsyncMock())


class TestExitLocksAreReleased:
    def setup_method(self):
        main._exit_locks.clear()

    def teardown_method(self):
        main._exit_locks.clear()

    def test_a_lock_is_created_on_first_use(self):
        main._get_exit_lock("SBIN", "ORB")
        assert "SBIN:ORB" in main._exit_locks

    def test_the_same_lock_comes_back_for_the_same_position(self):
        assert main._get_exit_lock("SBIN", "ORB") is main._get_exit_lock("SBIN", "ORB")

    def test_release_drops_the_entry(self):
        main._get_exit_lock("SBIN", "ORB")
        main._release_exit_lock("SBIN", "ORB")
        assert "SBIN:ORB" not in main._exit_locks

    def test_releasing_an_unknown_position_is_a_no_op(self):
        main._release_exit_lock("NOTHING", "ORB")

    def test_a_held_lock_is_not_dropped(self):
        """Dropping a lock another task is inside would let a second exit run concurrently -
        the exact duplicate-order race the lock exists to prevent."""
        async def _check():
            lock = main._get_exit_lock("SBIN", "ORB")
            async with lock:
                main._release_exit_lock("SBIN", "ORB")
                assert "SBIN:ORB" in main._exit_locks
            main._release_exit_lock("SBIN", "ORB")
            assert "SBIN:ORB" not in main._exit_locks

        asyncio.run(_check())

    def test_only_that_position_is_released(self):
        main._get_exit_lock("SBIN", "ORB")
        main._get_exit_lock("SBIN", "BREAKOUT")
        main._release_exit_lock("SBIN", "ORB")
        assert "SBIN:BREAKOUT" in main._exit_locks


class TestUnregisterReleasesTheLock:
    def test_tracker_unregister_frees_the_exit_lock(self):
        """unregister() is where a position's per-position state is already purged
        (_last_debug_log), so it is where the lock belongs too."""
        from signal_engine.models import Direction
        from signal_engine.tracker import PositionTracker, TrackedPosition

        main._exit_locks.clear()
        tracker = PositionTracker(MagicMock())
        tracker.register(TrackedPosition(
            symbol="SBIN", strategy="ORB", exchange="NSE", product="MIS",
            entry_price=800.0, quantity=10, sl=796.0, tp=810.0,
            direction=Direction.LONG, entry_order_id="E1",
        ))
        main._get_exit_lock("SBIN", "ORB")
        tracker.unregister("SBIN", "ORB")
        assert "SBIN:ORB" not in main._exit_locks
