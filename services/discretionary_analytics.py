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

Win / loss basis (NET): a completed position is a winner when its NET P&L after estimated
charges is > 0 and a loser when < 0 (break-even is neither). Win rate, profit factor,
expectancy, average win / loss, streaks, largest win / loss and the winner / loser hold times
all use that basis. A position whose net is unavailable (a leg without a charge estimate,
``net_pnl`` None) is NEVER measured on gross: it is left out of those statistics and counted
in ``net_unavailable_trades`` (its gross and its charges figures stay in every money total).

Money figures are GROSS realized P&L (``realized_pnl``) unless the name says otherwise;
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

Win / loss / ratios replicate ``compute_metrics`` on the NET basis (win: net > 0, loss:
net < 0, break-even is neither; profit factor None when there is a profit and no loss;
expectancy = win_rate * avg_win - (1 - win_rate) * avg_loss; win_rate in percent of the
positions with a net) so a group over the whole set equals the headline. These are per-group
figures: they are never summed.

"How you trade" (``trading_breakdowns``) reuses the same row builder over entry time of day,
entry weekday and holding time (bucket definitions at the constants below); ``consistency``
is the risk strip (largest win / loss, streaks with break-even ending a streak, daily
figures and drawdown only from 2 trading days).
"""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Callable
from dataclasses import dataclass
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


# Money below half a paise is float noise (0.1 + 0.2 - 0.3 = 5.55e-17), never a result.
_PAISE_EPS = 0.005


def clean_money(value):
    """The ONE float-noise guard for money: an amount whose absolute value is below half a
    paise is exactly 0.0 (break-even), anything else is returned unchanged. None stays None.
    Every win / loss / break-even decision runs on a value passed through this."""
    if value is None:
        return None
    value = float(value)
    return 0.0 if abs(value) < _PAISE_EPS else value


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


def net_value(t: dict) -> float | None:
    """NET P&L of a position or leg row: ``net_pnl`` when present, else realized minus the
    charges when those are known, else None. Never falls back to gross."""
    if "net_pnl" in t:
        value = t["net_pnl"]
    elif t.get("charges") is not None:
        value = float(t["realized_pnl"]) - float(t["charges"])
    else:
        value = None
    if value is None:
        return None
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("net P&L must be finite")
    return clean_money(value)


def _net_pnls(trades: list[dict]) -> list[float]:
    """The net P&L of the trades that have one (the statistics basis)."""
    return [v for v in (net_value(t) for t in trades) if v is not None]


def _stats(trades: list[dict], legs: list[dict] | None = None) -> dict:
    """Counts, ratios and hold times of the completed ``trades`` on the NET basis; money of
    ``legs`` (the same trades when omitted).

    ``trades`` counts every completed position; ``net_basis_trades`` those with a net P&L
    (the denominator of ``win_rate``); ``net_unavailable_trades`` the rest."""
    pnls = _net_pnls(trades)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = len(pnls)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    hold = _hold_stats(_hold_values(trades))
    if n == 0:
        # No position has a net P&L: the ratios are unknown, not 0 (never measured on gross).
        no_data = {
            "win_rate": None,
            "profit_factor": None,
            "expectancy": None,
            "avg_win": None,
            "avg_loss": None,
            "stats_unavailable_reason": "no positions with a net P&L",
        }
        return {
            "trades": len(trades),
            "net_basis_trades": 0,
            "net_unavailable_trades": len(trades),
            "wins": 0,
            "losses": 0,
            **no_data,
            **_money(trades if legs is None else legs),
            "avg_hold_minutes": hold["mean_minutes"],
            "median_hold_minutes": hold["median_minutes"],
        }
    win_rate = len(wins) / n
    avg_win = (gross_profit / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0
    if gross_loss > 0:
        profit_factor = round(gross_profit / gross_loss, 2)
    else:
        profit_factor = None if gross_profit > 0 else 0.0
    return {
        "trades": len(trades),
        "net_basis_trades": n,
        "net_unavailable_trades": len(trades) - n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": _pct(win_rate),
        "profit_factor": profit_factor,
        "expectancy": round(win_rate * avg_win - (1 - win_rate) * avg_loss, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "stats_unavailable_reason": None,
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
    ``trades``, ``wins``, ``losses`` (completed trades whose FINAL close is in the period; net
    basis, a trade without a net is neither),
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
        pnls = _net_pnls(row["_t"])
        out.append(
            {
                "period": key,
                "label": row["label"],
                "start": row["start"],
                "end": row["end"],
                "trades": len(row["_t"]),
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
    how many were used); winners are NET pnl > 0, losers NET pnl < 0, break-even and a
    trade without a net only count in overall."""
    winners = [t for t in trades if (net_value(t) or 0.0) > 0]
    losers = [t for t in trades if (net_value(t) or 0.0) < 0]
    return {
        "overall": _hold_stats(_hold_values(trades)),
        "winners": _hold_stats(_hold_values(winners)),
        "losers": _hold_stats(_hold_values(losers)),
    }


# ---------------------------------------------------------------- how you trade (buckets)


