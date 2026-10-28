"""Multi-day balance breakout (IDEA-034) - the shared rules.

The backtest adapter (backtest/strategies/balance_breakout.py) and the forward paper recorder
(analysis/idea034_paper.py) both call these functions; neither holds a rule of its own (G0.1).

THE IDEA
    A stock whose last N completed sessions form a balance (overlapping 70 percent value
    areas, composite range narrow against the daily ATR) has agreed on value. Trade the first
    ACCEPTED close outside that composite range, after the initial balance and before 13:00.
    Every level is computed from completed prior sessions, so it is known at 09:15.

RULES (pre-registered in research/ideas/idea-034-multi-day-balance-breakout/idea.md)
    Daily profile: 5m bars of the continuous session, each bar's volume spread evenly over the
    price bins its High-Low range touches; bin = 0.05 percent of the session mean close (floored
    at the 0.05 tick); POC is the fullest bin; the value area grows from the POC one bin at a
    time toward the side whose next two bins hold more volume until 70 percent is inside.
    Balance on day t: adjacent value areas of the last N sessions overlap by at least
    `min_va_overlap` of the narrower one, and composite height H = max(VAH) - min(VAL) is at
    most `atr_max_multiple` x daily ATR14 (mean true range of the 14 sessions before t).
    Trigger "p30": a 30-minute period close (anchored 09:15, ending 10:15 .. 12:45) beyond the
    composite edge while the previous 30-minute close was not. Trigger "two5": two consecutive 5m
    closes beyond the edge (both closing at or after 10:15, the second by 13:00) with the close
    before them inside. First breakout of the day only. v2: H at least `min_height_pct` percent
    of the broken edge.
    Stop: "edge" = edge -/+ `stop_buffer_atr` x ATR, or "mid" = composite midpoint.
    Target: the broken edge projected by H. Time exit 14:45.

v3 ENTRY (research/ideas/idea-034-multi-day-balance-breakout/PAPER-PLAN.md)
    A limit at the broken edge, live for 15 minutes from the signal close; it fills only when a
    1m bar trades at least one tick through it. `simulate_limit` models that fill and the exits on
    1m bars; `simulate_market` is the same signal entered at market, for the missed-trade check.

PLACEBOS (not part of the strategy; they isolate what each ingredient adds)
    "non_balance": same trigger on the same composite range when H is above the balance cap.
    "random_entry": on balance days, a random signal slot and side (seeded by symbol and day),
    with the stop and target distances the real rule would use, measured from the signal close.

TIMESTAMPS: frames carry an IST index stamped with each bar's open time, plus `day` and `mins`
session columns (backtest.indicators.add_session_columns).
"""

from __future__ import annotations

import datetime as dt
import zlib
from dataclasses import dataclass

import numpy as np
import pandas as pd

TICK = 0.05
SESSION_OPEN_MIN = 9 * 60 + 15
CONTINUOUS_END_MIN = 15 * 60 + 15
#: Last bar of each 30-minute period (anchored at 09:15) opens at offset 25 within the period.
PERIOD_MIN = 30
FIRST_SIGNAL_CLOSE_MIN = 10 * 60 + 15
LAST_SIGNAL_CLOSE_MIN = 13 * 60
BAR_MIN = 5
TIME_EXIT = dt.time(14, 45)
LIMIT_WINDOW_MIN = 15


@dataclass(frozen=True)
class BalanceParams:
    balance_days: int = 5
    atr_max_multiple: float = 1.5
    value_area_pct: float = 70.0
    bin_pct_of_price: float = 0.05
    min_va_overlap: float = 0.5
    atr_len: int = 14
    stop_buffer_atr: float = 0.25
    min_session_bars: int = 60
    trigger: str = "p30"  # p30 | two5
    stop_mode: str = "edge"  # edge | mid
    placebo: str = "none"  # none | non_balance | random_entry
    min_height_pct: float = 1.5  # v2: H as percent of the broken edge; 0.0 reproduces v1


