"""G6.3 evaluator: a report of entries whose stop or target lies beyond the two price-band bounds."""

from __future__ import annotations

import dataclasses

from signal_engine.backtest import evaluation as ev
from signal_engine.tests.test_evaluation_stats_gates import _evaluate, _noisy_book, _row


def _book(levels):
    """One trade per (sl, tp) pair on a 100.0 signal price."""
    base = _noisy_book(n=len(levels))
    return [dataclasses.replace(t, sl=sl, tp=tp) for t, (sl, tp) in zip(base, levels, strict=True)]


def _g63(trades):
    return _row(_evaluate(trades), "G6.3")


class TestG63Circuits:
    def test_it_is_evaluated_and_never_blocks(self):
        row = _g63(_book([(99.0, 0.0)] * 10))
        assert row["status"] == "report" and row["hard"] is False

    def test_the_share_beyond_each_bound_is_counted(self):
        # stops 1%, 3%, 12%; no target
        row = _g63(_book([(99.0, 0.0), (97.0, 0.0), (88.0, 0.0)]))
        assert row["value"] == round(2 / 3, 4)
        assert "2 of 3 entries" in row["evidence"] and "1 (33.3%) beyond 10%" in row["evidence"]

    def test_a_far_target_counts_even_with_a_tight_stop(self):
        row = _g63(_book([(99.5, 105.0), (99.5, 101.0)]))
        assert row["value"] == 0.5

    def test_a_level_exactly_on_the_bound_is_not_beyond_it(self):
        assert _g63(_book([(98.0, 0.0)]))["value"] == 0.0

    def test_trades_without_stop_or_target_levels_are_not_evaluated(self):
        nan = float("nan")
        row = _g63(_book([(nan, nan)] * 5))
        assert row["status"] == "not_evaluated"

    def test_no_trades_is_not_evaluated(self):
        assert _row(_evaluate([]), "G6.3")["status"] == "not_evaluated"

    def test_the_evidence_says_it_is_a_bound_not_a_check(self):
        text = _g63(_book([(99.0, 0.0)]))["evidence"]
        assert "bound, not a check" in text and "previous close" in text

    def test_it_is_registered(self):
        assert "G6.3" in ev._EVALUATORS


class TestG63UnderTheVerdictPolicy:
    def test_the_report_status_does_not_block_edge(self):
        from signal_engine.tests.test_verdict_mvp_policy import _derive, _evaluation, vd

        assert _derive(_evaluation("pass", G6_3="report"))["run_verdict"] == vd.EDGE

    def test_extreme_levels_still_report_and_never_fail(self):
        row = _g63(_book([(50.0, 200.0)] * 5))
        assert row["status"] == "report" and row["value"] == 1.0

    def test_the_proxy_and_approximation_limits_are_in_the_evidence(self):
        text = _g63(_book([(99.0, 0.0)]))["evidence"]
        assert "signal price stands in for the previous close" in text
        assert "actual daily bands" in text and "not held" in text
        assert "10%" in _g63(_book([(99.0, 0.0)]))["threshold"]
