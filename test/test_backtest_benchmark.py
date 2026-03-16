"""Tests for benchmark comparison."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import pytest

from backtest.benchmark import compute_buy_and_hold, compare_strategy_vs_benchmark


def _make_daily_prices(start_price: float, end_price: float, days: int = 252) -> pd.DataFrame:
    """Create synthetic daily OHLCV data with known start/end prices."""
    dates = pd.bdate_range("2025-01-01", periods=days)
    prices = np.linspace(start_price, end_price, days)
    # Add small noise to make it realistic
    np.random.seed(42)
    noise = np.random.randn(days) * 0.5
    closes = prices + noise
    closes[0] = start_price  # exact start
    closes[-1] = end_price   # exact end

    return pd.DataFrame({
        "open": closes * 0.999,
        "high": closes * 1.005,
        "low": closes * 0.995,
        "close": closes,
        "volume": np.random.randint(100000, 500000, days).astype(float),
    }, index=dates)


class TestBuyAndHold:
    def test_positive_return(self):
        """Stock going up should show positive return."""
        df = _make_daily_prices(100.0, 120.0, days=252)
        result = compute_buy_and_hold(df)
        assert result["total_return_pct"] > 0
        assert result["total_return_pct"] == pytest.approx(20.0, abs=1.0)

    def test_negative_return(self):
        """Stock going down should show negative return."""
        df = _make_daily_prices(100.0, 80.0, days=252)
        result = compute_buy_and_hold(df)
        assert result["total_return_pct"] < 0

    def test_max_drawdown(self):
        """Max drawdown should be negative."""
        df = _make_daily_prices(100.0, 120.0, days=252)
        result = compute_buy_and_hold(df)
        assert result["max_drawdown_pct"] <= 0

    def test_has_required_keys(self):
        """Result should include all required metrics."""
        df = _make_daily_prices(100.0, 110.0)
        result = compute_buy_and_hold(df)
        for key in ["total_return_pct", "annualized_return_pct", "max_drawdown_pct"]:
            assert key in result, f"Missing key: {key}"


class TestCompareStrategy:
    def test_positive_alpha(self):
        """Strategy with higher return should show positive alpha."""
        result = compare_strategy_vs_benchmark(
            strategy_return_pct=25.0,
            benchmark_return_pct=15.0,
            strategy_name="ORB",
            benchmark_name="NIFTY 50",
        )
        assert result["alpha_pct"] > 0
        assert result["outperforms"] is True

    def test_negative_alpha(self):
        """Strategy underperforming should show negative alpha."""
        result = compare_strategy_vs_benchmark(
            strategy_return_pct=5.0,
            benchmark_return_pct=15.0,
            strategy_name="ORB",
            benchmark_name="NIFTY 50",
        )
        assert result["alpha_pct"] < 0
        assert result["outperforms"] is False

    def test_format_message(self):
        """Result should include a formatted message."""
        result = compare_strategy_vs_benchmark(
            strategy_return_pct=20.0,
            benchmark_return_pct=10.0,
        )
        assert "message" in result
        assert len(result["message"]) > 0
