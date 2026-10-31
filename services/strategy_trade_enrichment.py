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

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from database.engine_factory import create_db_engine
from portfolio.broker_fees import attention_for, fees_for
from portfolio.costs import india_delivery, india_intraday
from utils.logging import get_logger
from utils.thread_safe_cache import LockedTTLCache

logger = get_logger(__name__)

SIGNAL_TRADES_DB = Path(__file__).resolve().parent.parent / "signal_engine" / "data" / "trades.db"

# A ledger entry is the sandbox/broker fill; the signal's entry is the alert
# price. They differ by slippage, never by anything near this.
_PRICE_MATCH_TOLERANCE = 0.005
_IST = timezone(timedelta(hours=5, minutes=30))

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
    return round(schedule.charge(buy_value, sell_value, orders=2) + _dp_charge(trade, schedule), 2)


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


def _entry_slippage(trade: dict, signal_entry: float) -> float:
    """Adverse entry slippage in rupees: the fill against the signal's alert price.

    Positive means the fill was worse than the alert (a LONG bought higher, a SHORT sold
    lower), negative means better. It is already inside the realized P&L, so it is
    reported, never deducted again.
    """
    per_share = float(trade["entry_price"]) - signal_entry
    if trade.get("direction") == "SHORT":
        per_share = -per_share
    return round(per_share * float(trade["closed_quantity"]), 2)


def enrich_trades(
    trades: list[dict], start_date: str, end_date: str, db_path: Path | None = None
) -> list[dict]:
    """Copies of `trades` with `cost`, `risk_amount`, `r_multiple`, `entry_slippage` and
    `signal_sl` (the matched signal stop price)
    added (each None when it cannot be determined)."""
    index = _load_signal_stops(start_date, end_date, db_path)
    costs = estimate_trade_costs(trades)
    enriched = []
    for trade, cost in zip(trades, costs, strict=True):
        stop = _match_stop(trade, index) if index else None
        risk_amount = r_multiple = entry_slippage = signal_sl = None
        if stop is not None:
            signal_sl = stop[1]
            entry_slippage = _entry_slippage(trade, stop[0])
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
                "entry_slippage": entry_slippage,
                "signal_sl": signal_sl,
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
            round(100.0 * est_costs / turnover, 4)
            if est_costs is not None and turnover > 0
            else None
        ),
        "avg_charge_per_trade": round(est_costs / len(trades), 2)
        if est_costs is not None
        else None,
        "by_broker": _by_broker(trades, costs),
        # True when any trade was priced on a stand-in schedule rather than its broker's own
        "fee_basis_assumed": any(fees_for(t.get("broker")).assumed for t in trades),
    }


def summarize_costs(trades: list[dict]) -> dict:
    """Charge metrics for raw closed trades, without the signal-DB R lookup (comparison rows)."""
    return _charge_metrics(trades, estimate_trade_costs(trades))


def _slippage_metrics(enriched: list[dict]) -> dict:
    """Measured entry slippage over the trades whose signal could be matched.

    Informational: the fills the P&L is built on already carry it. The basis in basis points
    is the entry turnover of the same covered trades, so a partial match is not diluted by
    trades that have no figure.
    """
    measured = [t for t in enriched if t.get("entry_slippage") is not None]
    if not measured:
        return {
            "entry_slippage": None,
            "entry_slippage_bps": None,
            "slippage_covered": 0,
            "slippage_total": len(enriched),
        }
    total = sum(t["entry_slippage"] for t in measured)
    entry_value = sum(float(t["entry_price"]) * float(t["closed_quantity"]) for t in measured)
    return {
        "entry_slippage": round(total, 2),
        "entry_slippage_bps": round(10000.0 * total / entry_value, 2) if entry_value > 0 else None,
        "slippage_covered": len(measured),
        "slippage_total": len(enriched),
    }


