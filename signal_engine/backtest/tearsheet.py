"""Tearsheet writer: a trade list in, a schema-v2 run record out.

A run record is one immutable backtest execution stored under
`research/ideas/<idea>/runs/<run_id>.json` (schema: research/README.md). Re-running an idea
after a change writes a NEW record that names its parent, so runs can be compared over time.

Conventions that decide whether two runs are comparable, pinned here rather than left to
each caller:

  COSTS ARE IN THE HEADLINE.  `net_bps` is what the strategy kept; `gross_bps` is shown
  beside it, never instead of it. `cost_sensitivity` shows how the answer moves if real
  costs are worse than modelled.

  UNKNOWN IS OMITTED, NOT ZERO.  A statistic that cannot be computed (one trading day, no
  losers) is left out of the record so the page shows "n/a". The schema still requires
  `trades`, `gross_bps` and `net_bps`, so a record with `trades == 0` carries 0.0 in those
  two and the reader must treat them as n/a.

  CALENDAR ORDER.  The equity curve and drawdown are built from per-day sums, so the order
  the harness collected trades in (symbol by symbol) cannot change them. Drawdown is
  measured from an equity curve seeded at 0, so an opening slump counts.

  HONEST TRIAL COUNT.  `n_trials` is how many variants were looked at to arrive at this
  one. `hurdle_t` is the |t| a result must clear at that count, next to the observed
  `t_stat`.

Metrics are in R and basis points of notional. `bps = R x stop% x 100`, which is what
makes runs with different stop widths comparable.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

import numpy as np
import pandas as pd

from signal_engine.backtest.concurrency import apply_position_cap, concurrency_profile
from signal_engine.backtest.metrics import clustered_t, hurdle_t

#: Cap per series, so a record stays small enough to load on a phone.
MAX_SERIES_POINTS = 500

#: Flat round-trip costs (bps) the sensitivity table always shows, plus the model's own.
SENSITIVITY_BPS = (0, 5, 10, 20)


def downsample(points: Sequence[list], max_points: int, keep: str = "last") -> list[list]:
    """Thin `points` to at most `max_points`, always keeping the first and last.

    `keep="last"` keeps the last point of each bucket (right for a cumulative curve);
    `keep="min"` keeps the lowest value in each bucket, so the deepest drawdown survives.
    """
    if keep not in ("last", "min"):
        raise ValueError(f"keep must be 'last' or 'min', got {keep!r}")
    if len(points) <= max_points:
        return list(points)
    middle = list(points[1:-1])
    size = math.ceil(len(middle) / max(max_points - 2, 1))
    picked = []
    for start in range(0, len(middle), size):
        chunk = middle[start : start + size]
        picked.append(min(chunk, key=lambda p: p[1]) if keep == "min" else chunk[-1])
    return [points[0], *picked, points[-1]]


def _num(value: float, digits: int = 4) -> float | None:
    """A JSON-safe number, or None when the value is NaN or infinite."""
    value = float(value)
    return round(value, digits) if math.isfinite(value) else None


def _put(target: dict[str, Any], key: str, value: float, digits: int = 4) -> None:
    number = _num(value, digits)
    if number is not None:
        target[key] = number


def _iso_day(day: Any) -> str:
    return pd.Timestamp(day).date().isoformat()


#: NSE sessions in a year, for annualising the daily Sharpe.
TRADING_DAYS = 252


def _daily_sharpe(net_bps: np.ndarray, days: np.ndarray) -> float:
    """Annualised Sharpe of the per-session sum of net bps. NaN when it is undefined."""
    daily = pd.Series(net_bps).groupby(days).sum()
    if len(daily) < 3 or daily.std(ddof=1) == 0:
        return float("nan")
    return float(daily.mean() / daily.std(ddof=1) * math.sqrt(TRADING_DAYS))


def _core(trades: Sequence) -> dict[str, Any]:
    """The metrics shared by the full run and each half of an IS/OOS split.

    `t_stat` and `sharpe` are scored on net bps, the same unit as the headline `net_bps`.
    R weights every trade equally whatever its stop width; bps weights by notional, which
    is what a fixed-size slot risks. They can still differ in sign from `net_bps`: the
    headline is a per-trade mean, while `t_stat` counts each trading day once (trades on
    one day share that day's market move). When the sign differs, the result is carried
    by a few heavy signal days, which is a finding, not a bug.
    """
    out: dict[str, Any] = {"trades": len(trades), "gross_bps": 0.0, "net_bps": 0.0}
    if not trades:
        return out
    r_net = np.array([t.r_net for t in trades])
    r_gross = np.array([t.r_gross for t in trades])
    risk_pct = np.array([t.risk_pct for t in trades])
    days = np.array([_iso_day(t.day) for t in trades])
    net_bps = r_net * risk_pct * 100
    out["gross_bps"] = _num((r_gross * risk_pct).mean() * 100) or 0.0
    out["net_bps"] = _num(net_bps.mean()) or 0.0
    out["win_rate"] = round(float((r_net > 0).mean()), 4)
    _put(out, "t_stat", clustered_t(net_bps, days), 3)
    _put(out, "sharpe", _daily_sharpe(net_bps, days), 3)
    return out


def _daily_curves(trades: Sequence) -> tuple[list[list], list[list], float]:
    """Cumulative net R and drawdown, one point per trading day, in calendar order."""
    if not trades:
        return [], [], 0.0
    daily = (
        pd.Series([t.r_net for t in trades])
        .groupby([_iso_day(t.day) for t in trades])
        .sum()
        .sort_index()
    )
    equity = daily.cumsum()
    peak = np.maximum.accumulate(np.maximum(equity.to_numpy(), 0.0))
    drawdown = equity.to_numpy() - peak
    eq_pts = [[d, round(float(v), 4)] for d, v in equity.items()]
    dd_pts = [[d, round(float(v), 4)] for d, v in zip(equity.index, drawdown, strict=True)]
    return eq_pts, dd_pts, float(-drawdown.min())


def _monthly(trades: Sequence) -> list[dict[str, Any]]:
    if not trades:
        return []
    frame = pd.DataFrame(
        {"month": [_iso_day(t.day)[:7] for t in trades], "r": [t.r_net for t in trades]}
    )
    grouped = frame.groupby("month")["r"].agg(["size", "sum", "mean"])
    return [
        {
            "month": m,
            "n": int(row["size"]),
            "total_r": round(float(row["sum"]), 4),
            "net_r": round(float(row["mean"]), 4),
        }
        for m, row in grouped.iterrows()
    ]


def _extended(trades: Sequence, n_trials: int) -> dict[str, Any]:
    """Headline metrics: the core set plus payoff shape and the trial-aware hurdle."""
    out = _core(trades)
    out["days"] = len({_iso_day(t.day) for t in trades})
    out["breakeven_cost_bps"] = out["gross_bps"]
    _put(out, "hurdle_t", hurdle_t(n_trials), 3)
    if not trades:
        return out
    r = np.array([t.r_net for t in trades])
    wins, losses = r[r > 0], r[r <= 0]
    _put(out, "expectancy_r", r.mean())
    if len(wins) and len(losses) and losses.sum() != 0:
        _put(out, "profit_factor", wins.sum() / abs(losses.sum()), 3)
        _put(out, "payoff", wins.mean() / abs(losses.mean()), 3)
    return out


def _cost_sensitivity(gross_bps: float, model_bps: float) -> list[dict[str, float]]:
    levels = sorted({*SENSITIVITY_BPS, round(float(model_bps), 4)})
    return [{"round_trip_bps": c, "net_bps": round(gross_bps - c, 4)} for c in levels]


def _split(trades: Sequence, oos_start: str) -> tuple[list, list]:
    days = sorted(_iso_day(t.day) for t in trades)
    if days and not days[0] < oos_start <= days[-1]:
        raise ValueError(
            f"oos_start {oos_start} must fall after the first trade ({days[0]}) "
            f"and no later than the last ({days[-1]})"
        )
    return (
        [t for t in trades if _iso_day(t.day) < oos_start],
        [t for t in trades if _iso_day(t.day) >= oos_start],
    )


def _position_caps(
    trades: Sequence, caps: Sequence[int]
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    """The concurrency profile, and the result an account with N slots would have had."""
    rows = []
    for cap in caps:
        taken = apply_position_cap(trades, cap)
        rows.append(
            {
                "max_positions": cap,
                **_core(taken),
                "taken_pct": round(100 * len(taken) / len(trades), 1) if trades else 0.0,
            }
        )
    return concurrency_profile(trades), rows


def build_run_record(
    trades: Iterable,
    *,
    run_id: str,
    run_date: str,
    code_version: str,
    data: dict[str, Any],
    period: dict[str, str],
    costs: dict[str, Any],
    params: dict[str, Any] | None = None,
    parent_run_id: str | None = None,
    change_note: str = "",
    n_trials: int = 1,
    oos_start: str | None = None,
    position_caps: Sequence[int] = (),
    baseline: dict[str, Any] | None = None,
    validation: Sequence[str] = (),
    notes: str = "",
) -> dict[str, Any]:
    """Build a schema-v2 run record from `Trade` objects. Inputs are not modified.

    `position_caps` (slot counts, e.g. `(3, 5, 10)`) adds the concurrency profile and the
    result with only that many positions open at once. Every trade then needs an
    `entry_time`. Leave it empty to skip.
    """
    if n_trials < 1:
        raise ValueError(f"n_trials must be >= 1, got {n_trials}")
    caps = sorted(set(position_caps))
    if any(isinstance(c, bool) or not isinstance(c, int) or c < 1 for c in caps):
        raise ValueError(f"max_positions must be an integer >= 1, got {list(position_caps)!r}")
    trades = list(trades)

    metrics = _extended(trades, n_trials)
    equity, drawdown, max_dd = _daily_curves(trades)
    if trades:
        metrics["max_dd_r"] = round(max_dd, 4)

    record: dict[str, Any] = {
        "run_id": run_id,
        "date": run_date,
        "code_version": code_version,
        "data": dict(data),
        "period": dict(period),
        "costs": dict(costs),
        "metrics": metrics,
        "params": dict(params or {}),
        "parent_run_id": parent_run_id,
        "change_note": change_note,
        "n_trials": n_trials,
        "promoted": False,
        "cost_sensitivity": _cost_sensitivity(metrics["gross_bps"], costs["round_trip_bps"]),
        "series": {
            "equity_r": downsample(equity, MAX_SERIES_POINTS),
            "drawdown_r": downsample(drawdown, MAX_SERIES_POINTS, keep="min"),
            "monthly": _monthly(trades),
        },
    }
    if oos_start is not None:
        in_sample, out_of_sample = _split(trades, oos_start)
        record["split"] = {"type": "oos", "oos_start": oos_start}
        record["in_sample"] = _core(in_sample)
        record["out_of_sample"] = _core(out_of_sample)
    if caps:
        profile, record["position_caps"] = _position_caps(trades, caps)
        metrics["peak_concurrent"] = profile["peak"]
        metrics["median_concurrent"] = profile["median"]
    if baseline is not None:
        record["baseline"] = dict(baseline)
    if validation:
        record["validation"] = list(validation)
    if notes:
        record["notes"] = notes
    return record
