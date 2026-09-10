"""notify_channel's ANALYZE/LIVE split (2026-09-11) - same pattern and same reasoning as
signal_engine/analysis/breakingtrade/alerts.py's identical mechanism for the per-strategy
channels: paper-phase chatter must never sit in the same admin channel as a real-money alert.
"""

import pytest

from signal_engine import api_client, mode_guard, notifier


@pytest.fixture(autouse=True)
def reset_mode_cache():
    # notifier._current_phase() delegates to mode_guard, which owns the single process-wide
    # cache these three modules used to keep three copies of (2026-09-11).
    mode_guard.reset()
    yield
    mode_guard.reset()


def _async_returns(value):
    async def _fake(*_a, **_k):
        return value
    return _fake


class TestCurrentPhase:
    """_current_phase() reads OpenAlgo's live analyze/live state via
    api_client.fetch_trading_mode() - faked here rather than hitting the network."""

    @pytest.mark.asyncio
    async def test_analyze_mode_maps_to_the_analyze_phase(self, monkeypatch):
        monkeypatch.setattr(api_client, "fetch_trading_mode", _async_returns(("analyze", True)))
        assert await notifier._current_phase() == "analyze"

    @pytest.mark.asyncio
    async def test_live_mode_maps_to_the_live_phase(self, monkeypatch):
        monkeypatch.setattr(api_client, "fetch_trading_mode", _async_returns(("live", False)))
        assert await notifier._current_phase() == "live"

    @pytest.mark.asyncio
    async def test_unreachable_openalgo_falls_back_to_the_last_known_phase(self, monkeypatch):
        """Was "always analyze", which routed a live-money SL-FAILED alert into the paper
        channel whenever the API blipped. The last real answer is both safer and stabler."""
        monkeypatch.setattr(api_client, "fetch_trading_mode", _async_returns(("live", False)))
        assert await notifier._current_phase() == "live"
        mode_guard._cache["checked_at"] = 0.0
        monkeypatch.setattr(api_client, "fetch_trading_mode", _async_returns(("unknown", False)))
        assert await notifier._current_phase() == "live"

    @pytest.mark.asyncio
    async def test_unreachable_with_nothing_known_yet_defaults_to_analyze(self, monkeypatch):
        monkeypatch.setattr(api_client, "fetch_trading_mode", _async_returns(("unknown", False)))
        assert await notifier._current_phase() == "analyze"

    @pytest.mark.asyncio
    async def test_mode_is_cached_within_the_ttl(self, monkeypatch):
        calls = []

        async def fake():
            calls.append(1)
            return ("live", False)

        monkeypatch.setattr(api_client, "fetch_trading_mode", fake)
        await notifier._current_phase()
        await notifier._current_phase()
        assert len(calls) == 1


class TestChannelForPhase:
    def test_returns_the_matching_phase(self, monkeypatch):
        monkeypatch.setattr(notifier, "settings", type("S", (), {
            "notify_channel": {"analyze": "A", "live": "L"},
        })())
        assert notifier._channel_for_phase("analyze") == "A"
        assert notifier._channel_for_phase("live") == "L"

    def test_falls_back_to_whichever_phase_is_configured(self, monkeypatch):
        """A safety-relevant admin alert (SL failed, risk halted) must never be silently
        dropped just because one phase's channel isn't configured yet."""
        monkeypatch.setattr(notifier, "settings", type("S", (), {
            "notify_channel": {"analyze": "A"},
        })())
        assert notifier._channel_for_phase("live") == "A"

    def test_none_when_nothing_is_configured(self, monkeypatch):
        monkeypatch.setattr(notifier, "settings", type("S", (), {"notify_channel": {}})())
        assert notifier._channel_for_phase("analyze") is None


class _FakeMessage:
    def __init__(self, message_id):
        self.id = message_id


class _FakeClient:
    def __init__(self):
        self.sent = []
        self.pinned = []
        self.unpinned = []
        self._next_id = 1

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        message = _FakeMessage(self._next_id)
        self._next_id += 1
        return message

    async def pin_message(self, chat_id, message, notify=False):
        self.pinned.append((chat_id, message.id))

    async def unpin_message(self, chat_id, message_id):
        self.unpinned.append((chat_id, message_id))


class TestSendAndPinDaySummary:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            notifier, "_DAY_SUMMARY_PIN_STATE_PATH", str(tmp_path / "day_summary_pin_state.json")
        )
        monkeypatch.setattr(notifier, "settings", type("S", (), {
            "notify_channel": {"analyze": type("Ch", (), {"id": -100})()},
            "notify_level": "quiet",
        })())
        monkeypatch.setattr(notifier, "_current_phase", _async_returns("analyze"))
        self.client = _FakeClient()
        monkeypatch.setattr(notifier, "_client", self.client)

    @pytest.mark.asyncio
    async def test_first_summary_is_sent_and_pinned(self):
        delivered = await notifier._send_and_pin_day_summary("DAY SUMMARY | text")

        assert delivered is True
        assert self.client.sent == [(-100, "DAY SUMMARY | text")]
        assert self.client.pinned == [(-100, 1)]
        assert self.client.unpinned == []

    @pytest.mark.asyncio
    async def test_second_days_summary_unpins_the_first(self):
        await notifier._send_and_pin_day_summary("day 1")
        await notifier._send_and_pin_day_summary("day 2")

        assert self.client.pinned == [(-100, 1), (-100, 2)]
        assert self.client.unpinned == [(-100, 1)]

    @pytest.mark.asyncio
    async def test_pin_state_persists_across_process_restarts(self):
        """A fresh process (a real restart) must still find yesterday's pinned message id from
        the on-disk state file, not just within one call."""
        await notifier._send_and_pin_day_summary("day 1")

        second_client = _FakeClient()
        import signal_engine.notifier as notifier_module
        notifier_module._client = second_client
        await notifier._send_and_pin_day_summary("day 2")

        assert second_client.unpinned == [(-100, 1)]

    @pytest.mark.asyncio
    async def test_pin_failure_does_not_affect_the_delivered_result(self, monkeypatch):
        """The send already succeeded by the time pinning is attempted - a pin/unpin failure
        (e.g. the bot lost admin rights) must not be reported as a failed delivery."""
        async def failing_pin(*a, **k):
            raise RuntimeError("no pin rights")

        monkeypatch.setattr(self.client, "pin_message", failing_pin)

        delivered = await notifier._send_and_pin_day_summary("text")

        assert delivered is True
        assert self.client.sent == [(-100, "text")]

    @pytest.mark.asyncio
    async def test_suppressed_by_notify_level_sends_nothing(self, monkeypatch):
        monkeypatch.setattr(notifier, "should_notify", lambda *a, **k: False)

        delivered = await notifier._send_and_pin_day_summary("text")

        assert delivered is False
        assert self.client.sent == []

    @pytest.mark.asyncio
    async def test_client_not_ready_sends_nothing(self, monkeypatch):
        monkeypatch.setattr(notifier, "_client", None)

        delivered = await notifier._send_and_pin_day_summary("text")

        assert delivered is False
