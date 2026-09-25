"""
Orchestration for the Strategy Daily Performance page (day-by-day metrics,
extends the real-time Strategy P&L page - see
docs/strategy-daily-performance.md).

Resolves live/analyze mode the same way
`services/strategy_performance_service.py` does (`get_analyze_mode()` - the
client never passes mode), reads the closed-trade ledger from
`database/strategy_book_db.py`, and hands it to
`services/strategy_metrics_service.py` for the actual math.
"""

from typing import Any

from database.settings_db import get_analyze_mode
from database.strategy_book_db import get_closed_trades, list_strategies
from services.strategy_metrics_service import compute_metrics, resolve_period
from utils.logging import get_logger

logger = get_logger(__name__)

VALID_PERIODS = {"7d", "30d", "90d", "ytd", "all"}


def _current_mode() -> str:
    return "analyze" if get_analyze_mode() else "live"


def get_daily_performance(strategy: str | None, period: str) -> tuple[bool, dict[str, Any], int]:
    """Metrics + daily P&L series + equity curve for one strategy, or the
    pooled portfolio (every strategy's closed trades combined) if `strategy`
    is omitted."""
    if period not in VALID_PERIODS:
        return False, {"status": "error", "message": f"Invalid period: {period}"}, 400

    mode = _current_mode()
    trades = get_closed_trades(strategy=strategy, mode=mode)
    start_date, end_date = resolve_period(period, trades)
    # resolve_period("all") derives its own start from the trades already
    # fetched above; a fixed window still needs the trades re-filtered to
    # that window before computing metrics.
    trades_in_range = [t for t in trades if start_date <= t["trade_date"] <= end_date]

    metrics = compute_metrics(trades_in_range, start_date, end_date)
    return (
        True,
        {
            "status": "success",
            "mode": mode,
            "strategy": strategy,
            "period": period,
            **metrics,
        },
        200,
    )


def get_strategy_comparison(period: str) -> tuple[bool, dict[str, Any], int]:
    """The same metrics, condensed, for every strategy in the current mode
    side by side - the "bird's eye view" comparison table."""
    if period not in VALID_PERIODS:
        return False, {"status": "error", "message": f"Invalid period: {period}"}, 400

    mode = _current_mode()
    strategies = list_strategies()
    if not strategies:
        return True, {"status": "success", "mode": mode, "period": period, "strategies": []}, 200

    rows = []
    for name in strategies:
        trades = get_closed_trades(strategy=name, mode=mode)
        if not trades:
            continue
        start_date, end_date = resolve_period(period, trades)
        trades_in_range = [t for t in trades if start_date <= t["trade_date"] <= end_date]
        if not trades_in_range:
            continue
        m = compute_metrics(trades_in_range, start_date, end_date)
        rows.append(
            {
                "strategy": name,
                "trades_count": m["trades_count"],
                "win_rate": m["win_rate"],
                "profit_factor": m["profit_factor"],
                "net_profit": m["net_profit"],
                "expectancy": m["expectancy"],
                "sharpe_ratio": m["sharpe_ratio"],
                "max_drawdown": m["max_drawdown"],
                "best_win_streak": m["best_win_streak"],
                "best_loss_streak": m["best_loss_streak"],
            }
        )

    # Best net_profit first - the comparison table's default sort answers
    # "which strategy is doing best" at a glance.
    rows.sort(key=lambda r: r["net_profit"], reverse=True)

    return True, {"status": "success", "mode": mode, "period": period, "strategies": rows}, 200
