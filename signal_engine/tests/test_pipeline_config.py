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
    def test_shipped_config_is_shadow_only_breakout_stays_on_pine(self):
        # Enabled since 2026-09-30 for the shadow run. The invariant that matters: while BREAKOUT
        # fails the acceptance gates it must stay on Pine/tracker, so the runner only records.
        entry = settings.runner.strategies["BREAKOUT"]
        assert (entry.signal_source, entry.exit_owner) == ("pine", "tracker")

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

    def test_exit_owner_is_required(self, tmp_path, monkeypatch):
        with pytest.raises(ConfigError, match="exit_owner"):
            load(tmp_path, monkeypatch, lambda c: c["strategies"]["BREAKOUT"].pop("exit_owner"))

    def test_bad_exit_owner(self, tmp_path, monkeypatch):
        def mut(c):
            c["strategies"]["BREAKOUT"]["exit_owner"] = "both"

        with pytest.raises(ConfigError, match="exit_owner"):
            load(tmp_path, monkeypatch, mut)

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
