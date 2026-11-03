"""
Known-answer unit tests for services/strategy_metrics_service.py.

Every metric is checked against a hand-computed expected value from a small,
fully specified synthetic trade list - not against the module's own
intermediate output - so a bug in the formula itself would be caught, not
just a regression from the current implementation.

Run with: uv run pytest test/test_strategy_metrics_service.py -v
"""

import numpy as np
import pytest

from services.strategy_metrics_service import compute_metrics, resolve_period


def _trade(pnl, trade_date):
    return {"realized_pnl": pnl, "trade_date": trade_date}


def test_win_rate_profit_factor_expectancy_payoff_ratio():
    trades = [
        _trade(100, "2026-01-01"),
        _trade(-50, "2026-01-02"),
        _trade(-20, "2026-01-02"),
        _trade(30, "2026-01-03"),
    ]
    m = compute_metrics(trades, "2026-01-01", "2026-01-03")

    assert m["trades_count"] == 4
    assert m["winning_trades"] == 2
    assert m["losing_trades"] == 2
    assert m["win_rate"] == pytest.approx(50.0)
    assert m["gross_profit"] == pytest.approx(130.0)
    assert m["gross_loss"] == pytest.approx(70.0)
    assert m["avg_win"] == pytest.approx(65.0)
    assert m["avg_loss"] == pytest.approx(35.0)
    assert m["profit_factor"] == pytest.approx(130 / 70, abs=0.01)
    assert m["expectancy"] == pytest.approx(0.5 * 65 - 0.5 * 35)
    assert m["payoff_ratio"] == pytest.approx(65 / 35, abs=0.01)


def test_max_drawdown_ulcer_and_net_profit():
    # Daily totals: day1=+100, day2=-70 (two trades), day3=+30
    # Equity curve: 100, 30, 60. Running max: 100,100,100.
    # Drawdown: 0, -70, -40 -> max_drawdown = -70.
    trades = [
        _trade(100, "2026-01-01"),
        _trade(-50, "2026-01-02"),
        _trade(-20, "2026-01-02"),
        _trade(30, "2026-01-03"),
    ]
    m = compute_metrics(trades, "2026-01-01", "2026-01-03")

    expected_ulcer = np.sqrt((0**2 + 70**2 + 40**2) / 3)

    assert m["net_profit"] == pytest.approx(60.0)
    assert m["max_drawdown"] == pytest.approx(-70.0)
    assert m["ulcer_index"] == pytest.approx(expected_ulcer, abs=0.01)
    assert m["best_day"] == pytest.approx(100.0)
    assert m["worst_day"] == pytest.approx(-70.0)


def test_constant_daily_pnl_gives_zero_sharpe_and_sortino_by_definition():
    """Zero standard deviation is guarded explicitly (division by zero), not
    treated as an undefined/infinite Sharpe - this is the module's documented
    edge-case behavior, verified here so a future refactor cannot silently
    change it to raise or return inf."""
    trades = [_trade(50, "2026-01-01"), _trade(50, "2026-01-02")]
    m = compute_metrics(trades, "2026-01-01", "2026-01-02")

    assert m["sharpe_ratio"] == 0.0
    assert m["sortino_ratio"] == 0.0


def test_sharpe_matches_hand_computed_two_day_series():
    # Daily totals: 10, 30. Mean=20, population std=10 (both points 10 away
    # from the mean). Sharpe = mean/std * sqrt(252) = 2 * sqrt(252).
    trades = [_trade(10, "2026-01-01"), _trade(30, "2026-01-02")]
    m = compute_metrics(trades, "2026-01-01", "2026-01-02")

    assert m["sharpe_ratio"] == pytest.approx(2 * np.sqrt(252), abs=0.01)


def test_zero_trade_days_are_included_as_zero_pnl_days():
    """A 5-day window with trades only on day 1 must still produce 5 daily_pnl
    points (4 of them zero) - omitting no-trade days would inflate Sharpe."""
    trades = [_trade(100, "2026-01-01")]
    m = compute_metrics(trades, "2026-01-01", "2026-01-05")

    assert m["calendar_days"] == 5
    assert len(m["daily_pnl"]) == 5
    assert m["trading_days"] == 1  # only day 1 has a nonzero total
    assert [p["pnl"] for p in m["daily_pnl"]] == [100.0, 0.0, 0.0, 0.0, 0.0]


def test_consecutive_win_and_loss_streaks():
    # Daily totals across 6 days: +10, +10, +10, -5, -5, +10
    # -> longest win streak = 3 (days 1-3), longest loss streak = 2 (days 4-5)
    trades = [
        _trade(10, "2026-01-01"),
        _trade(10, "2026-01-02"),
        _trade(10, "2026-01-03"),
        _trade(-5, "2026-01-04"),
        _trade(-5, "2026-01-05"),
        _trade(10, "2026-01-06"),
    ]
    m = compute_metrics(trades, "2026-01-01", "2026-01-06")

    assert m["best_win_streak"] == 3
    assert m["best_loss_streak"] == 2


