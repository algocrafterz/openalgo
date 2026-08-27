"""Dividend/Growth value-zone mean reversion - backtest adapter.

Source: `signal_engine/pinescripts/swing/dividend-growth/dividend-growth.pine`

THE IDEA
    Position price inside its own N-day range. Bottom 30% of the range is the "value
    zone" and is bought; top 30% is the "sell zone" and is sold. A five-component score
    (position 25 / RSI 25 / trend 25 / volume 15 / price action 10) gates the entry.

WHY THIS ADAPTER EXISTS
    The Pine strategy block cannot lose. It carries no stop, and both of its exits are
    conditioned on being in profit:

        if sell_tradeable and is_profitable      -> close
        if in_position and current_profit_pct >= effective_profit_target -> close

    A losing position is therefore never closed; it is carried to the end of the data
    and reported as an "open trade", outside the win rate and outside the P&L. The
    strategy's own analysis doc records this as a fix ("Why Losses Appeared Previously
    ... backtest end forced-close of underwater open positions"). It is not a fix - it
    is the removal of the only mechanism that was reporting the truth. Any win rate that
    block produces is an accounting artifact, not a measurement.

    This adapter runs the same signal through the honest swing engine: stop enforced,
    gap-through priced, holding period capped, and every position closed and counted.

WHAT IS PARAMETERISED RATHER THAN ASSUMED
    - stop_mode      the Pine has none. "none" reproduces that (stop parked 99% away,
                     so max_hold does the closing); the others are real stops.
    - exit_needs_profit  True reproduces the profit-only exit. Default False.
    - use_score      False strips the 5-component score down to the bare "price is in
                     the bottom 30% of its range" factor. If the score is doing work,
                     turning it off must hurt in BOTH windows.

READ gross_bps, NOT net_R, WHEN COMPARING STOP MODES. R is normalised by stop width, so
a 99%-wide stop makes every R microscopic. gross_bps is mean percent return in basis
points and is invariant to the stop; the swing cost line is ~20 bps.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from signal_engine.backtest import indicators as ind
from signal_engine.backtest.strategies.swing_base import SwingStrategy
from signal_engine.backtest.types import Ctx, EntrySignal, Position

#: The two hardcoded lists in the Pine. Kept verbatim so the shipped configuration can
#: be tested as shipped - but note that three names were REMOVED from the growth list
#: after they lost (BIOCON "structural decline", INDUSINDBK "sustained downtrend",
#: ETERNAL "no mean reversion"). Selecting the universe after seeing the outcome is the
#: purest form of overfitting, so the broad-universe result is the one that counts.
DIVIDEND_SYMBOLS = [
    "ITC", "CASTROLIND", "HINDPETRO", "NTPC", "POWERGRID", "COALINDIA", "HINDZINC",
    "PETRONET", "SJVN", "GAIL", "ONGC", "NMDC", "RECLTD",
]
GROWTH_SYMBOLS = [
    "MOTHERSON", "SAIL", "TATASTEEL", "NATIONALUM", "BANKBARODA", "AARTIIND", "BEL",
    "VEDL", "DABUR", "AMBUJACEM", "IRCTC", "INDHOTEL", "MARICO", "DLF", "HINDALCO",
    "JSWSTEEL", "HINDUNILVR", "BAJFINANCE",
]


@dataclass(frozen=True)
class ValueZoneParams:
    # ---- mode ----------------------------------------------------------
    #: "dividend" and "growth" are the two hardcoded parameter sets in the Pine.
    #: The Pine picks between them by symbol-list membership; here it is explicit so
    #: the same setting can be run over the whole universe.
    mode: str = "dividend"

    # ---- range / zones -------------------------------------------------
    lookback: int = 0                 # 0 = mode default (dividend 90, growth 60)
    value_zone: float = 30.0
    deep_value: float = 25.0
    sell_zone: float = 70.0
    extreme_sell: float = 90.0

    # ---- indicators ----------------------------------------------------
    rsi_len: int = 14
    ema_fast: int = 12
    ema_slow: int = 26
    trend_sma: int = 50
    vol_period: int = 30
    vol_surge: float = 1.5

    # ---- gating --------------------------------------------------------
    use_score: bool = True            # False = bare value-zone factor
    entry_score: float = 0.0          # 0 = mode default (dividend 38, growth 50)
    strong_sell_score: float = 0.0    # 0 = mode default (dividend 55, growth 70)
    gap_days: int = 10
    enable_dip: bool = True
    min_price: float = 20.0

    # ---- risk and exit -------------------------------------------------
    stop_mode: str = "none"           # none | pct | atr | range_low
    stop_pct: float = 10.0
    stop_atr_mult: float = 3.0
    atr_len: int = 14
    profit_target_pct: float = 0.0    # 0 = mode default (dividend 10, growth 5)
    exit_on_sell: bool = True
    exit_needs_profit: bool = False   # True reproduces the Pine's profit-only exit

    # ---- derived defaults ----------------------------------------------
    @property
    def _lookback(self) -> int:
        return self.lookback or (90 if self.mode == "dividend" else 60)

    @property
    def _entry_score(self) -> float:
        return self.entry_score or (38.0 if self.mode == "dividend" else 50.0)

    @property
    def _strong_sell(self) -> float:
        return self.strong_sell_score or (55.0 if self.mode == "dividend" else 70.0)

    @property
    def _target(self) -> float:
        return self.profit_target_pct or (10.0 if self.mode == "dividend" else 5.0)


def _step(x: pd.Series, bands: list[tuple[float, float]], above: bool) -> pd.Series:
    """Piecewise-constant score. `bands` is ordered most-extreme first."""
    out = pd.Series(0.0, index=x.index)
    assigned = pd.Series(False, index=x.index)
    for thr, pts in bands:
        hit = ((x >= thr) if above else (x <= thr)).fillna(False)
        out[hit & ~assigned] = pts
        assigned |= hit
    return out


class ValueZone(SwingStrategy):
    name = "Dividend/Growth value-zone mean reversion"
    tag = "VALZONE"
    pine = "signal_engine/pinescripts/swing/dividend-growth/dividend-growth.pine"

    # ---- preparation ---------------------------------------------------

    def prepare(self, df: pd.DataFrame, p: ValueZoneParams) -> pd.DataFrame:
        d = df[["Open", "High", "Low", "Close", "Volume"]].dropna().copy()
        d["day"] = pd.Series(d.index, index=d.index).dt.date
        c, h, low_, o, v = d["Close"], d["High"], d["Low"], d["Open"], d["Volume"]

        n = p._lookback
        ll = low_.rolling(n).min()
        hh = h.rolling(n).max()
        rng = (hh - ll).replace(0, np.nan)
        pos = ((c - ll) / rng * 100.0).fillna(50.0)

        rsi = ind.rsi(c, p.rsi_len)
        ef = ind.ema(c, p.ema_fast)
        es = ind.ema(c, p.ema_slow)
        sma = c.rolling(p.trend_sma).mean()
        vavg = v.rolling(p.vol_period).mean()
        vr = (v / vavg.replace(0, np.nan)).fillna(1.0)
        atr = ind.atr(d, p.atr_len)

        # ---- BUY score, component by component, as in the Pine ---------
        s_pos = _step(pos, [(p.deep_value, 25), (p.value_zone, 18), (45, 8), (60, 3)], above=False)
        s_rsi = _step(rsi, [(25, 25), (30, 22), (35, 20), (45, 16), (55, 12), (65, 6)], above=False)

        cross_up = (ef > es) & (ef.shift(1) <= es.shift(1))
        s_tr = pd.Series(0.0, index=d.index)
        s_tr += np.where(cross_up, 10.0, np.where(ef > es, 6.0, 0.0))
        s_tr += np.where((ef > ef.shift(1)) & (ef.shift(1) > ef.shift(2)), 8.0,
                         np.where(ef > ef.shift(1), 4.0, 0.0))
        s_tr += np.where(c > sma, 7.0, np.where(c > sma * 0.97, 4.0, 0.0))
        s_tr = s_tr.clip(upper=25)

        s_vol = _step(vr, [(2.5, 15), (p.vol_surge, 12), (1.3, 9), (1.1, 6), (0.9, 3)], above=True)

        rngb = (h - low_).replace(0, np.nan)
        body = (c - o).abs()
        s_pa = pd.Series(0.0, index=d.index)
        s_pa += np.where((c > o) & ((c - o) / rngb > 0.6), 4.0, np.where(c > o, 2.0, 0.0))
        s_pa += np.where((low_ >= low_.shift(1)) & (c > c.shift(1)), 3.0,
                         np.where(c > c.shift(1), 2.0, np.where(low_ >= low_.shift(1), 1.0, 0.0)))
        s_pa += np.where((body / rngb < 0.4) & (c > (h + low_) / 2), 3.0, 0.0)

        buy_score = (s_pos + s_rsi + s_tr + s_vol + s_pa).fillna(0.0)

        # ---- SELL score ------------------------------------------------
        z_pos = _step(pos, [(p.extreme_sell, 25), (p.sell_zone, 18), (65, 10), (55, 5)], above=True)
        z_rsi = _step(rsi, [(80, 25), (75, 22), (70, 20), (65, 18), (60, 16), (55, 12), (50, 6)],
                      above=True)
        ef_pct = np.where(ef > 0, (c - ef) / ef * 100.0, 0.0)
        z_tr = pd.Series(0.0, index=d.index)
        z_tr += np.where(ef_pct >= 5, 10.0, np.where(ef_pct >= 3, 7.0, np.where(ef_pct >= 1.5, 4.0, 0.0)))
        z_tr += np.where(c > sma * 1.15, 10.0, np.where(c > sma * 1.10, 7.0,
                         np.where(c > sma * 1.05, 4.0, np.where(c > sma, 2.0, 0.0))))
        z_tr += np.where((c > c.shift(1)) & (c.shift(1) > c.shift(2)) & (c.shift(2) > c.shift(3)),
                         5.0, 0.0)
        z_tr = z_tr.clip(upper=25)
        z_vol = _step(vr, [(2.5, 15), (2.0, 12), (p.vol_surge, 9), (1.2, 6), (0.9, 3)], above=True)
        z_vol = pd.Series(np.where((c < o) & (vr >= 1.5) & (z_vol < 15),
                                   np.minimum(z_vol + 3, 15), z_vol), index=d.index)
        up_sh = h - pd.concat([c, o], axis=1).max(axis=1)
        z_pa = pd.Series(0.0, index=d.index)
        z_pa += np.where((c < o) & ((o - c) / rngb > 0.6), 4.0,
                 np.where(c < o, 2.0,
                 np.where((c > o) & (up_sh / rngb > 0.4), 3.0,
                 np.where(body / rngb < 0.3, 2.0, 0.0))))
        z_pa += np.where((h <= h.shift(1)) & (c < c.shift(1)), 3.0,
                 np.where(c < c.shift(1), 2.0, np.where(h <= h.shift(1), 1.0, 0.0)))
        z_pa += np.where((body / rngb < 0.3) & (up_sh / rngb > 0.6), 3.0, 0.0)
        z_pa = z_pa.clip(upper=10)
        sell_score = (z_pos + z_rsi + z_tr + z_vol + z_pa).fillna(0.0)

        # ---- dip signal (crossunder of deep-value line) ----------------
        dip = ((pos < p.deep_value) & (pos.shift(1) >= p.deep_value)
               & (rsi < 50) & (vr >= 1.3) & (c > o))

        # ---- everything the entry reads must be as of YESTERDAY --------
        d["pos"] = pos
        d["buy_score"] = buy_score
        d["sell_score"] = sell_score
        d["p_pos"] = pos.shift(1)
        d["p_buy"] = buy_score.shift(1)
        d["p_dip"] = dip.astype(float).shift(1).fillna(0.0)
        d["p_close"] = c.shift(1)
        d["p_ll"] = ll.shift(1)
        d["p_atr"] = atr.shift(1)
        d["in_sell_zone"] = (pos >= p.sell_zone).astype(float)
        return d.dropna(subset=["p_pos", "p_buy", "p_close", "p_ll"])

    def prepare_key(self, p: ValueZoneParams) -> tuple:
        return (p._lookback, p.value_zone, p.deep_value, p.sell_zone, p.extreme_sell,
                p.rsi_len, p.ema_fast, p.ema_slow, p.trend_sma, p.vol_period,
                p.vol_surge, p.atr_len)

    # ---- entry ---------------------------------------------------------

    def reset_symbol(self, p) -> None:
        self._last_entry_bar = -10_000

    def on_entry(self, c: Ctx, i: int, p, sig: EntrySignal) -> None:
        self._last_entry_bar = i

    def entry_at_open(self, c: Ctx, i: int, p: ValueZoneParams, direction: int):
        if direction != 1 or i < 1:               # long only - CNC delivery, no shorts
            return None
        if i - self._last_entry_bar < p.gap_days:
            return None
        op = c["Open"][i]
        if not np.isfinite(op) or op < p.min_price:
            return None

        in_zone = c["p_pos"][i] <= p.value_zone
        if p.use_score:
            fires = (in_zone and c["p_buy"][i] >= p._entry_score) or \
                    (p.enable_dip and c["p_dip"][i] > 0)
        else:
            fires = in_zone
        if not fires:
            return None

        if p.stop_mode == "pct":
            sl = op * (1.0 - p.stop_pct / 100.0)
        elif p.stop_mode == "atr":
            a = c["p_atr"][i]
            if not np.isfinite(a):
                return None
            sl = op - p.stop_atr_mult * a
        elif p.stop_mode == "range_low":
            sl = c["p_ll"][i]
        elif p.stop_mode == "none":
            sl = op * 0.01                        # parked out of reach; max_hold closes
        else:
            raise ValueError(f"unknown stop_mode {p.stop_mode!r}")

        tp = op * (1.0 + p._target / 100.0)
        return EntrySignal(direction=1, sl=float(sl), tp=float(tp), tag="VALUE_ZONE")

    # ---- exit ----------------------------------------------------------

    def custom_exit(self, c: Ctx, i: int, p: ValueZoneParams, pos: Position) -> str | None:
        """STRONG sell signal in the sell zone.

        `exit_needs_profit=True` reproduces the Pine, which refuses to act on its own
        sell signal unless the trade happens to be green. That is not a rule, it is a
        way of never booking a loss - included only so the two can be compared.
        """
        if not p.exit_on_sell:
            return None
        if c["in_sell_zone"][i] <= 0 or c["sell_score"][i] < p._strong_sell:
            return None
        if p.exit_needs_profit and c["Close"][i] <= pos.entry:
            return None
        return "SELL_SIGNAL"
