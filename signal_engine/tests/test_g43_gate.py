"""G4.3 bar resolution: why the engine's same-bar reading is the pessimistic one, and the evaluator.

The engine checks the stop before the target on every bar (STOP FIRST), so the stop-first and
the pessimistic readings are one rule and the sign cannot flip. The engine tests below prove the
rule on every path that exists; the one path that is not pessimistic (a ladder whose raised stop
applies only from the next bar) is shown too, and the evaluator refuses to call it met."""

from __future__ import annotations

import numpy as np
import pandas as pd

from signal_engine.backtest import evaluation as ev
from signal_engine.backtest.engine import simulate
from signal_engine.backtest.swing_engine import SwingRunConfig, simulate_swing
from signal_engine.backtest.types import Ctx
from signal_engine.strategy_core.exits import ExitPlan, Ladder
from signal_engine.tests.test_backtest_engine import RUN, FireOnce, bars
from signal_engine.tests.test_evaluation_stats_gates import _evaluate, _noisy_book, _row
from signal_engine.tests.test_swing_engine_semantics import frame
from signal_engine.tests.test_backtest_ledgers import SwingScript

FLAT = (100, 101, 99, 100)


def _intraday(rows, strat):
    return simulate(Ctx(bars(rows), "T"), strat, None, RUN, None)


class TestTheEngineTakesTheStopWhenOneBarSpansBoth:
    def test_long_on_a_later_bar(self):
        rows = [FLAT, FLAT, FLAT, (100, 111, 89, 100), FLAT]
        t = _intraday(rows, FireOnce(at=1, sl=90, tp=110))
        assert t[0].reason == "SL" and t[0].exit == 90

    def test_short_on_a_later_bar(self):
        rows = [FLAT, FLAT, FLAT, (100, 111, 89, 100), FLAT]
        t = _intraday(rows, FireOnce(at=1, sl=110, tp=90, direction=-1))
        assert t[0].reason == "SL" and t[0].exit == 110

    def test_on_the_entry_bar_itself(self):
        rows = [FLAT, FLAT, (100, 111, 89, 100), FLAT]
        t = _intraday(rows, FireOnce(at=1, sl=90, tp=110))
        assert t[0].reason == "SL"

    def test_a_bar_that_opens_through_the_stop_fills_at_the_open_not_the_stop(self):
        rows = [FLAT, FLAT, FLAT, (88, 111, 87, 100), FLAT]
        t = _intraday(rows, FireOnce(at=1, sl=90, tp=110))
        assert t[0].reason == "SL_GAP" and t[0].exit == 88

    def test_the_swing_engine_does_the_same_on_a_daily_bar(self):
        df = frame([100.0] * 6)
        df.loc[df.index[3], ["High", "Low"]] = [111.0, 89.0]
        run = simulate_swing(Ctx(df, "A"), SwingScript({1: (1, 90.0, 110.0)}), None, SwingRunConfig())
        assert run[0].reason == "SL"


class TestTheLadder:
    def _step(self, plan, bar):
        ladder = Ladder(1, 100.0, 90.0, (110.0,), plan)
        ladder.step(*bar)
        return ladder

    def test_the_original_stop_is_taken_before_a_target_on_the_same_bar(self):
        ladder = self._step(ExitPlan((1.0,), "none"), (100.0, 111.0, 89.0, 100.0))
        assert [f.reason for f in ladder.fills] == ["SL"]

    def test_a_raised_stop_is_only_pessimistic_when_same_bar_stop_is_true(self):
        # the bar touches target 1 (breakeven then moves the stop to 100) and its low reaches 100
        bar = (101.0, 111.0, 99.0, 105.0)
        opt = self._step(ExitPlan((0.5,), "breakeven"), bar)
        pess = self._step(ExitPlan((0.5,), "breakeven", same_bar_stop=True), bar)
        assert [f.reason for f in opt.fills] == ["TP1"]
        assert [f.reason for f in pess.fills] == ["TP1", "SL"]


def _g43(params):
    return _row(_evaluate(_noisy_book(), params=params), "G4.3")


