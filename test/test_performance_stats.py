"""
Tests for services/performance_stats.py.

The reference functions below are written independently of the module under test: plain
Python loops over the `statistics` and `math` modules, no numpy, no shared helpers. A formula
bug in the module therefore cannot also be in the reference.

The golden test reads the real strategy export. It is not committed (it holds real trades);
point OPENALGO_GOLDEN_TRADES_CSV at it, or leave it at the default download location. The test
is skipped when the file is absent.

Run with: uv run pytest test/test_performance_stats.py -v
"""

import csv
import math
import os
import statistics
from pathlib import Path

import numpy as np
import pytest

from services import performance_stats as ps
from services.strategy_metrics_service import compute_metrics

SQRT252 = math.sqrt(252)


# ---- independent reference implementations --------------------------------------------


def ref_sharpe(returns, rf_annual=0.0):
    mean = statistics.fmean(returns)
    sd = statistics.stdev(returns)  # sample standard deviation
    return (mean - rf_annual / 252) / sd * SQRT252


def ref_sortino(returns, target=0.0):
    squares = [min(r - target, 0.0) ** 2 for r in returns]
    dd = math.sqrt(sum(squares) / len(returns))  # over every observation
    return (statistics.fmean(returns) - target) / dd * SQRT252


def ref_bootstrap_means(pnls, days, resamples, seed):
    """Whole-day resampling written with explicit loops, same draw matrix as the module."""
    order = sorted(set(days), key=days.index)
    per_day = {d: [p for p, x in zip(pnls, days, strict=True) if x == d] for d in order}
    rng = np.random.default_rng(seed)
    draw = rng.integers(0, len(order), size=(resamples, len(order)))
    out = []
    for row in draw:
        total = count = 0
        for i in row:
            vals = per_day[order[i]]
            total += sum(vals)
            count += len(vals)
        out.append(total / count)
    return out


def _returns(n, pattern):
    return [pattern[i % len(pattern)] for i in range(n)]


# ---- Sharpe and Sortino ---------------------------------------------------------------


def test_mixed_series_with_zero_days_matches_reference():
    r = _returns(80, [0.012, -0.007, 0.0, 0.004, -0.011, 0.0, 0.009])
    assert ps.sharpe_ratio(np.array(r)) == pytest.approx(ref_sharpe(r), rel=1e-12)
    assert ps.sortino_ratio(np.array(r)) == pytest.approx(ref_sortino(r), rel=1e-12)


def test_zero_return_days_are_kept_in_both_denominators():
    with_zeros = [0.01, -0.02, 0.0, 0.0, 0.03, -0.01]
    without = [x for x in with_zeros if x != 0.0]
    assert ps.sortino_ratio(np.array(with_zeros)) == pytest.approx(ref_sortino(with_zeros))
    assert ps.sortino_ratio(np.array(with_zeros)) != pytest.approx(ref_sortino(without))
    assert ps.sharpe_ratio(np.array(with_zeros)) != pytest.approx(ref_sharpe(without))


def test_downside_deviation_is_measured_from_the_target_not_from_the_losing_days_mean():
    """The defect this module replaces: the std of losing days around their own mean.
    Two equal losses have zero spread, yet they are a real downside."""
    r = [0.02, -0.01, -0.01, 0.03]
    assert ps.downside_deviation(np.array(r)) == pytest.approx(math.sqrt((0.0001 * 2) / 4))
    assert ps.sortino_ratio(np.array(r)) == pytest.approx(ref_sortino(r))


def test_all_profitable_days_sharpe_is_finite_sortino_is_undefined():
    r = np.array(_returns(70, [0.01, 0.02, 0.015]))
    assert ps.sharpe_ratio(r) == pytest.approx(ref_sharpe(list(r)))
    assert ps.sortino_ratio(r) is None  # zero downside: undefined, never infinity


def test_all_losing_days_give_negative_finite_ratios():
    r = _returns(70, [-0.01, -0.02, -0.005])
    assert ps.sharpe_ratio(np.array(r)) == pytest.approx(ref_sharpe(r))
    assert ps.sortino_ratio(np.array(r)) == pytest.approx(ref_sortino(r))
    assert ps.sharpe_ratio(np.array(r)) < 0 and ps.sortino_ratio(np.array(r)) < 0