def parse_time(value) -> datetime | None:
    """A naive IST datetime from a datetime or an ISO string ("YYYY-MM-DD HH:MM:SS" or with
    "T"); None when missing or unparsable. The book returns its times as ISO strings."""
    return _as_datetime(value)


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
    (closed_at, id). A win is NET pnl > 0, a loss NET pnl < 0; a break-even trade (net == 0)
    ends both streaks (it is neither, so the run is no longer consecutive). A trade without
    a net is skipped: it neither extends nor ends a streak."""
    best_win = best_loss = cur_win = cur_loss = 0
    for t in sorted(trades, key=lambda t: (str(t["closed_at"]), t["id"])):
        pnl = net_value(t)
        if pnl is None:
            continue
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
    """Risk and consistency strip; trade-level items (largest win / loss, streaks) are on the
    NET basis, daily figures and the legacy drawdown GROSS realized (before charges) unless
    named net.

    * largest_win / largest_loss: the completed trade with the highest NET pnl > 0 / lowest
      NET pnl < 0 (ties: the earliest by (closed_at, id)); None when there is none.
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
    wins = [t for t in trades if (net_value(t) or 0.0) > 0]
    losses = [t for t in trades if (net_value(t) or 0.0) < 0]

    def when(t):
        return (str(t["closed_at"]), t["id"])

    largest_win = min(wins, key=lambda t: (-net_value(t), *when(t))) if wins else None
    largest_loss = min(losses, key=lambda t: (net_value(t), *when(t))) if losses else None
    streak_win, streak_loss = trade_streaks(trades)
    days = len(daily_rows)
    gross_days = [float(r["realized_gross"]) for r in daily_rows]
    enough = days >= MIN_DAYS_FOR_DAILY
    overall = (hold or {}).get("overall") or {}
    out = {
        "basis": (
            "trade-level items (largest win / loss, streaks) are net of estimated charges; "
            "daily figures and drawdown are gross realized P&L (before charges) unless the "
            "name says net"
        ),
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
    # Additive keys below: each is ABSENT (not null) when it cannot be computed.
    for name, value in (
        ("net_days", net_day_stats(daily_rows)),
        ("trades_per_day", trades_per_day(trades)),
        ("reentry_after_loss", reentry_after_loss(trades)),
        ("quantity_by_symbol", quantity_by_symbol(trades)),
    ):
        if value:
            out[name] = value
    return out


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


def position_key(row: dict) -> tuple:
    """(broker, account, exchange, symbol, product, position_seq): the identity of a position
    within one build, shared by a position row, a completed trade and every one of its legs."""
    return _leg_key(row)


def orphan_fill_counts(orphans: list[dict] | None, rows: list[dict]) -> dict[tuple, int]:
    """{position_key: number of orphan fills} for ``rows`` (anything with an identity,
    ``opened_at`` and ``closed_at``). An orphan counts for a position when it belongs to the
    same identity (broker, account, exchange, symbol, product) and its fill_time lies in
    [opened_at, closed_at] (no ``closed_at``: from opened_at on)."""
    by_ident: dict[tuple, list[datetime]] = {}
    for o in orphans or []:
        moment = _as_datetime(o.get("fill_time"))
        if moment is None:
            continue
        ident = (o.get("broker"), o.get("account"), o.get("exchange"), o.get("symbol"))
        by_ident.setdefault((*ident, o.get("product")), []).append(moment)
    out: dict[tuple, int] = {}
    for row in rows:
        ident = (row["broker"], row["account"], row["exchange"], row["symbol"], row["product"])
        opened = _as_datetime(row.get("opened_at"))
        closed = _as_datetime(row.get("closed_at"))
        n = 0
        for moment in by_ident.get(ident, []):
            if opened is not None and moment < opened:
                continue
            if closed is not None and moment > closed:
                continue
            n += 1
        out[position_key(row)] = n
    return out


def position_points(entry_price, exit_price, direction) -> float:
    """Points per UNIT of the instrument (never lot-scaled) of a closed position from its
    quantity-weighted entry and exit prices: LONG exit - entry, SHORT entry - exit. Equals
    ``realized_pnl / closed_quantity``."""
    entry, exit_ = float(entry_price), float(exit_price)
    if not (math.isfinite(entry) and math.isfinite(exit_)):
        raise ValueError("prices must be finite")
    return exit_ - entry if str(direction).upper() == "LONG" else entry - exit_


def group_completed(
    legs: list[dict], flat_positions: list[dict], orphans: list[dict] | None = None
) -> list[dict]:
    """One completed trade per FLAT position (flat to flat; a reversal ends it), built at read
    time from the closing ``legs`` sharing its identity and ``position_seq`` (one build).

    ``id`` is the position row id (stable within the build). ``closed_quantity`` is the total
    closed, ``entry_price`` / ``exit_price`` the quantity-weighted averages of the legs,
    ``realized_pnl`` / ``charges`` the sums (``charges`` None and ``charges_status``
    "unavailable" when any leg has no estimate; ``net_pnl`` None then), ``opened_at`` the
    position's opening, and ``closed_at``, ``trade_date``, ``hold_minutes`` and
    ``overnight`` those of the FINAL leg (every leg is measured from the position's
    ``opened_at``). ``legs`` is the leg count. Ordered by (closed_at, id).

    Reliability fields carried from the position: ``opening_unknown`` (the identity had a fill
    whose opening is not in the ledger), ``orphan_fills`` / ``has_orphan_fills`` (orphan
    fills of the same identity between the position's opening and final close, counted from
    ``orphans`` when given, else 0) and ``points_per_unit`` (``position_points``).
    """
    by_key: dict[tuple, list[dict]] = {}
    for leg in legs:
        by_key.setdefault(_leg_key(leg), []).append(leg)
    out = []
    orphan_counts: dict[tuple, int] = {}
    if orphans:
        spans = []
        for pos in flat_positions:
            items = by_key.get(_leg_key(pos), [])
            if pos.get("status") == "FLAT" and items:
                last = max(items, key=lambda t: (t["closed_at"], t["id"]))
                spans.append({**pos, "closed_at": last["closed_at"]})
        orphan_counts = orphan_fill_counts(orphans, spans)
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
                "net_pnl": None if charges is None else clean_money(pnl - charges),
                "charges_breakdown": breakdown,
                "legs": len(items),
                "leg_ids": [t["id"] for t in items],
                "opening_unknown": bool(pos.get("opening_unknown")),
                "orphan_fills": orphan_counts.get(_leg_key(pos), 0),
                "has_orphan_fills": orphan_counts.get(_leg_key(pos), 0) > 0,
                "points_per_unit": position_points(
                    sum(float(t["closed_quantity"]) * float(t["entry_price"]) for t in items) / qty,
                    sum(float(t["closed_quantity"]) * float(t["exit_price"]) for t in items) / qty,
                    pos["direction"],
                ),
            }
        )
    out.sort(key=lambda t: (t["closed_at"], t["id"]))
    return out


