"""Run verdict derivation (lifecycle 1.5)."""

from __future__ import annotations

import copy
from typing import Any

from signal_engine.backtest import evaluation as ev
from signal_engine.backtest import verdict as vd


def make_eval(default: str = "pass", **overrides: str) -> dict[str, Any]:
    """Full evaluation block from the real gate list; `G2_1="fail"` overrides one gate."""
    over = {k.replace("_", "."): v for k, v in overrides.items()}
    dims: dict[str, list[dict[str, Any]]] = {}
    for gate, dim, title, hard, _why in ev._GATES:
        status = over.get(gate, default)
        dims.setdefault(dim, []).append(
            {
                "gate": gate,
                "title": title,
                "status": status,
                "hard": hard,
                "value": None,
                "threshold": "",
                "evidence": "",
            }
        )
    return {
        "version": 1,
        "criteria": "x",
        "horizon": "intraday",
        "dimensions": [{"name": n, "rows": r} for n, r in dims.items()],
        "summary": {},
    }


OK = {"ok": True, "failures": []}
DQ_OK = {"status": "PASS", "affects_validity": False, "items": []}


def derive(evaluation, **kw):
    args = {"validity": OK, "data_quality": DQ_OK, "readiness_missing": []}
    args.update(kw)
    return vd.derive(evaluation, **args)


def gates(result):
    return [r["gate"] for r in result["reasons"]]


def test_shape_is_exact():
    r = derive(make_eval())
    assert set(r) == {"run_verdict", "primary_failed_gate", "reasons", "caps_applied"}


def test_rule1_validity_failure_is_inconclusive_with_no_gates():
    bad = {"ok": False, "failures": [{"check": "lookahead", "detail": "uses next bar"}]}
    r = derive(make_eval(G1_2="fail"), validity=bad)
    assert r["run_verdict"] == "INCONCLUSIVE"
    assert r["primary_failed_gate"] is None
    assert gates(r) == [None]
    assert "lookahead" in r["reasons"][0]["text"]
    assert r["reasons"][0]["evidence_ref"] is None


def test_rule1_each_validity_failure_named():
    bad = {"ok": False, "failures": [{"check": "a", "detail": "x"}, {"check": "b", "detail": "y"}]}
    r = derive(make_eval(), validity=bad)
    assert len(r["reasons"]) == 2


def test_rule1_data_quality_warning_affecting_validity():
    dq = {"status": "WARNING", "affects_validity": True, "items": ["gaps"]}
    r = derive(make_eval(G2_1="fail"), data_quality=dq)
    assert r["run_verdict"] == "INCONCLUSIVE"
    assert gates(r) == [None]
    assert "data quality" in r["reasons"][0]["text"].lower()


def test_data_quality_warning_not_affecting_validity_is_ignored():
    dq = {"status": "WARNING", "affects_validity": False, "items": ["gaps"]}
    assert derive(make_eval(), data_quality=dq)["run_verdict"] == "EDGE"


def test_rule2_min_sample_fail_beats_hard_fail():
    r = derive(make_eval(G2_1="fail", G1_2="fail"))
    assert r["run_verdict"] == "INCONCLUSIVE"
    assert gates(r) == ["G2.1"]
    assert r["reasons"][0]["evidence_ref"] == "evaluation:G2.1"


def test_rule2_min_sample_not_evaluated():
    r = derive(make_eval(G2_1="not_evaluated"))
    assert r["run_verdict"] == "INCONCLUSIVE"
    assert r["reasons"][0]["status"] == "NOT_RUN"


def test_rule3_hard_fail_is_no_edge_one_reason_each_in_gate_order():
    r = derive(make_eval(G4_4="fail", G1_2="fail", G3_1="fail"))
    assert r["run_verdict"] == "NO_EDGE"
    assert gates(r) == ["G1.2", "G4.4"]
    assert r["primary_failed_gate"] == "G1.2"
    assert r["reasons"][0]["status"] == "FAIL"


def test_rule3_beats_rule4():
    r = derive(make_eval(G1_2="fail", G3_2="not_evaluated"))
    assert r["run_verdict"] == "NO_EDGE"


def test_hard_gate_not_evaluated_is_not_no_edge():
    r = derive(make_eval(G1_2="not_evaluated"))
    assert r["run_verdict"] == "MARGINAL"


def test_rule4_required_backtest_gate_fail_or_not_run_is_marginal():
    r = derive(make_eval(G3_2="not_evaluated", G0_1="fail", G6_1="fail"))
    assert r["run_verdict"] == "MARGINAL"
    assert gates(r) == ["G0.1", "G3.2", "G6.1"]
    assert r["primary_failed_gate"] == "G0.1"
    assert [x["status"] for x in r["reasons"]] == ["FAIL", "NOT_RUN", "FAIL"]


