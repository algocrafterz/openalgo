"""Every enabled strategy channel gets an EOD summary - including "nothing traded today".

2026-09-11: intraday-orb and intraday-breakout got no EOD summary at all.
_send_per_strategy_day_summaries() iterates `by_strategy`, which is built from
tracker._completed_trades - so a strategy with no CLOSED trade is simply absent, and the
channel stays silent.

Silence is the one thing a monitoring channel must never mean two things by. ORB that day had
one declined and one broker-rejected signal; BREAKOUT had a TP HIT arrive for a position it
never opened. "No summary" is indistinguishable from "the engine was down", which is exactly
the failure mode `enabled: false` was introduced to end for channels.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import notifier


class _Ch:
    def __init__(self, name, cid):
        self.name, self.id, self.enabled = name, cid, True


CHANNELS = (
    _Ch("intraday-orb-analyze", -1),
    _Ch("intraday-breakout-analyze", -2),
    _Ch("intraday-breakingtrade-analyze", -3),
)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(notifier, "settings", MagicMock(
        telegram_channels=CHANNELS, notify_level="quiet", notify_channel={}))
    c = MagicMock()
    c.send_message = AsyncMock()
    monkeypatch.setattr(notifier, "_client", c)
    return c


def _sent(client):
    return {call.args[0]: call.args[1] for call in client.send_message.await_args_list}


class TestQuietStrategiesStillReport:
    @pytest.mark.asyncio
    async def test_a_strategy_with_no_trades_still_gets_a_summary(self, client):
        await notifier._send_per_strategy_day_summaries({}, "11-Sep-2026", {}, "analyze")
        assert set(_sent(client)) == {-1, -2, -3}

    @pytest.mark.asyncio
    async def test_the_quiet_message_says_no_trades(self, client):
        await notifier._send_per_strategy_day_summaries({}, "11-Sep-2026", {}, "analyze")
        assert "No trades" in _sent(client)[-1]

    @pytest.mark.asyncio
    async def test_a_strategy_that_traded_gets_its_real_summary(self, client):
        rec = MagicMock(strategy="BREAKINGTRADE", symbol="SBIN", total_pnl=500.0,
                        r_multiple=1.2, exit_types=["TP1"], direction="LONG",
                        entry_price=800.0, exit_price=805.0)
        await notifier._send_per_strategy_day_summaries(
            {"BREAKINGTRADE": [rec]}, "11-Sep-2026", {"BREAKINGTRADE": 100_000.0}, "analyze")
        sent = _sent(client)
        assert "SBIN" in sent[-3]
        assert "No trades" in sent[-1] and "No trades" in sent[-2]

    @pytest.mark.asyncio
    async def test_only_this_phase_s_channels_are_written_to(self, client, monkeypatch):
        monkeypatch.setattr(notifier, "settings", MagicMock(
            telegram_channels=CHANNELS + (_Ch("intraday-orb-live", -9),),
            notify_level="quiet", notify_channel={}))
        await notifier._send_per_strategy_day_summaries({}, "11-Sep-2026", {}, "analyze")
        assert -9 not in _sent(client)

    @pytest.mark.asyncio
    async def test_a_disabled_channel_is_not_written_to(self, client, monkeypatch):
        off = _Ch("intraday-breakout-analyze", -2)
        off.enabled = False
        monkeypatch.setattr(notifier, "settings", MagicMock(
            telegram_channels=(CHANNELS[0], off), notify_level="quiet", notify_channel={}))
        await notifier._send_per_strategy_day_summaries({}, "11-Sep-2026", {}, "analyze")
        assert set(_sent(client)) == {-1}

    @pytest.mark.asyncio
    async def test_one_channel_failing_does_not_stop_the_others(self, client):
        client.send_message.side_effect = [RuntimeError("bot removed"), None, None]
        await notifier._send_per_strategy_day_summaries({}, "11-Sep-2026", {}, "analyze")
        assert client.send_message.await_count == 3
