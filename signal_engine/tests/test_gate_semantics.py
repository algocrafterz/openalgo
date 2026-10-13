"""Gate semantics: classification, outcome vocabulary and the three-question assessment."""

from __future__ import annotations

import pytest

from signal_engine.backtest import evaluation as ev
from signal_engine.backtest import gate_semantics as gs
from signal_engine.backtest import promotion as pr
from signal_engine.backtest import verdict as vd
from signal_engine.tests.test_verdict import DQ_OK, OK, make_eval

GATE_IDS = [g[0] for g in ev._GATES]


def _row(status, gate="G2.1"):
    return {"gate": gate, "status": status, "hard": False}


def _assess(evaluation, *, validity=OK, data_quality=DQ_OK, missing=(), critical=()):
    """Mirror run_artifact._derived: critical rows hold the verdict, the rest qualify it."""
    limitations = [m for m in missing if m not in critical]
    verdict = vd.derive(
        evaluation,
        validity=validity,
        data_quality=data_quality,
        readiness_missing=list(critical),
        qualifications=limitations,
    )
    promo = pr.evaluate_promotion(
        evaluation,
        code_dirty=False,
        reproducible=True,
        readiness_missing=[*missing, *critical],
        validity=validity,
        data_quality=data_quality,
        run_status="VALID",
        readiness_critical=list(critical),
    )
    return (
        gs.assess(
            evaluation,
            verdict=verdict,
            promotion=promo,
            validity=validity,
            data_quality=data_quality,
        ),
        verdict,
    )


class TestMatrixCompleteness:
    def test_every_gate_has_exactly_one_class(self):
        assert set(gs.GATE_CLASS) == set(GATE_IDS)
        assert set(gs.GATE_CLASS.values()) <= set(gs.CLASSES)

    def test_every_gate_has_a_not_evaluated_cause(self):
        assert set(gs.NOT_EVALUATED_CAUSE) == set(GATE_IDS)
        assert set(gs.NOT_EVALUATED_CAUSE.values()) <= set(gs.CAUSES)

    def test_order_conformance_and_process_gates_do_not_invalidate_the_simulation(self):
        """Tick size concerns live orders and test coverage is a process attestation; neither
        is a validity failure in the criteria, so neither may turn a result INCONCLUSIVE."""
        assert gs.GATE_CLASS["G6.4"] == "promotion_operational"
        assert gs.GATE_CLASS["G0.7"] == "promotion_operational"

    def test_an_unverifiable_cost_basis_is_an_integrity_matter(self):
        """A stale charge schedule means the net figures cannot be trusted."""
        assert gs.GATE_CLASS["G6.7"] == "integrity"

    def test_matrix_rows_cover_every_gate_in_gate_order(self):
        rows = gs.matrix_rows()
        assert [r["gate"] for r in rows] == sorted(GATE_IDS, key=pr.gate_order)
        assert {"gate", "title", "class", "hard", "not_evaluated_means", "stage"} <= set(rows[0])


class TestOutcomeVocabulary:
    @pytest.mark.parametrize(
        ("status", "outcome"),
        [
            ("pass", "PASS"),
            ("fail", "FAIL"),
            ("not_applicable", "NOT_APPLICABLE"),
            ("report", "REPORT"),
        ],
    )
    def test_decided_statuses_map_directly(self, status, outcome):
        assert gs.outcome(_row(status)) == outcome

    def test_too_little_data_is_inconclusive_not_blocked(self):
        assert gs.outcome(_row("not_evaluated", "G3.3")) == "INCONCLUSIVE"

    def test_missing_prerequisite_is_blocked_not_inconclusive(self):
        assert gs.outcome(_row("not_evaluated", "G5.1")) == "BLOCKED"  # stage not run
        assert gs.outcome(_row("not_evaluated", "G0.1")) == "BLOCKED"  # attestation missing
        assert gs.outcome(_row("not_evaluated", "G0.4")) == "BLOCKED"  # data missing

    def test_a_gate_that_cannot_resolve_in_a_backtest_is_blocked(self):
        assert gs.outcome(_row("not_evaluated", "G7.1")) == "BLOCKED"

    def test_unknown_gate_not_evaluated_is_blocked(self):
        assert gs.outcome(_row("not_evaluated", "Z.9")) == "BLOCKED"

    def test_missing_evidence_is_never_pass_or_fail(self):
        for gate in GATE_IDS:
            assert gs.outcome(_row("not_evaluated", gate)) in ("BLOCKED", "INCONCLUSIVE")


