# services/discretionary_analytics.py
"""
Pure aggregation helpers for the Discretionary Performance page (Stage 3a). No I/O.

Input is a list of closed-trade dicts as ``database/discretionary_book_db.load_trades``
returns them. Every figure is GROSS realized P&L (``realized_pnl``) unless the name says
otherwise; ``charges`` are ESTIMATED and may be missing per trade (``charges is None``,
``charges_status == "unavailable"``).

Buckets are by ``trade_date``, the SESSION date of the close (not the calendar date of
``closed_at``), the same date ``compute_metrics`` uses, so daily buckets reconcile exactly
with ``metrics.daily_pnl`` and every set of buckets sums to ``realized_gross``.

Net of charges
--------------
``net_of_charges = realized_gross - charges`` where an unavailable charge counts as 0, the
SAME basis as the summary's top-level ``net`` / ``realized_net_of_charges``, so buckets add up
to the headline. Because that is then not complete, every row also carries
``charges_unavailable_trades`` and ``net_charges_complete`` (True when it is 0) so the UI can
label the figure as partial.

Win / loss / ratios replicate ``compute_metrics`` (win: pnl > 0, loss: pnl < 0, break-even is
neither; profit factor None when there is a profit and no loss; expectancy =
win_rate * avg_win - (1 - win_rate) * avg_loss; win_rate in percent) so a group over the
whole set equals the headline. These are per-group figures: they are never summed.
"""

from __future__ import annotations

import statistics
from datetime import date, timedelta

GRANULARITIES = ("daily", "weekly", "monthly")
GROUP_KEYS = {"symbol": "symbol", "direction": "direction", "product": "product"}
HOLDINGS = ("intraday", "overnight")
_PLACES = 4


def _r(value, places: int = _PLACES):
    return None if value is None else round(float(value), places)


def _pct(value: float) -> float:
    return round(value * 100, 2)


def _hold_values(trades: list[dict]) -> list[float]:
    out = []
    for t in trades:
        value = t.get("hold_minutes")
        if value is not None:
            out.append(float(value))
    return out


def _hold_stats(values: list[float]) -> dict:
    """mean / median minutes of the trades that have a hold time (None when there is none)."""
    if not values:
        return {"count": 0, "mean_minutes": None, "median_minutes": None}
    return {
        "count": len(values),
        "mean_minutes": _r(sum(values) / len(values), 2),
        "median_minutes": _r(statistics.median(values), 2),
    }


def _money(trades: list[dict]) -> dict:
    gross = sum(float(t["realized_pnl"]) for t in trades)
    charges = sum(float(t["charges"]) for t in trades if t.get("charges") is not None)
    unavailable = sum(1 for t in trades if t.get("charges") is None)
    return {
        "realized_gross": _r(gross),
        "charges": _r(charges),
        "charges_unavailable_trades": unavailable,
        "net_of_charges": _r(gross - charges),
        "net_charges_complete": unavailable == 0,
    }


