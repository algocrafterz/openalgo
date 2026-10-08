"""EVWAP-EARLY-PY wiring: config, shared bar fetch, exit-driver selection, tracker ownership."""

import asyncio
import datetime as dt
from types import SimpleNamespace

import pandas as pd
import pytest

from signal_engine.config import ConfigError, RunnerStrategy, settings
from signal_engine.pipeline import service, single_source
from signal_engine.pipeline.bars import SharedBarSource
from signal_engine.pipeline.exit_driver import ExitDriver
from signal_engine.pipeline.strategies.ema_vwap_early import TAG, EarlyDisplacementStrategy
from signal_engine.pipeline.trail_driver import TrailExitDriver
from signal_engine.strategies import REGISTRY
from signal_engine.timeutils import IST
from signal_engine.tracker import _ladder_owns_exits

from .test_pipeline_config import load

TRAIL = {
    "signal_source": "python",
    "exit_plan": "trailing_stop",
    "exit_owner": "strategy",
    "paper_only": True,
    "supersedes_pine_tag": None,
    "paper_limits": "mode",
    "max_entries_per_direction_per_day": None,
}


def with_trail(**over):
    def mutate(runner):
        runner["strategies"][TAG] = {**TRAIL, **over}

    return mutate


class TestShippedConfig:
    def test_registered_paper_only_python_with_strategy_owned_exits(self):
        entry = settings.runner.strategies[TAG]
        assert (entry.signal_source, entry.exit_plan, entry.exit_owner) == (
            "python", "trailing_stop", "strategy",
        )
        assert entry.paper_only is True and entry.supersedes_pine_tag is None
        assert entry.universe is None  # shares runner.universe with BREAKOUT-PY
        assert TAG in REGISTRY and REGISTRY[TAG].tradeable is False
        assert TAG in service.STRATEGY_FACTORIES and TAG not in service.SPEC_BOUND_TAGS
        single_source.check_consistency(settings.runner, REGISTRY.keys())

    def test_profile_has_no_tp_levels(self):
        profile = settings.strategy_profiles[TAG]
        assert profile["product"] == "MIS" and not profile.get("tp_levels")


class TestConfigValidation:
    def test_trailing_plan_loads(self, tmp_path, monkeypatch):
        entry = load(tmp_path, monkeypatch, with_trail()).runner.strategies[TAG]
        assert entry.exit_owner == "strategy" and entry.universe is None

    @pytest.mark.parametrize(
        "over", [{"exit_owner": "ladder"}, {"exit_plan": "tp1_only"}], ids=["owner", "plan"]
    )
    def test_trailing_plan_and_strategy_owner_go_together(self, tmp_path, monkeypatch, over):
        with pytest.raises(ConfigError, match="trailing_stop"):
            load(tmp_path, monkeypatch, with_trail(**over))

    def test_per_strategy_universe(self, tmp_path, monkeypatch):
        r = load(tmp_path, monkeypatch, with_trail(universe=["TCS", "INFY"])).runner
        assert r.strategies[TAG].universe == ("TCS", "INFY")
        assert service.strategy_symbols(r, TAG, ["SBIN"]) == ["TCS", "INFY"]
        assert service.strategy_symbols(r, "BREAKOUT", ["SBIN"]) == ["SBIN"]

    def test_bad_universe_is_rejected(self, tmp_path, monkeypatch):
        with pytest.raises(ConfigError, match="universe"):
            load(tmp_path, monkeypatch, with_trail(universe="fno_all"))


class TestExitOwnership:
    def test_tracker_stands_down_for_strategy_owned_exits(self):
        assert _ladder_owns_exits(settings, TAG) is True

    def test_build_exits_picks_the_trail_driver(self):
        cfg = SimpleNamespace(
            strategies={TAG: RunnerStrategy("python", "trailing_stop", "strategy")},
            bar_interval_minutes=5,
        )
        strategy = EarlyDisplacementStrategy()
        driver = service.build_exits(cfg, TAG, positions=None, broker_qty=None,
                                     strategy=strategy, context=object())
        assert isinstance(driver, TrailExitDriver) and driver.strategy == TAG
        with pytest.raises(service.RunnerStartError):
            service.build_exits(cfg, TAG, positions=None, broker_qty=None)

    def test_ladder_strategies_still_get_the_ladder(self):
        cfg = SimpleNamespace(
            strategies={"BREAKOUT-PY": RunnerStrategy("python", "tp1_only", "ladder")},
            bar_interval_minutes=5,
        )
        assert isinstance(service.build_exits(cfg, "BREAKOUT-PY", None, None), ExitDriver)

    def test_pine_cannot_be_strategy_owned(self):
        cfg = SimpleNamespace(
            enabled=True, universe=("TCS",),
            strategies={TAG: RunnerStrategy("pine", "trailing_stop", "strategy")},
        )
        with pytest.raises(single_source.SingleSourceError, match="exit_owner strategy"):
            single_source.check_consistency(cfg, [TAG])


class CountingSource:
    def __init__(self, frames):
        self.frames = frames
        self.calls: list[list[str]] = []

    async def fetch(self, symbols, interval_min, day, now):
        self.calls.append(list(symbols))
        return {s: self.frames[s] for s in symbols}


def bars(last_open: str) -> pd.DataFrame:
    idx = pd.date_range("2026-10-09 09:15", f"2026-10-09 {last_open}", freq="5min", tz=IST)
    return pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1.0}, index=idx)


