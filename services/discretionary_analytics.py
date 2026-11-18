# services/discretionary_analytics.py
"""
Pure aggregation helpers for the Discretionary Performance page (Stage 3a). No I/O.

Two inputs
----------
* ``trades``: COMPLETED trades, one per position from flat to flat (``group_completed``
  builds them from the closing legs and the FLAT positions of one build). Trade counts,
  wins / losses, every ratio and every hold-time figure come from these only.
* ``legs``: the closing legs as ``database/discretionary_book_db.load_trades`` returns them
  (one row per closing fill, partial exits included). Money (``realized_gross``,
  ``charges``, ``net_of_charges``, ``charges_unavailable_trades``) comes from these, so a
  partial exit counts on its own day even while the position is still open. When ``legs``
  is omitted the money comes from ``trades`` themselves (a completed trade carries the sum
  of its legs).

Every figure is GROSS realized P&L (``realized_pnl``) unless the name says otherwise;
``charges`` are ESTIMATED and may be missing (``charges is None``,
``charges_status == "unavailable"``); ``charges_unavailable_trades`` counts the closing
LEGS without an estimate (it qualifies a money figure, so it follows the money rows).

Buckets are by ``trade_date``, the SESSION date of the close (not the calendar date of
``closed_at``), the same date ``compute_metrics`` uses. A leg's money goes to its own
close date; a completed trade is counted on the date of its FINAL close. Daily buckets
therefore reconcile with ``metrics.daily_pnl`` and every set of buckets sums to
``realized_gross``; the bucket trade counts sum to ``metrics.trades_count``. A bucket exists
when it has a leg or a completed trade, so a day with only a partial exit shows its money
and 0 trades. Group rows (symbol / side / product / holding) are split the same way; the
holding bucket of a leg's money is the leg's own overnight flag, the holding bucket of a
completed trade is the flag of its final close (opened in an earlier session than it closed).

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


def _stats(trades: list[dict], legs: list[dict] | None = None) -> dict:
    """Counts, ratios and hold times of the completed ``trades``; money of ``legs`` (the
    same trades when omitted)."""
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
        **_money(trades if legs is None else legs),
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


def period_breakdown(
    trades: list[dict], granularity: str, legs: list[dict] | None = None
) -> list[dict]:
    """One row per period with at least one completed trade or closing leg, oldest first.

    Fields: ``period`` (daily: the date; weekly: the Monday date; monthly: ``YYYY-MM``),
    ``label`` (weekly: ISO ``YYYY-Www``), ``start``, ``end`` (inclusive calendar bounds),
    ``trades``, ``wins``, ``losses`` (completed trades whose FINAL close is in the period),
    ``realized_gross``, ``charges``, ``charges_unavailable_trades``, ``net_of_charges``,
    ``net_charges_complete`` (the legs closed in the period; ``trades`` when ``legs`` is None).
    """
    if granularity not in GRANULARITIES:
        raise ValueError(f"granularity must be one of {', '.join(GRANULARITIES)}")
    money_rows = trades if legs is None else legs
    buckets: dict[str, dict] = {}
    for kind, items in (("_t", trades), ("_m", money_rows)):
        for t in items:
            key, label, start, end = _bucket(t["trade_date"], granularity)
            row = buckets.setdefault(
                key, {"label": label, "start": start, "end": end, "_t": [], "_m": []}
            )
            row[kind].append(t)
    out = []
    for key in sorted(buckets):
        row = buckets[key]
        pnls = [float(t["realized_pnl"]) for t in row["_t"]]
        out.append(
            {
                "period": key,
                "label": row["label"],
                "start": row["start"],
                "end": row["end"],
                "trades": len(pnls),
                "wins": sum(1 for p in pnls if p > 0),
                "losses": sum(1 for p in pnls if p < 0),
                **_money(row["_m"]),
            }
        )
    return out


# ---------------------------------------------------------------- groups


def _split(rows: list[dict], name_of) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for t in rows:
        groups.setdefault(name_of(t), []).append(t)
    return groups


def group_breakdown(trades: list[dict], key: str, legs: list[dict] | None = None) -> list[dict]:
    """Per-group stats for ``key`` in symbol / direction / product, best realized_gross first
    (ties by group name). Each row: ``key`` (the group value) plus trades, wins, losses,
    win_rate, profit_factor, expectancy, avg_win, avg_loss, avg_hold_minutes,
    median_hold_minutes (completed ``trades``) and realized_gross, charges,
    charges_unavailable_trades, net_of_charges, net_charges_complete (``legs``; the trades
    when None). A group with legs but no completed trade has trades 0."""
    if key not in GROUP_KEYS:
        raise ValueError(f"key must be one of {', '.join(GROUP_KEYS)}")

    def name_of(t):
        return str(t.get(key) or "")

    done = _split(trades, name_of)
    money = done if legs is None else _split(legs, name_of)
    rows = [
        {"key": name, **_stats(done.get(name, []), money.get(name, []))}
        for name in set(done) | set(money)
    ]
    rows.sort(key=lambda r: (-r["realized_gross"], r["key"]))
    return rows


def holding_breakdown(trades: list[dict], legs: list[dict] | None = None) -> dict:
    """{"intraday": stats, "overnight": stats}; both keys always present. A row is overnight
    when its ``overnight`` flag is true (opened in an earlier session than it closed): for a
    completed trade the flag of its final close, for a leg (money) the leg's own flag."""

    def name_of(t):
        return "overnight" if t.get("overnight") else "intraday"

    done = _split(trades, name_of)
    money = done if legs is None else _split(legs, name_of)
    return {name: _stats(done.get(name, []), money.get(name, [])) for name in HOLDINGS}


