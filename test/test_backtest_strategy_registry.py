"""Tests for strategy registry - dynamic strategy loading by name."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from backtest.strategies import get_strategy, list_strategies, StrategyNotFoundError
from backtest.strategies.base import Strategy
from backtest.strategies.orb import ORBConfig, ORBStrategy


class TestGetStrategy:
    def test_loads_orb_strategy(self):
        config = ORBConfig(orb_minutes=15, tp_multiplier=1.0)
        strategy = get_strategy("orb", orb_config=config)
        assert isinstance(strategy, ORBStrategy)
        assert strategy.config.tp_multiplier == 1.0

    def test_orb_default_config(self):
        strategy = get_strategy("orb")
        assert isinstance(strategy, ORBStrategy)
        assert strategy.config.orb_minutes == 15

    def test_unknown_strategy_raises(self):
        with pytest.raises(StrategyNotFoundError, match="unknown_strat"):
            get_strategy("unknown_strat")

    def test_case_insensitive(self):
        strategy = get_strategy("ORB")
        assert isinstance(strategy, ORBStrategy)

    def test_returns_strategy_base_class(self):
        strategy = get_strategy("orb")
        assert isinstance(strategy, Strategy)


class TestListStrategies:
    def test_orb_in_list(self):
        strategies = list_strategies()
        assert "orb" in strategies

    def test_returns_dict_with_descriptions(self):
        strategies = list_strategies()
        assert isinstance(strategies, dict)
        assert isinstance(strategies["orb"], str)
        assert len(strategies["orb"]) > 0
