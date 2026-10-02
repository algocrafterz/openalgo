"""Rules, parameters and strategy fingerprints: identity of a strategy version."""

from __future__ import annotations

import pytest

from signal_engine.research.intake import fingerprints as fp
from signal_engine.tests.intake_fixtures import make_spec


class TestCanonical:
    def test_key_order_does_not_matter(self):
        assert fp.canonical_json({"a": 1, "b": [1, 2]}) == fp.canonical_json({"b": [1, 2], "a": 1})

    def test_list_order_matters(self):
        assert fp.canonical_json([1, 2]) != fp.canonical_json([2, 1])

    def test_sha256_is_64_hex(self):
        digest = fp.sha256_hex("x")
        assert len(digest) == 64 and int(digest, 16) >= 0


class TestRules:
    def test_stable_under_reordering(self):
        spec = make_spec()
        reordered = {k: spec[k] for k in reversed(list(spec))}
        assert fp.rules_fingerprint(spec) == fp.rules_fingerprint(reordered)

    @pytest.mark.parametrize(
        "change",
        [
            {"entry": ["5-min close > VWAP", "EMA9 > EMA25"]},
            {"exit": ["Close below VWAP"]},
            {"stop": ["2 ATR(14) below entry"]},
            {"take_profit": ["2R target"]},
            {"direction": "long_only"},
            {"timeframe": "15m"},
            {"holding_period": "20 bars"},
            {"filters": ["Skip names priced under 100"]},
            {"session_window": {"entry": "09:30-14:00"}},
            {"sizing": {"risk_per_trade_pct": 2.0, "capital_per_trade": 100000}},
            {"portfolio": {"mode": "single_position", "scope": "global"}},
            {"re_entry": {"allowed": True, "max_per_symbol_day": 1}},
            {"indicators": [{"name": "EMA", "period": 9}]},
        ],
    )
    def test_changes_with_any_rule(self, change):
        assert fp.rules_fingerprint(make_spec(**change)) != fp.rules_fingerprint(make_spec())

    @pytest.mark.parametrize(
        "change",
        [
            {"name": "Renamed"},
            {"source": {"kind": "video", "ref": "_inbox/other"}},
            {"profile": "other-profile"},
            {"rationale": "because"},
            {"kill_criterion": "never"},
            {"disclosure": {"prior_variants": 9}},
            {"parameters": {"ema_fast": 12}},
            {"schema_version": 2},
        ],
    )
    def test_ignores_metadata_profile_and_parameters(self, change):
        assert fp.rules_fingerprint(make_spec(**change)) == fp.rules_fingerprint(make_spec())


class TestParams:
    def test_changes_with_a_value(self):
        assert fp.params_fingerprint({"a": 1}) != fp.params_fingerprint({"a": 2})

    def test_stable_under_key_order(self):
        assert fp.params_fingerprint({"a": 1, "b": 2}) == fp.params_fingerprint({"b": 2, "a": 1})

    def test_none_word_equals_empty(self):
        assert fp.params_fingerprint("none") == fp.params_fingerprint({})


class TestStrategy:
    def test_parameter_edit_moves_params_and_strategy_not_rules(self):
        a, b = make_spec(), make_spec(parameters={"ema_fast": 12})
        assert fp.rules_fingerprint(a) == fp.rules_fingerprint(b)
        assert fp.params_fingerprint(a["parameters"]) != fp.params_fingerprint(b["parameters"])
        sa = fp.strategy_fingerprint(
            fp.rules_fingerprint(a), fp.params_fingerprint(a["parameters"])
        )
        sb = fp.strategy_fingerprint(
            fp.rules_fingerprint(b), fp.params_fingerprint(b["parameters"])
        )
        assert sa != sb

    def test_rule_edit_moves_rules_and_strategy_not_params(self):
        a, b = make_spec(), make_spec(exit=["Close below VWAP"])
        assert fp.params_fingerprint(a["parameters"]) == fp.params_fingerprint(b["parameters"])
        assert fp.rules_fingerprint(a) != fp.rules_fingerprint(b)

    def test_combines_both_components(self):
        assert fp.strategy_fingerprint("a" * 64, "b" * 64) != fp.strategy_fingerprint(
            "b" * 64, "a" * 64
        )