def test_profit_factor_is_null_not_infinite_when_there_are_no_losses():
    trades = [_trade(100, "2026-01-01"), _trade(50, "2026-01-02")]
    m = compute_metrics(trades, "2026-01-01", "2026-01-02")

    assert m["profit_factor"] is None
    assert m["payoff_ratio"] is None


def test_empty_trade_list_returns_zeroed_metrics_not_an_error():
    m = compute_metrics([], "2026-01-01", "2026-01-03")

    assert m["trades_count"] == 0
    assert m["win_rate"] == 0.0
    assert m["net_profit"] == 0.0
    assert m["profit_factor"] == 0.0
    assert len(m["daily_pnl"]) == 3


@pytest.mark.parametrize(
    "period,expected_start",
    [
        ("1d", "2026-12-26"),
        ("7d", "2026-12-20"),
        ("15d", "2026-12-12"),
        ("30d", "2026-11-27"),
        ("90d", "2026-09-28"),  # naturally lands after the floor - unclamped
    ],
)
def test_resolve_period_fixed_windows(period, expected_start):
    from datetime import date

    today = date(2026, 12, 26)
    start, end = resolve_period(period, closed_trades=[], today=today)

    assert start == expected_start
    assert end == "2026-12-26"


def test_resolve_period_ytd_is_clamped_to_the_reliable_ledger_start():
    """Jan 1 predates StrategyClosedTrade (added 2026-09-25) entirely, so a
    literal year-to-date window would silently include the known-incomplete
    2026-09-08..09-24 backfilled window (see LEDGER_RELIABLE_SINCE) - ytd
    must clamp to the floor, not the calendar year start."""
    from datetime import date

    start, end = resolve_period("ytd", closed_trades=[], today=date(2026, 12, 26))

    assert start == "2026-09-25"
    assert end == "2026-12-26"


def test_resolve_period_all_starts_at_earliest_trade_within_the_reliable_window():
    from datetime import date

    trades = [_trade(10, "2026-10-01"), _trade(20, "2026-10-15")]
    start, end = resolve_period("all", trades, today=date(2026, 10, 26))

    assert start == "2026-10-01"
    assert end == "2026-10-26"


def test_resolve_period_all_clamps_out_pre_ledger_trades():
    """A trade dated before LEDGER_RELIABLE_SINCE exists (hand-reconstructed
    for a handful of legs - see the module docstring) but must never become
    the reported start; that window is known-incomplete."""
    from datetime import date

    trades = [_trade(10, "2026-09-10"), _trade(20, "2026-10-01")]
    start, end = resolve_period("all", trades, today=date(2026, 10, 26))

    assert start == "2026-09-25"
    assert end == "2026-10-26"


def test_resolve_period_all_with_no_trades_is_a_single_day():
    from datetime import date

    start, end = resolve_period("all", [], today=date(2026, 10, 26))

    assert start == end == "2026-10-26"


def test_resolve_period_1d_falls_back_to_the_last_traded_session():
    """On a weekend or holiday there is nothing closed today; 1D shows the
    most recent session that has closed trades instead of an empty page."""
    from datetime import date

    trades = [_trade(10, "2026-10-29"), _trade(20, "2026-10-30")]
    start, end = resolve_period("1d", trades, today=date(2026, 11, 1))

    assert start == end == "2026-10-30"


def test_resolve_period_1d_is_today_when_today_has_trades():
    from datetime import date

    trades = [_trade(10, "2026-10-30"), _trade(20, "2026-11-02")]
    start, end = resolve_period("1d", trades, today=date(2026, 11, 2))

    assert start == end == "2026-11-02"


def test_resolve_period_1d_ignores_pre_ledger_and_future_dated_trades():
    from datetime import date

    trades = [_trade(10, "2026-09-10"), _trade(20, "2026-12-01")]
    start, end = resolve_period("1d", trades, today=date(2026, 11, 2))

    assert start == end == "2026-11-02"


def test_resolve_period_custom_returns_the_requested_range():
    from datetime import date

    start, end = resolve_period(
        "custom", [], today=date(2026, 11, 2), start="2026-10-05", end="2026-10-20"
    )

    assert (start, end) == ("2026-10-05", "2026-10-20")


def test_resolve_period_custom_clamps_to_ledger_floor_and_today():
    from datetime import date

    start, end = resolve_period(
        "custom", [], today=date(2026, 11, 2), start="2026-01-01", end="2026-12-31"
    )

    assert (start, end) == ("2026-09-25", "2026-11-02")


