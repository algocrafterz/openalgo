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
from signal_engine.backtest.costs import MODEL_NAME, CostModel, cost_profile
from signal_engine.backtest.exit_views import exit_views, split_data_gap
from signal_engine.backtest.metrics import clustered_t, hurdle_t
from signal_engine.backtest.regime import regime_block
from signal_engine.backtest.splits import purge_and_embargo

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


def _core_arrays(
    days: np.ndarray, r_gross: np.ndarray, r_net: np.ndarray, risk_pct: np.ndarray
) -> dict[str, Any]:
    """The metrics shared by the full run and each half of an IS/OOS split.

    `t_stat` and `sharpe` are scored on net bps, the same unit as the headline `net_bps`.
    R weights every trade equally whatever its stop width; bps weights by notional, which
    is what a fixed-size slot risks. They can still differ in sign from `net_bps`: the
    headline is a per-trade mean, while `t_stat` counts each trading day once (trades on
    one day share that day's market move). When the sign differs, the result is carried
    by a few heavy signal days, which is a finding, not a bug.
    """
    out: dict[str, Any] = {"trades": len(r_net), "gross_bps": 0.0, "net_bps": 0.0}
    if not len(r_net):
        return out
    net_bps = r_net * risk_pct * 100
    out["gross_bps"] = _num((r_gross * risk_pct).mean() * 100) or 0.0
    out["net_bps"] = _num(net_bps.mean()) or 0.0
    out["win_rate"] = round(float((r_net > 0).mean()), 4)
    _put(out, "t_stat", clustered_t(net_bps, days), 3)
    _put(out, "sharpe", _daily_sharpe(net_bps, days), 3)
    return out


def _arrays(trades: Sequence) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    return (
        np.array([_iso_day(t.day) for t in trades]),
        np.array([t.r_gross for t in trades], dtype=float),
        np.array([t.r_net for t in trades], dtype=float),
        np.array([t.risk_pct for t in trades], dtype=float),
    )


def r_undefined(trades: Sequence) -> bool:
    """True when some trade has no stop, so R multiples are not applicable to the run."""
    return any(not t.r_defined for t in trades)


def _bps_arrays(trades: Sequence) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    return (
        np.array([_iso_day(t.day) for t in trades]),
        np.array([t.gross_bps for t in trades], dtype=float),
        np.array([t.net_bps for t in trades], dtype=float),
    )


def _core_from_bps(days: np.ndarray, gross_bps: np.ndarray, net_bps: np.ndarray) -> dict[str, Any]:
    """`_core_arrays` for trades with no R: the same figures, read from per-trade bps."""
    out: dict[str, Any] = {"trades": len(net_bps), "gross_bps": 0.0, "net_bps": 0.0}
    if not len(net_bps):
        return out
    out["gross_bps"] = _num(gross_bps.mean()) or 0.0
    out["net_bps"] = _num(net_bps.mean()) or 0.0
    out["win_rate"] = round(float((net_bps > 0).mean()), 4)
    _put(out, "t_stat", clustered_t(net_bps, days), 3)
    _put(out, "sharpe", _daily_sharpe(net_bps, days), 3)
    return out


def _core(trades: Sequence) -> dict[str, Any]:
    if r_undefined(trades):
        return _core_from_bps(*_bps_arrays(trades))
    return _core_arrays(*_arrays(trades))


def _curves_arrays(days: np.ndarray, r_net: np.ndarray) -> tuple[list[list], list[list], float]:
    """Cumulative net R and drawdown, one point per trading day, in calendar order."""
    if not len(r_net):
        return [], [], 0.0
    daily = pd.Series(r_net).groupby(days).sum().sort_index()
    equity = daily.cumsum()
    peak = np.maximum.accumulate(np.maximum(equity.to_numpy(), 0.0))
    drawdown = equity.to_numpy() - peak
    eq_pts = [[d, round(float(v), 4)] for d, v in equity.items()]
    dd_pts = [[d, round(float(v), 4)] for d, v in zip(equity.index, drawdown, strict=True)]
    return eq_pts, dd_pts, float(-drawdown.min())


