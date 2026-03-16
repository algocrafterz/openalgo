#!/usr/bin/env python3
"""
Multi-symbol batch backtest runner.

Runs a strategy across all symbols in a batch config, collects per-symbol
results, and generates consolidated reports.

Usage:
    uv run python backtest/runner.py
    uv run python backtest/runner.py --config backtest/strategies/orb/config.yaml
"""

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from backtest.data_loader import load_index_data, load_ohlcv_raw
from backtest.strategies import get_strategy


@dataclass
class SymbolResult:
    """Results for a single symbol backtest."""

    symbol: str
    exchange: str
    total_trades: int
    winners: int
    losers: int
    win_rate: float
    total_pnl: float
    avg_r_multiple: float
    profit_factor: float
    long_trades: int
    short_trades: int
    long_pnl: float
    short_pnl: float
    exit_reasons: dict
    error: str | None
    total_gross_pnl: float = 0.0
    total_costs: float = 0.0
    trade_log: pd.DataFrame = field(default_factory=pd.DataFrame)


@dataclass
class BatchResult:
    """Aggregated results for a batch backtest."""

    results: list[SymbolResult]
    total_symbols: int
    strategy_name: str
    interval: str
    start_date: str
    end_date: str

    def summary_df(self) -> pd.DataFrame:
        """Generate summary DataFrame of all symbol results."""
        columns = ["symbol", "exchange", "total_trades", "winners", "losers",
                    "win_rate", "total_pnl", "gross_pnl", "costs", "avg_r",
                    "profit_factor", "long_trades", "short_trades",
                    "long_pnl", "short_pnl", "error"]
        if not self.results:
            return pd.DataFrame(columns=columns)

        rows = []
        for r in self.results:
            rows.append({
                "symbol": r.symbol,
                "exchange": r.exchange,
                "total_trades": r.total_trades,
                "winners": r.winners,
                "losers": r.losers,
                "win_rate": round(r.win_rate, 1),
                "total_pnl": round(r.total_pnl, 2),
                "gross_pnl": round(r.total_gross_pnl, 2),
                "costs": round(r.total_costs, 2),
                "avg_r": round(r.avg_r_multiple, 2),
                "profit_factor": round(r.profit_factor, 2),
                "long_trades": r.long_trades,
                "short_trades": r.short_trades,
                "long_pnl": round(r.long_pnl, 2),
                "short_pnl": round(r.short_pnl, 2),
                "error": r.error or "",
            })
        return pd.DataFrame(rows)

    def combined_trade_log(self) -> pd.DataFrame:
        """Combine all symbol trade logs into a single DataFrame."""
        logs = [r.trade_log for r in self.results if not r.trade_log.empty]
        if not logs:
            return pd.DataFrame()
        return pd.concat(logs, ignore_index=True)


def _compute_trade_log(
    signals: pd.DataFrame, df: pd.DataFrame, symbol: str,
    costs=None, product: str = "MIS", slippage_pct: float = 0.0,
) -> pd.DataFrame:
    """Extract trade log from detailed signals with optional cost/slippage deduction."""
    entries = signals[signals["long_entry"] | signals["short_entry"]].copy()
    exits = signals[signals["exit"]].copy()

    trades = []
    exit_iter = iter(exits.iterrows())

    for entry_time, entry_row in entries.iterrows():
        direction = "LONG" if entry_row["long_entry"] else "SHORT"
        entry_price = entry_row["entry_price"]
        sl_price = entry_row["sl_price"]
        tp_price = entry_row["tp_price"]

        exit_time = None
        exit_price = None
        exit_reason = "unknown"

        for ex_time, _ex_row in exit_iter:
            if ex_time > entry_time:
                exit_time = ex_time
                exit_price = df.loc[ex_time, "close"] if ex_time in df.index else None
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

        gross_pnl = (exit_price - entry_price) if direction == "LONG" else (entry_price - exit_price)
        risk = abs(entry_price - sl_price)

        # Compute transaction cost + slippage
        cost = 0.0
        if costs is not None:
            # Use cost_per_trade for accurate per-leg costs (assume 1 share)
            is_buy_entry = direction == "LONG"
            cost += float(costs.cost_per_trade(entry_price, is_buy=is_buy_entry, product=product))
            cost += float(costs.cost_per_trade(exit_price, is_buy=(not is_buy_entry), product=product))
        # Slippage: symmetric cost on both legs
        cost += float(entry_price + exit_price) * slippage_pct

        net_pnl = float(gross_pnl) - cost
        gross_r = float(gross_pnl) / risk if risk > 0 else 0.0
        cost_r = cost / risk if risk > 0 else 0.0
        net_r = gross_r - cost_r

        trades.append({
            "symbol": symbol,
            "entry_time": entry_time,
            "exit_time": exit_time,
            "direction": direction,
            "entry_price": round(entry_price, 2),
            "exit_price": round(exit_price, 2),
            "sl_price": round(sl_price, 2),
            "tp_price": round(tp_price, 2),
            "gross_pnl": round(gross_pnl, 4),
            "net_pnl": round(net_pnl, 4),
            "cost": round(cost, 4),
            "gross_r_multiple": round(gross_r, 4),
            "net_r_multiple": round(net_r, 4),
            "cost_r": round(cost_r, 4),
            "pnl": round(net_pnl, 4),           # alias for backward compat
            "r_multiple": round(net_r, 4),       # alias for backward compat
            "pnl_pct": round((gross_pnl / entry_price) * 100, 2),
            "exit_reason": exit_reason,
        })

    return pd.DataFrame(trades)


