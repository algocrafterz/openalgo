"""IB (Initial Balance) extension double-breakout - backtest adapter.

Source: `signal_engine/pinescripts/intraday/ib-extension/ib-extension.pine`

THE IDEA
    Two nested opening ranges, both of which must break the same way:

        WEEKLY IB   high/low of Monday + Tuesday        -> the week's bias
        DAILY  IB   high/low of 09:15-10:15 today       -> today's range

        long  = close > W-High AND close > D-High, on Wed/Thu/Fri
        short = close < W-Low  AND close < D-Low

    plus a volume gate, a Nifty-50 regime gate, and a gap/circuit guard. Stop is the
    far side of the daily IB minus half an ATR; target is a 1x extension of the daily
    IB range beyond the broken edge.

THE GEOMETRY IS THE THING TO TEST
    Entry is the breakout close, which is at or above D-High. Stop is D-Low - 0.5*ATR,
    i.e. the FULL daily IB range plus a buffer below. Target is D-High + 1.0 * range.
    So even at the most favourable possible entry - exactly at D-High - the trade is

        reward = 1.00 x range
        risk   = 1.00 x range + 0.5 ATR

    a payoff below 1:1 before the entry has drifted any distance above D-High. The
    strategy's own analysis doc reports that the R:R filter had to be switched OFF
    because it rejected the setups ("R:R appears poor even for setups that would have
    been valid"). Turning off the instrument that reports a problem does not fix it.
    `tp_ext` and `sl_mode` are parameterised here so the data can rank the geometry
    rather than the doc asserting it.

BAR RESOLUTION
    The Pine runs on clock-aligned 30-minute bars and reads its IB from the 09:00,
    09:30 and 10:00 bars. This adapter runs on 5-minute bars and takes the IB from the
    true 09:15-10:15 window, which is the range the 30-minute construction is
    approximating. The breakout is then detected at 5-minute resolution, so signals
    fire earlier and more often than on 30-minute bars; `confirm_bars` coarsens the
    trigger back toward the shipped cadence for comparison.

DATA CONSTRAINT
    yfinance caps 5-minute history at 60 days, and this strategy only trades Wed/Thu/Fri
    of weeks with a valid Mon+Tue. That leaves roughly 35 tradeable sessions. The
    conclusion has to lean on the breadth of the F&O basket, not on the length of the
    window, and the out-of-sample half is only ~14 sessions - thin enough that an OOS
    row here is weak evidence either way.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from signal_engine.backtest import indicators as ind
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal


@dataclass(frozen=True)
class IbExtParams:
    # ---- daily IB ------------------------------------------------------
    ib_minutes: int = 60              # 09:15-10:15, the Pine's 3 x 30-min bars
    d_range_min_pct: float = 0.15     # IB range as % of its midpoint
    d_range_max_pct: float = 2.5

    # ---- weekly IB -----------------------------------------------------
    use_weekly: bool = True           # False isolates the plain daily-IB breakout
    w_range_min_pct: float = 0.30
    w_range_max_pct: float = 4.0

    # ---- confirmation ---------------------------------------------------
    #: Bars the composite condition must hold before firing. 1 = the Pine's rising
    #: edge at 5-minute resolution; 6 approximates one 30-minute bar.
    confirm_bars: int = 1

    # ---- volume gate ----------------------------------------------------
    use_volume: bool = True
    vol_mult: float = 1.5
    #: "sma" is the Pine's flat N-bar average. "rvol" compares the bar against the same
    #: clock slot on prior sessions, which is what a breakout at 10:20 actually needs.
    vol_mode: str = "sma"
    vol_sma_bars: int = 375           # 65 x 30min = 1 week -> 375 x 5min

    # ---- guards ---------------------------------------------------------
    use_regime: bool = True           # Nifty-50 daily close vs its 50 EMA
    use_circuit_guard: bool = True
    circuit_gap_pct: float = 4.0
    #: The Pine has no entry-time cap; its own doc lists one as a pending improvement.
    entry_cutoff_min: int = 14 * 60
    #: Skip a breakout already extended this many ATRs past the broken edge. 0 = off.
    max_chase_atr: float = 0.0

    # ---- risk and target -------------------------------------------------
    sl_mode: str = "ib"               # ib | atr
    atr_len: int = 14
    sl_atr_mult: float = 0.5          # buffer beyond the IB edge (ib mode)
    atr_sl_mult: float = 1.5          # stop distance from entry (atr mode)
    tp_ext: float = 1.0               # target = broken edge +/- tp_ext * IB range
    min_price: float = 20.0


def _weekly_ib(day: pd.Series, dow: pd.Series, hi: pd.Series, lo: pd.Series) -> pd.DataFrame:
    """Mon+Tue high/low per ISO week, and whether the day may trade on it.

    Built from completed DAILY aggregates and joined back by week, so a Wednesday bar
    only ever sees Monday's and Tuesday's finished sessions.
    """
    daily = pd.DataFrame({"day": day, "dow": dow, "hi": hi, "lo": lo}).groupby("day").agg(
        dow=("dow", "first"), hi=("hi", "max"), lo=("lo", "min"))
    idx = pd.to_datetime(daily.index)
    daily["week"] = (idx.isocalendar().year.astype(str) + "-"
                     + idx.isocalendar().week.astype(str)).to_numpy()
    early = daily[daily["dow"].isin([0, 1])]          # Monday=0, Tuesday=1
    w = early.groupby("week").agg(w_hi=("hi", "max"), w_lo=("lo", "min"),
                                  n_early=("hi", "size"))
    return daily.join(w, on="week")


class IbExtension(Strategy):
    name = "IB extension double breakout (weekly + daily)"
    tag = "IBEXT"
    pine = "signal_engine/pinescripts/intraday/ib-extension/ib-extension.pine"

    def __init__(self, regime: pd.Series | None = None) -> None:
        # Daily Nifty-50 regime the use_regime gate reads, loaded once here. Before
        # this, nothing in the codebase ever called load_regime(): with no regime
        # loaded, prepare() filled the "regime" column with 0.0 for every bar, and
        # entry()'s `c["regime"][i] != direction` is true for 0.0 against BOTH +1 and
        # -1 - use_regime=True (the shipped default) silently rejected every signal in
        # both directions for the life of this adapter. Zero trades, no error, no log
        # line - the same silent-failure shape as the IST bug (root CLAUDE.md).
        # `regime=<series>` stays available for a caller that wants to isolate the
        # filter with a specific series; `p.use_regime=False` remains the way to
        # disable the check entirely.
        self.regime = load_regime() if regime is None else regime

    # ---- preparation ---------------------------------------------------

    def prepare(self, df: pd.DataFrame, p: IbExtParams) -> pd.DataFrame:
        d = ind.add_session_columns(df)
        day = d["day"]
        dow = pd.Series(pd.to_datetime(d.index.date).dayofweek, index=d.index)

        # ---- daily IB, frozen when the window closes -------------------
        inside = d["from_open"] < p.ib_minutes
        d_hi = d["High"].where(inside).groupby(day).max()
        d_lo = d["Low"].where(inside).groupby(day).min()
        d["d_hi"] = day.map(d_hi)
        d["d_lo"] = day.map(d_lo)
        d["d_rng"] = d["d_hi"] - d["d_lo"]
        d_mid = (d["d_hi"] + d["d_lo"]) / 2
        d_pct = (d["d_rng"] / d_mid.replace(0, np.nan) * 100.0)
        d["d_valid"] = ((d_pct >= p.d_range_min_pct) & (d_pct <= p.d_range_max_pct)
                        & (~inside)).astype(float)

        # ---- weekly IB (Mon+Tue), joined by ISO week -------------------
        wk = _weekly_ib(day, dow, d["High"], d["Low"])
        d["w_hi"] = day.map(wk["w_hi"])
        d["w_lo"] = day.map(wk["w_lo"])
        w_mid = (d["w_hi"] + d["w_lo"]) / 2
        w_pct = ((d["w_hi"] - d["w_lo"]) / w_mid.replace(0, np.nan) * 100.0)
        # Wed/Thu/Fri only, and the week must actually have had a Mon or Tue
        tradeable_dow = dow.isin([2, 3, 4])
        d["w_valid"] = (tradeable_dow & (w_pct >= p.w_range_min_pct)
                        & (w_pct <= p.w_range_max_pct)
                        & (day.map(wk["n_early"]).fillna(0) > 0)).astype(float)

        # ---- volume ----------------------------------------------------
        if p.vol_mode == "rvol":
            d["vol_ratio"] = ind.rvol_time_of_day(d, day)
        else:
            base = d["Volume"].rolling(p.vol_sma_bars, min_periods=50).mean()
            d["vol_ratio"] = d["Volume"] / base.replace(0, np.nan)

        # ---- guards ----------------------------------------------------
        prev_close = day.map(d["Close"].groupby(day).last().shift(1))
        d["gap_pct"] = ((d["Close"] - prev_close).abs() / prev_close.replace(0, np.nan) * 100.0)
        if self.regime is not None:
            r = self.regime.copy()
            r.index = pd.to_datetime(r.index).date
            d["regime"] = day.map(r).fillna(0.0)      # +1 bull, -1 bear, 0 unknown
        else:
            d["regime"] = 0.0

        d["atr"] = ind.atr(d, p.atr_len)
        return d

    def prepare_key(self, p: IbExtParams) -> tuple:
        return (p.ib_minutes, p.d_range_min_pct, p.d_range_max_pct, p.w_range_min_pct,
                p.w_range_max_pct, p.vol_mode, p.vol_sma_bars, p.atr_len)

    # ---- state ---------------------------------------------------------

    def reset_symbol(self, p) -> None:
        self._run_up = self._run_dn = 0

    def reset_session(self, p) -> None:
        self._run_up = self._run_dn = 0

    def on_bar(self, c: Ctx, i: int, p: IbExtParams) -> None:
        """Count consecutive bars the raw breakout has held, for `confirm_bars`."""
        up, dn = self._raw(c, i, p)
        self._run_up = self._run_up + 1 if up else 0
        self._run_dn = self._run_dn + 1 if dn else 0

    def _raw(self, c: Ctx, i: int, p: IbExtParams) -> tuple[bool, bool]:
        if c["d_valid"][i] <= 0:
            return False, False
        px = c["Close"][i]
        up = px > c["d_hi"][i]
        dn = px < c["d_lo"][i]
        if p.use_weekly:
            if c["w_valid"][i] <= 0:
                return False, False
            up = up and px > c["w_hi"][i]
            dn = dn and px < c["w_lo"][i]
        return bool(up), bool(dn)

    # ---- entry ---------------------------------------------------------

    def entry(self, c: Ctx, i: int, p: IbExtParams, direction: int):
        px = c["Close"][i]
        if not np.isfinite(px) or px < p.min_price:
            return None
        if c["mins"][i] >= p.entry_cutoff_min:
            return None

        up, dn = self._raw(c, i, p)
        held = self._run_up if direction == 1 else self._run_dn
        # `held` counts bars up to and including i-1 (on_bar runs after entry() for i),
        # so the current bar plus the run must reach confirm_bars.
        if not (up if direction == 1 else dn) or held + 1 < p.confirm_bars:
            return None

        if p.use_volume:
            vr = c["vol_ratio"][i]
            if not np.isfinite(vr) or vr < p.vol_mult:
                return None
        if p.use_circuit_guard:
            g = c["gap_pct"][i]
            if np.isfinite(g) and g > p.circuit_gap_pct:
                return None
        # 0 = unknown regime (prepare()'s fillna(0.0), or a date load_regime() has no
        # coverage for) and must PASS, not block - the gate only rejects a bar where
        # the regime is actually KNOWN and disagrees with this direction.
        if p.use_regime and c["regime"][i] not in (0.0, direction):
            return None

        atr = c["atr"][i]
        if not np.isfinite(atr):
            return None
        d_hi, d_lo, rng = c["d_hi"][i], c["d_lo"][i], c["d_rng"][i]
        edge = d_hi if direction == 1 else d_lo
        if p.max_chase_atr > 0 and abs(px - edge) > p.max_chase_atr * atr:
            return None

        if p.sl_mode == "ib":
            sl = (d_lo - p.sl_atr_mult * atr) if direction == 1 else (d_hi + p.sl_atr_mult * atr)
        elif p.sl_mode == "atr":
            sl = px - direction * p.atr_sl_mult * atr
        else:
            raise ValueError(f"unknown sl_mode {p.sl_mode!r}")
        tp = edge + direction * p.tp_ext * rng
        return EntrySignal(direction=direction, sl=float(sl), tp=float(tp),
                           tag="IB_UP" if direction == 1 else "IB_DN")


def load_regime(ema_len: int = 50, period: str = "max") -> pd.Series:
    """Daily Nifty-50 direction: +1 above its EMA, -1 below.

    Indexed by date and consumed by `IbExtension.prepare`. The EMA is computed on
    closed daily bars and the series is shifted one day, so a bar on date D reads the
    regime as of D-1's close - the Pine's `lookahead_off` behaviour.

    `period="max"` (not the old "2y"): the historify-backed backtest this strategy
    otherwise runs against covers ~10 years, and daily yfinance data has no equivalent
    of the 60-day intraday cap - there is no reason to truncate it to 2 years. Any
    date this still does not cover reads as regime 0 (unknown), which `entry()`
    treats as a pass, not a block.
    """
    import yfinance as yf
    nif = yf.download("^NSEI", period=period, interval="1d", progress=False, auto_adjust=True)
    if isinstance(nif.columns, pd.MultiIndex):
        nif.columns = nif.columns.get_level_values(0)
    close = nif["Close"].dropna()
    e = ind.ema(close, ema_len)
    sig = np.where(close > e, 1.0, np.where(close < e, -1.0, 0.0))
    return pd.Series(sig, index=close.index).shift(1).dropna()