def summarize_enrichment(enriched: list[dict]) -> dict:
    """Estimated charges, net-after-charges and expectancy in R, with coverage."""
    r_values = [t["r_multiple"] for t in enriched if t["r_multiple"] is not None]
    return {
        **_charge_metrics(enriched, [t["cost"] for t in enriched]),
        **_slippage_metrics(enriched),
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


_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _bucket_row(key: str, label, pnls: list[float]) -> dict:
    return {
        key: label,
        "trades": len(pnls),
        "net_pnl": round(sum(pnls), 2),
        "win_rate": round(100.0 * sum(1 for p in pnls if p > 0) / len(pnls), 2),
    }


def hour_breakdown(trades: list[dict]) -> list[dict]:
    """Trades, net P&L and win rate by EXIT hour (IST, from closed_at), hour ascending.

    Trades with no parseable closed_at are left out. See entry_hour_breakdown for entry hour.
    """
    buckets: dict[int, list[float]] = {}
    for t in trades:
        closed_at = t.get("closed_at") or ""
        try:
            hour = int(closed_at[11:13])
        except ValueError:
            continue
        if not 0 <= hour <= 23:
            continue
        buckets.setdefault(hour, []).append(float(t["realized_pnl"]))
    return [_bucket_row("hour", h, buckets[h]) for h in sorted(buckets)]


def weekday_breakdown(trades: list[dict]) -> list[dict]:
    """Trades, net P&L and win rate by weekday of trade_date, Mon first, present days only."""
    buckets: dict[int, list[float]] = {}
    for t in trades:
        try:
            wd = date.fromisoformat(t["trade_date"]).weekday()
        except (ValueError, TypeError, KeyError):
            continue
        buckets.setdefault(wd, []).append(float(t["realized_pnl"]))
    return [_bucket_row("weekday", _WEEKDAYS[w], buckets[w]) for w in sorted(buckets)]


def _parse_naive(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T")[:19])
    except (ValueError, TypeError):
        return None


def hold_minutes(trade: dict) -> float | None:
    """Minutes between opened_at and closed_at; None when either is missing,
    unparseable, or the result is negative."""
    opened, closed = _parse_naive(trade.get("opened_at")), _parse_naive(trade.get("closed_at"))
    if opened is None or closed is None:
        return None
    minutes = (closed - opened).total_seconds() / 60.0
    return round(minutes, 2) if minutes >= 0 else None


def with_hold_minutes(trades: list[dict]) -> list[dict]:
    """Copies of `trades` with `hold_min` added (None when it cannot be determined)."""
    return [{**t, "hold_min": hold_minutes(t)} for t in trades]


def _percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolated percentile (q in 0..1) of an ascending, non-empty list."""
    pos = q * (len(sorted_values) - 1)
    low = int(pos)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (pos - low)


def _mean_or_none(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def hold_stats(trades: list[dict]) -> dict:
    """Holding-time summary in minutes over trades that have an entry time.

    Winners are realized_pnl > 0, losers < 0. Every figure is None when no trade is covered."""
    holds = [(hold_minutes(t), float(t["realized_pnl"])) for t in trades]
    holds = [(m, p) for m, p in holds if m is not None]
    minutes = sorted(m for m, _ in holds)
    stats = {
        "avg_min": None,
        "median_min": None,
        "p90_min": None,
        "winners_avg_min": _mean_or_none([m for m, p in holds if p > 0]),
        "losers_avg_min": _mean_or_none([m for m, p in holds if p < 0]),
        "covered": len(minutes),
        "total": len(trades),
    }
    if minutes:
        stats["avg_min"] = _mean_or_none(minutes)
        stats["median_min"] = round(_percentile(minutes, 0.5), 1)
        stats["p90_min"] = round(_percentile(minutes, 0.9), 1)
    return stats


def entry_hour_breakdown(trades: list[dict]) -> list[dict]:
    """Trades, net P&L, win rate and average hold by ENTRY hour (from opened_at), ascending.
    Trades without an entry time are left out."""
    buckets: dict[int, list[dict]] = {}
    for t in trades:
        opened = _parse_naive(t.get("opened_at"))
        if opened is not None:
            buckets.setdefault(opened.hour, []).append(t)
    rows = []
    for hour in sorted(buckets):
        group = buckets[hour]
        row = _bucket_row("hour", hour, [float(t["realized_pnl"]) for t in group])
        row["avg_hold_min"] = _mean_or_none(
            [m for m in (hold_minutes(t) for t in group) if m is not None]
        )
        rows.append(row)
    return rows


HOLD_BUCKETS = ("<15 min", "15-60 min", "1-3 h", "3 h+ same day", "Overnight")


def _hold_bucket(trade: dict) -> str | None:
    minutes = hold_minutes(trade)
    if minutes is None:
        return None
    if _parse_naive(trade["opened_at"]).date() != _parse_naive(trade["closed_at"]).date():
        return "Overnight"
    if minutes < 15:
        return HOLD_BUCKETS[0]
    if minutes < 60:
        return HOLD_BUCKETS[1]
    if minutes < 180:
        return HOLD_BUCKETS[2]
    return HOLD_BUCKETS[3]


def hold_bucket_breakdown(trades: list[dict]) -> list[dict]:
    """Trades, net P&L, win rate and average P&L by holding-time bucket, in fixed order,
    only buckets with trades. Overnight means the entry and exit dates differ."""
    buckets: dict[str, list[float]] = {}
    for t in trades:
        name = _hold_bucket(t)
        if name is not None:
            buckets.setdefault(name, []).append(float(t["realized_pnl"]))
    rows = []
    for name in HOLD_BUCKETS:
        if name in buckets:
            row = _bucket_row("bucket", name, buckets[name])
            row["avg_pnl"] = round(sum(buckets[name]) / len(buckets[name]), 2)
            rows.append(row)
    return rows


def net_equity_curve(
    equity_curve: list[dict], enriched: list[dict]
) -> tuple[list[dict], dict[str, int]]:
    """Cumulative gross P&L minus estimated charges, on the same dates as `equity_curve`.

    A trade whose cost is None counts as zero cost; coverage reports how many
    trades had a cost estimate.
    """
    cost_by_day: dict[str, float] = {}
    covered = 0
    for t in enriched:
        if t.get("cost") is not None:
            covered += 1
            cost_by_day[t["trade_date"]] = cost_by_day.get(t["trade_date"], 0.0) + float(t["cost"])
    curve = []
    cum_cost = 0.0
    prev_gross = 0.0
    net = 0.0
    for point in equity_curve:
        cum_cost += cost_by_day.get(point["date"], 0.0)
        gross_day = point["value"] - prev_gross
        prev_gross = point["value"]
        net += gross_day - cost_by_day.get(point["date"], 0.0)
        curve.append({"date": point["date"], "value": round(net, 2)})
    return curve, {"covered": covered, "total": len(enriched)}


def _load_signal_entries(
    start_date: str, end_date: str, db_path: Path | None
) -> dict[tuple, set[tuple[float, str]]]:
    """(strategy, symbol, direction, mode, date) -> {(entry, executed_at), ...}."""
    path = Path(db_path) if db_path else SIGNAL_TRADES_DB
    if not path.exists():
        return {}
    with _engine_for(path).connect() as conn:
        rows = conn.execute(
            text(
                "SELECT strategy, symbol, direction, trade_mode, substr(executed_at, 1, 10), "
                "entry, executed_at FROM trades "
                "WHERE status = 'SUCCESS' AND direction IN ('LONG', 'SHORT') "
                "AND entry IS NOT NULL AND entry > 0 AND executed_at IS NOT NULL "
                "AND substr(executed_at, 1, 10) BETWEEN :s AND :e"
            ),
            {"s": start_date, "e": end_date},
        ).all()
    index: dict[tuple, set[tuple[float, str]]] = {}
    for strategy, symbol, direction, mode, day, entry, executed_at in rows:
        index.setdefault((strategy, symbol, direction, mode, day), set()).add(
            (float(entry), str(executed_at))
        )
    return index


def _match_entry_time(trade: dict, index: dict) -> str | None:
    key = (
        trade["strategy"],
        trade["symbol"],
        trade["direction"],
        trade["mode"],
        trade["trade_date"],
    )
    entry_price = float(trade["entry_price"])
    near = {
        e
        for e in index.get(key, set())
        if entry_price > 0 and abs(e[0] - entry_price) / entry_price <= _PRICE_MATCH_TOLERANCE
    }
    return next(iter(near))[1] if len(near) == 1 else None


def _to_epoch(naive_ist: str) -> int:
    parsed = datetime.fromisoformat(naive_ist.replace(" ", "T")[:19])
    return int(parsed.replace(tzinfo=_IST).timestamp())


def _default_bar_reader(symbol: str, exchange: str, start_ts: int, end_ts: int):
    from database.historify_db import get_ohlcv

    return get_ohlcv(symbol, exchange, "1m", start_ts, end_ts)


# A closed session's excursions never change, and the single-session view polls.
_EXCURSION_CACHE = LockedTTLCache(maxsize=5000, ttl=6 * 3600)


def _trade_excursion(trade: dict, executed_at: str, bar_reader) -> tuple[float, float] | None:
    entry_ts = _to_epoch(executed_at) // 60 * 60  # the entry minute's bar overlaps the entry
    exit_ts = _to_epoch(trade["closed_at"])
    if exit_ts < entry_ts:
        return None
    bars = bar_reader(trade["symbol"], trade["exchange"], entry_ts, exit_ts)
    if bars is None or len(bars) == 0:
        return None
    high, low = float(bars["high"].max()), float(bars["low"].min())
    entry_price, qty = float(trade["entry_price"]), float(trade["closed_quantity"])
    if trade.get("direction") == "SHORT":
        mfe, mae = entry_price - low, high - entry_price
    else:
        mfe, mae = high - entry_price, entry_price - low
    return round(max(mfe, 0.0) * qty, 2), round(max(mae, 0.0) * qty, 2)


def add_excursions(
    trades: list[dict],
    start_date: str,
    end_date: str,
    db_path: Path | None = None,
    bar_reader=None,
) -> tuple[list[dict], int, int]:
    """Copies of `trades` with `mfe` and `mae` in rupees (times closed quantity).

    Upper bound, not exact: the path is every 1-minute Historify bar overlapping
    [entry time, closed_at], where entry time is the trade's own opened_at, or the
    signal executed_at when opened_at is missing, and full-bar highs and lows are used, so
    ticks before the real fill or after the real exit within the edge bars are
    included. LONG: mfe = max high - entry, mae = entry - min low; SHORT mirrored;
    both floored at 0. A trade with no unique signal match, no bars or any error
    gets None for both. Never raises; one exception is logged per call.
    Returns (trades, covered, total).
    """
    reader = bar_reader or _default_bar_reader
    # Only the real reader is cached: an injected one (tests) must see every call.
    cache = _EXCURSION_CACHE if bar_reader is None else None
    today = date.today().isoformat()
    try:
        index = _load_signal_entries(start_date, end_date, db_path)
    except Exception:
        logger.exception("Could not read signal entry times for excursions")
        index = {}
    out, covered, logged = [], 0, False
    for trade in trades:
        mfe = mae = None
        try:
            # The trade's own entry time wins; the signal-db match is the fallback.
            executed_at = trade.get("opened_at") or (
                _match_entry_time(trade, index) if index else None
            )
            if executed_at and trade.get("closed_at"):
                key = (
                    trade["symbol"],
                    trade["exchange"],
                    trade.get("direction"),
                    executed_at,
                    str(trade["closed_at"]),
                    float(trade["entry_price"]),
                    float(trade["closed_quantity"]),
                )
                result = cache.get(key) if cache is not None else None
                if result is None:
                    result = _trade_excursion(trade, executed_at, reader)
                    # A past session's bars are final; today's may still be arriving.
                    if result is not None and cache is not None and trade["trade_date"] < today:
                        cache[key] = result
                if result is not None:
                    mfe, mae = result
                    covered += 1
        except Exception:
            if not logged:
                logger.exception("Could not compute trade excursion")
                logged = True
        out.append({**trade, "mfe": mfe, "mae": mae})
    return out, covered, len(out)


def with_export_fields(trades: list[dict]) -> list[dict]:
    """Copies of enriched `trades` with the derived CSV columns added: `net_pnl`
    (realized - cost, None when cost unknown), `return_pct` (realized over entry
    notional, 4 dp), `sl_price`, `entry_hour` (IST) and `weekday`. The timestamps
    become ISO 8601 with an explicit +05:30 offset."""
    out = []
    for t in trades:
        pnl = float(t["realized_pnl"])
        cost = t.get("cost")
        notional = float(t["entry_price"]) * float(t["closed_quantity"])
        opened, closed = _parse_naive(t.get("opened_at")), _parse_naive(t.get("closed_at"))
        day = _parse_naive(t.get("trade_date"))
        out.append(
            {
                **t,
                "opened_at": opened.isoformat() + "+05:30" if opened else None,
                "closed_at": closed.isoformat() + "+05:30" if closed else None,
                "net_pnl": round(pnl - float(cost), 2) if cost is not None else None,
                "return_pct": round(pnl / notional * 100.0, 4) if notional > 0 else None,
                "sl_price": round(float(t["signal_sl"]), 4)
                if t.get("signal_sl") is not None
                else None,
                "entry_hour": opened.hour if opened else None,
                "weekday": _WEEKDAYS[day.weekday()] if day else None,
            }
        )
    return out
