"""Statistics the acceptance criteria name: Newey-West t, PSR/DSR, MinTRL, PBO, walk-forward."""

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import stats


def _ar1(n, phi, seed=0, mean=0.0):
    rng = np.random.default_rng(seed)
    e = rng.normal(size=n)
    x = np.zeros(n)
    for i in range(1, n):
        x[i] = phi * x[i - 1] + e[i]
    return x + mean


class TestNeweyWest:
    def test_matches_plain_t_for_white_noise(self):
        x = np.random.default_rng(1).normal(0.1, 1.0, 4000)
        plain = x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))
        assert stats.newey_west_t(x, lags=0) == pytest.approx(
            plain, rel=1e-3
        )  # n vs n-1 in the variance

    def test_positive_autocorrelation_shrinks_t(self):
        x = _ar1(3000, 0.6, seed=2, mean=0.1)
        plain = x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))
        assert abs(stats.newey_west_t(x)) < abs(plain)

    def test_short_or_constant_series_is_nan(self):
        assert np.isnan(stats.newey_west_t(np.array([1.0, 2.0])))
        assert np.isnan(stats.newey_west_t(np.ones(50)))


class TestSharpeFamily:
    def test_psr_is_half_at_the_benchmark(self):
        x = np.random.default_rng(3).normal(0.0, 1.0, 1000)
        sr = x.mean() / x.std(ddof=1)
        assert stats.probabilistic_sharpe(x, benchmark=sr) == pytest.approx(0.5, abs=1e-6)

    def test_psr_rises_with_a_real_edge(self):
        x = np.random.default_rng(4).normal(0.15, 1.0, 1500)
        assert stats.probabilistic_sharpe(x) > 0.99

    def test_expected_max_sharpe_grows_with_trials(self):
        a = stats.expected_max_sharpe(1, sr_var=0.01)
        b = stats.expected_max_sharpe(10, sr_var=0.01)
        c = stats.expected_max_sharpe(100, sr_var=0.01)
        assert a == 0.0 and 0.0 < b < c

    def test_dsr_falls_as_trials_rise(self):
        x = np.random.default_rng(5).normal(0.08, 1.0, 1500)
        one = stats.deflated_sharpe(x, n_trials=1)["dsr"]
        many = stats.deflated_sharpe(x, n_trials=50)["dsr"]
        assert many < one

    def test_dsr_reports_its_inputs(self):
        x = np.random.default_rng(6).normal(0.05, 1.0, 800)
        out = stats.deflated_sharpe(x, n_trials=10)
        assert {"dsr", "sharpe_daily", "sharpe_threshold", "n_obs", "n_trials"} <= set(out)
        assert out["n_obs"] == 800

    def test_dsr_undefined_for_tiny_samples(self):
        assert stats.deflated_sharpe(np.array([0.1, 0.2, 0.3]), n_trials=3) is None

    def test_min_track_record_shorter_for_bigger_sharpe(self):
        small = np.random.default_rng(7).normal(0.05, 1.0, 3000)
        big = np.random.default_rng(7).normal(0.20, 1.0, 3000)
        assert stats.min_track_record_length(big) < stats.min_track_record_length(small)

    def test_min_track_record_infinite_when_sharpe_below_benchmark(self):
        x = np.random.default_rng(8).normal(-0.1, 1.0, 500)
        assert stats.min_track_record_length(x) == float("inf")


class TestPbo:
    def test_pure_noise_pbo_near_half(self):
        perf = np.random.default_rng(9).normal(size=(640, 12))
        assert 0.3 < stats.pbo_cscv(perf, n_slices=8) < 0.7

    def test_a_dominant_strategy_has_low_pbo(self):
        rng = np.random.default_rng(10)
        perf = rng.normal(size=(640, 12))
        perf[:, 3] += 0.5
        assert stats.pbo_cscv(perf, n_slices=8) < 0.1

    def test_needs_at_least_two_strategies(self):
        with pytest.raises(ValueError):
            stats.pbo_cscv(np.zeros((100, 1)), n_slices=8)

    def test_slices_must_be_even(self):
        with pytest.raises(ValueError):
            stats.pbo_cscv(np.zeros((100, 4)), n_slices=7)


class _T:
    def __init__(self, day, r):
        self.day, self.r_net = day, r


class TestWalkForward:
    def test_windows_are_consecutive_and_full_length(self):
        w = stats.walk_forward_windows("2020-01-01", "2022-12-31", months=6)
        assert len(w) == 6
        assert w[0][0] == pd.Timestamp("2020-01-01")
        assert w[1][0] == w[0][1]

    def test_partial_trailing_window_is_dropped(self):
        w = stats.walk_forward_windows("2020-01-01", "2021-03-31", months=6)
        assert len(w) == 2

    def test_share_of_positive_windows(self):
        days = pd.date_range("2020-01-01", "2022-12-31", freq="7D")
        trades = [_T(d, 0.2 if d.year != 2021 else -0.2) for d in days]
        out = stats.walk_forward(trades, months=6, start="2020-01-01", end="2022-12-31")
        assert out["windows"] == 6
        assert out["positive"] == 4
        assert out["share_positive"] == pytest.approx(4 / 6)

    def test_windows_without_trades_count_against(self):
        trades = [_T(pd.Timestamp("2020-02-01"), 0.3), _T(pd.Timestamp("2022-11-01"), 0.3)]
        out = stats.walk_forward(trades, months=6, start="2020-01-01", end="2022-12-31")
        assert out["empty"] == 4
        assert out["positive"] == 2
