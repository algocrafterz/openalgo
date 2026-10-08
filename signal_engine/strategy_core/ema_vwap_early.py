"""EMA/VWAP/ATR early displacement with an aggressive trailing stop and no take-profit.

Source of truth: `strategy_library/lipiscripts/intraday/ema-vwap-atr-early-displacement-v2.lipi` (the discretionary
chart study; its early-entry rule is unchanged from the version this port was written against) and
`research/ideas/_inbox/ema-vwap-atr-early-displacement.txt` (the research framework), translated
into deterministic rules. The
strategy log `strategy_library/intraday/ema-vwap-early/STRATEGY-LOG.md` records every
assumption made in the translation.

    displacement = (EMA9(close) - session VWAP(hlc3)) / ATR14        on 5-minute bars

ENTRY (judged on a CLOSED bar; the order goes out after that bar closes):
  early long   previous displacement <= -T and current displacement > -T     (T = 0.50)
  early short  previous displacement >= +T and current displacement < +T
  crossover    previous <= 0 < current (long) / previous >= 0 > current (short); Baseline A only
then, in this order, the first failing filter names the skip reason:
  momentum     displacement ROC (current - previous) must point the trade's way
  extension    displacement beyond max_entry_displacement on the trade's side is too late
  EMA20        strict: close on the trade's side of EMA20; preferred: recorded only; disabled
  VWAP chop    too many zero crossings in the recent window AND a small mean |displacement|
  structure    a protective stop must exist below (long) / above (short) the signal close

INITIAL STOP (stop_basis SWING): the latest confirmed same-session swing low (long) / high (short)
minus/plus stop_buffer_atr x ATR. A swing at bar j is confirmed only at bar j + swing_right, so the
stop never reads a future bar. When no confirmed swing sits on the loss side (stop_basis
FALLBACK_EXTREME): the lowest low (long) / highest high (short) of the preceding
stop_fallback_lookback completed same-session bars, EXCLUDING the signal bar, minus/plus the same
buffer. Neither level on the loss side of the signal close: FILTERED_NO_STRUCTURE.

EXIT: no take-profit. The broker SL-M holds the initial stop intrabar. `TrailState` is stepped once
per closed bar and owns everything else. The stop it computes at the close of bar k is the stop in
force during bar k + 1; it only ever tightens. A trailing exit is decided at a bar close when the bar
touched the stop that was in force during it, or closed beyond the stop just computed, and is filled
at the next bar's open (a market order in paper/live, `custom_exit_fill="next_open"` in backtest).

Pure: no I/O, no clock. The pipeline strategy, the live exit driver and the backtest adapter all
call this module, so the rules scored are the rules traded.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from signal_engine.features.price import atr, ema, session_vwap

#: Continuous trading only: the closing auction bars (15:15 onwards) feed no indicator.
LAST_BIN_MIN = 15 * 60 + 10

#: take_profit: none. One rule, two encodings, because the two carriers differ:
#:   paper/live  Signal.tp is a float; 0 means none. The validator accepts 0 only for an exit plan
#:               without targets (config.NO_TARGET_EXIT_PLANS) and rejects any other value for it.
#:   backtest    EntrySignal.tp is NaN. The engine tests `high >= tp`, which 0 would satisfy at once;
#:               no bar can reach NaN, so no target fill exists.
#: The exits in both are the same: initial structural stop, trailing stop, 14:45 time exit.
TAKE_PROFIT = "none"
#: ~20 sessions of 72 continuous-trading 5-minute bars, for the ATR regime diagnostic.
REGIME_BARS = 20 * 72
#: Bars before (and including) the signal bar kept as the displacement trajectory diagnostic.
PRE_SIGNAL_BARS = 6
PAPER_TP_NONE = 0.0
BACKTEST_TP_NONE = float("nan")

ENTRY_MODES = ("early", "crossover")
EMA20_FILTERS = ("strict", "preferred", "disabled")
TRAIL_MODES = ("atr", "structure", "hybrid")
ACTIVATIONS = ("immediate", "r_multiple", "crossover", "displacement")
FAILURE_ACTIONS = ("tighten", "exit")

# Signal reason codes. Accepted entries:
EARLY_LONG_DISPLACEMENT = "EARLY_LONG_DISPLACEMENT"
EARLY_SHORT_DISPLACEMENT = "EARLY_SHORT_DISPLACEMENT"
EARLY_LONG_MOMENTUM_CONFIRMED = "EARLY_LONG_MOMENTUM_CONFIRMED"
EARLY_SHORT_MOMENTUM_CONFIRMED = "EARLY_SHORT_MOMENTUM_CONFIRMED"
CROSSOVER_LONG = "CROSSOVER_LONG"
CROSSOVER_SHORT = "CROSSOVER_SHORT"
# Skipped triggers:
FILTERED_MOMENTUM_CONTRACTION = "FILTERED_MOMENTUM_CONTRACTION"
FILTERED_EXCESSIVE_EXTENSION = "FILTERED_EXCESSIVE_EXTENSION"
FILTERED_EMA20_CONFLICT = "FILTERED_EMA20_CONFLICT"
FILTERED_VWAP_CHOP = "FILTERED_VWAP_CHOP"
FILTERED_NO_STRUCTURE = "FILTERED_NO_STRUCTURE"
FILTERED_OUTSIDE_WINDOW = "FILTERED_OUTSIDE_WINDOW"
FILTERED_WARMUP = "FILTERED_WARMUP"

# Exit reasons.
TRAILING_STOP = "TRAILING_STOP"
INITIAL_STOP = "INITIAL_STOP"
MOMENTUM_FAILURE = "MOMENTUM_FAILURE"
FORCE_EXIT = "FORCE_EXIT"
SESSION_CLOSE = "SESSION_CLOSE"

# Market states (the setup side of the state machine; position states live in TrailState).
NEUTRAL = "NEUTRAL"
EARLY_BULLISH_SETUP = "EARLY_BULLISH_SETUP"
EARLY_BEARISH_SETUP = "EARLY_BEARISH_SETUP"
BULLISH_EXPANSION = "BULLISH_EXPANSION"
BEARISH_EXPANSION = "BEARISH_EXPANSION"
BULLISH_CONTRACTION = "BULLISH_CONTRACTION"
BEARISH_CONTRACTION = "BEARISH_CONTRACTION"


@dataclass(frozen=True)
class EarlyDisplacementParams:
    # indicators
    ema_fast: int = 9
    ema_slow: int = 20
    atr_len: int = 14
    vwap_source: str = "hlc3"  # the .lipi uses talib.vwap(hlc3)
    min_history_bars: int = 60  # bars of history before any signal (EMA20/ATR14 settled)
    # entry
    entry_mode: str = "early"  # early (the strategy) | crossover (Baseline A)
    early_threshold: float = 0.50  # long triggers through -T, short through +T
    momentum_confirm: bool = True
    min_displacement_roc: float = 0.0
    ema20_filter: str = "strict"
    chop_filter: bool = True
    chop_lookback: int = 5
    chop_max_sign_changes: int = 1
    chop_min_mean_abs_disp: float = 0.50
    max_entry_displacement: float = 1.5
    # window: minutes after midnight IST of the SIGNAL bar's open, [start, cutoff)
    min_entry_min: int = 9 * 60 + 20
    entry_cutoff_min: int = 14 * 60 + 15
    # initial stop
    swing_left: int = 2
    swing_right: int = 2
    stop_buffer_atr: float = 0.10
    stop_fallback_lookback: int = 6
    wide_stop_atr: float = 3.0  # classification only, never a rejection
    # trailing stop
    trail_mode: str = "hybrid"
    trailing_atr_multiple: float = 1.0
    trailing_buffer_atr: float = 0.10
    trailing_activation: str = "crossover"
    trailing_activation_r: float = 0.5
    trailing_activation_displacement: float = 0.5
    contraction_bars: int = 2
    contraction_atr_multiple: float = 0.75
    failure_atr_multiple: float = 0.5
    on_momentum_failure: str = "tighten"
    # diagnostics only (the .lipi reversal diamonds)
    reversal_move: float = 0.05

    def __post_init__(self) -> None:
        checks = (
            ("entry_mode", self.entry_mode, ENTRY_MODES),
            ("ema20_filter", self.ema20_filter, EMA20_FILTERS),
            ("trail_mode", self.trail_mode, TRAIL_MODES),
            ("trailing_activation", self.trailing_activation, ACTIVATIONS),
            ("on_momentum_failure", self.on_momentum_failure, FAILURE_ACTIONS),
            ("vwap_source", self.vwap_source, ("hlc3", "Close")),
        )
        for name, value, allowed in checks:
            if value not in allowed:
                raise ValueError(f"{name} must be one of {allowed}, got {value!r}")
        if not self.early_threshold > 0:
            raise ValueError(f"early_threshold must be positive, got {self.early_threshold}")
        for name in ("trailing_atr_multiple", "contraction_atr_multiple", "failure_atr_multiple"):
            if not getattr(self, name) > 0:
                raise ValueError(f"{name} must be positive")
        if self.swing_left < 1 or self.swing_right < 1 or self.chop_lookback < 2:
            raise ValueError("swing_left/swing_right must be >= 1 and chop_lookback >= 2")

    def with_(self, **kw) -> EarlyDisplacementParams:
        return replace(self, **kw)


# ---- inputs -------------------------------------------------------------------------------------


def _minute_of_day(index: pd.DatetimeIndex) -> np.ndarray:
    return np.asarray(index.hour * 60 + index.minute)


def _confirmed_swings(low: np.ndarray, high: np.ndarray, day: np.ndarray, left: int, right: int):
    """Latest confirmed same-session swing low and high as of each bar (NaN when none yet), plus the
    bar position of each swing (its pivot bar), for the swing-age diagnostic.

    Bar j is a swing low when its low is strictly below the `left` lows before it and not above the
    `right` lows after it, all in j's session. It becomes known at bar j + right and is carried
    forward to the end of that session.
    """
    n = len(low)
    sw_lo, sw_hi = np.full(n, np.nan), np.full(n, np.nan)
    lo_bar, hi_bar = np.full(n, np.nan), np.full(n, np.nan)
    for j in range(left, n - right):
        if day[j - left] != day[j + right]:
            continue
        if low[j] < low[j - left : j].min() and low[j] <= low[j + 1 : j + right + 1].min():
            sw_lo[j + right], lo_bar[j + right] = low[j], j
        if high[j] > high[j - left : j].max() and high[j] >= high[j + 1 : j + right + 1].max():
            sw_hi[j + right], hi_bar[j + right] = high[j], j
    days = pd.Series(day)

    def carry(a: np.ndarray) -> np.ndarray:
        return pd.Series(a).groupby(days).ffill().to_numpy()

    return carry(sw_lo), carry(sw_hi), carry(lo_bar), carry(hi_bar)


def _fallback_extremes(low: np.ndarray, high: np.ndarray, day: np.ndarray, n: int):
    """Lowest low / highest high of the PRECEDING `n` completed same-session bars, excluding the
    current bar (fewer right after the open, NaN on the session's first bar), and the bar position
    of each extreme (the most recent one on a tie), for the swing-age diagnostic.
    """
    size = len(low)
    f_lo, f_hi = np.full(size, np.nan), np.full(size, np.nan)
    lo_bar, hi_bar = np.full(size, np.nan), np.full(size, np.nan)
    start = 0
    for k in range(size):
        if k > 0 and day[k] != day[k - 1]:
            start = k
        lo = max(start, k - n)
        if lo >= k:
            continue
        w_lo, w_hi = low[lo:k], high[lo:k]
        i_lo = lo + len(w_lo) - 1 - int(np.argmin(w_lo[::-1]))
        i_hi = lo + len(w_hi) - 1 - int(np.argmax(w_hi[::-1]))
        f_lo[k], lo_bar[k] = low[i_lo], i_lo
        f_hi[k], hi_bar[k] = high[i_hi], i_hi
    return f_lo, f_hi, lo_bar, hi_bar


def _chop_stats(disp: np.ndarray, day: np.ndarray, lookback: int) -> tuple[np.ndarray, np.ndarray]:
    """Zero crossings and mean |displacement| over the last `lookback` same-session bars."""
    n = len(disp)
    changes = np.full(n, np.nan)
    mean_abs = np.full(n, np.nan)
    sign = np.sign(disp)
    for k in range(lookback - 1, n):
        lo = k - lookback + 1
        if day[lo] != day[k]:
            continue
        win = disp[lo : k + 1]
        if np.isnan(win).any():
            continue
        s = sign[lo : k + 1]
        s = s[s != 0]
        changes[k] = float(np.count_nonzero(s[1:] != s[:-1])) if len(s) > 1 else 0.0
        mean_abs[k] = float(np.abs(win).mean())
    return changes, mean_abs


def _reversals(disp: np.ndarray, close: np.ndarray, threshold: float, move: float) -> np.ndarray:
    """The .lipi reversal diamonds with its state lock: +1 bullish, -1 bearish, 0 none.

    Diagnostics only (never an entry). One event per change of reversal direction, so a run of
    qualifying bars produces a single event, as in the chart study.
    """
    out = np.zeros(len(disp), dtype=np.int8)
    state = 0
    for k in range(2, len(disp)):
        d0, d1, d2 = disp[k], disp[k - 1], disp[k - 2]
        if np.isnan(d0) or np.isnan(d1) or np.isnan(d2):
            continue
        bull = (
            d2 <= -threshold
            and d1 > d2
            and d0 > d1
            and d0 - d1 >= move
            and close[k] > close[k - 1]
            and d0 < 0
        )
        bear = (
            d2 >= threshold
            and d1 < d2
            and d0 < d1
            and d1 - d0 >= move
            and close[k] < close[k - 1]
            and d0 > 0
        )
        if bull and state != 1:
            out[k], state = 1, 1
        elif bear and state != -1:
            out[k], state = -1, -1
    return out


def market_state(cur: float, prev: float, threshold: float) -> str:
    """The setup-side state of one bar from its displacement and the previous one."""
    if np.isnan(cur) or np.isnan(prev):
        return NEUTRAL
    rising, falling = cur > prev, cur < prev
    if cur < 0 and rising and prev <= -threshold / 2:
        return EARLY_BULLISH_SETUP
    if cur > 0 and falling and prev >= threshold / 2:
        return EARLY_BEARISH_SETUP
    if cur > 0:
        return BULLISH_EXPANSION if rising else BULLISH_CONTRACTION
    if cur < 0:
        return BEARISH_EXPANSION if falling else BEARISH_CONTRACTION
    return NEUTRAL


def build_inputs(df: pd.DataFrame, p: EarlyDisplacementParams) -> pd.DataFrame:
    """Every value a decision at bar k may read, each as of bar k's close. Causal.

    `df` holds 5-minute OHLCV bars on a tz-aware IST open-time index, warm-up sessions first.
    Closing auction bars are dropped before any indicator is computed.
    """
    d = df[_minute_of_day(df.index) <= LAST_BIN_MIN][["Open", "High", "Low", "Close", "Volume"]]
    d = d.astype("float64").copy()
    day = np.asarray(d.index.date)
    d["ema_f"] = ema(d["Close"], p.ema_fast)
    d["ema_s"] = ema(d["Close"], p.ema_slow)
    d["vwap"] = session_vwap(d, p.vwap_source)
    d["atr"] = atr(d, p.atr_len)
    d["disp"] = (d["ema_f"] - d["vwap"]) / d["atr"].where(d["atr"] > 0)
    same_session = np.r_[False, day[1:] == day[:-1]]
    d["disp_prev"] = d["disp"].shift(1).where(same_session)
    d["roc"] = d["disp"] - d["disp_prev"]
    low, high = d["Low"].to_numpy(), d["High"].to_numpy()
    d["swing_low"], d["swing_high"], d["swing_low_bar"], d["swing_high_bar"] = _confirmed_swings(
        low, high, day, p.swing_left, p.swing_right
    )
    # Fallback stop level: the extreme of the PRECEDING `stop_fallback_lookback` completed bars of
    # the same session, excluding the signal bar itself.
    d["fallback_low"], d["fallback_high"], d["fallback_low_bar"], d["fallback_high_bar"] = (
        _fallback_extremes(low, high, day, p.stop_fallback_lookback)
    )
    d["chop_changes"], d["chop_mean_abs"] = _chop_stats(d["disp"].to_numpy(), day, p.chop_lookback)
    d["reversal"] = _reversals(
        d["disp"].to_numpy(), d["Close"].to_numpy(), p.early_threshold, p.reversal_move
    )
    # Volatility regime (diagnostics only): ATR against its median over about the last 20 sessions
    # of continuous-trading bars, current bar included.
    d["atr_regime"] = (
        d["atr"] / d["atr"].rolling(REGIME_BARS, min_periods=REGIME_BARS // 4).median()
    )
    d["minute"] = _minute_of_day(d.index)
    d["bar_no"] = np.arange(len(d))
    return d


# ---- entry --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryDecision:
    """The verdict on one closed bar that carried a trigger. `accepted` False = skipped."""

    direction: int
    reason: str
    accepted: bool
    stop: float = float("nan")
    stop_basis: str = ""
    context: dict = field(default_factory=dict)


def trigger(cur: float, prev: float, p: EarlyDisplacementParams) -> int:
    """+1 / -1 when this bar's displacement transition is a long / short trigger, else 0."""
    if np.isnan(cur) or np.isnan(prev):
        return 0
    if p.entry_mode == "crossover":
        if prev <= 0 < cur:
            return 1
        if prev >= 0 > cur:
            return -1
        return 0
    t = p.early_threshold
    if prev <= -t < cur:
        return 1
    if prev >= t > cur:
        return -1
    return 0


def initial_stop(
    row: pd.Series, direction: int, p: EarlyDisplacementParams
) -> tuple[float, str, float, float]:
    """(stop, basis, structural level, level bar position) for an entry at this closed bar.

    Long: below the latest confirmed swing low; short: above the latest confirmed swing high; the
    buffer is stop_buffer_atr x ATR beyond the level. The stop is NaN when no level sits on the
    loss side of the signal close.
    """
    close, a = float(row["Close"]), float(row["atr"])
    side = "low" if direction == 1 else "high"
    level, at = float(row[f"swing_{side}"]), float(row[f"swing_{side}_bar"])
    basis = "SWING"
    if np.isnan(level) or direction * (close - level) <= 0:
        level, at = float(row[f"fallback_{side}"]), float(row[f"fallback_{side}_bar"])
        basis = "FALLBACK_EXTREME"
    stop = level - direction * p.stop_buffer_atr * a
    if np.isnan(stop) or direction * (close - stop) <= 0:
        return float("nan"), "", float("nan"), float("nan")
    return stop, basis, level, at


def _accepted_reason(direction: int, p: EarlyDisplacementParams) -> str:
    if p.entry_mode == "crossover":
        return CROSSOVER_LONG if direction == 1 else CROSSOVER_SHORT
    if p.momentum_confirm:
        return EARLY_LONG_MOMENTUM_CONFIRMED if direction == 1 else EARLY_SHORT_MOMENTUM_CONFIRMED
    return EARLY_LONG_DISPLACEMENT if direction == 1 else EARLY_SHORT_DISPLACEMENT


def _entry_context(row: pd.Series, direction: int, p: EarlyDisplacementParams) -> dict:
    close, a = float(row["Close"]), float(row["atr"])
    return {
        "ema9": float(row["ema_f"]),
        "ema20": float(row["ema_s"]),
        "vwap": float(row["vwap"]),
        "atr": a,
        "disp": float(row["disp"]),
        "disp_prev": float(row["disp_prev"]),
        "disp_roc": float(row["roc"]),
        "early_threshold": -direction * p.early_threshold if p.entry_mode == "early" else 0.0,
        "dist_vwap_atr": (close - float(row["vwap"])) / a,
        "dist_ema20_atr": (close - float(row["ema_s"])) / a,
        "ema20_aligned": bool(direction * (close - float(row["ema_s"])) > 0),
        "ema9_to_vwap_atr": float(row["disp"]),  # the displacement itself, named for the report
        # None (not NaN) while under ~5 sessions of history, so the key is always present
        "atr_regime": (
            None
            if "atr_regime" not in row or np.isnan(row["atr_regime"])
            else float(row["atr_regime"])
        ),
        "chop_changes": float(row["chop_changes"]),
        "chop_mean_abs": float(row["chop_mean_abs"]),
        "market_state": market_state(
            float(row["disp"]), float(row["disp_prev"]), p.early_threshold
        ),
    }


def evaluate_bar(row: pd.Series, p: EarlyDisplacementParams) -> EntryDecision | None:
    """Decision for one closed input row, or None when it carries no trigger at all."""
    cur, prev = float(row["disp"]), float(row["disp_prev"])
    direction = trigger(cur, prev, p)
    if direction == 0:
        return None
    ctx = _entry_context(row, direction, p)

    def skip(reason: str) -> EntryDecision:
        return EntryDecision(direction, reason, False, context=ctx)

    if int(row["bar_no"]) < p.min_history_bars or np.isnan(float(row["atr"])):
        return skip(FILTERED_WARMUP)
    if not p.min_entry_min <= int(row["minute"]) < p.entry_cutoff_min:
        return skip(FILTERED_OUTSIDE_WINDOW)
    if p.momentum_confirm and not direction * (cur - prev) > p.min_displacement_roc:
        return skip(FILTERED_MOMENTUM_CONTRACTION)
    if direction * cur > p.max_entry_displacement:
        return skip(FILTERED_EXCESSIVE_EXTENSION)
    if p.ema20_filter == "strict" and not ctx["ema20_aligned"]:
        return skip(FILTERED_EMA20_CONFLICT)
    if p.chop_filter:
        changes, mean_abs = float(row["chop_changes"]), float(row["chop_mean_abs"])
        if (
            not np.isnan(changes)
            and changes > p.chop_max_sign_changes
            and mean_abs < p.chop_min_mean_abs_disp
        ):
            return skip(FILTERED_VWAP_CHOP)
    stop, basis, level, level_bar = initial_stop(row, direction, p)
    if np.isnan(stop):
        return skip(FILTERED_NO_STRUCTURE)
    risk = abs(float(row["Close"]) - stop)
    stop_atr = risk / float(row["atr"])
    ctx = {
        **ctx,
        "initial_stop": stop,
        "stop_basis": basis,
        "swing_level": level,
        "entry_to_swing_atr": abs(float(row["Close"]) - level) / float(row["atr"]),
        # bars from the structural level's own bar to the signal bar (pivot bar for SWING)
        "swing_age_bars": int(row["bar_no"]) - int(level_bar),
        "initial_risk": risk,
        "stop_atr": stop_atr,
        "wide_stop": bool(stop_atr > p.wide_stop_atr),
    }
    return EntryDecision(direction, _accepted_reason(direction, p), True, stop, basis, ctx)


def evaluate_frame(
    inputs: pd.DataFrame, p: EarlyDisplacementParams, since: pd.Timestamp | None = None
) -> dict[pd.Timestamp, EntryDecision]:
    """Every triggered bar of an input frame, accepted or skipped, keyed by bar open time.

    `since` limits the scan to bars opening at or after it (the runner only needs today).
    """
    out: dict[pd.Timestamp, EntryDecision] = {}
    disp, prev = inputs["disp"].to_numpy(), inputs["disp_prev"].to_numpy()
    usable = ~np.isnan(disp) & ~np.isnan(prev)
    if since is not None:
        usable &= np.asarray(inputs.index >= since)
    for k in np.flatnonzero(usable):
        decision = evaluate_bar(inputs.iloc[k], p)
        if decision is not None:
            out[inputs.index[k]] = decision
    return out


# ---- trailing stop ------------------------------------------------------------------------------


@dataclass(frozen=True)
class TrailBar:
    """One closed bar as the trailing stop reads it."""

    open: float
    high: float
    low: float
    close: float
    disp: float
    atr: float
    swing_low: float
    swing_high: float
    time: pd.Timestamp | None = None


@dataclass(frozen=True)
class TrailExit:
    reason: str  # TRAILING_STOP, MOMENTUM_FAILURE, or INITIAL_STOP (owned by the broker stop)
    stop: float  # the stop level that decided it
    trigger: str  # "touched", "closed_beyond" or "event"


def trail_bar(row: pd.Series) -> TrailBar:
    return TrailBar(
        float(row["Open"]),
        float(row["High"]),
        float(row["Low"]),
        float(row["Close"]),
        float(row["disp"]),
        float(row["atr"]),
        float(row["swing_low"]),
        float(row["swing_high"]),
        row.name if isinstance(row.name, pd.Timestamp) else None,
    )


class TrailState:
    """Trade management for one open position. Stepped once per CLOSED bar from the fill bar on.

    Position states: LONG/SHORT until the EMA/VWAP crossover, then *_EXPANSION, *_CONTRACTION while
    displacement falls back for `contraction_bars` bars, MOMENTUM_FAILURE once displacement crosses
    back through zero after the crossover. Contraction and failure tighten the ATR distance; they
    never exit by themselves unless `on_momentum_failure` is "exit".
    """

    def __init__(
        self, direction: int, entry: float, initial_stop: float, p: EarlyDisplacementParams
    ):
        if direction not in (1, -1):
            raise ValueError(f"direction must be 1 or -1, got {direction}")
        if not direction * (entry - initial_stop) > 0:
            raise ValueError(
                f"initial stop {initial_stop} is not on the loss side of entry {entry}"
            )
        self.d = direction
        self.p = p
        self.entry = float(entry)
        self.initial_stop = float(initial_stop)
        self.risk = abs(self.entry - self.initial_stop)
        self.stop = float(initial_stop)  # in force for the NEXT bar
        self.best = self.entry  # most favourable price since entry
        self.worst = self.entry
        self.activated = p.trailing_activation == "immediate"
        self.crossed = False
        self.cross_time: pd.Timestamp | None = None
        self.cross_price = float("nan")
        self.failed = False
        self.contracted = False
        self._falling = 0
        self._prev_disp = float("nan")
        self.mfe_pre = self.mae_pre = self.mfe_post = self.mae_post = 0.0
        self.bars = 0
        self.last_close = float("nan")
        self.exit: TrailExit | None = None
        # raw per-bar facts for research (diagnostics only, never read by a decision)
        self.path: list[dict] = []
        self.disp_at_cross = float("nan")
        self.disp_max = float("-inf")
        self.disp_min = float("inf")
        self.last_disp = float("nan")
        self.bars_to_mfe = 0
        self.bars_to_mae = 0

    @property
    def trailed(self) -> bool:
        """True once the stop has moved off the initial (broker) stop."""
        return self.d * (self.stop - self.initial_stop) > 1e-9

    @property
    def state(self) -> str:
        side = "BULLISH" if self.d == 1 else "BEARISH"
        if self.failed:
            return "MOMENTUM_FAILURE"
        if self.crossed:
            return f"{side}_CONTRACTION" if self.contracted else f"{side}_EXPANSION"
        return "LONG" if self.d == 1 else "SHORT"

    def _r(self, price: float) -> float:
        return self.d * (price - self.entry) / self.risk

    def _tighten(self, candidate: float) -> None:
        if np.isnan(candidate):
            return
        self.stop = max(self.stop, candidate) if self.d == 1 else min(self.stop, candidate)

    def _multiple(self) -> float:
        m = self.p.trailing_atr_multiple
        if self.contracted:
            m = min(m, self.p.contraction_atr_multiple)
        if self.failed:
            m = min(m, self.p.failure_atr_multiple)
        return m

    def _candidate(self, b: TrailBar) -> float:
        atr_c = self.best - self.d * self._multiple() * b.atr
        swing = b.swing_low if self.d == 1 else b.swing_high
        struct_c = swing - self.d * self.p.trailing_buffer_atr * b.atr
        if self.p.trail_mode == "atr":
            return atr_c
        if self.p.trail_mode == "structure":
            return struct_c
        if np.isnan(struct_c):
            return atr_c
        return max(atr_c, struct_c) if self.d == 1 else min(atr_c, struct_c)

    def _events(self, b: TrailBar) -> None:
        d, disp = self.d, b.disp
        if np.isnan(disp):
            return
        if not self.crossed and d * disp > 0:
            self.crossed, self.cross_time, self.cross_price = True, b.time, b.close
            self.disp_at_cross = disp
        if self.crossed and not self.failed and d * disp < 0:
            self.failed = True
        if self.crossed and not np.isnan(self._prev_disp) and d * (disp - self._prev_disp) < 0:
            self._falling += 1
        else:
            self._falling = 0
        self.contracted = self.crossed and self._falling >= self.p.contraction_bars
        self._prev_disp = disp
        if not self.activated:
            mode = self.p.trailing_activation
            if mode == "crossover":
                self.activated = self.crossed
            elif mode == "r_multiple":
                self.activated = self._r(self.best) >= self.p.trailing_activation_r
            elif mode == "displacement":
                self.activated = d * disp >= self.p.trailing_activation_displacement

    def _excursions(self, b: TrailBar, adverse_limit: float | None = None) -> None:
        """Update excursions with one bar. `adverse_limit` caps the adverse side at the price the
        position was actually closed at (the initial stop bar: the stop, or the open on a gap)."""
        fav = b.high if self.d == 1 else b.low
        adv = b.low if self.d == 1 else b.high
        if adverse_limit is not None:
            adv = adverse_limit
        before = (max(self.mfe_pre, self.mfe_post), max(self.mae_pre, self.mae_post))
        self.best = max(self.best, fav) if self.d == 1 else min(self.best, fav)
        self.worst = min(self.worst, adv) if self.d == 1 else max(self.worst, adv)
        mfe, mae = max(self._r(fav), 0.0), max(-self._r(adv), 0.0)
        if self.crossed:
            self.mfe_post, self.mae_post = max(self.mfe_post, mfe), max(self.mae_post, mae)
        else:
            self.mfe_pre, self.mae_pre = max(self.mfe_pre, mfe), max(self.mae_pre, mae)
        if mfe > before[0]:
            self.bars_to_mfe = self.bars
        if mae > before[1]:
            self.bars_to_mae = self.bars

    def _note(self, b: TrailBar, in_force: float) -> None:
        """Append this bar's raw facts to the path (diagnostics only)."""
        if not np.isnan(b.disp):
            self.disp_max, self.disp_min = max(self.disp_max, b.disp), min(self.disp_min, b.disp)
            self.last_disp = b.disp
        self.path.append(
            {
                "time": None if b.time is None else str(b.time),
                "bar": self.bars,
                "stop_in_force": in_force,
                "stop_after": self.stop,
                "activated": self.activated,
                "state": self.state,
                "mfe_r": max(self.mfe_pre, self.mfe_post),
                "mae_r": max(self.mae_pre, self.mae_post),
                "close_r": self._r(b.close),
            }
        )

    def step(self, b: TrailBar) -> TrailExit | None:
        """Advance one closed bar. Returns the exit this bar decided, else None. Sticky once exited."""
        if self.exit is not None:
            return self.exit
        self.bars += 1
        self.last_close = b.close
        d = self.d
        in_force = self.stop
        if (b.low <= self.initial_stop) if d == 1 else (b.high >= self.initial_stop):
            # The broker SL-M (backtest: the engine stop) owns this exit, intrabar.
            gapped = (b.open <= self.initial_stop) if d == 1 else (b.open >= self.initial_stop)
            self._excursions(b, adverse_limit=b.open if gapped else self.initial_stop)
            self.exit = TrailExit(INITIAL_STOP, self.initial_stop, "touched")
            self._note(b, in_force)
            return self.exit
        self._excursions(b)
        if self.trailed and ((b.low <= in_force) if d == 1 else (b.high >= in_force)):
            self.exit = TrailExit(TRAILING_STOP, in_force, "touched")
            self._note(b, in_force)
            return self.exit
        self._events(b)
        if self.activated:
            self._tighten(self._candidate(b))
        if self.trailed and d * (b.close - self.stop) <= 0:
            self.exit = TrailExit(TRAILING_STOP, self.stop, "closed_beyond")
        elif self.failed and self.p.on_momentum_failure == "exit":
            self.exit = TrailExit(MOMENTUM_FAILURE, self.stop, "event")
        self._note(b, in_force)
        return self.exit

    def snapshot(self) -> dict:
        """Diagnostics for the trade record."""
        return {
            "trail_state": self.state,
            "trailing_stop": self.stop,
            "initial_stop": self.initial_stop,
            "trail_activated": self.activated,
            "best_price_since_entry": self.best,
            "worst_price_since_entry": self.worst,
            "mfe_r": max(self.mfe_pre, self.mfe_post),
            "mae_r": max(self.mae_pre, self.mae_post),
            "mfe_pre_cross_r": self.mfe_pre,
            "mae_pre_cross_r": self.mae_pre,
            "mfe_post_cross_r": self.mfe_post,
            "mae_post_cross_r": self.mae_post,
            "crossover_occurred": self.crossed,
            "crossover_time": None if self.cross_time is None else str(self.cross_time),
            "crossover_price": None if np.isnan(self.cross_price) else self.cross_price,
            # price advantage of the early entry over the crossover close, in R (positive = earlier was better)
            "entry_advantage_vs_cross_r": None
            if np.isnan(self.cross_price)
            else self._r(self.cross_price),
            "momentum_contracted": self.contracted,
            "momentum_failed": self.failed,
            "bars_held": self.bars,
            "last_bar_close": None if np.isnan(self.last_close) else self.last_close,
            "bars_to_mfe": self.bars_to_mfe,
            "bars_to_mae": self.bars_to_mae,
            "disp_at_cross": None if np.isnan(self.disp_at_cross) else self.disp_at_cross,
            "disp_max_in_trade": None if self.disp_max == float("-inf") else self.disp_max,
            "disp_min_in_trade": None if self.disp_min == float("inf") else self.disp_min,
            "disp_at_last_bar": None if np.isnan(self.last_disp) else self.last_disp,
        }


def replay_trail(
    inputs: pd.DataFrame,
    fill_time: pd.Timestamp,
    upto_time: pd.Timestamp,
    direction: int,
    entry: float,
    stop: float,
    p: EarlyDisplacementParams,
) -> tuple[TrailState, pd.Timestamp | None]:
    """Step a fresh TrailState over the closed bars [fill_time, upto_time].

    Returns the state and the open time of the bar that decided an exit (None while open).
    A stateless replay: a restart mid-trade rebuilds the same stop from the same bars.
    """
    state = TrailState(direction, entry, stop, p)
    window = inputs.loc[(inputs.index >= fill_time) & (inputs.index <= upto_time)]
    for ts, row in window.iterrows():
        if state.step(trail_bar(row)) is not None:
            return state, ts
    return state, None
