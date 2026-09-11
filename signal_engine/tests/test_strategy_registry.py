"""M10: one registry for a strategy, instead of six hand-maintained maps.

Adding a strategy meant touching config.yaml `telegram.channels`, `strategy_profiles` and
`blacklist`, plus notifier._STRATEGY_CHANNEL_BASE, strategy_cards.CARDS and (for a Python
strategy) alerts._CHANNEL_NAME_BY_GROUP. EMA9 and EMA9VWAP had the first three and none of
the last three, so nothing in the running system knew they existed — and nothing said whether
that was deliberate.
"""

import pytest

from signal_engine import strategies


class TestRegistryShape:
    def test_every_entry_is_keyed_by_its_own_tag(self):
        for tag, meta in strategies.REGISTRY.items():
            assert meta.tag == tag

    def test_tags_are_upper_case(self):
        assert all(tag == tag.upper() for tag in strategies.REGISTRY)

    def test_the_existing_tag_constants_are_all_registered(self):
        for tag in (strategies.ORB, strategies.BREAKOUT, strategies.BREAKINGTRADE,
                    strategies.BREAKINGTRADE_WATCHLIST, strategies.EMA9,
                    strategies.EMA9_VWAP, strategies.RSI_TP_MR):
            assert tag in strategies.REGISTRY

    def test_channel_bases_are_unique_among_strategies_that_have_one(self):
        bases = [m.channel_base for m in strategies.REGISTRY.values() if m.channel_base]
        assert len(bases) == len(set(bases))


class TestChannelBaseLookup:
    def test_returns_the_base_for_a_channelled_strategy(self):
        assert strategies.channel_base("ORB") == "intraday-orb"

    def test_is_case_insensitive(self):
        assert strategies.channel_base("orb") == "intraday-orb"

    def test_none_for_a_strategy_with_no_engine_channel(self):
        """EMA9VWAP is measured, not traded — it deliberately has no channel."""
        assert strategies.channel_base("EMA9VWAP") is None

    def test_none_for_an_unknown_tag(self):
        assert strategies.channel_base("NOPE") is None


class TestNotifierUsesTheRegistry:
    def test_per_strategy_summary_routing_matches_the_registry(self):
        from signal_engine import notifier

        for tag, meta in strategies.REGISTRY.items():
            if meta.channel_base:
                assert notifier._STRATEGY_CHANNEL_BASE.get(tag) == meta.channel_base


class TestCardsCoverEveryChannelledStrategy:
    def test_every_channel_base_has_a_reference_card(self):
        """A channelled strategy with no card pins nothing to its own channel — the gap the
        card mechanism exists to close."""
        from signal_engine import strategy_cards

        for meta in strategies.REGISTRY.values():
            if meta.channel_base:
                assert meta.channel_base in strategy_cards.CARDS, meta.tag
