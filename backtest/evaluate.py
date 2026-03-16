#!/usr/bin/env python3
"""
Backtest evaluation system.

Computes strategy health metrics, risk profile, and edge analysis from
batch backtest results. Generates actionable insights and a formatted report.

Usage:
    # Standalone (from saved CSV files)
    uv run python backtest/evaluate.py backtest/results/orb/latest

    # Integrated (called automatically by runner.py after each batch run)
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Layer 1: Metrics computation (pure functions -> dicts)
# ---------------------------------------------------------------------------

def compute_strategy_health(trades: pd.DataFrame) -> dict:
    """Core strategy quality metrics from trade log.

    Uses r_multiple (net of costs) as the primary metric.
    If gross_r_multiple exists, also computes gross expectancy for comparison.
    """
    empty_result = {k: 0.0 for k in [
        "expectancy_r", "profit_factor", "payoff_ratio", "edge_ratio",
        "win_rate", "avg_winner_r", "avg_loser_r", "total_trades",
        "gross_expectancy_r", "avg_cost_r",
    ]}
    if trades.empty:
        return empty_result

    winners = trades[trades["r_multiple"] > 0]
    losers = trades[trades["r_multiple"] <= 0]

    win_rate = len(winners) / len(trades) if len(trades) > 0 else 0
    avg_winner_r = winners["r_multiple"].mean() if len(winners) > 0 else 0
    avg_loser_r = losers["r_multiple"].mean() if len(losers) > 0 else 0

    expectancy_r = trades["r_multiple"].mean()

    gross_win = winners["r_multiple"].sum() if len(winners) > 0 else 0
    gross_loss = abs(losers["r_multiple"].sum()) if len(losers) > 0 else 0
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")

    payoff_ratio = abs(avg_winner_r / avg_loser_r) if avg_loser_r != 0 else float("inf")

    # Edge ratio: (avg_winner * win_rate) / (avg_loser * loss_rate)
    loss_rate = 1 - win_rate
    edge_ratio = (
        (avg_winner_r * win_rate) / (abs(avg_loser_r) * loss_rate)
        if loss_rate > 0 and avg_loser_r != 0 else float("inf")
    )

    # Gross expectancy (if cost columns exist)
    gross_expectancy_r = expectancy_r  # same if no cost columns
    avg_cost_r = 0.0
    if "gross_r_multiple" in trades.columns:
        gross_expectancy_r = round(trades["gross_r_multiple"].mean(), 3)
    if "cost_r" in trades.columns:
        avg_cost_r = round(trades["cost_r"].mean(), 3)

    return {
        "expectancy_r": round(expectancy_r, 3),
        "gross_expectancy_r": gross_expectancy_r,
        "avg_cost_r": avg_cost_r,
        "profit_factor": round(profit_factor, 2),
        "payoff_ratio": round(payoff_ratio, 2),
        "edge_ratio": round(edge_ratio, 2),
        "win_rate": round(win_rate * 100, 1),
        "avg_winner_r": round(avg_winner_r, 3),
        "avg_loser_r": round(avg_loser_r, 3),
        "total_trades": len(trades),
    }


def compute_risk_profile(trades: pd.DataFrame) -> dict:
    """Drawdown, streaks, and tail risk metrics."""
    if trades.empty:
        return {k: 0.0 for k in [
            "max_drawdown_r", "worst_streak", "best_streak",
            "largest_win_r", "largest_loss_r", "r_sharpe",
            "tail_ratio", "pct_positive_days",
        ]}

    r = trades["r_multiple"]
    cumulative = r.cumsum()
    running_max = cumulative.cummax()
    drawdown = cumulative - running_max
    max_drawdown_r = round(drawdown.min(), 2)

    # Streaks
    is_win = (r > 0).astype(int)
    streaks = is_win.groupby((is_win != is_win.shift()).cumsum())
    win_streaks = [len(g) for _, g in streaks if g.iloc[0] == 1]
    lose_streaks = [len(g) for _, g in streaks if g.iloc[0] == 0]
    best_streak = max(win_streaks) if win_streaks else 0
    worst_streak = max(lose_streaks) if lose_streaks else 0

    largest_win_r = round(r.max(), 2)
    largest_loss_r = round(r.min(), 2)

    # R-Sharpe: mean(R) / std(R)
    r_std = r.std()
    r_sharpe = round(r.mean() / r_std, 2) if r_std > 0 else 0.0

    # Tail ratio: 95th percentile / abs(5th percentile)
    p95 = np.percentile(r, 95)
    p5 = abs(np.percentile(r, 5))
    tail_ratio = round(p95 / p5, 2) if p5 > 0 else float("inf")

    # Percentage of positive trading days
    if "exit_time" in trades.columns:
        trades_copy = trades.copy()
        trades_copy["date"] = pd.to_datetime(trades_copy["exit_time"]).dt.date
        daily_r = trades_copy.groupby("date")["r_multiple"].sum()
        pct_positive_days = round(
            (daily_r > 0).sum() / len(daily_r) * 100, 1
        ) if len(daily_r) > 0 else 0.0
    else:
        pct_positive_days = 0.0

    return {
        "max_drawdown_r": max_drawdown_r,
        "worst_streak": worst_streak,
        "best_streak": best_streak,
        "largest_win_r": largest_win_r,
        "largest_loss_r": largest_loss_r,
        "r_sharpe": r_sharpe,
        "tail_ratio": tail_ratio,
        "pct_positive_days": pct_positive_days,
    }


def compute_edge_analysis(
    trades: pd.DataFrame, summary: pd.DataFrame | None = None
) -> dict:
    """Direction, exit-type, symbol-tier, and day-of-week breakdowns."""
    if trades.empty:
        return {
            "long_avg_r": 0.0, "short_avg_r": 0.0,
            "long_count": 0, "short_count": 0,
            "exit_breakdown": {}, "symbol_tiers": {},
            "dow_r": {},
        }

    # Direction stats
    longs = trades[trades["direction"] == "LONG"]
    shorts = trades[trades["direction"] == "SHORT"]
    long_avg_r = round(longs["r_multiple"].mean(), 3) if len(longs) > 0 else 0.0
    short_avg_r = round(shorts["r_multiple"].mean(), 3) if len(shorts) > 0 else 0.0

    # Exit type breakdown
    exit_breakdown = {}
    for reason in ["SL", "TP", "TIME"]:
        subset = trades[trades["exit_reason"] == reason]
        exit_breakdown[reason] = {
            "count": len(subset),
            "pct": round(len(subset) / len(trades) * 100, 1) if len(trades) > 0 else 0,
            "avg_r": round(subset["r_multiple"].mean(), 3) if len(subset) > 0 else 0.0,
        }

    # Symbol tiers based on summary or trades
    symbol_tiers = _compute_symbol_tiers(trades, summary)

    # Day-of-week R
    dow_r = {}
    if "entry_time" in trades.columns:
        trades_copy = trades.copy()
        trades_copy["dow"] = pd.to_datetime(trades_copy["entry_time"]).dt.day_name()
        dow_map = trades_copy.groupby("dow")["r_multiple"].mean()
        for day in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]:
            if day in dow_map.index:
                dow_r[day] = round(dow_map[day], 3)

    return {
        "long_avg_r": long_avg_r,
        "short_avg_r": short_avg_r,
        "long_count": len(longs),
        "short_count": len(shorts),
        "exit_breakdown": exit_breakdown,
        "symbol_tiers": symbol_tiers,
        "dow_r": dow_r,
    }


def _compute_symbol_tiers(
    trades: pd.DataFrame, summary: pd.DataFrame | None
) -> dict[str, list[str]]:
    """Classify symbols into green/yellow/red tiers by avg R."""
    if "symbol" not in trades.columns:
        return {"green": [], "yellow": [], "red": []}

    sym_r = trades.groupby("symbol")["r_multiple"].mean()

    green = sorted(sym_r[sym_r >= 0.15].index.tolist())
    yellow = sorted(sym_r[(sym_r >= -0.1) & (sym_r < 0.15)].index.tolist())
    red = sorted(sym_r[sym_r < -0.1].index.tolist())

    return {"green": green, "yellow": yellow, "red": red}


# ---------------------------------------------------------------------------
# Layer 2: Insight generation (rules -> tagged strings)
# ---------------------------------------------------------------------------

def generate_insights(health: dict, risk: dict, edge: dict) -> list[str]:
    """Generate tagged insight strings from computed metrics."""
    insights = []

    # Strategy health insights (net of costs)
    exp = health["expectancy_r"]
    gross_exp = health.get("gross_expectancy_r", exp)
    avg_cost_r = health.get("avg_cost_r", 0.0)

    if exp > 0.10:
        insights.append(f"[STRENGTH] Net expectancy: {exp}R per trade (after costs)")
    elif exp > 0:
        insights.append(f"[WARNING] Marginal net expectancy: {exp}R per trade - thin edge after costs")
    else:
        insights.append(f"[WARNING] Negative net expectancy: {exp}R per trade - strategy loses money after costs")

    # Cost erosion warning
    if avg_cost_r > 0 and gross_exp > 0 and exp <= 0:
        insights.append(
            f"[WARNING] Gross-positive ({gross_exp}R) but net-negative ({exp}R) - "
            f"costs of {avg_cost_r}R/trade erode the entire edge"
        )
    elif avg_cost_r > 0 and gross_exp > 0:
        erosion_pct = round((avg_cost_r / gross_exp) * 100, 0)
        if erosion_pct >= 50:
            insights.append(
                f"[WARNING] Costs consume {erosion_pct:.0f}% of gross edge "
                f"({avg_cost_r}R cost vs {gross_exp}R gross)"
            )

    pf = health["profit_factor"]
    if pf >= 1.5:
        insights.append(f"[STRENGTH] Profit factor {pf} - strong risk-adjusted returns")
    elif pf < 1.0:
        insights.append(f"[WARNING] Profit factor {pf} - net losses exceed net wins")

    wr = health["win_rate"]
    if wr >= 55:
        insights.append(f"[STRENGTH] Win rate {wr}% - above-average hit rate")
    elif wr < 40:
        insights.append(f"[WARNING] Win rate {wr}% - needs higher payoff ratio to compensate")

    # Risk profile insights
    ws = risk["worst_streak"]
    if ws >= 8:
        insights.append(
            f"[WARNING] Worst losing streak: {ws} trades - "
            "size positions to survive 2x this streak"
        )
    elif ws >= 5:
        insights.append(f"[WARNING] Worst losing streak: {ws} trades - psychologically challenging")

    dd = risk["max_drawdown_r"]
    if dd <= -10:
        insights.append(f"[WARNING] Max drawdown {dd}R - significant capital at risk")

    tail = risk["tail_ratio"]
    if tail >= 1.2:
        insights.append(f"[STRENGTH] Tail ratio {tail} - winners are fatter than losers")
    elif tail < 0.8:
        insights.append(f"[WARNING] Tail ratio {tail} - loser tails are fatter than winners")

    sharpe = risk["r_sharpe"]
    if sharpe >= 0.15:
        insights.append(f"[STRENGTH] R-Sharpe {sharpe} - consistent edge")

    ppd = risk["pct_positive_days"]
    if ppd >= 55:
        insights.append(f"[STRENGTH] {ppd}% positive days - consistent daily returns")

    # Edge analysis insights
    long_r = edge["long_avg_r"]
    short_r = edge["short_avg_r"]
    if long_r - short_r >= 0.15:
        insights.append(
            f"[ACTION] Long avg {long_r}R vs Short avg {short_r}R - "
            "consider reducing short exposure or going long-only"
        )
    elif short_r - long_r >= 0.15:
        insights.append(
            f"[ACTION] Short avg {short_r}R vs Long avg {long_r}R - "
            "consider reducing long exposure"
        )

    # Exit type analysis
    eb = edge.get("exit_breakdown", {})
    time_exits = eb.get("TIME", {})
    if time_exits.get("count", 0) > 0 and time_exits.get("avg_r", 0) < -0.1:
        insights.append(
            f"[ACTION] TIME exits avg {time_exits['avg_r']}R ({time_exits['count']} trades) - "
            "investigate tighter targets or earlier exit rules"
        )
    if time_exits.get("pct", 0) > 40:
        insights.append(
            f"[WARNING] {time_exits['pct']}% of trades exit on TIME - "
            "strategy may not be reaching TP often enough"
        )

    # Symbol tier insights
    tiers = edge.get("symbol_tiers", {})
    red_symbols = tiers.get("red", [])
    if red_symbols:
        insights.append(
            f"[ACTION] Red-tier symbols (avg R < -0.1): {', '.join(red_symbols)} - "
            "consider removing from universe"
        )
    green_symbols = tiers.get("green", [])
    if green_symbols:
        insights.append(
            f"[STRENGTH] Green-tier symbols (avg R >= 0.15): {', '.join(green_symbols)}"
        )

    # Day-of-week insights
    dow = edge.get("dow_r", {})
    if dow:
        best_day = max(dow, key=dow.get)
        worst_day = min(dow, key=dow.get)
        if dow[worst_day] < -0.15:
            insights.append(
                f"[ACTION] {worst_day} avg {dow[worst_day]}R - "
                "consider skipping or reducing size on this day"
            )
        if dow[best_day] > 0.2:
            insights.append(f"[STRENGTH] {best_day} avg {dow[best_day]}R - strongest day")

    return insights


# ---------------------------------------------------------------------------
# Layer 3: Report formatting (-> terminal-friendly text)
# ---------------------------------------------------------------------------

def format_report(
    health: dict, risk: dict, edge: dict, insights: list[str],
    header: str = "",
) -> str:
    """Format all metrics and insights into a readable text report."""
    lines = []
    w = 90

    if header:
        lines.append("=" * w)
        lines.append(f"  {header}")
        lines.append("=" * w)
    else:
        lines.append("=" * w)
        lines.append("  BACKTEST EVALUATION REPORT")
        lines.append("=" * w)

    # Strategy Health
    lines.append("")
    lines.append(f"  STRATEGY HEALTH ({health['total_trades']} trades)")
    lines.append("-" * w)
    lines.append(f"  Net Expectancy:  {health['expectancy_r']:+.3f} R/trade")
    lines.append(f"  Win Rate:        {health['win_rate']}%")
    lines.append(f"  Profit Factor:   {health['profit_factor']}")
    lines.append(f"  Payoff Ratio:    {health['payoff_ratio']}")
    lines.append(f"  Edge Ratio:      {health['edge_ratio']}")
    lines.append(f"  Avg Winner:      {health['avg_winner_r']:+.3f} R")
    lines.append(f"  Avg Loser:       {health['avg_loser_r']:+.3f} R")

    # Cost impact section (if costs were applied)
    gross_exp = health.get("gross_expectancy_r", health["expectancy_r"])
    avg_cost_r = health.get("avg_cost_r", 0.0)
    if avg_cost_r > 0:
        lines.append("")
        lines.append("  COST IMPACT")
        lines.append("-" * w)
        lines.append(f"  Gross Expectancy:  {gross_exp:+.3f} R/trade")
        lines.append(f"  Avg Cost/Trade:    -{avg_cost_r:.3f} R")
        lines.append(f"  Net Expectancy:    {health['expectancy_r']:+.3f} R/trade")
        if gross_exp > 0:
            erosion = round((avg_cost_r / gross_exp) * 100, 1)
            lines.append(f"  Cost Erosion:      {erosion}% of gross edge")

    # Risk Profile
    lines.append("")
    lines.append("  RISK PROFILE")
    lines.append("-" * w)
    lines.append(f"  Max Drawdown:    {risk['max_drawdown_r']} R")
    lines.append(f"  Worst Streak:    {risk['worst_streak']} consecutive losses")
    lines.append(f"  Best Streak:     {risk['best_streak']} consecutive wins")
    lines.append(f"  Largest Win:     {risk['largest_win_r']:+} R")
    lines.append(f"  Largest Loss:    {risk['largest_loss_r']:+} R")
    lines.append(f"  R-Sharpe:        {risk['r_sharpe']}")
    lines.append(f"  Tail Ratio:      {risk['tail_ratio']}")
    lines.append(f"  Positive Days:   {risk['pct_positive_days']}%")

    # Edge Analysis
    lines.append("")
    lines.append("  EDGE ANALYSIS")
    lines.append("-" * w)
    lines.append(
        f"  Long:  {edge['long_count']} trades, avg {edge['long_avg_r']:+.3f} R"
    )
    lines.append(
        f"  Short: {edge['short_count']} trades, avg {edge['short_avg_r']:+.3f} R"
    )

    # Exit breakdown
    eb = edge.get("exit_breakdown", {})
    if eb:
        lines.append("")
        lines.append("  Exit Breakdown:")
        for reason in ["TP", "SL", "TIME"]:
            if reason in eb:
                e = eb[reason]
                lines.append(
                    f"    {reason:4s}  {e['count']:3d} trades ({e['pct']:4.1f}%)  "
                    f"avg {e['avg_r']:+.3f} R"
                )

    # Symbol tiers
    tiers = edge.get("symbol_tiers", {})
    if any(tiers.values()):
        lines.append("")
        lines.append("  Symbol Tiers:")
        if tiers.get("green"):
            lines.append(f"    Green (R >= +0.15):  {', '.join(tiers['green'])}")
        if tiers.get("yellow"):
            lines.append(f"    Yellow (-0.1 to +0.15): {', '.join(tiers['yellow'])}")
        if tiers.get("red"):
            lines.append(f"    Red (R < -0.1):     {', '.join(tiers['red'])}")

    # Day-of-week R
    dow = edge.get("dow_r", {})
    if dow:
        lines.append("")
        lines.append("  Day-of-Week Avg R:")
        for day in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]:
            if day in dow:
                bar = "+" * max(0, int(dow[day] * 10)) if dow[day] > 0 else "-" * max(0, int(abs(dow[day]) * 10))
                lines.append(f"    {day:9s}  {dow[day]:+.3f} R  {bar}")

    # Insights
    if insights:
        lines.append("")
        lines.append("  INSIGHTS")
        lines.append("-" * w)
        for insight in insights:
            lines.append(f"  {insight}")

    lines.append("")
    lines.append("=" * w)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def evaluate_batch(batch, output_dir: Path | str) -> str:
    """
    Evaluate a BatchResult and save evaluation.txt.

    Called from runner.py after tearsheet generation.

    Args:
        batch: BatchResult from runner.run_batch().
        output_dir: Directory to save evaluation.txt.

    Returns:
        Path to the generated evaluation file.
    """
    trades = batch.combined_trade_log()
    summary = batch.summary_df()

    if trades.empty:
        return ""

    health = compute_strategy_health(trades)
    risk = compute_risk_profile(trades)
    edge = compute_edge_analysis(trades, summary)
    insights = generate_insights(health, risk, edge)

    header = (
        f"BACKTEST EVALUATION - {batch.strategy_name.upper()} "
        f"({batch.start_date} to {batch.end_date})"
    )
    report = format_report(health, risk, edge, insights, header=header)

    print(report)

    output_path = Path(output_dir) / "evaluation.txt"
    output_path.write_text(report)
    return str(output_path)


def evaluate_from_csv(results_dir: str) -> str:
    """
    Standalone evaluation from saved CSV files.

    Args:
        results_dir: Path to results directory containing trades.csv and summary.csv.

    Returns:
        Formatted report string.
    """
    results_path = Path(results_dir).resolve()

    trades_path = results_path / "trades.csv"
    if not trades_path.exists():
        print(f"No trades.csv found in {results_path}")
        return ""

    trades = pd.read_csv(trades_path)
    if trades.empty:
        print("trades.csv is empty")
        return ""

    summary = None
    summary_path = results_path / "summary.csv"
    if summary_path.exists():
        summary = pd.read_csv(summary_path)

    health = compute_strategy_health(trades)
    risk = compute_risk_profile(trades)
    edge = compute_edge_analysis(trades, summary)
    insights = generate_insights(health, risk, edge)

    header = f"BACKTEST EVALUATION - {results_path.name}"
    report = format_report(health, risk, edge, insights, header=header)

    print(report)

    output_path = results_path / "evaluation.txt"
    output_path.write_text(report)
    print(f"\nEvaluation saved: {output_path}")

    return report


def main():
    parser = argparse.ArgumentParser(description="Evaluate backtest results")
    parser.add_argument(
        "results_dir",
        help="Path to results directory (e.g., backtest/results/orb/latest)",
    )
    args = parser.parse_args()
    evaluate_from_csv(args.results_dir)


if __name__ == "__main__":
    main()
