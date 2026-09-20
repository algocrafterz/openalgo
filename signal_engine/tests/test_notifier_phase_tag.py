"""The [PAPER]/[LIVE] phase tag every Telegram send carries, added 2026-09-20 the night before
the first live day.

WHY THIS EXISTS

Until this change, whether a message described real money or paper money depended entirely on
which physical Telegram channel it happened to land in - nothing in the text itself said so
(except notify_startup_summary()'s one banner line). A muted channel, a forwarded screenshot,
or simply misremembering which chat is which, and a paper fill could read as a real one or vice
versa. _deliver() is the single low-level send every path in notifier.py now goes through, so
this file pins that EVERY path gets the tag - a new notify_* function or a new send path cannot
forget it, because it never has to know the tag exists.
"""

from unittest.mock import AsyncMock, patch

import pytest

from signal_engine import notifier
from signal_engine.config import TelegramChannel


def _async_returns(value):
    async def _fake(*_a, **_k):
        return value
    return _fake


class _FakeClient:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return type("Msg", (), {"id": 1})()

    async def pin_message(self, *a, **k):
        pass

    async def unpin_message(self, *a, **k):
        pass


class TestDeliverTagsByPhase:
    @pytest.mark.asyncio
    async def test_analyze_gets_the_paper_tag(self):
        client = _FakeClient()
        await notifier._deliver(client, -100, "hello", "analyze")
        assert client.sent == [(-100, "[PAPER] hello")]

    @pytest.mark.asyncio
    async def test_live_gets_the_live_tag(self):
        client = _FakeClient()
        await notifier._deliver(client, -100, "hello", "live")
        assert client.sent == [(-100, "[LIVE] hello")]

    @pytest.mark.asyncio
    async def test_unknown_phase_gets_no_tag_rather_than_a_wrong_one(self):
        """An unrecognised phase string must never silently claim to be either - see
        _PHASE_TAG.get(phase, "") - a wrong tag would be worse than no tag."""
        client = _FakeClient()
        await notifier._deliver(client, -100, "hello", "unknown")
        assert client.sent == [(-100, "hello")]


class TestEveryNotifierSendPathIsTagged:
    """Each of notifier.py's four independent send paths (notify(), _mirror_to_strategy(),
    _send_and_pin_day_summary(), _send_oneshot()) used to call _client.send_message() (or its
    own client) directly - one of these being missed here is exactly the kind of gap a shared
    _deliver() is meant to make impossible."""

    @pytest.mark.asyncio
    async def test_notify_tags_the_message(self, monkeypatch):
        client = _FakeClient()
        monkeypatch.setattr(notifier, "_client", client)
        monkeypatch.setattr(notifier, "settings", type("S", (), {
            "notify_channel": {"analyze": TelegramChannel(name="signal-engine-analyze", id=-1)},
            "notify_level": "quiet",
        })())
        monkeypatch.setattr(notifier, "_current_phase", _async_returns("analyze"))

        await notifier.notify("order sent")

        assert client.sent == [(-1, "[PAPER] order sent")]

    @pytest.mark.asyncio
    async def test_mirror_to_strategy_tags_the_message(self, monkeypatch):
        client = _FakeClient()
        monkeypatch.setattr(notifier, "_client", client)
        monkeypatch.setattr(notifier, "_current_phase", _async_returns("live"))
        monkeypatch.setattr(
            notifier, "_channel_for_strategy",
            lambda strategy, phase: TelegramChannel(name="intraday-orb-live", id=-2),
        )

        await notifier._mirror_to_strategy("entry_filled", "filled", "ORB")

        assert client.sent == [(-2, "[LIVE] filled")]

    @pytest.mark.asyncio
    async def test_send_and_pin_day_summary_tags_the_message(self, monkeypatch, tmp_path):
        client = _FakeClient()
        monkeypatch.setattr(notifier, "_client", client)
        monkeypatch.setattr(
            notifier, "_DAY_SUMMARY_PIN_STATE_PATH", str(tmp_path / "pin_state.json")
        )
        monkeypatch.setattr(notifier, "settings", type("S", (), {
            "notify_channel": {"live": TelegramChannel(name="signal-engine-live", id=-3)},
            "notify_level": "quiet",
        })())
        monkeypatch.setattr(notifier, "_current_phase", _async_returns("live"))

        await notifier._send_and_pin_day_summary("DAY SUMMARY | text")

        assert client.sent == [(-3, "[LIVE] DAY SUMMARY | text")]

    @pytest.mark.asyncio
    async def test_send_oneshot_tags_the_message(self, monkeypatch):
        monkeypatch.setattr(notifier, "settings", type("S", (), {
            "notify_channel": {"analyze": TelegramChannel(name="signal-engine-analyze", id=-4)},
            "telegram_api_id": 1, "telegram_api_hash": "x",
        })())
        monkeypatch.setattr(notifier, "_current_phase", _async_returns("analyze"))
        mock_client = AsyncMock()
        mock_client.is_user_authorized.return_value = True

        with patch("telethon.TelegramClient", return_value=mock_client):
            await notifier._send_oneshot("startup text")

        channel_id, message = mock_client.send_message.call_args[0]
        assert channel_id == -4
        assert message == "[PAPER] startup text"