def run_single_symbol(
    symbol: str,
    exchange: str,
    interval: str,
    start_date: str,
    end_date: str,
    orb_config=None,
    strategy_name: str = "orb",
    index_data: pd.DataFrame | None = None,
    costs=None,
    product: str = "MIS",
    slippage_pct: float = 0.0,
) -> SymbolResult:
    """
    Run backtest for a single symbol.

    Returns SymbolResult with trade statistics and trade log.
    """
    try:
        df = load_ohlcv_raw(symbol, exchange, interval, start_date, end_date)
    except (ValueError, Exception) as e:
        return SymbolResult(
            symbol=symbol, exchange=exchange,
            total_trades=0, winners=0, losers=0,
            win_rate=0.0, total_pnl=0.0,
            avg_r_multiple=0.0, profit_factor=0.0,
            long_trades=0, short_trades=0,
            long_pnl=0.0, short_pnl=0.0,
            exit_reasons={}, error=str(e),
        )

    strategy = get_strategy(strategy_name, orb_config=orb_config, index_data=index_data)
    signals = strategy.generate_signals_detailed(df)

    trade_log = _compute_trade_log(
        signals, df, symbol,
        costs=costs, product=product, slippage_pct=slippage_pct,
    )

    if trade_log.empty:
        return SymbolResult(
            symbol=symbol, exchange=exchange,
            total_trades=0, winners=0, losers=0,
            win_rate=0.0, total_pnl=0.0,
            avg_r_multiple=0.0, profit_factor=0.0,
            long_trades=0, short_trades=0,
            long_pnl=0.0, short_pnl=0.0,
            exit_reasons={}, error=None,
            trade_log=trade_log,
        )

    total = len(trade_log)
    winners = len(trade_log[trade_log["pnl"] > 0])
    losers = len(trade_log[trade_log["pnl"] < 0])
    win_rate = (winners / total) * 100 if total > 0 else 0

    total_pnl = trade_log["pnl"].sum()
    avg_r = trade_log["r_multiple"].mean()

    win_pnl = trade_log[trade_log["pnl"] > 0]["pnl"].sum()
    lose_pnl = abs(trade_log[trade_log["pnl"] < 0]["pnl"].sum())
    profit_factor = win_pnl / lose_pnl if lose_pnl > 0 else float("inf")

    longs = trade_log[trade_log["direction"] == "LONG"]
    shorts = trade_log[trade_log["direction"] == "SHORT"]

    exit_reasons = trade_log["exit_reason"].value_counts().to_dict()

    total_gross_pnl = trade_log["gross_pnl"].sum()
    total_costs = trade_log["cost"].sum()

    return SymbolResult(
        symbol=symbol, exchange=exchange,
        total_trades=total, winners=winners, losers=losers,
        win_rate=win_rate, total_pnl=total_pnl,
        avg_r_multiple=avg_r, profit_factor=profit_factor,
        long_trades=len(longs), short_trades=len(shorts),
        long_pnl=longs["pnl"].sum(), short_pnl=shorts["pnl"].sum(),
        exit_reasons=exit_reasons, error=None,
        total_gross_pnl=total_gross_pnl, total_costs=total_costs,
        trade_log=trade_log,
    )