class TestSharedBarSource:
    def test_two_sessions_share_one_fetch_per_bar(self):
        inner = CountingSource({"TCS": bars("10:00"), "INFY": bars("10:00")})
        shared = SharedBarSource(inner)
        day, now = dt.date(2026, 10, 9), pd.Timestamp("2026-10-09 10:05:03", tz=IST)

        async def both():
            return await asyncio.gather(
                shared.fetch(["TCS", "INFY"], 5, day, now), shared.fetch(["TCS", "INFY"], 5, day, now)
            )

        a, b = asyncio.run(both())
        assert inner.calls == [["TCS", "INFY"]]
        assert a["TCS"].index[-1] == b["TCS"].index[-1] == pd.Timestamp("2026-10-09 10:00", tz=IST)

    def test_refetches_when_a_newer_bar_can_have_closed(self):
        inner = CountingSource({"TCS": bars("10:00")})
        shared = SharedBarSource(inner)
        day = dt.date(2026, 10, 9)
        asyncio.run(shared.fetch(["TCS"], 5, day, pd.Timestamp("2026-10-09 10:05:03", tz=IST)))
        asyncio.run(shared.fetch(["TCS"], 5, day, pd.Timestamp("2026-10-09 10:10:03", tz=IST)))
        assert len(inner.calls) == 2

    def test_late_bar_is_not_served_from_cache(self):
        inner = CountingSource({"TCS": bars("09:55")})  # 10:00 bar not yet published
        shared = SharedBarSource(inner)
        day, now = dt.date(2026, 10, 9), pd.Timestamp("2026-10-09 10:05:03", tz=IST)
        asyncio.run(shared.fetch(["TCS"], 5, day, now))
        asyncio.run(shared.fetch(["TCS"], 5, day, now))
        assert len(inner.calls) == 2


class TestNoTakeProfitInvariant:
    @staticmethod
    def signal(strategy: str, tp: float):
        from signal_engine.models import Direction, Signal

        return Signal(strategy=strategy, direction=Direction.LONG, symbol="TCS", entry=100.0,
                      sl=98.0, tp=tp, exchange="NSE", product="MIS")

    def test_validator_accepts_tp_none_only_for_a_no_target_strategy(self):
        from signal_engine.config import has_no_take_profit
        from signal_engine.pipeline import pretrade

        assert has_no_take_profit(TAG) and not has_no_take_profit("BREAKOUT-PY")
        assert pretrade.check(self.signal(TAG, 0.0)) is None
        # a target can never sneak into a no-target strategy
        assert "TP must be 0" in pretrade.check(self.signal(TAG, 140.0)).reason
        assert "TP must be 0" in pretrade.check(self.signal(TAG, 100.5)).reason
        # every other strategy still needs a real target
        assert "TP must be positive" in pretrade.check(self.signal("BREAKOUT-PY", 0.0)).reason

    def test_bad_fill_has_no_target_to_overshoot(self):
        from signal_engine.strategy_core.trade_policy import TP_OVERSHOT, bad_fill

        assert bad_fill(1, 100.0, 98.0, 0.0, 100.2, 0.5).violated is False
        assert bad_fill(1, 100.0, 98.0, float("nan"), 100.2, 0.5).violated is False
        assert bad_fill(1, 100.0, 98.0, 100.1, 100.2, 0.5).kind == TP_OVERSHOT  # unchanged with a target

    def test_fire_without_targets_needs_the_explicit_marker(self):
        from signal_engine.pipeline.signals import fire_to_signal
        from signal_engine.pipeline.strategy import Fire

        ts = pd.Timestamp("2026-10-09 10:00", tz=IST)
        bare = Fire("TCS", ts, 1, 100.0, 98.0, (), "X")
        with pytest.raises(ValueError, match="no targets"):
            fire_to_signal(bare, "MIS")
        both = Fire("TCS", ts, 1, 100.0, 98.0, (104.0,), "X", {"take_profit": "none"})
        with pytest.raises(ValueError, match="carries targets"):
            fire_to_signal(both, "MIS")
        ok = Fire("TCS", ts, 1, 100.0, 98.0, (), "X", {"take_profit": "none"})
        assert fire_to_signal(ok, "MIS", strategy=TAG).tp == 0.0


class TestTakeProfitNoneIsConsistent:
    """spec, config, backtest adapter, paper strategy, trail driver and validator all say: no target."""

    def test_every_layer_encodes_take_profit_none(self):
        from pathlib import Path

        import numpy as np
        import yaml

        from signal_engine.backtest.strategies.ema_vwap_early import EmaVwapEarly
        from signal_engine.config import NO_TARGET_EXIT_PLANS
        from signal_engine.pipeline import pretrade
        from signal_engine.strategy_core import ema_vwap_early as core

        spec = yaml.safe_load(
            (Path(__file__).resolve().parents[1]
             / "research/ideas/idea-033-ema-vwap-early-trail/spec.yaml").read_text()
        )
        assert spec["take_profit"] == core.TAKE_PROFIT == "none"
        assert settings.runner.strategies[TAG].exit_plan in NO_TARGET_EXIT_PLANS
        assert "tp_levels" not in settings.strategy_profiles[TAG] or not settings.strategy_profiles[TAG]["tp_levels"]
        assert core.PAPER_TP_NONE == 0.0 and np.isnan(core.BACKTEST_TP_NONE)
        # paper: the validator takes only the none value for this strategy
        assert pretrade.check(TestNoTakeProfitInvariant.signal(TAG, core.PAPER_TP_NONE)) is None
        assert pretrade.check(TestNoTakeProfitInvariant.signal(TAG, 103.0)) is not None
        # backtest: the adapter's entry carries the none value
        import inspect

        assert "BACKTEST_TP_NONE" in inspect.getsource(EmaVwapEarly.entry)
