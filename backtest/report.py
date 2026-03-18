"""
Backtest reporting - QuantStats tearsheet generation.
"""

import matplotlib
matplotlib.use("Agg")  # Non-interactive backend for WSL/headless

import pandas as pd


def generate_batch_tearsheet(batch_result, output_path: str = "batch_tearsheet.html") -> str:
    """
    Generate a combined HTML tearsheet for batch results.

    Uses per-symbol trade logs to construct a combined equity curve.

    Args:
        batch_result: BatchResult from run_batch().
        output_path: Path to save HTML file.

    Returns:
        Path to the generated HTML file, or empty string on failure.
    """
    try:
        import quantstats as qs
    except ImportError:
        print("quantstats not installed. Install with: uv add quantstats")
        return ""

    trades = batch_result.combined_trade_log()
    if trades.empty:
        print("No trades to generate tearsheet from.")
        return ""

    # Build daily P&L series from trade log
    if "exit_time" in trades.columns:
        trades["exit_time"] = pd.to_datetime(trades["exit_time"])
        daily_pnl = trades.groupby(trades["exit_time"].dt.date)["pnl"].sum()
        daily_pnl.index = pd.to_datetime(daily_pnl.index)

        # Convert to returns (assume initial capital from context)
        initial_capital = 100_000.0  # default
        daily_returns = daily_pnl / initial_capital

        qs.reports.html(
            daily_returns,
            title=f"Batch Backtest - {batch_result.strategy_name.upper()} "
                  f"({batch_result.start_date} to {batch_result.end_date})",
            output=output_path,
        )
        return str(output_path)

    return ""
