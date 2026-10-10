"""Execution resolution: platform defaults plus declared overrides, every value with a source."""

from __future__ import annotations

import pytest
import yaml

from signal_engine.backtest import record_run
from signal_engine.backtest.types import RunConfig
from signal_engine.research.intake import execution, profiles
from signal_engine.tests.intake_fixtures import make_spec

PROFILE = profiles.load_profile("flattrade-nse-cash")
SWING = {"style": "swing", "direction": "long_only", "timeframe": "1d"}


def _resolve(spec=None, overrides=None):
    return execution.resolve_execution(spec or make_spec(), PROFILE, overrides)


class TestPlatformDefaults:
    def test_slippage_is_the_house_default(self):
        v = _resolve().get("execution.slippage_bps_per_side")
        assert v.value == 4.0
        assert v.value == record_run.SLIPPAGE_BPS_PER_SIDE
        assert v.source == "platform_default"

    def test_intraday_cost_comes_from_the_existing_constant(self):
        v = _resolve().get("execution.cost_bps_round_trip")
        assert v.value == record_run.INTRADAY_COST_BPS
        assert v.source == "platform_default"

    def test_swing_cost_comes_from_the_existing_constant(self):
        v = _resolve(make_spec(product="CNC", **SWING)).get("execution.cost_bps_round_trip")
        assert v.value == record_run.SWING_COST_BPS

    def test_oos_fraction_and_embargo_come_from_existing_constants(self):
        r = _resolve()
        assert r.get("execution.oos_fraction").value == record_run.OOS_FRACTION
        assert r.get("execution.embargo_sessions").value == record_run.EMBARGO_SESSIONS

    def test_bar_size_is_derived_from_timeframe(self):
        v = _resolve(make_spec(timeframe="15m")).get("execution.bar_size")
        assert v.value == "15m" and v.source == "derived"

    def test_warmup_is_the_longest_indicator_period(self):
        v = _resolve().get("execution.warmup_bars")
        assert v.value == 20 and v.source == "derived"
        assert "EMA" in v.reason

    def test_warmup_is_zero_without_periodic_indicators(self):
        spec = make_spec(indicators=[{"name": "VWAP", "anchor": "session"}])
        assert _resolve(spec).get("execution.warmup_bars").value == 0
        assert _resolve(make_spec(indicators="none")).get("execution.warmup_bars").value == 0

    def test_a_higher_timeframe_indicator_counts_in_base_bars(self):
        spec = make_spec(timeframe="5m", indicators=[{"name": "EMA_15M", "period": 20, "source": "close"}])
        v = _resolve(spec).get("execution.warmup_bars")
        assert v.value == 60 and v.source == "derived"  # 20 fifteen-minute bars are 60 five-minute bars
        assert "EMA_15M(20)" in v.reason and "60 5m bars" in v.reason

    def test_the_longest_span_wins_across_timeframes(self):
        spec = make_spec(timeframe="5m", indicators=[
            {"name": "EMA", "period": 50, "source": "close"},
            {"name": "ATR_15M", "period": 14, "source": "close"},      # 42 base bars
            {"name": "EMA_1H", "period": 10, "source": "close"},       # 120 base bars
            {"name": "ADX_15M", "period": 14, "source": "close"}])  # fmt: skip
        assert _resolve(spec).get("execution.warmup_bars").value == 120
        spec2 = make_spec(timeframe="5m", indicators=[
            {"name": "EMA", "period": 50, "source": "close"}, {"name": "ATR_15M", "period": 14, "source": "close"}])  # fmt: skip
        assert _resolve(spec2).get("execution.warmup_bars").value == 50  # 42 < 50

    def test_an_indicator_on_the_base_timeframe_or_without_a_suffix_is_unchanged(self):
        same = make_spec(timeframe="15m", indicators=[{"name": "EMA_15M", "period": 20, "source": "close"}])
        assert _resolve(same).get("execution.warmup_bars").value == 20
        plain = make_spec(indicators=[{"name": "EMA", "period": 20, "source": "close"}])
        assert _resolve(plain).get("execution.warmup_bars").value == 20

    def test_a_partial_base_bar_rounds_up(self):
        spec = make_spec(timeframe="15m", indicators=[{"name": "EMA_20M", "period": 10, "source": "close"}])
        assert _resolve(spec).get("execution.warmup_bars").value == 14  # ceil(10 x 20 / 15)

    def test_a_daily_spec_ignores_a_suffix_it_cannot_scale(self):
        spec = make_spec(product="CNC", **{**SWING, "indicators": [{"name": "EMA_15M", "period": 20, "source": "close"}]})
        assert _resolve(spec).get("execution.warmup_bars").value == 20

    def test_idea_029_resolves_sixty_bars_and_the_recorded_v4_value_is_not_rewritten(self):
        import json
        from pathlib import Path

        import yaml

        root = Path(execution.__file__).resolve().parents[2] / "research" / "ideas" / "idea-029-vwap-2sd-range-mean-reversion"
        spec = yaml.safe_load((root / "spec.yaml").read_text())
        v = _resolve(spec).get("execution.warmup_bars")
        assert v.value == 60 and "60 5m bars" in v.reason
        recorded = json.loads((root / "runs" / "2026-10-09-vwap-2sd-range-mean-reversion-v4" / "execution" / "config.json").read_text())
        assert recorded["resolved"]["execution.warmup_bars"]["value"] == 20  # immutable evidence

    def test_product_is_derived_from_style_when_absent(self):
        v = _resolve(make_spec(product=None)).get("execution.product")
        assert v.value == "MIS" and v.source == "derived"

    def test_product_stated_in_the_spec_is_user_sourced(self):
        v = _resolve().get("execution.product")
        assert v.value == "MIS" and v.source == "user"

    def test_swing_product_is_cnc(self):
        assert _resolve(make_spec(product=None, **SWING)).get("execution.product").value == "CNC"


