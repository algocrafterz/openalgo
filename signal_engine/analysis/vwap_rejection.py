"""VWAP rejection: an independent signal family, measured on its own. Research only, never traded.

Source of truth: `research/ideas/_inbox/ema-vwap-atr-early-displacement-v2.lipi`, the VWAP REJECTION
block. `detect` mirrors that block line for line so the chart and this study flag the same bars.

    displacement = (EMA9(close) - session VWAP(hlc3)) / ATR14        on 5-minute bars

Research taxonomy (each family is detected and measured on its own; none relabels another):
  EARLY_DISPLACEMENT  displacement leaves the +/-0.50 band toward zero (the v1 + markers)
  VWAP_REJECTION      displacement approaches zero, does not cross, turns away (this module)
  ACTUAL_CROSSOVER    displacement crosses zero

BEARISH rejection at bar k (BULLISH is the exact mirror, signs flipped):
  1. this session's leg went to <= -depth (the leg restarts when displacement exceeds +zone)
  2. displacement then entered the near-VWAP zone [-zone, +zone]; its highest value there is the peak
  3. displacement[k] <= peak - move and displacement[k] < displacement[k-1]
  4. close[k] < close[k-1]
  One event per approach; a new leg below -depth re-arms it. All state restarts each session.

Measurement of an event at bar k (forward-looking, so diagnostics only):
  signal price  close[k]; one ATR unit = ATR14 at bar k
  window        bars k+1 .. the last continuous-trading bar of the same session
  MFE / MAE     best / worst excursion from the signal price in the rejection's direction, in ATR;
                bars_to_* = bars after k where the extreme was first reached
  crossover     first zero crossing of displacement in the window, either direction (v1 definition:
                prev <= 0 < cur up, prev >= 0 > cur down); the rejection event is kept as is
  outcome       SUCCESS: displacement reaches -depth (bearish) / +depth (bullish) before any crossing
                against the rejection; FAILED: it crosses against the rejection first;
                UNRESOLVED: neither before the session ends

    uv run python -m signal_engine.analysis.vwap_rejection --out DIR
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from signal_engine.strategy_core.ema_vwap_early import EarlyDisplacementParams, build_inputs
from utils.logging import get_logger

logger = get_logger(__name__)

# Family names of the research taxonomy.
EARLY_DISPLACEMENT = "EARLY_DISPLACEMENT"
VWAP_REJECTION = "VWAP_REJECTION"
ACTUAL_CROSSOVER = "ACTUAL_CROSSOVER"

# Reason codes of this family.
VWAP_REJECTION_BEARISH = "VWAP_REJECTION_BEARISH"
VWAP_REJECTION_BULLISH = "VWAP_REJECTION_BULLISH"

# Outcomes.
SUCCESS = "SUCCESS"
FAILED = "FAILED"
UNRESOLVED = "UNRESOLVED"

_NONE_PEAK = -99.0
_NONE_TROUGH = 99.0


@dataclass(frozen=True)
class RejectionParams:
    """The v2 .lipi baseline. Fixed for research; sensitivity is a separate, later test."""

    depth: float = 0.75
    zone: float = 0.25
    move: float = 0.10


@dataclass(frozen=True)
class Rejection:
    k: int
    direction: int  # -1 bearish, +1 bullish
    reason: str
    prior_extreme: float  # deepest displacement of the leg before the approach
    zone_extreme: float  # near-VWAP peak (bearish) / trough (bullish)


def detect(disp: np.ndarray, close: np.ndarray, day: np.ndarray, p: RejectionParams) -> list[Rejection]:
    """Every rejection event, in bar order. Same statement order as the .lipi block."""
    out: list[Rejection] = []
    bear_low = bear_peak = bull_high = bull_trough = 0.0
    bear_done = bull_done = 0
    for k in range(len(disp)):
        if k == 0 or day[k] != day[k - 1]:
            bear_low, bear_peak, bear_done = 0.0, _NONE_PEAK, 0
            bull_high, bull_trough, bull_done = 0.0, _NONE_TROUGH, 0
        d = disp[k]
        if np.isnan(d):
            continue
        if d > p.zone:
            bear_low, bear_peak, bear_done = 0.0, _NONE_PEAK, 0
        if d < -p.zone:
            bull_high, bull_trough, bull_done = 0.0, _NONE_TROUGH, 0
        if d <= p.zone and d < bear_low:
            bear_low = d
        if d >= -p.zone and d > bull_high:
            bull_high = d
        in_zone = -p.zone <= d <= p.zone
        if in_zone and d > bear_peak:
            bear_peak = d
        if in_zone and d < bull_trough:
            bull_trough = d
        prev = disp[k - 1] if k > 0 else np.nan
        price_down = k > 0 and close[k] < close[k - 1]
        price_up = k > 0 and close[k] > close[k - 1]
        bear = (
            bear_done == 0 and bear_low <= -p.depth and bear_peak >= -p.zone
            and d <= bear_peak - p.move and d < prev and price_down
        )
        bull = (
            bull_done == 0 and bull_high >= p.depth and bull_trough <= p.zone
            and d >= bull_trough + p.move and d > prev and price_up
        )
        if bear:
            bear_done = 1
            out.append(Rejection(k, -1, VWAP_REJECTION_BEARISH, bear_low, bear_peak))
        if bull:
            bull_done = 1
            out.append(Rejection(k, 1, VWAP_REJECTION_BULLISH, bull_high, bull_trough))
        if d < -p.depth:
            bear_peak, bear_done = _NONE_PEAK, 0
        if d > p.depth:
            bull_trough, bull_done = _NONE_TROUGH, 0
    return out


def _first_cross(disp: np.ndarray, k: int, end: int) -> tuple[int, int]:
    """(bar, direction) of the first zero crossing in bars k+1..end-1, or (-1, 0)."""
    for j in range(k + 1, end):
        prev, cur = disp[j - 1], disp[j]
        if prev <= 0 < cur:
            return j, 1
        if prev >= 0 > cur:
            return j, -1
    return -1, 0


def _outcome(disp: np.ndarray, e: Rejection, end: int, p: RejectionParams) -> tuple[str, int]:
    for j in range(e.k + 1, end):
        prev, cur = disp[j - 1], disp[j]
        if e.direction * cur >= p.depth:
            return SUCCESS, j
        against = (prev <= 0 < cur) if e.direction == -1 else (prev >= 0 > cur)
        if against:
            return FAILED, j
    return UNRESOLVED, -1


def _excursion(values: np.ndarray) -> tuple[float, float]:
    """(max, bars to its first occurrence, 1-based) of a forward series, NaN when empty."""
    if len(values) == 0:
        return float("nan"), float("nan")
    i = int(np.argmax(values))
    return float(values[i]), float(i + 1)


def measure(inputs: pd.DataFrame, e: Rejection, p: RejectionParams, symbol: str) -> dict[str, Any]:
    """One record per event: signal-time diagnostics plus forward excursions and outcome."""
    day = np.asarray(inputs.index.date)
    k = e.k
    end = k + 1
    while end < len(inputs) and day[end] == day[k]:
        end += 1
    row = inputs.iloc[k]
    price, a = float(row["Close"]), float(row["atr"])
    disp = inputs["disp"].to_numpy()
    high = inputs["High"].to_numpy()[k + 1 : end]
    low = inputs["Low"].to_numpy()[k + 1 : end]
    fav = (price - low) / a if e.direction == -1 else (high - price) / a
    adv = (high - price) / a if e.direction == -1 else (price - low) / a
    mfe, bars_mfe = _excursion(fav)
    mae, bars_mae = _excursion(adv)
    cross_bar, cross_dir = _first_cross(disp, k, end)
    outcome, out_bar = _outcome(disp, e, end, p)
    d = float(row["disp"])
    return {
        "symbol": symbol,
        "session_date": str(day[k]),
        "signal_time": inputs.index[k],
        "family": VWAP_REJECTION,
        "reason_code": e.reason,
        "direction": e.direction,
        "signal_price": price,
        "atr": a,
        "prior_extreme": e.prior_extreme,
        "zone_extreme": e.zone_extreme,
        "disp_at_signal": d,
        "dist_from_zero": abs(d),
        "pullback": abs(e.zone_extreme - d),
        "price_vwap_atr": (price - float(row["vwap"])) / a,
        "price_ema20_atr": (price - float(row["ema_s"])) / a,
        "forward_bars": end - k - 1,
        "mfe_atr": mfe,
        "mae_atr": mae,
        "bars_to_mfe": bars_mfe,
        "bars_to_mae": bars_mae,
        "crossover_after": cross_bar >= 0,
        "crossover_dir": cross_dir,
        "bars_to_crossover": float(cross_bar - k) if cross_bar >= 0 else float("nan"),
        "outcome": outcome,
        "success": {SUCCESS: True, FAILED: False}.get(outcome),
        "bars_to_outcome": float(out_bar - k) if out_bar >= 0 else float("nan"),
    }


def events_for_symbol(symbol: str, df: pd.DataFrame, p: RejectionParams) -> list[dict[str, Any]]:
    """All rejection records of one symbol's 5m frame. Indicators come from the strategy's own
    `build_inputs`, so EMA9, EMA20, session VWAP(hlc3) and ATR14 match the chart study."""
    inputs = build_inputs(df, EarlyDisplacementParams())
    day = np.asarray(inputs.index.date)
    events = detect(inputs["disp"].to_numpy(), inputs["Close"].to_numpy(), day, p)
    return [measure(inputs, e, p, symbol) for e in events]


def summarize(rows: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """Counts and median excursions per reason code."""
    out: dict[str, dict[str, Any]] = {}
    for reason, g in rows.groupby("reason_code"):
        out[str(reason)] = {
            "n": int(len(g)),
            "outcomes": {str(k): int(v) for k, v in g["outcome"].value_counts().items()},
            "median_mfe_atr": float(g["mfe_atr"].median()),
            "median_mae_atr": float(g["mae_atr"].median()),
        }
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    from signal_engine.backtest.mfe_mae_report import load_published

    args.out.mkdir(parents=True, exist_ok=True)
    frames, prov, _ = load_published(args.out)
    p = RejectionParams()
    rows: list[dict[str, Any]] = []
    for symbol in sorted(frames):
        rows.extend(events_for_symbol(symbol, frames[symbol], p))
    df = pd.DataFrame(rows)
    df.to_csv(args.out / "vwap_rejection_events.csv", index=False)
    summary = {
        "params": vars(p),
        "dataset_version": prov.get("dataset_version"),
        "symbols": len(frames),
        "by_reason": summarize(df) if len(df) else {},
    }
    (args.out / "vwap_rejection_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    logger.info("VWAP rejection events: %d across %d symbols -> %s", len(df), len(frames), args.out)


if __name__ == "__main__":
    main()