def test_constant_returns():
    flat_up = np.full(70, 0.001)
    assert ps.sharpe_ratio(flat_up) is None  # no spread
    assert ps.sortino_ratio(flat_up) is None  # no downside
    flat_zero = np.zeros(70)
    assert ps.sharpe_ratio(flat_zero) is None and ps.sortino_ratio(flat_zero) is None
    flat_down = np.full(70, -0.001)
    assert ps.sharpe_ratio(flat_down) is None
    # A constant loss has no variation either; a ratio would only measure its level.
    assert ps.sortino_ratio(flat_down) is None


def test_tiny_samples_return_none_not_numbers():
    assert ps.sharpe_ratio(np.array([])) is None
    assert ps.sharpe_ratio(np.array([0.01])) is None
    assert ps.sortino_ratio(np.array([-0.01])) is None


def test_risk_free_rate_and_target_are_applied_per_day():
    r = _returns(80, [0.012, -0.007, 0.0, 0.004, -0.011, 0.0, 0.009])
    assert ps.sharpe_ratio(np.array(r), 0.07) == pytest.approx(ref_sharpe(r, 0.07), rel=1e-12)
    assert ps.sortino_ratio(np.array(r), 0.0005) == pytest.approx(ref_sortino(r, 0.0005), rel=1e-12)


# ---- sample-size gating ---------------------------------------------------------------


def _pnl_series(sessions, losing):
    """sessions daily P&L values with `losing` of them negative."""
    return np.array([-300.0 if i < losing else 500.0 + i for i in range(sessions)])


def test_fewer_than_sixty_sessions_withholds_both_ratios():
    out = ps.risk_adjusted_summary(_pnl_series(59, 20), capital=200000)
    assert out["sharpe_ratio"] is None and out["sortino_ratio"] is None
    assert "at least 60 trading sessions" in out["ratio_warning"]
    assert out["ratio_sessions"] == 59


def test_sixty_sessions_with_ten_losing_shows_both():
    pnl = _pnl_series(60, 10)
    out = ps.risk_adjusted_summary(pnl, capital=200000)
    r = [x / 200000 for x in pnl]
    assert out["sharpe_ratio"] == pytest.approx(ref_sharpe(r), abs=0.005)
    assert out["sortino_ratio"] == pytest.approx(ref_sortino(r), abs=0.005)
    assert out["ratio_warning"] is None


def test_nine_losing_sessions_withholds_both_ratios():
    out = ps.risk_adjusted_summary(_pnl_series(60, 9), capital=200000)
    assert out["sharpe_ratio"] is None
    assert out["sortino_ratio"] is None
    assert "at least 10 losing sessions" in out["ratio_warning"]
    assert out["ratio_losing_sessions"] == 9


# ---- capital ---------------------------------------------------------------------------


def test_doubling_capital_leaves_both_ratios_unchanged_at_zero_rf_and_target():
    pnl = _pnl_series(90, 25)
    a = ps.risk_adjusted_summary(pnl, capital=200000)
    b = ps.risk_adjusted_summary(pnl, capital=400000)
    assert a["sharpe_ratio"] == b["sharpe_ratio"] is not None
    assert a["sortino_ratio"] == b["sortino_ratio"] is not None
    assert a["ratio_capital"] == 200000 and b["ratio_capital"] == 400000


def test_daily_returns_scale_inversely_with_capital():
    pnl = np.array([100.0, -50.0, 0.0])
    assert ps.daily_returns(pnl, 1000.0) == pytest.approx([0.1, -0.05, 0.0])
    assert ps.daily_returns(pnl, 2000.0) == pytest.approx([0.05, -0.025, 0.0])


def test_nonzero_risk_free_rate_or_target_makes_ratios_depend_on_capital():
    pnl = _pnl_series(90, 25)
    s_a = ps.risk_adjusted_summary(pnl, 200000, risk_free_annual=0.07)["sharpe_ratio"]
    s_b = ps.risk_adjusted_summary(pnl, 400000, risk_free_annual=0.07)["sharpe_ratio"]
    assert s_a != s_b
    t_a = ps.risk_adjusted_summary(pnl, 200000, target_daily=0.0005)["sortino_ratio"]
    t_b = ps.risk_adjusted_summary(pnl, 400000, target_daily=0.0005)["sortino_ratio"]
    assert t_a != t_b


