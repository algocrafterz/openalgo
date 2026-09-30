"""Market regime classification and per-regime strategy results.

One fixed, versioned rule, so every backtest is cut the same way and results stay
comparable across runs. Bump `REGIME_VERSION` whenever a label could change for the same
inputs.

Two dimensions, each labelled per trading day:

  trend   up / down / range, from the NIFTY close against its 50 and 200 day averages and
          the 20 day slope of the 50 day average. Up needs the close above both averages
          with the 50 day rising by a minimum amount, down is the mirror image, anything mixed is range.
  vol     high / mid / low, from the trailing-year percentile of India VIX, falling back
          per day to the percentile of NIFTY 20 day realised volatility when VIX is
          missing. Both labels are stored so their agreement can be checked.

CAUSALITY. The label of a trading day uses data through the PRIOR day's close only: every
indicator is shifted one day before it is read, percentiles are trailing (never
full-sample terciles, which would peek at the future), and the persistence rule below is a
forward-only state machine. A strategy entering at that day's open could have known it.

PERSISTENCE. A new label must hold for several consecutive days before it takes over, so a
regime does not flicker on one noisy bar. The cost is a few days of lag at each turn.

This module classifies and summarises; it does not filter. Any rule of the form "trade only
in regime X" is itself a hypothesis and has to pass the normal out-of-sample tests.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

REGIME_VERSION = 1

TREND_LABELS = ("up", "down", "range")
VOL_LABELS = ("high", "mid", "low")
DIMENSION_LABELS = {"trend": TREND_LABELS, "vol": VOL_LABELS}

#: A regime with fewer trades than this is reported but flagged, never hidden.
MIN_REGIME_TRADES = 30

TRADING_DAYS = 252


@dataclass(frozen=True)
class RegimeParams:
    """Every knob of the classification, recorded with each result that used it."""

    fast: int = 50
    slow: int = 200
    slope_lookback: int = 20
    #: The 50 day average must have moved at least this fraction over the lookback for a
    #: trend call. Without it a +-2% oscillation reads as alternating up and down trends.
    min_slope: float = 0.005
    trend_persist: int = 5
    rv_window: int = 20
    vol_window: int = 252
    vol_min_history: int = 126
    high_pct: float = 2 / 3
    low_pct: float = 1 / 3
    vol_persist: int = 3
    #: VIX gaps up to this many sessions are carried forward before falling back.
    vix_ffill_days: int = 3


def persist(raw: pd.Series, n: int) -> pd.Series:
    """Hold each label until a different one has repeated for `n` consecutive days.

    Missing values stay missing and do not count toward a switch. The first label seen
    starts the state. Forward-only, so a value never depends on later rows.
    """
    out: list[Any] = []
    state: Any = None
    candidate: Any = None
    streak = 0
    for value in raw.tolist():
        if value is None or (isinstance(value, float) and math.isnan(value)):
            out.append(np.nan)
            continue
        if state is None:
            state = value
        elif value == state:
            candidate, streak = None, 0
        else:
            streak = streak + 1 if value == candidate else 1
            candidate = value
            if streak >= n:
                state, candidate, streak = value, None, 0
        out.append(state)
    return pd.Series(out, index=raw.index, dtype=object)


def _trend(close: pd.Series, p: RegimeParams) -> pd.Series:
    fast = close.rolling(p.fast).mean()
    slow = close.rolling(p.slow).mean()
    slope = fast / fast.shift(p.slope_lookback) - 1.0
    # Price against BOTH averages, plus the direction of the fast one. The 50 vs 200
    # cross is left out on purpose: it lags a fast crash by weeks (Mar 2020 read as a
    # range until late in the month), while price vs the 200 day average does not.
    up = (close > fast) & (close > slow) & (slope > p.min_slope)
    down = (close < fast) & (close < slow) & (slope < -p.min_slope)
    raw = pd.Series(np.where(up, "up", np.where(down, "down", "range")), index=close.index)
    raw = raw.where(slow.notna() & slope.notna())
    return persist(raw, p.trend_persist)


def _midrank(window: np.ndarray) -> float:
    current = window[-1]
    if np.isnan(current):
        return np.nan
    valid = window[~np.isnan(window)]
    return float((valid < current).mean() + 0.5 * (valid == current).mean())


def _trailing_percentile(x: pd.Series, p: RegimeParams) -> pd.Series:
    return x.rolling(p.vol_window, min_periods=p.vol_min_history).apply(_midrank, raw=True)


def _vol_label(pct: pd.Series, p: RegimeParams) -> pd.Series:
    raw = pd.Series(
        np.where(pct >= p.high_pct, "high", np.where(pct <= p.low_pct, "low", "mid")),
        index=pct.index,
    ).where(pct.notna())
    return persist(raw, p.vol_persist)


def build_regime_days(
    close: pd.Series, vix: pd.Series | None = None, params: RegimeParams | None = None
) -> pd.DataFrame:
    """Label every day of `close` (a NIFTY daily close series indexed by trading day).

    Returns a frame indexed like `close` with `trend`, `vol`, `vol_source` (vix or
    realised) plus the two per-method labels `vix_vol` and `rv_vol`. A day with too little
    history is left as NaN. The label on day D uses closes through D-1.
    """
    p = params or RegimeParams()
    close = close.astype(float)
    trend = _trend(close, p)

    log_ret = np.log(close).diff()
    realised = log_ret.rolling(p.rv_window).std() * math.sqrt(TRADING_DAYS)
    rv_vol = _vol_label(_trailing_percentile(realised, p), p)

    if vix is None:
        vix_vol = pd.Series(np.nan, index=close.index, dtype=object)
    else:
        aligned = vix.astype(float).reindex(close.index).ffill(limit=p.vix_ffill_days or None)
        if p.vix_ffill_days == 0:
            aligned = vix.astype(float).reindex(close.index)
        vix_vol = _vol_label(_trailing_percentile(aligned, p), p)

    use_vix = vix_vol.notna()
    vol = vix_vol.where(use_vix, rv_vol)
    source = pd.Series(
        np.where(use_vix, "vix", np.where(rv_vol.notna(), "realised", None)),
        index=close.index,
        dtype=object,
    )

    frame = pd.DataFrame(
        {"trend": trend, "vol": vol, "vol_source": source, "vix_vol": vix_vol, "rv_vol": rv_vol}
    )
    # The label of day D is what was known at the close of D-1.
    return frame.shift(1)


def vol_agreement(frame: pd.DataFrame) -> dict[str, float]:
    """How often the VIX and realised-volatility labels agree, over days both exist."""
    both = frame[["vix_vol", "rv_vol"]].dropna()
    if both.empty:
        return {"days": 0, "exact": float("nan"), "opposite": float("nan")}
    a, b = both["vix_vol"], both["rv_vol"]
    opposite = ((a == "high") & (b == "low")) | ((a == "low") & (b == "high"))
    return {
        "days": int(len(both)),
        "exact": float((a == b).mean()),
        "opposite": float(opposite.mean()),
    }


def _max_drawdown(r: np.ndarray) -> float:
    eq = np.cumsum(r)
    return float(np.max(np.maximum.accumulate(eq) - eq)) if len(eq) else 0.0


def _day_key(day: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(day)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_localize(None)
    return stamp.normalize()


def _rows(trades: list, labels: list[str | None], order: tuple[str, ...], min_trades: int):
    rows = []
    for name in order:
        picked = [t for t, lab in zip(trades, labels, strict=True) if lab == name]
        if not picked:
            continue
        r = np.array([t.r_net for t in picked])
        rows.append(
            {
                "regime": name,
                "trades": len(picked),
                "net_r": round(float(r.mean()), 4),
                "total_r": round(float(r.sum()), 3),
                "win_rate": round(float((r > 0).mean()), 4),
                "max_dd_r": round(_max_drawdown(r), 4),
                "low_sample": len(picked) < min_trades,
            }
        )
    return rows


def regime_block(
    trades: list,
    regime_days: pd.DataFrame,
    *,
    min_trades: int = MIN_REGIME_TRADES,
    source: dict[str, Any] | None = None,
    params: RegimeParams | None = None,
) -> dict[str, Any]:
    """Per-regime results for a trade list, each trade placed by its entry day.

    Returns `{version, params, min_trades, source, trend: [...], vol: [...],
    unclassified_trades}`. A trade whose day has no label (before the warm-up ends, or a
    day missing from the regime series) counts as unclassified in each dimension it lacks.
    Trades within a regime are taken in entry order for the drawdown.
    """
    ordered = sorted(trades, key=lambda t: (pd.Timestamp(t.entry_time), t.symbol))
    keys = [_day_key(t.day) for t in ordered]
    block: dict[str, Any] = {
        "version": REGIME_VERSION,
        "params": asdict(params or RegimeParams()),
        "min_trades": min_trades,
        "source": dict(source or {}),
    }
    unclassified = np.zeros(len(ordered), dtype=bool)
    for dim, order in DIMENSION_LABELS.items():
        lookup = regime_days[dim].reindex(pd.DatetimeIndex(keys))
        labels = [None if pd.isna(v) else str(v) for v in lookup.tolist()]
        unclassified |= np.array([lab is None for lab in labels], dtype=bool)
        block[dim] = _rows(ordered, labels, order, min_trades)
    block["unclassified_trades"] = int(unclassified.sum())
    return block


def breadth(block: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Per dimension: adequately sampled regimes, and how many of them are net positive."""
    out: dict[str, dict[str, int]] = {}
    for dim in DIMENSION_LABELS:
        rows = [r for r in block.get(dim, []) if not r["low_sample"]]
        out[dim] = {"evaluated": len(rows), "positive": sum(1 for r in rows if r["net_r"] > 0)}
    return out