def _daily_curves(trades: Sequence) -> tuple[list[list], list[list], float]:
    """Cumulative net R and drawdown, one point per trading day, in calendar order."""
    days, _, r_net, _ = _arrays(trades)
    return _curves_arrays(days, r_net)


def headline_metrics(
    days: Sequence[str],
    r_gross: Sequence[float],
    r_net: Sequence[float],
    risk_pct: Sequence[float],
    *,
    n_trials: int = 1,
    gross_bps: Sequence[float] | None = None,
    net_bps: Sequence[float] | None = None,
) -> dict[str, Any]:
    """The headline metrics of a run from plain per-trade arrays.

    Pass `gross_bps` and `net_bps` (per trade, basis points of notional) for trades that have no
    stop. R is then not applicable: the R-only metrics are left out, profit factor and drawdown
    are computed in bps, and `r_defined` is False.

    The single implementation behind `build_run_record`'s `metrics` block and the run
    comparison's recomputed views. `days` are ISO session dates, `risk_pct` is the stop
    distance in percent of price. Unknown statistics are omitted, never zero.
    """
    d = np.array([str(x) for x in days])
    if net_bps is not None:
        gb, nb = (np.array(v, dtype=float) for v in (gross_bps, net_bps))
        return _headline_from_bps(d, gb, nb, n_trials)
    g, n, p = (np.array(v, dtype=float) for v in (r_gross, r_net, risk_pct))
    out = _core_arrays(d, g, n, p)
    out["days"] = len(set(d.tolist()))
    out["breakeven_cost_bps"] = out["gross_bps"]
    _put(out, "hurdle_t", hurdle_t(n_trials), 3)
    if not len(n):
        return out
    wins, losses = n[n > 0], n[n <= 0]
    _put(out, "expectancy_r", n.mean())
    out["total_cost_r"] = round(float((g - n).sum()), 4)
    if len(wins) and len(losses) and losses.sum() != 0:
        _put(out, "profit_factor", wins.sum() / abs(losses.sum()), 3)
        _put(out, "payoff", wins.mean() / abs(losses.mean()), 3)
    out["max_dd_r"] = round(_curves_arrays(d, n)[2], 4)
    return out


def _headline_from_bps(
    d: np.ndarray, gb: np.ndarray, nb: np.ndarray, n_trials: int
) -> dict[str, Any]:
    out = _core_from_bps(d, gb, nb)
    out["days"] = len(set(d.tolist()))
    out["breakeven_cost_bps"] = out["gross_bps"]
    _put(out, "hurdle_t", hurdle_t(n_trials), 3)
    if not len(nb):
        return out
    out["r_defined"] = False
    wins, losses = nb[nb > 0], nb[nb <= 0]
    if len(wins) and len(losses) and losses.sum() != 0:
        _put(out, "profit_factor", wins.sum() / abs(losses.sum()), 3)
    out["max_dd_bps"] = round(_curves_arrays(d, nb)[2], 4)
    out["total_cost_bps"] = round(float((gb - nb).sum()), 4)
    return out


def _monthly(trades: Sequence) -> list[dict[str, Any]]:
    if not trades:
        return []
    if r_undefined(trades):
        frame = pd.DataFrame(
            {"month": [_iso_day(t.day)[:7] for t in trades], "b": [t.net_bps for t in trades]}
        )
        grouped = frame.groupby("month")["b"].agg(["size", "sum", "mean"])
        return [
            {
                "month": m,
                "n": int(row["size"]),
                "total_bps": round(float(row["sum"]), 4),
                "net_bps": round(float(row["mean"]), 4),
            }
            for m, row in grouped.iterrows()
        ]
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


def _cost_sensitivity(
    gross_bps: float, model_bps: float, extra_bps: Sequence[float] = ()
) -> list[dict[str, float]]:
    levels = sorted(
        {*SENSITIVITY_BPS, round(float(model_bps), 4), *(round(float(b), 4) for b in extra_bps)}
    )
    return [{"round_trip_bps": c, "net_bps": round(gross_bps - c, 4)} for c in levels]


STATUTORY_MODEL = MODEL_NAME


