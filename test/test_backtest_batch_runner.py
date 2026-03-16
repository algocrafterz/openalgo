"""Tests for multi-symbol batch backtest runner."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import pytest

from backtest.runner import run_single_symbol, run_batch, BatchResult, SymbolResult
from backtest.strategies.orb import ORBConfig


def _make_5m_data(symbol: str = "SBIN", days: int = 3) -> pd.DataFrame:
    """Create synthetic 5m OHLCV for testing (multiple sessions)."""
    frames = []
    base_price = 500.0

    for d in range(days):
        date = f"2025-06-{16 + d:02d}"
        times = pd.date_range(f"{date} 09:15:00", f"{date} 15:25:00", freq="5min")
        n = len(times)
        np.random.seed(42 + d)

        opens = base_price + np.random.randn(n).cumsum() * 0.5
        highs = opens + np.abs(np.random.randn(n)) * 1.5
        lows = opens - np.abs(np.random.randn(n)) * 1.5
        closes = opens + np.random.randn(n) * 0.5
        volumes = np.random.randint(50_000, 200_000, n).astype(float)

        df = pd.DataFrame({
            "open": opens, "high": highs, "low": lows,
            "close": closes, "volume": volumes,
        }, index=times)
        frames.append(df)

    return pd.concat(frames)


class TestSymbolResult:
    def test_symbol_result_fields(self):
        result = SymbolResult(
            symbol="SBIN", exchange="NSE",
            total_trades=10, winners=6, losers=4,
            win_rate=60.0, total_pnl=500.0,
            avg_r_multiple=0.5, profit_factor=1.5,
            long_trades=7, short_trades=3,
            long_pnl=400.0, short_pnl=100.0,
            exit_reasons={"TP": 6, "SL": 3, "TIME": 1},
            error=None,
        )
        assert result.symbol == "SBIN"
        assert result.win_rate == 60.0

    def test_error_result(self):
        result = SymbolResult(
            symbol="SBIN", exchange="NSE",
            total_trades=0, winners=0, losers=0,
            win_rate=0.0, total_pnl=0.0,
            avg_r_multiple=0.0, profit_factor=0.0,
            long_trades=0, short_trades=0,
            long_pnl=0.0, short_pnl=0.0,
            exit_reasons={},
            error="No data available",
        )
        assert result.error == "No data available"
        assert result.total_trades == 0


class TestRunSingleSymbol:
    @patch("backtest.runner.load_ohlcv_raw")
    def test_returns_symbol_result(self, mock_load):
        mock_load.return_value = _make_5m_data("SBIN", days=5)

        config = ORBConfig(
            orb_minutes=15, breakout_buffer_pct=0.0,
            enable_volume_filter=False, enable_trend_filter=False,
            enable_htf_filter=False, enable_index_filter=False,
            enable_gap_filter=False, enable_orb_range_filter=False,
            enable_min_entry_time=False,
            stop_mode="PCT_BASED", pct_based_stop=2.0,
            tp_multiplier=1.5,
            entry_cutoff_hour=14, time_exit_hour=15,
        )
        result = run_single_symbol(
            symbol="SBIN", exchange="NSE", interval="5m",
            start_date="2025-01-01", end_date="2025-06-01",
            orb_config=config, strategy_name="orb",
        )
        assert isinstance(result, SymbolResult)
        assert result.symbol == "SBIN"
        assert result.error is None

    @patch("backtest.runner.load_ohlcv_raw")
    def test_handles_no_data(self, mock_load):
        mock_load.side_effect = ValueError("No data in Historify")
        result = run_single_symbol(
            symbol="NOSYMBOL", exchange="NSE", interval="5m",
            start_date="2025-01-01", end_date="2025-06-01",
            orb_config=ORBConfig(), strategy_name="orb",
        )
        assert result.error is not None
        assert result.total_trades == 0

    @patch("backtest.runner.load_ohlcv_raw")
    def test_returns_trade_log(self, mock_load):
        mock_load.return_value = _make_5m_data("SBIN", days=5)
        config = ORBConfig(
            orb_minutes=15, breakout_buffer_pct=0.0,
            enable_volume_filter=False, enable_trend_filter=False,
            enable_htf_filter=False, enable_index_filter=False,
            enable_gap_filter=False, enable_orb_range_filter=False,
            enable_min_entry_time=False,
            stop_mode="PCT_BASED", pct_based_stop=2.0,
            tp_multiplier=1.5,
            entry_cutoff_hour=14, time_exit_hour=15,
        )
        result = run_single_symbol(
            symbol="SBIN", exchange="NSE", interval="5m",
            start_date="2025-01-01", end_date="2025-06-01",
            orb_config=config, strategy_name="orb",
        )
        # trade_log is a DataFrame (possibly empty)
        assert hasattr(result, "trade_log")
        assert isinstance(result.trade_log, pd.DataFrame)


class TestRunBatch:
    @patch("backtest.runner.load_ohlcv_raw")
    def test_runs_multiple_symbols(self, mock_load):
        mock_load.return_value = _make_5m_data("TEST", days=5)

        config = {
            "symbols": [
                {"symbol": "SBIN", "exchange": "NSE"},
                {"symbol": "PNB", "exchange": "NSE"},
            ],
            "interval": "5m",
            "start_date": "2025-01-01",
            "end_date": "2025-06-01",
            "initial_capital": 100000,
            "position_size_pct": 0.10,
            "product": "MIS",
            "strategy": "orb",
            "orb_config": ORBConfig(
                orb_minutes=15, breakout_buffer_pct=0.0,
                enable_volume_filter=False, enable_trend_filter=False,
                enable_htf_filter=False, enable_index_filter=False,
                enable_gap_filter=False, enable_orb_range_filter=False,
                enable_min_entry_time=False,
                stop_mode="PCT_BASED", pct_based_stop=2.0,
                tp_multiplier=1.5,
                entry_cutoff_hour=14, time_exit_hour=15,
            ),
            "costs": MagicMock(round_trip_pct=MagicMock(return_value=0.0001)),
        }
        batch_result = run_batch(config)
        assert isinstance(batch_result, BatchResult)
        assert len(batch_result.results) == 2
        assert batch_result.total_symbols == 2

    @patch("backtest.runner.load_ohlcv_raw")
    def test_skips_failed_symbols(self, mock_load):
        """If one symbol fails, others still run."""
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("No data")
            return _make_5m_data("TEST", days=5)

        mock_load.side_effect = side_effect

        config = {
            "symbols": [
                {"symbol": "NOSYMBOL", "exchange": "NSE"},
                {"symbol": "SBIN", "exchange": "NSE"},
            ],
            "interval": "5m",
            "start_date": "2025-01-01",
            "end_date": "2025-06-01",
            "initial_capital": 100000,
            "position_size_pct": 0.10,
            "product": "MIS",
            "strategy": "orb",
            "orb_config": ORBConfig(
                orb_minutes=15, breakout_buffer_pct=0.0,
                enable_volume_filter=False, enable_trend_filter=False,
                enable_htf_filter=False, enable_index_filter=False,
                enable_gap_filter=False, enable_orb_range_filter=False,
                enable_min_entry_time=False,
                stop_mode="PCT_BASED", pct_based_stop=2.0,
                tp_multiplier=1.5,
                entry_cutoff_hour=14, time_exit_hour=15,
            ),
            "costs": MagicMock(round_trip_pct=MagicMock(return_value=0.0001)),
        }
        batch_result = run_batch(config)
        assert len(batch_result.results) == 2
        errors = [r for r in batch_result.results if r.error is not None]
        successes = [r for r in batch_result.results if r.error is None]
        assert len(errors) == 1
        assert len(successes) == 1

    @patch("backtest.runner.load_ohlcv_raw")
    def test_batch_result_has_summary(self, mock_load):
        mock_load.return_value = _make_5m_data("TEST", days=5)

        config = {
            "symbols": [{"symbol": "SBIN", "exchange": "NSE"}],
            "interval": "5m",
            "start_date": "2025-01-01",
            "end_date": "2025-06-01",
            "initial_capital": 100000,
            "position_size_pct": 0.10,
            "product": "MIS",
            "strategy": "orb",
            "orb_config": ORBConfig(
                orb_minutes=15, breakout_buffer_pct=0.0,
                enable_volume_filter=False, enable_trend_filter=False,
                enable_htf_filter=False, enable_index_filter=False,
                enable_gap_filter=False, enable_orb_range_filter=False,
                enable_min_entry_time=False,
                stop_mode="PCT_BASED", pct_based_stop=2.0,
                tp_multiplier=1.5,
                entry_cutoff_hour=14, time_exit_hour=15,
            ),
            "costs": MagicMock(round_trip_pct=MagicMock(return_value=0.0001)),
        }
        batch_result = run_batch(config)
        summary = batch_result.summary_df()
        assert isinstance(summary, pd.DataFrame)
        assert "symbol" in summary.columns
        assert "total_trades" in summary.columns