def value_area(
    high: np.ndarray,
    low: np.ndarray,
    volume: np.ndarray,
    mean_close: float,
    bin_pct: float = 0.05,
    va_pct: float = 70.0,
) -> tuple[float, float, float] | None:
    """(VAL, VAH, POC) of one session from its bars, or None when it holds no volume.

    Each bar's volume is spread over the bins its range touches in proportion to overlap; a
    zero-range bar goes entirely to the bin that holds its price.
    """
    lo, hi = float(np.min(low)), float(np.max(high))
    width = max(TICK, bin_pct / 100.0 * mean_close)
    nb = max(1, int(np.ceil((hi - lo) / width - 1e-9)))
    edges = lo + width * np.arange(nb + 1)
    ov = np.clip(
        np.minimum(high[:, None], edges[None, 1:]) - np.maximum(low[:, None], edges[None, :-1]),
        0.0,
        None,
    )
    span = high - low
    flat = span <= 0
    w = np.zeros_like(ov)
    ok = ~flat
    w[ok] = ov[ok] / span[ok][:, None]
    if flat.any():
        idx = np.clip(((low[flat] - lo) // width).astype(int), 0, nb - 1)
        w[np.flatnonzero(flat), idx] = 1.0
    profile = (w * volume[:, None]).sum(axis=0)
    total = float(profile.sum())
    if not np.isfinite(total) or total <= 0:
        return None
    poc = int(np.argmax(profile))
    a = b = poc
    acc = float(profile[poc])
    target = va_pct / 100.0 * total
    while acc < target - 1e-12:
        up = float(profile[b + 1 : b + 3].sum()) if b + 1 < nb else -1.0
        dn = float(profile[max(a - 2, 0) : a].sum()) if a - 1 >= 0 else -1.0
        if up < 0 and dn < 0:
            break
        if up >= dn:
            b += 1
            acc += float(profile[b])
        else:
            a -= 1
            acc += float(profile[a])
    return float(edges[a]), float(edges[b + 1]), float((edges[poc] + edges[poc + 1]) / 2.0)


def daily_table(d: pd.DataFrame, p: BalanceParams) -> pd.DataFrame:
    """One row per usable session: val, vah, poc, hi, lo, cl (continuous bars only)."""
    cont = d[d["mins"] < CONTINUOUS_END_MIN]
    rows = []
    days = cont["day"].to_numpy()
    if len(days) == 0:
        return pd.DataFrame(columns=["val", "vah", "poc", "hi", "lo", "cl"])
    cut = np.flatnonzero(days[1:] != days[:-1]) + 1
    starts = np.concatenate([[0], cut])
    ends = np.concatenate([cut, [len(days)]])
    hi_a, lo_a = cont["High"].to_numpy(float), cont["Low"].to_numpy(float)
    cl_a, vo_a = cont["Close"].to_numpy(float), cont["Volume"].to_numpy(float)
    for s, e in zip(starts, ends, strict=True):
        if e - s < p.min_session_bars:
            continue
        va = value_area(
            hi_a[s:e],
            lo_a[s:e],
            vo_a[s:e],
            float(cl_a[s:e].mean()),
            p.bin_pct_of_price,
            p.value_area_pct,
        )
        if va is None:
            continue
        rows.append((days[s], *va, hi_a[s:e].max(), lo_a[s:e].min(), cl_a[e - 1]))
    out = pd.DataFrame(rows, columns=["day", "val", "vah", "poc", "hi", "lo", "cl"])
    return out.set_index("day")


def balance_levels(tab: pd.DataFrame, p: BalanceParams) -> pd.DataFrame:
    """For each session t: composite high/low of the N sessions BEFORE t, its overlap test and
    height in daily ATR. Every value at row t uses rows before t only."""
    n = p.balance_days
    prev_cl = tab["cl"].shift(1)
    tr = pd.concat(
        [tab["hi"] - tab["lo"], (tab["hi"] - prev_cl).abs(), (tab["lo"] - prev_cl).abs()], axis=1
    ).max(axis=1)
    tr.iloc[0] = np.nan  # no previous close: not a true range
    atr = tr.rolling(p.atr_len, min_periods=p.atr_len).mean()
    width = tab["vah"] - tab["val"]
    inter = np.minimum(tab["vah"], tab["vah"].shift(1)) - np.maximum(
        tab["val"], tab["val"].shift(1)
    )
    narrow = np.minimum(width, width.shift(1)).replace(0, np.nan)
    overlap = (inter / narrow).clip(lower=-1.0)
    out = pd.DataFrame(index=tab.index)
    out["bal_hi"] = tab["vah"].shift(1).rolling(n, min_periods=n).max()
    out["bal_lo"] = tab["val"].shift(1).rolling(n, min_periods=n).min()
    out["ov_min"] = overlap.shift(1).rolling(n - 1, min_periods=n - 1).min()
    out["atr_d"] = atr.shift(1)
    out["bal_h"] = out["bal_hi"] - out["bal_lo"]
    out["h_atr"] = out["bal_h"] / out["atr_d"].replace(0, np.nan)
    return out


def _gate(bars: pd.DataFrame, p: BalanceParams) -> pd.Series:
    ok = bars["h_atr"].notna() & bars["ov_min"].notna()
    if p.placebo == "non_balance":
        return ok & (bars["h_atr"] > p.atr_max_multiple)
    return ok & (bars["ov_min"] >= p.min_va_overlap) & (bars["h_atr"] <= p.atr_max_multiple)


def _raw_triggers(d: pd.DataFrame, p: BalanceParams) -> tuple[pd.Series, pd.Series]:
    """Long and short breakout flags per bar, before the balance gate and first-of-day rule."""
    day, mins, close = d["day"], d["mins"], d["Close"]
    hi, lo = d["bal_hi"], d["bal_lo"]
    if p.trigger == "p30":
        ends = ((mins - SESSION_OPEN_MIN) % PERIOD_MIN) == (PERIOD_MIN - BAR_MIN)
        in_win = (mins + BAR_MIN >= FIRST_SIGNAL_CLOSE_MIN) & (
            mins + BAR_MIN <= LAST_SIGNAL_CLOSE_MIN
        )
        pc = close.where(ends)
        filled = pc.groupby(day).ffill()
        prev = filled.groupby(day).shift(1)
        mask = ends & in_win
        return (mask & (close > hi) & (prev <= hi)), (mask & (close < lo) & (prev >= lo))
    if p.trigger == "two5":
        c1 = close.groupby(day).shift(1)
        c2 = close.groupby(day).shift(2)
        mask = (mins >= FIRST_SIGNAL_CLOSE_MIN) & (mins + BAR_MIN <= LAST_SIGNAL_CLOSE_MIN)
        return (
            mask & (close > hi) & (c1 > hi) & (c2 <= hi),
            mask & (close < lo) & (c1 < lo) & (c2 >= lo),
        )
    raise ValueError(f"unknown trigger {p.trigger!r}")


def _random_triggers(d: pd.DataFrame, gate: pd.Series, symbol: str) -> tuple[pd.Series, pd.Series]:
    """Placebo: one random signal slot and side per balance day, seeded by symbol and day."""
    slots = np.array([FIRST_SIGNAL_CLOSE_MIN - BAR_MIN + PERIOD_MIN * k for k in range(6)])
    long_ = np.zeros(len(d), dtype=bool)
    short_ = np.zeros(len(d), dtype=bool)
    day, mins = d["day"].to_numpy(), d["mins"].to_numpy()
    for dy in pd.unique(day[gate.to_numpy()]):
        rng = np.random.default_rng(zlib.crc32(f"{symbol}|{dy}".encode()))
        slot, side = int(rng.integers(0, 6)), bool(rng.integers(0, 2))
        hit = np.flatnonzero((day == dy) & (mins == slots[slot]))
        if len(hit):
            (long_ if side else short_)[hit[0]] = True
    return pd.Series(long_, index=d.index), pd.Series(short_, index=d.index)


def mark_signals(d: pd.DataFrame, tab: pd.DataFrame, p: BalanceParams, symbol: str) -> pd.DataFrame:
    """Attach the balance levels and the signal columns (sig_dir, sig_sl, sig_tp) to `d`.

    `d` is a 5m session frame with day/mins columns and `tab` its daily_table. A signal sits on
    the bar whose close is the trigger; the order is placed when that bar closes.
    """
    lv = balance_levels(tab, p)
    for col in lv.columns:
        d[col] = d["day"].map(lv[col])
    gate = _gate(d, p)
    if p.placebo == "random_entry":
        rg = d["ov_min"].notna() & d["h_atr"].notna()
        rg &= (d["ov_min"] >= p.min_va_overlap) & (d["h_atr"] <= p.atr_max_multiple)
        long_, short_ = _random_triggers(d, rg, symbol)
    else:
        long_, short_ = _raw_triggers(d, p)
        long_, short_ = long_ & gate, short_ & gate
    if p.min_height_pct > 0:
        h = d["bal_h"] * 100.0
        long_ &= (h / d["bal_hi"]) >= p.min_height_pct
        short_ &= (h / d["bal_lo"]) >= p.min_height_pct
    any_sig = long_ | short_
    first = any_sig & (any_sig.astype(int).groupby(d["day"]).cumsum() == 1)
    long_, short_ = long_ & first, short_ & first
    buf = p.stop_buffer_atr * d["atr_d"]
    mid = (d["bal_hi"] + d["bal_lo"]) / 2.0
    if p.placebo == "random_entry":
        sl_l, tp_l = d["Close"] - buf, d["Close"] + d["bal_h"]
        sl_s, tp_s = d["Close"] + buf, d["Close"] - d["bal_h"]
    else:
        sl_l = mid if p.stop_mode == "mid" else d["bal_hi"] - buf
        sl_s = mid if p.stop_mode == "mid" else d["bal_lo"] + buf
        tp_l, tp_s = d["bal_hi"] + d["bal_h"], d["bal_lo"] - d["bal_h"]
    d["sig_dir"] = np.where(long_, 1, np.where(short_, -1, 0)).astype(np.int8)
    d["sig_sl"] = np.where(long_, sl_l, np.where(short_, sl_s, np.nan))
    d["sig_tp"] = np.where(long_, tp_l, np.where(short_, tp_s, np.nan))
    return d


# --- v3: limit entry and exits on 1-minute bars ---------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """One signal's modelled result. gross_bps is signed by direction, in bps of the fill."""

    filled: bool
    fill_time: pd.Timestamp | None = None
    fill_price: float | None = None
    exit_time: pd.Timestamp | None = None
    exit_price: float | None = None
    reason: str = ""
    gross_bps: float | None = None


def round_to_tick(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 2)


def _walk_exit(
    bars: pd.DataFrame, i0: int, s: int, stop: float, target: float, time_exit: dt.time
) -> tuple[pd.Timestamp, float, str]:
    """Exit from row i0 (the fill bar, already open). Stop is checked before the target; the
    fill bar honours only its stop (worst case). Returns (exit time, exit price, reason)."""
    o, h, lo = (
        bars["Open"].to_numpy(float),
        bars["High"].to_numpy(float),
        bars["Low"].to_numpy(float),
    )
    idx = bars.index
    for k in range(i0, len(bars)):
        if idx[k].time() >= time_exit:
            return idx[k], float(o[k]), "TIME"
        if k > i0 and ((s > 0 and o[k] <= stop) or (s < 0 and o[k] >= stop)):
            return idx[k], float(o[k]), "SL"
        if (s > 0 and lo[k] <= stop) or (s < 0 and h[k] >= stop):
            return idx[k], float(stop), "SL"
        if k == i0:
            continue
        if (s > 0 and h[k] >= target) or (s < 0 and lo[k] <= target):
            gap = (s > 0 and o[k] > target) or (s < 0 and o[k] < target)
            return idx[k], float(o[k] if gap else target), "TP"
    return idx[-1], float(bars["Close"].iloc[-1]), "EOD"


def _from(bars: pd.DataFrame, start: dt.time) -> int | None:
    after = np.flatnonzero(np.array([t >= start for t in bars.index.time]))
    return int(after[0]) if len(after) else None


def _outcome(bars, i0, s, entry, stop, target, time_exit) -> Outcome:
    t_x, px, why = _walk_exit(bars, i0, s, stop, target, time_exit)
    gross = s * (px - entry) / entry * 1e4
    return Outcome(True, bars.index[i0], float(entry), t_x, px, why, float(gross))


def simulate_limit(
    bars: pd.DataFrame,
    start: dt.time,
    direction: int,
    limit: float,
    stop: float,
    target: float,
    tick: float,
    window_min: int = LIMIT_WINDOW_MIN,
    time_exit: dt.time = TIME_EXIT,
) -> Outcome:
    """A limit placed at `start` (the signal close) on one session's 1m bars.

    It fills on the first bar inside [start, start + window_min) that trades at least one tick
    through the limit, at the limit or at that bar's open when the open is already through it.
    A fill at or beyond the stop exits flat at the fill (SL_GAP_ENTRY): the stop is already
    triggered, and the study's 'exit at the stop price' would book a gain that never existed.
    """
    s = 1 if direction > 0 else -1
    i_start = _from(bars, start)
    if i_start is None:
        return Outcome(False)
    end = (
        dt.datetime.combine(dt.date(2000, 1, 1), start) + dt.timedelta(minutes=window_min)
    ).time()
    lim = round_to_tick(limit, tick)
    for k in range(i_start, len(bars)):
        t = bars.index[k]
        if t.time() >= end:
            break
        lo, hi, op = bars["Low"].iloc[k], bars["High"].iloc[k], bars["Open"].iloc[k]
        through = lo <= lim - tick + 1e-9 if s > 0 else hi >= lim + tick - 1e-9
        if not through:
            continue
        entry = float(min(op, lim) if s > 0 else max(op, lim))
        if s * (entry - stop) <= 0:
            return Outcome(True, t, entry, t, entry, "SL_GAP_ENTRY", 0.0)
        return _outcome(bars, k, s, entry, stop, target, time_exit)
    return Outcome(False)


def simulate_market(
    bars: pd.DataFrame,
    start: dt.time,
    direction: int,
    stop: float,
    target: float,
    time_exit: dt.time = TIME_EXIT,
) -> Outcome:
    """The same signal entered at market: the open of the first 1m bar at or after `start`."""
    s = 1 if direction > 0 else -1
    i0 = _from(bars, start)
    if i0 is None:
        return Outcome(False)
    return _outcome(bars, i0, s, float(bars["Open"].iloc[i0]), stop, target, time_exit)
