"""Next action lookup."""

from __future__ import annotations

import copy

import pytest

from signal_engine.backtest import evaluation as ev
from signal_engine.backtest import next_action as na
from signal_engine.backtest import promotion as pr
from signal_engine.backtest import verdict as vd
from signal_engine.tests.test_verdict import DQ_OK, OK, make_eval


def act(evaluation, code_dirty=False, reproducible=True, primary_gate=None):
    p = pr.evaluate_promotion(evaluation, code_dirty=code_dirty, reproducible=reproducible)
    return na.next_action(
        p,
        evaluation,
        code_dirty=code_dirty,
        reproducible=reproducible,
        primary_gate=primary_gate,
    )


def test_every_gate_has_a_rule():
    missing = [g[0] for g in ev._GATES if g[0] not in na.RULES]
    assert missing == []
    for rule in na.RULES.values():
        assert rule["action"] and rule["reason"]


def test_every_dimension_has_a_default():
    for dim in {g[1] for g in ev._GATES}:
        assert dim in na.DIMENSION_DEFAULTS


@pytest.mark.parametrize(
    "gate", [g[0] for g in ev._GATES if g[0][0] == "G" and int(g[0][1:].split(".")[0]) <= 6]
)
def test_each_backtest_rule_fires_for_its_gate(gate):
    out = act(make_eval(**{gate.replace(".", "_"): "not_evaluated"}))
    assert out["gate"] == gate
    assert out["action"] == na.RULES[gate]["action"]
    assert out["reason"] == na.RULES[gate]["reason"]


@pytest.mark.parametrize("gate", ["G7.1", "G8.3", "G9.2"])
def test_later_stage_gates_fire_once_earlier_stages_ready(gate):
    out = act(make_eval(**{gate.replace(".", "_"): "fail"}))
    assert out["gate"] == gate
    assert out["action"] == na.RULES[gate]["action"]


def test_oos_wording():
    assert act(make_eval(G3_2="not_evaluated"))["action"] == "Run out-of-sample validation"


def test_first_problem_in_gate_order_is_chosen():
    out = act(make_eval(G4_6="fail", G1_2="not_evaluated"))
    assert out["gate"] == "G1.2"


def test_dirty_run_keeps_the_gate_action_and_extends_the_reason():
    out = act(make_eval(G3_2="fail"), code_dirty=True)
    assert out["gate"] == "G3.2"
    assert out["action"] == na.RULES["G3.2"]["action"]
    assert out["reason"] == na.RULES["G3.2"]["reason"] + na.DIRTY_SUFFIX
    assert out["reason"].endswith("; also commit the working tree and re-run from a clean state")


def test_not_reproducible_run_keeps_the_gate_action():
    out = act(make_eval(G3_2="fail"), reproducible=False)
    assert out["gate"] == "G3.2"
    assert out["reason"].endswith(na.DIRTY_SUFFIX)


def test_dirty_with_no_open_gate_returns_commit_action():
    out = act(make_eval(), code_dirty=True)
    assert out == {
        "action": "Commit the working tree and re-run from a clean state",
        "reason": out["reason"],
        "gate": None,
    }
    assert out["reason"]


def test_not_reproducible_overrides():
    out = act(make_eval(), reproducible=False)
    assert out["gate"] is None
    assert out["action"].startswith("Commit the working tree")


def test_everything_ready():
    out = act(make_eval())
    assert out["action"] == "Advance to the next stage"
    assert out["gate"] is None


def test_stage_without_gates_falls_back():
    out = act(make_eval(default="report"))
    assert out["gate"] is None
    assert out["action"]


def test_does_not_change_verdict():
    e = make_eval(G3_2="not_evaluated")
    before = vd.derive(e, validity=OK, data_quality=DQ_OK, readiness_missing=[])
    act(e)
    assert vd.derive(e, validity=OK, data_quality=DQ_OK, readiness_missing=[]) == before
    assert "run_verdict" not in act(e)


def test_inputs_not_mutated():
    e = make_eval(G3_2="fail")
    p = pr.evaluate_promotion(e, code_dirty=False, reproducible=True)
    e0, p0 = copy.deepcopy(e), copy.deepcopy(p)
    na.next_action(p, e, code_dirty=False, reproducible=True)
    assert e == e0 and p == p0


def test_primary_gate_is_used_before_an_earlier_open_gate():
    e = make_eval(G0_1="not_evaluated", G2_1="fail")
    assert act(e)["gate"] == "G0.1"
    assert act(e, primary_gate="G2.1")["gate"] == "G2.1"


def test_primary_gate_not_open_is_ignored():
    e = make_eval(G0_1="not_evaluated")
    assert act(e, primary_gate="G2.1")["gate"] == "G0.1"
    assert act(e, primary_gate="X.9")["gate"] == "G0.1"


def test_first_hard_failed_gate_beats_gate_order():
    e = make_eval(G0_1="not_evaluated", G1_2="fail")
    hard = {r["gate"] for d in e["dimensions"] for r in d["rows"] if r["hard"]}
    assert "G1.2" in hard
    assert act(e)["gate"] == "G1.2"
