"""
Day-by-day strategy performance metrics.

Computes professional trade evaluation metrics (win rate, profit factor,
expectancy, Sharpe/Sortino/Calmar, max drawdown, Ulcer Index, recovery
factor, streaks) from the closed-trade ledger in
`database/strategy_book_db.py`'s `StrategyClosedTrade`.

Metrics are computed on a rupee-denominated daily P&L series, not a
%-return series: strategies here have no isolated allocated capital to
normalize against. See docs/strategy-daily-performance.md ("design
decisions", point 3) for why this matches how retail trading journals
(Tradervue, Edgewonk) report, and is not a shortcut.

Every calendar day in the requested range is represented, including
zero-trade days as a 0 P&L day - omitting them silently inflates Sharpe (an
intraday-Sharpe annualization pitfall; see docs/strategy-daily-performance.md
point 4). This is pure computation with no DB access, so it is fully
unit-testable with synthetic, known-answer data.
"""

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252


def _daily_pnl_series(closed_trades: list[dict], start_date: str, end_date: str) -> pd.Series:
    """One realized-PnL total per calendar day across [start_date, end_date],
    zero-filled for days with no closed trade."""
    idx = pd.date_range(start=start_date, end=end_date, freq="D")
    if not closed_trades:
        return pd.Series(0.0, index=idx)
    df = pd.DataFrame(closed_trades)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    daily = df.groupby("trade_date")["realized_pnl"].sum()
    return daily.reindex(idx, fill_value=0.0)


def _max_drawdown_and_ulcer(equity: pd.Series) -> tuple[float, float]:
    if len(equity) == 0:
        return 0.0, 0.0
    running_max = equity.cummax()
    drawdown = equity - running_max
    max_dd = float(drawdown.min())
    ulcer = float(np.sqrt((drawdown**2).mean()))
    return max_dd, ulcer


def _consecutive_streaks(daily_pnl: pd.Series) -> tuple[int, int]:
    """Longest consecutive-win and consecutive-loss streak, counted over
    trading days with a nonzero result - a flat (no-trade) day neither breaks
    nor extends a streak."""
    best_win = best_loss = cur_win = cur_loss = 0
    for value in daily_pnl:
        if value > 0:
            cur_win += 1
            cur_loss = 0
        elif value < 0:
            cur_loss += 1
            cur_win = 0
        else:
            continue
        best_win = max(best_win, cur_win)
        best_loss = max(best_loss, cur_loss)
    return best_win, best_loss


def _finite(value: float) -> float | None:
    """inf is not valid JSON - a strategy with zero losing trades reports
    profit_factor as null (undefined, not infinite) rather than a value
    jsonify() would choke on."""
    if value in (float("inf"), float("-inf")) or (isinstance(value, float) and np.isnan(value)):
        return None
    return round(value, 2)


def resolve_period(
    period: str, closed_trades: list[dict], today: date | None = None
) -> tuple[str, str]:
    """Maps a period keyword ("7d"/"30d"/"90d"/"ytd"/"all") to an ISO
    [start_date, end_date] range.

    "all" is bounded by the earliest closed trade, not a fixed lookback -
    forward-only tracking (see docs/strategy-daily-performance.md point 2)
    means there is nothing to show before this feature's own deploy date.
    """
    today = today or datetime.now().date()
    if period == "7d":
        start = today - timedelta(days=6)
    elif period == "90d":
        start = today - timedelta(days=89)
    elif period == "ytd":
        start = today.replace(month=1, day=1)
    elif period == "all":
        if closed_trades:
            start = min(
                datetime.strptime(t["trade_date"], "%Y-%m-%d").date() for t in closed_trades
            )
        else:
            start = today
    else:  # default: 30d
        start = today - timedelta(days=29)
    return start.isoformat(), today.isoformat()


def compute_metrics(closed_trades: list[dict], start_date: str, end_date: str) -> dict:
    """Full metrics table for one strategy/mode (or the pooled portfolio)
    over [start_date, end_date]."""
    daily_pnl = _daily_pnl_series(closed_trades, start_date, end_date)
    equity = daily_pnl.cumsum()

    trades_count = len(closed_trades)
    wins = [t["realized_pnl"] for t in closed_trades if t["realized_pnl"] > 0]
    losses = [t["realized_pnl"] for t in closed_trades if t["realized_pnl"] < 0]

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    win_rate = (len(wins) / trades_count) if trades_count else 0.0
    avg_win = (gross_profit / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0
    profit_factor = (
        (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    )
    expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss
    payoff_ratio = (
        (avg_win / avg_loss) if avg_loss > 0 else (float("inf") if avg_win > 0 else 0.0)
    )

    daily_std = float(daily_pnl.std(ddof=0))
    daily_mean = float(daily_pnl.mean()) if len(daily_pnl) else 0.0
    sharpe = (daily_mean / daily_std * np.sqrt(TRADING_DAYS_PER_YEAR)) if daily_std > 0 else 0.0

    downside = daily_pnl[daily_pnl < 0]
    downside_std = float(downside.std(ddof=0)) if len(downside) > 1 else 0.0
    sortino = (
        (daily_mean / downside_std * np.sqrt(TRADING_DAYS_PER_YEAR)) if downside_std > 0 else 0.0
    )

    max_dd, ulcer = _max_drawdown_and_ulcer(equity)
    net_profit = float(equity.iloc[-1]) if len(equity) else 0.0
    recovery_factor = (
        (net_profit / abs(max_dd)) if max_dd < 0 else (float("inf") if net_profit > 0 else 0.0)
    )

    annualized_pnl = daily_mean * TRADING_DAYS_PER_YEAR
    calmar = (
        (annualized_pnl / abs(max_dd)) if max_dd < 0 else (float("inf") if annualized_pnl > 0 else 0.0)
    )

    best_win_streak, best_loss_streak = _consecutive_streaks(daily_pnl)

    return {
        "start_date": start_date,
        "end_date": end_date,
        "trades_count": trades_count,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": round(win_rate * 100, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "net_profit": round(net_profit, 2),
        "profit_factor": _finite(profit_factor),
        "expectancy": round(expectancy, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "payoff_ratio": _finite(payoff_ratio),
        "sharpe_ratio": round(sharpe, 2),
        "sortino_ratio": round(sortino, 2),
        "calmar_ratio": _finite(calmar),
        "max_drawdown": round(max_dd, 2),
        "ulcer_index": round(ulcer, 2),
        "recovery_factor": _finite(recovery_factor),
        "best_win_streak": best_win_streak,
        "best_loss_streak": best_loss_streak,
        "best_day": round(float(daily_pnl.max()), 2) if len(daily_pnl) else 0.0,
        "worst_day": round(float(daily_pnl.min()), 2) if len(daily_pnl) else 0.0,
        "trading_days": int((daily_pnl != 0).sum()),
        "calendar_days": len(daily_pnl),
        "daily_pnl": [
            {"date": d.strftime("%Y-%m-%d"), "pnl": round(float(v), 2)} for d, v in daily_pnl.items()
        ],
        "equity_curve": [
            {"date": d.strftime("%Y-%m-%d"), "value": round(float(v), 2)} for d, v in equity.items()
        ],
    }
