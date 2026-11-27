"""Market State Analysis report: closed trades grouped by the NIFTY 50 daily market state.

Read-only. Per-trade rows come from the existing services unchanged (the strategy
closed-trade ledger through ``enrich_trades``, or the discretionary completed
trades). Nothing here recomputes a charge or a P&L; see
``services/market_state_service.py`` for the rules.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from services import market_state_service as ms
from services import strategy_daily_performance_service as daily
from utils.logging import get_logger

logger = get_logger(__name__)

SOURCE_STRATEGY = "strategy"
SOURCE_DISCRETIONARY = "discretionary"
VALID_SOURCES = (SOURCE_STRATEGY, SOURCE_DISCRETIONARY)

BASIS_NOTE = (
    "Win rate and Gross P&L are before charges. Net P&L, Avg net/trade and Profit factor are "
    "after estimated charges and are shown only when every trade in the row has an estimate."
)


def _report(
    *,
    source: str,
    mode: str,
    period: str,
    start_date: str,
    end_date: str,
    rows: list[dict],
    bar_reader: Callable | None,
) -> dict[str, Any]:
    labels, data_status = ms.classify_dates_with_status(
        {r["trade_date"] for r in rows}, bar_reader=bar_reader
    )
    return {
        "status": "success",
        "benchmark": ms.BENCHMARK_LABEL,
        "methodology": ms.methodology(),
        "basis_note": BASIS_NOTE,
        "source": source,
        "mode": mode,
        "period": period,
        "start_date": start_date,
        "end_date": end_date,
        "trades_count": len(rows),
        "trend": ms.group_by_state(rows, labels, "trend"),
        "volatility": ms.group_by_state(rows, labels, "volatility"),
        "unclassified": ms.unclassified_summary(rows, labels),
        "data_status": data_status,
    }


def get_strategy_market_state(
    strategy: str | None,
    period: str,
    start: str | None = None,
    end: str | None = None,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    mode: str,
    bar_reader: Callable | None = None,
) -> tuple[bool, dict[str, Any], int]:
    """Strategy closed trades of ``mode`` in the window, grouped by market state.

    Uses the same window and strategy filter helpers as ``get_daily_performance``.
    """
    all_trades = daily.get_closed_trades(mode=mode) if period in daily.VALID_PERIODS else []
    window = daily._resolve(period, all_trades, start, end)
    if isinstance(window, dict):
        return False, window, 400
    start_date, end_date = window
    trades = daily._select(all_trades, daily._include_set(strategy, include), exclude)
    in_range = daily._in_range(trades, start_date, end_date)
    enriched = daily.enrich_trades(in_range, start_date, end_date)
    rows = [
        {"trade_date": t["trade_date"], "realized_pnl": t["realized_pnl"], "cost": t["cost"]}
        for t in enriched
    ]
    report = _report(
        source=SOURCE_STRATEGY,
        mode=mode,
        period=period,
        start_date=start_date,
        end_date=end_date,
        rows=rows,
        bar_reader=bar_reader,
    )
    return True, report, 200


def get_discretionary_market_state(
    period: str,
    start: str | None = None,
    end: str | None = None,
    broker: str | None = None,
    symbol: str | None = None,
    account: str | None = None,
    category: str = "all",
    *,
    bar_reader: Callable | None = None,
) -> dict[str, Any]:
    """Discretionary completed trades in the window, grouped by market state.

    Raises ``FilterError`` / ``ValueError`` from the discretionary service for a bad
    period, range or category.
    """
    from services import discretionary_pnl_service as disc

    scope = disc.get_completed_trade_rows(period, start, end, broker, symbol, account, category)
    return _report(
        source=SOURCE_DISCRETIONARY,
        mode="live",
        period=period,
        start_date=scope["start_date"],
        end_date=scope["end_date"],
        rows=scope["rows"],
        bar_reader=bar_reader,
    )


def get_today_state(
    session_date: str | None = None, *, bar_reader: Callable | None = None
) -> dict[str, Any]:
    """The label that applies to the current session: the previous valid bar before it."""
    if session_date is None:
        from utils.session import get_trading_session_date

        session_date = get_trading_session_date()
    labels, data_status = ms.classify_dates_with_status([session_date], bar_reader=bar_reader)
    label = labels[session_date]
    return {
        "status": "success",
        "benchmark": ms.BENCHMARK_LABEL,
        "session_date": session_date,
        "trend": label["trend"],
        "volatility": label["volatility"],
        "label_status": label["status"],
        "as_of": label["as_of"],
        "reason": label["reason"],
        "data_status": data_status,
    }