# ---------------------------------------------------------------- points per instrument

CATEGORIES = ("intraday", "swing", "unclassified")
CATEGORY_FILTERS = ("all", "intraday", "swing")


def _num(value, name: str = "value") -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def points_by_instrument(trades: list[dict]) -> list[dict]:
    """Points metrics PER INSTRUMENT (symbol + exchange) of the closed positions; points are
    never summed across instruments.

    Points of a position = per UNIT (not lot-scaled) from the quantity-weighted entry and
    exit prices: LONG exit - entry, SHORT entry - exit (``position_points``). A WINNER is a
    position with NET P&L > 0 and a LOSER one with NET P&L < 0 (the same basis as every other
    win / loss figure); a position without a net is in neither group. Rupee net P&L stays
    the authoritative result: points ignore size and charges.

    Row: ``symbol``, ``exchange``, ``positions``, ``gross_points_gained`` (sum of the points
    of positions with points > 0), ``gross_points_lost`` (sum of the absolute points of
    positions with points < 0), ``net_points`` (their difference, the sum of all points),
    ``avg_points_per_position``, ``winners`` / ``losers`` (counts), ``avg_win_points`` /
    ``avg_loss_points`` (mean points of the winners / of the losers, negative), and
    ``largest_win_points`` / ``largest_loss_points`` (the best points among the winners /
    the worst among the losers). A figure with no sample is None. Rows: best net_points
    first (ties by symbol, exchange).
    """
    groups: dict[tuple[str, str], list[dict]] = {}
    for t in trades:
        groups.setdefault((str(t["symbol"]), str(t["exchange"])), []).append(t)
    rows = []
    for (symbol, exchange), items in groups.items():
        pts = [
            (position_points(t["entry_price"], t["exit_price"], t["direction"]), net_value(t))
            for t in items
        ]
        all_points = [p for p, _ in pts]
        win_points = [p for p, n in pts if n is not None and n > 0]
        loss_points = [p for p, n in pts if n is not None and n < 0]
        gained = sum(p for p in all_points if p > 0)
        lost = -sum(p for p in all_points if p < 0)
        rows.append(
            {
                "symbol": symbol,
                "exchange": exchange,
                "positions": len(items),
                "gross_points_gained": _r(gained),
                "gross_points_lost": _r(lost),
                "net_points": _r(sum(all_points)),
                "avg_points_per_position": _r(_mean(all_points)),
                "winners": len(win_points),
                "losers": len(loss_points),
                "avg_win_points": _r(_mean(win_points)),
                "avg_loss_points": _r(_mean(loss_points)),
                "largest_win_points": _r(max(win_points)) if win_points else None,
                "largest_loss_points": _r(min(loss_points)) if loss_points else None,
            }
        )
    rows.sort(key=lambda r: (-r["net_points"], r["symbol"], r["exchange"]))
    return rows


# ---------------------------------------------------------------- streaks and drawdown (net)


def _by_close(trades: list[dict]) -> list[dict]:
    return sorted(trades, key=lambda t: (str(t["closed_at"]), t["id"]))


def current_streak(trades: list[dict]) -> dict:
    """The streak the sequence ends in: {"type": "win" | "loss" | None, "length": n}. Net
    basis, ordered by (closed_at, id); a break-even trade ends it (type None, length 0); a
    trade without a net is skipped."""
    kind, length = None, 0
    for t in _by_close(trades):
        pnl = net_value(t)
        if pnl is None:
            continue
        new = "win" if pnl > 0 else "loss" if pnl < 0 else None
        if new is None:
            kind, length = None, 0
        elif new == kind:
            length += 1
        else:
            kind, length = new, 1
    return {"type": kind, "length": length}


def drawdown(trades: list[dict]) -> dict:
    """Drawdown of the NET REALISED equity of the closed positions.

    Equity starts at 0 and moves by each position's net P&L at its close, chronologically
    by (closed_at, id); a position without a net is left out (counted in
    ``net_unavailable_positions``). Peak starts at 0, so a loss before any gain is a
    drawdown. Amounts are POSITIVE rupees (0 = none):

    * ``max_drawdown`` = the largest (peak equity - equity) over the sequence,
      ``max_drawdown_at`` the close time of the position that reached it;
    * ``current_drawdown`` = peak equity - latest equity (0 at a new high);
    * ``peak_equity`` / ``latest_equity``.

    A percentage is NOT given: discretionary trading has no capital basis
    (``percent_unavailable_reason``).
    """
    equity = peak = worst = 0.0
    worst_at = None
    used = skipped = 0
    peak_at = None  # close time of the position that set the peak (None: the start at 0)
    first_open = None
    first_open_dt = None
    series: list[tuple] = []  # (closed_at, equity) of every position with a net
    worst_idx = None
    worst_peak = 0.0
    worst_start = None
    worst_from_zero = False
    for t in _by_close(trades):
        pnl = net_value(t)
        if pnl is None:
            skipped += 1
            continue
        used += 1
        # Earliest opening among the positions used so far (not the first one to close).
        opened = _as_datetime(t.get("opened_at"))
        if opened is not None and (first_open_dt is None or opened < first_open_dt):
            first_open, first_open_dt = t.get("opened_at"), opened
        equity += pnl
        series.append((t["closed_at"], equity))
        if equity > peak:
            peak_at = t["closed_at"]
        peak = max(peak, equity)
        if peak - equity > worst:
            worst = peak - equity
            worst_at = t["closed_at"]
            worst_idx = len(series) - 1
            worst_peak = peak
            worst_start = peak_at if peak_at is not None else first_open
            worst_from_zero = peak_at is None
    detail = _drawdown_detail(
        series, worst_idx, worst, worst_peak, worst_start, started_at_zero=worst_from_zero
    )
    out = {
        "basis": "net realised equity by position close time, peak starts at 0",
        "positions": used,
        "net_unavailable_positions": skipped,
        "max_drawdown": _r(worst, 2),
        "max_drawdown_at": worst_at,
        "current_drawdown": _r(peak - equity, 2),
        "peak_equity": _r(peak, 2),
        "latest_equity": _r(equity, 2),
        "max_drawdown_pct": None,
        "current_drawdown_pct": None,
        "percent_unavailable_reason": "no capital basis",
    }
    if detail:
        out["max_drawdown_detail"] = detail
    return out


