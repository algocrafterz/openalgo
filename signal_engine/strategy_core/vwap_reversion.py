"""IDEA-029 VWAP 2SD range mean reversion: input builder and per-bar state machine.

The rules are the frozen spec at
`signal_engine/research/ideas/idea-029-vwap-2sd-range-mean-reversion/spec.yaml`. Two parts:

`build_inputs(df, p)` is vectorized and causal. From 5-minute bars (IST open-time index) it
derives every value a decision at bar k may read, each as of bar k's close: the session VWAP
of hlc3 and its volume-weighted SD, the 2SD bands, EMA9 and Wilder ATR14 on 5m, and, from the
latest COMPLETED fixed 15-minute bar, ADX14 / EMA20 / ATR14 and the regime test.

`VwapReversionMachine` is the setup state machine. Its state depends on fills (no setup forms
while a position is open; re-entry needs an extreme after the exit bar; at most two trades per
session), so it is driven one completed bar at a time and told about fills and exits. The
backtest adapter (`backtest/strategies/vwap_reversion.py`) and a live runner drive it the same
way: `on_bar` at each bar close, then `on_fill` / `on_fill_skipped` / `on_exit` as the broker
reports them. Pure Python, no I/O, no clock.

Only bars inside the data contract's complete bins (09:15-15:10) are strategy bars: closing
auction bars from 15:15 feed no indicator and are never decision bars.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from signal_engine.features.htf import completed_htf_position, resample_15m
from signal_engine.features.price import adx, atr, ema, session_vwap_sd

#: First and last 5-minute bin of the data contract (`data.complete_bins`).
FIRST_BIN_MIN = 9 * 60 + 15
LAST_BIN_MIN = 15 * 60 + 10
#: Spec `session_window.entry` 09:20-14:15 are FILL times at the next bar's open, so the
#: confirmation bar opens 09:15 at the earliest and 14:10 at the latest.
FIRST_CONFIRM_MIN = 9 * 60 + 15
LAST_CONFIRM_MIN = 14 * 60 + 10
#: Spec `re_entry.max_per_symbol_day`. Not a tunable parameter, so not in the params class.
MAX_TRADES_PER_SESSION = 2

DIAGNOSTIC_KEYS = (
    "setups_armed_long",
    "setups_armed_short",
    "setups_replaced",
    "setups_cancelled_deeper_fail",
    "setups_cancelled_vwap_touch",
    "setups_expired",
    "setups_cancelled_cutoff",
    "setups_cancelled_position",
    "confirmations",
    "entries_skipped_beyond_level",
    "confirmations_blocked_by_cap",
    "blocked_by_warmup",
    "blocked_by_regime",
)

INPUT_COLUMNS = (
    "vwap",
    "vwap_sd",
    "upper",
    "lower",
    "ema9",
    "atr5",
    "h15_pos",
    "n15",
    "adx15",
    "ema20_15",
    "ema20_15_lag",
    "atr15",
    "regime_ok",
    "warm",
    "confirmable",
)


@dataclass(frozen=True)
class VwapReversionParams:
    """Exactly the frozen spec's `parameters` block (names and values)."""

    vwap_sd: float = 2.0
    atr_period_5m: int = 14
    min_vwap_distance_atr: float = 0.75
    stop_buffer_atr: float = 0.20
    adx_period_15m: int = 14
    adx_max: float = 22.0
    ema_regime_period_15m: int = 20
    ema_regime_lookback_bars: int = 4
    ema_regime_max_change_atr: float = 0.25
    atr_period_15m: int = 14
    ema_entry_period_5m: int = 9
    arm_bars: int = 6
    warmup_15m_bars: int = 100


def _minute_of_day(index: pd.DatetimeIndex) -> np.ndarray:
    return np.asarray(index.hour * 60 + index.minute)


