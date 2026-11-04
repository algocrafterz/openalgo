"""ORB-PY wiring: the pipeline strategy, the TP1/TP1.5 exit driver and the config rules."""

import copy
import datetime as dt

import pandas as pd
import pytest
import yaml

from signal_engine.config import ConfigError, _build_settings, settings
from signal_engine.models import Direction
from signal_engine.pipeline import single_source
from signal_engine.pipeline.exit_driver import ExitDriver, TargetExitDriver, plan_for
from signal_engine.pipeline.service import STRATEGY_FACTORIES, build_exits
from signal_engine.pipeline.signals import fire_to_signal
from signal_engine.pipeline.strategies.orb import TAG, OrbStrategy
from signal_engine.strategies import REGISTRY
from signal_engine.timeutils import IST
from signal_engine.tracker import TrackedPosition

from .test_config import RUNNER_CFG, TestValidConfigLoadsSuccessfully
from .test_orb_core import DAY, _breakout_day, _frame


def ts(h, m):
    return pd.Timestamp(dt.datetime(2026, 10, 14, h, m), tz=IST)


class TestStrategyAdapter:
    def test_fire_carries_the_pine_alert_levels(self):
        fires = OrbStrategy().evaluate("SBIN", _frame(_breakout_day()), pd.DataFrame())
        assert len(fires) == 1
        f = fires[0]
        assert f.tag == "ORB15-LONG" and f.direction == 1
        assert f.entry_ref == pytest.approx(101.9)  # the entry bar's open, as the Pine printed it
        assert len(f.targets) == 4
        assert f.context["tp1_5"] == f.targets[1] and f.context["tp3"] == f.targets[3]

    def test_signal_takes_tp1_as_tp_and_keeps_tp1_5_in_context(self):
        f = OrbStrategy().evaluate("SBIN", _frame(_breakout_day()), pd.DataFrame())[0]
        sig = fire_to_signal(f, "MIS", strategy=TAG)
        assert sig.strategy == "ORB-PY" and sig.tp == f.targets[0]
        assert sig.context["tp1_5"] == f.targets[1]
        assert sig.sig_id.startswith("ORB-PY-SBIN-")

    def test_trigger_family_names_no_key_level(self):
        # main.structural_runner_sl must fall back to the TP1 buffer rule, as for a Pine ORB alert
        from signal_engine.main import _LEVEL_CONTEXT_KEYS

        assert "ORB15" not in _LEVEL_CONTEXT_KEYS

    def test_entry_window_matches_the_pine_gates(self):
        assert OrbStrategy().entry_window_min == (9 * 60 + 45, 11 * 60)

    def test_registered_as_a_paper_only_python_strategy(self):
        assert STRATEGY_FACTORIES[TAG] is OrbStrategy
        assert REGISTRY[TAG].tradeable is False and REGISTRY[TAG].channel_base is None


class FakePositions:
    def __init__(self, pos):
        self.pos = pos

    def find_position(self, symbol, strategy):
        return self.pos if (symbol, strategy) == (self.pos.symbol, self.pos.strategy) else None


class FakeBroker:
    async def __call__(self, pos):
        return 0


def _pos(**kw):
    base = {
        "symbol": "SBIN",
        "strategy": "ORB-PY",
        "exchange": "NSE",
        "product": "MIS",
        "entry_price": 100.0,
        "quantity": 100,
        "sl": 98.0,
        "tp": 101.6,
        "direction": Direction.LONG,
        "fill_price": 100.0,
        "original_quantity": 100,
        "entry_time": dt.datetime(2026, 10, 14, 9, 50, 3, tzinfo=IST),
        "context": {"tp1_5": 102.4, "tp2": 103.2, "tp3": 104.8},
        "sig_id": "ORB-PY-SBIN-20261014-0945",
    }
    base.update(kw)
    return TrackedPosition(**base)


def _driver(pos):
    return TargetExitDriver(
        strategy="ORB-PY",
        plan=plan_for("orb_tp1_tp15"),
        positions=FakePositions(pos),
        broker_qty=FakeBroker(),
        interval_min=5,
        safety_bars=2,
    )


