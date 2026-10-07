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

from signal_engine import listener, main, mode_guard


@pytest.fixture(autouse=True)
def _clean_guard():
    """The listener tests below drive more than listener.max_retries failures, which declares the
    feed down (mode_guard.halt_feed). Without this reset the halt outlives the test and refuses
    every entry in any test file that runs after this one."""
    mode_guard.reset()
    yield
    mode_guard.reset()


class _FloodWaitError(Exception):
    def __init__(self, seconds):
        self.seconds = seconds
        super().__init__(f"A wait of {seconds} seconds is required")


def _fail_then_flood(failures: int):
    """connect() side effect: `failures` ordinary errors, then floods until the budget ends."""
    state = {"n": 0}

    def _effect(*_a, **_k):
        state["n"] += 1
        if state["n"] <= failures:
            raise RuntimeError("down")
        raise _FloodWaitError(60)

    return _effect


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
    async def test_exiting_after_ordinary_failures_then_flood_disconnects(self, monkeypatch):
        """Plain connection failures no longer end the listener (see
        test_listener_outage_recovery); it exits only when the flood budget is spent. The
        client must still be released on that exit, after any number of ordinary failures."""
        monkeypatch.setattr(listener, "_flood_wait_error", lambda: _FloodWaitError)
        client = MagicMock()
        client.disconnect = AsyncMock()
        with patch.object(listener, "_build_client", return_value=client), \
             patch.object(listener, "_connect", AsyncMock(side_effect=_fail_then_flood(8))), \
             patch.object(listener.asyncio, "sleep", AsyncMock()), \
             patch.object(listener, "_serve", AsyncMock()):
            await listener.start_listener(AsyncMock())
        client.disconnect.assert_awaited()

    @pytest.mark.asyncio
    async def test_cancelling_the_listener_during_an_outage_disconnects(self, monkeypatch):
        """Shutdown while the feed is down is the exit path that matters now: the retry loop
        has no natural end, so cancellation must still release the socket and session."""
        monkeypatch.setattr(listener, "_flood_wait_error", lambda: _FloodWaitError)
        real_sleep = asyncio.sleep
        client = MagicMock()
        client.disconnect = AsyncMock()

        async def _yielding_sleep(_secs):
            await real_sleep(0)

        with patch.object(listener, "_build_client", return_value=client), \
             patch.object(listener, "_connect", AsyncMock(side_effect=RuntimeError("down"))), \
             patch.object(listener.asyncio, "sleep", _yielding_sleep), \
             patch.object(listener, "_serve", AsyncMock()):
            task = asyncio.create_task(listener.start_listener(AsyncMock()))
            await real_sleep(0.05)
            assert not task.done()  # still retrying, did not give up
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        client.disconnect.assert_awaited()

    @pytest.mark.asyncio
    async def test_a_failing_disconnect_does_not_raise(self, monkeypatch):
        """Teardown must not become the failure."""
        monkeypatch.setattr(listener, "_flood_wait_error", lambda: _FloodWaitError)
        client = MagicMock()
        client.disconnect = AsyncMock(side_effect=RuntimeError("already gone"))
        with patch.object(listener, "_build_client", return_value=client), \
             patch.object(listener, "_connect", AsyncMock(side_effect=_fail_then_flood(3))), \
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