def _minutes_between(start, end) -> float | None:
    a, b = _as_datetime(start), _as_datetime(end)
    if a is None or b is None:
        return None
    return round(max((b - a).total_seconds(), 0.0) / 60.0, 2)


def _drawdown_detail(
    series: list[tuple], worst_idx, worst: float, peak_level: float, start, started_at_zero: bool
) -> dict | None:
    """Timing of the deepest NET drawdown, or None when there is no drawdown (a key that is
    then absent from ``drawdown``). ``start_at`` is the close of the position that set the
    peak (or, when the peak is the starting 0, the earliest opening among the positions closed up
    to the trough),
    ``trough_at`` the close that reached the depth, ``recovered_at`` the first later close
    whose equity is back at the peak (absent when never recovered). ``duration_minutes`` runs
    from the start to the recovery, or to the latest close when not recovered; absent when
    a time is missing."""
    if worst_idx is None or round(worst, 2) <= 0:
        return None
    recovered_at = next(
        (c for c, eq in series[worst_idx + 1 :] if clean_money(eq - peak_level) >= 0), None
    )
    end = recovered_at if recovered_at is not None else series[-1][0]
    detail: dict = {
        "depth": _r(worst, 2),
        "trough_at": series[worst_idx][0],
        "recovered": recovered_at is not None,
        "start_basis": "first position opened" if started_at_zero else "peak close",
    }
    if start is not None:
        detail["start_at"] = start
    if recovered_at is not None:
        detail["recovered_at"] = recovered_at
    duration = _minutes_between(start, end)
    if duration is not None:
        detail["duration_minutes"] = duration
        detail["duration_basis"] = (
            "start to recovery" if recovered_at is not None else "start to latest close"
        )
    return detail


# ---------------------------------------------------------------- behaviour (additive)

# A position opened within this many minutes after the close of a losing position counts as
# a quick re-entry. Objective timing only; it says nothing about why the trader re-entered.
REENTRY_WINDOW_MINUTES = 15


def net_day_stats(daily_rows: list[dict]) -> dict:
    """Winning / losing / flat days on the NET basis (``net_of_charges`` of each daily row).
    A day whose charges are incomplete has no reliable net and is counted in
    ``net_unavailable_days``, not as a win or loss. Empty dict when no day has a net."""
    nets = []
    skipped = 0
    for r in daily_rows:
        if not r.get("net_charges_complete", True) or r.get("net_of_charges") is None:
            skipped += 1
            continue
        nets.append(clean_money(float(r["net_of_charges"])))
    if not nets:
        return {}
    winning = sum(1 for v in nets if v > 0)
    return {
        "basis": "net of estimated charges, by close date",
        "total_days": len(nets),
        "winning_days": winning,
        "losing_days": sum(1 for v in nets if v < 0),
        "flat_days": sum(1 for v in nets if v == 0),
        "winning_day_pct": _pct(winning / len(nets)),
        "net_unavailable_days": skipped,
    }


def trades_per_day(trades: list[dict]) -> dict:
    """Completed positions per close session date: average over the days with a close and
    the busiest day (earliest on a tie). Empty dict without trades."""
    counts: dict[str, int] = {}
    for t in trades:
        if not t.get("trade_date"):
            continue
        day = str(t["trade_date"])[:10]
        counts[day] = counts.get(day, 0) + 1
    if not counts:
        return {}
    max_day = min(counts, key=lambda d: (-counts[d], d))
    return {
        "basis": "completed positions by close date",
        "days": len(counts),
        "avg": _r(sum(counts.values()) / len(counts), 2),
        "max": counts[max_day],
        "max_day": max_day,
    }


def reentry_after_loss(trades: list[dict], window_minutes: int = REENTRY_WINDOW_MINUTES) -> dict:
    """Timing of the next position opened after each losing position (net < 0) closed.

    The next position is the first one, in any symbol, opened at or after the close of the
    loss (the loss itself never counts). ``reentries_within_window`` counts those opened
    within ``window_minutes``. Empty dict when no loss is followed by a later entry."""
    opens = sorted(
        ((m, t.get("id")) for t in trades if (m := _as_datetime(t.get("opened_at"))) is not None),
        key=lambda x: x[0],
    )
    losses = 0
    quick = 0
    shortest = None
    for t in trades:
        pnl = net_value(t)
        closed = _as_datetime(t.get("closed_at"))
        if pnl is None or pnl >= 0 or closed is None:
            continue
        nxt = next((m for m, pid in opens if pid != t.get("id") and m >= closed), None)
        if nxt is None:
            continue
        gap = (nxt - closed).total_seconds() / 60.0
        losses += 1
        if gap <= window_minutes:
            quick += 1
        shortest = gap if shortest is None else min(shortest, gap)
    if not losses:
        return {}
    return {
        "window_minutes": window_minutes,
        "losses_followed_by_entry": losses,
        "reentries_within_window": quick,
        "shortest_gap_minutes": _r(shortest, 2),
    }


