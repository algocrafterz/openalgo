"""A trade's OUTCOME belongs in the same channel as its entry.

For the PineScript strategies the engine acts on the ENTRY alert only: it sizes the position,
places the SL-M and the TP, and manages the trade from there. The PineScript's own "SL HIT" /
"TP1 HIT" alerts are ignored - they describe what the SCRIPT thinks happened on its chart,
which is not what the engine did with the order.

So a trader watching intraday-orb-analyze saw the entry, then messages the engine ignored,
and nothing at all about the real outcome - which went to the admin channel instead. The only
way to find out whether the stop or the target actually filled was to open the broker app.

Outcomes are now mirrored into the strategy's own channel: entry, then the close, in one
place. The admin channel still gets everything, since it is the cross-strategy view.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from signal_engine import notifier


class _Ch:
    def __init__(self, name, cid):
        self.name, self.id, self.enabled = name, cid, True


@pytest.fixture
def sent(monkeypatch):
    monkeypatch.setattr(notifier, "settings", MagicMock(
        telegram_channels=(_Ch("intraday-orb-analyze", -10),
                           _Ch("intraday-orb-live", -11)),
        notify_channel={"analyze": _Ch("signal-engine-analyze", -99)},
        notify_level="quiet"))
    client = MagicMock()
    client.send_message = AsyncMock()
    monkeypatch.setattr(notifier, "_client", client)
    monkeypatch.setattr(notifier, "_current_phase", AsyncMock(return_value="analyze"))
    return client


def _targets(client):
    return [c.args[0] for c in client.send_message.await_args_list]


class TestOutcomesReachTheStrategyChannel:
    @pytest.mark.asyncio
    async def test_a_close_reaches_both_the_strategy_and_admin_channels(self, sent):
        await notifier.notify_position_closed("SBIN", 250.0, strategy="ORB",
                                              exit_price=805.0, entry_price=800.0)
        assert set(_targets(sent)) == {-10, -99}

    @pytest.mark.asyncio
    async def test_the_message_names_which_exit_fired(self, sent):
        await notifier.notify_position_closed("SBIN", -120.0, strategy="ORB",
                                              exit_price=796.0, entry_price=800.0,
                                              exit_types=["SL"])
        body = sent.send_message.await_args_list[0].args[1]
        assert "SL" in body and "SBIN" in body

    @pytest.mark.asyncio
    async def test_an_entry_fill_reaches_the_strategy_channel(self, sent):
        await notifier.notify_entry_filled("SBIN", "LONG", 800.0, 10, 799.0, strategy="ORB")
        assert -10 in _targets(sent)

    @pytest.mark.asyncio
    async def test_a_time_exit_reaches_the_strategy_channel(self, sent):
        await notifier.notify_time_exit("SBIN", strategy="ORB", pnl=0.0)
        assert -10 in _targets(sent)

    @pytest.mark.asyncio
    async def test_only_this_phase_s_channel_is_used(self, sent):
        await notifier.notify_position_closed("SBIN", 1.0, strategy="ORB")
        assert -11 not in _targets(sent)

    @pytest.mark.asyncio
    async def test_a_strategy_with_no_channel_still_reaches_admin(self, sent):
        """EMA9VWAP is measured, not traded - no channel by design."""
        await notifier.notify_position_closed("SBIN", 1.0, strategy="EMA9VWAP")
        assert _targets(sent) == [-99]

    @pytest.mark.asyncio
    async def test_a_failing_strategy_channel_does_not_lose_the_admin_copy(self, sent):
        sent.send_message.side_effect = [None, RuntimeError("bot removed")]
        await notifier.notify_position_closed("SBIN", 1.0, strategy="ORB")
        assert sent.send_message.await_count == 2


class TestRoutineTrafficIsNotMirrored:
    @pytest.mark.asyncio
    async def test_an_order_placed_step_does_not_reach_the_strategy_channel(self, sent):
        """Only OUTCOMES are mirrored - the strategy channel already carries the alert that
        triggered the order, and duplicating every intermediate step would drown it."""
        await notifier.notify_order_placed("SBIN", "LONG", strategy="ORB", signal_price=800.0)
        assert -10 not in _targets(sent)
