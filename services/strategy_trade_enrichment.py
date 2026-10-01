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
from portfolio.broker_fees import attention_for, fees_for
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


def _schedule_for(trade: dict):
    """The cost schedule for a trade's instrument, or None when it is not modelled."""
    exchange = str(trade.get("exchange", "")).upper()
    product = str(trade.get("product", "")).upper()
    if exchange not in ("NSE", "BSE"):
        return None
    broker = trade.get("broker")
    if product == "MIS":
        return india_intraday(exchange, broker=broker)
    if product == "CNC":
        return india_delivery(exchange, broker=broker)
    return None


def _dp_charge(trade: dict, schedule) -> float:
    """DP charge on a delivery sell (a closed LONG CNC), with GST; 0 otherwise.

    The broker charges it per scrip debit, however many shares sold, so a position sold in
    several legs on one day pays it once (see estimate_trade_costs).
    """
    if str(trade.get("product", "")).upper() != "CNC" or trade.get("direction") != "LONG":
        return 0.0
    return fees_for(trade.get("broker")).dp_charge * (1.0 + schedule.tax_rate)


def estimate_trade_cost(trade: dict) -> float | None:
    """Estimated statutory charges plus brokerage for one round trip, or None
    when the instrument is not one the cost schedules model."""
    schedule = _schedule_for(trade)
    if schedule is None:
        return None

    qty = float(trade["closed_quantity"])
    entry_value = float(trade["entry_price"]) * qty
    exit_value = float(trade["exit_price"]) * qty
    if trade.get("direction") == "LONG":
        buy_value, sell_value = entry_value, exit_value
    else:
        buy_value, sell_value = exit_value, entry_value
    return round(
        schedule.charge(buy_value, sell_value, orders=2) + _dp_charge(trade, schedule), 2
    )


def _entry_order_charge(trade: dict, schedule) -> float:
    """What the entry order alone costs in brokerage plus the GST on it."""
    entry_value = float(trade["entry_price"]) * float(trade["closed_quantity"])
    # One order, entry side only: the per-order cap then applies to that order's own value.
    brokerage = schedule.breakdown(entry_value, 0.0, orders=1).get("brokerage", 0.0)
    return brokerage * (1.0 + schedule.tax_rate)


def estimate_trade_costs(trades: list[dict]) -> list[float | None]:
    """Estimated charges per closed-trade row, counting each entry order once.

    A closed-trade row is one exit leg. A position closed in several legs sent a single
    entry order, so only its first leg (by closing time) carries the entry brokerage; later
    legs carry the exit only. Legs of one position share (strategy, symbol, mode, date,
    direction, entry price). Charging every row a full entry+exit pair overstated brokerage
    on partial exits (2026-10-01: Rs 5,734 estimated against Rs 5,574 from the real order
    count). Output order matches the input.
    """
    costs: list[float | None] = [estimate_trade_cost(t) for t in trades]
    groups: dict[tuple, list[int]] = {}
    for i, t in enumerate(trades):
        key = (
            t.get("strategy"),
            t.get("symbol"),
            t.get("mode"),
            t.get("trade_date"),
            t.get("direction"),
            round(float(t.get("entry_price") or 0), 4),
        )
        groups.setdefault(key, []).append(i)
    for indexes in groups.values():
        if len(indexes) < 2:
            continue
        indexes.sort(key=lambda i: trades[i].get("closed_at") or "")
        for i in indexes[1:]:
            schedule = _schedule_for(trades[i])
            if costs[i] is not None and schedule is not None:
                # later legs: no second entry order, and no second DP debit that day
                saved = _entry_order_charge(trades[i], schedule) + _dp_charge(trades[i], schedule)
                costs[i] = round(costs[i] - saved, 2)
    return costs


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
    costs = estimate_trade_costs(trades)
    enriched = []
    for trade, cost in zip(trades, costs, strict=True):
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
                "cost": cost,
                "risk_amount": risk_amount,
                "r_multiple": r_multiple,
            }
        )
    return enriched


def trades_broker(rows: list) -> str:
    """The broker name the first trade in a group recorded (for messages about assumed rates)."""
    return str(rows[0][0].get("broker") or "") if rows else ""


def _by_broker(trades: list[dict], costs: list[float | None]) -> list[dict]:
    """Charges isolated per broker, so a change of broker never blends two fee schedules.

    A trade with no recorded broker (or one this system has no published fees for) is priced on
    the assumed default schedule and reported under "default" with `assumed` true.
    """
    groups: dict[str, list[tuple[dict, float | None]]] = {}
    for trade, cost in zip(trades, costs, strict=True):
        groups.setdefault(fees_for(trade.get("broker")).key, []).append((trade, cost))
    out = []
    for key, rows in sorted(groups.items()):
        fees = fees_for(key)
        gross = sum(float(t["realized_pnl"]) for t, _ in rows)
        all_costed = all(c is not None for _, c in rows)
        est = round(sum(c for _, c in rows), 2) if all_costed else None
        out.append(
            {
                "broker": key,
                "label": fees.label,
                "assumed": fees.assumed,
                "source": fees.source,
                # why these charges need a look (assumed rates, or fees read long ago), else None
                "attention": attention_for(key if not fees.assumed else trades_broker(rows)),
                "trades": len(rows),
                "gross": round(gross, 2),
                "est_costs": est,
                "net_after_costs": round(gross - est, 2) if est is not None else None,
                "charges_pct_of_gross": (
                    round(100.0 * est / gross, 2) if est is not None and gross > 0 else None
                ),
            }
        )
    return out


def _charge_metrics(trades: list[dict], costs: list[float | None]) -> dict:
    """Estimated charges and how much of the profit and turnover they take.

    Every figure is None unless every trade could be costed: a partial total would read as
    cheaper than the day really was.
    """
    all_costed = bool(trades) and all(c is not None for c in costs)
    gross = sum(float(t["realized_pnl"]) for t in trades)
    turnover = sum(
        (float(t["entry_price"]) + float(t["exit_price"])) * float(t["closed_quantity"])
        for t in trades
    )
    est_costs = round(sum(costs), 2) if all_costed else None
    return {
        "est_costs": est_costs,
        "net_after_costs": round(gross - est_costs, 2) if est_costs is not None else None,
        # Share of the gross profit the charges take. Undefined without a gross profit (a
        # losing day has nothing for charges to "eat"); above 100 means charges exceed profit.
        "charges_pct_of_gross": (
            round(100.0 * est_costs / gross, 2) if est_costs is not None and gross > 0 else None
        ),
        "turnover": round(turnover, 2),
        "charges_pct_of_turnover": (
            round(100.0 * est_costs / turnover, 4) if est_costs is not None and turnover > 0 else None
        ),
        "avg_charge_per_trade": round(est_costs / len(trades), 2) if est_costs is not None else None,
        "by_broker": _by_broker(trades, costs),
        # True when any trade was priced on a stand-in schedule rather than its broker's own
        "fee_basis_assumed": any(fees_for(t.get("broker")).assumed for t in trades),
    }


def summarize_costs(trades: list[dict]) -> dict:
    """Charge metrics for raw closed trades, without the signal-DB R lookup (comparison rows)."""
    return _charge_metrics(trades, estimate_trade_costs(trades))


def summarize_enrichment(enriched: list[dict]) -> dict:
    """Estimated charges, net-after-charges and expectancy in R, with coverage."""
    r_values = [t["r_multiple"] for t in enriched if t["r_multiple"] is not None]
    return {
        **_charge_metrics(enriched, [t["cost"] for t in enriched]),
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
