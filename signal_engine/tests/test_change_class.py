"""Tests for run change classification."""

from __future__ import annotations

import copy

from signal_engine.backtest import change_class as cc


def _manifest() -> dict:
    return {
        "fingerprints": {
            "rules": "r0",
            "params": "p0",
            "strategy": "s0",
            "execution": "e0",
            "cost": "c0",
            "universe": "u0",
            "profile": "f0",
            "dataset": "d0",
        },
        "period": {"start": "2020-01-01", "end": "2024-12-31"},
        "engine": {
            "engine_version": "1.0",
            "code_commit": "abc",
            "code_dirty": False,
            "dirty_paths": [],
        },
    }


def _classify_with(mutator) -> list[str]:
    b = _manifest()
    mutator(b)
    return cc.classify(_manifest(), b)


def test_identical_is_research_rerun():
    assert cc.classify(_manifest(), _manifest()) == [cc.RESEARCH_RERUN]


def test_rules_change():
    assert _classify_with(lambda m: m["fingerprints"].update(rules="r1")) == [cc.STRATEGY_CHANGE]


def test_params_change():
    assert _classify_with(lambda m: m["fingerprints"].update(params="p1")) == [cc.PARAMETER_CHANGE]


def test_strategy_alone_does_not_double_count():
    assert _classify_with(lambda m: m["fingerprints"].update(strategy="s1")) == [cc.RESEARCH_RERUN]


def test_rules_and_params_with_strategy_not_counted_thrice():
    def mut(m):
        m["fingerprints"].update(rules="r1", params="p1", strategy="s1")

    assert _classify_with(mut) == [cc.STRATEGY_CHANGE, cc.PARAMETER_CHANGE]


def test_execution_change():
    assert _classify_with(lambda m: m["fingerprints"].update(execution="e1")) == [
        cc.EXECUTION_CHANGE
    ]


def test_cost_change():
    assert _classify_with(lambda m: m["fingerprints"].update(cost="c1")) == [cc.COST_CHANGE]


def test_universe_change():
    assert _classify_with(lambda m: m["fingerprints"].update(universe="u1")) == [cc.UNIVERSE_CHANGE]


def test_profile_change():
    assert _classify_with(lambda m: m["fingerprints"].update(profile="f1")) == [
        cc.DEPLOYMENT_PROFILE_CHANGE
    ]


def test_dataset_change_with_same_period():
    assert _classify_with(lambda m: m["fingerprints"].update(dataset="d1")) == [cc.DATASET_CHANGE]


def test_date_range_only_change():
    assert _classify_with(lambda m: m["period"].update(end="2025-06-30")) == [cc.DATA_RANGE_CHANGE]


def test_start_change():
    assert _classify_with(lambda m: m["period"].update(start="2019-01-01")) == [
        cc.DATA_RANGE_CHANGE
    ]


def test_engine_version_change():
    assert _classify_with(lambda m: m["engine"].update(engine_version="2.0")) == [cc.ENGINE_CHANGE]


def test_engine_commit_change():
    assert _classify_with(lambda m: m["engine"].update(code_commit="def")) == [cc.ENGINE_CHANGE]


def test_dirty_flags_ignored():
    def mut(m):
        m["engine"].update(code_dirty=True, dirty_paths=["x.py"])

    assert _classify_with(mut) == [cc.RESEARCH_RERUN]


def test_many_changes_in_order():
    def mut(m):
        m["engine"]["code_commit"] = "zzz"
        m["period"]["end"] = "2025-01-01"
        m["fingerprints"].update(
            rules="r1", params="p1", execution="e1", cost="c1", universe="u1", profile="f1",
            dataset="d1",
        )  # fmt: skip

    assert _classify_with(mut) == list(cc.ORDER)


def test_order_constant():
    assert cc.ORDER == (
        cc.STRATEGY_CHANGE,
        cc.PARAMETER_CHANGE,
        cc.EXECUTION_CHANGE,
        cc.COST_CHANGE,
        cc.UNIVERSE_CHANGE,
        cc.DEPLOYMENT_PROFILE_CHANGE,
        cc.DATASET_CHANGE,
        cc.DATA_RANGE_CHANGE,
        cc.ENGINE_CHANGE,
    )


def test_present_vs_null_is_changed():
    b = _manifest()
    b["fingerprints"]["cost"] = None
    assert cc.classify(_manifest(), b) == [cc.COST_CHANGE]
    c = _manifest()
    del c["fingerprints"]["cost"]
    assert cc.classify(_manifest(), c) == [cc.COST_CHANGE]


def test_missing_in_both_is_unchanged():
    a, b = _manifest(), _manifest()
    del a["fingerprints"]["cost"]
    b["fingerprints"]["cost"] = None
    assert cc.classify(a, b) == [cc.RESEARCH_RERUN]
    del b["fingerprints"]["cost"]
    assert cc.classify(a, b) == [cc.RESEARCH_RERUN]


def test_missing_sections_tolerated():
    assert cc.classify({}, {}) == [cc.RESEARCH_RERUN]
    assert cc.classify({}, _manifest()) == list(cc.ORDER)


def test_inputs_not_mutated():
    a, b = _manifest(), _manifest()
    b["fingerprints"]["rules"] = "r9"
    a0, b0 = copy.deepcopy(a), copy.deepcopy(b)
    cc.classify(a, b)
    assert a == a0 and b == b0


def test_first_run_classes_empty():
    assert cc.first_run_classes() == []