@pytest.mark.parametrize("capital", [0, -5, float("nan")])
def test_invalid_capital_is_rejected(capital):
    with pytest.raises(ValueError):
        ps.daily_returns(np.array([1.0]), capital)


def test_compute_metrics_capital_invariance_and_default():
    sessions = ps.weekday_sessions("2026-01-05", "2026-04-24")  # 14 weeks, 70 sessions
    trades = [
        {"trade_date": d, "realized_pnl": -300.0 if i % 4 == 0 else 450.0}
        for i, d in enumerate(sessions)
    ]
    start, end = sessions[0], sessions[-1]
    base = compute_metrics(trades, start, end, sessions=sessions, capital=200000)
    double = compute_metrics(trades, start, end, sessions=sessions, capital=400000)
    default = compute_metrics(trades, start, end, sessions=sessions)
    assert base["sharpe_ratio"] == double["sharpe_ratio"] is not None
    assert base["sortino_ratio"] == double["sortino_ratio"] is not None
    assert default["ratio_capital"] == 100_000
    assert default["sharpe_ratio"] == base["sharpe_ratio"]  # invariance again


def test_compute_metrics_uses_net_of_costs():
    sessions = ps.weekday_sessions("2026-01-05", "2026-04-24")
    trades = [
        {"trade_date": d, "realized_pnl": -50.0 if i % 6 == 0 else 100.0 + (i % 5) * 20}
        for i, d in enumerate(sessions)
    ]
    gross = compute_metrics(trades, sessions[0], sessions[-1], sessions=sessions, capital=1e5)
    net = compute_metrics(
        trades,
        sessions[0],
        sessions[-1],
        costs=[30.0] * len(trades),
        sessions=sessions,
        capital=1e5,
    )
    assert net["sharpe_ratio"] < gross["sharpe_ratio"]
    r = [(t["realized_pnl"] - 30.0) / 1e5 for t in trades]
    assert net["sharpe_ratio"] == pytest.approx(ref_sharpe(r), abs=0.005)


# ---- sessions --------------------------------------------------------------------------


def test_weekends_are_not_sessions():
    days = ps.weekday_sessions("2026-10-23", "2026-10-28")  # Fri to Wed
    assert days == ["2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28"]


def test_holidays_drop_out_and_traded_weekend_session_is_kept():
    from datetime import date

    out = ps.trading_sessions(
        "2026-11-02",
        "2026-11-09",
        traded_dates=["2026-11-08"],  # a Sunday special session
        is_holiday=lambda d: d == date(2026, 11, 4),
    )
    assert "2026-11-04" not in out
    assert "2026-11-08" in out and "2026-11-07" not in out
    assert out == sorted(out)


def test_no_trade_sessions_are_zero_observations_not_missing():
    sessions = ["2026-10-26", "2026-10-27", "2026-10-28"]
    values = ps.daily_pnl_values({"2026-10-27": 120.0}, sessions)
    assert list(values) == [0.0, 120.0, 0.0]


def test_traded_dates_outside_the_range_are_ignored():
    out = ps.trading_sessions("2026-10-26", "2026-10-27", traded_dates=["2026-10-31"])
    assert out == ["2026-10-26", "2026-10-27"]


# ---- Student t -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("confidence", "df", "expected"),
    [
        (0.95, 1, 12.7062047),
        (0.95, 3, 3.1824463),
        (0.90, 3, 2.3533634),
        (0.95, 195, 1.9721),
        (0.90, 195, 1.6527),
    ],
)
def test_t_critical_known_values(confidence, df, expected):
    assert ps.t_critical(confidence, df) == pytest.approx(expected, abs=1e-3)


def test_t_critical_agrees_with_scipy():
    stats = pytest.importorskip("scipy.stats")
    for df in (2, 5, 24, 100, 195, 1000):
        for level in (0.90, 0.95, 0.99):
            assert ps.t_critical(level, df) == pytest.approx(
                stats.t.ppf(0.5 + level / 2, df), rel=1e-7
            )


