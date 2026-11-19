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

"How you trade" (``trading_breakdowns``) reuses the same row builder over entry time of day,
entry weekday and holding time (bucket definitions at the constants below); ``consistency``
is the risk strip (largest win / loss, streaks with break-even ending a streak, daily
figures and drawdown only from 2 trading days).
"""

from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta

from services.discretionary_reconstruction import session_date

GRANULARITIES = ("daily", "weekly", "monthly")
GROUP_KEYS = {"symbol": "symbol", "direction": "direction", "product": "product"}
HOLDINGS = ("intraday", "overnight")
_PLACES = 4

# Time-of-day buckets by the ENTRY time (``opened_at``, naive IST) of the position, NSE cash
# oriented. Each bucket is [start, end): 09:15:00 is in "09:15-10:00", 10:00:00 in
# "10:00-11:30", 15:29:59 in "15:00-15:30". Anything before 09:15 or at/after 15:30 (MCX
# evening, pre-open, a missing time) is "other".
TIME_OF_DAY_BUCKETS = (
    ("09:15-10:00", (9, 15), (10, 0)),
    ("10:00-11:30", (10, 0), (11, 30)),
    ("11:30-13:30", (11, 30), (13, 30)),
    ("13:30-15:00", (13, 30), (15, 0)),
    ("15:00-15:30", (15, 0), (15, 30)),
)
TIME_OTHER = "other"
# Weekday of the ENTRY session date (session_date(opened_at)); a weekend or holiday session
# (Budget Saturday, Muhurat) is listed under its own weekday.
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# Holding-time buckets: "overnight" when the row's ``overnight`` flag is set (opened in an
# earlier session than it closed), otherwise by ``hold_minutes`` in [lower, upper):
# <5 min, 5-15 min, 15-60 min, 1-4 h, over 4 h the same session; "unknown" without a hold time.
HOLD_BUCKETS = ("<5m", "5-15m", "15-60m", "1-4h", ">4h same day", "overnight", "unknown")
_HOLD_LIMITS = ((5.0, "<5m"), (15.0, "5-15m"), (60.0, "15-60m"), (240.0, "1-4h"))


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


# ---------------------------------------------------------------- how you trade (buckets)


def _as_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("T", " "))
        except ValueError:
            return None
    return None


def time_of_day_bucket(opened_at) -> str:
    """The TIME_OF_DAY_BUCKETS label of an entry time ([start, end)), else TIME_OTHER."""
    moment = _as_datetime(opened_at)
    if moment is None:
        return TIME_OTHER
    hm = (moment.hour, moment.minute)
    for label, start, end in TIME_OF_DAY_BUCKETS:
        if start <= hm < end:
            return label
    return TIME_OTHER


def weekday_bucket(opened_at) -> str:
    """Weekday ("Mon".."Sun") of the entry SESSION date; "unknown" without a time."""
    moment = _as_datetime(opened_at)
    if moment is None:
        return "unknown"
    return WEEKDAYS[session_date(moment).weekday()]


def hold_bucket(row: dict) -> str:
    """HOLD_BUCKETS label of a completed trade or a leg (its own overnight flag / hold time)."""
    if row.get("overnight"):
        return "overnight"
    minutes = row.get("hold_minutes")
    if minutes is None:
        return "unknown"
    minutes = float(minutes)
    for upper, label in _HOLD_LIMITS:
        if minutes < upper:
            return label
    return ">4h same day"


def summary_stats(trades: list[dict], legs: list[dict] | None = None) -> dict:
    """The per-group row figures (``_stats``) of a whole set: counts and ratios of the
    completed ``trades``, money of ``legs`` (the trades when None)."""
    return _stats(trades, legs)


def bucket_breakdown(trades: list[dict], legs: list[dict] | None, name_of, order) -> list[dict]:
    """Rows of the SAME row builder as ``group_breakdown`` for an arbitrary bucketing
    ``name_of(row)``, in the fixed ``order`` (unknown names after it, sorted). Counts / ratios
    from the completed ``trades``, money from ``legs`` (each leg in its own bucket), plus
    ``trading_days``: distinct trade_date of the bucket's completed trades. Only buckets with
    a trade or a leg are returned."""
    done = _split(trades, name_of)
    money = done if legs is None else _split(legs, name_of)
    names = set(done) | set(money)
    ordered = [n for n in order if n in names] + sorted(n for n in names if n not in order)
    rows = []
    for name in ordered:
        row = {"key": name, **_stats(done.get(name, []), money.get(name, []))}
        row["trading_days"] = len({str(t["trade_date"]) for t in done.get(name, [])})
        rows.append(row)
    return rows


def trading_breakdowns(trades: list[dict], legs: list[dict] | None = None) -> dict:
    """{"time_of_day", "weekday", "hold_bucket"}: ``bucket_breakdown`` rows. Time of day and
    weekday bucket by the ENTRY (``opened_at`` of the position, the same for every leg of
    it); holding time by each row's own hold (a completed trade: its final close; a leg: its
    own close), the same split ``holding_breakdown`` uses for its overnight flag."""
    return {
        "time_of_day": bucket_breakdown(
            trades,
            legs,
            lambda t: time_of_day_bucket(t.get("opened_at")),
            [b[0] for b in TIME_OF_DAY_BUCKETS] + [TIME_OTHER],
        ),
        "weekday": bucket_breakdown(
            trades, legs, lambda t: weekday_bucket(t.get("opened_at")), list(WEEKDAYS)
        ),
        "hold_bucket": bucket_breakdown(trades, legs, hold_bucket, list(HOLD_BUCKETS)),
    }


# ---------------------------------------------------------------- risk and consistency


def trade_streaks(trades: list[dict]) -> tuple[int, int]:
    """(max consecutive wins, max consecutive losses) over the completed ``trades`` ordered by
    (closed_at, id). A win is pnl > 0, a loss pnl < 0; a break-even trade (pnl == 0) ends
    both streaks (it is neither, so the run is no longer consecutive)."""
    best_win = best_loss = cur_win = cur_loss = 0
    for t in sorted(trades, key=lambda t: (str(t["closed_at"]), t["id"])):
        pnl = float(t["realized_pnl"])
        if pnl > 0:
            cur_win, cur_loss = cur_win + 1, 0
        elif pnl < 0:
            cur_win, cur_loss = 0, cur_loss + 1
        else:
            cur_win = cur_loss = 0
        best_win = max(best_win, cur_win)
        best_loss = max(best_loss, cur_loss)
    return best_win, best_loss


def _trade_ref(t: dict) -> dict:
    return {
        "id": t["id"],
        "build_id": t.get("build_id"),
        "broker": t["broker"],
        "account": t["account"],
        "symbol": t["symbol"],
        "exchange": t["exchange"],
        "product": t["product"],
        "direction": t["direction"],
        "realized_pnl": _r(t["realized_pnl"]),
        "charges": _r(t.get("charges")),
        "charges_status": t.get("charges_status"),
        "net_pnl": _r(t.get("net_pnl")),
        "closed_at": t["closed_at"],
        "trade_date": t["trade_date"],
    }


MIN_DAYS_FOR_DAILY = 2
UNAVAILABLE_FEW_DAYS = f"needs at least {MIN_DAYS_FOR_DAILY} trading days"


def max_drawdown_from_zero(daily_rows: list[dict]) -> float:
    """Max drawdown (<= 0) of the cumulative GROSS realized daily P&L, with the equity peak
    starting at 0 (before the first day), so a loss on the first trading day is a drawdown
    (days -100 then +50 give -100). Daily rows come from ``period_breakdown(..., "daily")``
    (``realized_gross`` by close date, legs). Excludes charges and unrealized. Used for the
    discretionary headline ``metrics.max_drawdown`` and ``consistency.max_drawdown``;
    ``compute_metrics`` (shared with Live / Paper) is not touched."""
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for row in sorted(daily_rows, key=lambda r: str(r["period"])):
        equity += float(row["realized_gross"])
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return round(worst, 2)


def consistency(
    trades: list[dict], daily_rows: list[dict], hold: dict, max_drawdown: float | None = None
) -> dict:
    """Risk and consistency strip, all GROSS realized (before charges) unless named net.

    * largest_win / largest_loss: the completed trade with the highest pnl > 0 / lowest
      pnl < 0 (ties: the earliest by (closed_at, id)); None when there is none.
    * max_consecutive_wins / _losses: ``trade_streaks`` (break-even ends a streak).
    * trading_days: daily rows (``period_breakdown(..., "daily")``: days with a closing leg or
      a completed trade); profitable / losing / flat days by the row's realized_gross.
    * avg_daily_pnl (gross) and avg_daily_net (net of estimated charges, unavailable charges
      as 0, ``avg_daily_net_complete`` False then): mean over those trading days; None with
      a reason below MIN_DAYS_FOR_DAILY days.
    * max_drawdown: ``max_drawdown_from_zero`` of the daily rows (equity peak starts at 0;
      gross realized by close date, excludes charges and unrealized); the ``max_drawdown``
      argument is ignored (kept for callers); None with a reason below MIN_DAYS_FOR_DAILY days (one day has no
      peak to fall from).
    * avg / median hold: ``hold_summary`` overall (completed trades).
    """
    wins = [t for t in trades if float(t["realized_pnl"]) > 0]
    losses = [t for t in trades if float(t["realized_pnl"]) < 0]

    def when(t):
        return (str(t["closed_at"]), t["id"])

    largest_win = min(wins, key=lambda t: (-float(t["realized_pnl"]), *when(t))) if wins else None
    largest_loss = (
        min(losses, key=lambda t: (float(t["realized_pnl"]), *when(t))) if losses else None
    )
    streak_win, streak_loss = trade_streaks(trades)
    days = len(daily_rows)
    gross_days = [float(r["realized_gross"]) for r in daily_rows]
    enough = days >= MIN_DAYS_FOR_DAILY
    overall = (hold or {}).get("overall") or {}
    return {
        "basis": "gross realized P&L (before charges) unless the name says net",
        "completed_trades": len(trades),
        "largest_win": _trade_ref(largest_win) if largest_win else None,
        "largest_loss": _trade_ref(largest_loss) if largest_loss else None,
        "max_consecutive_wins": streak_win,
        "max_consecutive_losses": streak_loss,
        "streak_rule": (
            "completed trades ordered by close time (ties by id); a break-even trade ends "
            "both streaks"
        ),
        "trading_days": days,
        "profitable_days": sum(1 for v in gross_days if v > 0),
        "losing_days": sum(1 for v in gross_days if v < 0),
        "flat_days": sum(1 for v in gross_days if v == 0),
        "avg_daily_pnl": _r(sum(gross_days) / days, 2) if enough else None,
        "avg_daily_net": (
            _r(sum(float(r["net_of_charges"]) for r in daily_rows) / days, 2) if enough else None
        ),
        "avg_daily_net_complete": all(r.get("net_charges_complete", True) for r in daily_rows),
        "avg_daily_unavailable_reason": None if enough else UNAVAILABLE_FEW_DAYS,
        "max_drawdown": max_drawdown_from_zero(daily_rows) if enough else None,
        "max_drawdown_unavailable_reason": None if enough else UNAVAILABLE_FEW_DAYS,
        "drawdown_basis": (
            "gross realized P&L by close date, equity peak starts at 0; "
            "excludes charges and unrealized"
        ),
        "avg_hold_minutes": overall.get("mean_minutes"),
        "median_hold_minutes": overall.get("median_minutes"),
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