@pytest.mark.asyncio
class TestTargetExitDriver:
    async def test_tp1_sells_half_with_the_pine_label_and_no_stop_override(self):
        d = _driver(_pos())
        out = await d.on_bar("SBIN", ts(10, 0), 100.5, 101.7, 100.4, 101.65)
        assert [(s.tp_level, s.exit_qty_pct, s.direction) for s in out] == [
            ("TP1", 0.5, Direction.EXIT)
        ]
        assert "ladder_sl" not in out[0].context  # the tracker's runner-stop rule decides the stop

    async def test_tp1_5_closes_the_rest_and_is_labelled_tp1_5(self):
        d = _driver(_pos())
        await d.on_bar("SBIN", ts(10, 0), 100.5, 101.7, 100.4, 101.65)
        out = await d.on_bar("SBIN", ts(10, 5), 101.6, 102.6, 101.5, 102.5)
        assert [(s.tp_level, s.exit_qty_pct) for s in out] == [("TP1.5", 1.0)]

    async def test_both_targets_in_one_bar_send_both_exits(self):
        d = _driver(_pos())
        out = await d.on_bar("SBIN", ts(10, 0), 100.5, 102.6, 100.4, 102.5)
        assert [(s.tp_level, s.exit_qty_pct) for s in out] == [("TP1", 0.5), ("TP1.5", 1.0)]

    async def test_short_trade_uses_downward_targets(self):
        pos = _pos(direction=Direction.SHORT, sl=102.0, tp=98.4, context={"tp1_5": 97.6})
        out = await _driver(pos).on_bar("SBIN", ts(10, 0), 99.0, 99.2, 98.3, 98.4)
        assert [s.tp_level for s in out] == ["TP1"]

    async def test_no_exit_while_neither_target_is_touched(self):
        assert await _driver(_pos()).on_bar("SBIN", ts(10, 0), 100.5, 101.0, 100.2, 100.8) == []

    async def test_a_position_without_tp1_5_is_left_to_the_tracker(self):
        d = _driver(_pos(context={}))
        assert await d.on_bar("SBIN", ts(10, 0), 100.5, 101.7, 100.4, 101.65) == []
        assert "SBIN" in d.unmanaged

    async def test_a_bar_that_closed_before_the_entry_is_ignored(self):
        assert await _driver(_pos()).on_bar("SBIN", ts(9, 40), 100.5, 105.0, 100.0, 104.0) == []

    async def test_a_stop_touch_sends_nothing_until_the_broker_check(self):
        out = await _driver(_pos()).on_bar("SBIN", ts(10, 0), 100.0, 100.5, 97.9, 98.5)
        assert out == []


class TestExitPlanAndOwner:
    def test_plan_is_half_then_the_rest_at_market_after_the_bar_closes(self):
        plan = plan_for("orb_tp1_tp15")
        assert (
            plan.fractions == (0.5, 0.5)
            and plan.stop_rule == "none"
            and plan.tp_fill == "bar_close"
        )

    def test_plain_exit_driver_still_reads_tp2_and_tp3(self):
        assert ExitDriver.EXTRA_TARGET_KEYS == ("tp2", "tp3")

    def test_shipped_config_runs_orb_py_paper_only_with_tracker_exits(self):
        e = settings.runner.strategies["ORB-PY"]
        assert (e.signal_source, e.exit_plan, e.exit_owner) == ("python", "orb_tp1_tp15", "targets")
        assert e.paper_only is True and e.supersedes_pine_tag is None
        assert len(e.universe) == 24 and "BHEL" not in e.universe

    def test_the_tracker_keeps_its_exits_for_orb_py(self):
        from signal_engine.tracker import _ladder_owns_exits

        assert _ladder_owns_exits(settings, "ORB-PY") is False
        assert _ladder_owns_exits(settings, "EVWAP-EARLY-PY") is True

    def test_build_exits_returns_the_target_driver(self):
        d = build_exits(settings.runner, "ORB-PY", FakePositions(_pos()), FakeBroker())
        assert isinstance(d, TargetExitDriver)

    def test_pine_orb_entries_are_not_superseded_so_both_orbs_trade(self):
        assert single_source.pine_superseded(settings.runner, "ORB", "SBIN") is False
        assert single_source.pine_superseded(settings.runner, "ORB", "ZOMATO") is False


def _load(tmp_path, monkeypatch, mutate):
    cfg = TestValidConfigLoadsSuccessfully()._full_config()
    cfg["runner"] = copy.deepcopy(RUNNER_CFG)
    mutate(cfg["runner"])
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(cfg))
    monkeypatch.setattr("signal_engine.config._YAML_PATH", str(path))
    monkeypatch.setattr("signal_engine.config._ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr("signal_engine.config._SECTORS_PATH", str(tmp_path / "sectors.yaml"))
    return _build_settings()


class TestConfigRules:
    def test_target_plan_and_owner_go_together(self, tmp_path, monkeypatch):
        def mutate(c):
            c["strategies"]["BREAKOUT"]["exit_plan"] = "orb_tp1_tp15"

        with pytest.raises(ConfigError, match="orb_tp1_tp15"):
            _load(tmp_path, monkeypatch, mutate)

    def test_targets_owner_needs_a_python_source(self):
        cfg = copy.deepcopy(settings.runner)
        entry = cfg.strategies["ORB-PY"]
        bad = type(entry)(**{**entry.__dict__, "signal_source": "pine"})
        cfg = type(cfg)(**{**cfg.__dict__, "strategies": {"ORB-PY": bad}})
        with pytest.raises(single_source.SingleSourceError, match="exit_owner targets"):
            single_source.check_consistency(cfg, ["ORB-PY"])
