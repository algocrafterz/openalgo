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

from collections import defaultdict
from datetime import date, datetime
from typing import Any

from database.market_calendar_db import is_market_holiday
from database.settings_db import get_analyze_mode
from database.strategy_book_db import get_closed_trades
from services.performance_insights_service import (
    build_insights,
    comparison_extras,
    excursion_report,
)
from services.performance_stats import trading_sessions
from services.strategy_metrics_service import (
    LEDGER_RELIABLE_SINCE,
    REFERENCE_CAPITAL_PER_STRATEGY,
    compute_capital_view,
    compute_metrics,
    edge_stats,
    last_session_date,
    resolve_period,
)
from services.strategy_trade_enrichment import (
    add_excursions,
    enrich_trades,
    entry_hour_breakdown,
    estimate_trade_costs,
    hold_bucket_breakdown,
    hold_stats,
    hour_breakdown,
    net_equity_curve,
    summarize_costs,
    summarize_enrichment,
    symbol_breakdown,
    weekday_breakdown,
    with_export_fields,
    with_hold_minutes,
)
from utils.logging import get_logger

logger = get_logger(__name__)

# Above this many trades the export skips MFE/MAE: the bar reads are per trade.
EXPORT_EXCURSION_MAX_TRADES = 2000

VALID_PERIODS = {"1d", "7d", "15d", "30d", "90d", "ytd", "all", "custom"}


def _current_mode() -> str:
    return "analyze" if get_analyze_mode() else "live"


def _today() -> date:
    return datetime.now().date()


def _resolve(
    period: str, trades: list[dict], start: str | None, end: str | None
) -> tuple[str, str] | dict[str, Any]:
    """The [start, end] window, or an error body for a bad period or range."""
    if period not in VALID_PERIODS:
        return {"status": "error", "message": f"Invalid period: {period}"}
    try:
        return resolve_period(period, trades, today=_today(), start=start, end=end)
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}


def _in_range(trades: list[dict], start_date: str, end_date: str) -> list[dict]:
    return [t for t in trades if start_date <= t["trade_date"] <= end_date]


def _select(
    trades: list[dict],
    include: set[str] | list[str] | None,
    exclude: set[str] | list[str] | None,
) -> list[dict]:
    """Trades of the chosen strategies. An empty include list means every
    strategy; a name in both lists is excluded."""
    include_set = set(include or ())
    exclude_set = set(exclude or ())
    return [
        t
        for t in trades
        if (not include_set or t["strategy"] in include_set) and t["strategy"] not in exclude_set
    ]


def _include_set(strategy: str | None, include: list[str] | None) -> set[str]:
    """The single `strategy` argument and the `include` list, as one set."""
    names = set(include or ())
    if strategy:
        names.add(strategy)
    return names


def _sessions(start_date: str, end_date: str, trades: list[dict]) -> list[str]:
    """NSE trading sessions in the window: weekdays that are not exchange holidays, plus any
    date a trade closed on. A calendar lookup that fails falls back to weekdays."""
    return trading_sessions(
        start_date,
        end_date,
        traded_dates=[t["trade_date"] for t in trades],
        is_holiday=lambda day: is_market_holiday(day, "NSE"),
    )


def _assumed_capital(capital: float | None, strategies: int) -> float:
    """Assumed allocated capital behind the daily returns: the amount per strategy (the
    configured control, else the reference amount) times the strategies selected."""
    return float(capital or REFERENCE_CAPITAL_PER_STRATEGY) * max(1, strategies)


def _edge_from_costs(trades: list[dict], costs: list[float | None]) -> dict:
    """Net-of-charges edge statistics; an unknown cost counts as 0."""
    net = [float(t["realized_pnl"]) - (c or 0.0) for t, c in zip(trades, costs, strict=True)]
    return edge_stats(
        net,
        costed=sum(1 for c in costs if c is not None),
        trade_dates=[t["trade_date"] for t in trades],
    )


