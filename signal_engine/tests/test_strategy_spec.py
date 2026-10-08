"""The frozen BREAKOUT v1.0 specification as the one source of values (spec item 0.3, step S10)."""

import copy
import dataclasses
from types import SimpleNamespace

import pytest
import yaml

from signal_engine.backtest.strategies.breakout import Breakout
from signal_engine.config import RunnerStrategy, settings
from signal_engine.pipeline.service import STRATEGY_FACTORIES
from signal_engine.strategy_core import breakout as core
from signal_engine.strategy_core import strategy_spec as ss
from signal_engine.strategy_core.breakout import BreakoutParams


@pytest.fixture(scope="module")
def spec():
    return ss.load_spec()


class TestLoad:
    def test_the_shipped_file_is_v1_0(self, spec):
        assert (spec.id, spec.version) == ("BREAKOUT", "v1.0")
        assert len(spec.fingerprint) == 64

    def test_the_fingerprint_changes_with_any_edit(self, spec, tmp_path):
        body = copy.deepcopy(spec.body)
        body["core"]["score_threshold"] = 8
        edited = tmp_path / "strategy.yaml"
        edited.write_text(yaml.safe_dump({"strategy": body}))
        assert ss.load_spec(edited).fingerprint != spec.fingerprint

    @pytest.mark.parametrize("content", ["", "strategy: 3", "strategy: {id: X, version: v1}"])
    def test_a_malformed_file_is_refused(self, tmp_path, content):
        path = tmp_path / "strategy.yaml"
        path.write_text(content)
        with pytest.raises(ss.SpecError):
            ss.load_spec(path)

    def test_a_missing_file_is_refused(self, tmp_path):
        with pytest.raises(ss.SpecError):
            ss.load_spec(tmp_path / "nope.yaml")

    def test_the_stamp_names_the_three_identifiers(self, spec):
        assert set(spec.stamp()) == {"strategy_id", "strategy_version", "strategy_fingerprint"}


class TestCoreMatchesTheSpec:
    def test_the_code_defaults_equal_the_spec_values(self, spec):
        """BreakoutParams() is what the backtest adapter uses, so it must be the frozen spec."""
        assert BreakoutParams.from_spec(spec.core) == BreakoutParams()

    def test_every_spec_core_key_is_consumed(self, spec):
        fields = {f.name for f in dataclasses.fields(BreakoutParams)}
        unused = [k for k in spec.core if k not in fields and k not in core._SPEC_HANDLED]
        assert unused == []

    def test_a_key_the_core_cannot_honour_is_refused_not_ignored(self, spec):
        with pytest.raises(ValueError, match="invented_rule"):
            BreakoutParams.from_spec({**spec.core, "invented_rule": 1})

    @pytest.mark.parametrize("key", ["gap_filter", "nr_filter", "afternoon_window"])
    def test_an_excluded_filter_cannot_be_switched_on_by_the_spec(self, spec, key):
        with pytest.raises(ValueError, match=key):
            BreakoutParams.from_spec({**spec.core, key: {"enabled": True}})

    def test_the_break_bar_quirk_must_stay_frozen(self, spec):
        with pytest.raises(ValueError, match="retest_includes_break_bar"):
            BreakoutParams.from_spec({**spec.core, "retest_includes_break_bar": False})

    def test_the_stop_floor_must_match_the_core(self, spec):
        with pytest.raises(ValueError, match="stop floor"):
            BreakoutParams.from_spec({**spec.core, "sl_min_pct": 0.004})


class TestInputsMatchTheSpec:
    def test_the_input_builder_defaults_equal_the_spec_inputs_and_htf_minutes(self, spec):
        import inspect

        from signal_engine.strategy_core.inputs import build_breakout_inputs

        defaults = {
            k: v.default
            for k, v in inspect.signature(build_breakout_inputs).parameters.items()
            if v.default is not inspect.Parameter.empty
        }
        for key, value in {**spec.body["inputs"], "htf_minutes": spec.core["htf_minutes"]}.items():
            assert defaults[key] == value, key


