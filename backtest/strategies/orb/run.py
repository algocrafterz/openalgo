#!/usr/bin/env python3
"""
ORB Strategy Backtest Runner.

Usage:
    uv run python backtest/strategies/orb/run.py
    uv run python backtest/strategies/orb/run.py --config backtest/config.yaml
    uv run python backtest/strategies/orb/run.py --symbol RELIANCE --start 2025-06-01 --end 2026-01-01

Outputs:
    - Console: Trade summary statistics
    - backtest_trades.csv: Detailed trade log
    - backtest_tearsheet.html: QuantStats tearsheet (if quantstats installed)
"""

import argparse
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from backtest.config import ConfigError, load_config
from backtest.data_loader import load_ohlcv_raw
from backtest.report import generate_tearsheet, print_summary, save_trade_log
from backtest.strategies.orb import ORBConfig, ORBStrategy


def run_backtest(config: dict) -> None:
    """Run ORB backtest with the given configuration."""
    symbol = config["symbol"]
    exchange = config["exchange"]
    interval = config["interval"]
    start_date = config["start_date"]
    end_date = config["end_date"]
    initial_capital = config["initial_capital"]
    position_size_pct = config["position_size_pct"]
    orb_config = config["orb_config"]
    costs = config["costs"]
    product = config["product"]

    print(f"Loading {exchange}:{symbol} {interval} data ({start_date} to {end_date})...")
    df = load_ohlcv_raw(symbol, exchange, interval, start_date, end_date)
    print(f"Loaded {len(df)} candles")

    # Run strategy
    strategy = ORBStrategy(orb_config)
    print(f"Running {strategy.describe()}...")
    signals = strategy.generate_signals_detailed(df)

    total_entries = signals["long_entry"].sum() + signals["short_entry"].sum()
    total_exits = signals["exit"].sum()
    print(f"Signals: {total_entries} entries, {total_exits} exits")

    if total_entries == 0:
        print("No trades generated. Check your data and parameters.")
        return

    # Save trade log
    trades_csv = save_trade_log(signals, df, "backtest_trades.csv")
    print(f"Trade log saved: {trades_csv}")

    # Print summary
    print_summary(trades_csv)

    # VectorBT portfolio simulation (if available)
    try:
        import vectorbt as vbt

        fee = costs.round_trip_pct(product)
        print(f"Transaction costs: {fee*100:.4f}% per leg ({product})")

        long_entries = signals["long_entry"].values
        short_entries = signals["short_entry"].values
        exits = signals["exit"].values

        portfolio = vbt.Portfolio.from_signals(
            close=df["close"],
            entries=long_entries,
            short_entries=short_entries,
            exits=exits,
            short_exits=exits,
            size=position_size_pct,
            size_type="percent",
            fees=fee,
            init_cash=initial_capital,
            freq="5min" if "m" in interval else "1D",
        )

        print(f"\n{'='*60}")
        print("  VECTORBT PORTFOLIO STATS")
        print(f"{'='*60}")
        stats = portfolio.stats()
        print(stats)
        print(f"{'='*60}\n")

        # Generate QuantStats tearsheet from portfolio returns
        daily_returns = portfolio.daily_returns()
        if len(daily_returns) > 0:
            tearsheet_path = generate_tearsheet(
                daily_returns,
                title=f"ORB-{orb_config.orb_minutes}min | {symbol} | {start_date} to {end_date}",
                output_path="backtest_tearsheet.html",
            )
            if tearsheet_path:
                print(f"Tearsheet saved: {tearsheet_path}")

    except ImportError:
        print("\nvectorbt not installed. Install with: uv add vectorbt")
        print("Skipping portfolio simulation. Trade log is still available.")


def main():
    parser = argparse.ArgumentParser(description="ORB Strategy Backtest")
    parser.add_argument("--config", default="backtest/config.yaml", help="Config YAML path")
    parser.add_argument("--symbol", help="Override symbol")
    parser.add_argument("--exchange", help="Override exchange")
    parser.add_argument("--interval", help="Override interval")
    parser.add_argument("--start", help="Override start date (YYYY-MM-DD)")
    parser.add_argument("--end", help="Override end date (YYYY-MM-DD)")
    parser.add_argument("--capital", type=float, help="Override initial capital")
    parser.add_argument("--tp", type=float, help="Override TP multiplier (e.g., 1.5)")

    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"Config error: {e}")
        sys.exit(1)

    # CLI overrides
    if args.symbol:
        config["symbol"] = args.symbol
    if args.exchange:
        config["exchange"] = args.exchange
    if args.interval:
        config["interval"] = args.interval
    if args.start:
        config["start_date"] = args.start
    if args.end:
        config["end_date"] = args.end
    if args.capital:
        config["initial_capital"] = args.capital
    if args.tp:
        config["orb_config"] = ORBConfig(
            **{**config["orb_config"].__dict__, "tp_multiplier": args.tp}
        )

    run_backtest(config)


if __name__ == "__main__":
    main()
