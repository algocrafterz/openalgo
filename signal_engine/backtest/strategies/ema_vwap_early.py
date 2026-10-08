"""EVWAP-EARLY-PY: a thin backtest adapter over strategy_core.ema_vwap_early.

ONE CODE PATH. Entry rules, the initial stop and the trailing stop live in the core; the paper
runner (pipeline.strategies.ema_vwap_early + pipeline.trail_driver) calls the same functions.

Execution model, matching paper/live:
  * entry: signal on a closed bar, fill at the next bar's open (engine default);
  * initial stop: the engine stop at the signal stop, checked intrabar (the broker SL-M);
    `trail()` is never used, so the engine stop never moves, as the broker SL-M never moves;
  * trailing stop: `custom_exit` steps the core's TrailState at each closed bar and the exit fills
    at the next bar's open (`custom_exit_fill="next_open"`, a market order sent after the close);
  * no take-profit: `tp` is NaN, so the engine never fills a target;
  * time exit: the deployment profile (14:45), applied by the engine.

Baselines from the requirement (section 31) are parameter sets of the same core, see `BASELINES`:
  A crossover, B early, C early + momentum, D early + momentum + EMA20 (the paper default).

`diagnostics` collects one dict per closed trade (TrailState.snapshot plus the entry context and
the exit reason), accumulated over every symbol run on this instance.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from signal_engine.backtest import indicators as ind
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal, Position
from signal_engine.strategy_core.ema_vwap_early import (
    BACKTEST_TP_NONE,
    INITIAL_STOP,
    EarlyDisplacementParams,
    TrailBar,
    TrailState,
    build_inputs,
    evaluate_frame,
)

__all__ = ["BASELINES", "EarlyDisplacementParams", "EmaVwapEarly"]

BASELINES: dict[str, EarlyDisplacementParams] = {
    "A_crossover": EarlyDisplacementParams(
        entry_mode="crossover", ema20_filter="disabled", chop_filter=False
    ),
    "B_early": EarlyDisplacementParams(
        momentum_confirm=False, ema20_filter="disabled", chop_filter=False
    ),
    "C_early_momentum": EarlyDisplacementParams(ema20_filter="disabled", chop_filter=False),
    "D_early_momentum_ema20": EarlyDisplacementParams(),
}

_INPUT_COLS = (
    "ema_f",
    "ema_s",
    "vwap",
    "atr",
    "disp",
    "disp_prev",
    "roc",
    "swing_low",
    "swing_high",
)


class EmaVwapEarly(Strategy):
    name = "ema_vwap_early - EMA/VWAP/ATR early displacement, trailing stop, no TP"
    tag = "EVWAP-EARLY-PY"
    pine = ""  # destination python; the discretionary reference is the .lipi study
    run_overrides = {
        "skip_open_minutes": 0,
        # the core's own window [09:20, 14:15) decides; this engine cutoff never binds before it
        "entry_cutoff_min": 14 * 60 + 20,
        "max_trades_per_day": 50,
        "custom_exit_fill": "next_open",
        "warmup_bars": 60,
    }

    def __init__(self) -> None:
        self._trail: TrailState | None = None
        self._entry_ctx: dict = {}
        self._decisions: dict = {}
        self.diagnostics: list[dict] = []

    def prepare_key(self, p: EarlyDisplacementParams) -> tuple:
        return tuple(sorted(vars(p).items()))

    def prepare(self, df: pd.DataFrame, p: EarlyDisplacementParams) -> pd.DataFrame:
        d = ind.add_session_columns(df)
        inputs = build_inputs(d, p)
        # Closing auction rows keep NaN inputs: they never signal and never move the trail.
        for col in _INPUT_COLS:
            d[col] = inputs[col].reindex(d.index)
        decisions = evaluate_frame(inputs, p)
        sig_dir = np.zeros(len(d), dtype=np.int8)
        sig_stop = np.full(len(d), np.nan)
        pos_of = {ts: k for k, ts in enumerate(d.index)}
        for ts, dec in decisions.items():
            if dec.accepted:
                k = pos_of[ts]
                sig_dir[k], sig_stop[k] = dec.direction, dec.stop
        d["sig_dir"], d["sig_stop"] = sig_dir, sig_stop
        self._decisions = decisions
        return d

    def reset_symbol(self, p) -> None:
        self._trail = None
        self._entry_ctx = {}

    # ---- entry -----------------------------------------------------------

    def entry(
        self, c: Ctx, i: int, p: EarlyDisplacementParams, direction: int
    ) -> EntrySignal | None:
        if int(c["sig_dir"][i]) != direction:
            return None
        dec = self._decisions.get(c.index[i])
        return EntrySignal(
            direction=direction,
            sl=float(c["sig_stop"][i]),
            tp=BACKTEST_TP_NONE,  # take_profit none (see strategy_core.ema_vwap_early)
            tag=dec.reason if dec is not None else "",
            meta=dict(dec.context) if dec is not None else {},
        )

    def on_entry(self, c: Ctx, i: int, p, sig: EntrySignal) -> None:
        self._entry_ctx = {"signal_time": str(c.index[i]), "signal_reason": sig.tag, **sig.meta}

    def on_fill(self, c: Ctx, i: int, p: EarlyDisplacementParams, pos: Position) -> None:
        try:
            self._trail = TrailState(pos.direction, pos.entry, pos.sl, p)
        except ValueError:
            self._trail = None  # filled through the stop; the engine closes it at once

    # ---- exit ------------------------------------------------------------

    def custom_exit(self, c: Ctx, i: int, p, pos: Position) -> str | None:
        if self._trail is None:
            return None
        bar = TrailBar(
            float(c["Open"][i]),
            float(c["High"][i]),
            float(c["Low"][i]),
            float(c["Close"][i]),
            float(c["disp"][i]),
            float(c["atr"][i]),
            float(c["swing_low"][i]),
            float(c["swing_high"][i]),
            c.index[i],
        )
        decided = self._trail.step(bar)
        if decided is None or decided.reason == INITIAL_STOP:
            return None
        return decided.reason

    def on_exit(self, c: Ctx, i: int, p, trade) -> None:
        if self._trail is not None:
            self.diagnostics.append(
                {
                    "symbol": c.symbol,
                    "exit_time": str(c.index[i]),
                    "exit_reason": getattr(trade, "reason", ""),
                    **self._entry_ctx,
                    **self._trail.snapshot(),
                }
            )
        self._trail = None
        self._entry_ctx = {}