# ---- edge check ------------------------------------------------------------------------


def _day_labels(n_days, per_day):
    return [f"2026-10-{d + 1:02d}" for d in range(n_days) for _ in range(per_day)]


def test_intervals_are_student_t_and_nested():
    e = ps.edge_stats([10.0, 20.0, 30.0, 40.0], costed=4)
    se = statistics.stdev([10, 20, 30, 40]) / 2
    assert e["expectancy_ci95"] == pytest.approx(
        [25 - 3.1824463 * se, 25 + 3.1824463 * se], abs=0.01
    )
    assert e["expectancy_ci90"][0] > e["expectancy_ci95"][0]
    assert e["expectancy_ci90"][1] < e["expectancy_ci95"][1]
    assert e["edge_verdict"] == ps.VERDICT_INSUFFICIENT


def test_bootstrap_matches_independent_reference_and_is_deterministic():
    pnls = [float(((i * 37) % 23) - 8) for i in range(60)]
    days = _day_labels(12, 5)
    ref = ref_bootstrap_means(pnls, days, ps.BOOTSTRAP_RESAMPLES, ps.BOOTSTRAP_SEED)
    got = ps.clustered_bootstrap_means(np.array(pnls), days)
    assert list(got) == pytest.approx(ref, rel=1e-12)
    assert len(got) == 5000
    again = ps.clustered_bootstrap_means(np.array(pnls), days)
    assert list(again) == list(got)
    e = ps.edge_stats(pnls, trade_dates=days)
    ref_lo, ref_hi = np.percentile(ref, [2.5, 97.5])
    assert e["bootstrap_ci95"] == pytest.approx([ref_lo, ref_hi], abs=0.006)
    assert e["bootstrap_resamples"] == 5000
    assert e["bootstrap_p_nonpositive"] == pytest.approx(sum(1 for m in ref if m <= 0) / 5000)


def test_bootstrap_resamples_whole_days_so_it_is_wider_than_trade_resampling():
    # Two big days dominate: trades inside a day are perfectly correlated.
    pnls, days = [], []
    for d in range(12):
        level = 80.0 if d % 2 == 0 else -40.0
        pnls += [level] * 8
        days += [f"2026-10-{d + 1:02d}"] * 8
    clustered = ps.edge_stats(pnls, trade_dates=days)["bootstrap_ci95"]
    iid = ps.edge_stats(pnls)["bootstrap_ci95"]
    assert clustered[1] - clustered[0] > iid[1] - iid[0]


def _flat_noise(n, mean, spread):
    return [mean + spread if i % 2 == 0 else mean - spread for i in range(n)]


def test_verdict_positive_supported_needs_trades_days_and_bootstrap():
    pnls = [10.0 + (d % 3) + (5.0 if i % 2 == 0 else -5.0) for d in range(10) for i in range(4)]
    e = ps.edge_stats(pnls, costed=40, trade_dates=_day_labels(10, 4))
    assert e["day_t_stat"] >= 2 and e["bootstrap_ci95"][0] > 0
    assert e["edge_verdict"] == ps.VERDICT_SUPPORTED


def test_verdict_positive_but_inconclusive_when_days_disagree():
    pnls, days = [], []
    for d in range(10):
        day_mean = 60.0 if d % 2 == 0 else -30.0
        pnls += [day_mean + (1.0 if i % 2 == 0 else -1.0) for i in range(10)]
        days += [f"2026-10-{d + 1:02d}"] * 10
    e = ps.edge_stats(pnls, costed=100, trade_dates=days)
    assert e["t_stat"] >= 2 > e["day_t_stat"]
    assert e["edge_verdict"] == ps.VERDICT_POSITIVE_INCONCLUSIVE


def test_verdict_negative():
    pnls = [-10.0 + (d % 3) + (5.0 if i % 2 == 0 else -5.0) for d in range(10) for i in range(4)]
    e = ps.edge_stats(pnls, trade_dates=_day_labels(10, 4))
    assert e["edge_verdict"] == ps.VERDICT_NEGATIVE


