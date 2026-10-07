"""A Telegram outage must heal itself, not halt the engine until someone restarts it.

Incident 2026-10-07: the host lost its route to Telegram (Errno 113) from about 09:20. The
listener spent its five reconnect retries inside roughly three minutes, gave up for good, and
mode_guard.halt() - sticky by design - refused every entry for the rest of the session. The
network was back long before the operator noticed; nothing in the engine ever tried again.
No paper trades were taken all day, for any strategy.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import listener, mode_guard


@pytest.fixture(autouse=True)
def _clean_guard():
    mode_guard.reset()
    yield
    mode_guard.reset()


class _FloodWaitError(Exception):
    def __init__(self, seconds):
        self.seconds = seconds
        super().__init__(f"A wait of {seconds} seconds is required")


@pytest.fixture(autouse=True)
def _patch_flood_error(monkeypatch):
    monkeypatch.setattr(listener, "_flood_wait_error", lambda: _FloodWaitError)


def _run_scenario(failures: int):
    """connect() fails `failures` times, succeeds once (then the session drops), then floods
    until the flood budget ends the run, which is the only way the listener ever returns."""
    state = {"halted_at_connect": [], "calls": 0}

    async def _connect(_client):
        state["halted_at_connect"].append(mode_guard.is_halted())
        state["calls"] += 1
        if state["calls"] <= failures:
            raise OSError(113, "Connect call failed")
        if state["calls"] == failures + 1:
            return
        raise _FloodWaitError(1)

    return state, _connect


async def _drive(connect, sleeps):
    async def _sleep(secs):
        sleeps.append(secs)

    notify = AsyncMock()
    with patch.object(listener, "_connect", connect), \
         patch.object(listener.asyncio, "sleep", _sleep), \
         patch.object(listener, "_build_client", MagicMock()), \
         patch.object(listener, "_serve", AsyncMock()), \
         patch.object(listener.notifier, "notify_event", notify):
        await listener.start_listener(AsyncMock())
    return notify


class TestOutageDoesNotEndTheListener:
    @pytest.mark.asyncio
    async def test_keeps_retrying_past_the_fast_budget_and_recovers(self):
        failures = listener.settings.listener_max_retries + 4
        state, connect = _run_scenario(failures)
        sleeps = []
        await _drive(connect, sleeps)

        # Every failure, then the successful connect, was attempted: it did not give up at
        # the budget. (The trailing flood attempts are the harness ending the run.)
        assert state["calls"] >= failures + 1

    @pytest.mark.asyncio
    async def test_entries_are_blocked_while_down_and_released_on_reconnect(self):
        budget = listener.settings.listener_max_retries
        state, connect = _run_scenario(budget + 2)
        notify = await _drive(connect, [])

        flags = state["halted_at_connect"]
        assert flags[budget] is True          # budget spent, feed declared down
        assert flags[budget + 2] is True      # still down on the attempt that then succeeds
        assert flags[budget + 3] is False     # the first attempt after recovery sees it lifted
        assert not mode_guard.is_halted()

        events = [c.args[0] for c in notify.await_args_list]
        assert events.count("listener_down") == 1
        assert events.count("listener_restored") == 1

    @pytest.mark.asyncio
    async def test_a_short_blip_never_halts_anything(self):
        state, connect = _run_scenario(listener.settings.listener_max_retries - 1)
        notify = await _drive(connect, [])

        assert not any(state["halted_at_connect"])
        assert [c.args[0] for c in notify.await_args_list] == []

    @pytest.mark.asyncio
    async def test_backoff_is_capped_so_recovery_is_noticed_quickly(self):
        state, connect = _run_scenario(60)
        sleeps = []
        await _drive(connect, sleeps)

        assert max(sleeps) <= listener._MAX_RECONNECT_BACKOFF_SECONDS
        assert sleeps[0] == listener.settings.listener_base_backoff

    @pytest.mark.asyncio
    async def test_recovery_does_not_lift_a_mode_flip_halt(self):
        mode_guard.halt("OpenAlgo mode changed from ANALYZE to LIVE")
        state, connect = _run_scenario(listener.settings.listener_max_retries + 1)
        await _drive(connect, [])

        assert mode_guard.is_halted()
        assert "mode changed" in mode_guard.halt_reason()


class TestSignalsFromTheOutageAreNeverReplayed:
    """Recovery must not turn an hours-old alert into a fresh entry."""

    @staticmethod
    def _event(age_seconds: float):
        from datetime import UTC, datetime, timedelta

        msg = MagicMock()
        msg.text = "ORB LONG SBIN entry=800 sl=790 tp=820"
        msg.date = (datetime.now(UTC) - timedelta(seconds=age_seconds)).replace(tzinfo=None)
        event = MagicMock()
        event.message = msg
        event.chat_id = -1003518225740
        return event

    @pytest.mark.asyncio
    async def test_a_message_delivered_after_the_outage_is_dropped(self):
        on_message = AsyncMock()
        handler = listener._make_handler(on_message, {})
        with patch.object(listener, "_record_phase_decline"):
            await handler(self._event(listener.settings.stale_signal_seconds + 3600))
        on_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_fresh_message_still_reaches_the_pipeline(self):
        on_message = AsyncMock()
        handler = listener._make_handler(on_message, {})
        with patch.object(listener, "_phase_mismatch", AsyncMock(return_value=None)):
            await handler(self._event(1))
        on_message.assert_awaited_once()