class TestAssessment:
    def test_has_the_three_questions(self):
        a, _ = _assess(make_eval())
        assert set(a) >= {"research_validity", "strategy_evidence", "promotion_readiness", "gates"}

    def test_all_pass_is_valid_edge_and_ready(self):
        a, v = _assess(make_eval())
        assert a["research_validity"]["status"] == "PASS"
        assert a["strategy_evidence"]["status"] == "EDGE"
        assert v["run_verdict"] == "EDGE"
        assert a["promotion_readiness"]["backtest"] == pr.READY

    def test_invalid_run_is_not_a_negative_strategy_result(self):
        bad = {"ok": False, "failures": [{"check": "lineage", "detail": "x"}]}
        a, v = _assess(make_eval(G1_2="fail"), validity=bad)
        assert a["research_validity"]["status"] == "FAIL"
        assert a["strategy_evidence"]["status"] == "INCONCLUSIVE"
        assert v["run_verdict"] == "INCONCLUSIVE"
        assert a["promotion_readiness"]["backtest"] == pr.NOT_READY

    def test_failed_integrity_gate_makes_the_verdict_inconclusive_not_marginal(self):
        a, v = _assess(make_eval(G0_5="fail"))
        assert v["run_verdict"] == "INCONCLUSIVE"
        assert a["research_validity"]["status"] == "FAIL"
        assert a["strategy_evidence"]["status"] == "INCONCLUSIVE"
        assert any("G0.5" in r["text"] for r in v["reasons"])

    def test_broken_execution_assumption_is_inconclusive_even_if_hard(self):
        """G6.9 (a trade open past the broker square-off) means the simulation is wrong,
        so the performance numbers say nothing about the strategy: not NO_EDGE."""
        _, v = _assess(make_eval(G6_9="fail"))
        assert v["run_verdict"] == "INCONCLUSIVE"

    def test_off_tick_stop_on_a_losing_strategy_stays_no_edge(self):
        """IDEA-029 shape: clearly negative, with 2 of 26,144 stops off the tick grid."""
        _, v = _assess(make_eval(G1_2="fail", G6_4="fail"))
        assert v["run_verdict"] == "NO_EDGE"

    def test_off_tick_stops_alone_never_claim_no_edge_but_still_block_promotion(self):
        """An off-grid trigger is an execution-modelling defect worth at most one tick, not
        evidence about the strategy: the claim stays below EDGE and the run cannot advance
        until it is recorded again with the stop snapped onto the dated grid."""
        a, v = _assess(make_eval(G6_4="fail"))
        assert v["run_verdict"] == "MARGINAL"
        assert a["promotion_readiness"]["backtest"] == pr.NOT_READY

    def test_integrity_failure_outranks_a_hard_strategy_failure(self):
        _, v = _assess(make_eval(G0_5="fail", G1_2="fail"))
        assert v["run_verdict"] == "INCONCLUSIVE"

    def test_missing_attestation_blocks_validity_but_strategy_diagnostics_stay_available(self):
        a, v = _assess(make_eval(G0_1="not_evaluated"))
        assert v["run_verdict"] == "MARGINAL"
        assert a["research_validity"]["status"] == "BLOCKED"
        assert a["strategy_evidence"]["status"] == "PROMISING"
        assert a["promotion_readiness"]["backtest"] == pr.NOT_READY

    def test_hard_strategy_failure_is_no_edge_and_validity_is_independent(self):
        a, v = _assess(make_eval(G1_2="fail"))
        assert a["strategy_evidence"]["status"] == "NO_EDGE"
        assert v["run_verdict"] == "NO_EDGE"
        assert a["research_validity"]["status"] == "PASS"

    def test_too_few_trades_is_inconclusive(self):
        a, v = _assess(make_eval(G2_1="fail"))
        assert a["strategy_evidence"]["status"] == "INCONCLUSIVE"
        assert v["run_verdict"] == "INCONCLUSIVE"

    def test_a_scoped_limitation_qualifies_a_clean_result_but_it_is_still_edge(self):
        a, v = _assess(make_eval(), missing=["Point-in-time universe membership"])
        assert v["run_verdict"] == "EDGE"
        assert v["qualifications"] == ["Point-in-time universe membership"]
        assert a["strategy_evidence"]["status"] == "EDGE"
        assert a["research_validity"]["status"] == "PASS"
        assert a["research_validity"]["limitations"] == ["Point-in-time universe membership"]
        assert a["promotion_readiness"]["backtest"] == pr.READY
        assert a["promotion_readiness"]["live"] == pr.READY

    def test_an_unverified_critical_capability_holds_a_clean_result_at_promising(self):
        a, v = _assess(make_eval(), critical=["Run manifest"])
        assert v["run_verdict"] == "MARGINAL"
        assert a["strategy_evidence"]["status"] == "PROMISING"
        assert a["promotion_readiness"]["backtest"] == pr.NOT_READY

    def test_open_strategy_gate_is_marginal_not_promising(self):
        a, _ = _assess(make_eval(G3_2="not_evaluated"))
        assert a["strategy_evidence"]["status"] == "MARGINAL"
        assert a["promotion_readiness"]["backtest"] == pr.NOT_READY

    def test_open_risk_gate_is_marginal_not_promising(self):
        a, _ = _assess(make_eval(G5_1="not_evaluated"))
        assert a["strategy_evidence"]["status"] == "MARGINAL"

    def test_gate_outcomes_list_every_non_pass_with_class_and_cause(self):
        a, _ = _assess(make_eval(G5_1="not_evaluated", G3_3="not_evaluated"))
        by_gate = {g["gate"]: g for g in a["gates"]["open"]}
        assert by_gate["G5.1"]["outcome"] == "BLOCKED"
        assert by_gate["G5.1"]["class"] == "risk_constraint"
        assert by_gate["G3.3"]["outcome"] == "INCONCLUSIVE"
        assert a["gates"]["counts"]["PASS"] > 0

    @pytest.mark.parametrize(
        "overrides",
        [
            {},
            {"G1_2": "fail"},
            {"G2_1": "fail"},
            {"G3_2": "not_evaluated"},
            {"G0_5": "fail"},
            {"G0_1": "not_evaluated"},
            {"G5_1": "fail"},
            {"G2_2": "fail"},
            {"G6_9": "fail"},
        ],
    )
    def test_assessment_never_contradicts_the_stored_verdict(self, overrides):
        a, v = _assess(make_eval(**overrides))
        evidence, legacy = a["strategy_evidence"]["status"], v["run_verdict"]
        if legacy == "MARGINAL":
            assert evidence in ("MARGINAL", "PROMISING")
        else:
            assert evidence == legacy


