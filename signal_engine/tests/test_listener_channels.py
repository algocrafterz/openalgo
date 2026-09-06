"""Which channels the listener actually subscribes to.

`enabled: false` has to do two things, and the second is easy to forget: stop the engine
subscribing, AND still say out loud at startup that the channel exists and is off. A paper
-phase strategy that is silently absent from the logs is exactly the state this feature was
added to end — BREAKOUT traded nothing for a fortnight and no log line ever mentioned it.
"""

import pytest

from signal_engine.config import TelegramChannel
from signal_engine.listener import _channel_names, _split_by_enabled


class TestSubscriptionSplit:
    def test_splits_enabled_from_disabled_preserving_order(self):
        channels = (
            TelegramChannel(name="intraday-orb", id=-1003518225740),
            TelegramChannel(name="intraday-breakout", id=-1004450500772, enabled=False),
            TelegramChannel(name="smidestn", id=-1002773559154),
        )
        watching, skipped = _split_by_enabled(channels)
        assert [c.name for c in watching] == ["intraday-orb", "smidestn"]
        assert [c.name for c in skipped] == ["intraday-breakout"]

    def test_all_enabled_leaves_nothing_skipped(self):
        channels = (TelegramChannel(name="a", id=-1), TelegramChannel(name="b", id=-2))
        watching, skipped = _split_by_enabled(channels)
        assert len(watching) == 2
        assert skipped == ()

    def test_all_disabled_yields_no_subscriptions(self):
        """Distinct from an empty channels list, and must not be mistaken for it."""
        channels = (TelegramChannel(name="paper", id=-1, enabled=False),)
        watching, skipped = _split_by_enabled(channels)
        assert watching == ()
        assert [c.name for c in skipped] == ["paper"]

    def test_empty_config_splits_to_empty(self):
        assert _split_by_enabled(()) == ((), ())


class TestChannelNames:
    def test_disabled_channels_still_resolve_to_a_name(self):
        """A stray message from a disabled channel must log by name, not as a bare id."""
        channels = (
            TelegramChannel(name="intraday-orb", id=-1),
            TelegramChannel(name="intraday-breakout", id=-2, enabled=False),
        )
        names = _channel_names(channels)
        assert names[-2] == "intraday-breakout"
        assert names["-2"] == "intraday-breakout"