def test_verdict_inconclusive_for_a_non_positive_mean_without_evidence():
    pnls = _flat_noise(40, -0.5, 50.0)
    assert ps.edge_stats(pnls)["edge_verdict"] == ps.VERDICT_INCONCLUSIVE


def test_verdict_insufficient_below_thirty_trades_or_ten_days():
    assert ps.edge_stats(_flat_noise(29, 10.0, 5.0))["edge_verdict"] == ps.VERDICT_INSUFFICIENT
    few_days = ps.edge_stats(_flat_noise(30, 10.0, 5.0), trade_dates=_day_labels(3, 10))
    assert few_days["edge_verdict"] == ps.VERDICT_INSUFFICIENT
    assert ps.edge_stats([])["edge_verdict"] == ps.VERDICT_INSUFFICIENT


def test_constant_pnl_has_no_t_stat_and_no_crash():
    e = ps.edge_stats([5.0] * 40)
    assert e["t_stat"] is None and e["net_pnl_std"] == 0.0
    assert e["edge_verdict"] == ps.VERDICT_INCONCLUSIVE


# ---- golden test against the real export -----------------------------------------------

GOLDEN_CSV = Path(
    os.environ.get(
        "OPENALGO_GOLDEN_TRADES_CSV",
        "/mnt/c/Users/Anand/Downloads/strategy-trades-2026-09-25_2026-11-03.csv",
    )
)


@pytest.fixture(scope="module")
def golden_rows():
    if not GOLDEN_CSV.exists():
        pytest.skip("golden trades export not present")
    with GOLDEN_CSV.open(newline="") as f:
        return list(csv.DictReader(f))


def test_golden_totals_reconcile_with_the_export(golden_rows):
    gross = sum(float(r["realized_pnl"]) for r in golden_rows)
    cost = sum(float(r["est_cost"]) for r in golden_rows)
    net = sum(float(r["net_pnl"]) for r in golden_rows)
    assert len(golden_rows) == 196
    assert gross == pytest.approx(17045.03, abs=0.005)
    assert cost == pytest.approx(3125.88, abs=0.005)
    assert net == pytest.approx(13919.15, abs=0.005)
    assert net == pytest.approx(gross - cost, abs=0.005)  # charges deducted exactly once


def test_golden_edge_check(golden_rows):
    net = [float(r["net_pnl"]) for r in golden_rows]
    days = [r["trade_date"] for r in golden_rows]
    e = ps.edge_stats(net, costed=len(net), trade_dates=days)
    se = statistics.stdev(net) / math.sqrt(len(net))
    assert e["net_expectancy"] == pytest.approx(71.02, abs=0.005)
    assert e["t_stat"] == pytest.approx(3.11, abs=0.005)
    assert e["edge_days"] == 25 and e["day_t_stat"] == pytest.approx(1.85, abs=0.005)
    crit95 = 1.9721  # Student-t, 195 df
    assert e["expectancy_ci95"] == pytest.approx(
        [statistics.fmean(net) - crit95 * se, statistics.fmean(net) + crit95 * se], abs=0.05
    )
    # day t-stat < 2: positive, but not supported
    assert e["edge_verdict"] == ps.VERDICT_POSITIVE_INCONCLUSIVE
    assert 0 < e["bootstrap_ci95"][0] < e["bootstrap_ci90"][0] < 71.02 < e["bootstrap_ci90"][1]


def test_golden_sharpe_and_sortino_match_reference_and_are_withheld(golden_rows):
    by_day = {}
    for r in golden_rows:
        by_day[r["trade_date"]] = by_day.get(r["trade_date"], 0.0) + float(r["net_pnl"])
    sessions = ps.trading_sessions("2026-09-25", "2026-11-03", traded_dates=by_day)
    assert len(sessions) == 28  # weekdays; the holiday calendar is applied by the service
    pnl = ps.daily_pnl_values(by_day, sessions)
    reference = [x / 200000 for x in pnl]
    # Ungated primitives agree with the independent reference...
    assert ps.sharpe_ratio(np.array(reference)) == pytest.approx(ref_sharpe(reference), rel=1e-12)
    assert ps.sortino_ratio(np.array(reference)) == pytest.approx(ref_sortino(reference), rel=1e-12)
    assert ref_sharpe(reference) == pytest.approx(5.51, abs=0.01)
    assert ref_sortino(reference) == pytest.approx(22.18, abs=0.01)
    # ...and the page-level summary refuses to show them on 28 sessions.
    summary = ps.risk_adjusted_summary(pnl, 200000)
    assert summary["sharpe_ratio"] is None and summary["sortino_ratio"] is None
    assert summary["ratio_sessions"] == 28 and summary["ratio_losing_sessions"] == 9