class TestProfileMapping:
    def test_mis_profile_values_are_broker_config(self):
        r = _resolve()
        assert r.get("profile.mis_squareoff_min").value == 900
        assert r.get("profile.squareoff_margin_min").value == 5
        assert r.get("profile.time_exit_min").value == 885
        assert r.get("profile.mis_squareoff_min").source == "broker_config"

    def test_cnc_records_carry_forward_and_no_mis_cutoffs(self):
        r = _resolve(make_spec(product="CNC", **SWING))
        assert r.get("profile.carry_forward").value is True
        assert not any(v.field == "profile.mis_squareoff_min" for v in r.values)

    def test_run_config_fields_apply_to_a_runconfig(self):
        applied = RunConfig().with_(**_resolve().run_config_fields())
        assert applied.mis_squareoff_min == 900
        assert applied.squareoff_margin_min == 5
        assert applied.time_exit_min == 885
        assert applied.cost_bps == record_run.INTRADAY_COST_BPS

    def test_profile_name_and_hash_are_recorded(self):
        r = _resolve()
        assert r.profile_name == "flattrade-nse-cash"
        assert r.profile_sha256 == PROFILE.sha256


class TestOverrides:
    def test_override_replaces_the_default_and_is_user_sourced(self):
        r = _resolve(overrides={"slippage_bps_per_side": {"value": 7.5, "reason": "thin names"}})
        v = r.get("execution.slippage_bps_per_side")
        assert v.value == 7.5 and v.source == "user" and v.reason == "thin names"

    def test_override_needs_a_reason(self):
        with pytest.raises(execution.ExecutionError, match="reason"):
            _resolve(overrides={"slippage_bps_per_side": {"value": 7.5}})

    def test_override_value_must_be_positive_number(self):
        with pytest.raises(execution.ExecutionError, match="slippage_bps_per_side"):
            _resolve(overrides={"slippage_bps_per_side": {"value": -1, "reason": "x"}})

    @pytest.mark.parametrize("field", ["entry", "exit", "stop", "direction", "style", "timeframe"])
    def test_strategy_intent_in_execution_yaml_is_rejected(self, field):
        with pytest.raises(execution.ExecutionError, match="spec.yaml"):
            _resolve(overrides={field: {"value": "x", "reason": "x"}})

    def test_unknown_override_is_rejected(self):
        with pytest.raises(execution.ExecutionError, match="unknown"):
            _resolve(overrides={"latency_ms": {"value": 5, "reason": "x"}})


