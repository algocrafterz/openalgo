"""Exposure, concurrency, turnover, holding time and concentration of a recorded run.

Read-only and descriptive (proposed 2026-10-09, for review before any recording integration).
It does not change position sizing or execution rules and creates no trial.

  SOURCE.      A run's `evidence/trades.parquet` and its recorded `assumptions.json`. No bars are
               needed.
  ASSUMPTIONS. Capital allocation is whatever the run recorded and nothing else is assumed:
               `sizing.capital_per_trade` is the fixed notional of every trade, `sizing.basis` is
               fixed_notional, `portfolio.mode` / `portfolio.scope` say how many positions the
               engine allowed. When the scope is per symbol the engine has no portfolio-wide cap,
               so concurrency across symbols is whatever the signals produced. The report converts
               positions to notional at the recorded figure. It states no margin, leverage or
               account size, because the run recorded none: capital needed is not derived here.
  OCCUPANCY.   A position occupies every 5-minute bar from its entry bar to its exit bar, both
               inclusive (the exit time is the open stamp of the exit bar). Concurrency is the
               time-weighted count of open positions over the bars in which at least one position
               is open, in bar units. A position that crosses an IST date is counted as overnight;
               an intraday run must report zero.
  TURNOVER.    Each round trip trades twice its notional. Notional traded, round trips and
               entries are given per IST session.
  CONCENTRATION. Share of trades by symbol (top five, top ten, Herfindahl index and its inverse,
               the effective number of symbols), how many entries land in the single busiest bar
               of a session, and the make-up of the book at the moment of peak concurrency.
  MONEY.       Gross, cost and net rupees are bps times the fixed notional, summed over trades,
               with no compounding and no portfolio constraint. They restate the bps result in
               rupees and are not a simulated account.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd

from signal_engine.backtest.concurrency import concurrency_profile
from signal_engine.backtest.cost_contribution import seal
from signal_engine.timeutils import IST

SCHEMA = "exposure_turnover/1"
BAR_MINUTES = 5
STEP_NS = BAR_MINUTES * 60 * 10**9
PERCENTILES = (5, 25, 50, 75, 90, 95, 99)
MIN_GROUP = 30
REQUIRED = (
    "symbol",
    "side",
    "entry_time",
    "exit_time",
    "exit_reason",
    "gross_bps",
    "net_bps",
    "cost_bps",
)
ASSUMPTION_FIELDS = (
    "sizing.capital_per_trade",
    "sizing.basis",
    "portfolio.mode",
    "portfolio.scope",
)


class ExposureError(ValueError):
    """The inputs cannot support the report."""


def _num(x: Any) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), 6)


def _pct(values: np.ndarray) -> dict[str, Any]:
    if len(values) == 0:
        return {"n": 0}
    q = np.percentile(values, PERCENTILES)
    return {
        "n": int(len(values)),
        "mean": _num(values.mean()),
        "max": _num(values.max()),
        **{f"p{p}": _num(v) for p, v in zip(PERCENTILES, q, strict=True)},
    }


def _weighted_quantiles(values: np.ndarray, weights: np.ndarray) -> dict[str, Any]:
    keep = weights > 0
    values, weights = values[keep], weights[keep]
    if len(values) == 0:
        return {"bars": 0}
    order = np.argsort(values, kind="stable")
    v, w = values[order], weights[order]
    cum = np.cumsum(w) / w.sum()
    out = {"bars": _num(w.sum()), "mean": _num(np.average(v, weights=w)), "max": _num(v.max())}
    for p in PERCENTILES:
        out[f"p{p}"] = _num(v[min(int(np.searchsorted(cum, p / 100.0)), len(v) - 1)])
    return out


def _check(trades: pd.DataFrame) -> None:
    missing = [c for c in REQUIRED if c not in trades.columns]
    if missing:
        raise ExposureError(f"trade list lacks columns: {', '.join(missing)}")
    if trades.empty:
        raise ExposureError("trade list is empty")


def read_assumptions(run_dir: str | Path) -> dict[str, Any]:
    """The recorded allocation assumptions, by field, with their ids and sources."""
    raw = json.loads((Path(run_dir) / "assumptions.json").read_text())
    found: dict[str, Any] = {}
    for e in raw.get("entries", []):
        if e.get("field") in ASSUMPTION_FIELDS:
            found[e["field"]] = {"value": e.get("value"), "id": e.get("id"), "source": e.get("source")}
    return found


def notional_from(assumptions: dict[str, Any]) -> float:
    entry = assumptions.get("sizing.capital_per_trade")
    try:
        value = float(entry["value"])
    except (TypeError, KeyError, ValueError) as exc:
        raise ExposureError("the run did not record sizing.capital_per_trade") from exc
    if not value > 0:
        raise ExposureError(f"sizing.capital_per_trade must be positive, got {value}")
    return value


def _utc(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True)


def occupancy(trades: pd.DataFrame, notional: float) -> dict[str, Any]:
    """Time-weighted concurrency and exposure, and the book at the moment of peak concurrency."""
    entry, exit_ = _utc(trades["entry_time"]), _utc(trades["exit_time"])
    start = entry.astype("int64").to_numpy()
    end = exit_.astype("int64").to_numpy() + STEP_NS
    is_long = (trades["side"].to_numpy() == "BUY").astype(int)
    times = np.concatenate([start, end])
    delta = np.concatenate([np.ones(len(start), int), -np.ones(len(end), int)])
    long_delta = np.concatenate([is_long, -is_long])
    order = np.lexsort((delta, times))  # at one instant, exits (-1) before entries (+1)
    times, delta, long_delta = times[order], delta[order], long_delta[order]
    conc = np.cumsum(delta)
    n_long = np.cumsum(long_delta)
    n_short = conc - n_long
    dur = np.diff(times, append=times[-1]) / STEP_NS
    active = conc > 0
    w = np.where(active, dur, 0.0)
    day = np.asarray(pd.to_datetime(times, utc=True).tz_convert(IST).date)
    peak_i = int(np.argmax(conc))
    t_peak = times[peak_i]
    book = trades.loc[(start <= t_peak) & (end > t_peak)]
    per_day = pd.Series(conc[active], index=day[active]).groupby(level=0).max()
    crossing = entry.dt.tz_convert(IST).dt.date != exit_.dt.tz_convert(IST).dt.date
    net_long_time = dur[active & (n_long > n_short)].sum()
    refs = [
        SimpleNamespace(entry_time=a, exit_time=b, symbol=s)
        for a, b, s in zip(entry, exit_, trades["symbol"], strict=True)
    ]
    return {
        "overnight_positions": int(crossing.sum()),
        "concurrent_positions_time_weighted": _weighted_quantiles(conc.astype(float), w),
        "gross_exposure_notional_time_weighted": _weighted_quantiles(conc * notional * 1.0, w),
        "net_exposure_notional_time_weighted": _weighted_quantiles(
            (n_long - n_short) * notional * 1.0, w
        ),
        "share_of_active_bar_time_net_long": _num(net_long_time / dur[active].sum()),
        "peak": {
            "concurrent_positions": int(conc[peak_i]),
            "gross_exposure_notional": _num(conc[peak_i] * notional),
            "at_ist": pd.Timestamp(t_peak, tz="UTC").tz_convert(IST).isoformat(),
            "long": int((book["side"] == "BUY").sum()),
            "short": int((book["side"] == "SELL").sum()),
            "distinct_symbols": int(book["symbol"].nunique()),
        },
        "peak_concurrency_per_session": _pct(per_day.to_numpy(float)),
        "sessions_with_a_position": int(len(per_day)),
        "reference_entry_profile": concurrency_profile(refs),
    }


def holding(trades: pd.DataFrame) -> dict[str, Any]:
    """Holding time in minutes from the entry bar open to the end of the exit bar."""
    mins = (_utc(trades["exit_time"]) - _utc(trades["entry_time"])).dt.total_seconds().to_numpy()
    mins = mins / 60.0 + BAR_MINUTES
    out: dict[str, Any] = {
        "definition": "minutes from the entry bar open to the end of the exit bar",
        "all": _pct(mins),
        "share_within_one_bar": _num((mins <= BAR_MINUTES).mean()),
        "share_within_30_minutes": _num((mins <= 30).mean()),
        "share_over_120_minutes": _num((mins > 120).mean()),
        "by_exit_reason": {},
    }
    for reason, g in trades.assign(_m=mins).groupby("exit_reason"):
        out["by_exit_reason"][str(reason)] = (
            _pct(g["_m"].to_numpy()) if len(g) >= MIN_GROUP else {"n": int(len(g)), "withheld": True}
        )
    return out


def turnover(trades: pd.DataFrame, notional: float) -> dict[str, Any]:
    """Round trips, entries and notional traded per IST session, and in total."""
    entry = _utc(trades["entry_time"]).dt.tz_convert(IST)
    day = entry.dt.date.to_numpy()
    per_day = trades.groupby(day).size()
    busiest = (
        pd.DataFrame({"day": day, "bar": entry.dt.floor(f"{BAR_MINUTES}min").to_numpy()})
        .groupby(["day", "bar"])
        .size()
        .groupby(level=0)
        .max()
    )
    minutes = (entry.dt.hour * 60 + entry.dt.minute).to_numpy()
    edges = list(range(9 * 60, 16 * 60 + 1, 30))
    buckets = {
        f"{lo // 60:02d}:{lo % 60:02d}": _num(((minutes >= lo) & (minutes < hi)).mean())
        for lo, hi in zip(edges[:-1], edges[1:], strict=True)
    }
    per_year = trades.groupby(entry.dt.year.to_numpy()).size()
    per_symbol_day = trades.groupby([day, trades["symbol"].to_numpy()]).size()
    return {
        "notional_per_round_trip_traded": _num(2 * notional),
        "round_trips_total": int(len(trades)),
        "notional_traded_total": _num(2 * notional * len(trades)),
        "sessions_with_a_trade": int(len(per_day)),
        "round_trips_per_session": _pct(per_day.to_numpy(float)),
        "notional_traded_per_session": _pct(per_day.to_numpy(float) * 2 * notional),
        "entries_in_busiest_bar_of_session": _pct(busiest.to_numpy(float)),
        "entry_time_share_by_30_min_ist": buckets,
        "round_trips_by_year": {str(k): int(v) for k, v in per_year.items()},
        "entries_per_symbol_per_session": _pct(per_symbol_day.to_numpy(float)),
    }


def concentration(trades: pd.DataFrame) -> dict[str, Any]:
    share = trades["symbol"].value_counts(normalize=True)
    hhi = float((share**2).sum())
    return {
        "symbols_traded": int(len(share)),
        "top_5_symbol_trade_share": _num(share.iloc[:5].sum()),
        "top_10_symbol_trade_share": _num(share.iloc[:10].sum()),
        "most_traded": [{"symbol": str(s), "share": _num(v)} for s, v in share.iloc[:5].items()],
        "herfindahl": _num(hhi),
        "effective_number_of_symbols": _num(1.0 / hhi),
        "long_share_of_trades": _num((trades["side"] == "BUY").mean()),
    }


def money(trades: pd.DataFrame, notional: float) -> dict[str, Any]:
    scale = notional / 1e4
    return {
        "basis": f"bps x fixed notional {notional:g} per trade, summed; no compounding, no cap",
        "gross": _num(trades["gross_bps"].sum() * scale),
        "costs": _num(trades["cost_bps"].sum() * scale),
        "net": _num(trades["net_bps"].sum() * scale),
        "mean_round_trip_cost_bps": _num(trades["cost_bps"].mean()),
    }


def build_report(trades: pd.DataFrame, assumptions: dict[str, Any]) -> dict[str, Any]:
    _check(trades)
    notional = notional_from(assumptions)
    t = trades.reset_index(drop=True)
    return {
        "assumptions": assumptions,
        "notional_per_trade": _num(notional),
        "trades": int(len(t)),
        "occupancy": occupancy(t, notional),
        "holding": holding(t),
        "turnover": turnover(t, notional),
        "concentration": concentration(t),
        "money": money(t, notional),
    }


def build_evidence(
    trades: pd.DataFrame, assumptions: dict[str, Any], source: dict[str, Any]
) -> dict[str, Any]:
    body = {
        "schema": SCHEMA,
        "report_only": True,
        "diagnostic_not_trials": True,
        "source": source,
        "report": build_report(trades, assumptions),
    }
    return {**body, "content_sha256": seal(body)}


def validate_evidence(evidence: dict[str, Any]) -> list[str]:
    problems = []
    if evidence.get("schema") != SCHEMA:
        problems.append(f"schema is {evidence.get('schema')!r}, expected {SCHEMA!r}")
    body = {k: v for k, v in evidence.items() if k != "content_sha256"}
    if evidence.get("content_sha256") != seal(body):
        problems.append("content_sha256 does not match the payload")
    if evidence.get("report_only") is not True or evidence.get("diagnostic_not_trials") is not True:
        problems.append("report_only and diagnostic_not_trials must both be true")
    return problems


def evidence_for_run(run_dir: str | Path) -> dict[str, Any]:
    run = Path(run_dir)
    path = run / "evidence" / "trades.parquet"
    manifest = run / "manifest.json"
    source = {
        "run_dir": run.name,
        "trades_parquet_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "assumptions_sha256": hashlib.sha256((run / "assumptions.json").read_bytes()).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()
        if manifest.exists()
        else None,
        "bar_minutes": BAR_MINUTES,
    }
    return build_evidence(pd.read_parquet(path), read_assumptions(run), source)


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(description="Exposure and turnover of a recorded run (read only)")
    p.add_argument("run_dir")
    p.add_argument("--out", required=True, help="where to write exposure_turnover.json")
    args = p.parse_args(argv)
    evidence = evidence_for_run(args.run_dir)
    problems = validate_evidence(evidence)
    Path(args.out).write_text(json.dumps(evidence, indent=1, allow_nan=False))
    sys.stdout.write(f"wrote {args.out}; problems: {problems or 'none'}\n")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
