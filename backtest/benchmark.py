"""
Benchmark comparison for backtest results.

Computes buy-and-hold returns and compares against strategy performance
to determine if the strategy adds alpha over passive investing.

Usage:
    from backtest.benchmark import compute_buy_and_hold, compare_strategy_vs_benchmark

    bh = compute_buy_and_hold(ohlcv_df)
    comparison = compare_strategy_vs_benchmark(
        strategy_return_pct=25.0,
        benchmark_return_pct=bh["total_return_pct"],
    )
"""

import numpy as np
import pandas as pd


def compute_buy_and_hold(df: pd.DataFrame) -> dict:
    """
    Compute buy-and-hold statistics from OHLCV DataFrame.

    Args:
        df: OHLCV DataFrame with 'close' column and DatetimeIndex.

    Returns:
        Dict with total_return_pct, annualized_return_pct, max_drawdown_pct.
    """
    closes = df["close"].values
    start_price = closes[0]
    end_price = closes[-1]

    total_return_pct = ((end_price / start_price) - 1) * 100

    # Annualize based on trading days
    trading_days = len(df)
    years = trading_days / 252
    if years > 0 and total_return_pct > -100:
        annualized = ((1 + total_return_pct / 100) ** (1 / years) - 1) * 100
    else:
        annualized = total_return_pct

    # Max drawdown
    cummax = np.maximum.accumulate(closes)
    drawdown = (closes - cummax) / cummax * 100
    max_dd = float(drawdown.min())

    return {
        "total_return_pct": round(total_return_pct, 2),
        "annualized_return_pct": round(annualized, 2),
        "max_drawdown_pct": round(max_dd, 2),
    }


def compare_strategy_vs_benchmark(
    strategy_return_pct: float,
    benchmark_return_pct: float,
    strategy_name: str = "Strategy",
    benchmark_name: str = "Buy & Hold",
) -> dict:
    """
    Compare strategy return against benchmark.

    Args:
        strategy_return_pct: Strategy total return (%).
        benchmark_return_pct: Benchmark total return (%).
        strategy_name: Name for display.
        benchmark_name: Name for display.

    Returns:
        Dict with alpha_pct, outperforms (bool), message (str).
    """
    alpha = strategy_return_pct - benchmark_return_pct
    outperforms = alpha > 0

    if outperforms:
        message = (
            f"{strategy_name} outperforms {benchmark_name} by "
            f"{alpha:+.1f}pp ({strategy_return_pct:+.1f}% vs {benchmark_return_pct:+.1f}%)"
        )
    else:
        message = (
            f"{strategy_name} underperforms {benchmark_name} by "
            f"{abs(alpha):.1f}pp ({strategy_return_pct:+.1f}% vs {benchmark_return_pct:+.1f}%)"
        )

    return {
        "alpha_pct": round(alpha, 2),
        "outperforms": outperforms,
        "strategy_return_pct": round(strategy_return_pct, 2),
        "benchmark_return_pct": round(benchmark_return_pct, 2),
        "message": message,
    }
