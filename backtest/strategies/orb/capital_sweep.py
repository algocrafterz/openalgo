#!/usr/bin/env python3
"""
Capital sweep: run ORB backtest across multiple capital levels to find optimal sizing.

Usage:
    uv run python backtest/strategies/orb/capital_sweep.py
    uv run python backtest/strategies/orb/capital_sweep.py --symbol RELIANCE
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

import numpy as np
import pandas as pd

from backtest.config import load_config
from backtest.costs import IndianCosts
from backtest.data_loader import load_ohlcv_raw
from backtest.strategies.orb import ORBConfig, ORBStrategy


def sweep(config: dict, capital_levels: list[float]) -> pd.DataFrame:
    """Run backtest at each capital level and collect key metrics."""
    symbol = config["symbol"]
    exchange = config["exchange"]
    interval = config["interval"]
    start_date = config["start_date"]
    end_date = config["end_date"]
    orb_config = config["orb_config"]
    costs = config["costs"]
    product = config["product"]
    position_size_pct = config["position_size_pct"]

    print(f"Loading {exchange}:{symbol} {interval} data ({start_date} to {end_date})...")
    df = load_ohlcv_raw(symbol, exchange, interval, start_date, end_date)
    print(f"Loaded {len(df)} candles")

    # Generate signals once (they don't depend on capital)
    strategy = ORBStrategy(orb_config)
    signals = strategy.generate_signals_detailed(df)

    total_entries = signals["long_entry"].sum() + signals["short_entry"].sum()
    print(f"Strategy: {strategy.describe()} | Signals: {total_entries} entries")

    if total_entries == 0:
        print("No trades generated.")
        return pd.DataFrame()

    try:
        import vectorbt as vbt
    except ImportError:
        print("vectorbt required: uv add vectorbt")
        sys.exit(1)

    fee = costs.round_trip_pct(product)

    long_entries = signals["long_entry"].values
    short_entries = signals["short_entry"].values
    exits = signals["exit"].values
    freq = "5min" if "m" in interval else "1D"

    results = []
    for capital in capital_levels:
        portfolio = vbt.Portfolio.from_signals(
            close=df["close"],
            entries=long_entries,
            short_entries=short_entries,
            exits=exits,
            short_exits=exits,
            size=position_size_pct,
            size_type="percent",
            fees=fee,
            init_cash=capital,
            freq=freq,
        )

        stats = portfolio.stats()
        total_return = stats.get("Total Return [%]", 0.0)
        total_pnl = stats.get("End Value", capital) - capital
        max_dd = stats.get("Max Drawdown [%]", 0.0)
        sharpe = stats.get("Sharpe Ratio", 0.0)
        win_rate = stats.get("Win Rate [%]", 0.0)
        num_trades = stats.get("Total Trades", 0)
        avg_trade_pct = stats.get("Avg Winning Trade [%]", 0.0)

        results.append({
            "Capital (INR)": int(capital),
            "Net P&L": round(total_pnl, 2),
            "Return %": round(total_return, 2),
            "Max DD %": round(max_dd, 2),
            "Sharpe": round(sharpe, 3) if not np.isnan(sharpe) else 0.0,
            "Win Rate %": round(win_rate, 1),
            "Trades": int(num_trades),
        })

    return pd.DataFrame(results)


def main():
    parser = argparse.ArgumentParser(description="ORB Capital Sweep")
    parser.add_argument("--config", default="backtest/config.yaml")
    parser.add_argument("--symbol", help="Override symbol")
    parser.add_argument("--min-capital", type=int, default=5000, help="Min capital (INR)")
    parser.add_argument("--max-capital", type=int, default=500000, help="Max capital (INR)")
    parser.add_argument("--step", type=int, default=0, help="Step size (0=auto)")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.symbol:
        config["symbol"] = args.symbol

    # Build capital levels in sensible INR multiples
    if args.step > 0:
        levels = list(range(args.min_capital, args.max_capital + 1, args.step))
    else:
        # Auto: 5K-50K by 5K, 50K-200K by 25K, 200K-500K by 50K
        levels = list(range(5000, 50001, 5000))
        levels += list(range(75000, 200001, 25000))
        levels += list(range(250000, 500001, 50000))
        levels = [l for l in levels if args.min_capital <= l <= args.max_capital]

    print(f"Sweeping {len(levels)} capital levels: {levels[0]:,} to {levels[-1]:,} INR\n")

    results_df = sweep(config, levels)

    if results_df.empty:
        return

    # Display results
    print(f"\n{'='*85}")
    print(f"  CAPITAL SWEEP RESULTS — {config['symbol']}")
    print(f"{'='*85}")
    print(results_df.to_string(index=False))
    print(f"{'='*85}")

    # Find optimal
    if len(results_df) > 0:
        # Best by return %
        best_ret = results_df.loc[results_df["Return %"].idxmax()]
        # Best by Sharpe
        best_sharpe = results_df.loc[results_df["Sharpe"].idxmax()]
        # Best by absolute P&L
        best_pnl = results_df.loc[results_df["Net P&L"].idxmax()]

        print(f"\nOptimal by Return %:  {int(best_ret['Capital (INR)']):>8,} INR  ->  {best_ret['Return %']:>7.2f}%  (P&L: {best_ret['Net P&L']:>10,.2f})")
        print(f"Optimal by Sharpe:    {int(best_sharpe['Capital (INR)']):>8,} INR  ->  Sharpe {best_sharpe['Sharpe']:>6.3f}  (P&L: {best_sharpe['Net P&L']:>10,.2f})")
        print(f"Optimal by Net P&L:   {int(best_pnl['Capital (INR)']):>8,} INR  ->  P&L {best_pnl['Net P&L']:>10,.2f}  (Return: {best_pnl['Return %']:>7.2f}%)")

    # Save CSV
    results_df.to_csv("capital_sweep_results.csv", index=False)
    print(f"\nResults saved: capital_sweep_results.csv")


if __name__ == "__main__":
    main()
