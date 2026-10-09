"""G5.2 gate evaluator: reads the sealed live-caps evidence (`live_caps.run_stage`).

It passes only for a completed stage whose primary capped book meets the G1.2 expectancy and the
G2.1 trade count; anything else leaves it not evaluated or failed, never a silent pass."""

from __future__ import annotations

import pytest

from signal_engine.backtest import live_caps as lc
from signal_engine.tests.test_evaluation_stats_gates import _evaluate, _noisy_book, _row


def _evidence(primary_r=0.2, primary_trades=500, other_r=0.3, state=lc.COMPLETE, **extra):
    def row(slots, primary, net_r, trades):
        return {
            "max_open_positions": slots, "primary": primary, "trades": trades, "net_r": net_r,
            "net_bps": 20.0, "win_rate": 0.5, "peak_open_positions": slots,
            "vetoes": {"band": 3, "daily": 1, "weekly": 0, "monthly": 2, "slots": 5, "trades_day": 0},
            "trade_sha256": "x",
        }  # fmt: skip

    ev = {
        "version": 1, "state": state, "diagnostic_not_trials": True,
        "headline": {"trades": 900, "net_r": 0.3, "net_bps": 25.0},
        "base_check": {"trades": 900, "net_r": 0.3, "net_bps": 25.0, "trade_sha256": "y"},
        "passes": [row(1, True, primary_r, primary_trades), row(2, False, other_r, 700), row(3, False, other_r, 800)],
        "assumptions": ["a"], "limitations": ["l"],
    }  # fmt: skip
    ev.update(extra)
    return ev


def _g52(evidence, **kw):
    return _row(_evaluate(_noisy_book(), live_caps=evidence, **kw), "G5.2")


class TestG52Evaluator:
    def test_a_completed_capped_run_that_meets_the_bars_passes(self):
        row = _g52(_evidence())
        assert row["status"] == "pass" and row["value"] == 0.2

    def test_the_expectancy_bar_is_inclusive_and_is_the_g12_bar(self):
        assert _g52(_evidence(primary_r=0.05))["status"] == "pass"
        assert _g52(_evidence(primary_r=0.049))["status"] == "fail"

    def test_the_trade_count_bar_is_the_g21_bar(self):
        assert _g52(_evidence(primary_trades=200))["status"] == "pass"
        assert _g52(_evidence(primary_trades=199))["status"] == "fail"

    def test_only_the_primary_case_decides_not_the_sensitivities(self):
        assert _g52(_evidence(primary_r=0.01, other_r=0.5))["status"] == "fail"
        assert _g52(_evidence(primary_r=0.2, other_r=-0.5))["status"] == "pass"

    @pytest.mark.parametrize("state", [lc.BASE_REPLAY_MISMATCH, lc.UNSUPPORTED, lc.ERROR])
    def test_an_incomplete_stage_is_not_evaluated_and_says_why(self, state):
        row = _g52(_evidence(state=state, reason="boom"))
        assert row["status"] == "not_evaluated" and "boom" in row["evidence"] and row["value"] is None

    def test_malformed_evidence_is_not_evaluated(self):
        for bad in ({"state": "complete"}, {**_evidence(), "passes": []}, {**_evidence(), "limitations": []}):
            assert _g52(bad)["status"] == "not_evaluated"

    def test_no_evidence_is_not_evaluated_and_a_slot_sweep_does_not_stand_in(self):
        caps = [{"max_positions": 1, "trades": 900, "gross_bps": 60.0, "net_bps": 48.0}]
        row = _g52(None, position_caps=caps)
        assert row["status"] == "not_evaluated" and "Information only" in row["evidence"]

    def test_the_evidence_text_states_the_approximations_and_is_not_a_feasibility_claim(self):
        text = _g52(_evidence())["evidence"]
        for word in ("not proof of live feasibility", "pooled", "unrealised", "reproduced the headline"):
            assert word in text

    def test_the_text_reports_the_vetoes_and_the_sensitivities(self):
        text = _g52(_evidence())["evidence"]
        assert "band 3" in text and "slots 5" in text and "2 slots" in text and "3 slots" in text

    def test_a_pass_here_allows_edge_only_when_the_other_required_gates_also_pass(self):
        from signal_engine.tests.test_verdict_mvp_policy import _derive, _evaluation

        assert _derive(_evaluation("pass", G5_2="pass"))["run_verdict"] == "EDGE"
        assert _derive(_evaluation("pass", G5_2="not_evaluated"))["run_verdict"] == "MARGINAL"
