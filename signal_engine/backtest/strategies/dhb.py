"""DHB - "Day High Breakout" gainer-pullback continuation (TradeXPavan) adapter.

Source: `signal_engine/pinescripts/intraday/intraday-dhb.txt`, a discretionary setup
posted to a Telegram channel. The text is a checklist, not a specification, so every
vague term below had to be pinned to a number before it could be tested. Those choices
are the whole result - a different reading of "healthy pullback" is a different
strategy - so each one is stated with its default and swept in the ablation.

THE SETUP, AS WRITTEN                    HOW IT IS MADE TESTABLE HERE
  "Top Gainers, 09:15-10:00"             gain vs previous close >= `min_gain_pct`,
                                         measured at 10:00 and frozen. The genuine
                                         cross-sectional "top N of the universe" read
                                         is applied on top of the trade list by
                                         `top_gainers_by_day()`, since one adapter only
                                         ever sees one symbol.
  "High Trading Volume"                  session volume to that point vs the same clock
                                         slot on prior sessions (RVOL) >= `min_rvol`.
  "Strong Momentum" / "Clean Price       not separately encoded. Momentum is already
   Action"                               the gain filter; "clean" is discretionary and
                                         any proxy would be invented, not derived.
  "Do not select after 10:00"            the day high is FROZEN at 10:00 - that frozen
                                         value is the First Day High (FDH). Entries may
                                         still trigger later.
  "Wait for a healthy pullback"          retrace of the (FDH - pre-FDH session low) leg
                                         must land inside
                                         [`pullback_min_pct`, `pullback_max_pct`].
                                         Too shallow is chasing; too deep is broken.
  "Bullish Engulfing / Hammer /          three explicit 5-minute candle tests, each
   Strong Bullish Rejection"             independently switchable, and the confirmation
                                         must be within `confirm_lookback` bars of the
                                         entry so a stale one cannot arm a trade.
  "Buy after price breaks the FDH"       close > FDH * (1 + `breakout_buffer_pct`/100),
                                         filled at the next bar's open by the engine.
  "Volume should support the breakout"   breakout bar volume >= `vol_mult` x its SMA.
  "Stop below the Latest Swing Low"      lowest low of the last `swing_lookback` bars,
                                         less `sl_buffer_pct`.
  "Target 1: 1:2, Target 2: 1:3, trail"  `tp_r` (2.0). The engine models ONE exit, so a
                                         partial at 1:2 with the rest trailed cannot be
                                         represented; 1:2 full-exit is the closest
                                         honest approximation and is, on this evidence,
                                         the better of the two anyway.

LONG ONLY. The setup is written for top gainers and has no short leg.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from signal_engine.backtest import indicators as ind
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal


@dataclass(frozen=True)
class DhbParams:
    # ---- selection window (09:15 - 10:00) ------------------------------
    #: Minute-of-day at which the day high freezes into the FDH and selection closes.
    select_end_min: int = 10 * 60
    min_gain_pct: float = 1.0
    min_rvol: float = 1.5
    min_price: float = 50.0

    # ---- pullback ------------------------------------------------------
    #: Retrace of the (FDH - pre-FDH session low) leg, in percent.
    pullback_min_pct: float = 20.0
    pullback_max_pct: float = 70.0
    #: Bars after the FDH freezes within which the whole sequence must complete.
    max_bars_wait: int = 60

    # ---- confirmation candle -------------------------------------------
    require_confirm: bool = True
    use_engulf: bool = True
    use_hammer: bool = True
    use_rejection: bool = True
    #: Close must be in the top (1 - this) of the bar's range to count as a rejection.
    rejection_clv: float = 0.70
    confirm_lookback: int = 6

    # ---- entry ---------------------------------------------------------
    breakout_buffer_pct: float = 0.05
    use_volume: bool = True
    vol_ma_len: int = 50
    vol_mult: float = 1.2

    # ---- stop ----------------------------------------------------------
    swing_lookback: int = 10
    sl_buffer_pct: float = 0.05
    #: Reject a setup whose stop is wider than this fraction of price - the position
    #: would be too small to be worth the ticket.
    max_sl_pct: float = 0.025

    # ---- target ---------------------------------------------------------
    tp_r: float = 2.0


class Dhb(Strategy):
    name = "DHB gainer-pullback breakout (TradeXPavan)"
    tag = "DHB"
    pine = "signal_engine/pinescripts/intraday/intraday-dhb.txt"
    #: The setup cannot arm before 10:00 (the FDH freezes there) and is explicitly a
    #: wait-for-the-pullback rule, so the 11:00 entry cutoff that suits ORB would discard
    #: most of its signals. Long only, one trade per symbol per day.
    run_overrides = {"skip_open_minutes": 45, "entry_cutoff_min": 13 * 60 + 30,
                     "time_exit_min": 15 * 60, "max_trades_per_day": 1,
                     "allow_shorts": False}

    def prepare(self, df: pd.DataFrame, p: DhbParams) -> pd.DataFrame:
        d = ind.add_session_columns(df)
        day = d["day"]
        o, h, low, close, vol = d["Open"], d["High"], d["Low"], d["Close"], d["Volume"]

        # ---- FDH: the running session high, frozen at select_end_min ----
        # cummax inside the session up to the cutoff, then held flat for the rest of
        # the day. Nothing after 10:00 can move it, which is the point of the rule.
        in_win = d["mins"] < p.select_end_min
        d["fdh"] = day.map(h.where(in_win).groupby(day).max())
        # Session low made BEFORE the FDH froze - the base of the leg being retraced.
        d["pre_low"] = day.map(low.where(in_win).groupby(day).min())
        d["selected"] = (~in_win).astype(float)

        # ---- gainer test, measured at the cutoff and frozen -------------
        prev_close = day.map(close.groupby(day).last().shift(1))
        last_in_win = day.map(close.where(in_win).groupby(day).last())
        d["gain_pct"] = (last_in_win - prev_close) / prev_close.replace(0, np.nan) * 100.0
        d["rvol"] = ind.rvol_time_of_day(d, day)
        # RVOL frozen at the cutoff too, so a late volume burst cannot retro-select.
        d["rvol_sel"] = day.map(d["rvol"].where(in_win).groupby(day).last())

        # ---- confirmation candles ---------------------------------------
        rng = (h - low).replace(0, np.nan)
        body = (close - o).abs()
        up = close > o
        engulf = up & (close.shift(1) < o.shift(1)) & (close >= o.shift(1)) & (o <= close.shift(1))
        lower_wick = np.minimum(o, close) - low
        hammer = (lower_wick >= 2 * body) & ((h - np.maximum(o, close)) <= body) & (rng > 0)
        clv = (close - low) / rng
        rejection = up & (clv >= p.rejection_clv)

        confirm = pd.Series(False, index=d.index)
        if p.use_engulf:
            confirm |= engulf.fillna(False)
        if p.use_hammer:
            confirm |= hammer.fillna(False)
        if p.use_rejection:
            confirm |= rejection.fillna(False)
        d["confirm"] = confirm.astype(float)

        # ---- volume support for the breakout bar ------------------------
        base = vol.rolling(p.vol_ma_len, min_periods=10).mean()
        d["vol_ratio"] = vol / base.replace(0, np.nan)

        # ---- latest swing low: lowest low of the trailing window --------
        d["swing_low"] = low.rolling(p.swing_lookback, min_periods=2).min()
        return d

    def prepare_key(self, p: DhbParams) -> tuple:
        return (p.select_end_min, p.use_engulf, p.use_hammer, p.use_rejection,
                p.rejection_clv, p.vol_ma_len, p.swing_lookback)

    # ---- per-session state machine -------------------------------------

    def reset_symbol(self, p: DhbParams) -> None:
        self.reset_session(p)

    def reset_session(self, p: DhbParams) -> None:
        self._pb_low = np.nan       # lowest low seen since the FDH froze
        self._pb_ok = False         # a retrace of legal depth has happened
        self._confirm_bar = -10**9  # bar index of the most recent confirmation
        self._fdh_bar = -1

    def on_bar(self, c: Ctx, i: int, p: DhbParams) -> None:
        if c["selected"][i] <= 0:
            return
        if self._fdh_bar < 0:
            self._fdh_bar = i

        low_i, fdh, pre_low = c["Low"][i], c["fdh"][i], c["pre_low"][i]
        self._pb_low = low_i if np.isnan(self._pb_low) else min(self._pb_low, low_i)

        # Depth of the retrace as a fraction of the leg that made the FDH.
        leg = fdh - pre_low
        if leg > 0:
            depth = (fdh - self._pb_low) / leg * 100.0
            if p.pullback_min_pct <= depth <= p.pullback_max_pct:
                self._pb_ok = True
            elif depth > p.pullback_max_pct:
                # Retraced through the base: the leg is gone, not a pullback. Re-arm
                # only if price rebuilds, which it cannot here since the FDH is frozen.
                self._pb_ok = False

        if c["confirm"][i] > 0 and self._pb_ok:
            self._confirm_bar = i

    def entry(self, c: Ctx, i: int, p: DhbParams, direction: int):
        if direction != 1:                       # long only
            return None
        if c["selected"][i] <= 0 or self._fdh_bar < 0:
            return None
        if i - self._fdh_bar > p.max_bars_wait:
            return None

        close, fdh = c["Close"][i], c["fdh"][i]
        if np.isnan(fdh) or close < p.min_price:
            return None
        if not (c["gain_pct"][i] >= p.min_gain_pct):
            return None
        if not (c["rvol_sel"][i] >= p.min_rvol):
            return None
        if not self._pb_ok:
            return None
        if p.require_confirm and (i - self._confirm_bar) > p.confirm_lookback:
            return None

        # Break of the First Day High, on this closed bar.
        if not (close > fdh * (1 + p.breakout_buffer_pct / 100.0)):
            return None
        # ...and it must be the FIRST such close, otherwise every bar of an extended
        # move re-fires the same signal.
        if c["Close"][i - 1] > fdh * (1 + p.breakout_buffer_pct / 100.0):
            return None
        if p.use_volume and not (c["vol_ratio"][i] >= p.vol_mult):
            return None

        sl = c["swing_low"][i] * (1 - p.sl_buffer_pct / 100.0)
        if np.isnan(sl) or sl >= close:
            return None
        risk = close - sl
        if risk / close > p.max_sl_pct:
            return None
        return EntrySignal(direction=1, sl=float(sl), tp=float(close + p.tp_r * risk),
                           tag="DHB")


def top_gainers_by_day(trades, frames, top_n: int = 5,
                       cutoff_min: int = 10 * 60) -> list:
    """Keep only trades on the day's top-`top_n` gainers of the whole universe.

    The rule says "select only Top Gainers", which is a statement ABOUT THE UNIVERSE and
    cannot be expressed inside a per-symbol adapter. The ranking uses the same frozen
    cutoff the adapter does, so nothing after 10:00 informs the selection.
    """
    gains: dict = {}
    for sym, df in frames.items():
        d = ind.add_session_columns(df)
        day = d["day"]
        in_win = d["mins"] < cutoff_min
        prev_close = d["Close"].groupby(day).last().shift(1)
        last_in = d["Close"].where(in_win).groupby(day).last()
        g = (last_in - prev_close) / prev_close.replace(0, np.nan) * 100.0
        for dt, v in g.dropna().items():
            gains.setdefault(dt, []).append((float(v), sym))

    keep = {dt: {s for _, s in sorted(v, reverse=True)[:top_n]} for dt, v in gains.items()}
    return [t for t in trades if t.symbol in keep.get(t.day, set())]