class TestRunConfigMatchesTheSpec:
    def test_the_adapter_run_overrides_equal_the_spec_policy(self, spec):
        run = Breakout.run_overrides
        assert run["max_entries_per_direction"] == spec.policy["max_entries_per_direction_per_symbol_per_day"]
        assert run["max_entry_slippage_r"] == spec.policy["max_entry_slippage_r"]
        assert run["time_exit_at_open"] is (spec.exits["time_exit_fill"] == "bar_open")
        assert run["exit_plan"].tp_fill == (
            "bar_close" if spec.exits["tp_fill"] == "touching_bar_close" else "target"
        )

    def test_the_shipped_config_matches_the_spec(self, spec):
        entry = settings.runner.strategies["BREAKOUT-PY"]
        time_exit = (settings.time_exit_hour, settings.time_exit_minute)
        assert ss.check_runner(spec, "BREAKOUT-PY", entry, time_exit) == []


GOOD = RunnerStrategy("python", "tp1_only", "ladder", True, 2, "live", "BREAKOUT")


class TestStartupCheck:
    def test_a_matching_config_has_no_problems(self, spec):
        assert ss.check_runner(spec, "BREAKOUT-PY", GOOD, (14, 45)) == []

    @pytest.mark.parametrize(
        "entry,time_exit,needle",
        [
            (GOOD, (15, 0), "time_exit"),
            (RunnerStrategy("python", "tp1_only", "ladder", True, 3, "live", "BREAKOUT"), (14, 45), "max_entries"),
            (RunnerStrategy("python", "tp1_only", "ladder", True, None, "live", "BREAKOUT"), (14, 45), "max_entries"),
            (RunnerStrategy("python", "ladder_30_35_35", "ladder", True, 2, "live", "BREAKOUT"), (14, 45), "exit_plan"),
            (RunnerStrategy("python", "tp1_only", "tracker", True, 2, "live", "BREAKOUT"), (14, 45), "exit_owner"),
            (RunnerStrategy("python", "tp1_only", "ladder", True, 2, "mode", "BREAKOUT"), (14, 45), "paper_limits"),
            (RunnerStrategy("python", "tp1_only", "ladder", True, 2, "live", None), (14, 45), "supersedes_pine_tag"),
        ],
    )
    def test_each_difference_is_reported(self, spec, entry, time_exit, needle):
        problems = ss.check_runner(spec, "BREAKOUT-PY", entry, time_exit)
        assert any(needle in p for p in problems), problems

    @pytest.mark.asyncio
    async def test_start_up_refuses_on_a_difference_and_passes_on_a_match(self, monkeypatch):
        from signal_engine import startup

        sent = []

        async def fake_notify(**kw):
            sent.append(kw)

        monkeypatch.setattr(startup.notifier, "notify_startup_result", fake_notify)

        def fake_settings(entry):
            return SimpleNamespace(
                runner=SimpleNamespace(strategies={"BREAKOUT-PY": entry}),
                time_exit_hour=14, time_exit_minute=45,
            )

        monkeypatch.setattr(startup, "settings", fake_settings(GOOD))
        assert await startup._check_runner_against_spec() is True
        bad = RunnerStrategy("python", "tp1_only", "ladder", True, 5, "live", "BREAKOUT")
        monkeypatch.setattr(startup, "settings", fake_settings(bad))
        assert await startup._check_runner_against_spec() is False
        assert "max_entries" in sent[-1]["summary"]

    @pytest.mark.asyncio
    async def test_a_pine_only_config_needs_no_spec(self, monkeypatch):
        from signal_engine import startup

        pine = SimpleNamespace(runner=SimpleNamespace(strategies={"BREAKOUT": RunnerStrategy("pine", "tp1_only")}))
        monkeypatch.setattr(startup, "settings", pine)
        assert await startup._check_runner_against_spec() is True


class TestFactoryCarriesTheStamp:
    def test_breakout_py_uses_the_spec_params_and_stamps_its_signals(self, spec):
        strategy = STRATEGY_FACTORIES["BREAKOUT-PY"]()
        assert strategy.params == BreakoutParams()
        assert strategy.spec_stamp == spec.stamp()

    def test_the_pine_shadow_strategy_is_not_stamped(self):
        assert STRATEGY_FACTORIES["BREAKOUT"]().spec_stamp == {}
