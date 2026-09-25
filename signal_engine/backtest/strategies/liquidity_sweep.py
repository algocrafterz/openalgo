"""Daily liquidity-sweep engulfing continuation - backtest adapter.

Source: signal_engine/pinescripts/ideas/ideas-evaluated.txt, "strategy:5" (Omar Agag's $500/day
prop-firm setup, Instagram reel). Original framing: 4-hour engulfing candle that sweeps
the prior candle's low marks a buy zone (the lower half of the engulfing candle's body);
drop to the 15-minute chart, wait for price to pull back into that zone with market
structure realigning bullish, enter with a stop below the zone and a 2:1 target.

THE IDEA, ADAPTED FOR NSE DAILY SWING
    This repo's `turtle_soup.py` already proved the underlying mechanism is real on NSE
    data: fading a failed PDH/PDL break beats trading it for continuation (gross t=7.29
    at zero cost), but on 5-minute bars the edge is a few bps against a 16 bps round-trip
    cost - too thin to survive. This adapter tests the SAME mechanism (a stop-hunt that
    reverses) on daily bars instead, where the cost is a smaller fraction of the typical
    move and the edge has more room to matter. Historify does not carry clean 4-hour bars
    for the NSE universe, so the setup is recast one timeframe down on both legs: the
    anchor candle is a DAILY bullish engulfing bar that also undercuts the prior day's
    low (the sweep), and the "15-minute pullback + structure" trigger becomes a
    SUBSEQUENT DAY that dips back into the zone and closes above a recent local high
    (the daily-bar proxy for "market structure realigns bullish").

    Long-only, CNC/delivery swing - matches `ema_pullback.py` and `rsi-tp-mr`'s
    convention (India's T+1 cash settlement makes swing shorting impractical).

WHAT THE ORIGINAL DOES NOT SPECIFY (each is an input here, not a constant)
    - what counts as "sweeps the low" beyond a bare Low < prior Low   -> implicit in the
      engulfing definition below; no separate threshold needed
    - how big the engulfing candle's body must be to be a real signal,
      not noise                                                       -> min_engulf_body_pct
    - how long the buy zone stays valid before being abandoned        -> zone_valid_days
    - what invalidates the zone before it is used                     -> a close back below
      the sweep candle's own low (its stated worst case) kills the setup
    - what "market structure realigns bullish" means precisely        -> structure_lookback
      (close breaks the rolling N-day high made before the pullback)
    - the stop: "below the buy zone / below the sweep low" names TWO
      different levels without picking one                            -> stop_mode
    - the $250-risk/$500-target prop-firm framing does not carry over -
      OpenAlgo sizes swing risk as a % of capital via `risk_per_trade`,
      not a fixed dollar amount on leveraged futures; the 2:1 R:R ratio
      is the only piece of that framing that is strategy-shaped rather
      than account-shaped, so only the ratio is kept (tp_r=2.0)

WHY THIS MIGHT COMPLEMENT THE EXISTING BOOK
    `ema_pullback.py` (candidate, not yet backtested) buys strength after a shallow dip
    in a confirmed uptrend - a trend-continuation bet. `rsi-tp-mr` buys weakness with no
    trend requirement - mean reversion. This is a third, different bet: a single
    stop-hunt-and-reverse EVENT (not a trend state) that then needs a second, independent
    confirmation (the pullback+structure day) before risking capital - closer in spirit
    to `turtle_soup` than to either existing swing strategy, just moved to a timeframe
    where turtle_soup's already-proven directional edge was too small to trade.

STATUS: Candidate, not yet backtested. Needs the same 10-year Historify daily pull the
other swing strategies use. See STRATEGY-ANALYSIS.md in
signal_engine/pinescripts/swing/liquidity-sweep/ for the full writeup; results get
appended there once this runs.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from signal_engine.backtest.strategies.swing_base import SwingStrategy
from signal_engine.backtest.types import Ctx, EntrySignal


@dataclass(frozen=True)
class LiquiditySweepParams:
    # ---- anchor candle: daily bullish engulfing that sweeps the prior low ----------
    min_engulf_body_pct: float = 60.0   # body must be >= this % of the day's H-L range

    # ---- buy zone ----------------------------------------------------------------
    zone_fraction: float = 0.5          # lower X% of the engulfing candle's body

    # ---- zone lifecycle ------------------------------------------------------------
    zone_valid_days: int = 10           # sessions the zone stays watched before expiring
    structure_lookback: int = 10        # window for "recent local high" (structure break)

    # ---- risk and exit --------------------------------------------------------------
    stop_mode: str = "zone_low"         # zone_low | sweep_low
    sl_buffer_pct: float = 0.3          # extra room below the stop level
    tp_r: float = 2.0                   # "2:1 risk-reward" is explicit in the source

    # ---- filters ---------------------------------------------------------------------
    min_price: float = 50.0             # penny prints make the % bands meaningless


class LiquiditySweep(SwingStrategy):
    name = "Daily liquidity-sweep engulfing continuation (Instagram idea #5)"
    tag = "LIQSWEEP"
    pine = ""  # Python-only candidate, same reasoning as ema_pullback.py

    # ---- preparation -------------------------------------------------------------

    def prepare(self, df: pd.DataFrame, p: LiquiditySweepParams) -> pd.DataFrame:
        d = df[["Open", "High", "Low", "Close", "Volume"]].dropna().copy()
        d["day"] = pd.Series(d.index, index=d.index).dt.date

        o = d["Open"].to_numpy()
        h = d["High"].to_numpy()
        low = d["Low"].to_numpy()
        c = d["Close"].to_numpy()
        n = len(d)

        body_hi = np.maximum(o, c)
        body_lo = np.minimum(o, c)
        day_range = h - low
        body_pct = np.zeros(n)
        has_range = day_range > 0
        body_pct[has_range] = (body_hi[has_range] - body_lo[has_range]) / day_range[has_range] * 100.0

        # bullish engulfing candle that also sweeps (dips below) the prior candle's low
        sweep = np.zeros(n, dtype=bool)
        sweep[1:] = (
            (low[1:] < low[:-1])              # the sweep: undercuts the prior low
            & (c[1:] > o[1:])                 # today closed green
            & (o[1:] < c[:-1])                # opened inside/below the prior candle's body
            & (c[1:] > o[:-1])                # closed above it: bullish body engulf
            & (body_pct[1:] >= p.min_engulf_body_pct)
        )

        zone_lo_anchor = body_lo
        zone_hi_anchor = body_lo + p.zone_fraction * (body_hi - body_lo)
        invalid_level = low  # a close back below the sweep candle's own low kills the zone

        # "market structure realigns bullish": today's close breaks the local high made
        # BEFORE the pullback bar - shift(1) so the window never includes today itself.
        swing_high_prior = (
            pd.Series(h).rolling(p.structure_lookback, min_periods=1).max().shift(1).to_numpy()
        )

        z_lo = np.full(n, np.nan)
        z_hi = np.full(n, np.nan)
        z_invalid = np.full(n, np.nan)
        pullback_signal = np.zeros(n, dtype=bool)

        anchor = -1
        a_lo = a_hi = a_invalid = 0.0
        zone_fired = False
        for i in range(n):
            if anchor >= 0:
                age = i - anchor
                if age > p.zone_valid_days or c[i] < a_invalid:
                    anchor = -1  # zone expired, or invalidated by a close back below the sweep low

            if sweep[i]:
                anchor = i
                a_lo, a_hi, a_invalid = zone_lo_anchor[i], zone_hi_anchor[i], invalid_level[i]
                zone_fired = False
                continue  # the anchor day itself is not a pullback-watch day

            if anchor >= 0:
                z_lo[i], z_hi[i], z_invalid[i] = a_lo, a_hi, a_invalid
                if not zone_fired and low[i] <= a_hi and c[i] > swing_high_prior[i]:
                    pullback_signal[i] = True
                    zone_fired = True  # one entry attempt per swept zone

        d["p_signal"] = pd.Series(pullback_signal, index=d.index).shift(1, fill_value=False)
        d["p_zone_lo"] = pd.Series(z_lo, index=d.index).shift(1)
        d["p_sweep_low"] = pd.Series(z_invalid, index=d.index).shift(1)
        return d

    def prepare_key(self, p: LiquiditySweepParams) -> tuple:
        return (p.min_engulf_body_pct, p.zone_fraction, p.zone_valid_days, p.structure_lookback)

    # ---- entry ---------------------------------------------------------------------

    def entry_at_open(self, c: Ctx, i: int, p: LiquiditySweepParams, direction: int):
        if direction != 1:
            return None  # long only: no cash-market swing shorting (T+1 settlement)
        if i < 1 or not c["p_signal"][i]:
            return None

        op = c["Open"][i]
        if not np.isfinite(op) or op < p.min_price:
            return None

        stop_level = c["p_zone_lo"][i] if p.stop_mode == "zone_low" else c["p_sweep_low"][i]
        if not np.isfinite(stop_level):
            return None
        sl = stop_level * (1 - p.sl_buffer_pct / 100.0)
        risk = op - sl
        if risk <= 0:
            return None

        tp = op + p.tp_r * risk if p.tp_r > 0 else np.inf
        return EntrySignal(direction=1, sl=float(sl), tp=float(tp), tag="LIQ_SWEEP")
