"""Strategy spec template: an incomplete or ambiguous spec must be rejected before backtesting.

Principle: incomplete strategy -> clarify -> backtest, never incomplete -> assume -> backtest.
"""

import copy
from datetime import date
from pathlib import Path

import pytest
import yaml

from signal_engine.research import spec

GOOD = {
    "name": "VWAP EMA momentum",
    "market": "NSE cash equity, F&O-eligible names",
    "timeframe": "5m",
    "trading_style": "intraday",
    "direction": "both",
    "entry": [
        "5-min close > VWAP",
        "EMA9 > EMA20",
        "Enter at the next candle open",
    ],
    "exit": ["Close below VWAP", "Maximum hold 12 bars"],
    "stop_loss": ["1 ATR(14) below entry"],
    "take_profit": ["2R target"],
    "position_sizing": "Risk 1% of capital per trade",
    "max_holding_period": "12 bars",
    "filters": ["Skip names priced under 100"],
    "session_filters": ["Entries 09:30 to 11:30 IST"],
    "indicators": ["VWAP", "EMA(9)", "EMA(20)", "ATR(14)"],
    "parameters": {"ema_fast": 9, "ema_slow": 20, "atr_len": 14},
    "cost_assumptions": "16 bps round trip",
    "constraints": "none",
    "rationale": "Trend continuation after VWAP reclaim.",
}


def _with(**changes):
    out = copy.deepcopy(GOOD)
    out.update(changes)
    return out


class TestCompleteSpec:
    def test_good_spec_has_no_problems(self):
        assert spec.validate_spec(GOOD) == []

    def test_none_is_allowed_for_optional_rule_fields(self):
        s = _with(take_profit="none", filters="none", session_filters="none")
        assert spec.validate_spec(s) == []


class TestMissingFields:
    @pytest.mark.parametrize("field", spec.REQUIRED_FIELDS)
    def test_each_required_field_is_reported(self, field):
        s = copy.deepcopy(GOOD)
        del s[field]
        problems = spec.validate_spec(s)
        assert any(field in p for p in problems)

    @pytest.mark.parametrize("field", ["entry", "exit", "stop_loss", "rationale"])
    def test_empty_value_is_reported(self, field):
        problems = spec.validate_spec(_with(**{field: [] if field != "rationale" else "  "}))
        assert any(field in p for p in problems)

    def test_not_a_mapping(self):
        assert spec.validate_spec(["a"]) == ["spec is not a mapping"]

    def test_unknown_field_is_reported(self):
        assert any("surprise" in p for p in spec.validate_spec(_with(surprise=1)))


class TestAmbiguity:
    def test_vague_entry_is_rejected(self):
        problems = spec.validate_spec(_with(entry=["Buy when momentum is strong"]))
        assert any("entry" in p and "strong" in p for p in problems)

    def test_entry_without_any_number_is_rejected(self):
        problems = spec.validate_spec(_with(entry=["Close above VWAP"]))
        assert any("entry" in p and "number" in p for p in problems)

    def test_stop_loss_without_number_is_rejected(self):
        problems = spec.validate_spec(_with(stop_loss=["Below the recent low"]))
        assert any("stop_loss" in p for p in problems)

    def test_vague_word_in_other_rule_field(self):
        problems = spec.validate_spec(_with(exit=["Exit when the move looks exhausted, 3 bars"]))
        assert any("exit" in p and "looks" in p for p in problems)

    def test_max_holding_needs_a_limit(self):
        assert any(
            "max_holding_period" in p
            for p in spec.validate_spec(_with(max_holding_period="until done"))
        )

    def test_same_session_counts_as_a_limit(self):
        assert spec.validate_spec(_with(max_holding_period="same session")) == []

    def test_timeframe_must_have_unit(self):
        assert any("timeframe" in p for p in spec.validate_spec(_with(timeframe="fast")))

    def test_parameters_must_be_mapping(self):
        assert any("parameters" in p for p in spec.validate_spec(_with(parameters=["a"])))

    def test_cost_assumption_needs_a_number(self):
        assert any(
            "cost_assumptions" in p for p in spec.validate_spec(_with(cost_assumptions="normal"))
        )


class TestDirectionRules:
    def test_direction_must_be_known(self):
        assert any("direction" in p for p in spec.validate_spec(_with(direction="up")))

    def test_style_must_be_known(self):
        assert any("trading_style" in p for p in spec.validate_spec(_with(trading_style="hft")))

    @pytest.mark.parametrize("style", ["swing", "positional"])
    def test_swing_must_be_long_only(self, style):
        problems = spec.validate_spec(_with(trading_style=style, direction="both"))
        assert any("long_only" in p for p in problems)

    def test_swing_long_only_is_fine(self):
        assert spec.validate_spec(_with(trading_style="swing", direction="long_only")) == []


class TestLoading:
    def test_template_declares_every_required_field(self):
        template = Path(spec.__file__).parent / "ideas" / "_template" / "spec.yaml"
        data = yaml.safe_load(template.read_text(encoding="utf-8"))
        assert set(spec.REQUIRED_FIELDS) <= set(data)

    def test_template_is_not_itself_a_valid_spec(self):
        template = Path(spec.__file__).parent / "ideas" / "_template" / "spec.yaml"
        assert spec.validate_spec(yaml.safe_load(template.read_text(encoding="utf-8")))

    def test_load_spec_reads_yaml(self, tmp_path: Path):
        (tmp_path / "spec.yaml").write_text(yaml.safe_dump(GOOD), encoding="utf-8")
        assert spec.load_spec(tmp_path)["name"] == GOOD["name"]

    def test_load_spec_returns_none_when_absent(self, tmp_path: Path):
        assert spec.load_spec(tmp_path) is None

    def test_load_spec_wraps_yaml_errors(self, tmp_path: Path):
        (tmp_path / "spec.yaml").write_text("a: [unclosed", encoding="utf-8")
        with pytest.raises(spec.SpecError, match="spec.yaml"):
            spec.load_spec(tmp_path)


class TestRequirement:
    def test_new_ideas_require_a_spec(self):
        assert spec.spec_required("IDEA-027") is True
        assert spec.spec_required("IDEA-100") is True

    def test_existing_ideas_are_exempt(self):
        assert spec.spec_required("IDEA-026") is False
        assert spec.spec_required("IDEA-001") is False

    def test_check_idea_raises_for_missing_spec_on_new_idea(self, tmp_path: Path):
        with pytest.raises(spec.SpecError, match="spec.yaml is required"):
            spec.check_idea("IDEA-027", tmp_path)

    def test_check_idea_passes_for_exempt_idea_without_spec(self, tmp_path: Path):
        spec.check_idea("IDEA-012", tmp_path)

    def test_check_idea_validates_a_present_spec_even_when_exempt(self, tmp_path: Path):
        (tmp_path / "spec.yaml").write_text(yaml.safe_dump(_with(entry=["buy strong"])), "utf-8")
        with pytest.raises(spec.SpecError, match="entry"):
            spec.check_idea("IDEA-012", tmp_path)

    def test_check_idea_accepts_valid_spec(self, tmp_path: Path):
        (tmp_path / "spec.yaml").write_text(yaml.safe_dump(GOOD), encoding="utf-8")
        spec.check_idea("IDEA-027", tmp_path)


def test_yaml_dates_do_not_break_validation():
    # Guard against a YAML date sneaking into a text field via an unquoted value.
    problems = spec.validate_spec(_with(rationale=date(2026, 1, 1)))
    assert any("rationale" in p for p in problems)
