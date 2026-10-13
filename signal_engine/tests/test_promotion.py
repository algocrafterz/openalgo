"""Per-stage promotion decision (lifecycle 1.6)."""

from __future__ import annotations

import copy

import pytest

from signal_engine.backtest import evaluation as ev
from signal_engine.backtest import promotion as pr
from signal_engine.tests.test_verdict import CLEAN_RUN, DQ_OK, OK, make_eval


def run(evaluation, code_dirty=False, reproducible=True, **kw):
    return pr.evaluate_promotion(
        evaluation, code_dirty=code_dirty, reproducible=reproducible, **{**CLEAN_RUN, **kw}
    )


def decisions(p):
    return {k: v["decision"] for k, v in p["stages"].items()}


def test_stage_gates_mapping():
    assert list(pr.STAGE_GATES["backtest"]) == [0, 1, 2, 3, 4, 5, 6]
    assert list(pr.STAGE_GATES["paper"]) == [7, 8]
    assert list(pr.STAGE_GATES["live"]) == [9]


def test_shape():
    p = run(make_eval())
    assert set(p) == {"stages", "next_stage", "limitations", "critical"}
    assert p["limitations"] == []
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
            "rows": [{"gate": "Z.1", "title": "t", "status": status, "hard": hard}],
        }
    )
    return e


def test_unknown_gate_id_row_lands_in_backtest_stage():
    p = run(_with_extra_row("fail"))
    assert {"gate": "Z.1", "status": "FAIL"} in p["stages"]["backtest"]["gates"]
    assert p["stages"]["backtest"]["decision"] == pr.NOT_READY
    assert "gate Z.1 failed" in p["stages"]["backtest"]["reasons"]


def test_unknown_gate_id_row_passing_keeps_ready():
    assert decisions(run(_with_extra_row("pass")))["backtest"] == pr.READY


def test_a_scoped_limitation_blocks_no_stage_and_travels_with_the_record():
    """A scoped research limitation (a survivor-biased universe) narrows the claim; it is not a
    defect and not a mandatory data dependency, so it holds no stage. It is listed under
    `limitations` so no consumer reads a clean promotion as an unqualified research claim.
    """
    p = run(make_eval(), readiness_missing=["a", "b"])
    assert decisions(p) == dict.fromkeys(pr.STAGES, pr.READY)
    assert all(not s["reasons"] for s in p["stages"].values())
    assert p["limitations"] == ["a", "b"]


def test_a_critical_capability_is_never_also_listed_as_a_limitation():
    p = run(make_eval(), readiness_missing=["a", "crit"], readiness_critical=["crit"])
    assert p["limitations"] == ["a"]
    assert p["critical"] == ["crit"]
    assert decisions(p)["backtest"] == pr.NOT_READY


def test_limitation_is_carried_even_when_an_earlier_stage_blocks_live():
    p = run(make_eval(G7_1="not_evaluated"), readiness_missing=["a"])
    assert decisions(p) == {
        "backtest": "READY_FOR_NEXT_STAGE",
        "paper": "NOT_READY",
        "live": "BLOCKED",
    }
    assert p["limitations"] == ["a"]
    assert p["stages"]["live"]["reasons"] == ["the paper stage is not ready"]


INVALID = {"ok": False, "failures": [{"check": "config", "detail": "bad stop"}], "checks_run": []}
DQ_BAD = {"status": "WARNING", "affects_validity": True, "items": ["gaps"]}


def test_invalid_run_is_never_promotable_even_with_clean_gates():
    """The verdict says INCONCLUSIVE for an invalid run; promotion must not contradict it."""
    p = run(make_eval(), validity=INVALID)
    assert decisions(p)["backtest"] == pr.NOT_READY
    assert "run is not valid (config): bad stop" in p["stages"]["backtest"]["reasons"]
    assert decisions(p)["paper"] == pr.BLOCKED


def test_data_quality_that_affects_validity_blocks_promotion():
    p = run(make_eval(), data_quality=DQ_BAD)
    assert decisions(p)["backtest"] == pr.NOT_READY
    assert any("data quality" in r for r in p["stages"]["backtest"]["reasons"])


def test_valid_run_and_benign_data_quality_do_not_change_promotion():
    ok = {"ok": True, "failures": [], "checks_run": ["config"]}
    dq = {"status": "PASS", "affects_validity": False, "items": []}
    assert run(make_eval(), validity=ok, data_quality=dq) == run(make_eval())