def quantity_by_symbol(trades: list[dict]) -> list[dict]:
    """Min / median / max total closed quantity of the completed positions per symbol
    (exchange and symbol), sorted by symbol. Empty list without trades."""
    groups: dict[tuple, list[float]] = {}
    for t in trades:
        if t.get("symbol") is None or t.get("closed_quantity") is None:
            continue
        groups.setdefault((t["symbol"], t.get("exchange") or ""), []).append(
            float(t["closed_quantity"])
        )
    return [
        {
            "symbol": sym,
            "exchange": exch,
            "positions": len(qs),
            "min_quantity": min(qs),
            "median_quantity": _r(statistics.median(qs), 2),
            "max_quantity": max(qs),
        }
        for (sym, exch), qs in sorted(groups.items())
    ]


def risk_block(trades: list[dict]) -> dict:
    """Drawdown (``drawdown``) plus streaks: max wins / losses and the current streak."""
    max_win, max_loss = trade_streaks(trades)
    return {
        **drawdown(trades),
        "max_consecutive_wins": max_win,
        "max_consecutive_losses": max_loss,
        "current_streak": current_streak(trades),
    }


# ---------------------------------------------------------------- trade management, costs


def trade_management(trades: list[dict]) -> dict:
    """Holding time and net figures of the closed positions.

    ``median_net_pnl`` / ``avg_net_pnl`` over the positions with a net. ``winners`` /
    ``losers``: holding minutes (mean, median, count) of the positions with NET P&L > 0 /
    < 0; a break-even position (net == 0) belongs to neither (``break_even_excluded``), nor
    does one without a net (``net_unavailable_positions``). Holding minutes are from the
    position's opening to its final close; a position without one is not in the count.
    """
    nets = _net_pnls(trades)
    return {
        "net_basis_positions": len(nets),
        "net_unavailable_positions": len(trades) - len(nets),
        "median_net_pnl": _r(_median(nets), 2),
        "avg_net_pnl": _r(_mean(nets), 2),
        "winners": _hold_stats(_hold_values([t for t in trades if (net_value(t) or 0.0) > 0])),
        "losers": _hold_stats(_hold_values([t for t in trades if (net_value(t) or 0.0) < 0])),
        "break_even_excluded": sum(1 for v in nets if v == 0),
    }


def costs_summary(trades: list[dict]) -> dict:
    """Charges against gross profit of the closed positions.

    ``charges_pct_of_gross_profit`` = total estimated charges / total GROSS profit of the
    positions whose gross P&L is > 0, in percent. None with ``reason`` when that gross
    profit is 0 (never inf / NaN). Charges of a position without an estimate are not in the
    total (``charges_unavailable_positions`` counts them).
    """
    charges = [float(t["charges"]) for t in trades if t.get("charges") is not None]
    gross_profit = sum(float(t["realized_pnl"]) for t in trades if float(t["realized_pnl"]) > 0)
    total = sum(charges)
    if gross_profit > 0:
        pct, reason = _r(total / gross_profit * 100, 2), None
    else:
        pct, reason = None, "no gross profit"
    return {
        "total_charges": _r(total, 2),
        "gross_profit": _r(gross_profit, 2),
        "charges_pct_of_gross_profit": pct,
        "reason": reason,
        "charges_unavailable_positions": len(trades) - len(charges),
    }


def _table_row(key: str, items: list[dict]) -> dict:
    nets = _net_pnls(items)
    wins = sum(1 for v in nets if v > 0)
    charges = [float(t["charges"]) for t in items if t.get("charges") is not None]
    return {
        "key": key,
        "positions": len(items),
        "wins": wins,
        "losses": sum(1 for v in nets if v < 0),
        "win_rate": _pct(wins / len(nets)) if nets else None,
        "realized_gross": _r(sum(float(t["realized_pnl"]) for t in items), 2),
        "charges": _r(sum(charges), 2),
        "net_of_charges": _r(sum(nets), 2),
        "charges_unavailable_positions": len(items) - len(charges),
        "net_unavailable_positions": len(items) - len(nets),
    }


def monthly_table(trades: list[dict]) -> list[dict]:
    """One row per CLOSE month (``YYYY-MM`` of the position's ``trade_date``, the session
    date of its final close), oldest first: closed positions, wins, losses, win rate (net
    basis, of the positions with a net), gross, charges, net (sums over the positions;
    charges / net leave out positions without an estimate, counted in the ``*_unavailable_
    positions`` fields). Position-based, whole-position P&L on the final close month."""
    groups: dict[str, list[dict]] = {}
    for t in trades:
        groups.setdefault(str(t["trade_date"])[:7], []).append(t)
    return [{"period": k, **_table_row(k, groups[k])} for k in sorted(groups)]


def weekday_table(trades: list[dict]) -> list[dict]:
    """One row per weekday of the position's CLOSE date (``trade_date``): Mon..Fri always,
    Sat / Sun only when a position closed on one. Same columns as ``monthly_table``."""
    groups: dict[str, list[dict]] = {name: [] for name in WEEKDAYS[:5]}
    for t in trades:
        day = date.fromisoformat(str(t["trade_date"])[:10])
        groups.setdefault(WEEKDAYS[day.weekday()], []).append(t)
    return [{"weekday": k, **_table_row(k, groups[k])} for k in WEEKDAYS if k in groups]


# ---------------------------------------------------------------- session classification


