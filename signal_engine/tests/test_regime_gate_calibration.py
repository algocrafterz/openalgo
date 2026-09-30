"""Calibration of the X.2 regime-dispersion heuristic: how often does a zero-edge book pass?"""

import numpy as np
import pytest

from signal_engine.backtest import regime_gate_calibration as cal


def _book(seed=0, n_days=400, per_day=3, edge=0.0):
    """Synthetic trades: a day index, a trend label and a vol label per trade, noisy R."""
    rng = np.random.default_rng(seed)
    day = np.repeat(np.arange(n_days), per_day)
    trend_day = rng.choice(["up", "down", "range"], size=n_days)
    vol_day = rng.choice(["high", "mid", "low"], size=n_days)
    r = rng.normal(edge, 1.0, size=day.size)
    return r, day, {"trend": trend_day[day], "vol": vol_day[day]}


class TestMatrices:
    def test_sums_and_counts_by_day_and_regime(self):
        r = np.array([1.0, 2.0, 3.0, 4.0])
        day = np.array([0, 0, 1, 1])
        labels = np.array(["a", "b", "a", "a"])
        sums, cnts = cal.day_regime_matrices(r, day, labels, ("a", "b"))
        assert sums.tolist() == [[1.0, 2.0], [7.0, 0.0]]
        assert cnts.tolist() == [[1, 1], [2, 0]]

    def test_unlabelled_trades_are_dropped(self):
        r = np.array([1.0, 2.0])
        day = np.array([0, 1])
        labels = np.array(["a", None], dtype=object)
        sums, cnts = cal.day_regime_matrices(r, day, labels, ("a", "b"))
        assert cnts.sum() == 1


class TestRules:
    def test_fixed_k_rule(self):
        rule = cal.Rule("k2", minimum=2)
        assert rule.passes(positive=2, evaluated=3)
        assert not rule.passes(positive=1, evaluated=3)

    def test_all_rule_needs_every_evaluated_regime(self):
        rule = cal.Rule("all", minimum=None)
        assert rule.passes(positive=3, evaluated=3)
        assert not rule.passes(positive=2, evaluated=3)


class TestBootstrapRates:
    def test_zero_edge_false_pass_falls_as_the_bar_rises(self):
        r, day, labels = _book(seed=1)
        rates = cal.pass_rates(r, day, labels, edge=0.0, n_boot=1500, seed=3)
        k1, k2, k3 = (rates[name]["pass"] for name in ("k1", "k2", "k3"))
        assert k1 >= k2 >= k3
        assert k3 < 0.15

    def test_real_edge_passes_more_often_than_null(self):
        r, day, labels = _book(seed=2)
        null = cal.pass_rates(r, day, labels, edge=0.0, n_boot=1500, seed=4)
        real = cal.pass_rates(r, day, labels, edge=0.15, n_boot=1500, seed=4)
        assert real["k2"]["pass"] > null["k2"]["pass"]

    def test_deterministic_for_a_seed(self):
        r, day, labels = _book(seed=5)
        a = cal.pass_rates(r, day, labels, edge=0.0, n_boot=300, seed=9)
        b = cal.pass_rates(r, day, labels, edge=0.0, n_boot=300, seed=9)
        assert a == b

    def test_low_sample_regimes_are_not_judged(self):
        r, day, labels = _book(seed=6, n_days=10, per_day=1)
        rates = cal.pass_rates(r, day, labels, edge=0.0, n_boot=200, seed=1, min_trades=30)
        assert rates["k2"]["judged"] == 0.0

    def test_demeaning_removes_the_books_own_regime_edge(self):
        r, day, labels = _book(seed=7, edge=0.5)
        rates = cal.pass_rates(r, day, labels, edge=0.0, n_boot=800, seed=2)
        assert rates["k3"]["pass"] < 0.15


def test_choose_threshold_takes_the_loosest_rule_under_the_target():
    table = {
        "k1": {"pass": 0.60, "judged": 1.0},
        "k2": {"pass": 0.30, "judged": 1.0},
        "k3": {"pass": 0.08, "judged": 1.0},
    }
    assert cal.choose_rule(table, target=0.20) == "k3"
    assert cal.choose_rule(table, target=0.35) == "k2"
    with pytest.raises(ValueError):
        cal.choose_rule(table, target=0.01)
