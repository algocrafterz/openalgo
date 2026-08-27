"""Opening Range Breakout (Luxy variant) - backtest adapter.

Source: `signal_engine/pinescripts/intraday/orb/orb.pine` (the live strategy).
`breakout.pine` is the same file plus a key-level engine; with ORB-only triggers the
two are identical, so this adapter covers the ORB path of both.

MECHANICS, as read from the Pine (defaults in parentheses)

    ORB          first N minutes of the session (ORB15 = 09:15-09:30). ORB5/30/60 are
                 present but disabled; `orb_minutes` sweeps them.
    Pre-gates    gap from prev close <= 2.5%; ORB range / ORB mid in [0.4%, 3.5%];
                 no entry before 09:45 or after 11:00; ONE entry per session.
    Breakout     close crosses ORB High * (1 + buffer) - buffer is a percentage OF THE
                 LEVEL, and the shipped default is 0.5%, not the 0.1% the tooltip calls
                 "recommended". On a 1500-rupee stock that is 7.5 rupees of give-up
                 before the trade is even armed.
    Volume       max(vol, vol[1], vol[2]) >= SMA(vol, 50) * 1.2. The 3-bar window is
                 deliberate - momentum often builds before the level breaks.
    Direction    a 2-of-3 veto. Trend (price vs session VWAP), HTF (daily close vs its
                 20 EMA, needing 2% separation) and Index (NIFTY vs its VWAP) each cast
                 a vote; the entry is blocked only when TWO or more oppose it.
    Fill         next bar's OPEN. The Pine arms a pending entry and fills `open` on the
                 following bar, which is exactly the engine's next-bar-fill semantics.
    Stop         `stop_mode`. Every mode is floored by a minimum stop distance that
                 scales with ATR% and never goes below 0.3% of price.
    Target       TP1 = entry +/- max(ORB width, 0.8 * risk). Note this is anchored to
                 the ORB MEASURED MOVE, not to R - so R:R varies trade by trade. Live
                 execution is TP1 only, full exit (`strategy.exit` uses tp1).
    Exit         time exit 15:00 IST, else EOD.

WHAT THIS ADAPTER CANNOT SETTLE
    The live record (Telegram, Q1 2026: 145 entries, 67.3% WR, +0.280%/trade) is real
    forward evidence and outranks any backtest. yfinance gives 59 sessions of 5-minute
    bars, which is a DIFFERENT and much shorter window than that record. Where the two
    disagree the live record wins on the question "did this make money"; the backtest is
    useful for the questions the live record cannot answer - which gate is carrying the
    result, and how sensitive it is to the parameters nobody has swept.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from signal_engine.backtest import indicators as ind
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal


@dataclass(frozen=True)
class OrbParams:
    # ---- opening range -------------------------------------------------
    orb_minutes: int = 15             # ORB15 is the only stage enabled in the Pine

    # ---- pre-gates -----------------------------------------------------
    use_gap_filter: bool = True
    gap_max_pct: float = 2.5
    use_orb_range_filter: bool = True
    orb_range_min_pct: float = 0.4    # ORB range as % of ORB MID
    orb_range_max_pct: float = 3.5
    min_entry_min: int = 9 * 60 + 45  # 09:45 IST
    min_price: float = 20.0

    # ---- breakout ------------------------------------------------------
    #: Percent OF THE ORB LEVEL added before a break counts. Shipped default 0.5.
    breakout_buffer_pct: float = 0.5

    # ---- volume --------------------------------------------------------
    use_volume: bool = True
    vol_ma_len: int = 50
    vol_mult: float = 1.2
    strong_vol_mult: float = 1.8
    #: "sma3" is the Pine's max-of-3-bars vs a flat 50-bar SMA. "rvol" swaps the
    #: denominator for the same clock slot on prior sessions.
    vol_mode: str = "sma3"

    # ---- 2-of-3 directional veto ---------------------------------------
    use_trend: bool = True            # session VWAP
    use_htf: bool = True              # daily close vs 20 EMA, >= 2% apart
    htf_ema_len: int = 20
    htf_min_strength: float = 2.0
    use_index: bool = True            # NIFTY vs its session VWAP
    #: How many opposing votes block the entry. 2 is the Pine. 1 = require unanimity.
    veto_threshold: int = 2

    # ---- stop ----------------------------------------------------------
    stop_mode: str = "atr"            # atr | smart | scaled | orb_pct | pct
    #: The Pine's `sl := math.min(sl, orbLow - wickBuf)`. It keeps the stop outside the
    #: opening range so a wick back into it cannot stop the trade out - but because the
    #: entry already sits `breakout_buffer_pct` ABOVE the range, it forces
    #: risk >= buffer + ORB width + wick on every trade, which is what drives R:R to
    #: the tp_risk_floor. Turning it off is the core of the R:R fix.
    use_orb_stop_floor: bool = False
    atr_len: int = 14
    atr_mult: float = 3.0             # "ATR" mode only
    orb_stop_frac: float = 20.0       # "orb_pct" mode, % of ORB range beyond the edge
    pct_stop: float = 1.0             # "pct" mode

    # ---- target --------------------------------------------------------
    #: "orb" is the Pine: TP1 = max(ORB width, risk * tp_risk_floor), so the ORB measured
    #: move sets the target and R:R varies trade to trade. "r" pins the target to a fixed
    #: multiple of risk instead, which is the only way to actually CHOOSE an R:R.
    tp_mode: str = "orb"              # orb | r
    tp_r: float = 1.5                 # target in R, used when tp_mode="r"
    tp_mult: float = 1.0              # TP1. 1.5 / 2.0 / 3.0 are the other shipped rungs
    #: TP1 distance = max(ORB width, risk * tp_risk_floor). Must be >= 1.0: below that
    #: the "floor" caps reward under risk. Was 0.8 in the Pine until the R:R fix.
    tp_risk_floor: float = 1.5


def _min_stop_distance(entry: float, atr: float) -> float:
    """The Pine's volatility-scaled stop floor, stocks branch."""
    atr_pct = atr / entry * 100.0 if entry else 0.0
    mult = 1.5 if atr_pct > 5 else 1.0 if atr_pct > 3 else 0.7 if atr_pct > 1.5 else 0.5
    return max(atr * mult, entry * 0.003)


