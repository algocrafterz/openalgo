"""Tests for consolidated batch reporting."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest

from backtest.report import generate_batch_summary_csv
from backtest.runner import BatchResult, SymbolResult


def _make_symbol_result(symbol: str, trades: int = 10, pnl: float = 100.0) -> SymbolResult:
    return SymbolResult(
        symbol=symbol, exchange="NSE",
        total_trades=trades, winners=int(trades * 0.6), losers=int(trades * 0.4),
        win_rate=60.0, total_pnl=pnl,
        avg_r_multiple=0.5, profit_factor=1.5,
        long_trades=int(trades * 0.7), short_trades=int(trades * 0.3),
        long_pnl=pnl * 0.7, short_pnl=pnl * 0.3,
        exit_reasons={"TP": 6, "SL": 3, "TIME": 1},
        error=None,
        trade_log=pd.DataFrame({
            "symbol": [symbol] * trades,
            "pnl": [pnl / trades] * trades,
            "r_multiple": [0.5] * trades,
            "exit_reason": ["TP"] * 6 + ["SL"] * 3 + ["TIME"] * 1,
        }),
    )


def _make_batch_result(n_symbols: int = 3) -> BatchResult:
    results = [_make_symbol_result(f"SYM{i}", trades=10, pnl=100.0 * i)
               for i in range(1, n_symbols + 1)]
    return BatchResult(
        results=results,
        total_symbols=n_symbols,
        strategy_name="orb",
        interval="5m",
        start_date="2025-01-01",
        end_date="2026-03-01",
    )


class TestGenerateBatchSummaryCsv:
    def test_saves_csv(self):
        batch = _make_batch_result(3)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = generate_batch_summary_csv(batch, output_dir=tmpdir)
            assert Path(path).exists()
            df = pd.read_csv(path)
            assert len(df) == 3
            assert "symbol" in df.columns

    def test_includes_all_symbols(self):
        batch = _make_batch_result(5)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = generate_batch_summary_csv(batch, output_dir=tmpdir)
            df = pd.read_csv(path)
            assert len(df) == 5
            assert list(df["symbol"]) == [f"SYM{i}" for i in range(1, 6)]

    def test_saves_trade_log(self):
        batch = _make_batch_result(2)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = generate_batch_summary_csv(batch, output_dir=tmpdir)
            trades_path = Path(tmpdir) / "batch_trades.csv"
            assert trades_path.exists()
            trades = pd.read_csv(trades_path)
            assert len(trades) == 20  # 2 symbols * 10 trades

    def test_handles_empty_results(self):
        batch = BatchResult(
            results=[], total_symbols=0,
            strategy_name="orb", interval="5m",
            start_date="2025-01-01", end_date="2026-03-01",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = generate_batch_summary_csv(batch, output_dir=tmpdir)
            df = pd.read_csv(path)
            assert len(df) == 0