def get_daily_performance(
    strategy: str | None,
    period: str,
    start: str | None = None,
    end: str | None = None,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    capital: float | None = None,
) -> tuple[bool, dict[str, Any], int]:
    """Metrics + daily P&L series + equity curve for one strategy, or the
    pooled portfolio (every strategy's closed trades combined) if `strategy`
    is omitted. `include` adds more strategies to `strategy`; `exclude`
    removes strategies (exclusion wins). Pooled figures are always computed
    from the filtered trades.

    The window is resolved against every strategy's trades in the mode, so
    1D names the same last session whichever strategies are selected.

    `capital` (validated by the caller, rupees) adds one `capital_view` key with
    a scaled what-if of the rupee metrics at that capital per strategy. Without it the response
    is unchanged.
    """
    mode = _current_mode()
    all_trades = get_closed_trades(mode=mode) if period in VALID_PERIODS else []
    window = _resolve(period, all_trades, start, end)
    if isinstance(window, dict):
        return False, window, 400
    start_date, end_date = window

    trades = _select(all_trades, _include_set(strategy, include), exclude)
    trades_in_range = _in_range(trades, start_date, end_date)

    enriched = enrich_trades(trades_in_range, start_date, end_date)
    included = sorted({t["strategy"] for t in trades_in_range})
    sessions = _sessions(start_date, end_date, trades_in_range)
    metrics = compute_metrics(
        trades_in_range,
        start_date,
        end_date,
        costs=[t["cost"] for t in enriched],
        sessions=sessions,
        capital=_assumed_capital(capital, len(included)),
    )
    response = {
        "status": "success",
        "mode": mode,
        "strategy": strategy,
        "period": period,
        "included": included,
        "excluded": sorted(set(exclude or ())),
        **metrics,
        **summarize_enrichment(enriched),
        **_edge_from_costs(trades_in_range, [t["cost"] for t in enriched]),
        "by_symbol": symbol_breakdown(trades_in_range),
        "by_hour": hour_breakdown(trades_in_range),
        "by_weekday": weekday_breakdown(trades_in_range),
        "hold_stats": hold_stats(trades_in_range),
        "by_entry_hour": entry_hour_breakdown(trades_in_range),
        "by_hold_bucket": hold_bucket_breakdown(trades_in_range),
        "insights": build_insights(
            enriched, sessions, metrics["sharpe_ratio"], metrics["sortino_ratio"]
        ),
    }
    response["equity_curve_net"], response["equity_net_cost_coverage"] = net_equity_curve(
        metrics["equity_curve"], enriched
    )
    if capital is not None:
        response["capital_view"] = compute_capital_view(
            response, capital, max(1, len(response.get("included") or []))
        )
    if start_date == end_date:
        # Ratios like Sharpe mean nothing over one day; the trades themselves
        # are what a single-session view shows.
        with_excursions, covered, total = add_excursions(
            with_hold_minutes(enriched), start_date, end_date
        )
        response["trades"] = sorted(
            with_excursions, key=lambda t: t["closed_at"] or "", reverse=True
        )
        response["excursion_covered"] = covered
        response["excursion_total"] = total
    return True, response, 200


