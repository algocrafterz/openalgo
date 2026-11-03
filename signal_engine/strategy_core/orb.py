"""ORB15 opening-range breakout, ported from the Pine build that TradingView ran for paper alerts.

Source of truth: `orb.pine` as it stood before commit c2a127d4a (ORB15 enabled, ATR multiple 2.0,
stop always pushed past the ORB edge, target floored at 0.8R, flat SMA(50) volume baseline). That
build produced every ORB paper alert from 2026-09-22 to 2026-10-27. This module reproduces its
ENTRY rules and stop/target arithmetic and nothing else; exits are the runner's and the tracker's.

RULES (5-minute bars, IST, one entry per symbol per session)

  range     high/low of the bars opening 09:15, 09:20, 09:25. Frozen from the 09:30 bar.
  day gates the gap from the previous session close is at most 2.5%; the range is 0.4% to 3.5%
            of its midpoint.
  breakout  a bar opening 09:35 or later closes beyond high x 1.005 (long) or low x 0.995 (short)
            while the previous close was not beyond that level. Both are rising edges on the close.
  volume    the larger of this bar's volume and the two before it is at least 1.2 x SMA(volume, 50).
            The SMA is flat over the last 50 five-minute bars, across sessions. No SMA yet: pass.
  veto      the breakout is dropped when two or more of these oppose it: the close against the
            session VWAP, and the daily close against its EMA(20) by at least 2.0%. The Pine has a
            third vote, NIFTY against its own VWAP; the strategy interface carries one symbol's
            bars, so that vote is not cast (see INDEX_VOTE_MODELLED).
  entry     the NEXT bar. Its open time must be 09:45 or later and before 11:00. Entry price is
            that bar's open. The signal goes out after that bar closes, as the Pine alert did.
            A pending entry that fails the time gate is dropped and the session takes no trade.
  stop      ATR(14) x 2.0 x a price or volatility factor, pushed past the ORB edge by
            max(0.3 ATR, 0.15 ORB width), and never closer than the minimum stop distance.
  targets   tp1 = entry +/- max(ORB width, 0.8 x risk); tp1.5, tp2, tp3 at 1.5, 2 and 3 x that
            distance.

Pure: no I/O, no clock. The pipeline strategy and the backtest adapter call this module.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from signal_engine.features.price import atr, ema, session_vwap

#: The NIFTY-versus-VWAP vote of the Pine is not modelled: the strategy sees one symbol's bars. The
#: Pine builds that vote from `ta.vwap` on an index with no volume, which is probably undefined, so
#: it probably never voted. Unverified; the paper comparison against the Pine alerts settles it.
INDEX_VOTE_MODELLED = False

SESSION_OPEN_MIN = 9 * 60 + 15

# Skip reasons, one per rule, so a missing signal can be explained.
SKIP_NO_RANGE = "NO_RANGE"
SKIP_GAP = "GAP"
SKIP_RANGE_WIDTH = "RANGE_WIDTH"
SKIP_VOLUME = "VOLUME"
SKIP_VETO = "VETO"
SKIP_ENTRY_TIME = "ENTRY_TIME"
SKIP_NO_ENTRY_BAR = "NO_ENTRY_BAR"


@dataclass(frozen=True)
class OrbParams:
    orb_minutes: int = 15
    breakout_buffer_pct: float = 0.5
    vol_ma_len: int = 50
    vol_mult: float = 1.2
    vol_recent_bars: int = 3
    gap_max_pct: float = 2.5
    range_min_pct: float = 0.4
    range_max_pct: float = 3.5
    min_entry_min: int = 9 * 60 + 45
    entry_cutoff_min: int = 11 * 60
    atr_len: int = 14
    atr_mult: float = 2.0
    wick_atr: float = 0.3
    wick_orb: float = 0.15
    min_stop_floor_pct: float = 0.003
    tp1_min_rr: float = 0.8
    htf_ema_len: int = 20
    htf_min_strength_pct: float = 2.0
    veto_votes: int = 2
    bar_minutes: int = 5
    #: Bars stamped at or after this minute of the day (the closing auction) are left out of the
    #: volume average. None keeps every bar.
    vol_ma_exclude_from_min: int | None = 15 * 60 + 15


@dataclass(frozen=True)
class OrbEntry:
    bar_time: pd.Timestamp  # open time of the entry bar
    breakout_time: pd.Timestamp  # open time of the bar that closed beyond the level
    direction: int  # 1 long, -1 short
    entry: float  # open of the entry bar
    sl: float
    tp1: float
    tp1_5: float
    tp2: float
    tp3: float
    atr: float
    orb_high: float
    orb_low: float
    votes_against: int


def _min_stop_multiplier(atr_pct: float) -> float:
    if atr_pct > 5:
        return 1.5
    if atr_pct > 3:
        return 1.0
    if atr_pct > 1.5:
        return 0.7
    return 0.5


def stop_loss(
    direction: int, entry: float, atr_value: float, orb_high: float, orb_low: float, p: OrbParams
) -> float:
    """The Pine's calculateStopLoss in "ATR" mode with the ORB-edge push always applied."""
    orb_range = orb_high - orb_low
    valid_atr = orb_range * 0.5 if (not np.isfinite(atr_value) or atr_value == 0) else atr_value
    atr_pct = valid_atr / entry * 100
    min_dist = max(valid_atr * _min_stop_multiplier(atr_pct), entry * p.min_stop_floor_pct)
    wick = max(valid_atr * p.wick_atr, orb_range * p.wick_orb)
    if direction == 1:
        price_adjust = 1.0 if entry < 1000 else 0.7 if entry < 5000 else 0.5
        sl = entry - valid_atr * p.atr_mult * price_adjust
        sl = min(sl, orb_low - wick)
        return min(sl, entry - min_dist)
    vol_adjust = 1.2 if atr_pct > 3 else 1.0 if atr_pct > 1.5 else 0.8
    sl = entry + valid_atr * p.atr_mult * vol_adjust
    sl = max(sl, orb_high + wick)
    return max(sl, entry + min_dist)


