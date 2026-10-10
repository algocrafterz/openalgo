"""G6.8 evaluator: no position is open when the closing auction (15:15 IST) starts."""

from __future__ import annotations

import dataclasses

import pandas as pd

from signal_engine.backtest import evaluation as ev
from signal_engine.tests.test_evaluation_stats_gates import _evaluate, _noisy_book, _row

SESSION = {"mis_squareoff_min": 900, "squareoff_margin_min": 15, "bar_minutes": 5}


def _book(exit_hhmm="14:30", n=10, **fields):
    return [
        dataclasses.replace(t, exit_time=pd.Timestamp(f"{t.day} {exit_hhmm}"), **fields)
        for t in _noisy_book(n=n)
    ]


def _g68(trades=None, session=SESSION):
    kw = {} if session is None else {"session": session}
    return _row(_evaluate(trades if trades is not None else _book(), **kw), "G6.8")


class TestG68ClosingAuction:
    def test_a_book_flat_well_before_the_auction_passes(self):
        row = _g68()
        assert row["status"] == "pass" and row["hard"] is False
        assert "standing time exit is 14:45" in row["evidence"]

    def test_an_exit_bar_closing_exactly_at_the_auction_start_passes(self):
        # Bars are stamped at their open: the 15:10 bar closes 15:15, when the auction starts.
        assert _g68(_book("15:10"))["status"] == "pass"

    def test_the_first_bar_on_the_grid_that_closes_inside_the_auction_fails(self):
        row = _g68(_book("15:15"))  # closes 15:20
        assert row["status"] == "fail" and "latest exit completes 15:20" in row["evidence"]

    def test_an_exit_bar_closing_after_the_auction_start_fails(self):
        row = _g68(_book("15:11"))
        assert row["status"] == "fail" and "10 of 10 trades still open" in row["evidence"]

    def test_an_exit_at_the_open_is_flat_at_the_stamp(self):
        assert _g68(_book("15:14", exit_at_open=True))["status"] == "pass"

    def test_a_per_symbol_bar_size_is_used(self):
        session = {**SESSION, "bar_minutes_by_symbol": {"A.NS": 15}}
        assert _g68(_book("15:05"), session)["status"] == "fail"

    def test_a_time_exit_planned_inside_the_auction_fails_even_when_trades_are_flat(self):
        session = {"mis_squareoff_min": 940, "squareoff_margin_min": 5, "bar_minutes": 5}
        row = _g68(_book("14:30"), session)
        assert row["status"] == "fail" and "lies in the auction" in row["evidence"]

    def test_an_unrecorded_exit_plan_is_stated_not_assumed(self):
        row = _g68(session={"bar_minutes": 5})
        assert row["status"] == "pass" and "time exit is not recorded" in row["evidence"]

    def test_no_session_or_no_exit_times_is_not_evaluated(self):
        assert _g68(session=None)["status"] == "not_evaluated"
        open_trades = [dataclasses.replace(t, exit_time=None) for t in _noisy_book(n=5)]
        assert _g68(open_trades)["status"] == "not_evaluated"

    def test_it_is_registered(self):
        assert "G6.8" in ev._EVALUATORS