def _check_statutory_net(trades: Sequence, profile: dict[str, Any]) -> None:
    """The trades of a statutory-basis record must carry the cost model's net, not a flat one."""
    if r_undefined(trades):
        label, mean = "net_bps", float(np.mean([t.net_bps for t in trades]))
    else:
        label, mean = "net_r", float(np.mean([t.r_net for t in trades]))
    modelled = float(profile[label])
    if abs(mean - modelled) > 1e-4:
        raise ValueError(
            f"costs.model is {STATUTORY_MODEL} but the trades' mean {label} {mean:.4f} differs "
            f"from the cost model's {modelled:.4f}: the trades were not restated under it"
        )


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
    baseline_cross_sectional: dict[str, Any] | None = None,
    validation: Sequence[str] = (),
    notes: str = "",
    horizon: str = "intraday",
    regime_days: pd.DataFrame | None = None,
    regime_source: dict[str, Any] | None = None,
    cost_model: CostModel | None = None,
    embargo_sessions: int = 0,
    ledgers: dict[str, pd.DataFrame] | None = None,
    session: dict[str, Any] | None = None,
    trials: dict[str, Any] | None = None,
    truncation: dict[str, Any] | None = None,
    tick_grid: dict[str, Any] | None = None,
    perturbation: dict[str, Any] | None = None,
    live_caps: dict[str, Any] | None = None,
    monte_carlo: dict[str, Any] | None = None,
    latency: dict[str, Any] | None = None,
    capacity: dict[str, Any] | None = None,
    ledger_problems: Sequence[str] = (),
    data_quality: dict[str, Any] | None = None,
    calendar: Any = None,
    membership: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a schema-v2 run record from `Trade` objects. Inputs are not modified.

    `position_caps` (slot counts, e.g. `(3, 5, 10)`) adds the concurrency profile and the
    result with only that many positions open at once. Every trade then needs an
    `entry_time`. Leave it empty to skip.

    `horizon` (intraday, swing or positional) selects which acceptance gates apply to the
    `evaluation` profile attached to the record.

    `regime_days` (from `regime.build_regime_days`) adds the optional `regimes` block: results
    per market regime, each trade placed by its entry day. `regime_source` records where the
    regime inputs came from. Leave `regime_days` empty to skip.

    `cost_model` adds the optional `cost_model` block: the trades priced per trade with the
    statutory schedule at the deployed notional (G1.1, G1.3). This function does not choose a
    cost basis: the headline metrics read the net result the trades carry. When `costs["model"]`
    is `statutory-per-trade` the trades must already carry the cost model's net, and a mean
    that differs from the block's raises ValueError. A record with a flat `costs.model` is the
    older basis, where the block is a secondary view.

    `embargo_sessions` (with `oos_start`) purges in-sample trades that exit on or after the
    cut and skips the first sessions of the out-of-sample window (G3.5); what was removed is
    written into `split`. Zero keeps the plain split by entry day.

    `ledgers` (the four lineage frames) and `session` (mis_squareoff_min, squareoff_margin_min,
    bar_minutes, entry_window) feed the evaluators that need them (G0.5, G6.1, G6.5, G1.6);
    without them those gates stay not_evaluated.

    `data_quality` (the full per-run report), `calendar` (the derived `session.Calendar`) and
    `membership` (membership source, survivorship flag and eligibility policy) feed the data
    and session gates G0.3, G0.4, G0.6, G0.8, G4.7 and G6.2; each stays not_evaluated without
    its input. `trials` (material, external, total, source) is the ledger-derived trial count
    behind `n_trials` and feeds G2.5; `truncation` (a `TruncationResult` as a dict) feeds G0.2.
    `trials` is kept on the record when given.
    """
    if n_trials < 1:
        raise ValueError(f"n_trials must be >= 1, got {n_trials}")
    caps = sorted(set(position_caps))
    if any(isinstance(c, bool) or not isinstance(c, int) or c < 1 for c in caps):
        raise ValueError(f"max_positions must be an integer >= 1, got {list(position_caps)!r}")
    # DATA_GAP closes (spec item 19) are an exception, not normal evidence: they leave every metric and
    # gate, and are reported by exit_views with each date and symbol.
    trades, data_gap = split_data_gap(list(trades))

    no_r = r_undefined(trades)
    if no_r:
        days_arr, gross_arr, net_arr = _bps_arrays(trades)
        nans = np.full(len(trades), np.nan)
        metrics = headline_metrics(
            days_arr, nans, nans, nans, n_trials=n_trials, gross_bps=gross_arr, net_bps=net_arr
        )
        equity, drawdown, _ = _curves_arrays(days_arr, net_arr)
    else:
        metrics = headline_metrics(*_arrays(trades), n_trials=n_trials)
        equity, drawdown, _ = _daily_curves(trades)

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
        "cost_sensitivity": _cost_sensitivity(
            metrics["gross_bps"],
            costs["round_trip_bps"],
            [
                *([costs["flat_round_trip_bps"]] if "flat_round_trip_bps" in costs else []),
                *costs.get("reference_round_trip_bps", ()),
            ],
        ),
        "series": {
            ("equity_bps" if no_r else "equity_r"): downsample(equity, MAX_SERIES_POINTS),
            ("drawdown_bps" if no_r else "drawdown_r"): downsample(
                drawdown, MAX_SERIES_POINTS, keep="min"
            ),
            "monthly": _monthly(trades),
        },
    }
    if (trades or data_gap) and not no_r:
        record["exit_views"] = exit_views(trades, data_gap)
    if oos_start is not None:
        in_sample, out_of_sample = _split(trades, oos_start)
        record["split"] = {"type": "oos", "oos_start": oos_start}
        if embargo_sessions:
            in_sample, out_of_sample, info = purge_and_embargo(trades, oos_start, embargo_sessions)
            record["split"].update(info)
        record["in_sample"] = _core(in_sample)
        record["out_of_sample"] = _core(out_of_sample)
    if caps:
        profile, record["position_caps"] = _position_caps(trades, caps)
        metrics["peak_concurrent"] = profile["peak"]
        metrics["median_concurrent"] = profile["median"]
    if regime_days is not None and not no_r:
        # Regime results are R-based, so they are left out for a run whose trades have no R.
        record["regimes"] = regime_block(trades, regime_days, source=regime_source)
    statutory = costs.get("model") == STATUTORY_MODEL
    if statutory and trades and cost_model is None:
        raise ValueError("costs.model is statutory-per-trade but no cost model was given")
    if cost_model is not None:
        profile = cost_profile(
            trades,
            cost_model,
            costs.get("flat_round_trip_bps", costs["round_trip_bps"]),
            costs.get("reference_round_trip_bps", ()),
        )
        if profile is not None:
            record["cost_model"] = profile
            if statutory:
                _check_statutory_net(trades, profile)
    # Imported here: evaluation pulls in the registry and the data loaders, which a read-only
    # consumer of `headline_metrics` (the run comparison) must not pay for at import time.
    from signal_engine.backtest.evaluation import evaluate

    record["evaluation"] = evaluate(
        trades,
        metrics=metrics,
        n_trials=n_trials,
        horizon=horizon,
        cost_bps=costs["round_trip_bps"],
        cost_model=costs.get("model"),
        oos_start=oos_start,
        position_caps=record.get("position_caps"),
        params=record["params"],
        regimes=record.get("regimes"),
        cost_profile=record.get("cost_model"),
        split=record.get("split"),
        ledgers=ledgers,
        session=session,
        ledger_problems=ledger_problems,
        data_quality=data_quality,
        calendar=calendar,
        membership=membership,
        trials=trials,
        truncation=truncation,
        tick_grid=tick_grid,
        perturbation=perturbation,
        live_caps=live_caps,
        run_date=run_date,
        monte_carlo=monte_carlo,
        latency=latency,
        capacity=capacity,
    )
    if trials is not None:
        record["trials"] = dict(trials)
    if baseline is not None:
        record["baseline"] = dict(baseline)
    if baseline_cross_sectional is not None:
        record["baseline_cross_sectional"] = dict(baseline_cross_sectional)
    if validation:
        record["validation"] = list(validation)
    if notes:
        record["notes"] = notes
    return record