def run_batch(config: dict) -> BatchResult:
    """
    Run backtest across all symbols in config.

    Args:
        config: Batch config dict from load_batch_config().

    Returns:
        BatchResult with per-symbol results and aggregation methods.
    """
    symbols = config["symbols"]
    interval = config["interval"]
    start_date = config["start_date"]
    end_date = config["end_date"]
    orb_config = config.get("orb_config")
    strategy_name = config.get("strategy", "orb")
    costs = config.get("costs")
    product = config.get("product", "MIS")
    slippage_pct = config.get("slippage_pct", 0.0)

    # Load index data once for all symbols (used by index direction filter)
    index_data = None
    index_cfg = config.get("index")
    if index_cfg:
        print(f"  Loading index data: {index_cfg['exchange']}:{index_cfg['symbol']}...",
              end=" ", flush=True)
        index_data = load_index_data(
            symbol=index_cfg["symbol"], exchange=index_cfg["exchange"],
            interval=interval, start_date=start_date, end_date=end_date,
        )
        if index_data is not None:
            print(f"{len(index_data)} bars loaded")
        else:
            print("NOT AVAILABLE (index filter will be skipped)")

    results = []
    total = len(symbols)

    for i, sym in enumerate(symbols, 1):
        symbol = sym["symbol"]
        exchange = sym["exchange"]
        print(f"  [{i}/{total}] {exchange}:{symbol}...", end=" ", flush=True)

        result = run_single_symbol(
            symbol=symbol, exchange=exchange,
            interval=interval, start_date=start_date, end_date=end_date,
            orb_config=orb_config, strategy_name=strategy_name,
            index_data=index_data,
            costs=costs, product=product, slippage_pct=slippage_pct,
        )

        if result.error:
            print(f"SKIP ({result.error})")
        elif result.total_trades == 0:
            print("0 trades")
        else:
            print(f"{result.total_trades} trades, "
                  f"WR {result.win_rate:.0f}%, "
                  f"PnL {result.total_pnl:,.0f}, "
                  f"PF {result.profit_factor:.2f}")

        results.append(result)

    return BatchResult(
        results=results,
        total_symbols=total,
        strategy_name=strategy_name,
        interval=interval,
        start_date=start_date,
        end_date=end_date,
    )


def print_batch_summary(batch: BatchResult) -> None:
    """Print consolidated batch results to console."""
    summary = batch.summary_df()
    active = summary[summary["error"] == ""]

    print(f"\n{'='*90}")
    print(f"  BATCH BACKTEST RESULTS - {batch.strategy_name.upper()} "
          f"({batch.start_date} to {batch.end_date})")
    print(f"{'='*90}")

    if active.empty:
        print("  No successful backtests.")
        return

    # Per-symbol table
    display_cols = ["symbol", "total_trades", "win_rate", "total_pnl",
                    "avg_r", "profit_factor", "long_trades", "short_trades"]
    print(active[display_cols].to_string(index=False))

    # Aggregated stats
    total_trades = active["total_trades"].sum()
    total_pnl = active["total_pnl"].sum()
    avg_wr = active.loc[active["total_trades"] > 0, "win_rate"].mean()
    avg_pf = active.loc[
        (active["total_trades"] > 0) & (active["profit_factor"] < float("inf")),
        "profit_factor"
    ].mean()

    print(f"\n{'='*90}")
    print(f"  PORTFOLIO SUMMARY")
    print(f"{'='*90}")
    print(f"  Symbols tested:   {len(active)}/{batch.total_symbols}")
    print(f"  Total trades:     {int(total_trades)}")
    print(f"  Total P&L (net):  {total_pnl:,.2f}")

    # Show gross/costs breakdown if available
    if "gross_pnl" in active.columns:
        total_gross = active["gross_pnl"].sum()
        total_costs = active["costs"].sum() if "costs" in active.columns else 0
        if total_costs > 0:
            print(f"  Total P&L (gross): {total_gross:,.2f}")
            print(f"  Total Costs:      {total_costs:,.2f}")

    print(f"  Avg Win Rate:     {avg_wr:.1f}%")
    print(f"  Avg Profit Factor: {avg_pf:.2f}")

    # Top/bottom performers
    if len(active) >= 3:
        top = active.nlargest(3, "total_pnl")
        bottom = active.nsmallest(3, "total_pnl")
        print(f"\n  Top 3:    {', '.join(f'{r.symbol}({r.total_pnl:+,.0f})' for _, r in top.iterrows())}")
        print(f"  Bottom 3: {', '.join(f'{r.symbol}({r.total_pnl:+,.0f})' for _, r in bottom.iterrows())}")

    print(f"{'='*90}\n")


def _build_output_dir(base_dir: str, strategy_name: str) -> Path:
    """
    Build timestamped, strategy-specific output directory.

    Format: backtest/results/{strategy}/{YYYYMMDD_HHMMSS}/
    Example: backtest/results/orb/20260316_143022/

    Also maintains a 'latest' symlink for convenience.
    """
    from datetime import datetime

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(base_dir) / strategy_name / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    # Update 'latest' symlink
    latest = Path(base_dir) / strategy_name / "latest"
    if latest.is_symlink():
        latest.unlink()
    try:
        latest.symlink_to(timestamp)
    except OSError:
        pass  # symlink not supported on some filesystems

    return run_dir