def get_strategy_comparison(
    period: str,
    start: str | None = None,
    end: str | None = None,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> tuple[bool, dict[str, Any], int]:
    """The same metrics, condensed, for every strategy in the current mode
    side by side - the "bird's eye view" comparison table. `include` and
    `exclude` narrow the rows; `all_strategies` always lists every strategy.

    One ledger read for the mode, grouped here, rather than one per strategy.
    """
    mode = _current_mode()
    all_trades = get_closed_trades(mode=mode) if period in VALID_PERIODS else []
    window = _resolve(period, all_trades, start, end)
    if isinstance(window, dict):
        return False, window, 400
    start_date, end_date = window
    base = {
        "status": "success",
        "mode": mode,
        "period": period,
        "start_date": start_date,
        "end_date": end_date,
    }

    by_strategy: dict[str, list[dict]] = defaultdict(list)
    all_in_range = _in_range(_select(all_trades, include, exclude), start_date, end_date)
    for t in all_in_range:
        by_strategy[t["strategy"]].append(t)

    # Strategies come from the trades themselves: one whose position row was
    # reset or pruned still has history worth comparing.
    # One calendar lookup and one signal lookup for the window, shared by every row.
    sessions = _sessions(start_date, end_date, all_in_range)
    enriched_all = enrich_trades(all_in_range, start_date, end_date)
    enriched_by_strategy: dict[str, list[dict]] = defaultdict(list)
    for t in enriched_all:
        enriched_by_strategy[t["strategy"]].append(t)
    ratios: dict[str, tuple[float | None, float | None]] = {}
    rows = []
    for name in sorted(by_strategy):
        trades_in_range = by_strategy[name]
        trade_costs = [t["cost"] for t in enriched_by_strategy[name]]
        m = compute_metrics(
            trades_in_range,
            start_date,
            end_date,
            costs=trade_costs,
            sessions=sessions,
            capital=_assumed_capital(None, 1),
        )
        ratios[name] = (m["sharpe_ratio"], m["sortino_ratio"])
        costs = summarize_costs(trades_in_range)
        holds = hold_stats(trades_in_range)
        edge = _edge_from_costs(trades_in_range, trade_costs)
        rows.append(
            {
                "strategy": name,
                "est_costs": costs["est_costs"],
                "net_after_costs": costs["net_after_costs"],
                "charges_pct_of_gross": costs["charges_pct_of_gross"],
                "trades_count": m["trades_count"],
                "win_rate": m["win_rate"],
                "profit_factor": m["profit_factor"],
                "net_profit": m["net_profit"],
                "expectancy": m["expectancy"],
                "sharpe_ratio": m["sharpe_ratio"],
                "sortino_ratio": m["sortino_ratio"],
                "ratio_warning": m["ratio_warning"],
                "max_drawdown": m["max_drawdown"],
                "best_win_streak": m["best_win_streak"],
                "best_loss_streak": m["best_loss_streak"],
                "avg_hold_min": holds["avg_min"],
                "median_hold_min": holds["median_min"],
                "net_expectancy": edge["net_expectancy"],
                "t_stat": edge["t_stat"],
                "edge_verdict": edge["edge_verdict"],
            }
        )

    # Best net_profit first - the comparison table's default sort answers
    # "which strategy is doing best" at a glance.
    portfolio_metrics = compute_metrics(
        all_in_range,
        start_date,
        end_date,
        costs=[t["cost"] for t in enriched_all],
        sessions=sessions,
        capital=_assumed_capital(None, len(by_strategy)),
    )
    ratios["__portfolio__"] = (
        portfolio_metrics["sharpe_ratio"],
        portfolio_metrics["sortino_ratio"],
    )
    extras = comparison_extras(dict(enriched_by_strategy), sessions, ratios)
    for row in rows:
        row.update(extras["per_strategy"].get(row["strategy"], {}))
    rows.sort(key=lambda r: r["net_profit"], reverse=True)

    floor = LEDGER_RELIABLE_SINCE.isoformat()
    all_strategies = sorted({t["strategy"] for t in all_trades if t["trade_date"] >= floor})
    return (
        True,
        {
            **base,
            "included": sorted(by_strategy),
            "excluded": sorted(set(exclude or ())),
            "strategies": rows,
            "portfolio": extras["portfolio"],
            "correlation": extras["correlation"],
            "contribution": extras["contribution"],
            "all_strategies": all_strategies,
        },
        200,
    )


def get_excursion_quality(
    strategy: str | None,
    period: str,
    start: str | None = None,
    end: str | None = None,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> tuple[bool, dict[str, Any], int]:
    """MAE/MFE quality for the chosen trades. Separate from the daily report because it
    reads one-minute bars per trade; a past session's excursions are cached for hours.
    Above EXPORT_EXCURSION_MAX_TRADES trades it is skipped and says so."""
    mode = _current_mode()
    all_trades = get_closed_trades(mode=mode) if period in VALID_PERIODS else []
    window = _resolve(period, all_trades, start, end)
    if isinstance(window, dict):
        return False, window, 400
    start_date, end_date = window
    pool = _select(all_trades, _include_set(strategy, include), exclude)
    in_range = _in_range(pool, start_date, end_date)
    body: dict[str, Any] = {
        "status": "success",
        "mode": mode,
        "start_date": start_date,
        "end_date": end_date,
        "excursions": None,
    }
    if len(in_range) > EXPORT_EXCURSION_MAX_TRADES:
        body["message"] = "Too many trades in this range to read price bars for each one."
        return True, body, 200
    enriched = enrich_trades(in_range, start_date, end_date)
    with_excursions, _covered, _total = add_excursions(enriched, start_date, end_date)
    body["excursions"] = excursion_report(with_excursions)
    return True, body, 200


def get_last_session(mode: str | None = None) -> dict[str, Any] | None:
    """Realized P&L of the most recent session with closed trades (on or before
    today, inside the reliable ledger window), or None when there is none or the
    ledger cannot be read. Reporting only."""
    try:
        trades = get_closed_trades(mode=mode or _current_mode())
        day = last_session_date(trades, _today())
        if day is None:
            return None
        iso = day.isoformat()
        rows = [t for t in trades if t["trade_date"] == iso]
        strategies: dict[str, float] = defaultdict(float)
        for t in rows:
            strategies[t["strategy"]] += float(t["realized_pnl"])
        return {
            "date": iso,
            "strategies": {k: round(v, 2) for k, v in sorted(strategies.items())},
            "total": round(sum(strategies.values()), 2),
            "trades_count": len(rows),
        }
    except Exception:
        logger.exception("Could not compute the last session summary")
        return None


def get_trades_for_export(
    strategy: str | None,
    period: str,
    start: str | None = None,
    end: str | None = None,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> tuple[bool, dict[str, Any], int]:
    """Enriched closed trades in the resolved range, oldest first, for CSV export.
    MFE/MAE are added for ranges of up to EXPORT_EXCURSION_MAX_TRADES trades only.
    On success the body holds start_date, end_date and trades."""
    mode = _current_mode()
    all_trades = get_closed_trades(mode=mode) if period in VALID_PERIODS else []
    window = _resolve(period, all_trades, start, end)
    if isinstance(window, dict):
        return False, window, 400
    start_date, end_date = window
    pool = _select(all_trades, _include_set(strategy, include), exclude)
    in_range = _in_range(pool, start_date, end_date)
    enriched = with_hold_minutes(enrich_trades(in_range, start_date, end_date))
    enriched.sort(key=lambda t: t["closed_at"] or "")
    if len(enriched) <= EXPORT_EXCURSION_MAX_TRADES:
        enriched, _covered, _total = add_excursions(enriched, start_date, end_date)
    enriched = with_export_fields(enriched)
    return (
        True,
        {"status": "success", "start_date": start_date, "end_date": end_date, "trades": enriched},
        200,
    )