def test_diagnostic_x_gates_do_not_split_verdict_and_promotion():
    """X.* rows are heuristics outside the criteria document: the verdict ignores them, so a
    failed or unrun X row must not hold promotion either."""
    from signal_engine.backtest import verdict as vd
    from signal_engine.tests.test_verdict import DQ_OK, OK

    for status in ("not_evaluated", "fail"):
        e = make_eval(X_2=status)
        assert vd.derive(e, validity=OK, data_quality=DQ_OK, readiness_missing=[])[
            "run_verdict"
        ] == "EDGE"
        assert decisions(run(e))["backtest"] == pr.READY


def test_limitation_never_rescues_a_failed_gate():
    p = run(make_eval(G3_2="fail"), readiness_missing=["a"])
    assert decisions(p)["backtest"] == pr.NOT_READY
    assert "gate G3.2 failed" in p["stages"]["backtest"]["reasons"]


def test_empty_readiness_missing_is_unchanged():
    assert run(make_eval(), readiness_missing=[]) == run(make_eval())
    assert decisions(run(make_eval()))["backtest"] == pr.READY


def test_all_pass_with_limitation_is_marginal_verdict_but_ready_for_paper():
    """The verdict states how strong the claim is; promotion states what may happen next.

    The two answer different questions, so a clean run with an open limitation is held below
    EDGE and still eligible for paper. Neither may overstate the other.
    """
    from signal_engine.backtest import verdict as vd
    from signal_engine.tests.test_verdict import DQ_OK, OK

    e = make_eval()
    v = vd.derive(e, validity=OK, data_quality=DQ_OK, readiness_missing=["x"])
    assert v["run_verdict"] == "MARGINAL"
    assert v["caps_applied"] == ["engine readiness: x"]
    assert decisions(run(e, readiness_missing=["x"]))["backtest"] == pr.READY


class TestDiagnosticGatesAreNotRequired:
    def _stage(self, **kw):
        from signal_engine.tests.test_verdict import make_eval

        return pr.evaluate_promotion(
            make_eval(**kw), code_dirty=False, reproducible=True, **CLEAN_RUN
        )[
            "stages"
        ]["backtest"]

    def test_track_length_not_run_does_not_block_the_backtest_stage(self):
        assert self._stage(G2_7="not_evaluated")["decision"] == "READY_FOR_NEXT_STAGE"

    def test_track_length_failure_does_not_block_either(self):
        assert self._stage(G2_7="fail")["decision"] == "READY_FOR_NEXT_STAGE"

    def test_a_mandatory_sample_gate_still_blocks(self):
        assert self._stage(G2_1="not_evaluated")["decision"] == "NOT_READY"


