"""Per-mode risk limits, so paper and live settings can never be confused.

One config file served both modes, which meant the paper week either ran with live limits
(losing the outcome of every signal those limits refused) or someone edited the live numbers
to loosen them and had to remember to put them back. Both are bad; the second is dangerous.

`mode_profiles.live` and `mode_profiles.analyze` now state each mode's limits explicitly, and
the engine picks the profile matching whatever OpenAlgo reports at startup.
"""

import pytest

from signal_engine.config import _parse_mode_profiles, resolve_mode_profile
from signal_engine.risk_store import RiskStore
from signal_engine.runtime import apply_trade_mode, build_risk_engine


class TestProfileResolution:
    def test_live_profile_overrides_the_base_values(self):
        base = {"max_open_positions": 2, "max_trades_per_day": 10, "daily_loss_limit": 0.04}
        profiles = {"live": {"max_open_positions": 3}, "analyze": {"max_open_positions": 6}}
        assert resolve_mode_profile(base, profiles, "live")["max_open_positions"] == 3

    def test_analyze_profile_overrides_the_base_values(self):
        base = {"max_open_positions": 2, "daily_loss_limit": 0.04}
        profiles = {"live": {}, "analyze": {"max_open_positions": 6, "daily_loss_limit": 1.0}}
        resolved = resolve_mode_profile(base, profiles, "analyze")
        assert resolved["max_open_positions"] == 6
        assert resolved["daily_loss_limit"] == 1.0

    def test_keys_absent_from_the_profile_fall_through_to_base(self):
        base = {"max_open_positions": 2, "max_trades_per_day": 10}
        profiles = {"analyze": {"max_open_positions": 6}}
        resolved = resolve_mode_profile(base, profiles, "analyze")
        assert resolved["max_trades_per_day"] == 10

    def test_no_profiles_at_all_leaves_base_untouched(self):
        """Every pre-existing config has no mode_profiles block and must keep working."""
        base = {"max_open_positions": 2}
        assert resolve_mode_profile(base, {}, "analyze") == base

    def test_base_is_not_mutated(self):
        base = {"max_open_positions": 2}
        resolve_mode_profile(base, {"analyze": {"max_open_positions": 6}}, "analyze")
        assert base["max_open_positions"] == 2

    def test_unknown_mode_falls_back_to_base_rather_than_guessing(self):
        base = {"max_open_positions": 2}
        profiles = {"live": {"max_open_positions": 3}}
        assert resolve_mode_profile(base, profiles, "sandbox")["max_open_positions"] == 2


class TestApplyTradeMode:
    def _engine(self, tmp_path):
        store = RiskStore(db_path=str(tmp_path / "risk.db"))
        return build_risk_engine(store, trade_mode="live"), store

    def test_switching_to_analyze_applies_the_analyze_limits(self, tmp_path, monkeypatch):
        engine, _ = self._engine(tmp_path)
        monkeypatch.setattr(
            "signal_engine.runtime.settings",
            type("S", (), {"mode_profiles": {"analyze": {"max_open_positions": 6}}})(),
        )
        apply_trade_mode(engine, "analyze")
        assert engine.max_open_positions == 6

    def test_counters_are_repointed_at_the_mode_row(self, tmp_path, monkeypatch):
        """risk_store isolates live from analyze by (mode, date). An engine built at import
        with trade_mode='live' and never corrected would write the paper week's losses into
        the live row — the exact mixing the store exists to prevent."""
        engine, _ = self._engine(tmp_path)
        monkeypatch.setattr(
            "signal_engine.runtime.settings", type("S", (), {"mode_profiles": {}})()
        )
        apply_trade_mode(engine, "analyze")
        assert engine._trade_mode == "analyze"

    def test_applying_live_is_a_no_op_on_a_live_engine(self, tmp_path, monkeypatch):
        engine, _ = self._engine(tmp_path)
        before = engine.max_open_positions
        monkeypatch.setattr(
            "signal_engine.runtime.settings",
            type("S", (), {"mode_profiles": {"live": {}}})(),
        )
        apply_trade_mode(engine, "live")
        assert engine.max_open_positions == before
        assert engine._trade_mode == "live"


class TestParsing:
    def test_absent_block_yields_empty_profiles(self):
        """Every pre-existing config has no mode_profiles block and must keep working."""
        assert _parse_mode_profiles({}) == {}

    def test_profiles_are_parsed_and_lower_cased(self):
        parsed = _parse_mode_profiles(
            {"mode_profiles": {"LIVE": {"max_open_positions": 2},
                               "analyze": {"max_open_positions": 6}}}
        )
        assert parsed["live"]["max_open_positions"] == 2
        assert parsed["analyze"]["max_open_positions"] == 6

    def test_non_dict_entries_are_dropped_rather_than_crashing_startup(self):
        parsed = _parse_mode_profiles({"mode_profiles": {"live": None, "analyze": {"a": 1}}})
        assert "live" not in parsed
        assert parsed["analyze"] == {"a": 1}

    def test_a_non_dict_block_is_ignored(self):
        assert _parse_mode_profiles({"mode_profiles": "oops"}) == {}


class TestShippedConfig:
    def test_the_real_config_defines_both_modes(self):
        """A profile for only one mode is the confusing state this feature removes."""
        from signal_engine.config import settings

        assert set(settings.mode_profiles) >= {"live", "analyze"}

    def test_analyze_never_silently_inherits_a_live_loss_limit(self):
        from signal_engine.config import settings

        assert "daily_loss_limit" in settings.mode_profiles["analyze"]
        assert "daily_loss_limit" in settings.mode_profiles["live"]