@pytest.mark.parametrize(
    "start,end",
    [
        (None, "2026-10-20"),
        ("2026-10-05", None),
        ("05-10-2026", "2026-10-20"),
        ("2026-10-20", "2026-10-05"),
        ("2026-10-05'; DROP", "2026-10-20"),
        ("2027-01-01", "2027-01-05"),
    ],
)
def test_resolve_period_custom_rejects_bad_ranges(start, end):
    from datetime import date

    with pytest.raises(ValueError):
        resolve_period("custom", [], today=date(2026, 11, 2), start=start, end=end)


# ---- edge statistics (net per-trade) ----

from services.strategy_metrics_service import edge_stats  # noqa: E402


def test_edge_stats_known_answer():
    # values 10,20,30,40: mean 25, sample std sqrt(500/3)=12.9099, se=6.4550
    e = edge_stats([10.0, 20.0, 30.0, 40.0], costed=4)
    assert e["net_expectancy"] == pytest.approx(25.0)
    assert e["net_pnl_std"] == pytest.approx(12.9099, abs=0.01)
    assert e["t_stat"] == pytest.approx(25 / 6.4550, abs=1e-2)
    lo, hi = e["expectancy_ci95"]
    assert lo == pytest.approx(25 - 1.96 * 6.4550, abs=1e-2)
    assert hi == pytest.approx(25 + 1.96 * 6.4550, abs=1e-2)
    assert e["edge_trades"] == 4 and e["edge_cost_coverage"] == 4
    assert e["edge_verdict"] == "too few trades"


def test_edge_stats_single_trade_and_zero_std():
    one = edge_stats([5.0], costed=1)
    assert one["net_expectancy"] == 5.0
    assert one["t_stat"] is None and one["expectancy_ci95"] is None
    flat = edge_stats([3.0, 3.0, 3.0], costed=3)
    assert flat["t_stat"] is None and flat["net_pnl_std"] == 0.0


def test_edge_stats_empty():
    e = edge_stats([], costed=0)
    assert e["net_expectancy"] is None and e["t_stat"] is None
    assert e["edge_verdict"] == "too few trades" and e["edge_trades"] == 0


def _series(n, mean, spread):
    # alternating mean+spread / mean-spread, n even
    return [mean + spread if i % 2 == 0 else mean - spread for i in range(n)]


def test_edge_verdicts_at_thirty_trades():
    real = edge_stats(_series(30, 10.0, 5.0), costed=30)
    assert real["t_stat"] >= 2 and real["edge_verdict"] == "likely real"
    neg = edge_stats(_series(30, -10.0, 5.0), costed=30)
    assert neg["t_stat"] <= -2 and neg["edge_verdict"] == "likely negative"
    flat = edge_stats(_series(30, 0.5, 50.0), costed=30)
    assert abs(flat["t_stat"]) < 2
    assert flat["edge_verdict"] == "not distinguishable from zero"
    assert edge_stats(_series(29, 10.0, 5.0), costed=29)["edge_verdict"] == "too few trades"


def _dates(n_days, per_day):
    return [f"2026-10-{d + 1:02d}" for d in range(n_days) for _ in range(per_day)]


def test_edge_day_clustering_known_answer():
    # 2 days x 2 trades: day sums 30 and 70 -> mean 50, sample std 28.284, se 20
    e = edge_stats([10.0, 20.0, 30.0, 40.0], costed=4, trade_dates=_dates(2, 2))
    assert e["edge_days"] == 2
    assert e["day_t_stat"] == pytest.approx(2.5, abs=1e-2)


def test_edge_verdict_needs_ten_trading_days_when_dates_given():
    pnls = _series(30, 10.0, 5.0)
    assert edge_stats(pnls, costed=30, trade_dates=_dates(3, 10))["edge_verdict"] == (
        "too few days"
    )


def test_edge_verdict_requires_day_level_agreement():
    """Many same-day trades look significant per trade, but if the days
    themselves disagree the edge is not established."""
    pnls, dates = [], []
    for d in range(10):
        day_mean = 60.0 if d % 2 == 0 else -30.0  # day-to-day swings dominate
        pnls += [day_mean + (1.0 if i % 2 == 0 else -1.0) for i in range(10)]
        dates += [f"2026-10-{d + 1:02d}"] * 10
    e = edge_stats(pnls, costed=100, trade_dates=dates)
    assert e["t_stat"] >= 2
    assert e["day_t_stat"] < 2
    assert e["edge_verdict"] == "not distinguishable from zero"


def test_edge_verdict_real_when_trades_and_days_agree():
    pnls = [10.0 + (d % 3) + (5.0 if i % 2 == 0 else -5.0) for d in range(10) for i in range(4)]
    e = edge_stats(pnls, costed=40, trade_dates=_dates(10, 4))
    assert e["day_t_stat"] >= 2
    assert e["edge_verdict"] == "likely real"


# ---- compute_capital_view ----------------------------------------------------


