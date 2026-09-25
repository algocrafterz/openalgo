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
        ("7d", "2026-03-20"),
        ("30d", "2026-02-25"),
        ("90d", "2025-12-27"),
        ("ytd", "2026-01-01"),
    ],
)
def test_resolve_period_fixed_windows(period, expected_start):
    from datetime import date

    today = date(2026, 3, 26)
    start, end = resolve_period(period, closed_trades=[], today=today)

    assert start == expected_start
    assert end == "2026-03-26"


def test_resolve_period_all_starts_at_earliest_trade():
    from datetime import date

    trades = [_trade(10, "2026-01-15"), _trade(20, "2026-02-01")]
    start, end = resolve_period("all", trades, today=date(2026, 3, 26))

    assert start == "2026-01-15"
    assert end == "2026-03-26"


def test_resolve_period_all_with_no_trades_is_a_single_day():
    from datetime import date

    start, end = resolve_period("all", [], today=date(2026, 3, 26))

    assert start == end == "2026-03-26"