def build_inputs(df: pd.DataFrame, p: VwapReversionParams) -> pd.DataFrame:
    """Decision inputs for every 5-minute bar of `df`, each as of that bar's close.

    `df` holds Open/High/Low/Close/Volume on an IST open-time index; excluded sessions are
    simply absent, so every continuous indicator skips them without a reset. Rows outside the
    complete bins are kept (NaN values, never confirmable) so the result aligns with `df`.
    """
    mins = _minute_of_day(df.index)
    core_mask = (mins >= FIRST_BIN_MIN) & (mins <= LAST_BIN_MIN)
    core = df.loc[core_mask, ["Open", "High", "Low", "Close", "Volume"]].astype("float64")

    out = pd.DataFrame(index=df.index)
    vwap, sd = session_vwap_sd(core)
    out["vwap"] = vwap
    out["vwap_sd"] = sd
    out["upper"] = vwap + p.vwap_sd * sd
    out["lower"] = vwap - p.vwap_sd * sd
    out["ema9"] = ema(core["Close"], p.ema_entry_period_5m)
    out["atr5"] = atr(core, p.atr_period_5m)

    m15 = resample_15m(core)
    ema20 = ema(m15["Close"], p.ema_regime_period_15m)
    ema20_lag = ema20.shift(p.ema_regime_lookback_bars)
    atr15 = atr(m15, p.atr_period_15m)
    adx15 = adx(m15, p.adx_period_15m)
    regime = (adx15 <= p.adx_max) & (
        (ema20 - ema20_lag).abs() <= p.ema_regime_max_change_atr * atr15
    )

    pos = completed_htf_position(df.index, m15.index)

    def pick(s: pd.Series, missing) -> np.ndarray:
        # one sentinel appended at the end, so position -1 (no completed bar yet) reads it
        return np.append(s.to_numpy(), missing)[pos]

    out["h15_pos"] = pos
    out["n15"] = pos + 1
    out["adx15"] = pick(adx15.astype("float64"), np.nan)
    out["ema20_15"] = pick(ema20.astype("float64"), np.nan)
    out["ema20_15_lag"] = pick(ema20_lag.astype("float64"), np.nan)
    out["atr15"] = pick(atr15.astype("float64"), np.nan)
    out["regime_ok"] = pick(regime.astype(bool), False).astype(bool)
    out["warm"] = (pos + 1) >= p.warmup_15m_bars
    out["confirmable"] = core_mask & (mins >= FIRST_CONFIRM_MIN) & (mins <= LAST_CONFIRM_MIN)
    return out


def entry_beyond_levels(direction: int, fill: float, stop: float, target: float) -> bool:
    """The skip-at-fill rule: True when the entry bar's open is at/beyond the target or stop."""
    if direction == 1:
        return fill >= target or fill <= stop
    return fill <= target or fill >= stop


def target_for_bar(vwap: np.ndarray, j: int) -> float:
    """The exit target used on bar j: session VWAP as of the close of bar j - 1."""
    return float(vwap[j - 1])


@dataclass(frozen=True)
class Bar:
    """What a decision at the close of bar k reads. Every value includes bar k, nothing later."""

    k: int
    high: float
    low: float
    close: float
    vwap: float
    lower: float
    upper: float
    ema9: float
    atr: float
    regime_ok: bool
    warm: bool
    confirmable: bool


@dataclass(frozen=True)
class Confirmation:
    """A confirmed setup: a market order goes out for the open of bar `bar + 1`."""

    bar: int
    direction: int
    extreme: float
    stop: float
    target: float


@dataclass
class _Setup:
    extreme: float
    bar: int
    #: ATR14_5m at the extreme bar: the stop buffer is fixed from it.
    atr: float