def targets(
    direction: int, entry: float, sl: float, orb_high: float, orb_low: float, p: OrbParams
) -> tuple[float, float, float, float]:
    """(tp1, tp1_5, tp2, tp3): tp1 sits max(ORB width, 0.8 x risk) from entry, the rest at 1.5, 2, 3 x."""
    dist = max(orb_high - orb_low, abs(entry - sl) * p.tp1_min_rr)
    return tuple(entry + direction * m * dist for m in (1.0, 1.5, 2.0, 3.0))  # type: ignore[return-value]


@dataclass(frozen=True)
class OrbFrame:
    """Everything evaluate_frame found: accepted entries and the reason each session was skipped."""

    entries: list[OrbEntry]
    skips: dict[tuple[object, str], str]  # (session date, "LONG"/"SHORT"/"-") -> reason


def _volume_sma(df: pd.DataFrame, p: OrbParams) -> pd.Series:
    """SMA of volume over the last `vol_ma_len` bars, optionally leaving the closing auction out."""
    vol = df["Volume"]
    if p.vol_ma_exclude_from_min is not None:
        minute = df.index.hour * 60 + df.index.minute
        kept = vol[minute < p.vol_ma_exclude_from_min]
        return kept.rolling(p.vol_ma_len, min_periods=p.vol_ma_len).mean().reindex(df.index).ffill()
    return vol.rolling(p.vol_ma_len, min_periods=p.vol_ma_len).mean()


def _daily_ema_before(df: pd.DataFrame, n: int) -> dict[object, float]:
    """EMA(n) of daily closes through the PREVIOUS session, keyed by session date."""
    day = pd.Index(df.index.date)
    closes = df["Close"].groupby(day).last()
    smoothed = ema(closes, n)
    prev = smoothed.shift(1)
    return {d: float(v) for d, v in prev.items()}


def evaluate_frame(df: pd.DataFrame, p: OrbParams | None = None) -> OrbFrame:
    """Every ORB entry in `df` (5-minute bars, IST index, Open/High/Low/Close/Volume), oldest first."""
    p = p or OrbParams()
    if df.empty:
        return OrbFrame([], {})
    atr_s = atr(df, p.atr_len)
    vwap_s = session_vwap(df, "Close")
    vol_sma = _volume_sma(df, p)
    prev_ema = _daily_ema_before(df, p.htf_ema_len)
    alpha = 2.0 / (p.htf_ema_len + 1)
    day_keys = pd.Index(df.index.date)
    sessions = list(dict.fromkeys(day_keys))

    entries: list[OrbEntry] = []
    skips: dict[tuple[object, str], str] = {}
    prev_close_of_day: float | None = None
    for d in sessions:
        rows = np.flatnonzero(day_keys == d)
        last_close = float(df["Close"].iloc[rows[-1]])
        entry = _session_entry(
            df, rows, d, atr_s, vwap_s, vol_sma, prev_ema.get(d), alpha, prev_close_of_day, p, skips
        )
        if entry is not None:
            entries.append(entry)
        prev_close_of_day = last_close
    return OrbFrame(entries, skips)


