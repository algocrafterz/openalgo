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

# StrategyClosedTrade (the ledger this page reads) was added 2026-09-25 and
# only writes a row on every close from that date forward. A handful of
# trades from 2026-09-08 to 2026-09-24 were later hand-reconstructed into it
# (see docs/strategy-pnl-fork-modification.md), but only for the specific
# symbol/strategy legs that happened to get stuck open by a separate bug -
# every other trade that closed normally in that window has no row at all.
# Including that window would silently mix a complete ledger (09-25 onward)
# with a non-random partial one (a few reconstructed trades, mostly
# force-closes, standing in for a much larger population that isn't there),
# skewing win rate, trade counts and the equity curve. Clamped out here
# rather than deleted from the table - the reconstructed rows are individually
# accurate and worth keeping for anyone who deliberately wants that window,
# just never as a default or in an aggregate/comparison view.
LEDGER_RELIABLE_SINCE = date(2026, 9, 25)

# Rolling windows, in calendar days including today.
FIXED_WINDOW_DAYS = {"7d": 7, "15d": 15, "30d": 30, "90d": 90}


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


def last_session_date(closed_trades: list[dict], today: date) -> date | None:
    """Most recent trade_date with a closed trade, on or before today and inside
    the reliable ledger window; None when there is none."""
    floor, ceiling = LEDGER_RELIABLE_SINCE.isoformat(), today.isoformat()
    dates = [t["trade_date"] for t in closed_trades if floor <= t["trade_date"] <= ceiling]
    return date.fromisoformat(max(dates)) if dates else None


def _last_session(closed_trades: list[dict], today: date) -> date:
    """Most recent trade_date with a closed trade, on or before today and
    inside the reliable ledger window; today when there is none."""
    floor, ceiling = LEDGER_RELIABLE_SINCE.isoformat(), today.isoformat()
    dates = [t["trade_date"] for t in closed_trades if floor <= t["trade_date"] <= ceiling]
    return date.fromisoformat(max(dates)) if dates else today


def _parse_iso_date(value: str | None, name: str) -> date:
    if not value:
        raise ValueError(f"Choose a {name} date for the custom range.")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"The {name} date must be YYYY-MM-DD.") from exc


def _custom_range(start: str | None, end: str | None, today: date) -> tuple[date, date]:
    start_d = _parse_iso_date(start, "start")
    end_d = _parse_iso_date(end, "end")
    if start_d > end_d:
        raise ValueError("The start date must be on or before the end date.")
    if start_d > today:
        raise ValueError("The custom range starts in the future.")
    return start_d, min(end_d, today)


def resolve_period(
    period: str,
    closed_trades: list[dict],
    today: date | None = None,
    start: str | None = None,
    end: str | None = None,
) -> tuple[str, str]:
    """Maps a period keyword ("1d"/"7d"/"15d"/"30d"/"90d"/"ytd"/"all"/"custom")
    to an ISO [start_date, end_date] range.

    "1d" is the last traded session, not the calendar day: on a weekend or a
    holiday it is the most recent day with a closed trade.

    "all" is bounded by the earliest closed trade, not a fixed lookback -
    forward-only tracking (see docs/strategy-daily-performance.md point 2)
    means there is nothing to show before this feature's own deploy date.

    "custom" takes `start`/`end` (YYYY-MM-DD) and raises ValueError when they
    are missing, malformed, reversed or wholly in the future.
    """
    today = today or datetime.now().date()
    end_d = today
    if period == "1d":
        start_d = end_d = _last_session(closed_trades, today)
    elif period == "custom":
        start_d, end_d = _custom_range(start, end, today)
    elif period == "ytd":
        start_d = today.replace(month=1, day=1)
    elif period == "all":
        if closed_trades:
            start_d = min(date.fromisoformat(t["trade_date"]) for t in closed_trades)
        else:
            start_d = today
    else:
        days = FIXED_WINDOW_DAYS.get(period, FIXED_WINDOW_DAYS["30d"])
        start_d = today - timedelta(days=days - 1)
    start_d = max(start_d, LEDGER_RELIABLE_SINCE)
    # A custom range wholly before the reliable ledger collapses to its floor.
    end_d = max(end_d, start_d)
    return start_d.isoformat(), end_d.isoformat()


EDGE_MIN_TRADES = 30
EDGE_T_THRESHOLD = 2.0
EDGE_Z_95 = 1.96
# Trades on one day share that day's market, so they are not independent; the
# day-level check treats each trading day as one observation.
EDGE_MIN_DAYS = 10


def _t(values: np.ndarray) -> float | None:
    if len(values) < 2:
        return None
    std = float(values.std(ddof=1))
    return None if std == 0 else float(values.mean()) / (std / float(np.sqrt(len(values))))


