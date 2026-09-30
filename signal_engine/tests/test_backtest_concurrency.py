"""Concurrency profile and position cap for a trade list.

An uncapped multi-day strategy can hold dozens of positions at once (EMA pullback wanted a
median of 54 and a peak of 138), which no real account can. Its per-trade edge then only
exists for an account that takes every signal. These tests pin the two tools that expose
that: how many positions were open at each entry, and what is left when only N slots exist.
"""

import pandas as pd
import pytest

from signal_engine.backtest.concurrency import apply_position_cap, concurrency_profile
from signal_engine.backtest.types import Trade


def _t(symbol, entry, exit_, r_net=1.0):
    return Trade(
        symbol=symbol,
        day=pd.Timestamp(entry).date(),
        direction=1,
        tag="t",
        entry_time=pd.Timestamp(entry),
        entry=100.0,
        signal_price=100.0,
        sl=99.0,
        tp=0.0,
        risk=1.0,
        exit_time=pd.Timestamp(exit_),
        reason="x",
        r_gross=r_net,
        r_net=r_net,
    )


def _overlapping(n=4):
    """n trades that all open on consecutive days and all close on day 20."""
    return [_t(f"S{i}", f"2026-01-{i + 1:02d}", "2026-01-20") for i in range(n)]


class TestProfile:
    def test_counts_positions_open_at_each_entry_including_itself(self):
        p = concurrency_profile(_overlapping(4))
        assert p["peak"] == 4
        assert p["median"] == 2.5  # counts at entries are 1, 2, 3, 4

    def test_a_position_closed_before_the_next_entry_is_not_counted(self):
        trades = [_t("A", "2026-01-01", "2026-01-05"), _t("B", "2026-01-10", "2026-01-12")]
        assert concurrency_profile(trades)["peak"] == 1

    def test_exit_at_the_entry_moment_frees_the_slot(self):
        trades = [_t("A", "2026-01-01", "2026-01-05"), _t("B", "2026-01-05", "2026-01-09")]
        assert concurrency_profile(trades)["peak"] == 1

    def test_empty_list(self):
        assert concurrency_profile([]) == {"peak": 0, "median": 0.0}

    def test_input_order_does_not_matter(self):
        ts = _overlapping(6)
        assert concurrency_profile(ts) == concurrency_profile(list(reversed(ts)))

    def test_trade_without_exit_time_is_treated_as_still_open(self):
        open_trade = _t("A", "2026-01-01", "2026-01-02")
        open_trade.exit_time = None
        p = concurrency_profile([open_trade, _t("B", "2026-03-01", "2026-03-02")])
        assert p["peak"] == 2


class TestCap:
    def test_cap_limits_simultaneous_positions(self):
        kept = apply_position_cap(_overlapping(4), 2)
        assert len(kept) == 2
        assert concurrency_profile(kept)["peak"] == 2

    def test_a_freed_slot_is_reused_by_a_later_signal(self):
        trades = [
            _t("A", "2026-01-01", "2026-01-05"),
            _t("B", "2026-01-02", "2026-01-20"),
            _t("C", "2026-01-03", "2026-01-20"),  # blocked: A and B fill both slots
            _t("D", "2026-01-06", "2026-01-20"),  # A has exited, slot free
        ]
        kept = {t.symbol for t in apply_position_cap(trades, 2)}
        assert kept == {"A", "B", "D"}

    def test_cap_at_or_above_peak_keeps_everything(self):
        ts = _overlapping(4)
        assert len(apply_position_cap(ts, 4)) == 4
        assert len(apply_position_cap(ts, 50)) == 4

    def test_same_moment_ties_are_broken_deterministically_not_by_input_order(self):
        ts = [_t(f"S{i}", "2026-01-01", "2026-01-10") for i in range(8)]
        a = {t.symbol for t in apply_position_cap(ts, 3)}
        b = {t.symbol for t in apply_position_cap(list(reversed(ts)), 3)}
        assert a == b and len(a) == 3

    def test_result_is_chronological_and_inputs_are_not_modified(self):
        ts = list(reversed(_overlapping(4)))
        before = [t.symbol for t in ts]
        kept = apply_position_cap(ts, 3)
        assert [t.symbol for t in ts] == before
        assert [t.entry_time for t in kept] == sorted(t.entry_time for t in kept)

    def test_empty_list(self):
        assert apply_position_cap([], 5) == []

    @pytest.mark.parametrize("bad", [0, -1])
    def test_cap_below_one_is_rejected(self, bad):
        with pytest.raises(ValueError, match="max_positions"):
            apply_position_cap(_overlapping(2), bad)
