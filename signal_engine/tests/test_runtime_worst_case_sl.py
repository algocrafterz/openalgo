"""runtime._worst_case_live_sl_pct(): the tightest min_sl_pct among tradeable strategies,
wired into RiskEngine's dynamic max_open_positions (see test_dynamic_position_limit.py).
"""

from unittest.mock import patch

from signal_engine.runtime import _worst_case_live_sl_pct
from signal_engine.strategies import StrategyMeta


class TestWorstCaseLiveSlPct:
    def test_picks_the_tightest_override_among_tradeable_strategies(self):
        fake_registry = {
            "WIDE": StrategyMeta("WIDE", "wide-channel", True),
            "TIGHT": StrategyMeta("TIGHT", "tight-channel", True),
            "NOT_TRADEABLE": StrategyMeta("NOT_TRADEABLE", None, False),
        }
        fake_profiles = {
            "WIDE": {"min_sl_pct": 0.010},
            "TIGHT": {"min_sl_pct": 0.002},
            "NOT_TRADEABLE": {"min_sl_pct": 0.0001},  # excluded: not tradeable
        }
        with patch("signal_engine.runtime.REGISTRY", fake_registry), \
             patch("signal_engine.runtime.settings") as mock_settings:
            mock_settings.strategy_profiles = fake_profiles
            mock_settings.min_sl_pct = 0.005
            assert _worst_case_live_sl_pct() == 0.002

    def test_falls_back_to_global_floor_when_no_override(self):
        fake_registry = {"ORB": StrategyMeta("ORB", "orb-channel", True)}
        with patch("signal_engine.runtime.REGISTRY", fake_registry), \
             patch("signal_engine.runtime.settings") as mock_settings:
            mock_settings.strategy_profiles = {}
            mock_settings.min_sl_pct = 0.005
            assert _worst_case_live_sl_pct() == 0.005

    def test_no_tradeable_strategies_falls_back_to_global_floor(self):
        fake_registry = {"X": StrategyMeta("X", None, False)}
        with patch("signal_engine.runtime.REGISTRY", fake_registry), \
             patch("signal_engine.runtime.settings") as mock_settings:
            mock_settings.strategy_profiles = {}
            mock_settings.min_sl_pct = 0.005
            assert _worst_case_live_sl_pct() == 0.005