class TestStructure:
    def test_every_value_has_a_source_and_a_reason(self):
        for v in _resolve().values:
            assert v.source in execution.SOURCES
            assert v.reason
            assert v.section in ("execution", "profile")

    def test_field_names_are_unique(self):
        names = [v.field for v in _resolve().values]
        assert len(names) == len(set(names))

    def test_as_dict_is_a_flat_field_to_value_map(self):
        d = _resolve().as_dict()
        assert d["execution.bar_size"] == "5m"
        assert d["profile.mis_squareoff_min"] == 900

    def test_unknown_field_lookup_raises(self):
        with pytest.raises(KeyError):
            _resolve().get("execution.nope")

    def test_profile_that_lacks_the_product_is_rejected(self, tmp_path):
        (tmp_path / "cnc-only.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "cnc-only",
                    "broker": "x",
                    "market": "m",
                    "products": {"CNC": {"carry_forward": True}},
                }
            ),
            encoding="utf-8",
        )
        cnc_only = profiles.load_profile("cnc-only", tmp_path)
        with pytest.raises(execution.ExecutionError, match="MIS"):
            execution.resolve_execution(make_spec(), cnc_only)


class TestOverridesFile:
    def test_absent_file_returns_none(self, tmp_path):
        assert execution.load_overrides(tmp_path) is None

    def test_file_is_parsed(self, tmp_path):
        (tmp_path / "execution.yaml").write_text(
            yaml.safe_dump({"slippage_bps_per_side": {"value": 6, "reason": "r"}}),
            encoding="utf-8",
        )
        assert execution.load_overrides(tmp_path)["slippage_bps_per_side"]["value"] == 6

    def test_unreadable_file_raises(self, tmp_path):
        (tmp_path / "execution.yaml").write_text("a: [unclosed", encoding="utf-8")
        with pytest.raises(execution.ExecutionError, match="unreadable"):
            execution.load_overrides(tmp_path)


class TestReviewHardening:
    @pytest.mark.parametrize("content", ["- a\n- b\n", "just text\n", "5\n"])
    def test_non_mapping_execution_yaml_is_rejected(self, tmp_path, content):
        (tmp_path / "execution.yaml").write_text(content, encoding="utf-8")
        with pytest.raises(execution.ExecutionError, match="mapping"):
            execution.load_overrides(tmp_path)

    def test_empty_execution_yaml_means_no_overrides(self, tmp_path):
        (tmp_path / "execution.yaml").write_text("", encoding="utf-8")
        assert execution.load_overrides(tmp_path) == {}

    def test_bool_period_does_not_count_toward_warmup(self):
        spec = make_spec(indicators=[{"name": "EMA", "period": True}])
        assert _resolve(spec).get("execution.warmup_bars").value == 0


class TestEligibilityDefaults:
    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("execution.eligibility_min_prior_sessions", 60),
            ("execution.eligibility_min_volume", 1),
            ("execution.eligibility_min_price", 10.0),
        ],
    )
    def test_resolves_as_platform_default_with_reason(self, name, value):
        v = _resolve().get(name)
        assert v.value == value
        assert v.source == "platform_default"
        assert v.section == "execution"
        assert v.reason.strip()

    def test_swing_spec_gets_the_same_defaults(self):
        r = _resolve(make_spec(product="CNC", **SWING))
        assert r.get("execution.eligibility_min_prior_sessions").value == 60

    def test_catalog_classes_them_defaultable_medium(self):
        from signal_engine.research.intake import fields

        for name in (
            "execution.eligibility_min_prior_sessions",
            "execution.eligibility_min_volume",
            "execution.eligibility_min_price",
        ):
            f = fields.get(name)
            assert f.cls == fields.DEFAULTABLE and f.materiality == fields.MEDIUM

    def test_they_are_not_overridable_by_the_author(self):
        with pytest.raises(execution.ExecutionError, match="unknown override"):
            _resolve(overrides={"eligibility_min_price": {"value": 5, "reason": "x"}})