class TestRequiredGatesAreNeverInvisible:
    def test_a_failed_required_operational_gate_stops_the_claim_being_promising(self):
        a, v = _assess(make_eval(G0_7="fail"))
        assert v["run_verdict"] == "MARGINAL"
        assert a["strategy_evidence"]["status"] == "MARGINAL"

    def test_an_unrun_operational_gate_does_not(self):
        """G6.4 part B can never be run in a backtest; it must not stop PROMISING for ever."""
        a, _ = _assess(make_eval(G6_4="not_evaluated"))
        assert a["strategy_evidence"]["status"] == "PROMISING"

    def test_a_stale_charge_schedule_makes_the_run_inconclusive(self):
        a, v = _assess(make_eval(G6_7="fail"))
        assert v["run_verdict"] == "INCONCLUSIVE"
        assert a["research_validity"]["status"] == "FAIL"


class TestPaperExemptGatesDoNotHoldPromising:
    """Promotion and the strategy-evidence answer must agree about which gates are pre-live."""

    def test_pbo_not_evaluated_is_promising_not_marginal(self):
        a, _ = _assess(make_eval(G2_4="not_evaluated"))
        assert a["strategy_evidence"]["status"] == "PROMISING"
        assert a["promotion_readiness"]["backtest"] == pr.READY

    def test_the_two_answers_agree_about_the_exempt_gates(self):
        for gate in pr.PAPER_EXEMPT_GATES:
            key = gate.replace(".", "_")
            a, _ = _assess(make_eval(**{key: "not_evaluated"}))
            assert a["promotion_readiness"]["backtest"] == pr.READY
            assert a["strategy_evidence"]["status"] == "PROMISING", gate
            a, _ = _assess(make_eval(**{key: "fail"}))
            assert a["promotion_readiness"]["backtest"] == pr.NOT_READY
            assert a["strategy_evidence"]["status"] == "MARGINAL", gate
