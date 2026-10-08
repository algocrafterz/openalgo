"""The runner config section: fail-fast on every key, strict enums, safe defaults."""

import copy

import pytest
import yaml

from signal_engine.config import ConfigError, _build_settings, settings

from .test_config import RUNNER_CFG, TestValidConfigLoadsSuccessfully


def load(tmp_path, monkeypatch, mutate=None, drop_section=False):
    cfg = TestValidConfigLoadsSuccessfully()._full_config()
    cfg["runner"] = copy.deepcopy(RUNNER_CFG)
    if drop_section:
        del cfg["runner"]
    if mutate:
        mutate(cfg["runner"])
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(cfg))
    monkeypatch.setattr("signal_engine.config._YAML_PATH", str(path))
    monkeypatch.setattr("signal_engine.config._ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr("signal_engine.config._SECTORS_PATH", str(tmp_path / "sectors.yaml"))
    return _build_settings()


class TestRealConfig:
    def test_shipped_config_pine_breakout_is_untouched_and_python_is_paper_only(self):
        # The invariants that matter while BREAKOUT fails the acceptance gates: the Pine tag is
        # never Python-sourced or Ladder-managed, and every Python-sourced strategy is paper-only.
        pine = settings.runner.strategies.get("BREAKOUT")
        assert pine is None or (pine.signal_source, pine.exit_owner) == ("pine", "tracker")
        for tag, entry in settings.runner.strategies.items():
            if entry.signal_source == "python":
                assert entry.paper_only is True, tag

    def test_shipped_shadow_universe_is_an_explicit_unique_list(self):
        universe = settings.runner.universe
        assert isinstance(universe, tuple) and len(universe) == len(set(universe)) == 40
        assert "M&M" in universe
        assert "GVT&D" not in universe  # history endpoint returns HTTP 500 for it


class TestLoad:
    def test_values_are_loaded(self, tmp_path, monkeypatch):
        r = load(tmp_path, monkeypatch).runner
        assert r.enabled is False
        assert (r.bar_interval_minutes, r.bar_delay_seconds) == (5, 3.0)
        assert (r.cycle_deadline_seconds, r.retry_backoff_seconds) == (25.0, 2.0)
        assert (r.history_timeout_seconds, r.max_concurrent_requests) == (10.0, 8)
        assert r.warmup_sessions == 20 and r.universe == "fno_all"
        st = r.strategies["BREAKOUT"]
        assert (st.signal_source, st.exit_plan) == ("pine", "tp1_only")
        assert st.exit_owner == "tracker"

    def test_paper_only_is_required_and_a_bool(self, tmp_path, monkeypatch):
        with pytest.raises(ConfigError, match="paper_only"):
            load(tmp_path, monkeypatch, lambda c: c["strategies"]["BREAKOUT"].pop("paper_only"))

        def mut(c):
            c["strategies"]["BREAKOUT"]["paper_only"] = "yes"

        with pytest.raises(ConfigError, match="paper_only"):
            load(tmp_path, monkeypatch, mut)

    def test_paper_only_is_loaded(self, tmp_path, monkeypatch):
        r = load(tmp_path, monkeypatch).runner
        assert r.strategies["BREAKOUT"].paper_only is True

    def test_exit_owner_is_required(self, tmp_path, monkeypatch):
        with pytest.raises(ConfigError, match="exit_owner"):
            load(tmp_path, monkeypatch, lambda c: c["strategies"]["BREAKOUT"].pop("exit_owner"))

    def test_bad_exit_owner(self, tmp_path, monkeypatch):
        def mut(c):
            c["strategies"]["BREAKOUT"]["exit_owner"] = "both"

        with pytest.raises(ConfigError, match="exit_owner"):
            load(tmp_path, monkeypatch, mut)

    def test_entry_cap_is_required(self, tmp_path, monkeypatch):
        with pytest.raises(ConfigError, match="max_entries_per_direction_per_day"):
            load(
                tmp_path,
                monkeypatch,
                lambda c: c["strategies"]["BREAKOUT"].pop("max_entries_per_direction_per_day"),
            )

    @pytest.mark.parametrize("bad", [0, -1, 1.5, "2", True])
    def test_entry_cap_must_be_a_whole_number_of_at_least_one_or_null(self, tmp_path, monkeypatch, bad):
        def mut(c):
            c["strategies"]["BREAKOUT"]["max_entries_per_direction_per_day"] = bad

        with pytest.raises(ConfigError, match="max_entries_per_direction_per_day"):
            load(tmp_path, monkeypatch, mut)

    def test_entry_cap_is_loaded_and_null_means_no_cap(self, tmp_path, monkeypatch):
        def mut(c):
            c["strategies"]["BREAKOUT"]["max_entries_per_direction_per_day"] = 2

        assert load(tmp_path, monkeypatch, mut).runner.strategies["BREAKOUT"].max_entries_per_direction_per_day == 2
        assert load(tmp_path, monkeypatch).runner.strategies["BREAKOUT"].max_entries_per_direction_per_day is None

    def test_paper_limits_is_required(self, tmp_path, monkeypatch):
        with pytest.raises(ConfigError, match="paper_limits"):
            load(tmp_path, monkeypatch, lambda c: c["strategies"]["BREAKOUT"].pop("paper_limits"))

    def test_paper_limits_must_be_mode_or_live(self, tmp_path, monkeypatch):
        def mut(c):
            c["strategies"]["BREAKOUT"]["paper_limits"] = "pooled"

        with pytest.raises(ConfigError, match="paper_limits"):
            load(tmp_path, monkeypatch, mut)

    def test_paper_limits_is_loaded(self, tmp_path, monkeypatch):
        def mut(c):
            c["strategies"]["BREAKOUT"]["paper_limits"] = "live"

        assert load(tmp_path, monkeypatch, mut).runner.strategies["BREAKOUT"].paper_limits == "live"

    def test_supersedes_pine_tag_is_required(self, tmp_path, monkeypatch):
        with pytest.raises(ConfigError, match="supersedes_pine_tag"):
            load(tmp_path, monkeypatch, lambda c: c["strategies"]["BREAKOUT"].pop("supersedes_pine_tag"))

    @pytest.mark.parametrize("bad", ["", "  ", 3, "BREAKOUT"])
    def test_supersedes_pine_tag_must_be_another_tag_or_null(self, tmp_path, monkeypatch, bad):
        def mut(c):
            c["strategies"]["BREAKOUT"]["supersedes_pine_tag"] = bad

        with pytest.raises(ConfigError, match="supersedes_pine_tag"):
            load(tmp_path, monkeypatch, mut)

    def test_supersedes_pine_tag_is_loaded_upper_case(self, tmp_path, monkeypatch):
        def mut(c):
            c["strategies"]["BREAKOUT"]["supersedes_pine_tag"] = "orb"

        assert load(tmp_path, monkeypatch, mut).runner.strategies["BREAKOUT"].supersedes_pine_tag == "ORB"

    def test_shipped_breakout_py_supersedes_pine_breakout(self):
        assert settings.runner.strategies["BREAKOUT-PY"].supersedes_pine_tag == "BREAKOUT"

    def test_shipped_breakout_py_uses_the_live_limits_in_paper(self):
        assert settings.runner.strategies["BREAKOUT-PY"].paper_limits == "live"

    def test_shipped_breakout_py_cap_is_two(self):
        assert settings.runner.strategies["BREAKOUT-PY"].max_entries_per_direction_per_day == 2

    def test_explicit_universe_list(self, tmp_path, monkeypatch):
        r = load(tmp_path, monkeypatch, lambda c: c.update(universe=["TCS", "INFY"])).runner
        assert r.universe == ("TCS", "INFY")

    @pytest.mark.parametrize("key", list(RUNNER_CFG))
    def test_every_key_is_required(self, tmp_path, monkeypatch, key):
        with pytest.raises(ConfigError, match=f"runner.{key}"):
            load(tmp_path, monkeypatch, lambda c: c.pop(key))

    def test_section_is_required(self, tmp_path, monkeypatch):
        with pytest.raises(ConfigError, match="runner"):
            load(tmp_path, monkeypatch, drop_section=True)

    def test_bad_signal_source(self, tmp_path, monkeypatch):
        def mut(c):
            c["strategies"]["BREAKOUT"]["signal_source"] = "both"

        with pytest.raises(ConfigError, match="signal_source"):
            load(tmp_path, monkeypatch, mut)

    def test_bad_exit_plan(self, tmp_path, monkeypatch):
        def mut(c):
            c["strategies"]["BREAKOUT"]["exit_plan"] = "moon"

        with pytest.raises(ConfigError, match="exit_plan"):
            load(tmp_path, monkeypatch, mut)

    def test_strategy_keys_are_required(self, tmp_path, monkeypatch):
        def mut(c):
            del c["strategies"]["BREAKOUT"]["exit_plan"]

        with pytest.raises(ConfigError, match="exit_plan"):
            load(tmp_path, monkeypatch, mut)

    @pytest.mark.parametrize(
        "key,value",
        [
            ("bar_interval_minutes", 0),
            ("max_concurrent_requests", 0),
            ("warmup_sessions", 0),
            ("cycle_deadline_seconds", 0),
            ("universe", "everything"),
            ("universe", []),
            ("enabled", "yes"),
        ],
    )
    def test_invalid_values_are_rejected(self, tmp_path, monkeypatch, key, value):
        with pytest.raises(ConfigError, match=key):
            load(tmp_path, monkeypatch, lambda c: c.update({key: value}))

    def test_strategies_may_be_empty_only_as_a_mapping(self, tmp_path, monkeypatch):
        with pytest.raises(ConfigError, match="strategies"):
            load(tmp_path, monkeypatch, lambda c: c.update(strategies=["BREAKOUT"]))