# ---- corrections from the independent review ----------------------------------------


def test_float_noise_series_is_not_a_supported_edge():
    noisy = [0.1 + 0.2, 0.3] * 20  # equal in value, differ by float noise
    e = ps.edge_stats(noisy, trade_dates=_day_labels(20, 2))
    assert e["t_stat"] is None and e["day_t_stat"] is None
    assert e["edge_verdict"] == ps.VERDICT_INCONCLUSIVE


def test_chunked_bootstrap_is_the_same_stream_as_one_large_draw():
    pnls = [float(((i * 11) % 17) - 6) for i in range(90)]
    days = _day_labels(30, 3)
    got = ps.clustered_bootstrap_means(np.array(pnls), days, resamples=1000, seed=7)
    ref = ref_bootstrap_means(pnls, days, 1000, 7)
    assert list(got) == pytest.approx(ref, rel=1e-12)


def test_non_finite_inputs_never_reach_the_output():
    e = ps.edge_stats([1.0, float("nan")] * 20)
    assert e["edge_verdict"] == ps.VERDICT_INSUFFICIENT and e["bootstrap_ci95"] is None
    e = ps.edge_stats([1.0, float("inf")] * 20)
    assert e["net_expectancy"] is None
    pnl = _pnl_series(70, 20)
    pnl[3] = float("nan")
    out = ps.risk_adjusted_summary(pnl, 200000)
    assert out["sharpe_ratio"] is None and "could not be read" in out["ratio_warning"]


def test_constant_trades_on_one_day_are_insufficient_not_inconclusive():
    e = ps.edge_stats([5.0] * 40, trade_dates=["2026-10-01"] * 40)
    assert e["edge_days"] == 1 and e["edge_verdict"] == ps.VERDICT_INSUFFICIENT


def test_mismatched_dates_are_rejected_even_for_one_trade():
    with pytest.raises(ValueError):
        ps.edge_stats([5.0], trade_dates=[])


def test_day_test_uses_degrees_of_freedom_critical_value():
    """Ten days: |t| must pass the df=9 critical value (2.262), not a flat 2.0."""
    totals = [0.0, 5.0, 6.0, 3.0, -4.0, 4.0, -2.0, 5.0, 6.0, 1.0]  # day t-stat 2.167
    pnls, days = [], []
    for d, total in enumerate(totals):
        pnls += [total / 4 + (0.1 if i % 2 else -0.1) for i in range(4)]
        days += [f"2026-10-{d + 1:02d}"] * 4
    e = ps.edge_stats(pnls, trade_dates=days)
    assert 2.0 <= e["day_t_stat"] < ps.t_critical(0.95, 9)
    assert e["edge_verdict"] != ps.VERDICT_SUPPORTED


def test_warning_names_the_ratio_that_is_missing():
    flat = np.zeros(70)
    out = ps.risk_adjusted_summary(flat, 100000)
    assert out["sharpe_ratio"] is None
    assert "Sharpe" in out["ratio_warning"]
    # target above zero: the wording counts sessions below the target, not "losing" ones
    pnl = np.array([300.0 + (i % 7) * 10 for i in range(70)])
    pnl[:5] = 100.0  # only five sessions fall below a 0.2% daily target
    out = ps.risk_adjusted_summary(pnl, 100000, target_daily=0.002)
    assert "below the target" in out["ratio_warning"]
    assert out["sharpe_ratio"] is None  # fewer than ten sessions under the target


def test_infinite_capital_is_rejected():
    with pytest.raises(ValueError):
        ps.daily_returns(np.array([1.0]), float("inf"))
