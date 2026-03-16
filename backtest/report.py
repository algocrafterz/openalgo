"""
Backtest reporting - QuantStats tearsheet and trade log generation.
"""

import matplotlib
matplotlib.use("Agg")  # Non-interactive backend for WSL/headless

from pathlib import Path

import pandas as pd


def generate_tearsheet(
    returns: pd.Series,
    benchmark_returns: pd.Series | None = None,
    output_path: str = "backtest_tearsheet.html",
    title: str = "ORB Strategy Backtest",
) -> str:
    """
    Generate a QuantStats HTML tearsheet.

    Args:
        returns: Daily returns Series (from portfolio).
        benchmark_returns: Optional benchmark daily returns.
        output_path: Path to save HTML file.
        title: Report title.

    Returns:
        Path to the generated HTML file.
    """
    try:
        import quantstats as qs
    except ImportError:
        print("quantstats not installed. Install with: uv add quantstats")
        print("Skipping tearsheet generation.")
        return ""

    qs.reports.html(
        returns,
        benchmark=benchmark_returns,
        title=title,
        output=output_path,
    )
    return str(output_path)


def save_trade_log(
    signals_df: pd.DataFrame,
    df: pd.DataFrame,
    output_path: str = "backtest_trades.csv",
) -> str:
    """
    Extract and save trade log from detailed signals.

    Args:
        signals_df: Output from ORBStrategy.generate_signals_detailed()
        df: Original OHLCV DataFrame.
        output_path: Path to save CSV.

    Returns:
        Path to the generated CSV file.
    """
    entries = signals_df[signals_df["long_entry"] | signals_df["short_entry"]].copy()
    exits = signals_df[signals_df["exit"]].copy()

    trades = []
    exit_iter = iter(exits.iterrows())

    for entry_time, entry_row in entries.iterrows():
        direction = "LONG" if entry_row["long_entry"] else "SHORT"
        entry_price = entry_row["entry_price"]
        sl_price = entry_row["sl_price"]
        tp_price = entry_row["tp_price"]

        # Find corresponding exit
        exit_time = None
        exit_price = None
        exit_reason = "unknown"

        for ex_time, ex_row in exit_iter:
            if ex_time > entry_time:
                exit_time = ex_time
                exit_price = df.loc[ex_time, "close"] if ex_time in df.index else None
                # Determine exit reason
                if exit_price is not None:
                    if direction == "LONG":
                        if exit_price <= sl_price:
                            exit_reason = "SL"
                        elif exit_price >= tp_price:
                            exit_reason = "TP"
                        else:
                            exit_reason = "TIME"
                    else:
                        if exit_price >= sl_price:
                            exit_reason = "SL"
                        elif exit_price <= tp_price:
                            exit_reason = "TP"
                        else:
                            exit_reason = "TIME"
                break

        if exit_price is None:
            continue

        if direction == "LONG":
            pnl = exit_price - entry_price
        else:
            pnl = entry_price - exit_price

        pnl_pct = (pnl / entry_price) * 100
        risk = abs(entry_price - sl_price)
        r_multiple = pnl / risk if risk > 0 else 0

        trades.append({
            "entry_time": entry_time,
            "exit_time": exit_time,
            "direction": direction,
            "entry_price": round(entry_price, 2),
            "exit_price": round(exit_price, 2),
            "sl_price": round(sl_price, 2),
            "tp_price": round(tp_price, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "r_multiple": round(r_multiple, 2),
            "exit_reason": exit_reason,
            "orb_high": round(entry_row["orb_high"], 2) if not pd.isna(entry_row["orb_high"]) else None,
            "orb_low": round(entry_row["orb_low"], 2) if not pd.isna(entry_row["orb_low"]) else None,
        })

    trade_df = pd.DataFrame(trades)
    if not trade_df.empty:
        trade_df.to_csv(output_path, index=False)

    return str(output_path)


def print_summary(trades_csv: str) -> None:
    """Print a summary of backtest results from trade log."""
    path = Path(trades_csv)
    if not path.exists():
        print("No trade log found.")
        return

    df = pd.read_csv(path)
    if df.empty:
        print("No trades generated.")
        return

    total = len(df)
    winners = len(df[df["pnl"] > 0])
    losers = len(df[df["pnl"] < 0])
    win_rate = (winners / total) * 100 if total > 0 else 0

    total_pnl = df["pnl"].sum()
    avg_pnl = df["pnl"].mean()
    avg_winner = df[df["pnl"] > 0]["pnl"].mean() if winners > 0 else 0
    avg_loser = df[df["pnl"] < 0]["pnl"].mean() if losers > 0 else 0
    profit_factor = abs(df[df["pnl"] > 0]["pnl"].sum() / df[df["pnl"] < 0]["pnl"].sum()) if losers > 0 else float("inf")

    avg_r = df["r_multiple"].mean()
    max_r = df["r_multiple"].max()
    min_r = df["r_multiple"].min()

    # Exit reason breakdown
    exit_counts = df["exit_reason"].value_counts()

    # Direction breakdown
    longs = df[df["direction"] == "LONG"]
    shorts = df[df["direction"] == "SHORT"]

    print(f"\n{'='*60}")
    print(f"  BACKTEST RESULTS")
    print(f"{'='*60}")
    print(f"  Total Trades:    {total}")
    print(f"  Winners:         {winners} ({win_rate:.1f}%)")
    print(f"  Losers:          {losers}")
    print(f"  Total P&L:       {total_pnl:,.2f}")
    print(f"  Avg P&L/trade:   {avg_pnl:,.2f}")
    print(f"  Avg Winner:      {avg_winner:,.2f}")
    print(f"  Avg Loser:       {avg_loser:,.2f}")
    print(f"  Profit Factor:   {profit_factor:.2f}")
    print(f"  Avg R-Multiple:  {avg_r:.2f}")
    print(f"  Best Trade (R):  {max_r:.2f}")
    print(f"  Worst Trade (R): {min_r:.2f}")
    print(f"{'='*60}")
    print(f"  Exit Reasons:")
    for reason, count in exit_counts.items():
        print(f"    {reason}: {count} ({count/total*100:.1f}%)")
    print(f"{'='*60}")
    print(f"  Long Trades:  {len(longs)} (P&L: {longs['pnl'].sum():,.2f})")
    print(f"  Short Trades: {len(shorts)} (P&L: {shorts['pnl'].sum():,.2f})")
    print(f"{'='*60}\n")


def generate_batch_summary_csv(batch_result, output_dir: str = "backtest/results") -> str:
    """
    Save batch backtest results to CSV files.

    Args:
        batch_result: BatchResult from run_batch().
        output_dir: Directory to save output files.

    Returns:
        Path to the summary CSV file.
    """
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    # Summary CSV
    summary = batch_result.summary_df()
    summary_path = output / "batch_summary.csv"
    summary.to_csv(summary_path, index=False)

    # Combined trade log
    trades = batch_result.combined_trade_log()
    if not trades.empty:
        trades_path = output / "batch_trades.csv"
        trades.to_csv(trades_path, index=False)

    return str(summary_path)


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