class TestFailClosed:
    """The evaluator itself refuses; no caller can build an input that slips through."""

    def test_the_safety_inputs_cannot_be_omitted(self):
        with pytest.raises(TypeError):
            pr.evaluate_promotion(make_eval(), code_dirty=False, reproducible=True)

    def test_a_validity_block_that_was_not_supplied_is_not_ready(self):
        p = run(make_eval(), validity=None)
        assert decisions(p)["backtest"] == pr.NOT_READY
        assert "validity was not supplied" in p["stages"]["backtest"]["reasons"]

    def test_a_data_quality_report_that_was_not_supplied_is_not_ready(self):
        p = run(make_eval(), data_quality=None)
        assert decisions(p)["backtest"] == pr.NOT_READY

    def test_a_run_status_other_than_valid_is_not_ready(self):
        for status in ("INVALID_DATA", "", None, "anything"):
            p = run(make_eval(), run_status=status)
            assert decisions(p)["backtest"] == pr.NOT_READY, status

    def test_a_missing_gate_row_counts_as_not_run(self):
        e = make_eval()
        for dim in e["dimensions"]:
            dim["rows"] = [r for r in dim["rows"] if r["gate"] != "G3.2"]
        e["dimensions"] = [d for d in e["dimensions"] if d["rows"]]
        gates_present = {r["gate"] for d in e["dimensions"] for r in d["rows"]}
        missing = sorted({g[0] for g in ev._GATES} - gates_present)
        assert missing  # the fixture really lacks a row
        p = run(e)
        assert decisions(p)["backtest"] == pr.NOT_READY
        assert all(f"gate {g} has not been run" in p["stages"]["backtest"]["reasons"]
                   for g in missing if g not in pr.DIAGNOSTIC_GATES and pr.stage_of(g) == "backtest"
                   and g not in pr.PAPER_EXEMPT_GATES)

    def test_no_evaluation_at_all_is_not_ready(self):
        for empty in (None, {}, {"dimensions": []}):
            p = run(empty)
            assert decisions(p)["backtest"] == pr.NOT_READY
            assert "no evaluation supplied" in p["stages"]["backtest"]["reasons"]

    @pytest.mark.parametrize("state", ["fail", "not_evaluated"])
    def test_a_disqualifying_verdict_is_never_promotable(self, state):
        from signal_engine.backtest import verdict as vd

        for gate in ("G1.2", "G2.1", "G0.5", "G6.9", "G4.1"):
            e = make_eval(**{gate.replace(".", "_"): state})
            v = vd.derive(e, validity=OK, data_quality=DQ_OK, readiness_missing=[])
            if v["run_verdict"] in ("NO_EDGE", "INCONCLUSIVE"):
                assert decisions(run(e))["backtest"] == pr.NOT_READY, (gate, state)

    def test_promotion_is_ready_only_if_the_verdict_is_not_disqualifying(self):
        """The invariant, checked directly over many constructed states."""
        import itertools

        from signal_engine.backtest import verdict as vd

        gates = ["G0.5", "G1.2", "G2.1", "G3.2", "G4.1", "G5.1", "G6.9", "G0.7", "G2.4", "G0.1"]
        for combo in itertools.chain(
            [()], itertools.combinations(gates, 1), itertools.combinations(gates, 2)
        ):
            for state in ("fail", "not_evaluated"):
                e = make_eval(**{g.replace(".", "_"): state for g in combo})
                for lim in ([], ["Point-in-time universe membership"]):
                    p = run(e, readiness_missing=lim)
                    v = vd.derive(e, validity=OK, data_quality=DQ_OK, readiness_missing=lim)
                    if p["stages"]["backtest"]["decision"] == pr.READY:
                        assert v["run_verdict"] not in ("NO_EDGE", "INCONCLUSIVE"), (combo, state)
                        assert not [g for g in p["stages"]["backtest"]["gates"]
                                    if g["status"] in ("FAIL", "NOT_RUN")
                                    and g["gate"] not in pr.DIAGNOSTIC_GATES], (combo, state)
                        for g in pr.PAPER_EXEMPT_GATES:
                            if g in combo:
                                assert state != "fail", (combo, state)


class TestPaperEligibility:
    """A promising candidate reaches paper without live-only certification."""

    def test_live_only_gates_do_not_hold_the_paper_step(self):
        e = make_eval(G0_7="not_evaluated", G2_4="not_evaluated")
        p = run(e, readiness_missing=["Point-in-time universe membership"])
        assert decisions(p)["backtest"] == pr.READY
        assert p["stages"]["backtest"]["reasons"] == []

    def test_the_same_gates_still_hold_the_live_stage(self):
        e = make_eval(G0_7="not_evaluated", G2_4="not_evaluated")
        reasons = run(e)["stages"]["live"]["reasons"]
        assert "gate G0.7 has not been run" in reasons
        assert "gate G2.4 has not been run" in reasons

    def test_a_failed_live_only_gate_still_blocks_paper(self):
        """The exemption is from having to run before paper, never from passing once run."""
        for gate in ("G0.7", "G2.4"):
            p = run(make_eval(**{gate.replace(".", "_"): "fail"}))
            assert decisions(p)["backtest"] == pr.NOT_READY, gate
            assert f"gate {gate} failed" in p["stages"]["backtest"]["reasons"]

    def test_a_critical_engine_capability_that_is_unverified_holds_paper(self):
        p = run(make_eval(), readiness_critical=["Run manifest"])
        assert decisions(p)["backtest"] == pr.NOT_READY
        assert "critical engine capability not verified: Run manifest" in p["stages"]["backtest"]["reasons"]

    def test_an_unrelated_limitation_does_not(self):
        assert decisions(run(make_eval(), readiness_missing=["Regime table"]))["backtest"] == pr.READY

    def test_the_exempt_set_is_exactly_the_documented_one(self):
        assert set(pr.PAPER_EXEMPT_GATES) == {"G0.7", "G2.4"}
        for gate, why in pr.PAPER_EXEMPT_GATES.items():
            assert why and pr.stage_of(gate) == "backtest"