def edge_stats(
    net_pnls: list[float],
    costed: int | None = None,
    trade_dates: list[str] | None = None,
) -> dict:
    """Is the per-trade average distinguishable from luck? Inputs are per-trade
    NET P&L values (realized minus estimated charges; an unknown cost counts as
    0 and `costed` reports how many trades had a known cost).

    t_stat = mean / (sample std / sqrt(n)); None for n < 2 or zero spread.
    expectancy_ci95 = mean +/- 1.96 standard errors; None for n < 2.
    Verdict: fewer than 30 trades is "too few trades"; else t >= 2 is
    "likely real", t <= -2 "likely negative", otherwise "not distinguishable
    from zero". A normal-approximation screen, not a proof.

    With `trade_dates` (one per trade) the per-trade t is also checked at the
    day level: net P&L summed per trading day, t over those days
    (`day_t_stat`, `edge_days`). A verdict then needs at least EDGE_MIN_DAYS
    trading days and both t-stats past the threshold in the same direction;
    same-day trades are correlated, so the per-trade t alone overstates it.
    """
    n = len(net_pnls)
    base = {
        "edge_trades": n,
        "edge_cost_coverage": costed if costed is not None else n,
        "net_expectancy": None,
        "net_pnl_std": None,
        "t_stat": None,
        "expectancy_ci95": None,
        "edge_verdict": "too few trades",
        "edge_days": None,
        "day_t_stat": None,
    }
    if n == 0:
        return base
    arr = np.asarray(net_pnls, dtype=float)
    mean = float(arr.mean())
    base["net_expectancy"] = round(mean, 2)
    if n < 2:
        return base
    std = float(arr.std(ddof=1))
    base["net_pnl_std"] = round(std, 2)
    stderr = std / float(np.sqrt(n))
    base["expectancy_ci95"] = [
        round(mean - EDGE_Z_95 * stderr, 2),
        round(mean + EDGE_Z_95 * stderr, 2),
    ]
    if std == 0:
        return base
    t_stat = mean / stderr
    base["t_stat"] = round(t_stat, 2)
    t_values = [t_stat]
    enough = n >= EDGE_MIN_TRADES
    if trade_dates is not None:
        by_day = pd.Series(arr).groupby(pd.Series(list(trade_dates))).sum().to_numpy()
        day_t = _t(by_day)
        base["edge_days"] = len(by_day)
        base["day_t_stat"] = None if day_t is None else round(day_t, 2)
        t_values.append(day_t if day_t is not None else 0.0)
        if enough and len(by_day) < EDGE_MIN_DAYS:
            base["edge_verdict"] = "too few days"
            return base
    if enough:
        if min(t_values) >= EDGE_T_THRESHOLD:
            base["edge_verdict"] = "likely real"
        elif max(t_values) <= -EDGE_T_THRESHOLD:
            base["edge_verdict"] = "likely negative"
        else:
            base["edge_verdict"] = "not distinguishable from zero"
    return base


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
        (gross_profit / gross_loss)
        if gross_loss > 0
        else (float("inf") if gross_profit > 0 else 0.0)
    )
    expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss
    payoff_ratio = (avg_win / avg_loss) if avg_loss > 0 else (float("inf") if avg_win > 0 else 0.0)

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
        (annualized_pnl / abs(max_dd))
        if max_dd < 0
        else (float("inf") if annualized_pnl > 0 else 0.0)
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
            {"date": d.strftime("%Y-%m-%d"), "pnl": round(float(v), 2)}
            for d, v in daily_pnl.items()
        ],
        "equity_curve": [
            {"date": d.strftime("%Y-%m-%d"), "value": round(float(v), 2)} for d, v in equity.items()
        ],
    }


def _pct_of(value, capital: float) -> float | None:
    """`value` as a percent of `capital`, rounded to 2 places; None when it cannot be computed."""
    if value is None or not capital or capital <= 0 or capital != capital:
        return None
    return round(float(value) / capital * 100.0, 2)


def compute_capital_view(metrics: dict, capital: float) -> dict:
    """Percent-of-capital view of the rupee metrics already computed for a selection.

    Pure: no I/O, and `metrics` is read, never changed. `metrics` holds the keys
    `net_profit`, `net_after_costs` (may be None), `max_drawdown`, `best_day` and
    `worst_day`. Every percentage divides by the one fixed `capital`, applied to
    the whole window; none of it is a record of capital used at the time. A
    percentage that cannot be computed is None.
    """
    return {
        "capital": capital,
        "return_pct": _pct_of(metrics.get("net_profit"), capital),
        "return_net_pct": _pct_of(metrics.get("net_after_costs"), capital),
        "max_drawdown_pct_of_capital": _pct_of(metrics.get("max_drawdown"), capital),
        "best_day_pct": _pct_of(metrics.get("best_day"), capital),
        "worst_day_pct": _pct_of(metrics.get("worst_day"), capital),
        "basis": {
            "return_pct": "Gross realised P&L over the fixed capital entered by you",
            "return_net_pct": (
                "Realised P&L after estimated charges over the fixed capital entered by you"
            ),
            "max_drawdown_pct_of_capital": (
                "Largest fall in cumulative P&L over the fixed capital entered by you"
            ),
            "best_day_pct": "Best day's P&L over the fixed capital entered by you",
            "worst_day_pct": "Worst day's P&L over the fixed capital entered by you",
        },
    }