def test_capital_view_known_numbers():
    from services.strategy_metrics_service import compute_capital_view

    metrics = {
        "net_profit": 5000.0,
        "net_after_costs": 4500.0,
        "max_drawdown": -2000.0,
        "best_day": 3000.0,
        "worst_day": -1000.0,
    }
    v = compute_capital_view(metrics, 100000.0)
    assert v["capital"] == 100000.0
    assert v["strategies"] == 1
    assert v["total_capital"] == 100000.0
    assert v["reference_capital"] == 100000
    assert v["scale"] == 1.0
    assert v["scaled"]["net_profit"] == 5000.0
    assert v["return_pct"] == 5.0
    assert v["return_net_pct"] == 4.5
    assert v["max_drawdown_pct_of_capital"] == -2.0
    assert v["best_day_pct"] == 3.0
    assert v["worst_day_pct"] == -1.0
    assert set(v["basis"]) == {
        "return_pct",
        "return_net_pct",
        "max_drawdown_pct_of_capital",
        "best_day_pct",
        "worst_day_pct",
    }


def test_capital_view_does_not_change_its_input():
    import copy

    from services.strategy_metrics_service import compute_capital_view

    metrics = {
        "net_profit": 1.0,
        "net_after_costs": None,
        "max_drawdown": 0.0,
        "best_day": 1.0,
        "worst_day": 0.0,
        "extra": [1, 2],
    }
    before = copy.deepcopy(metrics)
    compute_capital_view(metrics, 1000.0)
    assert metrics == before


def test_capital_view_unknown_net_and_missing_keys_give_none():
    from services.strategy_metrics_service import compute_capital_view

    v = compute_capital_view({"net_profit": 10.0, "net_after_costs": None}, 100000.0)
    assert v["return_pct"] == 0.01
    assert v["return_net_pct"] is None
    assert v["scaled"]["net_after_costs"] is None
    assert v["scaled"]["est_costs"] is None
    assert v["best_day_pct"] is None


def test_capital_view_never_divides_by_zero():
    from services.strategy_metrics_service import compute_capital_view

    for bad in (0, 0.0, -5.0, float("nan")):
        v = compute_capital_view({"net_profit": 10.0, "max_drawdown": -1.0}, bad)
        assert v["return_pct"] is None
        assert v["scaled"]["net_profit"] is None
        assert v["max_drawdown_pct_of_capital"] is None


def test_capital_view_works_on_a_real_compute_metrics_result():
    from services.strategy_metrics_service import compute_capital_view

    trades = [_trade(100, "2026-01-01"), _trade(-50, "2026-01-02"), _trade(30, "2026-01-03")]
    m = compute_metrics(trades, "2026-01-01", "2026-01-03")
    v = compute_capital_view(m, 100000.0)
    assert v["return_pct"] == 0.08
    assert v["max_drawdown_pct_of_capital"] == round(m["max_drawdown"] / 1000.0, 2)
    assert v["best_day_pct"] == 0.1
    assert v["worst_day_pct"] == -0.05


_METRICS = {
    "net_profit": 5000.0,
    "net_after_costs": 4500.0,
    "est_costs": 500.0,
    "max_drawdown": -2000.0,
    "best_day": 3000.0,
    "worst_day": -1000.0,
}


def test_capital_view_scales_one_strategy_by_a_tenth():
    from services.strategy_metrics_service import compute_capital_view

    v = compute_capital_view(_METRICS, 10000.0, 1)
    assert v["scale"] == 0.1
    assert v["total_capital"] == 10000.0
    assert v["scaled"] == {
        "net_profit": 500.0,
        "net_after_costs": 450.0,
        "est_costs": 50.0,
        "max_drawdown": -200.0,
        "best_day": 300.0,
        "worst_day": -100.0,
    }
    assert v["return_pct"] == 5.0  # 500 on 10,000


def test_capital_view_three_strategies_total_capital_and_percent():
    from services.strategy_metrics_service import (
        REFERENCE_CAPITAL_PER_STRATEGY,
        compute_capital_view,
    )

    v = compute_capital_view(_METRICS, 10000.0, 3)
    assert v["strategies"] == 3
    assert v["total_capital"] == 30000.0
    assert v["scaled"]["net_profit"] == 500.0
    assert v["return_pct"] == round(5000.0 / (REFERENCE_CAPITAL_PER_STRATEGY * 3) * 100, 2)
    assert v["return_pct"] == round(v["scaled"]["net_profit"] / v["total_capital"] * 100, 2)
    assert v["max_drawdown_pct_of_capital"] == round(-200.0 / 30000.0 * 100, 2)


def test_capital_view_treats_zero_strategies_as_one():
    from services.strategy_metrics_service import compute_capital_view

    assert compute_capital_view(_METRICS, 1000.0, 0)["strategies"] == 1