@dataclass(frozen=True)
class SessionWindow:
    """One exchange trading session: naive IST ``start`` (inclusive) to ``end`` (exclusive)."""

    start: datetime
    end: datetime
    is_special: bool = False


class SessionCalendarUnavailable(Exception):
    """The calendar could not answer (a failure, as opposed to a confirmed closed day)."""


# provider(day, exchange) -> SessionWindow, None when the exchange is CONFIRMED closed that
# day, raises SessionCalendarUnavailable when the calendar cannot tell. Injected by the
# service; this module does no I/O.
SessionProvider = Callable[[date, str], SessionWindow | None]


def _window_containing(moment: datetime, exchange: str, provider: SessionProvider):
    """The session window holding ``moment`` (its own date, else the previous date for a
    session running past midnight), None when there is none."""
    for offset in (0, 1):
        window = provider(moment.date() - timedelta(days=offset), exchange)
        if window is not None and window.start <= moment < window.end:
            return window
    return None


def classify_with_reason(
    position: dict,
    provider: SessionProvider,
    *,
    reliable_since: date | None = None,
    now: datetime | None = None,
) -> tuple[str, str | None]:
    """(category, reason) of a position by the exchange session windows.

    * intraday: opened and fully closed inside the SAME exchange session window;
    * swing: still open after the opening session ended (a close in a later session, a
      partial exit and then overnight, or an open position whose opening session has ended
      at ``now``);
    * unclassified (reason): never guessed. ``opening_unknown`` (the opening is not in the
      ledger), ``orphan_fills`` (orphan fills inside the position), ``before_reliable_since``
      (opened before the ledger's reliable-since date), ``invalid_times``,
      ``fill_outside_session`` (the opening fill, or a closing fill after the opening session,
      is in no session window of its day, or the day is closed), ``special_session`` (opened
      in a special session), ``still_open_in_session`` (open now and the opening session has
      not ended), ``fallback_boundary`` (see below).

    Calendar failure (``SessionCalendarUnavailable``): the 03:00 session boundary
    (``session_date``) is used ONLY where the result is unambiguous: closed in a LATER
    session date than opened => swing with reason ``fallback_boundary`` (no session ends
    after the boundary, so the position was open across one); everything else (same session
    date, which would need the real session end) => unclassified, reason
    ``fallback_boundary``. The product (MIS / CNC) is never used.
    """
    if position.get("opening_unknown"):
        return "unclassified", "opening_unknown"
    if position.get("has_orphan_fills") or (position.get("orphan_fills") or 0) > 0:
        return "unclassified", "orphan_fills"
    opened = _as_datetime(position.get("opened_at"))
    closed = _as_datetime(position.get("closed_at"))
    if opened is None:
        return "unclassified", "invalid_times"
    if reliable_since is not None and session_date(opened) < reliable_since:
        return "unclassified", "before_reliable_since"
    if closed is not None and closed < opened:
        return "unclassified", "invalid_times"
    exchange = str(position.get("exchange") or "")
    try:
        win = _window_containing(opened, exchange, provider)
        if win is None:
            return "unclassified", "fill_outside_session"
        if win.is_special:
            return "unclassified", "special_session"
        if closed is None:
            if now is not None and now >= win.end:
                return "swing", None
            return "unclassified", "still_open_in_session"
        if closed < win.end:
            return "intraday", None
        if _window_containing(closed, exchange, provider) is None:
            return "unclassified", "fill_outside_session"
        return "swing", None
    except SessionCalendarUnavailable:
        pass
    if closed is None:
        later = now is not None and session_date(now) > session_date(opened)
    else:
        later = session_date(closed) > session_date(opened)
    return ("swing" if later else "unclassified"), "fallback_boundary"


def classify_position(
    position: dict,
    session_windows_provider: SessionProvider,
    *,
    reliable_since: date | None = None,
    now: datetime | None = None,
) -> str:
    """'intraday' | 'swing' | 'unclassified'; see ``classify_with_reason``."""
    return classify_with_reason(
        position, session_windows_provider, reliable_since=reliable_since, now=now
    )[0]


def category_counts(classified: list[tuple[str, str | None]]) -> dict:
    """{"intraday": n, "swing": n, "unclassified": n, "unclassified_reasons": {reason: n}}."""
    out: dict = dict.fromkeys(CATEGORIES, 0)
    reasons: dict[str, int] = {}
    for category, reason in classified:
        out[category] += 1
        if category == "unclassified":
            reasons[reason or "unknown"] = reasons.get(reason or "unknown", 0) + 1
    out["unclassified_reasons"] = dict(sorted(reasons.items()))
    return out


# ---------------------------------------------------------------- MAE / MFE (path method)

EXCURSION_NOTE = (
    "MAE/MFE for completed trades becomes available after market close, once price data "
    "processing is complete."
)
# OpenAlgo derivative symbols carry an expiry like 28NOV24 (NIFTY28NOV24FUT, NIFTY28NOV2424000CE);
# a cash equity symbol never does. (A CE / PE / FUT suffix alone is NOT a test: RELIANCE.)
_NON_CASH_SYMBOL = re.compile(r"\d{2}[A-Z]{3}\d{2}|INDEX", re.IGNORECASE)

EXCURSION_REASON_TEXT = {
    "unsupported_segment": "unsupported segment",
    "fill_times_unreliable": "fill times missing, out of order or ambiguous",
    "inconsistent_fills": "fills do not form a consistent position path",
    "no_fills": "fills of the position not found",
    "session_calendar_unavailable": "exchange calendar not available",
    "fill_outside_session": "a fill is outside the exchange session",
    "invalid_bars": "price data contains an invalid bar",
    "missing_bars": "price data has missing minutes",
    "after_market_close": "available after market close",
    "historify_incomplete": "price data for the day is not complete yet",
    "bar_read_failed": "price data could not be read",
    "not_computed_cap": "not computed (too many positions in range)",
    "open_position": "position is not closed",
}


