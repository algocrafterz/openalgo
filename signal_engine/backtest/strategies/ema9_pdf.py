"""The 9-EMA strategy as described in 791187576-9-EMA-Trading-Strategy.pdf.

This is deliberately SEPARATE from `ema9.py`. That adapter mirrors
`ema9-intraday.pine`, the production artefact. This one mirrors the PDF, so the two
can disagree and the disagreement stays visible.

WHAT THE PDF ACTUALLY SPECIFIES
    The document (a HowToTrade.com explainer, 11 pages) is a survey, not one system.
    It describes five crossover variants and then one worked "how to trade" method.
    Only the worked method carries a full rule set:

        1. Mark the previous day's high and low as support/resistance.
        2. A break of either begins a possible new trend.
        3. Wait for price to retrace back to the 9 EMA.
        4. The first ENGULFING candle back in the trend direction is the entry.
        5. Stop beyond that engulfing candle.
        6. Take profit when a DOJI prints after a significant move.

    The five crossover variants (9/30 WMA, 9/20 EMA, 9 EMA x VWAP, 9/21/55, 9/15)
    specify an ENTRY TRIGGER ONLY. The PDF gives them no stop and no target at all, so
    any risk model used to test them is supplied here, not by the document - which is
    itself the most important thing to know about them. They are tested with a swing
    stop and a fixed reward-to-risk, and `tp_r` is swept.

TWO DEFECTS IN THE SOURCE, recorded because they bear on how much weight it carries:
    - p9 ends the worked example with "our entry will be on the first FVG that forms
      ... it must fall within the 10 AM to the 11 AM interval". FVG and a fixed
      10-11 window belong to a different method; neither appears anywhere else in the
      document. The worked example is spliced from another article.
    - The PDF specifies a 1-minute chart. yfinance serves 7 days of 1-minute history
      against 60 days of 5-minute, so the headline run here is on 5-minute bars and a
      separate 1-minute run over the shorter window is reported alongside it.

"Take profit on a doji" is implemented as `custom_exit`: a doji whose body is at most
`doji_body_frac` of its range, once the trade is at least `doji_min_r` in favour ("a
significant price movement"). Without that qualifier the first indecision bar after
entry closes every trade for noise.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from signal_engine.backtest import indicators as ind
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal, Position


@dataclass(frozen=True)
class Ema9PdfParams:
    #: pdf     - the worked method: PD break, 9 EMA pullback, engulfing entry
    #: c930    - 9 EMA / 30 WMA, entry past the retracement candle's extreme
    #: c920    - 9/20 EMA crossover
    #: cvwap   - PD break, then 9 EMA crosses VWAP
    #: c92155  - 9 > 21 > 55 stack, 9x21 cross, entry past the last swing
    #: c915    - 9/15 EMA cross confirmed by an engulfing candle
    variant: str = "pdf"

    fast_len: int = 9
    or_minutes: int = 15

    # ---- worked method --------------------------------------------------
    break_buffer_atr: float = 0.05    # separates a break from a wick-through
    pullback_max_bars: int = 12       # how long a retrace may take before the setup dies
    require_engulf: bool = True       # rule 4, the entry trigger itself
    sl_buffer_atr: float = 0.05       # pad beyond the engulfing candle (rule 5)

    # ---- crossover variants ---------------------------------------------
    slow_len: int = 20                # c920
    wma_len: int = 30                 # c930
    mid_len: int = 21                 # c92155
    trend_len: int = 55               # c92155
    c915_len: int = 15                # c915
    gap_atr: float = 0.10             # c930 "noticeable wide gap"
    swing_lookback: int = 8
    trigger_max_bars: int = 10        # bars allowed between cross and trigger

    # ---- risk supplied here, NOT by the PDF, for the crossover variants --
    sl_atr_mult: float = 0.25
    tp_r: float = 2.0

    # ---- take profit on a doji (rule 6) ---------------------------------
    exit_on_doji: bool = True
    doji_body_frac: float = 0.10      # body <= 10% of the bar's range
    doji_min_r: float = 1.0           # "after a significant price movement"

    min_price: float = 20.0

    def with_(self, **kw) -> Ema9PdfParams:
        return replace(self, **kw)


class Ema9Pdf(Strategy):
    name = "9 EMA strategy, as published in the PDF"
    tag = "EMA9PDF"
    pine = "(none - this mirrors the PDF, not a script)"

    def __init__(self):
        self._reset()

    def _reset(self) -> None:
        self.brk_dir = 0            # worked method: which level broke
        self.brk_bar = -1
        self.brk_src = ""
        self.touched = False        # has price come back to the 9 EMA yet
        self.pb_ext = np.nan        # extreme of the pullback
        self.trig_dir = 0           # crossover variants: cross seen, awaiting trigger
        self.trig_bar = -1
        self.trig_level = np.nan

    reset_symbol = lambda self, p: self._reset()      # noqa: E731
    reset_session = lambda self, p: self._reset()     # noqa: E731

    def prepare_key(self, p) -> tuple:
        return (p.fast_len, p.slow_len, p.wma_len, p.mid_len, p.trend_len,
                p.c915_len, p.or_minutes, p.swing_lookback)

    # ---- preparation ----------------------------------------------------

    def prepare(self, df: pd.DataFrame, p: Ema9PdfParams) -> pd.DataFrame:
        d = ind.add_session_columns(df)
        day = d["day"]
        o, h, low_, c = d["Open"], d["High"], d["Low"], d["Close"]

        d["ema_f"] = ind.ema(c, p.fast_len)
        d["ema_s"] = ind.ema(c, p.slow_len)
        d["ema_m"] = ind.ema(c, p.mid_len)
        d["ema_t"] = ind.ema(c, p.trend_len)
        d["ema_15"] = ind.ema(c, p.c915_len)
        # 9/30 pairs the 9 EMA with a 30-period WEIGHTED moving average, not an EMA.
        w = np.arange(1, p.wma_len + 1, dtype=float)
        d["wma"] = c.rolling(p.wma_len).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)
        d["atr"] = ind.atr(d)
        d["vwap"] = ind.session_vwap(d, day)

        d["pdh"], d["pdl"] = ind.prev_day_levels(d, day)
        d["swing_lo"] = low_.rolling(p.swing_lookback).min()
        d["swing_hi"] = h.rolling(p.swing_lookback).max()

        # ---- engulfing, the PDF's entry trigger --------------------------
        body = (c - o).abs()
        bull, bear = c > o, c < o
        d["eng_bull"] = (bull & bear.shift(1) & (c >= o.shift(1))
                         & (o <= c.shift(1)) & (body > body.shift(1))).astype(float)
        d["eng_bear"] = (bear & bull.shift(1) & (c <= o.shift(1))
                         & (o >= c.shift(1)) & (body > body.shift(1))).astype(float)

        # ---- doji, the PDF's exit trigger --------------------------------
        rng = (h - low_).replace(0, np.nan)
        d["doji"] = (body <= p.doji_body_frac * rng).astype(float)

        # ---- crossovers ---------------------------------------------------
        for tag, a, b in (("920", "ema_f", "ema_s"), ("930", "ema_f", "wma"),
                          ("915", "ema_f", "ema_15"), ("921", "ema_f", "ema_m")):
            x, y = d[a], d[b]
            d[f"xup_{tag}"] = ((x > y) & (x.shift(1) <= y.shift(1))).astype(float)
            d[f"xdn_{tag}"] = ((x < y) & (x.shift(1) >= y.shift(1))).astype(float)
        vw = d["vwap"]
        d["xup_vwap"] = ((d["ema_f"] > vw) & (d["ema_f"].shift(1) <= vw.shift(1))).astype(float)
        d["xdn_vwap"] = ((d["ema_f"] < vw) & (d["ema_f"].shift(1) >= vw.shift(1))).astype(float)
        return d.dropna(subset=["atr", "ema_f"])

    # ---- state machine ---------------------------------------------------

    def on_bar(self, c: Ctx, i: int, p: Ema9PdfParams) -> None:
        if i == 0:
            return
        if p.variant in ("pdf", "cvwap"):
            self._track_break(c, i, p)
        elif p.variant in ("c930", "c92155"):
            self._track_trigger(c, i, p)

    def _track_break(self, c: Ctx, i: int, p: Ema9PdfParams) -> None:
        """Rules 1-3: previous-day level breaks, then price retraces to the 9 EMA."""
        close, ema_f = c["Close"], c["ema_f"]
        buf = p.break_buffer_atr * c["atr"][i]
        if self.brk_dir == 0:
            for lvl, dirn, src in ((c["pdh"][i], 1, "PDH"), (c["pdl"][i], -1, "PDL")):
                if not np.isfinite(lvl):
                    continue
                broke = (close[i] > lvl + buf and close[i - 1] <= lvl + buf) if dirn == 1 else \
                        (close[i] < lvl - buf and close[i - 1] >= lvl - buf)
                if broke:
                    self.brk_dir, self.brk_bar, self.brk_src = dirn, i, src
                    self.touched, self.pb_ext = False, np.nan
                    return
            return
        if i - self.brk_bar > p.pullback_max_bars:
            self._reset()
            return
        if i <= self.brk_bar:
            return      # the break bar's own low often sits on the EMA; that is no retrace
        if self.brk_dir == 1:
            if c["Low"][i] <= ema_f[i]:
                self.touched = True
            if self.touched:
                self.pb_ext = c["Low"][i] if np.isnan(self.pb_ext) else min(self.pb_ext, c["Low"][i])
        else:
            if c["High"][i] >= ema_f[i]:
                self.touched = True
            if self.touched:
                self.pb_ext = c["High"][i] if np.isnan(self.pb_ext) else max(self.pb_ext, c["High"][i])

    def _track_trigger(self, c: Ctx, i: int, p: Ema9PdfParams) -> None:
        """9/30 and 9/21/55 both arm on a cross, then wait for a price trigger.

        9/30:    the retracement candle is the one that crosses back through the 9 EMA
                 against the trend; the trigger is a close beyond ITS extreme.
        9/21/55: the trigger is a close beyond the last swing high / low.
        """
        close, ema_f = c["Close"], c["ema_f"]
        if p.variant == "c930":
            up = c["ema_f"][i] > c["wma"][i] and (c["ema_f"][i] - c["wma"][i]) >= p.gap_atr * c["atr"][i]
            dn = c["ema_f"][i] < c["wma"][i] and (c["wma"][i] - c["ema_f"][i]) >= p.gap_atr * c["atr"][i]
            if up and c["Low"][i] <= ema_f[i] and close[i] < c["Open"][i]:
                self.trig_dir, self.trig_bar, self.trig_level = 1, i, c["High"][i]
            elif dn and c["High"][i] >= ema_f[i] and close[i] > c["Open"][i]:
                self.trig_dir, self.trig_bar, self.trig_level = -1, i, c["Low"][i]
        else:
            stack_up = c["ema_f"][i] > c["ema_m"][i] > c["ema_t"][i]
            stack_dn = c["ema_f"][i] < c["ema_m"][i] < c["ema_t"][i]
            if c["xup_921"][i] > 0 and stack_up:
                self.trig_dir, self.trig_bar, self.trig_level = 1, i, c["swing_hi"][i]
            elif c["xdn_921"][i] > 0 and stack_dn:
                self.trig_dir, self.trig_bar, self.trig_level = -1, i, c["swing_lo"][i]
        if self.trig_dir and i - self.trig_bar > p.trigger_max_bars:
            self.trig_dir, self.trig_bar, self.trig_level = 0, -1, np.nan

    # ---- entry -----------------------------------------------------------

    def entry(self, c: Ctx, i: int, p: Ema9PdfParams, direction: int) -> EntrySignal | None:
        if i < 2:
            return None
        px, atr_i = c["Close"][i], c["atr"][i]
        if not np.isfinite(px) or px < p.min_price or not np.isfinite(atr_i) or atr_i <= 0:
            return None
        long_ = direction == 1
        eng = (c["eng_bull"][i] if long_ else c["eng_bear"][i]) > 0

        tag = ""
        sl = np.nan
        if p.variant == "pdf":
            # rules 2-5: level broke, price came back to the 9 EMA, engulfing candle
            if self.brk_dir != direction or not self.touched:
                return None
            if p.require_engulf and not eng:
                return None
            if (px <= c["ema_f"][i]) if long_ else (px >= c["ema_f"][i]):
                return None
            ext = c["Low"][i] if long_ else c["High"][i]
            if np.isfinite(self.pb_ext):
                ext = min(ext, self.pb_ext) if long_ else max(ext, self.pb_ext)
            sl = ext - direction * atr_i * p.sl_buffer_atr      # rule 5
            tag = f"PDF-{self.brk_src}"

        elif p.variant == "cvwap":
            if self.brk_dir != direction:
                return None
            if not ((c["xup_vwap"][i] > 0) if long_ else (c["xdn_vwap"][i] > 0)):
                return None
            tag = f"VWAP-{self.brk_src}"

        elif p.variant == "c920":
            if not ((c["xup_920"][i] > 0) if long_ else (c["xdn_920"][i] > 0)):
                return None
            tag = "9/20"

        elif p.variant == "c915":
            if not ((c["xup_915"][i] > 0) if long_ else (c["xdn_915"][i] > 0)):
                return None
            if p.require_engulf and not eng:
                return None
            tag = "9/15"

        elif p.variant in ("c930", "c92155"):
            if self.trig_dir != direction or not np.isfinite(self.trig_level):
                return None
            # entry is a CLOSE beyond the armed trigger level
            if (px <= self.trig_level) if long_ else (px >= self.trig_level):
                return None
            tag = "9/30" if p.variant == "c930" else "9/21/55"
        else:
            raise ValueError(f"unknown variant {p.variant!r}")

        # The PDF gives the crossover variants no stop. A swing stop padded by ATR is
        # supplied here so they can be measured at all.
        if not np.isfinite(sl):
            anchor = c["swing_lo"][i] if long_ else c["swing_hi"][i]
            sl = anchor - direction * atr_i * p.sl_atr_mult
        risk = abs(px - sl)
        if risk <= 0 or not np.isfinite(risk):
            return None
        return EntrySignal(direction=direction, sl=float(sl),
                           tp=float(px + direction * risk * p.tp_r), tag=tag)

    def on_entry(self, c: Ctx, i: int, p, sig: EntrySignal) -> None:
        self._reset()       # the setup is consumed and must not arm a second entry

    # ---- exit ------------------------------------------------------------

    def custom_exit(self, c: Ctx, i: int, p: Ema9PdfParams, pos: Position) -> str | None:
        """Rule 6: take profit on a doji, once the move has been significant."""
        if not p.exit_on_doji or not c["doji"][i] > 0:
            return None
        moved = (c["Close"][i] - pos.entry) if pos.direction == 1 else (pos.entry - c["Close"][i])
        return "DOJI" if moved >= pos.risk * p.doji_min_r else None
