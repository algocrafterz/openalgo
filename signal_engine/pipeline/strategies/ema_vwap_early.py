"""EVWAP-EARLY-PY as a pipeline Strategy: strategy_core.ema_vwap_early wrapped in the protocol.

The rules live in `strategy_core.ema_vwap_early`. This file builds the input frame, keeps the
accepted decisions, and reshapes each into a Fire. Skipped triggers are kept on
`last_skips` ((symbol, bar time) -> reason code) for diagnostics.

The strategy has NO take-profit. Fires carry no targets and `context["take_profit"] = "none"`,
so the Signal's tp is 0, which means "none". Invariant: no execution path may read a target for
this strategy. The validator accepts tp 0 only for an exit plan without targets (and rejects any
other tp for it), the bad-fill guard has no target to overshoot, the tracker's no-progress and
profit-lock rules skip runner-owned exits, and no TP order is ever placed. Exits are the trailing
driver (pipeline.trail_driver) and the 14:45 time exit.
"""

from __future__ import annotations

import math

import pandas as pd
from loguru import logger

from signal_engine.pipeline.strategy import Fire
from signal_engine.strategy_core.ema_vwap_early import (
    PRE_SIGNAL_BARS,
    TAKE_PROFIT,
    EarlyDisplacementParams,
    EntryDecision,
    build_inputs,
    evaluate_frame,
)

TAG = "EVWAP-EARLY-PY"
#: Prior sessions of 5-minute bars: EMA20 and ATR14 settle well inside 20 sessions (1,400+ bars).
DEFAULT_WARMUP_SESSIONS = 20


def _r4(v) -> float | None:
    v = float(v)
    return None if math.isnan(v) else round(v, 4)


def _trajectory(inputs: pd.DataFrame, bar_time: pd.Timestamp) -> dict:
    """Raw displacement path into the signal: the last PRE_SIGNAL_BARS same-session bars up to and
    including the signal bar (diagnostics only)."""
    upto = inputs.loc[:bar_time]
    upto = upto[upto.index.date == bar_time.date()].tail(PRE_SIGNAL_BARS)
    return {
        "pre_signal_path": [
            {
                "time": str(t),
                "close": _r4(r["Close"]),
                "disp": _r4(r["disp"]),
                "roc": _r4(r["roc"]),
                "vwap": _r4(r["vwap"]),
                "ema9": _r4(r["ema_f"]),
                "ema20": _r4(r["ema_s"]),
                "atr": _r4(r["atr"]),
            }
            for t, r in upto.iterrows()
        ]
    }


def _clustering(accepted: list, bar_time: pd.Timestamp, interval_min: int) -> dict:
    """Earlier accepted signals on this symbol today (traded or not): count, and the previous one."""
    prior = [(t, d) for t, d in accepted if t < bar_time]
    out: dict = {"signals_today_before": len(prior)}
    if prior:
        t, d = prior[-1]
        out["prev_signal_time"] = str(t)
        out["prev_signal_direction"] = "LONG" if d.direction == 1 else "SHORT"
        out["bars_since_prev_signal"] = int((bar_time - t) / pd.Timedelta(minutes=interval_min))
    return out


def _clean(ctx: dict) -> dict:
    return {k: v for k, v in ctx.items() if not (isinstance(v, float) and math.isnan(v))}


class EarlyDisplacementStrategy:
    interval_min = 5
    needs_1m = False

    def __init__(
        self,
        params: EarlyDisplacementParams | None = None,
        name: str = TAG,
        warmup_sessions: int = DEFAULT_WARMUP_SESSIONS,
    ) -> None:
        self.name = name
        self.params = params or EarlyDisplacementParams()
        self.warmup_sessions = warmup_sessions
        self.entry_window_min = (self.params.min_entry_min, self.params.entry_cutoff_min)
        #: (symbol, bar time) -> skip reason code from the evaluate() calls of this session
        self.last_skips: dict[tuple[str, pd.Timestamp], str] = {}

    def inputs(self, df5m: pd.DataFrame) -> pd.DataFrame:
        """The core's input frame for a 5-minute window (warm-up sessions, then today)."""
        return build_inputs(df5m, self.params)

    def evaluate(self, symbol: str, df5m: pd.DataFrame, df1m: pd.DataFrame) -> list[Fire]:
        inputs = self.inputs(df5m)
        if inputs.empty:
            return []
        # Only the latest session can produce a bar the runner acts on.
        decisions = evaluate_frame(inputs, self.params, since=inputs.index[-1].normalize())
        fires = []
        last = inputs.index[-1]
        accepted = [(t, d) for t, d in decisions.items() if d.accepted]
        for bar_time, decision in decisions.items():
            if decision.accepted:
                extra = {
                    **_trajectory(inputs, bar_time),
                    **_clustering(accepted, bar_time, self.interval_min),
                }
                fires.append(self._fire(symbol, bar_time, decision, df5m, extra))
                continue
            self.last_skips[(symbol, bar_time)] = decision.reason
            if bar_time == last:  # each bar is evaluated once live, so this logs each skip once
                direction = "LONG" if decision.direction == 1 else "SHORT"
                logger.info(
                    f"{self.name} {symbol} {bar_time:%H:%M} {direction} trigger skipped: "
                    f"{decision.reason} (disp {decision.context.get('disp', float('nan')):.2f})"
                )
        return fires

    def _fire(
        self,
        symbol: str,
        bar_time: pd.Timestamp,
        decision: EntryDecision,
        df5m: pd.DataFrame,
        extra: dict | None = None,
    ) -> Fire:
        entry = float(df5m.loc[bar_time, "Close"])
        context = _clean(decision.context)
        context.update(
            {
                "signal_reason": decision.reason,
                "signal_time": str(bar_time),
                "signal_price": entry,
                "take_profit": TAKE_PROFIT,
                "exit_model": "trailing_stop_no_tp",
                **(extra or {}),
            }
        )
        return Fire(
            symbol=symbol,
            bar_time=bar_time,
            direction=decision.direction,
            entry_ref=entry,
            sl=float(decision.stop),
            targets=(),  # no take-profit
            tag=decision.reason,
            context=context,
        )
