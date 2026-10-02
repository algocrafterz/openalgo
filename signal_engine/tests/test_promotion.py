"""Per-stage promotion decision (lifecycle 1.6)."""

from __future__ import annotations

import copy

from signal_engine.backtest import promotion as pr
from signal_engine.tests.test_verdict import make_eval


def run(evaluation, code_dirty=False, reproducible=True, **kw):
    return pr.evaluate_promotion(evaluation, code_dirty=code_dirty, reproducible=reproducible, **kw)


def decisions(p):
    return {k: v["decision"] for k, v in p["stages"].items()}


def test_stage_gates_mapping():
    assert list(pr.STAGE_GATES["backtest"]) == [0, 1, 2, 3, 4, 5, 6]
    assert list(pr.STAGE_GATES["paper"]) == [7, 8]
    assert list(pr.STAGE_GATES["live"]) == [9]


def test_shape():
    p = run(make_eval())
    assert set(p) == {"stages", "next_stage"}
    s = p["stages"]["backtest"]
    assert set(s) == {"decision", "gates", "reasons"}
    assert {"gate": "G3.1", "status": "PASS"} in s["gates"]


def test_everything_pass_all_ready_and_no_next_stage():
    p = run(make_eval())
    assert decisions(p) == dict.fromkeys(("backtest", "paper", "live"), "READY_FOR_NEXT_STAGE")
    assert p["next_stage"] is None


def test_backtest_ready_only_gives_paper_next():
    p = run(make_eval(G7_1="not_evaluated"))
    assert decisions(p) == {
        "backtest": "READY_FOR_NEXT_STAGE",
        "paper": "NOT_READY",
        "live": "BLOCKED",
    }
    assert p["next_stage"] == "paper"
    assert "paper" in p["stages"]["live"]["reasons"][0]


def test_backtest_not_ready_blocks_later_stages():
    p = run(make_eval(G3_2="not_evaluated"))
    assert decisions(p) == {"backtest": "NOT_READY", "paper": "BLOCKED", "live": "BLOCKED"}
    assert p["next_stage"] is None
    assert "backtest" in p["stages"]["paper"]["reasons"][0]
    assert "backtest" in p["stages"]["live"]["reasons"][0]
    st = {g["gate"]: g["status"] for g in p["stages"]["backtest"]["gates"]}
    assert st["G3.2"] == "NOT_RUN"


def test_fail_status_vocabulary():
    p = run(make_eval(G1_2="fail", G6_1="not_applicable", G3_1="report"))
    st = {g["gate"]: g["status"] for g in p["stages"]["backtest"]["gates"]}
    assert st["G1.2"] == "FAIL"
    assert st["G6.1"] == "NOT_APPLICABLE"
    assert st["G3.1"] == "REPORT"


def test_dirty_run_not_ready_with_reason():
    p = run(make_eval(), code_dirty=True)
    assert p["stages"]["backtest"]["decision"] == "NOT_READY"
    assert "dirty working tree" in p["stages"]["backtest"]["reasons"]
    assert p["stages"]["paper"]["decision"] == "BLOCKED"
    assert p["next_stage"] is None


def test_not_reproducible_not_ready_with_reason():
    p = run(make_eval(), reproducible=False)
    assert p["stages"]["backtest"]["decision"] == "NOT_READY"
    assert "not reproducible" in p["stages"]["backtest"]["reasons"]


def test_stage_with_no_required_gates():
    p = run(make_eval(default="report"))
    assert p["stages"]["backtest"]["decision"] == "NOT_READY"
    assert p["stages"]["backtest"]["reasons"] == ["no gates evaluated for this stage"]


def test_empty_evaluation():
    p = run({})
    assert decisions(p) == {"backtest": "NOT_READY", "paper": "BLOCKED", "live": "BLOCKED"}


def test_report_rows_do_not_block_ready():
    p = run(make_eval(G3_1="report"))
    assert p["stages"]["backtest"]["decision"] == "READY_FOR_NEXT_STAGE"


def test_inputs_not_mutated():
    e = make_eval(G3_2="fail")
    e0 = copy.deepcopy(e)
    run(e)
    assert e == e0


def _with_extra_row(status, hard=True):
    e = make_eval()
    e["dimensions"].append(
        {
            "name": "extra",
            "rows": [{"gate": "X.1", "title": "t", "status": status, "hard": hard}],
        }
    )
    return e


def test_unknown_gate_id_row_lands_in_backtest_stage():
    p = run(_with_extra_row("fail"))
    assert {"gate": "X.1", "status": "FAIL"} in p["stages"]["backtest"]["gates"]
    assert p["stages"]["backtest"]["decision"] == pr.NOT_READY
    assert "gate X.1 failed" in p["stages"]["backtest"]["reasons"]


def test_unknown_gate_id_row_passing_keeps_ready():
    assert decisions(run(_with_extra_row("pass")))["backtest"] == pr.READY


def test_readiness_missing_blocks_backtest_stage():
    p = run(make_eval(), readiness_missing=["a", "b"])
    assert decisions(p) == {"backtest": "NOT_READY", "paper": "BLOCKED", "live": "BLOCKED"}
    assert "engine readiness: a, b" in p["stages"]["backtest"]["reasons"]


def test_empty_readiness_missing_is_unchanged():
    assert run(make_eval(), readiness_missing=[]) == run(make_eval())
    assert decisions(run(make_eval()))["backtest"] == pr.READY


def test_all_pass_with_readiness_missing_is_marginal_and_not_ready():
    from signal_engine.backtest import verdict as vd
    from signal_engine.tests.test_verdict import DQ_OK, OK

    e = make_eval()
    v = vd.derive(e, validity=OK, data_quality=DQ_OK, readiness_missing=["x"])
    assert v["run_verdict"] == "MARGINAL"
    assert decisions(run(e, readiness_missing=["x"]))["backtest"] == pr.NOT_READY
