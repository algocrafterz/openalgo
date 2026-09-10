"""C1: a channel only trades when its phase matches the mode OpenAlgo is actually in.

config.yaml's `channels:` block has always claimed a two-gate rule — `enabled: true` AND
OpenAlgo in that phase's mode. Only the first gate existed. _split_by_enabled() filtered on
`enabled` alone and nothing anywhere compared a channel's -analyze/-live suffix to the
running mode, so flipping OpenAlgo to LIVE (which is exactly what promoting a strategy
requires) would have put all four enabled -analyze channels onto real money, silently.

The check runs PER MESSAGE rather than at subscribe time, so a mid-session flip is caught
too — subscriptions are fixed once the client connects.
"""

from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import mode_guard
from signal_engine.listener import _channel_phase, _make_handler


@pytest.fixture(autouse=True)
def _clean_guard():
    mode_guard.reset()
    yield
    mode_guard.reset()


class TestChannelPhase:
    def test_reads_the_analyze_suffix(self):
        assert _channel_phase("intraday-orb-analyze") == "analyze"

    def test_reads_the_live_suffix(self):
        assert _channel_phase("intraday-breakingtrade-watchlist-live") == "live"

    def test_unsuffixed_channel_has_no_phase(self):
        """smidestn and any other ad-hoc channel stay phase-agnostic — they are not part
        of the promotion workflow the suffix encodes."""
        assert _channel_phase("smidestn") is None

    def test_is_case_insensitive(self):
        assert _channel_phase("Intraday-ORB-LIVE") == "live"


class _Msg:
    def __init__(self, text="ORB LONG\nSymbol: SBIN"):
        self.text = text


class _Event:
    def __init__(self, chat_id, text="ORB LONG\nSymbol: SBIN"):
        self.chat_id = chat_id
        self.message = _Msg(text)


def _handler(names):
    return _make_handler(AsyncMock(), names)


@pytest.mark.asyncio
async def _run(handler, event):
    await handler(event)


class TestPhaseGate:
    @pytest.mark.asyncio
    async def test_matching_phase_is_processed(self):
        on_message = AsyncMock()
        handler = _make_handler(on_message, {-1: "intraday-orb-analyze"})
        mode_guard.prime("analyze")
        with patch("signal_engine.listener._is_stale", return_value=None):
            await handler(_Event(-1))
        on_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_analyze_channel_is_refused_while_openalgo_is_live(self):
        """The exact scenario: paper strategies left enabled when the mode flips."""
        on_message = AsyncMock()
        handler = _make_handler(on_message, {-1: "intraday-orb-analyze"})
        mode_guard.prime("live")
        with patch("signal_engine.listener._is_stale", return_value=None):
            await handler(_Event(-1))
        on_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_live_channel_is_refused_while_openalgo_is_in_analyze(self):
        on_message = AsyncMock()
        handler = _make_handler(on_message, {-1: "intraday-orb-live"})
        mode_guard.prime("analyze")
        with patch("signal_engine.listener._is_stale", return_value=None):
            await handler(_Event(-1))
        on_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unsuffixed_channel_is_always_processed(self):
        on_message = AsyncMock()
        handler = _make_handler(on_message, {-1: "smidestn"})
        mode_guard.prime("live")
        with patch("signal_engine.listener._is_stale", return_value=None):
            await handler(_Event(-1))
        on_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_refused_entry_is_recorded_as_declined(self):
        """A signal that vanishes with no ledger row is unauditable — the whole point of
        save_declined. A mode mismatch is a decline, not a parse failure."""
        on_message = AsyncMock()
        handler = _make_handler(on_message, {-1: "intraday-orb-analyze"})
        mode_guard.prime("live")
        with patch("signal_engine.listener._is_stale", return_value=None), \
             patch("signal_engine.listener._record_phase_decline") as rec:
            await handler(_Event(-1))
        rec.assert_called_once()
