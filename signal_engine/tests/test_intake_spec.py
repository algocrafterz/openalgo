"""Spec v2 validation: intent only, every problem reported, tagged validity or intake.

Principle: incomplete strategy -> clarify -> backtest, never incomplete -> assume -> backtest.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from signal_engine.research.intake import fields, spec
from signal_engine.tests.intake_fixtures import GOOD_SPEC, MULTI_POSITION, make_spec

TEMPLATE = Path(__file__).resolve().parents[1] / "research" / "ideas" / "_template" / "spec.yaml"


def _problems(data, kind=None):
    found = spec.validate_spec(data)
    return [p for p in found if kind is None or p.kind == kind]


def _texts(data, kind=None):
    return [f"{p.field}: {p.message}" for p in _problems(data, kind)]


class TestCompleteSpec:
    def test_good_spec_validates_clean(self):
        assert spec.validate_spec(GOOD_SPEC) == []

    def test_none_is_accepted_where_the_catalog_allows_it(self):
        s = make_spec(stop="none", take_profit="none", filters="none", session_window="none")
        assert spec.validate_spec(s) == []

    def test_timing_block_is_optional(self):
        assert "timing" not in GOOD_SPEC
        timed = make_spec(
            timing={
                "signal_evaluation": "bar_close",
                "order_submission": "signal_close",
                "fill_model": "next_bar_open",
            }
        )
        assert spec.validate_spec(timed) == []

    def test_multi_position_with_every_field_is_clean(self):
        assert spec.validate_spec(make_spec(portfolio=MULTI_POSITION)) == []


class TestTemplate:
    def test_template_placeholders_are_rejected_as_written(self):
        loaded = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
        assert spec.validate_spec(loaded) != []

    def test_template_uses_only_catalog_fields(self):
        loaded = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
        known = {f.name.split(".")[0] for f in fields.FIELDS} | {"schema_version"}
        assert set(loaded) <= known

    def test_template_covers_every_required_top_level_field(self):
        loaded = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
        required = {
            f.name.split(".")[0]
            for f in fields.FIELDS
            if f.section == fields.INTENT and f.cls == fields.BLOCKER
        }
        assert required <= set(loaded)


class TestCatalog:
    def test_every_field_has_exactly_one_class_and_section(self):
        names = [f.name for f in fields.FIELDS]
        assert len(names) == len(set(names))
        for f in fields.FIELDS:
            assert f.cls in fields.CLASSES
            assert f.section in fields.SECTIONS
            assert f.materiality in fields.MATERIALITIES

    def test_timing_fields_are_derivable_and_high_materiality(self):
        for name in ("timing.signal_evaluation", "timing.order_submission", "timing.fill_model"):
            f = fields.get(name)
            assert f.cls == fields.DERIVABLE
            assert f.materiality == fields.HIGH

    def test_portfolio_scope_is_high_materiality(self):
        assert fields.get("portfolio.scope").materiality == fields.HIGH

    def test_unknown_field_raises(self):
        with pytest.raises(KeyError):
            fields.get("no.such.field")


class TestShape:
    def test_non_mapping_is_a_validity_problem(self):
        found = spec.validate_spec(["not", "a", "mapping"])
        assert [(p.kind, p.field) for p in found] == [("validity", "spec")]

    def test_wrong_schema_version(self):
        found = _problems(make_spec(schema_version=1), "validity")
        assert [p.field for p in found] == ["schema_version"]

    def test_unknown_field_is_validity(self):
        s = make_spec()
        s["market"] = "NSE"
        assert "market: unknown field" in " ".join(_texts(s, "validity"))

    def test_every_problem_is_reported_not_only_the_first(self):
        s = make_spec(style="weekly", timeframe="3m", direction="sideways")
        fields_hit = {p.field for p in spec.validate_spec(s)}
        assert {"style", "timeframe", "direction"} <= fields_hit

    @pytest.mark.parametrize("timeframe", ["1m", "5m", "15m", "1h", "1d"])
    def test_checklist_timeframes_accepted(self, timeframe):
        style = "swing" if timeframe == "1d" else "intraday"
        product = "CNC" if style == "swing" else "MIS"
        s = make_spec(timeframe=timeframe, style=style, product=product, direction="long_only")
        assert _problems(s, "validity") == []

    @pytest.mark.parametrize("timeframe", ["3m", "2h", "5min", "1w", "", 5])
    def test_other_timeframes_rejected(self, timeframe):
        found = _problems(make_spec(timeframe=timeframe), "validity")
        assert [p.field for p in found] == ["timeframe"]


class TestMissingBlockers:
    @pytest.mark.parametrize(
        "field",
        [
            "name",
            "source",
            "profile",
            "style",
            "direction",
            "timeframe",
            "entry",
            "exit",
            "stop",
            "take_profit",
            "holding_period",
            "indicators",
            "parameters",
            "filters",
            "session_window",
            "re_entry",
            "sizing",
            "portfolio",
        ],
    )
    def test_each_blocker_is_an_intake_problem_when_missing(self, field):
        found = [p for p in _problems(make_spec(**{field: None}), "intake") if p.field == field]
        assert found, f"{field} not reported"
        assert "missing" in found[0].message

    def test_product_is_derivable_not_a_blocker(self):
        assert _problems(make_spec(product=None)) == []


class TestRuleWording:
    @pytest.mark.parametrize("field", ["entry", "exit", "stop"])
    def test_vague_wording_is_an_intake_problem(self, field):
        s = make_spec(**{field: ["Buy when momentum is strong, roughly 5"]})
        found = [p for p in _problems(s, "intake") if p.field == field]
        assert found and "strong" in found[0].message and "roughly" in found[0].message

    @pytest.mark.parametrize("field", ["entry", "exit", "stop"])
    def test_rule_without_a_number_is_an_intake_problem(self, field):
        s = make_spec(**{field: ["below the swing low"]})
        found = [p for p in _problems(s, "intake") if p.field == field]
        assert found and "number" in found[0].message

    def test_none_is_not_allowed_for_entry(self):
        found = [p for p in _problems(make_spec(entry="none")) if p.field == "entry"]
        assert found and "none" in found[0].message

    def test_holding_period_needs_a_limit(self):
        s = make_spec(holding_period="as long as it works")
        assert [p.field for p in _problems(s, "intake")] == ["holding_period"]

    @pytest.mark.parametrize("value", ["12 bars", "same_session", "same session", "3 days"])
    def test_holding_period_limits_accepted(self, value):
        found = _problems(make_spec(holding_period=value))
        assert [p for p in found if p.field == "holding_period"] == []

    @pytest.mark.parametrize("value", ["none", "None", "unlimited", "Unlimited"])
    def test_an_explicit_no_maximum_is_accepted(self, value):
        found = _problems(make_spec(holding_period=value))
        assert [p for p in found if p.field == "holding_period"] == []

    @pytest.mark.parametrize("value", ["no limit really", "until further notice", "open ended"])
    def test_other_ways_of_saying_no_maximum_are_still_not_a_limit(self, value):
        s = make_spec(holding_period=value)
        assert [p.field for p in _problems(s, "intake")] == ["holding_period"]


class TestDirectionAndProduct:
    @pytest.mark.parametrize("style", ["swing", "positional"])
    @pytest.mark.parametrize("direction", ["both", "short_only"])
    def test_shorts_in_swing_and_positional_are_a_validity_error(self, style, direction):
        s = make_spec(style=style, direction=direction, product="CNC", timeframe="1d")
        found = [p for p in _problems(s, "validity") if p.field == "direction"]
        assert found and "long_only" in found[0].message and style in found[0].message
        assert _problems(s, "intake") == []

    @pytest.mark.parametrize("direction", ["long_only", "short_only", "both"])
    def test_intraday_allows_every_direction(self, direction):
        assert _problems(make_spec(direction=direction)) == []

    @pytest.mark.parametrize("style", ["swing", "positional"])
    def test_long_only_swing_is_clean(self, style):
        s = make_spec(style=style, direction="long_only", product="CNC", timeframe="1d")
        assert _problems(s) == []

    @pytest.mark.parametrize(
        ("style", "product"), [("swing", "MIS"), ("positional", "MIS"), ("intraday", "CNC")]
    )
    def test_product_contradicting_style_is_a_validity_error(self, style, product):
        s = make_spec(style=style, product=product, direction="long_only", timeframe="1d")
        found = [p for p in _problems(s, "validity") if p.field == "product"]
        assert found and product in found[0].message and style in found[0].message

    def test_unknown_product_is_a_validity_error(self):
        assert [p.field for p in _problems(make_spec(product="NRML"), "validity")] == ["product"]


class TestPortfolio:
    def test_single_position_without_scope_is_an_intake_gap(self):
        found = _problems(make_spec(portfolio={"mode": "single_position"}))
        assert [(p.kind, p.field) for p in found] == [("intake", "portfolio.scope")]
        assert "High" in found[0].message

    def test_unknown_mode_is_validity(self):
        found = _problems(make_spec(portfolio={"mode": "many"}), "validity")
        assert [p.field for p in found] == ["portfolio.mode"]

    def test_global_scope_with_ranking_is_a_validity_error(self):
        s = make_spec(portfolio={"mode": "single_position", "scope": "global", "selection": "rank"})
        found = _problems(s, "validity")
        assert [p.field for p in found] == ["portfolio.selection"]
        assert "global" in found[0].message

    @pytest.mark.parametrize("extra", ["sector_limit", "correlation_limit"])
    def test_global_scope_with_sector_or_correlation_is_a_validity_error(self, extra):
        s = make_spec(portfolio={"mode": "single_position", "scope": "global", extra: 2})
        assert [p.field for p in _problems(s, "validity")] == [f"portfolio.{extra}"]

    def test_per_symbol_with_multi_position_field_says_declare_multi_position(self):
        s = make_spec(
            portfolio={"mode": "single_position", "scope": "per_symbol", "max_concurrent": 3}
        )
        found = _problems(s, "validity")
        assert [p.field for p in found] == ["portfolio.max_concurrent"]
        assert "multi_position" in found[0].message

    @pytest.mark.parametrize("field", sorted(set(MULTI_POSITION) - {"mode"}))
    def test_multi_position_requires_each_field(self, field):
        portfolio = {k: v for k, v in MULTI_POSITION.items() if k != field}
        found = _problems(make_spec(portfolio=portfolio), "intake")
        assert [p.field for p in found] == [f"portfolio.{field}"]

    def test_multi_position_does_not_take_a_scope(self):
        s = make_spec(portfolio={**MULTI_POSITION, "scope": "per_symbol"})
        assert [p.field for p in _problems(s, "validity")] == ["portfolio.scope"]

    def test_sector_and_correlation_limits_allowed_for_multi_position(self):
        s = make_spec(portfolio={**MULTI_POSITION, "sector_limit": 2, "correlation_limit": 0.8})
        assert _problems(s) == []


class TestTiming:
    def test_absent_timing_is_not_a_problem(self):
        assert _problems(make_spec()) == []

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("signal_evaluation", "whenever"),
            ("order_submission", "later"),
            ("fill_model", "midpoint"),
        ],
    )
    def test_bad_timing_value_is_validity(self, key, value):
        found = _problems(make_spec(timing={key: value}), "validity")
        assert [p.field for p in found] == [f"timing.{key}"]

    def test_unknown_timing_key_is_validity(self):
        found = _problems(make_spec(timing={"latency": "50ms"}), "validity")
        assert [p.field for p in found] == ["timing.latency"]


class TestIndicators:
    def test_source_defaults_to_close_for_price_indicators(self):
        assert _problems(make_spec(indicators=[{"name": "EMA", "period": 20}])) == []

    def test_vwap_without_anchor_is_ambiguous_and_blocks(self):
        found = _problems(make_spec(indicators=[{"name": "VWAP"}]))
        assert [(p.kind, p.field) for p in found] == [("intake", "indicators")]
        assert "VWAP" in found[0].message and "anchor" in found[0].message

    def test_unknown_indicator_without_source_blocks(self):
        found = _problems(make_spec(indicators=[{"name": "MYSTERY", "period": 9}]), "intake")
        assert found and "source" in found[0].message

    def test_unknown_indicator_with_source_is_clean(self):
        s = make_spec(indicators=[{"name": "MYSTERY", "period": 9, "source": "hl2"}])
        assert _problems(s) == []

    def test_indicator_source_must_be_known(self):
        s = make_spec(indicators=[{"name": "EMA", "period": 9, "source": "vibes"}])
        assert [p.field for p in _problems(s, "validity")] == ["indicators"]

    def test_indicator_entry_without_name_is_validity(self):
        found = _problems(make_spec(indicators=[{"period": 9}]), "validity")
        assert [p.field for p in found] == ["indicators"]

    def test_indicators_none_is_accepted(self):
        assert _problems(make_spec(indicators="none")) == []


class TestReEntry:
    def test_not_allowed_with_trigger_fields_is_validity(self):
        found = _problems(make_spec(re_entry={"allowed": False, "trigger": "after_stop"}))
        assert [(p.kind, p.field) for p in found] == [("validity", "re_entry.trigger")]

    def test_allowed_requires_max_per_symbol_day(self):
        found = _problems(make_spec(re_entry={"allowed": True}))
        assert [(p.kind, p.field) for p in found] == [("intake", "re_entry.max_per_symbol_day")]

    def test_allowed_with_limit_and_trigger_is_clean(self):
        s = make_spec(re_entry={"allowed": True, "max_per_symbol_day": 2, "trigger": "after_stop"})
        assert _problems(s) == []

    def test_unknown_trigger_is_validity(self):
        s = make_spec(re_entry={"allowed": True, "max_per_symbol_day": 1, "trigger": "whenever"})
        assert [p.field for p in _problems(s, "validity")] == ["re_entry.trigger"]


class TestSizingAndDisclosure:
    def test_sizing_needs_risk_and_capital(self):
        found = _problems(make_spec(sizing={"risk_per_trade_pct": 1.0}))
        assert [(p.kind, p.field) for p in found] == [("intake", "sizing.capital_per_trade")]

    def test_sizing_numbers_must_be_positive(self):
        s = make_spec(sizing={"risk_per_trade_pct": 0, "capital_per_trade": -5})
        assert {p.field for p in _problems(s, "validity")} == {
            "sizing.risk_per_trade_pct",
            "sizing.capital_per_trade",
        }

    def test_disclosure_is_never_a_blocker(self):
        assert _problems(make_spec(disclosure=None)) == []

    def test_disclosure_destination_validated(self):
        s = make_spec(disclosure={"prior_variants": 0, "destination": "fax"})
        assert [p.field for p in _problems(s, "validity")] == ["disclosure.destination"]

    def test_negative_prior_variants_is_validity(self):
        found = _problems(make_spec(disclosure={"prior_variants": -1}), "validity")
        assert [p.field for p in found] == ["disclosure.prior_variants"]


class TestProfileLink:
    def test_unknown_profile_is_a_validity_problem(self):
        found = _problems(make_spec(profile="no-such-broker"), "validity")
        assert [p.field for p in found] == ["profile"]
        assert "no-such-broker" in found[0].message

    def test_profile_that_does_not_support_the_product_is_validity(self, tmp_path):
        (tmp_path / "cash-only.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "cash-only",
                    "broker": "x",
                    "market": "NSE cash equity",
                    "products": {"CNC": {"carry_forward": True}},
                }
            ),
            encoding="utf-8",
        )
        found = spec.validate_spec(make_spec(profile="cash-only"), profiles_dir=tmp_path)
        assert [(p.kind, p.field) for p in found] == [("validity", "profile")]
        assert "MIS" in found[0].message


class TestLoadAndGate:
    def test_load_spec_returns_none_when_absent(self, tmp_path):
        assert spec.load_spec(tmp_path) is None

    def test_load_spec_raises_on_unreadable_yaml(self, tmp_path):
        (tmp_path / "spec.yaml").write_text("a: [unclosed", encoding="utf-8")
        with pytest.raises(spec.SpecError, match="unreadable"):
            spec.load_spec(tmp_path)

    def test_check_idea_requires_a_spec_for_every_idea(self, tmp_path):
        with pytest.raises(spec.SpecError, match="spec.yaml is required"):
            spec.check_idea("IDEA-012", tmp_path)

    def test_check_idea_lists_every_problem(self, tmp_path):
        bad = make_spec(entry=["Buy when momentum is strong"], style="weekly")
        (tmp_path / "spec.yaml").write_text(yaml.safe_dump(bad), encoding="utf-8")
        with pytest.raises(spec.SpecError) as err:
            spec.check_idea("IDEA-012", tmp_path)
        assert "entry" in str(err.value) and "style" in str(err.value)

    def test_check_idea_accepts_a_valid_spec(self, tmp_path):
        (tmp_path / "spec.yaml").write_text(yaml.safe_dump(GOOD_SPEC), encoding="utf-8")
        spec.check_idea("IDEA-012", tmp_path)

    def test_registry_problems_for_an_active_idea_without_spec(self, tmp_path):
        found = spec.registry_problems("IDEA-003", "backtest", tmp_path)
        assert found and "spec.yaml is required" in found[0]

    def test_registry_allows_inbox_idea_without_spec(self, tmp_path):
        assert spec.registry_problems("IDEA-003", "inbox", tmp_path) == []

    def test_registry_validates_a_present_spec_for_any_status(self, tmp_path):
        bad = make_spec(stop=["below the low"])
        (tmp_path / "spec.yaml").write_text(yaml.safe_dump(bad), encoding="utf-8")
        found = spec.registry_problems("IDEA-003", "inbox", tmp_path)
        assert any("stop" in p for p in found)

    def test_no_id_based_exemption_remains(self):
        assert not hasattr(spec, "SPEC_REQUIRED_FROM_ID")
        assert not hasattr(spec, "spec_required")


class TestReviewHardening:
    def test_non_bool_re_entry_allowed_is_validity(self):
        found = _problems(make_spec(re_entry={"allowed": "yes"}))
        assert [(p.kind, p.field) for p in found] == [("validity", "re_entry.allowed")]

    @pytest.mark.parametrize("name", ["EMA", "SMA", "RSI", "ATR", "ADX"])
    def test_periodic_indicator_without_period_is_an_intake_gap(self, name):
        found = _problems(make_spec(indicators=[{"name": name, "source": "close"}]), "intake")
        assert found and "period" in found[0].message

    def test_obv_needs_no_period(self):
        assert _problems(make_spec(indicators=[{"name": "OBV"}])) == []

    @pytest.mark.parametrize("window", ["14:30-09:20", "10:00-10:00", "9:20-14:30", "09:20"])
    def test_bad_entry_window_is_validity(self, window):
        found = _problems(make_spec(session_window={"entry": window}), "validity")
        assert [p.field for p in found] == ["session_window.entry"]

    def test_no_trade_windows_are_validated(self):
        s = make_spec(session_window={"entry": "09:20-14:30", "no_trade": ["12:00-11:00"]})
        assert [p.field for p in _problems(s, "validity")] == ["session_window.no_trade"]

    def test_no_trade_must_be_a_list(self):
        s = make_spec(session_window={"entry": "09:20-14:30", "no_trade": "12:00-13:00"})
        found = _problems(s, "validity")
        assert [p.field for p in found] == ["session_window.no_trade"]
        assert "list" in found[0].message

    def test_unknown_session_window_key_is_validity(self):
        found = _problems(make_spec(session_window={"entry": "09:20-14:30", "exit": "x"}))
        assert [p.field for p in found] == ["session_window.exit"]

    @pytest.mark.parametrize("key", ["max_concurrent", "max_trades_per_day"])
    def test_multi_position_counts_must_be_integers(self, key):
        found = _problems(make_spec(portfolio={**MULTI_POSITION, key: 2.5}), "validity")
        assert [p.field for p in found] == [f"portfolio.{key}"]

    def test_multi_position_non_numeric_exposure_is_validity(self):
        found = _problems(make_spec(portfolio={**MULTI_POSITION, "max_exposure": "lots"}))
        assert [(p.kind, p.field) for p in found] == [("validity", "portfolio.max_exposure")]

    @pytest.mark.parametrize("key", ["selection", "conflict"])
    def test_multi_position_enums_are_checked(self, key):
        found = _problems(make_spec(portfolio={**MULTI_POSITION, key: "vibes"}))
        assert [(p.kind, p.field) for p in found] == [("validity", f"portfolio.{key}")]

    @pytest.mark.parametrize("key", ["sector_limit", "correlation_limit"])
    def test_multi_position_limits_must_be_positive(self, key):
        found = _problems(make_spec(portfolio={**MULTI_POSITION, key: 0}), "validity")
        assert [p.field for p in found] == [f"portfolio.{key}"]

    @pytest.mark.parametrize(
        ("block", "bad"),
        [
            ("order_types", {"entry": "iceberg"}),
            ("order_types", {"slippage": "x"}),
            ("reference_prices", {"stop": "vibes"}),
            ("reference_prices", {"leverage": "entry_fill"}),
        ],
    )
    def test_optional_enum_blocks_reject_bad_values_and_keys(self, block, bad):
        found = _problems(make_spec(**{block: bad}), "validity")
        assert [p.field for p in found] == [f"{block}.{next(iter(bad))}"]

    def test_valid_optional_blocks_are_clean(self):
        s = make_spec(
            order_types={"entry": "limit", "stop": "stop_market", "target": "limit"},
            reference_prices={"stop": "entry_fill", "sizing": "signal_close"},
        )
        assert _problems(s) == []


class TestEntryWindowAgainstProfile:
    def test_window_inside_the_profile_cutoff_is_clean(self):
        assert _problems(make_spec(session_window={"entry": "09:20-14:45"})) == []

    @pytest.mark.parametrize("window", ["09:20-14:50", "09:20-15:10"])
    def test_mis_window_past_the_standing_exit_is_a_validity_error(self, window):
        found = _problems(make_spec(session_window={"entry": window}))
        assert [(p.kind, p.field) for p in found] == [("validity", "session_window.entry")]
        assert "flattrade-nse-cash" in found[0].message

    def test_swing_cnc_is_not_limited(self):
        s = make_spec(
            style="swing",
            direction="long_only",
            product="CNC",
            timeframe="1d",
            session_window={"entry": "09:20-15:25"},
        )
        assert _problems(s) == []

    def test_a_malformed_window_reports_only_its_own_problem(self):
        found = _problems(make_spec(session_window={"entry": "14:30-09:20"}))
        assert [p.field for p in found] == ["session_window.entry"]
        assert "start before" in found[0].message
