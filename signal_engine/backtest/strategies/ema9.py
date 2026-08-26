"""EMA9 intraday - backtest adapter.

Mirrors signal_engine/pinescripts/intraday/ema9/ema9-intraday.pine. The Pine script is
the production artefact; where the two could drift, Pine semantics win.

Defaults here match the Pine defaults, so `Ema9Params()` reproduces exactly what the
published script trades.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from signal_engine.backtest import indicators as ind
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal, Position


@dataclass(frozen=True)
class Ema9Params:
    mode: str = "C"                     # A conventional | B crossover | C pullback | ANY

    fast_len: int = 9
    slow_len: int = 20
    trend_len: int = 200

    confirm_bars: int = 1
    require_open_beyond: bool = False
    require_body_colour: bool = False
    require_close_beyond_prev: bool = False

    use_level_or: bool = True
    use_level_pd: bool = True
    use_level_pivot: bool = True
    or_minutes: int = 15
    pivot_len: int = 5
    break_buffer_atr: float = 0.05
    pullback_max_bars: int = 10
    require_pullback_touch: bool = True

    use_trend_ema: bool = False
    use_htf_bias: bool = False
    htf_mult: int = 3
    use_vwap: bool = True
    use_adx: bool = False
    adx_min: float = 20.0
    use_rsi: bool = False
    rsi_mid: float = 50.0
    use_volume: bool = False
    rvol_mult: float = 1.2
    rvol_days: int = 14

    use_slope: bool = False
    min_slope_atr: float = 0.03
    use_separation: bool = False
    min_sep_atr: float = 0.10
    max_cross_count: int = 0
    cross_lookback: int = 20
    min_atr_pct: float = 0.20

    sl_mode: str = "swing"              # swing | atr | ema
    swing_lookback: int = 8
    sl_atr_mult: float = 1.0
    force_sl_past_ema: bool = True
    tp1_r: float = 2.0

    exit_on_ema_cross: bool = False
    use_ema_trail: bool = False
    exit_on_opposite_cross: bool = True

    def with_(self, **kw) -> "Ema9Params":
        return replace(self, **kw)


class Ema9(Strategy):
    name = "EMA9 intraday"
    tag = "EMA9"
    pine = "signal_engine/pinescripts/intraday/ema9/ema9-intraday.pine"

    def __init__(self):
        self._reset()

    def _reset(self):
        self.brk_dir = 0
        self.brk_level = np.nan
        self.brk_bar = -1
        self.brk_src = ""
        self.brk_touched = False
        self.pb_ext = np.nan

    reset_symbol = lambda self, p: self._reset()      # noqa: E731
    reset_session = lambda self, p: self._reset()     # noqa: E731

    def prepare_key(self, p) -> tuple:
        # Only the fields prepare() actually reads, so an ablation over filter toggles
        # reuses the prepared frame instead of recomputing every indicator.
        return (p.fast_len, p.slow_len, p.trend_len, p.or_minutes, p.pivot_len,
                p.rvol_days, p.cross_lookback, p.swing_lookback, p.htf_mult)

    def prepare(self, df: pd.DataFrame, p: Ema9Params) -> pd.DataFrame:
        df = ind.add_session_columns(df)
        day = df["day"]

        df["ema_f"] = ind.ema(df["Close"], p.fast_len)
        df["ema_s"] = ind.ema(df["Close"], p.slow_len)
        df["ema_t"] = ind.ema(df["Close"], p.trend_len)
        df["atr"] = ind.atr(df)
        df["rsi"] = ind.rsi(df["Close"])
        df["adx"] = ind.adx(df)
        df["adx_rise"] = df["adx"] > df["adx"].shift(1)
        df["vwap"] = ind.session_vwap(df, day)
        df["rvol"] = ind.rvol_time_of_day(df, day, p.rvol_days)

        df["pdh"], df["pdl"] = ind.prev_day_levels(df, day)
        df["orh"], df["orl"] = ind.opening_range(df, day, p.or_minutes)
        df["or_done"] = df["from_open"] >= p.or_minutes
        df["ph"], df["pl"] = ind.last_pivots(df, p.pivot_len)
        df["htf_bull"], df["htf_bear"] = ind.htf_bias(df, p.htf_mult, p.fast_len)

        df["slope"] = (df["ema_f"] - df["ema_f"].shift(1)) / df["atr"].replace(0, np.nan)
        df["sep"] = (df["Close"] - df["ema_f"]).abs() / df["atr"].replace(0, np.nan)
        above = df["Close"] > df["ema_f"]
        df["crosses"] = (above != above.shift(1)).rolling(p.cross_lookback).sum()
        df["atr_pct"] = df["atr"] / df["Close"] * 100
        df["swing_lo"] = df["Low"].rolling(p.swing_lookback).min()
        df["swing_hi"] = df["High"].rolling(p.swing_lookback).max()

        x_up = (df["ema_f"] > df["ema_s"]) & (df["ema_f"].shift(1) <= df["ema_s"].shift(1))
        x_dn = (df["ema_f"] < df["ema_s"]) & (df["ema_f"].shift(1) >= df["ema_s"].shift(1))
        df["x_up"], df["x_dn"] = x_up, x_dn
        df["bars_since_xup"] = ind.bars_since(x_up)
        df["bars_since_xdn"] = ind.bars_since(x_dn)
        return df.dropna(subset=["atr", "ema_f"])

    # ---- Mode C: break, then wait for the pullback -----------------------

    def on_bar(self, c: Ctx, i: int, p: Ema9Params) -> None:
        if p.mode not in ("C", "ANY") or i == 0:
            return
        atr_i, close, ema_f = c["atr"][i], c["Close"], c["ema_f"]
        buf = p.break_buffer_atr * atr_i

        if self.brk_dir == 0:
            cands = []
            if p.use_level_or and c["or_done"][i]:
                cands += [(c["orh"][i], 1, "ORH"), (c["orl"][i], -1, "ORL")]
            if p.use_level_pd:
                cands += [(c["pdh"][i], 1, "PDH"), (c["pdl"][i], -1, "PDL")]
            if p.use_level_pivot:
                cands += [(c["ph"][i], 1, "PIVH"), (c["pl"][i], -1, "PIVL")]
            for lvl, d, src in cands:
                if np.isnan(lvl):
                    continue
                # A close BEYOND the level by the buffer, where the prior bar was not.
                # The buffer is what separates a break from a wick-through.
                broke = (close[i] > lvl + buf and close[i - 1] <= lvl + buf) if d == 1 else \
                        (close[i] < lvl - buf and close[i - 1] >= lvl - buf)
                if broke:
                    self.brk_dir, self.brk_level, self.brk_bar, self.brk_src = d, lvl, i, src
                    self.brk_touched, self.pb_ext = False, np.nan
                    break
            return

        if i - self.brk_bar > p.pullback_max_bars:
            self._reset()
            return
        if i <= self.brk_bar:
            # Only AFTER the break bar. A breakout candle's own low very often sits on
            # the fast EMA, and counting that would mark the pullback complete before
            # any pullback happened - collapsing break/retrace/enter into a plain break.
            return
        if self.brk_dir == 1:
            if c["Low"][i] <= ema_f[i]:
                self.brk_touched = True
            if self.brk_touched:
                self.pb_ext = c["Low"][i] if np.isnan(self.pb_ext) else min(self.pb_ext, c["Low"][i])
        else:
            if c["High"][i] >= ema_f[i]:
                self.brk_touched = True
            if self.brk_touched:
                self.pb_ext = c["High"][i] if np.isnan(self.pb_ext) else max(self.pb_ext, c["High"][i])

    # ---- entry -----------------------------------------------------------

    def _confirmed(self, c: Ctx, i: int, p: Ema9Params, is_long: bool) -> bool:
        """The candle-confirmation rule. confirm_bars=1 is the video as taught."""
        if i - p.confirm_bars + 1 < 1:
            return False
        o, h, low, close, ema_f = c["Open"], c["High"], c["Low"], c["Close"], c["ema_f"]
        for k in range(p.confirm_bars):
            j = i - k
            if (close[j] <= ema_f[j]) if is_long else (close[j] >= ema_f[j]):
                return False
        if p.require_open_beyond and ((o[i] <= ema_f[i]) if is_long else (o[i] >= ema_f[i])):
            return False
        if p.require_body_colour and ((close[i] <= o[i]) if is_long else (close[i] >= o[i])):
            return False
        if p.require_close_beyond_prev and ((close[i] <= h[i - 1]) if is_long else (close[i] >= low[i - 1])):
            return False
        return True

    def entry(self, c: Ctx, i: int, p: Ema9Params, direction: int) -> EntrySignal | None:
        is_long = direction == 1
        close, ema_f, ema_s, ema_t, atr_i = c["Close"], c["ema_f"], c["ema_s"], c["ema_t"], c["atr"][i]

        # shared gates
        if p.use_adx and not (c["adx"][i] >= p.adx_min and c["adx_rise"][i]):
            return None
        if p.use_volume and not (np.isnan(c["rvol"][i]) or c["rvol"][i] >= p.rvol_mult):
            return None
        if p.use_separation and not c["sep"][i] >= p.min_sep_atr:
            return None
        if p.max_cross_count and not c["crosses"][i] <= p.max_cross_count:
            return None
        if c["atr_pct"][i] < p.min_atr_pct:
            return None

        # directional filters
        if p.use_trend_ema and not ((close[i] > ema_t[i]) if is_long else (close[i] < ema_t[i])):
            return None
        if p.use_htf_bias and not (c["htf_bull"][i] if is_long else c["htf_bear"][i]):
            return None
        if p.use_vwap and not ((close[i] > c["vwap"][i]) if is_long else (close[i] < c["vwap"][i])):
            return None
        if p.use_rsi and not ((c["rsi"][i] > p.rsi_mid) if is_long else (c["rsi"][i] < p.rsi_mid)):
            return None
        if p.use_slope and not ((c["slope"][i] >= p.min_slope_atr) if is_long
                                else (c["slope"][i] <= -p.min_slope_atr)):
            return None
        if not self._confirmed(c, i, p, is_long):
            return None

        mode_a, mode_b, mode_c = p.mode in ("A", "ANY"), p.mode in ("B", "ANY"), p.mode in ("C", "ANY")
        tag = ""
        if mode_c and self.brk_dir == direction and (self.brk_touched or not p.require_pullback_touch):
            tag = f"C-{self.brk_src}"
        elif mode_b and ((c["bars_since_xup"][i] <= p.confirm_bars and ema_f[i] > ema_s[i]) if is_long
                         else (c["bars_since_xdn"][i] <= p.confirm_bars and ema_f[i] < ema_s[i])):
            tag = "B-CROSS"
        elif mode_a:
            j = i - p.confirm_bars
            if (close[j] <= ema_f[j]) if is_long else (close[j] >= ema_f[j]):
                tag = "A-CONV"
        if not tag:
            return None

        # stop: anchored on structure, then forced past the fast EMA
        anchor_lo, anchor_hi = c["swing_lo"][i], c["swing_hi"][i]
        if mode_c and self.brk_dir == direction and not np.isnan(self.pb_ext):
            anchor_lo = min(anchor_lo, self.pb_ext)
            anchor_hi = max(anchor_hi, self.pb_ext)
        if p.sl_mode == "atr":
            sl = close[i] - atr_i * p.sl_atr_mult if is_long else close[i] + atr_i * p.sl_atr_mult
        elif p.sl_mode == "ema":
            sl = ema_f[i] - atr_i * p.sl_atr_mult if is_long else ema_f[i] + atr_i * p.sl_atr_mult
        else:
            sl = anchor_lo - atr_i * p.sl_atr_mult if is_long else anchor_hi + atr_i * p.sl_atr_mult
        if p.force_sl_past_ema:
            pad = atr_i * 0.05
            sl = min(sl, ema_f[i] - pad) if is_long else max(sl, ema_f[i] + pad)

        risk = abs(close[i] - sl)
        if risk <= 0 or np.isnan(risk):
            return None
        tp = close[i] + risk * p.tp1_r if is_long else close[i] - risk * p.tp1_r
        return EntrySignal(direction=direction, sl=float(sl), tp=float(tp), tag=tag)

    def on_entry(self, c: Ctx, i: int, p: Ema9Params, sig: EntrySignal) -> None:
        if self.brk_dir == sig.direction:
            self._reset()      # the setup is consumed; it must not arm a second entry

    # ---- exits -----------------------------------------------------------

    def trail(self, c: Ctx, i: int, p: Ema9Params, pos: Position) -> float | None:
        if not p.use_ema_trail:
            return None
        moved = (c["High"][i] - pos.entry) if pos.direction == 1 else (pos.entry - c["Low"][i])
        if moved < pos.risk:
            return None       # arm only once the trade is 1R in favour
        off = c["atr"][i] * p.sl_atr_mult
        return c["ema_f"][i] - off if pos.direction == 1 else c["ema_f"][i] + off

    def custom_exit(self, c: Ctx, i: int, p: Ema9Params, pos: Position) -> str | None:
        long_pos = pos.direction == 1
        if p.exit_on_ema_cross and ((c["Close"][i] < c["ema_f"][i]) if long_pos
                                    else (c["Close"][i] > c["ema_f"][i])):
            return "EMA_CROSS"
        if p.exit_on_opposite_cross and p.mode in ("B", "ANY") and (c["x_dn"][i] if long_pos else c["x_up"][i]):
            return "OPPOSITE_CROSS"
        return None
