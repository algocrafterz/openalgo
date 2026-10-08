"""IDEA-029 VWAP 2SD range mean reversion: a thin backtest adapter over the strategy core.

ONE CODE PATH. Every rule (inputs, arming, replace-or-cancel, VWAP-touch cancel, confirmation,
arm window, cutoff, re-entry, the per-session cap, the stop, the skip-at-fill test and the
target) lives in `signal_engine.strategy_core.vwap_reversion`. This adapter only (1) attaches
the core's input columns in prepare(), (2) feeds each completed bar to the core's state machine
and (3) relays the engine's fill, skip and exit events back to it, which is the same sequence a
live runner follows with broker events.

Engine features this strategy opts into (both default-off for every other strategy):
  * `dynamic_target`: the target on bar j is the session VWAP at the close of bar j - 1, filled
    at that level, or at bar j's open when the bar opens beyond it; the stop wins a tie.
  * `skip_fill_beyond_levels`: an entry whose next-bar open is at/beyond the target or the stop
    is skipped (not entered and exited) and counted.

Diagnostics: `diagnostics()` returns the core's counters. They ACCUMULATE over every symbol and
every simulate() call made on this instance (reset_symbol and reset_session never zero them),
so a full-universe run on one instance reports universe totals. Use a fresh instance, or
`reset_diagnostics()`, for a separate count. The harness's look-ahead check deep-copies the
strategy, so it never adds to them.
"""

from __future__ import annotations

import pandas as pd

from signal_engine.backtest import indicators as ind
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.perturbation import structural, tunable
from signal_engine.backtest.types import Ctx, EntrySignal, Position
from signal_engine.strategy_core.vwap_reversion import (
    INPUT_COLUMNS,
    LAST_CONFIRM_MIN,
    MAX_TRADES_PER_SESSION,
    Confirmation,
    VwapReversionMachine,
    VwapReversionParams,
    bar_from_arrays,
    build_inputs,
    target_for_bar,
)

__all__ = ["VwapReversion", "VwapReversionParams"]

_OHLCV = ["Open", "High", "Low", "Close", "Volume"]
#: The live validator's global stop-distance floor (`risk.min_sl_pct`), pinned here so the run
#: does not read live config; a test fails if the two drift apart.
LIVE_MIN_SL_PCT = 0.005


class VwapReversion(Strategy):
    name = "vwap_reversion - VWAP 2SD range mean reversion (IDEA-029)"
    tag = "VWAP2SD"
    pine = ""  # destination python; no PineScript
    dynamic_target = True
    #: Only what the strategy owns. The time exit (14:45) and the MIS square-off come from the
    #: deployment profile via record_run, so they are not set here.
    run_overrides = {
        # fills 09:20 .. 14:15 at the next bar's open: signal bars 09:15 .. 14:10
        "skip_open_minutes": 0,
        "entry_cutoff_min": LAST_CONFIRM_MIN + 5,
        "max_trades_per_day": MAX_TRADES_PER_SESSION,
        "one_trade_per_direction": False,
        "allow_longs": True,
        "allow_shorts": True,
        "skip_fill_beyond_levels": True,
        # Execution constraint, not a strategy filter: the live validator rejects a stop closer
        # than risk.min_sl_pct (config.yaml, 0.5%) for any tag without a strategy_profiles
        # override, and this strategy has none. A signal live would reject is not traded here
        # either; the engine counts it as STOP_TOO_TIGHT.
        "min_sl_pct": LIVE_MIN_SL_PCT,
    }

    #: G4.1 declaration (owner-approved 2026-10-08): every parameter, with inclusive bounds. The
    #: bounds are validity limits, not search ranges; at the frozen values no +-20% neighbour
    #: reaches one. The warm-up length moves the data window, not the rule, so it is structural.
    param_meta = {
        "vwap_sd": tunable("float", 1.0, 3.5),
        "atr_period_5m": tunable("int", 5, 50),
        "min_vwap_distance_atr": tunable("float", 0.25, 3.0),
        "stop_buffer_atr": tunable("float", 0.05, 1.0),
        "adx_period_15m": tunable("int", 5, 30),
        "adx_max": tunable("float", 10.0, 40.0),
        "ema_regime_period_15m": tunable("int", 8, 60),
        "ema_regime_lookback_bars": tunable("int", 1, 12),
        "ema_regime_max_change_atr": tunable("float", 0.05, 1.0),
        "atr_period_15m": tunable("int", 5, 30),
        "ema_entry_period_5m": tunable("int", 3, 30),
        "arm_bars": tunable("int", 2, 18),
        "warmup_15m_bars": structural(
            "warm-up length of the 15-minute indicators; it moves the data window, not the rule"
        ),
    }

    def __init__(self) -> None:
        self._machine = VwapReversionMachine(VwapReversionParams())
        self._pending_conf: Confirmation | None = None

    # ---- inputs ----------------------------------------------------------------

    def _inputs(self, d: pd.DataFrame, p: VwapReversionParams) -> pd.DataFrame:
        return build_inputs(d[_OHLCV], p)

    def prepare(self, df: pd.DataFrame, p: VwapReversionParams) -> pd.DataFrame:
        d = ind.add_session_columns(df)
        inputs = self._inputs(d, p)
        for col in INPUT_COLUMNS:
            d[col] = inputs[col].to_numpy()
        return d

    # ---- state machine -------------------------------------------------------------

    def reset_symbol(self, p: VwapReversionParams) -> None:
        self._machine.p = p
        self._machine.reset_symbol()
        self._pending_conf = None

    def reset_session(self, p: VwapReversionParams) -> None:
        self._machine.reset_session()
        self._pending_conf = None

    def on_bar(self, c: Ctx, i: int, p: VwapReversionParams) -> None:
        self._pending_conf = self._machine.on_bar(bar_from_arrays(c.a, i))

    def entry(self, c: Ctx, i: int, p: VwapReversionParams, direction: int) -> EntrySignal | None:
        conf = self._pending_conf
        if conf is None or conf.bar != i or conf.direction != direction:
            return None
        return EntrySignal(
            direction=direction,
            sl=conf.stop,
            tp=conf.target,  # the entry bar's target: VWAP at the confirmation bar's close
            tag=f"{self.tag}-{'L' if direction == 1 else 'S'}",
            meta={"extreme": conf.extreme},
        )

    def on_fill(self, c: Ctx, i: int, p: VwapReversionParams, pos: Position) -> None:
        self._machine.on_fill(i)

    def on_fill_skipped(self, c: Ctx, i: int, p: VwapReversionParams, sig: EntrySignal) -> None:
        self._machine.on_fill_skipped(i)

    def on_exit(self, c: Ctx, i: int, p: VwapReversionParams, trade) -> None:
        self._machine.on_exit(i)

    def target(self, c: Ctx, j: int, p: VwapReversionParams, pos: Position | None) -> float:
        return target_for_bar(c["vwap"], j)

    # ---- reporting -----------------------------------------------------------------

    def diagnostics(self) -> dict[str, int]:
        return self._machine.diagnostics()

    def reset_diagnostics(self) -> None:
        self._machine.reset_diagnostics()