def excursion_segment_supported(exchange, symbol) -> bool:
    """NSE EQUITY only: exchange NSE and a cash symbol (derivative or index looking symbols
    are refused). NFO, BFO, BSE, MCX, commodities and indices are not supported."""
    if str(exchange or "").upper() != "NSE":
        return False
    text = str(symbol or "").strip()
    return bool(text) and _NON_CASH_SYMBOL.search(text) is None


def _unavailable(reason: str) -> dict:
    return {
        "status": "unavailable",
        "reason": reason,
        "reason_text": EXCURSION_REASON_TEXT.get(reason, reason),
        "mae_points": None,
        "mfe_points": None,
        "mae_amount": None,
        "mfe_amount": None,
        "minute_bar_precision": True,
    }


def excursion_unavailable(reason: str) -> dict:
    """An unavailable excursion result with ``reason`` (a key of EXCURSION_REASON_TEXT)."""
    return _unavailable(reason)


def _minute(moment: datetime) -> datetime:
    return moment.replace(second=0, microsecond=0)


def _path_states(fills: list[dict]):
    """Validate ``fills`` and return [(fill, qty_before, avg_before, qty_after, avg_after)].

    Raises ValueError("fill_times_unreliable" | "inconsistent_fills"). Each fill:
    ``time`` (datetime), ``role`` ("ENTRY" adds quantity, "EXIT" removes it), ``qty`` > 0,
    ``price`` > 0, in processing order.
    """
    if not fills:
        raise ValueError("no_fills")
    last_time = None
    for f in fills:
        moment = f.get("time")
        if not isinstance(moment, datetime):
            raise ValueError("fill_times_unreliable")
        if last_time is not None and moment < last_time:
            raise ValueError("fill_times_unreliable")
        last_time = moment
    # Equal timestamps with different roles: the open quantity between them is unknowable.
    by_time: dict[datetime, set[str]] = {}
    for f in fills:
        by_time.setdefault(f["time"], set()).add(f.get("role"))
    if any(len(roles) > 1 for roles in by_time.values()):
        raise ValueError("fill_times_unreliable")
    qty = avg = 0.0
    out = []
    for f in fills:
        try:
            amount, price = _num(f["qty"]), _num(f["price"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("inconsistent_fills") from None
        if amount <= 0 or price <= 0 or f.get("role") not in ("ENTRY", "EXIT"):
            raise ValueError("inconsistent_fills")
        before = (qty, avg)
        if f["role"] == "ENTRY":
            avg = (avg * qty + price * amount) / (qty + amount)
            qty += amount
        else:
            if qty <= 1e-9 or amount > qty + 1e-9:
                raise ValueError("inconsistent_fills")
            qty -= amount
            if qty < 1e-9:
                qty, avg = 0.0, 0.0
        out.append((f, before[0], before[1], qty, avg))
    if qty > 1e-6:
        raise ValueError("inconsistent_fills")
    return out


def expected_minutes(
    first: datetime, last: datetime, exchange: str, provider: SessionProvider
) -> list[datetime]:
    """Every session minute from ``first``'s minute to ``last``'s minute inclusive: for each
    calendar day in between the exchange session window (a closed day has none), clipped to
    those two minutes. Calendar failure raises SessionCalendarUnavailable."""
    lo, hi = _minute(first), _minute(last)
    minutes: list[datetime] = []
    day = lo.date()
    while day <= hi.date():
        window = provider(day, exchange)
        if window is not None:
            m = max(_minute(window.start), lo)
            stop = min(window.end, hi + timedelta(minutes=1))
            while m < stop:
                minutes.append(m)
                m += timedelta(minutes=1)
        day += timedelta(days=1)
    return minutes


def position_excursion(
    direction: str,
    fills: list[dict],
    bars: dict[datetime, tuple[float, float]],
    exchange: str,
    provider: SessionProvider,
) -> dict:
    """MAE / MFE of ONE closed position by the quantity-path method. Pure: bars and fills
    are arguments.

    Inputs: ``fills`` (see ``_path_states``) in processing order; ``bars`` maps a naive IST
    minute (the bar's start) to its (high, low); ``provider`` gives the exchange session
    windows.

    Path. Between consecutive fills the open quantity and the quantity-weighted AVERAGE
    ENTRY price are constant (an ENTRY fill re-weights the average, an EXIT fill only
    reduces the quantity). Per unit, against the average entry in force:
    LONG adverse = avg - low, favourable = high - avg; SHORT adverse = high - avg,
    favourable = avg - low; each floored at 0.

    * ``mae_points`` / ``mfe_points`` = the worst adverse / best favourable per-unit
      excursion over the whole path (points per unit, not lot-scaled).
    * ``mae_amount`` / ``mfe_amount`` (rupees) = the MAX over the observations of
      (per-unit excursion x the OPEN quantity at that moment). It may come from a different
      moment than the points figure.

    Minute-bar precision (ONE rule). A 1-minute bar does not say whether its high / low came
    before or after a fill inside that minute. So a bar in a FILL minute is NOT used for its
    high / low: only the fill price itself (a real traded price) is observed, against the
    average entry before the fill (when a position was open) and after it. Every other
    minute strictly between the first and last fill minutes contributes its full high /
    low against the state then. The result therefore never overstates (a LOWER BOUND of the
    true tick excursion) and is flagged ``minute_bar_precision`` True; it is not tick
    accurate.

    Strictness. Unavailable (``status`` "unavailable", a ``reason`` code, no number, no
    estimate and no fall-back to the average entry) when: the direction is unknown;
    fill times are missing, out of order or two fills of different roles share a timestamp
    (``fill_times_unreliable``); the quantity path is inconsistent; a fill is outside the
    exchange session (``fill_outside_session``); the calendar fails; ANY session minute from
    the first to the last fill minute inclusive has no valid bar (``missing_bars``; a minute
    with no trades is indistinguishable from missing data, so this is strict); a bar is
    invalid.
    """
    side = str(direction).upper()
    if side not in ("LONG", "SHORT"):
        return _unavailable("inconsistent_fills")
    try:
        states = _path_states(fills)
    except ValueError as exc:
        code = str(exc)
        return _unavailable(
            code if code in ("fill_times_unreliable", "no_fills") else "inconsistent_fills"
        )
    try:
        for f, *_rest in states:
            if _window_containing(f["time"], exchange, provider) is None:
                return _unavailable("fill_outside_session")
        needed = expected_minutes(states[0][0]["time"], states[-1][0]["time"], exchange, provider)
    except SessionCalendarUnavailable:
        return _unavailable("session_calendar_unavailable")
    valid: dict[datetime, tuple[float, float]] = {}
    for m in needed:
        bar = bars.get(m)
        if bar is None:
            return _unavailable("missing_bars")
        try:
            high, low = _num(bar[0]), _num(bar[1])
        except (TypeError, ValueError, IndexError):
            return _unavailable("invalid_bars")
        if low <= 0 or high < low:
            return _unavailable("invalid_bars")
        valid[m] = (high, low)

    long_side = side == "LONG"
    mae = mfe = mae_amt = mfe_amt = 0.0

    def observe(high: float, low: float, qty: float, avg: float) -> None:
        nonlocal mae, mfe, mae_amt, mfe_amt
        if qty <= 1e-9:
            return
        adverse = max(0.0, (avg - low) if long_side else (high - avg))
        favourable = max(0.0, (high - avg) if long_side else (avg - low))
        mae, mfe = max(mae, adverse), max(mfe, favourable)
        mae_amt, mfe_amt = max(mae_amt, adverse * qty), max(mfe_amt, favourable * qty)

    fill_minutes = {_minute(f["time"]) for f, *_rest in states}
    for f, qty_before, avg_before, qty_after, avg_after in states:
        price = float(f["price"])
        observe(price, price, qty_before, avg_before)
        observe(price, price, qty_after, avg_after)
    bars_used = 0
    for k, (f, _qb, _ab, qty_after, avg_after) in enumerate(states[:-1]):
        start, end = _minute(f["time"]), _minute(states[k + 1][0]["time"])
        for m in needed:
            if start < m < end and m not in fill_minutes:
                observe(valid[m][0], valid[m][1], qty_after, avg_after)
                bars_used += 1
    return {
        "status": "available",
        "reason": None,
        "reason_text": None,
        "mae_points": _r(mae, 4),
        "mfe_points": _r(mfe, 4),
        "mae_amount": _r(mae_amt, 2),
        "mfe_amount": _r(mfe_amt, 2),
        "minute_bar_precision": True,
        "bound": "lower_bound",
        "bars_used": bars_used,
        "bars_expected": len(needed),
    }


def profit_capture(net_pnl, gross_points, mfe_points) -> float | None:
    """Realised gross points per unit / MFE points, for a WINNER (net P&L > 0) only. None when
    the position is not a winner, the MFE is unavailable or the MFE is 0. Not capped: MFE is a
    lower bound (see ``position_excursion``)."""
    if net_pnl is None or mfe_points is None or gross_points is None:
        return None
    if clean_money(net_pnl) <= 0 or float(mfe_points) <= 0:
        return None
    return float(gross_points) / float(mfe_points)


def _dist(values: list[float], places: int = 4) -> dict:
    return {
        "count": len(values),
        "average": _r(_mean(values), places),
        "median": _r(_median(values), places),
    }


def excursion_aggregates(rows: list[dict]) -> dict:
    """Aggregates over per-position excursion results.

    Each row: ``status``, ``net_pnl``, ``mae_points``, ``mfe_points``, ``mae_amount``,
    ``mfe_amount``, ``profit_capture``. Winners are net > 0, losers net < 0 (break-even and
    no-net positions are in coverage only). Per group: average / median / count of MAE and MFE
    in points and in rupees; ``profit_capture`` (winners only): average / median / count over
    the winners where it is available. ``coverage``: ``available`` of ``total`` positions,
    plus the unavailable reasons.
    """
    done = [r for r in rows if r.get("status") == "available"]
    reasons: dict[str, int] = {}
    for r in rows:
        if r.get("status") != "available":
            key = r.get("reason") or "unknown"
            reasons[key] = reasons.get(key, 0) + 1

    def group(side) -> dict:
        items = [
            r for r in done if r.get("net_pnl") is not None and side(clean_money(r["net_pnl"]))
        ]
        return {
            "mae_points": _dist([float(r["mae_points"]) for r in items]),
            "mfe_points": _dist([float(r["mfe_points"]) for r in items]),
            "mae_amount": _dist([float(r["mae_amount"]) for r in items], 2),
            "mfe_amount": _dist([float(r["mfe_amount"]) for r in items], 2),
        }

    winners = group(lambda v: v > 0)
    captures = [float(r["profit_capture"]) for r in done if r.get("profit_capture") is not None]
    winners["profit_capture"] = _dist(captures)
    return {
        "winners": winners,
        "losers": group(lambda v: v < 0),
        "coverage": {
            "available": len(done),
            "total": len(rows),
            "label": f"{len(done)} of {len(rows)} positions available",
            "unavailable_reasons": dict(sorted(reasons.items())),
        },
        "minute_bar_precision": True,
        "precision_note": (
            "Minute-bar based lower bound of the true excursion, not tick accurate: the minute "
            "of each fill is observed only at the fill price."
        ),
    }
