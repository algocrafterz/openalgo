"""Multi-day balance breakout (IDEA-034) - backtest adapter.

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
    before them inside. First breakout of the day only. Fill at the next 5m open.
    Stop: "edge" = edge -/+ `stop_buffer_atr` x ATR, or "mid" = composite midpoint.
    Target: the broken edge projected by H. Time exit 14:45 (engine).

PLACEBOS (not part of the strategy; they isolate what each ingredient adds)
    "non_balance": same trigger on the same composite range when H is above the balance cap.
    "random_entry": on balance days, a random signal slot and side (seeded by symbol and day),
    with the stop and target distances the real rule would use, measured from the signal close.

TIMESTAMPS: the index is IST (add_session_columns). Bars are stamped with their open time.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass

import numpy as np
import pandas as pd

from signal_engine.backtest import indicators as ind
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal

TICK = 0.05
SESSION_OPEN_MIN = 9 * 60 + 15
CONTINUOUS_END_MIN = 15 * 60 + 15
#: Last bar of each 30-minute period (anchored at 09:15) opens at offset 25 within the period.
PERIOD_MIN = 30
FIRST_SIGNAL_CLOSE_MIN = 10 * 60 + 15
LAST_SIGNAL_CLOSE_MIN = 13 * 60
BAR_MIN = 5


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


class BalanceBreakout(Strategy):
    name = "balance_breakout - first accepted breakout of a multi-day value-area balance"
    tag = "BALBRK"
    pine = ""
    run_overrides = {
        "skip_open_minutes": 0,
        "entry_cutoff_min": LAST_SIGNAL_CLOSE_MIN,
        "max_trades_per_day": 1,
        "skip_fill_beyond_levels": True,
    }

    def __init__(self) -> None:
        self._tables: dict = {}

    def prepare_key(self, p: BalanceParams) -> tuple:
        return tuple(sorted(vars(p).items()))

    def prepare(self, df: pd.DataFrame, p: BalanceParams) -> pd.DataFrame:
        return self.prepare_for("", df, p)

    def _table(self, symbol: str, d: pd.DataFrame, p: BalanceParams) -> pd.DataFrame:
        key = (
            symbol,
            len(d),
            d.index[0],
            d.index[-1],
            p.value_area_pct,
            p.bin_pct_of_price,
            p.min_session_bars,
        )
        if symbol and key in self._tables:
            return self._tables[key]
        tab = daily_table(d, p)
        if symbol:
            self._tables = {k: v for k, v in self._tables.items() if k[0] == symbol}
            self._tables[key] = tab
        return tab

    def prepare_for(self, symbol: str, df: pd.DataFrame, p: BalanceParams) -> pd.DataFrame:
        d = ind.add_session_columns(df)
        n = len(d)
        d["sig_dir"] = np.zeros(n, dtype=np.int8)
        d["sig_sl"] = np.nan
        d["sig_tp"] = np.nan
        tab = self._table(symbol, d, p)
        if len(tab) <= p.balance_days + p.atr_len:
            return d
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

    def entry(self, c: Ctx, i: int, p: BalanceParams, direction: int) -> EntrySignal | None:
        if int(c["sig_dir"][i]) != direction:
            return None
        sl, tp = float(c["sig_sl"][i]), float(c["sig_tp"][i])
        if not (np.isfinite(sl) and np.isfinite(tp)):
            return None
        return EntrySignal(direction=direction, sl=sl, tp=tp, tag=self.tag)