def _stats(trades: list[dict]) -> dict:
    """Counts, money and the compute_metrics-style ratios of one set of trades."""
    pnls = [float(t["realized_pnl"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = len(pnls)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    win_rate = (len(wins) / n) if n else 0.0
    avg_win = (gross_profit / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0
    if gross_loss > 0:
        profit_factor = round(gross_profit / gross_loss, 2)
    else:
        profit_factor = None if gross_profit > 0 else 0.0
    hold = _hold_stats(_hold_values(trades))
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": _pct(win_rate),
        "profit_factor": profit_factor,
        "expectancy": round(win_rate * avg_win - (1 - win_rate) * avg_loss, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        **_money(trades),
        "avg_hold_minutes": hold["mean_minutes"],
        "median_hold_minutes": hold["median_minutes"],
    }


# ---------------------------------------------------------------- period buckets


def _bucket(trade_date: str, granularity: str) -> tuple[str, str, str, str]:
    """(key, label, start, end) of the bucket ``trade_date`` falls in; ISO weeks start Monday."""
    day = date.fromisoformat(str(trade_date)[:10])
    if granularity == "daily":
        iso = day.isoformat()
        return iso, iso, iso, iso
    if granularity == "weekly":
        monday = day - timedelta(days=day.weekday())
        sunday = monday + timedelta(days=6)
        year, week, _ = day.isocalendar()
        return monday.isoformat(), f"{year}-W{week:02d}", monday.isoformat(), sunday.isoformat()
    if granularity == "monthly":
        first = day.replace(day=1)
        nxt = (first + timedelta(days=32)).replace(day=1)
        last = nxt - timedelta(days=1)
        key = f"{day.year}-{day.month:02d}"
        return key, key, first.isoformat(), last.isoformat()
    raise ValueError(f"granularity must be one of {', '.join(GRANULARITIES)}")


def period_breakdown(trades: list[dict], granularity: str) -> list[dict]:
    """One row per period with at least one trade, oldest first.

    Fields: ``period`` (daily: the date; weekly: the Monday date; monthly: ``YYYY-MM``),
    ``label`` (weekly: ISO ``YYYY-Www``), ``start``, ``end`` (inclusive calendar bounds),
    ``trades``, ``wins``, ``losses``, ``realized_gross``, ``charges``,
    ``charges_unavailable_trades``, ``net_of_charges``, ``net_charges_complete``.
    """
    if granularity not in GRANULARITIES:
        raise ValueError(f"granularity must be one of {', '.join(GRANULARITIES)}")
    buckets: dict[str, dict] = {}
    for t in trades:
        key, label, start, end = _bucket(t["trade_date"], granularity)
        row = buckets.setdefault(key, {"label": label, "start": start, "end": end, "_t": []})
        row["_t"].append(t)
    out = []
    for key in sorted(buckets):
        row = buckets[key]
        items = row["_t"]
        pnls = [float(t["realized_pnl"]) for t in items]
        out.append(
            {
                "period": key,
                "label": row["label"],
                "start": row["start"],
                "end": row["end"],
                "trades": len(items),
                "wins": sum(1 for p in pnls if p > 0),
                "losses": sum(1 for p in pnls if p < 0),
                **_money(items),
            }
        )
    return out


# ---------------------------------------------------------------- groups


def group_breakdown(trades: list[dict], key: str) -> list[dict]:
    """Per-group stats for ``key`` in symbol / direction / product, best realized_gross first
    (ties by group name). Each row: ``key`` (the group value) plus trades, wins, losses,
    win_rate, profit_factor, expectancy, avg_win, avg_loss, realized_gross, charges,
    charges_unavailable_trades, net_of_charges, net_charges_complete, avg_hold_minutes,
    median_hold_minutes."""
    if key not in GROUP_KEYS:
        raise ValueError(f"key must be one of {', '.join(GROUP_KEYS)}")
    groups: dict[str, list[dict]] = {}
    for t in trades:
        groups.setdefault(str(t.get(key) or ""), []).append(t)
    rows = [{"key": name, **_stats(items)} for name, items in groups.items()]
    rows.sort(key=lambda r: (-r["realized_gross"], r["key"]))
    return rows


def holding_breakdown(trades: list[dict]) -> dict:
    """{"intraday": stats, "overnight": stats}; both keys always present. A trade is overnight
    when its ``overnight`` flag is true (opened in an earlier session than it closed)."""
    parts = {name: [] for name in HOLDINGS}
    for t in trades:
        parts["overnight" if t.get("overnight") else "intraday"].append(t)
    return {name: _stats(items) for name, items in parts.items()}


def hold_summary(trades: list[dict]) -> dict:
    """Hold time in minutes: {"overall", "winners", "losers"}, each {count, mean_minutes,
    median_minutes}. Trades without a hold time are skipped (count says how many were used);
    winners are pnl > 0, losers pnl < 0, break-even only counts in overall."""
    winners = [t for t in trades if float(t["realized_pnl"]) > 0]
    losers = [t for t in trades if float(t["realized_pnl"]) < 0]
    return {
        "overall": _hold_stats(_hold_values(trades)),
        "winners": _hold_stats(_hold_values(winners)),
        "losers": _hold_stats(_hold_values(losers)),
    }