def test_not_run_never_counts_as_pass():
    assert derive(make_eval(default="not_evaluated", G2_1="pass"))["run_verdict"] == "MARGINAL"


def test_report_and_not_applicable_rows_never_decide():
    r = derive(make_eval(G3_2="report", G6_1="not_applicable"))
    assert r["run_verdict"] == "EDGE"
    assert r["reasons"] == []
    assert r["primary_failed_gate"] is None


def test_paper_and_live_gates_do_not_decide_the_backtest_verdict():
    r = derive(make_eval(G7_1="not_evaluated", G9_1="fail", G8_1="fail"))
    assert r["run_verdict"] == "EDGE"


def test_rule5_edge():
    r = derive(make_eval())
    assert r["run_verdict"] == "EDGE"
    assert r["caps_applied"] == []


def test_readiness_cap_turns_edge_into_marginal():
    r = derive(make_eval(), readiness_missing=["cost model", "dst"])
    assert r["run_verdict"] == "MARGINAL"
    assert r["caps_applied"] == ["engine readiness: cost model, dst"]
    assert any("readiness" in x["text"].lower() for x in r["reasons"])


def test_readiness_cap_does_not_touch_other_verdicts():
    r = derive(make_eval(G1_2="fail"), readiness_missing=["x"])
    assert r["run_verdict"] == "NO_EDGE"
    assert r["caps_applied"] == []


def test_dirty_and_unreproducible_never_change_verdict():
    base = derive(make_eval())
    r = derive(make_eval(), code_dirty=True, reproducible=False)
    assert r == base


def test_empty_evaluation_is_inconclusive():
    r = derive({})
    assert r["run_verdict"] == "INCONCLUSIVE"
    assert gates(r) == ["G2.1"]


def test_inputs_not_mutated():
    e = make_eval(G1_2="fail")
    bad = {"ok": False, "failures": [{"check": "a", "detail": "b"}]}
    e0, b0 = copy.deepcopy(e), copy.deepcopy(bad)
    derive(e, validity=bad, readiness_missing=["x"])
    assert e == e0 and bad == b0


class TestMinimumSampleGates:
    def test_mandatory_gate_is_g2_1_and_track_length_is_diagnostic(self):
        assert vd.MIN_SAMPLE_GATES == ("G2.1",)
        assert vd.MIN_SAMPLE_DIAGNOSTIC_GATES == ("G2.7",)

    def test_track_length_not_run_never_makes_a_run_inconclusive(self):
        r = vd.derive(
            make_eval(G2_7="not_evaluated"), validity=OK, data_quality=DQ_OK, readiness_missing=[]
        )
        assert r["run_verdict"] != "INCONCLUSIVE"

    def test_track_length_open_is_listed_as_diagnostic_and_does_not_decide(self):
        base = vd.derive(make_eval(), validity=OK, data_quality=DQ_OK, readiness_missing=[])
        r = vd.derive(make_eval(G2_7="fail"), validity=OK, data_quality=DQ_OK, readiness_missing=[])
        diag = [x for x in r["reasons"] if x["gate"] == "G2.7"]
        assert diag and diag[0]["text"].startswith("Diagnostic")
        assert "does not decide" in diag[0]["text"]
        assert r["run_verdict"] == base["run_verdict"]
        assert r["primary_failed_gate"] != "G2.7"

    def test_diagnostic_is_not_listed_when_the_gate_passes(self):
        r = vd.derive(make_eval(), validity=OK, data_quality=DQ_OK, readiness_missing=[])
        assert not [x for x in r["reasons"] if x["gate"] == "G2.7"]

    def test_diagnostic_is_not_listed_for_an_invalid_run(self):
        bad = {"ok": False, "failures": [{"check": "lookahead", "detail": "x"}]}
        r = vd.derive(
            make_eval(G2_7="fail"), validity=bad, data_quality=DQ_OK, readiness_missing=[]
        )
        assert r["run_verdict"] == "INCONCLUSIVE"
        assert not [x for x in r["reasons"] if x["gate"] == "G2.7"]

    def test_mandatory_sample_gate_still_decides(self):
        r = vd.derive(
            make_eval(G2_1="not_evaluated"), validity=OK, data_quality=DQ_OK, readiness_missing=[]
        )
        assert r["run_verdict"] == "INCONCLUSIVE" and r["primary_failed_gate"] == "G2.1"