class Orb(Strategy):
    name = "Opening Range Breakout (orb.pine, ORB15)"
    tag = "ORB"
    pine = "signal_engine/pinescripts/intraday/orb/orb.pine"

    #: Index direction, indexed by 5-minute timestamp: +1 above its session VWAP, -1
    #: below. Set by `load_index()`; None disables the index vote.
    index_dir: pd.Series | None = None

    # ---- preparation ---------------------------------------------------

    def prepare(self, df: pd.DataFrame, p: OrbParams) -> pd.DataFrame:
        d = ind.add_session_columns(df)
        day = d["day"]

        # ---- opening range, frozen once the window closes --------------
        inside = d["from_open"] < p.orb_minutes
        d["orb_hi"] = day.map(d["High"].where(inside).groupby(day).max())
        d["orb_lo"] = day.map(d["Low"].where(inside).groupby(day).min())
        d["orb_rng"] = d["orb_hi"] - d["orb_lo"]
        mid = (d["orb_hi"] + d["orb_lo"]) / 2
        rng_pct = d["orb_rng"] / mid.replace(0, np.nan) * 100.0
        width_ok = ((rng_pct >= p.orb_range_min_pct) & (rng_pct <= p.orb_range_max_pct)
                    if p.use_orb_range_filter else pd.Series(True, index=d.index))
        d["orb_ok"] = ((~inside) & (d["orb_rng"] > 0) & width_ok).astype(float)

        # ---- gap from previous session close ---------------------------
        prev_close = day.map(d["Close"].groupby(day).last().shift(1))
        today_open = day.map(d["Open"].groupby(day).first())
        gap = ((today_open - prev_close).abs() / prev_close.replace(0, np.nan) * 100.0)
        gap_ok = ((gap <= p.gap_max_pct) | gap.isna()
                  if p.use_gap_filter else pd.Series(True, index=d.index))
        d["gap_ok"] = gap_ok.astype(float)

        # ---- volume ----------------------------------------------------
        if p.vol_mode == "rvol":
            d["vol_ratio"] = ind.rvol_time_of_day(d, day)
        else:
            v = d["Volume"]
            base = v.rolling(p.vol_ma_len, min_periods=10).mean()
            d["vol_ratio"] = v.rolling(3, min_periods=1).max() / base.replace(0, np.nan)

        # ---- trend vote: session VWAP ----------------------------------
        vwap = ind.session_vwap(d, day)
        d["trend_bull"] = (d["Close"] > vwap).astype(float)
        d["trend_bear"] = (d["Close"] < vwap).astype(float)

        # ---- HTF vote: daily close vs its 20 EMA, >= htf_min_strength apart ----
        # Computed from this symbol's own 5-minute bars rather than a separate daily
        # feed, because the harness does not tell prepare() which symbol it holds.
        # The EMA is shifted one session, so today's session never informs today's vote.
        # Only ~59 sessions are available, so the first ~20 abstain (bias 0) while the
        # EMA warms up - the vote is absent there, never wrong.
        daily = d["Close"].groupby(day).last()
        e = ind.ema(daily, p.htf_ema_len)
        dist = (daily - e) / e.replace(0, np.nan) * 100.0
        bias = pd.Series(
            np.where(dist >= p.htf_min_strength, 1.0,
                     np.where(dist <= -p.htf_min_strength, -1.0, 0.0)),
            index=daily.index).shift(1).fillna(0.0)
        d["htf"] = day.map(bias).fillna(0.0)

        # ---- index vote ------------------------------------------------
        if self.index_dir is not None:
            d["idx"] = pd.Series(self.index_dir.reindex(d.index).to_numpy(),
                                 index=d.index).fillna(0.0)
        else:
            d["idx"] = 0.0

        d["atr"] = ind.atr(d, p.atr_len)
        return d

    def prepare_key(self, p: OrbParams) -> tuple:
        return (p.orb_minutes, p.use_orb_range_filter, p.orb_range_min_pct,
                p.orb_range_max_pct, p.use_gap_filter, p.gap_max_pct, p.vol_mode,
                p.vol_ma_len, p.atr_len, p.htf_ema_len, p.htf_min_strength)

    # ---- entry ---------------------------------------------------------

    def entry(self, c: Ctx, i: int, p: OrbParams, direction: int):
        if i < 1 or c["orb_ok"][i] <= 0 or c["gap_ok"][i] <= 0:
            return None
        if c["mins"][i] < p.min_entry_min:
            return None
        px, prev = c["Close"][i], c["Close"][i - 1]
        if not np.isfinite(px) or px < p.min_price:
            return None

        hi, lo, rng = c["orb_hi"][i], c["orb_lo"][i], c["orb_rng"][i]
        if not np.isfinite(rng) or rng <= 0:
            return None

        # rising-edge cross of the buffered level
        if direction == 1:
            lvl = hi * (1.0 + p.breakout_buffer_pct / 100.0)
            if not (px > lvl and prev <= lvl):
                return None
        else:
            lvl = lo * (1.0 - p.breakout_buffer_pct / 100.0)
            if not (px < lvl and prev >= lvl):
                return None

        if p.use_volume:
            vr = c["vol_ratio"][i]
            if not np.isfinite(vr) or vr < min(p.vol_mult, p.strong_vol_mult):
                return None

        # 2-of-3 directional veto
        against = 0
        if p.use_trend:
            aligned = c["trend_bull"][i] > 0 if direction == 1 else c["trend_bear"][i] > 0
            if not aligned:
                against += 1
        if p.use_htf and c["htf"][i] == -direction:
            against += 1
        if p.use_index and c["idx"][i] == -direction:
            against += 1
        if against >= p.veto_threshold:
            return None

        atr = c["atr"][i]
        if not np.isfinite(atr) or atr <= 0:
            atr = rng * 0.5
        # The Pine computes SL/TP from the FILL (next bar's open); the engine hands the
        # signal price here, so the levels are built from close and the fill difference
        # is carried as real slippage - the same treatment every other adapter gets.
        sl = self._stop(px, hi, lo, rng, atr, p, direction)
        risk = abs(px - sl)
        if p.tp_mode == "r":
            tp_dist = risk * p.tp_r
        else:
            tp_dist = max(rng, risk * p.tp_risk_floor)
        tp = px + direction * tp_dist * p.tp_mult
        return EntrySignal(direction=direction, sl=float(sl), tp=float(tp),
                           tag="ORB_UP" if direction == 1 else "ORB_DN")

    def _stop(self, entry: float, hi: float, lo: float, rng: float,
              atr: float, p: OrbParams, d: int) -> float:
        atr_pct = atr / entry * 100.0
        wick = max(atr * 0.3, rng * 0.15)
        if p.stop_mode == "atr":
            if d == 1:
                adj = 1.0 if entry < 1000 else 0.7 if entry < 5000 else 0.5
                sl = entry - atr * p.atr_mult * adj
                sl = min(sl, lo - wick) if p.use_orb_stop_floor else sl
            else:
                vol_adj = 1.2 if atr_pct > 3 else 1.0 if atr_pct > 1.5 else 0.8
                sl = entry + atr * p.atr_mult * vol_adj
                sl = max(sl, hi + wick) if p.use_orb_stop_floor else sl
        elif p.stop_mode == "smart":
            m = 1.5 if atr_pct > 3 else 1.0 if atr_pct > 1.5 else 0.7
            sl = entry - atr * m if d == 1 else entry + atr * m
            if p.use_orb_stop_floor:
                sl = min(sl, lo - rng * 0.3) if d == 1 else max(sl, hi + rng * 0.3)
        elif p.stop_mode == "scaled":
            m = 2.5 if atr_pct > 5 else 2.0 if atr_pct > 3 else 1.5 if atr_pct > 1.5 else 1.2
            sl = entry - atr * m if d == 1 else entry + atr * m
            if p.use_orb_stop_floor:
                sl = min(sl, lo - wick) if d == 1 else max(sl, hi + wick)
        elif p.stop_mode == "orb_pct":
            f = p.orb_stop_frac / 100.0
            sl = lo - rng * f if d == 1 else hi + rng * f
        elif p.stop_mode == "pct":
            sl = entry * (1 - p.pct_stop / 100.0) if d == 1 else entry * (1 + p.pct_stop / 100.0)
        else:
            raise ValueError(f"unknown stop_mode {p.stop_mode!r}")

        floor = _min_stop_distance(entry, atr)
        return float(min(sl, entry - floor) if d == 1 else max(sl, entry + floor))


def load_index(symbol: str = "^NSEI") -> pd.Series:
    """Index direction on 5-minute bars: +1 above its session VWAP, -1 below.

    Mirrors the Pine's `request.security(indexSymbol, timeframe.period, ta.vwap(close))`
    with lookahead off - the value is the index's own running session VWAP on the same
    bar, which is information available at that moment.
    """
    import yfinance as yf
    ix = yf.download(symbol, period="60d", interval="5m", progress=False, auto_adjust=False)
    if isinstance(ix.columns, pd.MultiIndex):
        ix.columns = ix.columns.get_level_values(0)
    ix = ind.add_session_columns(ix)
    v = ind.session_vwap(ix, ix["day"])
    return pd.Series(np.where(ix["Close"] > v, 1.0, np.where(ix["Close"] < v, -1.0, 0.0)),
                     index=ix.index)