class VwapReversionMachine:
    """Setup state for one symbol. Counters accumulate across symbols and sessions.

    `reset_symbol` / `reset_session` clear trading state only; call `reset_diagnostics` to
    zero the counters (a fresh instance starts at zero).
    """

    def __init__(self, p: VwapReversionParams) -> None:
        self.p = p
        self._counts = dict.fromkeys(DIAGNOSTIC_KEYS, 0)
        self.reset_symbol()

    # ---- lifecycle ---------------------------------------------------------

    def reset_symbol(self) -> None:
        self._setups: dict[int, _Setup | None] = {1: None, -1: None}
        self._in_position = False
        self._last_exit = -1
        self._trades = 0

    def reset_session(self) -> None:
        for d in (1, -1):
            if self._setups[d] is not None:
                self._cancel(d, "setups_cancelled_cutoff")
        self._in_position = False
        self._trades = 0

    def reset_diagnostics(self) -> None:
        self._counts = dict.fromkeys(DIAGNOSTIC_KEYS, 0)

    def diagnostics(self) -> dict[str, int]:
        return dict(self._counts)

    # ---- broker events -----------------------------------------------------

    def on_fill(self, k: int) -> None:
        self._in_position = True
        self._trades += 1
        # An opposite setup cannot outlive the position: its extreme is not after the exit bar,
        # and re-entry needs an extreme formed after the bar on which the trade exited.
        for d in (1, -1):
            if self._setups[d] is not None:
                self._cancel(d, "setups_cancelled_position")

    def on_fill_skipped(self, k: int) -> None:
        self._counts["entries_skipped_beyond_level"] += 1

    def on_exit(self, k: int) -> None:
        self._in_position = False
        self._last_exit = k

    # ---- the per-bar rules -------------------------------------------------

    def _arms(self, d: int, b: Bar) -> bool:
        if not (b.atr > 0):  # also False for NaN
            return False
        if d == 1:
            return b.low <= b.lower and (b.vwap - b.low) / b.atr >= self.p.min_vwap_distance_atr
        return b.high >= b.upper and (b.high - b.vwap) / b.atr >= self.p.min_vwap_distance_atr

    def _cancel(self, d: int, counter: str) -> None:
        self._setups[d] = None
        self._counts[counter] += 1

    def _arm(self, d: int, b: Bar) -> None:
        self._setups[d] = _Setup(extreme=b.low if d == 1 else b.high, bar=b.k, atr=b.atr)
        self._counts["setups_armed_long" if d == 1 else "setups_armed_short"] += 1

    def _evaluate(self, d: int, s: _Setup, b: Bar) -> Confirmation | None:
        """Steps (1)-(3) of the spec for an armed setup on a later bar, then expiry."""
        deeper = b.low < s.extreme if d == 1 else b.high > s.extreme
        if deeper:
            if self._arms(d, b):
                self._setups[d] = _Setup(extreme=b.low if d == 1 else b.high, bar=b.k, atr=b.atr)
                self._counts["setups_replaced"] += 1
            else:
                self._cancel(d, "setups_cancelled_deeper_fail")
            return None
        touched = b.high >= b.vwap if d == 1 else b.low <= b.vwap
        if touched:
            self._cancel(d, "setups_cancelled_vwap_touch")
            return None
        if d == 1:
            price_ok = b.close > b.ema9 and b.close > b.lower
        else:
            price_ok = b.close < b.ema9 and b.close < b.upper
        if price_ok:
            # The would-be confirmation is the point where warm-up and regime are evaluated:
            # when either fails, the setup is cancelled, not carried to a later bar.
            if not b.warm:
                self._cancel(d, "blocked_by_warmup")
            elif not b.regime_ok:
                self._cancel(d, "blocked_by_regime")
            else:
                return self._confirm(d, s, b)
            return None
        if b.k - s.bar >= self.p.arm_bars:
            self._cancel(d, "setups_expired")
        return None

    def _confirm(self, d: int, s: _Setup, b: Bar) -> Confirmation | None:
        self._counts["confirmations"] += 1
        # The confirmation consumes only its own setup, also when the cap blocks it; an
        # opposite setup keeps its state.
        self._setups[d] = None
        if self._trades >= MAX_TRADES_PER_SESSION:
            self._counts["confirmations_blocked_by_cap"] += 1
            return None
        buffer = self.p.stop_buffer_atr * s.atr
        stop = s.extreme - buffer if d == 1 else s.extreme + buffer
        return Confirmation(bar=b.k, direction=d, extreme=s.extreme, stop=stop, target=b.vwap)

    def on_bar(self, b: Bar) -> Confirmation | None:
        """Process completed bar `b`. Returns the confirmation to act on, or None."""
        if self._in_position:
            return None  # no setup exists or forms while a position is open
        if not b.confirmable:
            # past the entry cutoff (or outside the session bins): cancel, never arm
            for d in (1, -1):
                if self._setups[d] is not None:
                    self._cancel(d, "setups_cancelled_cutoff")
            return None
        for d in (1, -1):
            s = self._setups[d]
            if s is None:
                if b.k > self._last_exit and self._arms(d, b):
                    self._arm(d, b)
                continue
            conf = self._evaluate(d, s, b)
            if conf is not None:
                return conf
        return None


def bar_from_arrays(cols: dict[str, np.ndarray], k: int) -> Bar:
    """Build bar k's decision inputs from column arrays (a prepared frame or a live window)."""
    return Bar(
        k=k,
        high=float(cols["High"][k]),
        low=float(cols["Low"][k]),
        close=float(cols["Close"][k]),
        vwap=float(cols["vwap"][k]),
        lower=float(cols["lower"][k]),
        upper=float(cols["upper"][k]),
        ema9=float(cols["ema9"][k]),
        atr=float(cols["atr5"][k]),
        regime_ok=bool(cols["regime_ok"][k]),
        warm=bool(cols["warm"][k]),
        confirmable=bool(cols["confirmable"][k]),
    )


__all__ = [
    "DIAGNOSTIC_KEYS",
    "INPUT_COLUMNS",
    "MAX_TRADES_PER_SESSION",
    "Bar",
    "Confirmation",
    "VwapReversionMachine",
    "VwapReversionParams",
    "bar_from_arrays",
    "build_inputs",
    "entry_beyond_levels",
    "target_for_bar",
]
