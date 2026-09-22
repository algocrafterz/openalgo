"""EMA-20 pullback continuation - backtest adapter (candidate, not yet run).

Source: stockkhoj.in (free NSE/BSE screener), swing-scan category. Its rule, as stated
on the marketing page, is a one-liner - "pullback to 20 EMA during uptrend" - with no
entry trigger, stop, or target specified. Everything below the one-liner is this
codebase's own design, written so the idea is testable rather than argued about, same
approach as gap_rsi.py.

THE IDEA
    Standard trend-following continuation entry (IBD/Minervini "buy the first
    pullback"): a stock in a confirmed uptrend that has already shown strength pulls
    back toward its rising 20 EMA, then a green reversal bar shows buyers stepping back
    in before the medium-term trend (50 EMA) is broken. Buy the next open, sell if the
    trend actually breaks. This is a LONG-ONLY CNC/delivery swing, matching how RSI2
    mean-reversion (signal_engine/pinescripts/swing/rsi-tp-mr/) is already traded -
    India's T+1 settlement makes swing shorting in cash impractical.

WHAT STOCKKHOJ DOES NOT SPECIFY (each is an input here, not a constant)
    - how far price must have run above the 20 EMA to count as "extended" -> ext_pct
    - how close the pullback must come to the 20 EMA                     -> touch_band_pct
    - how the reversal is confirmed (a specific candle shape)            -> close_position_pct
    - the stop and target                                                -> stop_mode, tp_r
    - what "uptrend" means precisely                                     -> ema_mid/ema_slow alignment
    - whether the run-up must have consolidated before the pullback,
      rather than diving straight from new-high to dip                  -> require_base, base_lookback_bars,
                                                                            base_max_range_pct
    - whether an already-extended move should be excluded               -> max_ext_pct

FOLDED IN FROM signal_engine/pinescripts/ideas/ideas-evaluated.txt, idea #4 (9/21 EMA cross + base +
pullback + defend + breakout, reviewed 2026-09-22)
    That idea's own thesis is that an EMA cross alone is not tradeable - what separates its
    "valid setup" example from its two "skip" examples is (a) a tight base/consolidation
    forming after the move and before the pullback, and (b) the move not having already run
    too far before the pullback (its "second cross - too late" example is +37% run-up before
    the entry trigger, all the upside gone). Both are directly applicable here since this
    strategy already has the identical shape (extension -> pullback -> reversal) with neither
    filter: `require_base`/`base_lookback_bars`/`base_max_range_pct` add the consolidation
    check, `max_ext_pct` caps the extension so an already-exhausted move is excluded. Both
    default OFF/uncapped (no behavior change to the shipped config) so the ablation, once this
    strategy is finally backtested, can show whether they help.

REGIME FILTER, ADDED 2026-09-22 AFTER THE FIRST BACKTEST
    The first run confirmed the risk this doc's "Known risks" section named: shipped
    defaults are significantly profitable IS (2019-12 to 2023-12, the post-COVID bull
    run) and significantly LOSING OOS (2023-12 onward) - a genuine regime split, not
    noise (both windows individually clear |t|>1.97). `use_regime` reuses the same
    Nifty-50-vs-its-50-EMA gate `ib_extension.py` already ships (`load_regime()`,
    imported rather than duplicated) so a long is only taken when the index itself is
    not in a KNOWN downtrend. Default `False` - no change to the already-tested shipped
    baseline - so the next ablation can show whether it actually narrows the IS/OOS gap
    rather than just being assumed to.

WHY IT MIGHT COMPLEMENT THE EXISTING BOOK
    RSI2 mean-reversion (signal_engine/pinescripts/swing/rsi-tp-mr/) buys weakness
    against a longer-term trend and holds a few days. This buys STRENGTH after a
    shallow dip and is meant to be held while the trend runs - a different regime bet
    (trend continuation vs. mean reversion), not a variant of the same edge.

STATUS: Candidate. No .pine exists - this may never need one, since RSI2 already
proves a Python-only swing setup can run through signal_engine without a PineScript
front end. NOT backtested yet: blocked on a 10y Historify daily pull for the F&O
universe. See STRATEGY-ANALYSIS.md in
signal_engine/pinescripts/swing/ema-pullback/ for the full writeup.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from signal_engine.backtest import indicators as ind
from signal_engine.backtest.strategies.ib_extension import load_regime
from signal_engine.backtest.strategies.swing_base import SwingStrategy
from signal_engine.backtest.types import Ctx, EntrySignal, Position


@dataclass(frozen=True)
class EmaPullbackParams:
    # ---- trend definition ----------------------------------------------
    ema_fast: int = 20                  # the EMA being pulled back to
    ema_mid: int = 50                   # medium-term trend / stop reference
    ema_slow: int = 200                 # long-term uptrend filter
    require_fast_above_mid: bool = False  # stricter: also require EMA20 > EMA50

    # ---- "was extended" filter ------------------------------------------
    ext_pct: float = 3.0                # close must have cleared EMA20 by this % ...
    lookback_bars: int = 10             # ... at least once in the N bars BEFORE the pullback
    max_ext_pct: float = 0.0            # idea #4: cap the run-up so an already-exhausted move
                                         # (e.g. +37% before the pullback) is excluded; 0 = uncapped

    # ---- "tight base" filter (idea #4: cross -> base -> pullback -> defend -> breakout) ----
    require_base: bool = False          # off by default - no behavior change until ablated in
    base_lookback_bars: int = 5         # sessions immediately before the pullback bar ...
    base_max_range_pct: float = 4.0     # ... whose High-Low range (as % of EMA20) must stay
                                         # within this band for the run to count as "based"

    # ---- pullback + reversal bar -----------------------------------------
    touch_band_pct: float = 1.0         # low must come within this % ABOVE EMA20
    pullback_floor_pct: float = 3.0     # low must not undercut EMA50 by more than this %
    close_position_pct: float = 50.0    # close must sit in the top X% of the day's range

    # ---- risk and exit ---------------------------------------------------
    stop_mode: str = "pullback_low"     # pullback_low | tighter_of_low_and_ema50
    sl_buffer_pct: float = 0.3          # extra room below the stop level
    tp_r: float = 0.0                   # 0 = open-ended (ride the trend exit)
    trend_exit_buffer_pct: float = 1.0  # close below EMA_mid * (1 - this%) exits
    trail_to_breakeven_r: float = 1.0   # 0/None disables; R multiple that arms breakeven

    # ---- filters -----------------------------------------------------
    min_price: float = 50.0             # penny prints make the % bands meaningless

    # ---- regime gate (added after the first backtest confirmed a bull-market bet) ----
    use_regime: bool = False            # off = shipped-baseline behavior, unchanged


class EmaPullback(SwingStrategy):
    name = "EMA-20 pullback continuation (stockkhoj.in swing scan)"
    tag = "EMAPB"
    pine = ""  # Python-only candidate; no PineScript port exists (or may ever be needed)

    def __init__(self, regime: pd.Series | None = None) -> None:
        # Same Nifty-50-vs-50-EMA regime `ib_extension.py` already loads, reused rather
        # than duplicated. Loaded once per instance regardless of p.use_regime, same as
        # IbExtension - `regime=<series>` stays available for a caller that wants to
        # isolate the filter with a specific series; `p.use_regime=False` (the shipped
        # default here) is the on/off switch actually used at entry time.
        self.regime = load_regime() if regime is None else regime

    # ---- preparation -----------------------------------------------------

    def prepare(self, df: pd.DataFrame, p: EmaPullbackParams) -> pd.DataFrame:
        d = df[["Open", "High", "Low", "Close", "Volume"]].dropna().copy()
        d["day"] = pd.Series(d.index, index=d.index).dt.date

        ema_f = ind.ema(d["Close"], p.ema_fast)
        ema_m = ind.ema(d["Close"], p.ema_mid)
        ema_s = ind.ema(d["Close"], p.ema_slow)

        uptrend = (d["Close"] > ema_s) & (ema_m > ema_s)
        if p.require_fast_above_mid:
            uptrend &= ema_f > ema_m

        # extension must predate the pullback bar itself: shift(1) drops today off the
        # window, then look back lookback_bars sessions before that.
        ext_pct_in_window = (
            ((d["Close"] - ema_f) / ema_f * 100.0)
            .shift(1).rolling(p.lookback_bars, min_periods=1).max()
        )
        was_extended = (ext_pct_in_window >= p.ext_pct).fillna(False)
        too_extended = (p.max_ext_pct > 0) & (ext_pct_in_window > p.max_ext_pct).fillna(False)
        extension_ok = was_extended & ~too_extended

        # idea #4's "base": the base_lookback_bars sessions immediately BEFORE the pullback
        # bar must have stayed inside a tight band, rather than running straight into the dip.
        base_hh = d["High"].rolling(p.base_lookback_bars, min_periods=1).max()
        base_ll = d["Low"].rolling(p.base_lookback_bars, min_periods=1).min()
        base_width_pct = (base_hh - base_ll) / ema_f * 100.0
        base_ok = (base_width_pct.shift(1) <= p.base_max_range_pct).fillna(False) if p.require_base \
            else pd.Series(True, index=d.index)

        touched = (d["Low"] <= ema_f * (1 + p.touch_band_pct / 100.0)) & (
            d["Low"] >= ema_m * (1 - p.pullback_floor_pct / 100.0)
        )
        day_range = (d["High"] - d["Low"]).replace(0, np.nan)
        close_in_upper_range = (d["Close"] - d["Low"]) >= (p.close_position_pct / 100.0) * day_range
        green_reversal = (d["Close"] > d["Open"]) & (d["Close"] > ema_f) & close_in_upper_range.fillna(False)

        pullback_bar = uptrend & extension_ok & base_ok & touched & green_reversal

        d["ema_f"] = ema_f
        d["ema_m"] = ema_m
        d["ema_s"] = ema_s
        # everything entry_at_open() reads must be as of the last CLOSED bar (i-1)
        d["p_signal"] = pullback_bar.shift(1, fill_value=False)
        d["p_low"] = d["Low"].shift(1)
        d["p_ema_m"] = ema_m.shift(1)

        # load_regime() already shifts by one day (Nifty regime as of D-1's close), so
        # mapping it onto day D's bar is itself the no-lookahead read entry_at_open needs.
        if self.regime is not None:
            r = self.regime.copy()
            r.index = pd.to_datetime(r.index).date
            d["regime"] = d["day"].map(r).fillna(0.0)      # +1 bull, -1 bear, 0 unknown
        else:
            d["regime"] = 0.0

        return d.dropna(subset=["ema_f", "ema_m", "ema_s"])

    def prepare_key(self, p: EmaPullbackParams) -> tuple:
        return (
            p.ema_fast, p.ema_mid, p.ema_slow, p.require_fast_above_mid,
            p.ext_pct, p.lookback_bars, p.max_ext_pct, p.touch_band_pct, p.pullback_floor_pct,
            p.close_position_pct, p.require_base, p.base_lookback_bars, p.base_max_range_pct,
        )

    # ---- entry -------------------------------------------------------------

    def entry_at_open(self, c: Ctx, i: int, p: EmaPullbackParams, direction: int):
        if direction != 1:
            return None  # long-only: no cash-market swing shorting (T+1 settlement)
        if i < 1 or not c["p_signal"][i]:
            return None
        # 0 = unknown regime (prepare()'s fillna(0.0), or a date load_regime() has no
        # coverage for) and must PASS, not block - only a KNOWN bearish regime rejects.
        if p.use_regime and c["regime"][i] == -1.0:
            return None

        op = c["Open"][i]
        p_low, p_ema_m = c["p_low"][i], c["p_ema_m"][i]
        if not np.isfinite(op) or op < p.min_price:
            return None

        stop_level = p_low if p.stop_mode == "pullback_low" else min(p_low, p_ema_m)
        sl = stop_level * (1 - p.sl_buffer_pct / 100.0)
        risk = op - sl
        if risk <= 0:
            return None

        tp = op + p.tp_r * risk if p.tp_r > 0 else np.inf
        return EntrySignal(direction=1, sl=float(sl), tp=float(tp), tag="EMA_PULLBACK")

    def reset_symbol(self, p) -> None:
        self._be_armed = False

    def on_entry(self, c: Ctx, i: int, p, sig: EntrySignal) -> None:
        self._be_armed = False

    # ---- exit ---------------------------------------------------------------

    def trail(self, c: Ctx, i: int, p: EmaPullbackParams, pos: Position) -> float | None:
        """Ratchet the stop to breakeven once the trade has moved trail_to_breakeven_r."""
        if not p.trail_to_breakeven_r or self._be_armed:
            return None
        gain_r = (c["Close"][i] - pos.entry) / pos.risk
        if gain_r >= p.trail_to_breakeven_r:
            self._be_armed = True
            return pos.entry
        return None

    def custom_exit(self, c: Ctx, i: int, p: EmaPullbackParams, pos: Position) -> str | None:
        """Trend-break exit: close below EMA_mid means the continuation thesis failed."""
        if c["Close"][i] < c["ema_m"][i] * (1 - p.trend_exit_buffer_pct / 100.0):
            return "TREND_BREAK"
        return None
