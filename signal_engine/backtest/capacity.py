"""G5.4 capacity: is each entry order at most 10% of the median 5-minute volume at its entry time?

Design:

  ORDER SIZE.   Shares = the cost model's order notional for the trade (risk per trade x capital
                over the stop distance, capped by leverage) divided by the entry fill price. The
                entry order only; the exit order is not tested here.
  VOLUME.       The median volume of the same time-of-day 5-minute bar over the previous 20
                sessions of that symbol in the run's data (at least 10 of them present). Never the
                entry day itself, so no look-ahead and no single noisy bar. A median of zero is an
                order with no measurable liquidity and counts as over the cap.
  VERDICT.      The cap is per order: one order above 10% fails the gate. Orders with fewer than
                10 prior sessions are unmeasured: counted, reported, never read as within the cap.
  CEILING.      Order size is linear in capital under the cost model, so the capital at which the
                largest measured order would reach the cap is capital x cap / largest participation;
                the same figure at the median, 95th and 99th percentile orders is reported too,
                because one order can make the first absurdly low.
  NOT FACTS.    Participation of a single order against a typical bar. It is not market impact, it
                ignores other orders in the same bar (portfolio caps are not applied) and does not
                model queueing or partial fills. A pass does not prove the order would fill.
  TRIALS.       A diagnostic of the recorded trades. It is not a trial.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd

from signal_engine.backtest.live_caps import trade_hash
from signal_engine.timeutils import IST
from utils.logging import get_logger

logger = get_logger(__name__)

VERSION = 1
CAP = 0.10
HISTORY_SESSIONS = 20
MIN_HISTORY = 10
CHUNK_SYMBOLS = 10
OFFENDERS = 5
SHARE_LEVELS = (0.01, 0.05, 0.10)

COMPLETE = "complete"
UNSUPPORTED = "unsupported"
ERROR = "error"
STATES = (COMPLETE, UNSUPPORTED, ERROR)

ASSUMPTIONS = (
    "order size is the cost model's entry notional over the entry fill price",
    f"volume is the median of the same time-of-day 5-minute bar over the previous "
    f"{HISTORY_SESSIONS} sessions of the symbol (at least {MIN_HISTORY}), never the entry day",
    "the cap is per entry order; one order over the cap fails the gate",
    "a median volume of zero counts as over the cap",
)
LIMITATIONS = (
    "participation of one order against a typical bar, not market impact; other orders in the "
    "same bar are ignored (portfolio caps are not applied)",
    "no queueing or partial-fill model; a pass does not prove an order would fill",
    "entry orders only; per-symbol intraday strategies only",
)


def prior_median_volume(frame: pd.DataFrame) -> pd.DataFrame:
    """Median volume of each (session, time of day) bar over the previous sessions, same time."""
    idx = pd.DatetimeIndex(frame.index)
    idx = idx.tz_localize(IST) if idx.tz is None else idx.tz_convert(IST)
    table = pd.DataFrame(
        {
            "day": idx.date,
            "tod": idx.hour * 60 + idx.minute,
            "v": frame["Volume"].to_numpy(dtype=float),
        }
    )
    wide = table.groupby(["day", "tod"])["v"].sum().unstack("tod").sort_index()
    return wide.shift(1).rolling(HISTORY_SESSIONS, min_periods=MIN_HISTORY).median()


def participation(trades: Sequence[Any], cost_model: Any, medians: pd.DataFrame) -> list[dict]:
    """One row per trade: shares, median volume (NaN when unmeasured) and the ratio."""
    rows = []
    for t in trades:
        stamp = pd.Timestamp(t.entry_time)
        stamp = stamp.tz_localize(IST) if stamp.tzinfo is None else stamp.tz_convert(IST)
        day, tod = stamp.date(), stamp.hour * 60 + stamp.minute
        shares = cost_model.notional(t) / float(t.entry)
        median = medians.at[day, tod] if day in medians.index and tod in medians.columns else np.nan
        if not np.isfinite(median):
            ratio = math.nan
        elif median <= 0:
            ratio = math.inf
        else:
            ratio = shares / float(median)
        rows.append({"trade": t, "shares": shares, "median_volume": float(median), "ratio": ratio})
    return rows


def run_stage(
    *,
    load_frames: Callable[[list[str]], Mapping[str, Any]],
    symbols: Sequence[str],
    cost_model: Any,
    headline_trades: Sequence[Any],
    shared: Mapping[str, Any] | None = None,
    chunk_size: int = CHUNK_SYMBOLS,
    cap: float = CAP,
) -> dict[str, Any]:
    """The `capacity.json` evidence for a run."""
    evidence: dict[str, Any] = {
        "version": VERSION,
        "state": "",
        "reason": "",
        "cap": cap,
        "history_sessions": HISTORY_SESSIONS,
        "min_history": MIN_HISTORY,
        "diagnostic_not_trials": True,
        "assumptions": list(ASSUMPTIONS),
        "limitations": list(LIMITATIONS),
        "shared": dict(shared or {}),
        "trades": len(headline_trades),
        "trade_sha256": trade_hash(headline_trades),
        "capital": float(cost_model.capital),
    }
    by_symbol: dict[str, list] = defaultdict(list)
    for t in headline_trades:
        by_symbol[t.symbol].append(t)
    rows: list[dict] = []
    try:
        ordered = sorted(by_symbol)
        for start in range(0, len(ordered), chunk_size):
            frames = dict(load_frames(ordered[start : start + chunk_size]))
            for symbol in sorted(frames):
                if symbol in by_symbol:
                    rows += participation(by_symbol[symbol], cost_model, prior_median_volume(frames[symbol]))
                frames[symbol] = None
            del frames
            logger.info(f"Capacity: {min(start + chunk_size, len(ordered))}/{len(ordered)} symbols")
    except Exception as exc:  # recorded as a state, never swallowed
        logger.exception("Capacity stage failed")
        return {**evidence, "state": ERROR, "reason": f"{type(exc).__name__}: {exc}"}
    seen = {id(r["trade"]) for r in rows}
    missing = [t for t in headline_trades if id(t) not in seen]
    ratios = np.array([r["ratio"] for r in rows], dtype=float)
    measured = ratios[~np.isnan(ratios)]
    finite = measured[np.isfinite(measured)]
    out: dict[str, Any] = {
        **evidence,
        "state": COMPLETE,
        "measured": int(len(measured)),
        "unmeasured": int(len(headline_trades) - len(measured)),
        "no_data_for_symbol": len(missing),
        "over_cap": int((measured > cap).sum()),
        "share_over": {f"{lv:g}": int((measured > lv).sum()) for lv in SHARE_LEVELS},
        "infinite_participation": int(np.isinf(measured).sum()),
    }
    if len(measured):
        out["max_participation"] = None if np.isinf(measured).any() else float(measured.max())
        out["participation"] = {
            f"p{q}": float(np.percentile(finite, q)) for q in (50, 95, 99)
        } if len(finite) else {}
        if len(finite) and not np.isinf(measured).any() and measured.max() > 0:
            out["capital_ceiling"] = float(cost_model.capital * cap / measured.max())
        if out["participation"]:
            # the capital at which this share of orders would sit at or under the cap (the
            # largest order alone can make the ceiling absurdly low)
            out["capital_at_percentile"] = {
                q: float(cost_model.capital * cap / v)
                for q, v in out["participation"].items()
                if v > 0
            }
        worst = sorted((r for r in rows if not np.isnan(r["ratio"])), key=lambda r: -r["ratio"])
        out["worst_orders"] = [
            {
                "symbol": r["trade"].symbol,
                "entry_time": pd.Timestamp(r["trade"].entry_time).isoformat(),
                "shares": round(r["shares"], 2),
                "median_volume": r["median_volume"],
                "participation": None if math.isinf(r["ratio"]) else r["ratio"],
            }
            for r in worst[:OFFENDERS]
        ]
    return out


def validate_evidence(evidence: Any) -> list[str]:
    """Problems that make a capacity evidence object unusable (empty when sound)."""
    if not isinstance(evidence, Mapping):
        return ["capacity evidence is not an object"]
    problems = []
    if evidence.get("state") not in STATES:
        problems.append(f"state must be one of {', '.join(STATES)}")
    if not isinstance(evidence.get("trades"), int) or not isinstance(evidence.get("trade_sha256"), str):
        problems.append("the evidence must name its trade list (count and fingerprint)")
    if evidence.get("diagnostic_not_trials") is not True:
        problems.append("diagnostic_not_trials must be true")
    if evidence.get("state") == COMPLETE:
        for key in ("measured", "unmeasured", "over_cap"):
            if not isinstance(evidence.get(key), int) or isinstance(evidence.get(key), bool):
                problems.append(f"{key} must be an integer count")
        if not isinstance(evidence.get("cap"), (int, float)) or not 0 < evidence["cap"] <= 1:
            problems.append("cap must be a share between 0 and 1")
    return problems
