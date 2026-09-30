"""Previous-session value area (PVAH / PPOC / PVAL), ported from breakout.pine.

Source functions: klAddBar and klFinalize (pine 2266-2320), klProfile (2327-2360), row step
`syminfo.mintick * klTicksPerRow` (2528) with klTicksPerRow = 5 and klValueAreaPct = 70.

How the Pine builds it, kept exactly:
  * Every 1-minute bar of the session is added to a histogram keyed by price row.
  * A bar's volume is spread EVENLY over the rows its low..high range covers (unlike the
    MarketProfile library in backtest/volume_profile.py, which bins on the close only).
  * A range spanning more than 1000 rows is dumped on its midpoint row.
  * POC is the fullest row; the value area then grows outward from it, always taking the
    fatter neighbouring row, UP on a tie, until it holds `va_pct` of the session volume.
  * Reported prices are row centres: (row + 0.5) * step.
  * The profile is frozen when the next session opens and read all of that next session.

Deliberate differences from a literal port:
  * Row indices use integer tick arithmetic, `floor(price / tick) // rows_per_tick`, not
    `floor(price / (tick * rows_per_tick))`, so a price sitting exactly on a row boundary
    lands in the same row regardless of float32/float64 noise in the source data.
  * On an exact POC volume tie the Pine keeps the first key in map insertion order; here
    that is the lowest price row. Exact ties are vanishingly rare with float volumes.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from signal_engine.features.session import continuous_session

#: Range wider than this many rows is treated as a halt/gap bar (pine klAddBar).
MAX_SPAN_ROWS = 1000
#: Guards floor() against float noise such as 2091.05 / 0.05 = 41820.99999999999.
_TICK_EPS = 1e-6


def _row_index(price: np.ndarray, tick: float, rows_per_tick: int) -> np.ndarray:
    return np.floor(price / tick + _TICK_EPS).astype(np.int64) // rows_per_tick


def histogram(
    lo: np.ndarray, hi: np.ndarray, vol: np.ndarray, tick: float, rows_per_tick: int = 5
) -> dict[int, float]:
    """Volume-at-price histogram {row: volume} for a set of bars (klAddBar)."""
    lo = np.asarray(lo, dtype=np.float64)
    hi = np.asarray(hi, dtype=np.float64)
    vol = np.asarray(vol, dtype=np.float64)
    keep = np.isfinite(lo) & np.isfinite(hi) & np.isfinite(vol) & (vol > 0)
    lo, hi, vol = lo[keep], hi[keep], vol[keep]
    if lo.size == 0:
        return {}

    b0 = _row_index(lo, tick, rows_per_tick)
    b1 = _row_index(hi, tick, rows_per_tick)
    span = b1 - b0 + 1
    wide = span > MAX_SPAN_ROWS

    rows_parts: list[np.ndarray] = []
    weight_parts: list[np.ndarray] = []

    if wide.any():
        rows_parts.append(_row_index((lo[wide] + hi[wide]) / 2.0, tick, rows_per_tick))
        weight_parts.append(vol[wide])

    normal = ~wide
    if normal.any():
        reps = span[normal]
        starts = np.repeat(b0[normal], reps)
        offsets = np.arange(reps.sum()) - np.repeat(np.cumsum(reps) - reps, reps)
        rows_parts.append(starts + offsets)
        weight_parts.append(np.repeat(vol[normal] / reps, reps))

    rows = np.concatenate(rows_parts)
    weights = np.concatenate(weight_parts)
    unique_rows, inverse = np.unique(rows, return_inverse=True)
    totals = np.bincount(inverse, weights=weights)
    return {int(r): float(v) for r, v in zip(unique_rows, totals, strict=True)}


def finalize_profile(
    hist: dict[int, float], va_frac: float, step: float
) -> tuple[float, float, float]:
    """Collapse a histogram into (VAH, POC, VAL) row-centre prices (klFinalize)."""
    if not hist:
        return (np.nan, np.nan, np.nan)

    keys = sorted(hist)
    total = 0.0
    poc_key = keys[0]
    poc_vol = -1.0
    for k in keys:
        v = hist[k]
        total += v
        if v > poc_vol:
            poc_vol = v
            poc_key = k
    min_key, max_key = keys[0], keys[-1]

    target = total * va_frac
    acc = poc_vol
    up = dn = poc_key
    guard = 0
    max_iter = (max_key - min_key) + 2
    while acc < target and guard < max_iter:
        can_up = up + 1 <= max_key
        can_dn = dn - 1 >= min_key
        if not can_up and not can_dn:
            break
        vol_up = hist.get(up + 1, 0.0) if can_up else 0.0
        vol_dn = hist.get(dn - 1, 0.0) if can_dn else 0.0
        if can_up and (not can_dn or vol_up >= vol_dn):
            up += 1
            acc += vol_up
        else:
            dn -= 1
            acc += vol_dn
        guard += 1

    return ((up + 0.5) * step, (poc_key + 0.5) * step, (dn + 0.5) * step)


def session_value_areas(
    df1m: pd.DataFrame,
    tick: float,
    rows_per_tick: int = 5,
    va_pct: float = 70.0,
    continuous_only: bool = True,
) -> pd.DataFrame:
    """One (vah, poc, val) row per session, indexed by session date.

    `df1m` holds in-session 1-minute bars (Open/High/Low/Close/Volume, IST index). A
    session with no volume yields NaN, which prev_session_value_area carries over.
    By default the profile uses the continuous market only (bars before 15:15), so NSE's
    closing auction (15:15-15:35 for F&O stocks since 2026-08-03) cannot pin the POC to the
    close and old and new sessions stay comparable. `continuous_only=False` includes the
    auction; it exists for measurement.
    """
    if continuous_only:
        df1m = continuous_session(df1m)
    step = tick * rows_per_tick
    days = pd.Series(df1m.index.date, index=df1m.index)
    rows = {}
    for day, chunk in df1m.groupby(days.values, sort=True):
        hist = histogram(
            chunk["Low"].to_numpy(),
            chunk["High"].to_numpy(),
            chunk["Volume"].to_numpy(),
            tick,
            rows_per_tick,
        )
        rows[day] = finalize_profile(hist, va_pct / 100.0, step)
    return pd.DataFrame.from_dict(rows, orient="index", columns=["vah", "poc", "val"])


def prev_session_value_area(
    df1m: pd.DataFrame,
    index: pd.DatetimeIndex,
    tick: float,
    rows_per_tick: int = 5,
    va_pct: float = 70.0,
    continuous_only: bool = True,
) -> pd.DataFrame:
    """PVAH / PPOC / PVAL for every timestamp in `index`.

    Each bar reads the latest COMPLETED earlier session with a profile. A session with no
    data keeps the older value area, as the Pine does (it only updates when the profile is
    non-empty). NaN until a first session has completed.
    """
    sessions = session_value_areas(df1m, tick, rows_per_tick, va_pct, continuous_only)
    dates = pd.Index(index.date)
    # unique(): the shift below is one SESSION, so the timeline must hold each date once.
    timeline = sessions.index.union(dates.unique()).sort_values()
    prev = sessions.reindex(timeline).shift(1).ffill()
    prev.columns = ["pvah", "ppoc", "pval"]
    out = prev.reindex(dates)
    out.index = index
    return out
