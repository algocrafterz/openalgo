"""Integration tests for cost-adjusted PnL pipeline.

Verifies that transaction costs and slippage are correctly wired
from config through runner to evaluate.
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import pytest

from backtest.costs import IndianCosts
from backtest.runner import _compute_trade_log, run_single_symbol, run_batch, SymbolResult
from backtest.strategies.orb import ORBConfig
from backtest.evaluate import compute_strategy_health


def _make_5m_data(days: int = 5) -> pd.DataFrame:
    """Create synthetic 5m OHLCV with predictable breakout patterns."""
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


def _make_simple_trade_log() -> pd.DataFrame:
    """Create a minimal trade log with known values for cost testing."""
    return pd.DataFrame([
        {
            "symbol": "TEST",
            "entry_time": "2025-06-16 10:00:00",
            "exit_time": "2025-06-16 11:00:00",
            "direction": "LONG",
            "entry_price": 100.0,
            "exit_price": 102.0,
            "sl_price": 98.0,
            "tp_price": 104.0,
            "gross_pnl": 2.0,
            "net_pnl": 1.5,       # after costs
            "cost": 0.5,
            "gross_r_multiple": 1.0,  # 2.0 / 2.0
            "net_r_multiple": 0.75,   # 1.5 / 2.0
            "cost_r": 0.25,
            "pnl": 1.5,           # alias for net_pnl
            "r_multiple": 0.75,   # alias for net_r_multiple
            "exit_reason": "TP",
        },
        {
            "symbol": "TEST",
            "entry_time": "2025-06-17 10:00:00",
            "exit_time": "2025-06-17 11:00:00",
            "direction": "SHORT",
            "entry_price": 100.0,
            "exit_price": 101.0,
            "sl_price": 102.0,
            "tp_price": 98.0,
            "gross_pnl": -1.0,
            "net_pnl": -1.5,
            "cost": 0.5,
            "gross_r_multiple": -0.5,
            "net_r_multiple": -0.75,
            "cost_r": 0.25,
            "pnl": -1.5,
            "r_multiple": -0.75,
            "exit_reason": "SL",
        },
    ])


# ---------------------------------------------------------------------------
# Tests for _compute_trade_log with costs
# ---------------------------------------------------------------------------

class TestTradeLogCostColumns:
    """Verify _compute_trade_log produces cost-related columns."""

    @patch("backtest.runner.load_ohlcv_raw")
    def _run_with_costs(self, mock_load, slippage_pct=0.0):
        """Helper: run a single symbol with costs and return trade log."""
        mock_load.return_value = _make_5m_data(days=5)
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
            costs=IndianCosts(), product="MIS", slippage_pct=slippage_pct,
        )
        return result

    def test_trade_log_has_cost_columns(self):
        """Trade log must include gross_pnl, net_pnl, cost, gross_r_multiple, net_r_multiple."""
        result = self._run_with_costs()
        if result.trade_log.empty:
            pytest.skip("No trades generated with test data")

        required_cols = ["gross_pnl", "net_pnl", "cost", "gross_r_multiple",
                         "net_r_multiple", "cost_r"]
        for col in required_cols:
            assert col in result.trade_log.columns, f"Missing column: {col}"

    def test_pnl_is_alias_for_net_pnl(self):
        """pnl column must equal net_pnl for backward compatibility."""
        result = self._run_with_costs()
        if result.trade_log.empty:
            pytest.skip("No trades generated with test data")

        pd.testing.assert_series_equal(
            result.trade_log["pnl"], result.trade_log["net_pnl"],
            check_names=False,
        )

    def test_r_multiple_is_alias_for_net_r_multiple(self):
        """r_multiple column must equal net_r_multiple."""
        result = self._run_with_costs()
        if result.trade_log.empty:
            pytest.skip("No trades generated with test data")

        pd.testing.assert_series_equal(
            result.trade_log["r_multiple"], result.trade_log["net_r_multiple"],
            check_names=False,
        )

    def test_net_pnl_less_than_gross_pnl(self):
        """Costs always reduce PnL: net_pnl < gross_pnl for every trade."""
        result = self._run_with_costs()
        if result.trade_log.empty:
            pytest.skip("No trades generated with test data")

        for _, row in result.trade_log.iterrows():
            assert row["net_pnl"] < row["gross_pnl"], (
                f"net_pnl ({row['net_pnl']}) should be less than "
                f"gross_pnl ({row['gross_pnl']})"
            )

    def test_cost_is_positive(self):
        """Cost must be positive for every trade."""
        result = self._run_with_costs()
        if result.trade_log.empty:
            pytest.skip("No trades generated with test data")

        assert (result.trade_log["cost"] > 0).all()

    def test_cost_r_matches_calculation(self):
        """cost_r = cost / risk, where risk = abs(entry - sl)."""
        result = self._run_with_costs()
        if result.trade_log.empty:
            pytest.skip("No trades generated with test data")

        for _, row in result.trade_log.iterrows():
            risk = abs(row["entry_price"] - row["sl_price"])
            if risk > 0:
                expected_cost_r = row["cost"] / risk
                assert abs(row["cost_r"] - expected_cost_r) < 0.001


class TestSlippageEffect:
    """Verify slippage increases costs."""

    @patch("backtest.runner.load_ohlcv_raw")
    def test_slippage_increases_cost(self, mock_load):
        """Running with slippage_pct > 0 should produce higher costs."""
        mock_load.return_value = _make_5m_data(days=5)
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

        result_no_slip = run_single_symbol(
            symbol="SBIN", exchange="NSE", interval="5m",
            start_date="2025-01-01", end_date="2025-06-01",
            orb_config=config, strategy_name="orb",
            costs=IndianCosts(), product="MIS", slippage_pct=0.0,
        )
        result_with_slip = run_single_symbol(
            symbol="SBIN", exchange="NSE", interval="5m",
            start_date="2025-01-01", end_date="2025-06-01",
            orb_config=config, strategy_name="orb",
            costs=IndianCosts(), product="MIS", slippage_pct=0.001,
        )

        if result_no_slip.trade_log.empty or result_with_slip.trade_log.empty:
            pytest.skip("No trades generated")

        avg_cost_no_slip = result_no_slip.trade_log["cost"].mean()
        avg_cost_with_slip = result_with_slip.trade_log["cost"].mean()
        assert avg_cost_with_slip > avg_cost_no_slip


class TestZeroCostsBackwardCompat:
    """With no costs and no slippage, net = gross (old behavior)."""

    @patch("backtest.runner.load_ohlcv_raw")
    def test_zero_costs_matches_old_behavior(self, mock_load):
        """costs=None and slippage_pct=0 should produce net_pnl == gross_pnl."""
        mock_load.return_value = _make_5m_data(days=5)
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
            costs=None, product="MIS", slippage_pct=0.0,
        )

        if result.trade_log.empty:
            pytest.skip("No trades generated")

        pd.testing.assert_series_equal(
            result.trade_log["net_pnl"], result.trade_log["gross_pnl"],
            check_names=False,
        )
        assert (result.trade_log["cost"] == 0).all()


class TestEvaluateUsesNetMetrics:
    """Evaluation functions must use net (cost-adjusted) R-multiples."""

    def test_evaluate_uses_net_r(self):
        """compute_strategy_health uses r_multiple which is net_r_multiple."""
        trades = _make_simple_trade_log()
        health = compute_strategy_health(trades)

        # r_multiple column = net values: [0.75, -0.75]
        # expectancy = mean = 0.0
        assert health["expectancy_r"] == 0.0

    def test_gross_expectancy_higher_than_net(self):
        """If we compute on gross_r_multiple, expectancy should be higher."""
        trades = _make_simple_trade_log()

        # Net: mean of [0.75, -0.75] = 0.0
        net_health = compute_strategy_health(trades)

        # Gross: mean of [1.0, -0.5] = 0.25
        trades_gross = trades.copy()
        trades_gross["r_multiple"] = trades_gross["gross_r_multiple"]
        gross_health = compute_strategy_health(trades_gross)

        assert gross_health["expectancy_r"] > net_health["expectancy_r"]


class TestSymbolResultCostFields:
    """SymbolResult should include cost summary fields."""

    @patch("backtest.runner.load_ohlcv_raw")
    def test_symbol_result_has_cost_fields(self, mock_load):
        mock_load.return_value = _make_5m_data(days=5)
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
            costs=IndianCosts(), product="MIS", slippage_pct=0.0,
        )
        assert hasattr(result, "total_gross_pnl")
        assert hasattr(result, "total_costs")


class TestBatchCostTotals:
    """Batch run with costs should populate cost totals."""

    @patch("backtest.runner.load_ohlcv_raw")
    def test_batch_passes_costs_through(self, mock_load):
        mock_load.return_value = _make_5m_data(days=5)
        config = {
            "symbols": [{"symbol": "SBIN", "exchange": "NSE"}],
            "interval": "5m",
            "start_date": "2025-01-01",
            "end_date": "2025-06-01",
            "initial_capital": 100000,
            "position_size_pct": 0.10,
            "product": "MIS",
            "strategy": "orb",
            "slippage_pct": 0.0005,
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
            "costs": IndianCosts(),
        }
        batch = run_batch(config)
        for r in batch.results:
            if r.total_trades > 0:
                assert r.total_costs > 0, "Costs should be applied in batch mode"


class TestConfigSlippagePct:
    """Config loader should parse slippage_pct."""

    def test_batch_config_has_slippage(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
symbols:
  - SBIN
interval: 5m
start_date: "2025-01-01"
end_date: "2025-06-01"
initial_capital: 100000
product: MIS
strategy: orb
slippage_pct: 0.001
""")
        from backtest.config import load_batch_config
        config = load_batch_config(str(config_file))
        assert config["slippage_pct"] == 0.001

    def test_batch_config_default_slippage(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
symbols:
  - SBIN
interval: 5m
start_date: "2025-01-01"
end_date: "2025-06-01"
initial_capital: 100000
product: MIS
strategy: orb
""")
        from backtest.config import load_batch_config
        config = load_batch_config(str(config_file))
        assert "slippage_pct" in config
        assert config["slippage_pct"] == 0.0005  # default
