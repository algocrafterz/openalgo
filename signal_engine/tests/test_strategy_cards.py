"""Per-channel reference cards (strategy_cards.py) - pinned to each strategy's Telegram
channel so a trader can see what it does / when it runs / how to act without leaving the app.
"""

import pytest

from signal_engine import strategy_cards
from signal_engine.config import TelegramChannel


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Every test starts from "nothing pinned yet" - without this, the pin-once behaviour
    tested below would read/write the real signal_engine/data/strategy_card_state.json and
    tests would interfere with each other (and with a real running engine)."""
    monkeypatch.setattr(strategy_cards, "_STATE_PATH", str(tmp_path / "strategy_card_state.json"))


class TestCardsRegistry:
    def test_every_live_signal_channel_has_a_card(self):
        """The four channels that actually carry trade signals must each have a card - a
        missing entry means send_and_pin_cards() silently skips that channel."""
        for name in (
            "intraday-orb",
            "intraday-breakout",
            "intraday-breakingtrade",
            "intraday-breakingtrade-watchlist",
        ):
            assert name in strategy_cards.CARDS

    def test_card_has_all_four_fields_non_empty(self):
        for card in strategy_cards.CARDS.values():
            assert card.title and card.what and card.when and card.action and card.updated

    def test_btst_has_a_card_even_though_it_is_never_auto_traded(self):
        assert "intraday-breakingtrade-btst" in strategy_cards.CARDS


class TestBaseNameAndPhase:
    """Every channel in production is now named "<strategy>-analyze" or "<strategy>-live" (see
    config.yaml's 2026-09-11 split) - CARDS is still keyed by the bare strategy name, so
    send_and_pin_cards must strip the suffix before looking a card up, or every channel
    silently stops getting its pinned card (exactly what shipped, briefly, before this test)."""

    def test_strips_the_analyze_suffix(self):
        assert strategy_cards._base_name("intraday-orb-analyze") == "intraday-orb"

    def test_strips_the_live_suffix(self):
        assert strategy_cards._base_name("intraday-orb-live") == "intraday-orb"

    def test_leaves_an_unsuffixed_name_alone(self):
        assert strategy_cards._base_name("smidestn") == "smidestn"

    def test_phase_of_analyze_channel(self):
        assert strategy_cards._phase("intraday-orb-analyze") == "analyze"

    def test_phase_of_live_channel(self):
        assert strategy_cards._phase("intraday-orb-live") == "live"

    def test_phase_of_unsuffixed_channel_is_none(self):
        assert strategy_cards._phase("smidestn") is None


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

    def test_analyze_phase_states_no_real_order_is_placed(self):
        card = strategy_cards.CARDS["intraday-orb"]
        text = strategy_cards.render(card, "analyze")
        assert "simulated" in text
        assert "No real order" in text

    def test_live_phase_states_a_real_order_is_placed(self):
        card = strategy_cards.CARDS["intraday-orb"]
        text = strategy_cards.render(card, "live")
        assert "REAL order" in text

    def test_no_phase_omits_the_phase_line_entirely(self):
        """Back-compat: callers that don't know the phase (or a non-split channel) get exactly
        the old rendering, with no phase line at all."""
        card = strategy_cards.CARDS["intraday-orb"]
        assert strategy_cards.render(card) == strategy_cards.render(card, None)
        assert "CHANNEL" not in strategy_cards.render(card)


class _FakeMessage:
    def __init__(self, message_id):
        self.id = message_id


class _FakeClient:
    def __init__(self, fail_pin_for=()):
        self.sent = []
        self.pinned = []
        self.unpinned = []
        self._fail_pin_for = set(fail_pin_for)
        self._next_message_id = 1

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))
        message = _FakeMessage(self._next_message_id)
        self._next_message_id += 1
        return message

    async def pin_message(self, chat_id, message, notify=False):
        if chat_id in self._fail_pin_for:
            raise RuntimeError("no pin rights in this chat")
        self.pinned.append(chat_id)

    async def unpin_message(self, chat_id, message_id):
        self.unpinned.append((chat_id, message_id))


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
    async def test_analyze_and_live_channels_both_pin_the_same_strategys_card(self):
        """The real config.yaml shape: two physical channels per strategy, "-analyze" and
        "-live", each must resolve to that strategy's ONE card (with a different phase line)."""
        client = _FakeClient()
        channels = [
            TelegramChannel(name="intraday-orb-analyze", id=-101),
            TelegramChannel(name="intraday-orb-live", id=-102),
        ]

        pinned = await strategy_cards.send_and_pin_cards(client, channels)

        assert pinned == 2
        assert set(client.pinned) == {-101, -102}
        sent_by_chat = dict(client.sent)
        assert "simulated" in sent_by_chat[-101]
        assert "REAL order" in sent_by_chat[-102]

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

    @pytest.mark.asyncio
    async def test_second_call_with_unchanged_cards_repins_nothing(self):
        """The actual ask: a routine restart must not re-send/re-pin when nothing changed."""
        client = _FakeClient()
        channels = [TelegramChannel(name="intraday-orb", id=-100)]

        first = await strategy_cards.send_and_pin_cards(client, channels)
        second = await strategy_cards.send_and_pin_cards(client, channels)

        assert first == 1
        assert second == 0
        assert client.sent == [(-100, strategy_cards.render(strategy_cards.CARDS["intraday-orb"]))]
        assert client.pinned == [-100]  # not called a second time

    @pytest.mark.asyncio
    async def test_content_change_repins_and_unpins_the_stale_message(self, monkeypatch):
        """Editing CARDS (the documented way to keep a card current) must trigger exactly one
        fresh pin, and the old pinned message must be unpinned so only one stays pinned."""
        client = _FakeClient()
        channels = [TelegramChannel(name="intraday-orb", id=-100)]

        await strategy_cards.send_and_pin_cards(client, channels)
        assert client.unpinned == []

        original = strategy_cards.CARDS["intraday-orb"]
        changed = strategy_cards.StrategyCard(
            title=original.title,
            what="A materially different description of what ORB does.",
            when=original.when,
            action=original.action,
            updated="2099-01-01",
        )
        monkeypatch.setitem(strategy_cards.CARDS, "intraday-orb", changed)

        pinned = await strategy_cards.send_and_pin_cards(client, channels)

        assert pinned == 1
        assert client.pinned == [-100, -100]  # pinned again for the new content
        assert client.unpinned == [(-100, 1)]  # the FIRST call's message (id 1) is unpinned

    @pytest.mark.asyncio
    async def test_pin_state_persists_across_process_restarts(self):
        """The whole point: a NEW send_and_pin_cards call (a fresh process, as a real restart
        would be) must still recognise "already pinned, unchanged" via the on-disk state file,
        not just within one call."""
        client = _FakeClient()
        channels = [TelegramChannel(name="intraday-orb", id=-100)]

        await strategy_cards.send_and_pin_cards(client, channels)
        second_call_pinned = await strategy_cards.send_and_pin_cards(_FakeClient(), channels)

        assert second_call_pinned == 0
