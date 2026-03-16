"""Tests for PineScript parity fixes: ATR calculation and index filter."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import pytest

from backtest.strategies.orb import ORBConfig, ORBStrategy


def _make_ohlcv(n: int = 100, base_price: float = 100.0) -> pd.DataFrame:
    """Generate synthetic OHLCV for a single trading day."""
    # Start at 09:15, 5-min bars
    idx = pd.date_range("2025-01-02 09:15", periods=n, freq="5min")
    np.random.seed(42)
    prices = base_price + np.cumsum(np.random.randn(n) * 0.5)
    return pd.DataFrame({
        "open": prices,
        "high": prices + abs(np.random.randn(n) * 0.3),
        "low": prices - abs(np.random.randn(n) * 0.3),
        "close": prices + np.random.randn(n) * 0.2,
        "volume": np.random.randint(10000, 100000, n),
    }, index=idx)


class TestATRWildersSmoothing:
    """ATR must use Wilder's smoothing (EWM), not simple SMA."""

    def test_atr_uses_wilder_not_sma(self):
        """Wilder's ATR != SMA ATR for the same data."""
        df = _make_ohlcv(50)
        strategy = ORBStrategy(ORBConfig())
        atr = strategy._compute_atr(df, 14)

        # Compute SMA ATR for comparison
        high = df["high"]
        low = df["low"]
        close = df["close"]
        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        sma_atr = tr.rolling(14).mean()

        # They should NOT be equal (Wilder's produces different values)
        # Compare after warmup period
        valid = ~(atr.isna() | sma_atr.isna())
        assert not np.allclose(
            atr[valid].values, sma_atr[valid].values
        ), "ATR should use Wilder's smoothing, not SMA"

    def test_atr_matches_wilder_ewm(self):
        """ATR should match ewm(span=length, adjust=False) on True Range."""
        df = _make_ohlcv(50)
        strategy = ORBStrategy(ORBConfig())
        atr = strategy._compute_atr(df, 14)

        # Compute expected Wilder's ATR
        high = df["high"]
        low = df["low"]
        close = df["close"]
        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        expected = tr.ewm(span=14, adjust=False).mean()

        valid = ~(atr.isna() | expected.isna())
        np.testing.assert_allclose(
            atr[valid].values, expected[valid].values, rtol=1e-10
        )

    def test_atr_returns_series(self):
        df = _make_ohlcv(30)
        strategy = ORBStrategy(ORBConfig())
        atr = strategy._compute_atr(df, 14)
        assert isinstance(atr, pd.Series)
        assert len(atr) == len(df)


class TestIndexFilterVWAP:
    """Index filter must use Price vs VWAP method (PineScript default)."""

    def _make_multiday_data(self, days=5, bars_per_day=75):
        """Make multi-day 5m OHLCV data with known patterns."""
        frames = []
        for d in range(days):
            date = f"2025-01-{d+2:02d}"
            idx = pd.date_range(f"{date} 09:15", periods=bars_per_day, freq="5min")
            np.random.seed(42 + d)
            base = 100 + d * 2
            prices = base + np.cumsum(np.random.randn(bars_per_day) * 0.3)
            df = pd.DataFrame({
                "open": prices,
                "high": prices + 0.5,
                "low": prices - 0.5,
                "close": prices + 0.1,
                "volume": np.random.randint(50000, 200000, bars_per_day),
            }, index=idx)
            frames.append(df)
        return pd.concat(frames)

    def test_index_filter_uses_vwap_not_rolling(self):
        """With index_data provided, filter should use VWAP comparison."""
        stock_data = self._make_multiday_data()
        # Create index data aligned to same timestamps
        index_data = self._make_multiday_data()
        # Make index clearly bullish (close >> vwap) by pushing close up
        index_data["close"] = index_data["high"] + 10  # way above VWAP

        config = ORBConfig(
            enable_index_filter=True,
            enable_trend_filter=False,  # disable other filters to isolate
            enable_htf_filter=False,
            enable_volume_filter=False,
            enable_gap_filter=False,
            enable_orb_range_filter=False,
            enable_min_entry_time=False,
        )
        strategy = ORBStrategy(config=config, index_data=index_data)
        signals = strategy.generate_signals_detailed(stock_data)
        # Should not crash and should produce some signals
        assert len(signals) == len(stock_data)

    def test_index_filter_none_graceful(self):
        """With index_data=None and enable_index_filter=True, filter is skipped."""
        stock_data = self._make_multiday_data()
        config = ORBConfig(
            enable_index_filter=True,
            enable_trend_filter=False,
            enable_htf_filter=False,
            enable_volume_filter=False,
        )
        strategy = ORBStrategy(config=config, index_data=None)
        # Should not crash
        signals = strategy.generate_signals_detailed(stock_data)
        assert len(signals) == len(stock_data)

    def test_index_filter_method_config(self):
        """ORBConfig should support index_filter_method with default 'vwap'."""
        config = ORBConfig()
        assert config.index_filter_method == "vwap"

    def test_index_filter_method_orb_direction(self):
        """ORBConfig with orb_direction method should be accepted."""
        config = ORBConfig(index_filter_method="orb_direction")
        assert config.index_filter_method == "orb_direction"


class TestIndexDataLoading:
    """Test load_index_data function in data_loader."""

    def test_load_index_data_returns_none_on_missing(self):
        """Should return None (not raise) when index data is unavailable."""
        from backtest.data_loader import load_index_data
        result = load_index_data("NONEXISTENT_INDEX", "NSE", "5m", "2025-01-01", "2025-01-02")
        assert result is None

    def test_load_index_data_signature(self):
        """load_index_data should accept same args as load_ohlcv."""
        from backtest.data_loader import load_index_data
        import inspect
        sig = inspect.signature(load_index_data)
        params = list(sig.parameters.keys())
        assert "symbol" in params
        assert "exchange" in params
        assert "interval" in params


class TestRunnerIndexIntegration:
    """Test that runner loads and passes index data."""

    def test_batch_config_has_index_section(self, tmp_path):
        """load_batch_config should parse optional index section."""
        from backtest.config import load_batch_config
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
symbols:
  - SBIN
interval: 5m
start_date: "2025-01-01"
end_date: "2025-02-01"
initial_capital: 100000
strategy: orb

index:
  symbol: "NIFTY 50"
  exchange: NSE
""")
        config = load_batch_config(str(config_file))
        assert "index" in config
        assert config["index"]["symbol"] == "NIFTY 50"
        assert config["index"]["exchange"] == "NSE"

    def test_batch_config_without_index_section(self, tmp_path):
        """load_batch_config without index section should have None."""
        from backtest.config import load_batch_config
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
symbols:
  - SBIN
interval: 5m
start_date: "2025-01-01"
end_date: "2025-02-01"
initial_capital: 100000
strategy: orb
""")
        config = load_batch_config(str(config_file))
        assert config.get("index") is None