def hold_summary(trades: list[dict]) -> dict:
    """Hold time in minutes of the COMPLETED trades: {"overall", "winners", "losers"}, each
    {count, mean_minutes, median_minutes}. Trades without a hold time are skipped (count says
    how many were used); winners are pnl > 0, losers pnl < 0, break-even only counts in
    overall."""
    winners = [t for t in trades if float(t["realized_pnl"]) > 0]
    losers = [t for t in trades if float(t["realized_pnl"]) < 0]
    return {
        "overall": _hold_stats(_hold_values(trades)),
        "winners": _hold_stats(_hold_values(winners)),
        "losers": _hold_stats(_hold_values(losers)),
    }


# ---------------------------------------------------------------- completed trades


def _leg_key(row: dict) -> tuple:
    return (
        row["broker"],
        row["account"],
        row["exchange"],
        row["symbol"],
        row["product"],
        int(row["position_seq"]),
    )


def group_completed(legs: list[dict], flat_positions: list[dict]) -> list[dict]:
    """One completed trade per FLAT position (flat to flat; a reversal ends it), built at read
    time from the closing ``legs`` sharing its identity and ``position_seq`` (one build).

    ``id`` is the position row id (stable within the build). ``closed_quantity`` is the total
    closed, ``entry_price`` / ``exit_price`` the quantity-weighted averages of the legs,
    ``realized_pnl`` / ``charges`` the sums (``charges`` None and ``charges_status``
    "unavailable" when any leg has no estimate; ``net_pnl`` None then), ``opened_at`` the
    position's opening, and ``closed_at``, ``trade_date``, ``hold_minutes`` and
    ``overnight`` those of the FINAL leg (every leg is measured from the position's
    ``opened_at``). ``legs`` is the leg count. Ordered by (closed_at, id).
    """
    by_key: dict[tuple, list[dict]] = {}
    for leg in legs:
        by_key.setdefault(_leg_key(leg), []).append(leg)
    out = []
    for pos in flat_positions:
        if pos.get("status") != "FLAT":
            continue
        items = sorted(by_key.get(_leg_key(pos), []), key=lambda t: (t["closed_at"], t["id"]))
        if not items:
            continue
        qty = sum(float(t["closed_quantity"]) for t in items)
        pnl = sum(float(t["realized_pnl"]) for t in items)
        unavailable = any(t.get("charges") is None for t in items)
        charges = None if unavailable else sum(float(t["charges"]) for t in items)
        breakdown: dict = {}
        if not unavailable:
            for t in items:
                for name, value in (t.get("charges_breakdown") or {}).items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        breakdown[name] = breakdown.get(name, 0.0) + float(value)
        last = items[-1]
        out.append(
            {
                "id": pos["id"],
                "build_id": pos.get("build_id"),
                "broker": pos["broker"],
                "account": pos["account"],
                "exchange": pos["exchange"],
                "symbol": pos["symbol"],
                "product": pos["product"],
                "position_seq": int(pos["position_seq"]),
                "direction": pos["direction"],
                "closed_quantity": qty,
                "entry_price": sum(
                    float(t["closed_quantity"]) * float(t["entry_price"]) for t in items
                )
                / qty,
                "exit_price": sum(
                    float(t["closed_quantity"]) * float(t["exit_price"]) for t in items
                )
                / qty,
                "opened_at": pos["opened_at"],
                "closed_at": last["closed_at"],
                "trade_date": last["trade_date"],
                "overnight": bool(last["overnight"]),
                "hold_minutes": last["hold_minutes"],
                "realized_pnl": pnl,
                "charges": charges,
                "charges_status": "unavailable" if unavailable else "ok",
                "net_pnl": None if charges is None else pnl - charges,
                "charges_breakdown": breakdown,
                "legs": len(items),
                "leg_ids": [t["id"] for t in items],
            }
        )
    out.sort(key=lambda t: (t["closed_at"], t["id"]))
    return out
