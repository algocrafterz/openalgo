"""MVP verdict policy: which gates decide the backtest verdict, and what cannot bypass them.

Owner decision of 2026-10-09 (docs/BACKTEST-ENGINE-READINESS.md, 'Gate classification for the
backtest verdict'). Three guarantees are tested against the real gate registry:

  1. EDGE needs every required backtest gate to have valid evidence. Any one of them failed or
     not evaluated keeps the verdict below EDGE.
  2. The only gates checked at a later stage are G4.8 and G6.6 (owner decisions of 2026-10-09),
     they are never hard gates or G0 to G3 gates, and the verdict and the promotion block both
     disclose them.
  3. G2.4 is not applicable only for exactly one trial in a ledger that G2.5 confirms.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from signal_engine.backtest import evaluation as ev
from signal_engine.backtest import promotion as pr
from signal_engine.backtest import verdict as vd
from signal_engine.tests.test_evaluation_stats_gates import _evaluate, _noisy_book, _row

OK = {"ok": True, "failures": []}
DQ_OK = {"status": "PASS", "affects_validity": False, "items": []}
V4 = Path(__file__).resolve().parents[1] / (
    "research/ideas/idea-029-vwap-2sd-range-mean-reversion/runs/"
    "2026-10-09-vwap-2sd-range-mean-reversion-v4/report/report.json"
)

#: Required backtest gates that still have no evaluator (they read as not evaluated and hold the
#: verdict at MARGINAL). This list shrinks as each evaluator is built; it must never grow, and
#: a gate must not leave it without a real evaluator and a test.
UNEVALUATED_REQUIRED = {
    "G0.1", "G0.7", "G4.2", "G5.3", "G5.4", "G5.5",
}


def _evaluation(default: str = "pass", **overrides: str) -> dict[str, Any]:
    """An evaluation block over the real registry; `G2_1="fail"` overrides one gate."""
    over = {k.replace("_", "."): v for k, v in overrides.items()}
    dims: dict[str, list[dict[str, Any]]] = {}
    for gate, dim, title, hard, _why in ev._GATES:
        dims.setdefault(dim, []).append(
            {
                "gate": gate,
                "title": title,
                "status": over.get(gate, default),
                "hard": hard,
                "value": None,
                "threshold": "",
                "evidence": "",
            }
        )
    return {
        "version": 2,
        "criteria": "x",
        "horizon": "intraday",
        "dimensions": [{"name": n, "rows": r} for n, r in dims.items()],
        "summary": {},
    }


def _derive(evaluation, **kw):
    args = {"validity": OK, "data_quality": DQ_OK, "readiness_missing": []}
    args.update(kw)
    return vd.derive(evaluation, **args)


REQUIRED = [
    g for g, *_ in ev._GATES if pr.stage_of(g) == "backtest" and g not in pr.DIAGNOSTIC_GATES
]
HARD = {g for g, _d, _t, hard, _w in ev._GATES if hard}


class TestStageOverrides:
    def test_g48_and_g66_are_the_only_exceptions_and_have_rationales(self):
        assert set(pr.STAGE_OVERRIDES) == {"G4.8", "G6.6"}
        for gate, (stage, why) in pr.STAGE_OVERRIDES.items():
            assert stage in pr.STAGES and stage != "backtest"
            assert why.strip(), f"{gate} has no rationale"

    def test_an_override_can_never_hide_a_hard_gate_or_an_early_gate(self):
        registry = {g for g, *_ in ev._GATES}
        for gate in pr.STAGE_OVERRIDES:
            assert gate in registry
            assert gate not in HARD
            assert pr.gate_number(gate) >= 4

    def test_g48_is_checked_at_the_paper_stage_and_its_rationale_does_not_call_it_irrelevant(self):
        assert pr.stage_of("G4.8") == "paper" and "G4.8" not in REQUIRED
        why = pr.STAGE_OVERRIDES["G4.8"][1]
        assert "has not been performed" in why and "unknown" in why
        assert "irrelevant" not in why and "not needed" not in why

    def test_every_other_gate_keeps_the_stage_of_its_number(self):
        for gate, *_ in ev._GATES:
            if gate in pr.STAGE_OVERRIDES:
                continue
            n = pr.gate_number(gate)
            if n is None:  # engine rows outside G<n>.<m> belong to no stage
                assert pr.stage_of(gate) is None
                continue
            want = "backtest" if n <= 6 else "paper" if n <= 8 else "live"
            assert pr.stage_of(gate) == want
        assert pr.stage_of("X.1") is None

    def test_g66_is_checked_at_the_paper_stage(self):
        assert pr.stage_of("G6.6") == "paper"
        assert "G6.6" not in REQUIRED


class TestEdgeNeedsEveryRequiredGate:
    def test_every_required_gate_passing_reaches_edge(self):
        result = _derive(_evaluation("pass"))
        assert result["run_verdict"] == vd.EDGE and result["primary_failed_gate"] is None

    @pytest.mark.parametrize("gate", REQUIRED)
    def test_one_required_gate_not_evaluated_prevents_edge(self, gate):
        result = _derive(_evaluation("pass", **{gate.replace(".", "_"): "not_evaluated"}))
        want = vd.INCONCLUSIVE if gate in vd.MIN_SAMPLE_GATES else vd.MARGINAL
        assert result["run_verdict"] == want
        assert result["primary_failed_gate"] == gate

    @pytest.mark.parametrize("gate", REQUIRED)
    def test_one_required_gate_failed_prevents_edge(self, gate):
        result = _derive(_evaluation("pass", **{gate.replace(".", "_"): "fail"}))
        assert result["run_verdict"] != vd.EDGE
        if gate in HARD:
            assert result["run_verdict"] == vd.NO_EDGE

    def test_a_report_or_not_applicable_row_does_not_block(self):
        evaluation = _evaluation("pass", G2_4="not_applicable", G4_6="report")
        assert _derive(evaluation)["run_verdict"] == vd.EDGE

    def test_unverified_engine_capabilities_also_hold_the_verdict(self):
        result = _derive(_evaluation("pass"), readiness_missing=["Spec schema split"])
        assert result["run_verdict"] == vd.MARGINAL
        assert result["caps_applied"]

    def test_an_invalid_run_is_inconclusive_whatever_the_gates_say(self):
        bad = {"ok": False, "failures": [{"check": "lookahead", "detail": "x"}]}
        assert _derive(_evaluation("pass"), validity=bad)["run_verdict"] == vd.INCONCLUSIVE


class TestPaperStageRequirementsStayVisible:
    def test_g66_not_run_does_not_decide_the_verdict_but_is_disclosed(self):
        result = _derive(_evaluation("pass", G6_6="not_evaluated"))
        assert result["run_verdict"] == vd.EDGE
        text = " ".join(r["text"] for r in result["reasons"])
        assert "G6.6" in [r["gate"] for r in result["reasons"]]
        assert "before the paper stage" in text and "does not decide this verdict" in text

    def test_g48_not_run_does_not_decide_the_verdict_but_is_disclosed(self):
        result = _derive(_evaluation("pass", G4_8="not_evaluated"))
        assert result["run_verdict"] == vd.EDGE
        text = " ".join(r["text"] for r in result["reasons"])
        assert "G4.8" in [r["gate"] for r in result["reasons"]]
        assert "before the paper stage" in text and "event-day analysis has not been performed" in text

    def test_g48_not_run_blocks_paper_promotion_and_is_never_shown_as_passed(self):
        evaluation = _evaluation("pass", G4_8="not_evaluated")
        out = pr.evaluate_promotion(evaluation, code_dirty=False, reproducible=True)
        paper = out["stages"]["paper"]
        assert out["stages"]["backtest"]["decision"] == pr.READY
        assert paper["decision"] == pr.NOT_READY and "gate G4.8 has not been run" in paper["reasons"]
        assert {g["gate"]: g["status"] for g in paper["gates"]}["G4.8"] == "NOT_RUN"

    def test_g48_evaluated_clears_the_paper_block(self):
        evaluation = _evaluation("pass", G4_8="report")
        out = pr.evaluate_promotion(evaluation, code_dirty=False, reproducible=True)
        assert "gate G4.8 has not been run" not in out["stages"]["paper"]["reasons"]
        assert not [r for r in _derive(evaluation)["reasons"] if r["gate"] == "G4.8"]

    def test_g66_passing_adds_no_disclosure(self):
        assert _derive(_evaluation("pass"))["reasons"] == []

    def test_g66_failed_is_disclosed_and_still_does_not_decide_the_backtest_verdict(self):
        result = _derive(_evaluation("pass", G6_6="fail"))
        assert result["run_verdict"] == vd.EDGE
        assert any(r["gate"] == "G6.6" and r["status"] == "FAIL" for r in result["reasons"])

    def test_promotion_blocks_the_paper_stage_on_g66(self):
        evaluation = _evaluation("pass", G6_6="not_evaluated")
        out = pr.evaluate_promotion(evaluation, code_dirty=False, reproducible=True)
        backtest, paper = out["stages"]["backtest"], out["stages"]["paper"]
        assert backtest["decision"] == pr.READY
        assert paper["decision"] == pr.NOT_READY
        assert "gate G6.6 has not been run" in paper["reasons"]
        assert "G6.6" not in {g["gate"] for g in backtest["gates"]}
        assert "G6.6" in {g["gate"] for g in paper["gates"]}

    def test_a_required_backtest_gate_not_run_keeps_the_backtest_stage_not_ready(self):
        evaluation = _evaluation("pass", G5_5="not_evaluated")
        out = pr.evaluate_promotion(evaluation, code_dirty=False, reproducible=True)
        assert out["stages"]["backtest"]["decision"] == pr.NOT_READY
        assert out["stages"]["paper"]["decision"] == pr.BLOCKED


class TestG24Policy:
    LEDGER = {"source": "trials.yaml", "material": 1, "external": 0, "total": 1}

    @staticmethod
    def _g24(n_trials, trials):
        return _row(_evaluate(_noisy_book(), n_trials=n_trials, trials=trials), "G2.4")

    def test_one_trial_in_a_confirmed_ledger_is_not_applicable(self):
        row = self._g24(1, self.LEDGER)
        assert row["status"] == "not_applicable"
        assert "nothing to rank" in row["evidence"]

    def test_one_trial_without_a_ledger_stays_open(self):
        row = self._g24(1, None)
        assert row["status"] == "not_evaluated" and "not confirmed" in row["evidence"]

    def test_a_ledger_from_another_source_stays_open(self):
        assert self._g24(1, {**self.LEDGER, "source": "manual"})["status"] == "not_evaluated"

    def test_a_ledger_total_that_disagrees_stays_open(self):
        assert self._g24(1, {**self.LEDGER, "total": 2})["status"] == "not_evaluated"

    def test_several_confirmed_trials_stay_open_until_pbo_is_computed(self):
        ledger = {"source": "trials.yaml", "material": 2, "external": 0, "total": 2}
        row = self._g24(2, ledger)
        assert row["status"] == "not_evaluated" and "grid" in row["evidence"]

    def test_the_two_outcomes_decide_the_verdict_differently(self):
        applicable = _derive(_evaluation("pass", G2_4="not_applicable"))
        open_ = _derive(_evaluation("pass", G2_4="not_evaluated"))
        assert applicable["run_verdict"] == vd.EDGE
        assert open_["run_verdict"] == vd.MARGINAL and open_["primary_failed_gate"] == "G2.4"


class TestG52IsNotASilentReport:
    """G5.2 asks for a run with the live caps. A generic slot sweep is not that run."""

    CAPS = [
        {"max_positions": 1, "trades": 900, "gross_bps": 60.0, "net_bps": 48.0},
        {"max_positions": 3, "trades": 1500, "gross_bps": 55.0, "net_bps": 40.0},
    ]

    def test_a_flattering_slot_sweep_does_not_evaluate_the_gate(self):
        row = _row(_evaluate(_noisy_book(), position_caps=self.CAPS), "G5.2")
        assert row["status"] == "not_evaluated" and row["value"] is None
        assert "no run with the live caps" in row["evidence"] and "not evidence for this gate" in row["evidence"]

    def test_the_sweep_is_still_shown_for_information(self):
        row = _row(_evaluate(_noisy_book(), position_caps=self.CAPS), "G5.2")
        assert "1 slots: net +48.00 bps" in row["evidence"]

    def test_without_any_cap_run_it_is_not_evaluated_too(self):
        row = _row(_evaluate(_noisy_book()), "G5.2")
        assert row["status"] == "not_evaluated" and "no run with the live caps" in row["evidence"]

    def test_it_blocks_edge_through_the_required_gate_rule(self):
        assert "G5.2" in REQUIRED
        result = _derive(_evaluation("pass", G5_2="not_evaluated"))
        assert result["run_verdict"] == vd.MARGINAL and result["primary_failed_gate"] == "G5.2"


class TestEdgeIsHonestToday:
    def test_the_required_gates_without_an_evaluator_are_the_known_ones(self):
        missing = {g for g in REQUIRED if g not in set(ev._EVALUATORS)}
        assert missing == UNEVALUATED_REQUIRED

    def test_a_clean_positive_book_cannot_reach_edge_while_any_remain(self):
        result = _derive(_evaluate(_noisy_book(n=900, mean=0.4)))
        assert result["run_verdict"] != vd.EDGE


@pytest.mark.skipif(not V4.exists(), reason="IDEA-029 v4 record not present")
class TestIdea029StaysNoEdge:
    @staticmethod
    def _recorded():
        return json.loads(V4.read_text())

    def test_the_recorded_evaluation_still_derives_no_edge(self):
        rec = self._recorded()
        result = vd.derive(
            rec["evaluation"],
            validity=rec["validity"],
            data_quality=rec["data_quality"],
            readiness_missing=[],
        )
        assert result["run_verdict"] == vd.NO_EDGE == rec["verdict"]["run_verdict"]
        assert result["primary_failed_gate"] == rec["verdict"]["primary_failed_gate"]

    def test_the_backtest_stage_is_not_ready_and_later_stages_are_blocked(self):
        rec = self._recorded()
        out = pr.evaluate_promotion(rec["evaluation"], code_dirty=False, reproducible=True)
        assert out["stages"]["backtest"]["decision"] == pr.NOT_READY
        assert out["stages"]["paper"]["decision"] == pr.BLOCKED
        assert out["stages"]["live"]["decision"] == pr.BLOCKED
