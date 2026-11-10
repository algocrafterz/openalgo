"""Engine 1.6 same-bar stop/target ambiguity flag.

When one bar reaches both the stop and the target and did not open through the stop, the engine
keeps the conservative resolution (stop taken) and marks Trade.exit_ambiguous. A gap through the
stop is not ambiguous. The target is never taken in preference to the stop.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest.engine import simulate
from signal_engine.backtest.swing_engine import SwingRunConfig, simulate_swing
from signal_engine.backtest.types import Ctx, EntrySignal, Trade
from signal_engine.strategy_core.exits import ExitPlan, Ladder
from signal_engine.tests.test_backtest_missing_bar_guard import (
    FLAT100,
    RUN,
    FireOnce,
    contiguous,
    frame,
)


def go(rows, strat, run=RUN, n=None):
    return simulate(Ctx(frame(rows, contiguous(len(rows))), "T"), strat, None, run)


class TestIntradayStopTarget:
    def test_bar_reaching_both_takes_the_stop_and_flags_it(self):
        # signal bar 1, fill bar 2 @100; bar 3 spans stop 95 and target 105 and opens inside
        rows = [FLAT100, FLAT100, FLAT100, (100.0, 106.0, 94.0, 100.0), FLAT100]
        (t,) = go(rows, FireOnce(at=1, sl=95.0, tp=105.0))
        assert t.reason == "SL" and t.exit == 95.0
        assert t.exit_ambiguous is True

    def test_short_side_is_flagged_too(self):
        rows = [FLAT100, FLAT100, FLAT100, (100.0, 106.0, 94.0, 100.0), FLAT100]
        (t,) = go(rows, FireOnce(at=1, sl=105.0, tp=95.0, direction=-1))
        assert t.reason == "SL" and t.exit == 105.0 and t.exit_ambiguous is True

    def test_fill_bar_that_reaches_both_is_flagged(self):
        rows = [FLAT100, FLAT100, (100.0, 106.0, 94.0, 100.0), FLAT100]
        (t,) = go(rows, FireOnce(at=1, sl=95.0, tp=105.0))
        assert t.reason == "SL" and t.exit_ambiguous is True

    def test_gap_through_the_stop_is_not_ambiguous_even_if_the_range_reaches_target(self):
        # bar 3 opens at 90 (through stop 95) and rallies to 106 (through target 105)
        rows = [FLAT100, FLAT100, FLAT100, (90.0, 106.0, 89.0, 100.0), FLAT100]
        (t,) = go(rows, FireOnce(at=1, sl=95.0, tp=105.0))
        assert t.reason == "SL_GAP" and t.exit == 90.0
        assert t.exit_ambiguous is False

    def test_stop_only_target_only_and_time_exit_are_not_flagged(self):
        stop_only = [FLAT100, FLAT100, FLAT100, (100.0, 101.0, 94.0, 96.0), FLAT100]
        (t,) = go(stop_only, FireOnce(at=1, sl=95.0, tp=105.0))
        assert t.reason == "SL" and t.exit_ambiguous is False
        tp_only = [FLAT100, FLAT100, FLAT100, (100.0, 106.0, 99.0, 104.0), FLAT100]
        (t,) = go(tp_only, FireOnce(at=1, sl=95.0, tp=105.0))
        assert t.reason == "TP" and t.exit_ambiguous is False
        (t,) = go([FLAT100] * 6, FireOnce(at=1, sl=95.0, tp=105.0))
        assert t.exit_ambiguous is False

    def test_never_resolved_in_favour_of_the_target(self):
        rows = [FLAT100, FLAT100, FLAT100, (100.0, 200.0, 0.5, 100.0), FLAT100]
        (t,) = go(rows, FireOnce(at=1, sl=95.0, tp=105.0))
        assert t.exit == 95.0 and t.r_gross < 0


class _Dyn(FireOnce):
    dynamic_target = True

    def __init__(self, at, sl, tp, level, direction=1):
        super().__init__(at, sl, tp, direction)
        self.level = level

    def target(self, c, j, p, pos):
        return self.level


class TestDynamicTarget:
    def test_both_reached_takes_the_stop_and_flags_it(self):
        rows = [FLAT100, FLAT100, FLAT100, (100.0, 106.0, 94.0, 100.0), FLAT100]
        (t,) = go(rows, _Dyn(at=1, sl=95.0, tp=130.0, level=105.0))
        assert t.reason == "SL" and t.exit == 95.0 and t.exit_ambiguous is True

    def test_target_only_bar_is_not_flagged(self):
        rows = [FLAT100, FLAT100, FLAT100, (100.0, 106.0, 99.0, 104.0), FLAT100]
        (t,) = go(rows, _Dyn(at=1, sl=95.0, tp=130.0, level=105.0))
        assert t.reason == "TP" and t.exit_ambiguous is False


class _Ladder(FireOnce):
    def entry(self, c, i, p, d):
        if i != self.at or d != 1 or self.fired:
            return None
        px = float(c["Close"][i])
        return EntrySignal(1, px - 5.0, px + 5.0, "L", meta={"targets": (px + 5.0, px + 10.0)})


class TestLadder:
    PLAN = ExitPlan((0.5, 0.5), "none")

    def test_ladder_step_flags_a_stop_and_target_in_one_bar(self):
        lad = Ladder(1, 100.0, 95.0, (105.0, 110.0), self.PLAN)
        lad.step(100.0, 106.0, 94.0, 100.0)
        assert lad.fills[-1].reason == "SL" and lad.ambiguous is True

    def test_ladder_step_gap_through_stop_is_not_ambiguous(self):
        lad = Ladder(1, 100.0, 95.0, (105.0, 110.0), self.PLAN)
        lad.step(90.0, 106.0, 89.0, 100.0)
        assert lad.fills[-1].reason == "SL_GAP" and lad.ambiguous is False

    def test_engine_marks_the_ladder_trade(self):
        rows = [FLAT100, FLAT100, FLAT100, (100.0, 106.0, 94.0, 100.0), FLAT100]
        (t,) = go(rows, _Ladder(at=1, sl=0, tp=0), RUN.with_(exit_plan=self.PLAN))
        assert t.exit == 95.0 and t.exit_ambiguous is True

    def test_target_hit_then_same_bar_stop_is_flagged(self):
        plan = ExitPlan((0.5, 0.5), "breakeven", same_bar_stop=True)
        lad = Ladder(1, 100.0, 95.0, (105.0, 110.0), plan)
        lad.step(100.0, 106.0, 99.0, 100.0)  # TP1, stop to 100, low 99 <= 100
        assert lad.done and lad.ambiguous is True

    def test_clean_target_bar_is_not_flagged(self):
        lad = Ladder(1, 100.0, 95.0, (105.0, 110.0), self.PLAN)
        lad.step(100.0, 106.0, 99.0, 100.0)
        assert lad.ambiguous is False


def _daily(rows):
    idx = pd.bdate_range("2026-01-05", periods=len(rows), tz="Asia/Kolkata")
    df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx, dtype="float64")
    df["Volume"] = 1000.0
    df["day"] = pd.Series(df.index.date, index=df.index)
    return df


class _SwingOnce:
    name = "swing-once"

    def __init__(self, at, sl, tp):
        self.at, self.sl, self.tp = at, sl, tp

    def reset_symbol(self, p):
        pass

    def reset_session(self, p):
        pass

    def entry_at_open(self, c, i, p, d):
        if d == 1 and i == self.at:
            return EntrySignal(1, self.sl, self.tp, "S")
        return None

    def on_entry(self, c, i, p, sig):
        pass

    def on_bar(self, c, i, p):
        pass

    def trail(self, c, i, p, pos):
        return None

    def custom_exit(self, c, i, p, pos):
        return None


class TestSwing:
    CFG = SwingRunConfig(min_sl_pct=0.0)

    def test_swing_bar_reaching_both_is_flagged(self):
        rows = [
            (100.0, 101.0, 99.0, 100.0),
            (100.0, 101.0, 99.0, 100.0),
            (100.0, 106.0, 94.0, 100.0),
        ]
        (t,) = simulate_swing(Ctx(_daily(rows), "S"), _SwingOnce(1, 95.0, 105.0), None, self.CFG)
        assert t.reason == "SL" and t.exit == 95.0 and t.exit_ambiguous is True

    def test_swing_gap_through_stop_is_not_flagged(self):
        rows = [
            (100.0, 101.0, 99.0, 100.0),
            (100.0, 101.0, 99.0, 100.0),
            (90.0, 106.0, 89.0, 100.0),
        ]
        (t,) = simulate_swing(Ctx(_daily(rows), "S"), _SwingOnce(1, 95.0, 105.0), None, self.CFG)
        assert t.reason == "SL_GAP" and t.exit_ambiguous is False

    def test_swing_trade_has_no_data_gap_flag(self):
        rows = [(100.0, 101.0, 99.0, 100.0)] * 3
        (t,) = simulate_swing(Ctx(_daily(rows), "S"), _SwingOnce(1, 95.0, math.inf), None, self.CFG)
        assert t.data_gap is False and isinstance(t, Trade)


def test_np_scalar_flags_are_plain_bools():
    rows = [FLAT100, FLAT100, FLAT100, (100.0, 106.0, 94.0, 100.0), FLAT100]
    (t,) = go(rows, FireOnce(at=1, sl=95.0, tp=105.0))
    assert type(t.exit_ambiguous) is bool and type(t.data_gap) is bool
    assert not isinstance(t.exit_ambiguous, np.bool_)
    assert pytest.approx(t.r_gross) == -1.0


# ---- the run-record blocks --------------------------------------------------------------------


def _three_trades():
    """One ambiguous stop (-1R, target would be +1R), one clean target (+1R), one clean stop."""
    amb = [FLAT100, FLAT100, FLAT100, (100.0, 106.0, 94.0, 100.0), FLAT100]
    win = [FLAT100, FLAT100, FLAT100, (100.0, 106.0, 99.0, 104.0), FLAT100]
    lose = [FLAT100, FLAT100, FLAT100, (100.0, 101.0, 94.0, 96.0), FLAT100]
    out = []
    for rows in (amb, win, lose):
        out += go(rows, FireOnce(at=1, sl=95.0, tp=105.0))
    return out


class TestExecutionWarningsBlock:
    def test_counts_shares_and_the_labelled_upper_bound(self):
        from signal_engine.backtest.execution_warnings import BOUND_LABEL, execution_warnings

        trades = _three_trades()
        assert [t.exit_ambiguous for t in trades] == [True, False, False]
        trades[2].data_gap = True
        block = execution_warnings(trades, entries_skipped_missing_bar=4)
        assert block["trades"] == 3
        assert block["ambiguous_exit_trades"] == 1
        assert block["ambiguous_exit_share"] == pytest.approx(1 / 3, abs=1e-6)
        assert block["data_gap_trades"] == 1 and block["entries_skipped_missing_bar"] == 4
        s = block["ambiguous_sensitivity"]
        assert s["label"] == BOUND_LABEL and "UPPER BOUND" in s["label"]
        assert s["trades"] == 1
        assert s["actual_mean_net_r"] == pytest.approx(-1.0)  # cost 0: the stop, hand-checked
        assert s["upper_bound_mean_net_r"] == pytest.approx(1.0)  # (105 - 100) / 5
        # whole run: actual (-1 + 1 - 1) / 3, bound (1 + 1 - 1) / 3
        assert s["run_mean_net_r_actual"] == pytest.approx(-1 / 3, abs=1e-6)
        assert s["run_mean_net_r_upper_bound"] == pytest.approx(1 / 3, abs=1e-6)
        assert s["exact"] is True

    def test_dynamic_target_marks_the_bound_inexact(self):
        from signal_engine.backtest.execution_warnings import execution_warnings

        block = execution_warnings(_three_trades(), dynamic_target=True)
        assert block["ambiguous_sensitivity"]["exact"] is False

    def test_a_run_without_ambiguity_reports_zero_and_no_bound(self):
        from signal_engine.backtest.execution_warnings import execution_warnings

        block = execution_warnings(_three_trades()[1:])
        assert block["ambiguous_exit_trades"] == 0
        assert block["ambiguous_sensitivity"]["upper_bound_mean_net_r"] is None

    def test_empty_run_has_zero_shares(self):
        from signal_engine.backtest.execution_warnings import execution_warnings

        block = execution_warnings([])
        assert block["trades"] == 0 and block["ambiguous_exit_share"] == 0.0

    def test_cost_is_kept_in_the_bound(self):
        from signal_engine.backtest.execution_warnings import execution_warnings

        (t, *_) = _three_trades()
        t.r_net = t.r_gross - 0.1  # 0.1R of cost
        s = execution_warnings([t])["ambiguous_sensitivity"]
        assert s["upper_bound_mean_net_r"] == pytest.approx(0.9)

    def test_build_run_record_carries_both_blocks_and_the_registry_accepts_them(self):
        from signal_engine.backtest.tearsheet import build_run_record
        from signal_engine.research.build_registry import _validate_run_v2

        record = build_run_record(
            _three_trades(),
            run_id="r",
            run_date="2026-10-05",
            code_version="v",
            data={"source": "t"},
            period={"start": "2026-06-04", "end": "2026-06-04"},
            costs={"round_trip_bps": 0.0, "model": "flat"},
            entries_skipped_missing_bar=2,
            lookahead_guard={
                "mode": "full",
                "lookahead_check": "run",
                "symbols_checked": 1,
                "sessions_checked": 3,
                "violations": 0,
            },
        )
        assert record["execution_warnings"]["entries_skipped_missing_bar"] == 2
        assert record["lookahead_guard"]["mode"] == "full"
        assert _validate_run_v2(record) == []

    def test_a_record_written_before_engine_1_6_still_validates(self):
        from signal_engine.research.build_registry import _validate_run_v2

        assert _validate_run_v2({"n_trials": 1, "promoted": False}) == []

    def test_malformed_blocks_are_rejected(self):
        from signal_engine.research.build_registry import _validate_run_v2

        assert _validate_run_v2({"execution_warnings": {"trades": -1}})
        assert _validate_run_v2({"execution_warnings": []})
        assert _validate_run_v2({"lookahead_guard": {"mode": "sometimes"}})
        assert _validate_run_v2({"lookahead_guard": {"mode": "full", "lookahead_check": "run"}})

    def test_both_blocks_reach_the_run_artifact_report(self):
        from signal_engine.backtest import run_artifact

        assert "execution_warnings" in run_artifact._REPORT_BLOCKS
        assert "lookahead_guard" in run_artifact._REPORT_BLOCKS
        assert run_artifact.ENGINE_VERSION == "1.6"
