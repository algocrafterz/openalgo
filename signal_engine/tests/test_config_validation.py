"""M10: config.yaml gaps that used to be silent are now startup failures or warnings.

Nothing validated that channel names were unique, that ids were unique, or that a strategy
with an -analyze channel also had the -live one it will need on promotion. A duplicated name
silently made _channel_for_strategy() pick the first; a duplicated id double-subscribed.
"""

from signal_engine.config import TelegramChannel, validate_channels


def _ch(name, cid, enabled=True):
    return TelegramChannel(name=name, id=cid, enabled=enabled)


class TestDuplicateDetection:
    def test_duplicate_names_are_reported(self):
        problems = validate_channels((_ch("intraday-orb-analyze", -1),
                                      _ch("intraday-orb-analyze", -2)))
        assert any("duplicate channel name" in p.lower() for p in problems)

    def test_duplicate_ids_are_reported(self):
        problems = validate_channels((_ch("a-analyze", -1), _ch("b-analyze", -1)))
        assert any("duplicate channel id" in p.lower() for p in problems)

    def test_a_clean_config_reports_nothing(self):
        problems = validate_channels((_ch("intraday-orb-analyze", -1),
                                      _ch("intraday-orb-live", -2, enabled=False)))
        assert problems == []


class TestPhasePairing:
    def test_an_analyze_channel_without_its_live_twin_is_reported(self):
        problems = validate_channels((_ch("intraday-orb-analyze", -1),))
        assert any("intraday-orb-live" in p for p in problems)

    def test_a_live_channel_without_its_analyze_twin_is_reported(self):
        problems = validate_channels((_ch("intraday-orb-live", -1),))
        assert any("intraday-orb-analyze" in p for p in problems)

    def test_an_unsuffixed_channel_needs_no_twin(self):
        assert validate_channels((_ch("smidestn", -1),)) == []


class TestBothPhasesEnabledIsRefused:
    def test_a_strategy_enabled_on_both_phases_is_reported(self):
        """Only one phase can ever be the live one. Both enabled means whichever phase
        OpenAlgo is in trades, and the other's alerts are refused - almost certainly a
        half-finished promotion rather than an intent."""
        problems = validate_channels((_ch("intraday-orb-analyze", -1),
                                      _ch("intraday-orb-live", -2)))
        assert any("both phases enabled" in p.lower() for p in problems)
