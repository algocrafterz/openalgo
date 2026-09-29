"""Per-trade enrichment for the Strategy Performance page: estimated charges,
R multiples and per-symbol P&L.

Nothing here invents data. A trade whose stop cannot be identified gets no R,
a trade on an instrument the cost schedule does not model gets no cost, and
both summaries report how many trades they cover so the page can show it.

R needs the stop the trade was planned with. The strategy book does not store
one, but the signal engine records entry and stop for every signal it
executes, so each closed trade is matched to that signal by strategy, symbol,
direction, session date and price. No match, or two matches that disagree,
means no R for that trade.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import text

from database.engine_factory import create_db_engine
from portfolio.costs import india_delivery, india_intraday
from utils.logging import get_logger

logger = get_logger(__name__)

SIGNAL_TRADES_DB = Path(__file__).resolve().parent.parent / "signal_engine" / "data" / "trades.db"

# A ledger entry is the sandbox/broker fill; the signal's entry is the alert
# price. They differ by slippage, never by anything near this.
_PRICE_MATCH_TOLERANCE = 0.005

_engines: dict[str, Any] = {}


def _engine_for(path: Path):
    key = str(path)
    if key not in _engines:
        _engines[key] = create_db_engine(f"sqlite:///{key}")
    return _engines[key]


def estimate_trade_cost(trade: dict) -> float | None:
    """Estimated statutory charges plus brokerage for one round trip, or None
    when the instrument is not one the cost schedules model."""
    exchange = str(trade.get("exchange", "")).upper()
    product = str(trade.get("product", "")).upper()
    if exchange not in ("NSE", "BSE"):
        return None
    if product == "MIS":
        schedule = india_intraday(exchange)
    elif product == "CNC":
        schedule = india_delivery(exchange)
    else:
        return None

    qty = float(trade["closed_quantity"])
    entry_value = float(trade["entry_price"]) * qty
    exit_value = float(trade["exit_price"]) * qty
    if trade.get("direction") == "LONG":
        buy_value, sell_value = entry_value, exit_value
    else:
        buy_value, sell_value = exit_value, entry_value
    return round(schedule.charge(buy_value, sell_value, orders=2), 2)


def _load_signal_stops(
    start_date: str, end_date: str, db_path: Path | None
) -> dict[tuple, set[tuple[float, float]]]:
    """(strategy, symbol, direction, mode, date) -> {(entry, sl), ...} for every
    executed entry signal in the range. Empty if the signal DB is unavailable."""
    path = Path(db_path) if db_path else SIGNAL_TRADES_DB
    if not path.exists():
        logger.debug(f"Signal trades DB not found at {path}; R multiples unavailable")
        return {}
    try:
        with _engine_for(path).connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT strategy, symbol, direction, trade_mode, "
                    "substr(executed_at, 1, 10) AS d, entry, sl FROM trades "
                    "WHERE status = 'SUCCESS' AND direction IN ('LONG', 'SHORT') "
                    "AND sl IS NOT NULL AND sl > 0 AND entry IS NOT NULL AND entry > 0 "
                    "AND substr(executed_at, 1, 10) BETWEEN :s AND :e"
                ),
                {"s": start_date, "e": end_date},
            ).all()
    except Exception:
        logger.exception("Could not read signal stops for R multiples")
        return {}

    index: dict[tuple, set[tuple[float, float]]] = {}
    for strategy, symbol, direction, mode, day, entry, sl in rows:
        index.setdefault((strategy, symbol, direction, mode, day), set()).add(
            (float(entry), float(sl))
        )
    return index


def _match_stop(trade: dict, index: dict) -> tuple[float, float] | None:
    candidates = index.get(
        (
            trade["strategy"],
            trade["symbol"],
            trade["direction"],
            trade["mode"],
            trade["trade_date"],
        ),
        set(),
    )
    entry_price = float(trade["entry_price"])
    near = {
        (entry, sl)
        for entry, sl in candidates
        if entry_price > 0 and abs(entry - entry_price) / entry_price <= _PRICE_MATCH_TOLERANCE
    }
    return next(iter(near)) if len(near) == 1 else None


def enrich_trades(
    trades: list[dict], start_date: str, end_date: str, db_path: Path | None = None
) -> list[dict]:
    """Copies of `trades` with `cost`, `risk_amount` and `r_multiple` added
    (each None when it cannot be determined)."""
    index = _load_signal_stops(start_date, end_date, db_path)
    enriched = []
    for trade in trades:
        stop = _match_stop(trade, index) if index else None
        risk_amount = r_multiple = None
        if stop is not None:
            risk_per_share = abs(stop[0] - stop[1])
            risk_amount = round(risk_per_share * float(trade["closed_quantity"]), 2)
            if risk_amount > 0:
                r_multiple = round(float(trade["realized_pnl"]) / risk_amount, 4)
            else:
                risk_amount = None
        enriched.append(
            {
                **trade,
                "cost": estimate_trade_cost(trade),
                "risk_amount": risk_amount,
                "r_multiple": r_multiple,
            }
        )
    return enriched


def summarize_enrichment(enriched: list[dict]) -> dict:
    """Estimated charges, net-after-charges and expectancy in R, with coverage."""
    r_values = [t["r_multiple"] for t in enriched if t["r_multiple"] is not None]
    costs = [t["cost"] for t in enriched]
    all_costed = bool(enriched) and all(c is not None for c in costs)
    est_costs = round(sum(costs), 2) if all_costed else None
    gross = sum(t["realized_pnl"] for t in enriched)
    return {
        "est_costs": est_costs,
        "net_after_costs": round(gross - est_costs, 2) if est_costs is not None else None,
        "expectancy_r": round(sum(r_values) / len(r_values), 2) if r_values else None,
        "total_r": round(sum(r_values), 2) if r_values else None,
        "r_covered": len(r_values),
        "r_total": len(enriched),
    }


def symbol_breakdown(trades: list[dict]) -> list[dict]:
    """Net P&L, trade count and win rate per symbol, best first."""
    by_symbol: dict[str, list[float]] = {}
    for t in trades:
        by_symbol.setdefault(t["symbol"], []).append(float(t["realized_pnl"]))
    rows = [
        {
            "symbol": symbol,
            "trades": len(pnls),
            "net_pnl": round(sum(pnls), 2),
            "win_rate": round(100.0 * sum(1 for p in pnls if p > 0) / len(pnls), 2),
        }
        for symbol, pnls in by_symbol.items()
    ]
    rows.sort(key=lambda r: r["net_pnl"], reverse=True)
    return rows
