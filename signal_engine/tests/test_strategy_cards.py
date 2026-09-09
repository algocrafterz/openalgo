"""Per-channel reference cards (strategy_cards.py) - pinned to each strategy's Telegram
channel so a trader can see what it does / when it runs / how to act without leaving the app.
"""

import pytest

from signal_engine import strategy_cards
from signal_engine.config import TelegramChannel


class TestCardsRegistry:
    def test_every_live_signal_channel_has_a_card(self):
        """The three channels that actually carry trade signals must each have a card - a
        missing entry means send_and_pin_cards() silently skips that channel."""
        for name in ("intraday-orb", "intraday-breakout", "intraday-breakingtrade"):
            assert name in strategy_cards.CARDS

    def test_card_has_all_four_fields_non_empty(self):
        for card in strategy_cards.CARDS.values():
            assert card.title and card.what and card.when and card.action and card.updated


class TestRender:
    def test_render_includes_every_field(self):
        card = strategy_cards.CARDS["intraday-orb"]
        text = strategy_cards.render(card)

        assert card.title in text
        assert card.what in text
        assert card.when in text
        assert card.action in text
        assert card.updated in text

    def test_render_is_plain_text_no_emoji(self):
        """Project convention (CLAUDE.md): no icons/emoji anywhere, including Telegram text."""
        for card in strategy_cards.CARDS.values():
            text = strategy_cards.render(card)
            assert text.isascii(), f"non-ASCII content (likely emoji) in card: {card.title}"


class _FakeMessage:
    pass


class _FakeClient:
    def __init__(self, fail_pin_for=()):
        self.sent = []
        self.pinned = []
        self._fail_pin_for = set(fail_pin_for)

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))
        return _FakeMessage()

    async def pin_message(self, chat_id, message, notify=False):
        if chat_id in self._fail_pin_for:
            raise RuntimeError("no pin rights in this chat")
        self.pinned.append(chat_id)


class TestSendAndPinCards:
    @pytest.mark.asyncio
    async def test_pins_a_card_for_every_known_channel(self):
        client = _FakeClient()
        channels = [
            TelegramChannel(name="intraday-orb", id=-100),
            TelegramChannel(name="intraday-breakout", id=-200),
            TelegramChannel(name="intraday-breakingtrade", id=-300),
        ]

        pinned = await strategy_cards.send_and_pin_cards(client, channels)

        assert pinned == 3
        assert set(client.pinned) == {-100, -200, -300}

    @pytest.mark.asyncio
    async def test_unknown_channel_name_is_skipped_not_errored(self):
        client = _FakeClient()
        channels = [TelegramChannel(name="smidestn", id=-1)]

        pinned = await strategy_cards.send_and_pin_cards(client, channels)

        assert pinned == 0
        assert client.pinned == []

    @pytest.mark.asyncio
    async def test_one_channel_failing_to_pin_does_not_block_the_others(self):
        client = _FakeClient(fail_pin_for={-200})
        channels = [
            TelegramChannel(name="intraday-orb", id=-100),
            TelegramChannel(name="intraday-breakout", id=-200),
        ]

        pinned = await strategy_cards.send_and_pin_cards(client, channels)

        assert pinned == 1
        assert client.pinned == [-100]