class TestG43Evaluator:
    def test_a_plain_stop_and_target_run_is_reported_as_stop_first(self):
        row = _g43({"engine": "intraday", "run": {"exit_plan": None}})
        assert row["status"] == "report" and row["value"] == "stop_first" and row["hard"] is False
        assert "sign cannot flip" in row["evidence"] and "not by a second run" in row["evidence"]

    def test_the_swing_engine_is_covered(self):
        assert _g43({"engine": "swing", "run": {}})["status"] == "report"

    def test_a_ladder_that_never_moves_the_stop_is_covered(self):
        plan = {"fractions": [0.3, 0.7], "stop_rule": "none", "same_bar_stop": False}
        assert _g43({"engine": "intraday", "run": {"exit_plan": plan}})["status"] == "report"

    def test_a_ladder_that_closes_everything_at_its_first_target_is_covered(self):
        plan = {"fractions": [1.0], "stop_rule": "breakeven", "same_bar_stop": False}
        assert _g43({"engine": "intraday", "run": {"exit_plan": plan}})["status"] == "report"

    def test_a_raised_stop_taken_the_same_bar_is_covered(self):
        plan = {"fractions": [0.3, 0.35, 0.35], "stop_rule": "tp_buffer", "same_bar_stop": True}
        assert _g43({"engine": "intraday", "run": {"exit_plan": plan}})["status"] == "report"

    def test_a_ladder_with_a_next_bar_raised_stop_is_not_called_met(self):
        plan = {"fractions": [0.3, 0.35, 0.35], "stop_rule": "tp_buffer", "same_bar_stop": False}
        row = _g43({"engine": "intraday", "run": {"exit_plan": plan}})
        assert row["status"] == "not_evaluated" and "optimistic" in row["evidence"]

    def test_unrecorded_exit_rules_are_not_evaluated(self):
        assert _g43(None)["status"] == "not_evaluated"
        assert _g43({"engine": "intraday"})["status"] == "not_evaluated"

    def test_it_is_registered(self):
        assert "G4.3" in ev._EVALUATORS


class TestEveryExitPlanConfiguration:
    """The plan as the run record stores it (`jsonable` of the dataclass), over the whole matrix."""

    @staticmethod
    def _record(plan):
        from signal_engine.backtest.record_run import jsonable

        return {"engine": "intraday", "run": jsonable(RUN.with_(exit_plan=plan))}

    def test_the_named_production_plans(self):
        from signal_engine.pipeline.exit_driver import PLANS

        tp1 = _g43(self._record(PLANS["tp1_only"]))
        ladder = _g43(self._record(PLANS["ladder_30_35_35"]))
        assert tp1["status"] == "report"
        assert ladder["status"] == "not_evaluated" and "unverified" in ladder["evidence"]

    def test_every_stop_raising_plan_with_a_next_bar_stop_is_not_evaluated(self):
        for rule in ("breakeven", "tp_buffer"):
            for fractions in ((0.5,), (0.3, 0.7), (0.0, 1.0), (0.3, 0.35, 0.35)):
                row = _g43(self._record(ExitPlan(fractions, rule, same_bar_stop=False)))
                assert row["status"] == "not_evaluated", (rule, fractions)

    def test_the_plans_that_leave_no_optimistic_reading_are_reported(self):
        cases = [
            ExitPlan((0.3, 0.7), "none"),
            ExitPlan((1.0,), "breakeven"),
            ExitPlan((0.3, 0.35, 0.35), "tp_buffer", same_bar_stop=True),
            ExitPlan((0.5,), "breakeven", same_bar_stop=True),
        ]
        for plan in cases:
            assert _g43(self._record(plan))["status"] == "report", plan

    def test_a_plan_that_cannot_be_read_is_never_taken_as_safe(self):
        for plan in ("ladder", [0.5], {"fractions": [0.5]}, {"stop_rule": "breakeven"},
                     {"fractions": ["x"], "stop_rule": "none"}):  # fmt: skip
            row = _g43({"engine": "intraday", "run": {"exit_plan": plan}})
            assert row["status"] == "not_evaluated" and "cannot be read" in row["evidence"], plan

    def test_a_report_makes_no_sign_comparison_claim_beyond_the_identity(self):
        row = _g43(self._record(None))
        assert row["status"] == "report" and row["value"] == "stop_first"
        assert "By construction, not by a second run" in row["evidence"]
        assert "not evaluated" in row["evidence"]  # the ladder case is named as outside it

    def test_the_evidence_separates_the_invariant_from_the_unverified_ladder(self):
        plan = {"fractions": [0.3, 0.35, 0.35], "stop_rule": "tp_buffer", "same_bar_stop": False}
        text = _g43({"engine": "intraday", "run": {"exit_plan": plan}})["evidence"]
        assert "STOP FIRST, pinned by engine tests" in text and "holds for the original stop" in text
        assert "does not cover this plan" in text and "unverified" in text
        assert "no sign comparison was made" in text

    def test_a_not_evaluated_g43_still_blocks_edge(self):
        from signal_engine.tests.test_verdict_mvp_policy import _derive, _evaluation, vd

        result = _derive(_evaluation("pass", G4_3="not_evaluated"))
        assert result["run_verdict"] == vd.MARGINAL and result["primary_failed_gate"] == "G4.3"
        assert _derive(_evaluation("pass", G4_3="report"))["run_verdict"] == vd.EDGE