def _run_walk_forward(config: dict, args) -> None:
    """Run walk-forward analysis with train/test split."""
    from backtest.walkforward import split_temporal, detect_overfitting
    from backtest.evaluate import compute_strategy_health

    strategy_name = config["strategy"]
    start_date = config["start_date"]
    end_date = config["end_date"]

    train, test = split_temporal(start_date, end_date, args.train_pct)
    print(f"Walk-Forward Analysis - {strategy_name.upper()}")
    print(f"  Train: {train[0]} to {train[1]}")
    print(f"  Test:  {test[0]} to {test[1]}")

    # Run in-sample
    is_config = {**config, "start_date": train[0], "end_date": train[1]}
    print(f"\n--- IN-SAMPLE ({train[0]} to {train[1]}) ---")
    is_batch = run_batch(is_config)
    is_trades = is_batch.combined_trade_log()
    is_health = compute_strategy_health(is_trades) if not is_trades.empty else {"expectancy_r": 0.0}

    # Run out-of-sample
    oos_config = {**config, "start_date": test[0], "end_date": test[1]}
    print(f"\n--- OUT-OF-SAMPLE ({test[0]} to {test[1]}) ---")
    oos_batch = run_batch(oos_config)
    oos_trades = oos_batch.combined_trade_log()
    oos_health = compute_strategy_health(oos_trades) if not oos_trades.empty else {"expectancy_r": 0.0}

    # Compare
    result = detect_overfitting(
        is_expectancy=is_health["expectancy_r"],
        oos_expectancy=oos_health["expectancy_r"],
    )

    w = 70
    print(f"\n{'='*w}")
    print("  WALK-FORWARD RESULTS")
    print(f"{'='*w}")
    print(f"  In-Sample Expectancy:     {is_health['expectancy_r']:+.3f} R ({len(is_trades)} trades)")
    print(f"  Out-of-Sample Expectancy: {oos_health['expectancy_r']:+.3f} R ({len(oos_trades)} trades)")
    print(f"  OOS/IS Ratio:             {result['ratio']}")
    print(f"  {result['message']}")
    print(f"{'='*w}")

    # Save results
    output_dir = _build_output_dir(args.output_dir, strategy_name)
    wf_report = (
        f"Walk-Forward Analysis\n"
        f"Train: {train[0]} to {train[1]}\n"
        f"Test:  {test[0]} to {test[1]}\n\n"
        f"IS Expectancy:  {is_health['expectancy_r']:+.3f} R ({len(is_trades)} trades)\n"
        f"OOS Expectancy: {oos_health['expectancy_r']:+.3f} R ({len(oos_trades)} trades)\n"
        f"OOS/IS Ratio:   {result['ratio']}\n"
        f"{result['message']}\n"
    )
    (output_dir / "walkforward.txt").write_text(wf_report)
    print(f"\nResults: {output_dir}")


def main():
    parser = argparse.ArgumentParser(description="Batch Backtest Runner")
    parser.add_argument("--config", default="backtest/strategies/orb/config.yaml",
                        help="Batch config YAML path")
    parser.add_argument("--output-dir", default="backtest/results",
                        help="Base output directory for results")
    parser.add_argument("--walk-forward", action="store_true",
                        help="Run walk-forward analysis (train/test split)")
    parser.add_argument("--train-pct", type=float, default=0.7,
                        help="Train fraction for walk-forward (default: 0.7)")
    args = parser.parse_args()

    from backtest.config import load_batch_config

    config = load_batch_config(args.config)
    strategy_name = config["strategy"]

    if args.walk_forward:
        _run_walk_forward(config, args)
        return

    print(f"Running {strategy_name.upper()} backtest on "
          f"{len(config['symbols'])} symbols...")

    batch = run_batch(config)
    print_batch_summary(batch)

    # Save results to timestamped, strategy-specific directory
    output_dir = _build_output_dir(args.output_dir, strategy_name)

    summary = batch.summary_df()
    summary_path = output_dir / "summary.csv"
    summary.to_csv(summary_path, index=False)
    print(f"Summary saved: {summary_path}")

    trades = batch.combined_trade_log()
    if not trades.empty:
        trades_path = output_dir / "trades.csv"
        trades.to_csv(trades_path, index=False)
        print(f"Trade log saved: {trades_path}")

    # Generate tearsheet if quantstats available
    try:
        from backtest.report import generate_batch_tearsheet
        tearsheet_path = output_dir / "tearsheet.html"
        generate_batch_tearsheet(
            batch, output_path=str(tearsheet_path),
        )
        print(f"Tearsheet saved: {tearsheet_path}")
    except (ImportError, Exception) as e:
        print(f"Tearsheet skipped: {e}")

    # Generate evaluation report
    try:
        from backtest.evaluate import evaluate_batch
        eval_path = evaluate_batch(batch, output_dir)
        if eval_path:
            print(f"Evaluation saved: {eval_path}")
    except Exception as e:
        print(f"Evaluation skipped: {e}")

    print(f"\nResults: {output_dir}")


if __name__ == "__main__":
    main()
