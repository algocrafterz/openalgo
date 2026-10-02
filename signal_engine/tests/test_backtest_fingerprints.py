"""Tests for per-component run fingerprints."""

from __future__ import annotations

import copy
import re

from signal_engine.backtest import fingerprints as fp
from signal_engine.research.intake import fingerprints as intake_fp
from signal_engine.research.intake.profiles import load_profile

HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _resolved() -> dict:
    return {
        "execution.bar_size": {"value": "5m", "ledger_id": "A-1"},
        "execution.entry_timing": {"value": "next_open", "ledger_id": "A-2"},
        "execution.slippage_bps_per_side": {"value": 5, "ledger_id": "A-3"},
        "execution.cost_bps_round_trip": {"value": 20, "ledger_id": "A-4"},
        "profile.name": {"value": "flattrade-nse-cash", "ledger_id": "A-5"},
    }


def _kwargs() -> dict:
    return {
        "intent": {"name": "x", "entry": {"rule": "a"}, "parameters": {"n": 5}},
        "resolved": _resolved(),
        "cost_model": {"schedule": "v1"},
        "symbols": ["SBIN", "INFY"],
        "universe_definition": "fno",
        "profile_content": {"name": "p", "broker": "b"},
        "dataset_fingerprint": "d" * 64,
    }


def test_reexports_match_intake():
    assert fp.rules_fingerprint is intake_fp.rules_fingerprint
    assert fp.params_fingerprint is intake_fp.params_fingerprint
    assert fp.strategy_fingerprint is intake_fp.strategy_fingerprint
    assert fp.canonical_json is intake_fp.canonical_json
    assert fp.sha256_hex is intake_fp.sha256_hex


def test_component_keys_and_hex():
    out = fp.component_fingerprints(**_kwargs())
    assert set(out) == {
        "rules", "params", "strategy", "execution", "cost", "universe", "profile", "dataset",
    }  # fmt: skip
    for key, value in out.items():
        assert HEX64.match(value), key


def test_deterministic():
    assert fp.component_fingerprints(**_kwargs()) == fp.component_fingerprints(**_kwargs())


def _changed(base: dict, other: dict) -> set[str]:
    return {k for k in base if base[k] != other[k]}


def test_parameter_edit_moves_params_and_strategy_only():
    base = fp.component_fingerprints(**_kwargs())
    kw = _kwargs()
    kw["intent"]["parameters"]["n"] = 6
    assert _changed(base, fp.component_fingerprints(**kw)) == {"params", "strategy"}


def test_rule_edit_moves_rules_and_strategy_only():
    base = fp.component_fingerprints(**_kwargs())
    kw = _kwargs()
    kw["intent"]["entry"]["rule"] = "b"
    assert _changed(base, fp.component_fingerprints(**kw)) == {"rules", "strategy"}


def test_slippage_moves_cost_only():
    base = fp.component_fingerprints(**_kwargs())
    kw = _kwargs()
    kw["resolved"]["execution.slippage_bps_per_side"]["value"] = 9
    assert _changed(base, fp.component_fingerprints(**kw)) == {"cost"}


def test_round_trip_cost_moves_cost_only():
    base = fp.component_fingerprints(**_kwargs())
    kw = _kwargs()
    kw["resolved"]["execution.cost_bps_round_trip"]["value"] = 33
    assert _changed(base, fp.component_fingerprints(**kw)) == {"cost"}


def test_cost_model_change_moves_cost_only_and_none_ok():
    base = fp.component_fingerprints(**_kwargs())
    kw = _kwargs()
    kw["cost_model"] = None
    assert _changed(base, fp.component_fingerprints(**kw)) == {"cost"}


def test_bar_size_moves_execution_only():
    base = fp.component_fingerprints(**_kwargs())
    kw = _kwargs()
    kw["resolved"]["execution.bar_size"]["value"] = "15m"
    assert _changed(base, fp.component_fingerprints(**kw)) == {"execution"}


def test_profile_field_in_resolved_excluded_from_execution():
    base = fp.execution_fingerprint(_resolved())
    r = _resolved()
    r["profile.name"]["value"] = "other"
    r["profile.extra"] = {"value": 1, "ledger_id": "A-9"}
    assert fp.execution_fingerprint(r) == base


def test_profile_content_moves_profile_only():
    base = fp.component_fingerprints(**_kwargs())
    kw = _kwargs()
    kw["profile_content"] = {"name": "p", "broker": "c"}
    assert _changed(base, fp.component_fingerprints(**kw)) == {"profile"}


def test_dataset_moves_dataset_only():
    base = fp.component_fingerprints(**_kwargs())
    kw = _kwargs()
    kw["dataset_fingerprint"] = "e" * 64
    assert _changed(base, fp.component_fingerprints(**kw)) == {"dataset"}


def test_universe_symbols_and_definition():
    base = fp.universe_fingerprint(["B", "A"], "d")
    assert fp.universe_fingerprint(["A", "B", "A"], "d") == base
    assert fp.universe_fingerprint(["A", "B", "C"], "d") != base
    assert fp.universe_fingerprint(["A", "B"], "e") != base


def test_universe_change_moves_universe_only():
    base = fp.component_fingerprints(**_kwargs())
    kw = _kwargs()
    kw["symbols"] = ["SBIN"]
    assert _changed(base, fp.component_fingerprints(**kw)) == {"universe"}


def test_ledger_ids_never_matter():
    base = fp.component_fingerprints(**_kwargs())
    kw = _kwargs()
    for entry in kw["resolved"].values():
        entry["ledger_id"] = "ZZ-" + str(entry["ledger_id"])
    assert fp.component_fingerprints(**kw) == base


def test_key_order_never_matters():
    base = fp.component_fingerprints(**_kwargs())
    kw = _kwargs()
    kw["resolved"] = dict(reversed(list(kw["resolved"].items())))
    kw["intent"] = dict(reversed(list(kw["intent"].items())))
    kw["profile_content"] = dict(reversed(list(kw["profile_content"].items())))
    assert fp.component_fingerprints(**kw) == base


def test_inputs_not_mutated():
    kw = _kwargs()
    before = copy.deepcopy(kw)
    fp.component_fingerprints(**kw)
    assert kw == before


def test_profile_fingerprint_matches_shipped_profile():
    profile = load_profile("flattrade-nse-cash")
    assert fp.profile_fingerprint(profile.as_dict()) == profile.sha256