def _session_entry(df, rows, d, atr_s, vwap_s, vol_sma, prev_ema, alpha, prev_close, p, skips):
    # Anchored to 09:15, not to the first bar present: a frame missing the 09:15 bar must not shift
    # the range or the first breakout bar. Bars stamped before 09:15 (pre-open) are not the session.
    open_at = df.index[rows[0]].normalize() + pd.Timedelta(minutes=SESSION_OPEN_MIN)
    rows = rows[((df.index[rows] - open_at) / pd.Timedelta(minutes=1)).to_numpy() >= 0]
    if len(rows) == 0:
        skips[(d, "-")] = SKIP_NO_RANGE
        return None
    idx = df.index[rows]
    mins = ((idx - open_at) / pd.Timedelta(minutes=1)).to_numpy()
    in_range = mins < p.orb_minutes
    if mins[0] != 0 or in_range.sum() < p.orb_minutes // p.bar_minutes:
        skips[(d, "-")] = SKIP_NO_RANGE
        return None
    high = df["High"].to_numpy()[rows]
    low = df["Low"].to_numpy()[rows]
    close = df["Close"].to_numpy()[rows]
    volume = df["Volume"].to_numpy()[rows]
    orb_high, orb_low = float(high[in_range].max()), float(low[in_range].min())
    gap = (
        0.0
        if prev_close is None
        else abs(float(df["Open"].iloc[rows[0]]) - prev_close) / prev_close * 100
    )
    mid = (orb_high + orb_low) / 2
    width_pct = (orb_high - orb_low) / mid * 100
    day_ok_gap = gap <= p.gap_max_pct
    day_ok_range = p.range_min_pct <= width_pct <= p.range_max_pct

    up_level = orb_high * (1 + p.breakout_buffer_pct / 100)
    dn_level = orb_low * (1 - p.breakout_buffer_pct / 100)
    vwap = vwap_s.to_numpy()[rows]
    sma = vol_sma.to_numpy()[rows]
    for i in range(1, len(rows)):
        if mins[i] <= p.orb_minutes:
            continue  # the range completes on the first bar after it; detection starts there
        direction = 0
        if close[i] > up_level and close[i - 1] <= up_level:
            direction = 1
        elif close[i] < dn_level and close[i - 1] >= dn_level:
            direction = -1
        if direction == 0:
            continue
        side = "LONG" if direction == 1 else "SHORT"
        recent = volume[max(0, i - p.vol_recent_bars + 1) : i + 1].max()
        if np.isfinite(sma[i]) and sma[i] > 0 and recent < sma[i] * p.vol_mult:
            skips[(d, side)] = SKIP_VOLUME
            continue
        against = 0
        if np.isfinite(vwap[i]) and (
            close[i] <= vwap[i] if direction == 1 else close[i] >= vwap[i]
        ):
            against += 1
        if prev_ema is not None:
            run_ema = alpha * close[i] + (1 - alpha) * prev_ema
            dist = (close[i] - run_ema) / run_ema * 100
            if abs(dist) >= p.htf_min_strength_pct and (dist < 0 if direction == 1 else dist > 0):
                against += 1
        if against >= p.veto_votes:
            skips[(d, side)] = SKIP_VETO
            continue
        # The breakout is accepted. From here the session takes this trade or none.
        if i + 1 >= len(rows):
            skips[(d, side)] = SKIP_NO_ENTRY_BAR
            return None
        e = i + 1
        open_min = idx[e].hour * 60 + idx[e].minute
        if not (day_ok_gap and day_ok_range):
            skips[(d, side)] = SKIP_GAP if not day_ok_gap else SKIP_RANGE_WIDTH
            return None
        if not (p.min_entry_min <= open_min < p.entry_cutoff_min):
            skips[(d, side)] = SKIP_ENTRY_TIME
            return None
        price = float(df["Open"].to_numpy()[rows][e])
        atr_e = float(atr_s.to_numpy()[rows][e])
        sl = stop_loss(direction, price, atr_e, orb_high, orb_low, p)
        tp1, tp15, tp2, tp3 = targets(direction, price, sl, orb_high, orb_low, p)
        return OrbEntry(
            idx[e],
            idx[i],
            direction,
            price,
            sl,
            tp1,
            tp15,
            tp2,
            tp3,
            atr_e,
            orb_high,
            orb_low,
            against,
        )
    return None
