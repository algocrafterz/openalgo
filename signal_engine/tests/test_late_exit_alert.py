"""E-15: a late EXIT alert is processed against the current position, never silently dropped,
and never acts on a position entered after the alert was sent."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import freshness, listener, main
from signal_engine.tests.test_main_characterization import (
    _exit_signal,
    _position,
    _settings_stub,
    harness,
)
from signal_engine.timeutils import IST

EXIT_TEXT = "RSI-TP-MR EXIT\nSymbol: RELIANCE\nProduct: CNC\nEntry: 2500\nSL: 2480\nTP: 2540"
ENTRY_TEXT = "ORB LONG SBIN entry=800 sl=790 tp=820"


def _event(text, age_seconds):
    msg = MagicMock()
    msg.text = text
    msg.date = (datetime.now(UTC) - timedelta(seconds=age_seconds)).replace(tzinfo=None)
    event = MagicMock()
    event.message = msg
    event.chat_id = -1003518225740
    return event


class TestListener:
    @pytest.mark.asyncio
    async def test_a_late_exit_reaches_the_pipeline(self):
        on_message = AsyncMock()
        handler = listener._make_handler(on_message, {})
        with patch.object(listener, "_phase_mismatch", AsyncMock(return_value=None)):
            await handler(_event(EXIT_TEXT, listener.settings.stale_signal_seconds + 3600))
        on_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_late_entry_is_still_dropped(self):
        on_message = AsyncMock()
        handler = listener._make_handler(on_message, {})
        with patch.object(listener, "_record_phase_decline"):
            await handler(_event(ENTRY_TEXT, listener.settings.stale_signal_seconds + 3600))
        on_message.assert_not_awaited()


class TestExitPath:
    @staticmethod
    def _late(seconds_ago):
        return datetime.now(UTC) - timedelta(seconds=seconds_ago)

    @pytest.mark.asyncio
    async def test_a_late_exit_sent_before_the_entry_is_refused(self):
        h = harness(settings=_settings_stub(stale_signal_seconds=60))
        pos = _position()
        pos.entry_time = datetime.now(IST) - timedelta(minutes=5)
        h.tracker.register(pos)
        with h.stack, patch.object(main, "_decline", AsyncMock()) as decline, \
                freshness.origin_scope(self._late(3600)):
            await main._handle_exit_locked(_exit_signal())
        assert decline.await_args.kwargs["stage"] == "late_exit"
        h.send_order.assert_not_awaited()
        assert pos.exit_pending is False

    @pytest.mark.asyncio
    async def test_a_late_exit_with_no_tracked_position_is_refused(self):
        h = harness(settings=_settings_stub(stale_signal_seconds=60))
        with h.stack, patch.object(main, "_decline", AsyncMock()) as decline, \
                freshness.origin_scope(self._late(3600)):
            await main._handle_exit_locked(_exit_signal())
        assert decline.await_args.kwargs["stage"] == "late_exit"
        h.send_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_late_exit_sent_after_the_entry_is_processed(self):
        h = harness(settings=_settings_stub(stale_signal_seconds=60))
        pos = _position()
        pos.entry_time = datetime.now(IST) - timedelta(hours=3)
        h.tracker.register(pos)
        with h.stack, patch.object(main, "_decline", AsyncMock()) as decline, \
                freshness.origin_scope(self._late(600)):
            await main._handle_exit_locked(_exit_signal())
        assert not any(c.kwargs.get("stage") == "late_exit" for c in decline.await_args_list)
        h.send_order.assert_awaited()

    @pytest.mark.asyncio
    async def test_a_fresh_exit_is_never_judged_here(self):
        h = harness(settings=_settings_stub(stale_signal_seconds=60))
        pos = _position()
        h.tracker.register(pos)  # entered just now; the alert is fresh
        with h.stack, patch.object(main, "_decline", AsyncMock()) as decline, \
                freshness.origin_scope(self._late(1)):
            await main._handle_exit_locked(_exit_signal())
        assert not any(c.kwargs.get("stage") == "late_exit" for c in decline.await_args_list)


class TestEntryFreshnessAtSend:
    """E-19 / S-06: the freshness limit is checked again immediately before the order."""

    @pytest.mark.asyncio
    async def test_an_entry_that_aged_during_sizing_is_not_sent(self):
        h = harness(settings=_settings_stub(stale_signal_seconds=60))
        with h.stack, patch.object(main, "_decline", AsyncMock()) as decline:
            with freshness.origin_scope(datetime.now(UTC) - timedelta(seconds=300)):
                refused = await main._entry_stale_before_send(_exit_signal())
        assert refused is True
        assert decline.await_args.kwargs["stage"] == "stale"

    @pytest.mark.asyncio
    async def test_a_fresh_entry_and_an_unknown_origin_are_sent(self):
        h = harness(settings=_settings_stub(stale_signal_seconds=60))
        with h.stack, patch.object(main, "_decline", AsyncMock()):
            assert await main._entry_stale_before_send(_exit_signal()) is False  # no origin
            with freshness.origin_scope(datetime.now(UTC) - timedelta(seconds=2)):
                assert await main._entry_stale_before_send(_exit_signal()) is False
