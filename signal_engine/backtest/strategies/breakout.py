"""BREAKOUT backtest adapter: a thin wrapper over strategy_core.breakout.

ONE CODE PATH. Every entry rule, gate, stop and target lives in
`signal_engine.strategy_core.breakout`; this adapter only (1) builds the core's input frame
from broker bars via `signal_engine.strategy_core.inputs` and (2) tells the shared engine
which bars fire. Paper (analyze) and live execution call the same core, so a change to the
strategy is made once and every mode picks it up. Nothing strategy-specific may be added here.

Data: broker 5-minute bars from the harness plus broker 1-minute bars (Historify) for the
value-area profile, and the broker tick size. No TradingView data is involved.

What the shared intraday engine does NOT model yet (explicit gaps, not approximations):
  * the TP ladder (TP1/TP1.5/TP2/TP3 partial exits) and runner stops - a trade here is scored
    on TP1 (the core's `tp1`) or the stop, as every other adapter is;
  * same-day re-entry cycles after a stop;
  * the day-type counter-bias veto (the core takes it as an input; it is not supplied).
The engine's own per-day trade cap and one-trade-per-direction rules apply on top of the
core's cooldown, so it is the engine, not the core, that enforces the session slot.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import numpy as np
import pandas as pd

from signal_engine.backtest import data
from signal_engine.backtest import indicators as ind
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal
from signal_engine.features.instruments import tick_size
from signal_engine.strategy_core.breakout import BreakoutParams, compute_signals
from signal_engine.strategy_core.inputs import build_breakout_inputs

__all__ = ["Breakout", "BreakoutParams"]

_OHLCV = ["Open", "High", "Low", "Close", "Volume"]


def _historify_minutes(symbol: str, df5: pd.DataFrame) -> pd.DataFrame:
    """Broker 1-minute bars covering the same span as the 5-minute frame."""
    start = df5.index[0].date().isoformat()
    end = (df5.index[-1] + pd.Timedelta(days=1)).date().isoformat()
    frames = data.from_historify(
        symbols=[symbol], interval="1m", start=start, end=end, min_sessions=1
    )
    if symbol not in frames:
        raise LookupError(f"no 1-minute Historify bars for {symbol} between {start} and {end}")
    return frames[symbol].astype("float64")


class Breakout(Strategy):
    name = "breakout - key-level engine (strategy_core.breakout)"
    tag = "BREAKOUT"
    pine = "signal_engine/strategy_library/intraday/orb/breakout.pine"
    #: The core enforces its own entry window (09:45 - 11:45), so the engine's generic
    #: opening skip and 11:00 cutoff must not cut it a second time.
    run_overrides = {"skip_open_minutes": 0, "entry_cutoff_min": 15 * 60 + 30}

    def __init__(
        self,
        minute_source: Callable[[str, pd.DataFrame], pd.DataFrame] | None = None,
        tick_source: Callable[[str], float] | None = None,
    ) -> None:
        self._minutes = minute_source or _historify_minutes
        self._tick = tick_source or tick_size

    def prepare(self, df: pd.DataFrame, p: BreakoutParams) -> pd.DataFrame:
        raise TypeError(
            "Breakout needs the symbol (1-minute bars, broker tick size); the harness calls prepare_for()"
        )

    def prepare_for(self, symbol: str, df: pd.DataFrame, p: BreakoutParams) -> pd.DataFrame:
        d = ind.add_session_columns(df)
        bars = d[_OHLCV].astype("float64")
        tick = self._tick(symbol)
        inputs = build_breakout_inputs(bars, self._minutes(symbol, bars), tick)
        fires = compute_signals(inputs, replace(p, tick=tick))

        d["fire_dir"] = 0.0
        d["fire_sl"] = np.nan
        d["fire_tp"] = np.nan
        d["fire_tp2"] = np.nan
        d["fire_tp3"] = np.nan
        d["fire_code"] = ""
        if not fires.empty:
            d.loc[fires.index, "fire_dir"] = fires["direction"].astype(float)
            d.loc[fires.index, "fire_sl"] = fires["sl"]
            d.loc[fires.index, "fire_tp"] = fires["tp1"]
            d.loc[fires.index, "fire_tp2"] = fires["tp2"]
            d.loc[fires.index, "fire_tp3"] = fires["tp3"]
            d.loc[fires.index, "fire_code"] = fires["code"]
        return d

    def entry(self, c: Ctx, i: int, p: BreakoutParams, direction: int) -> EntrySignal | None:
        if c["fire_dir"][i] != direction:
            return None
        return EntrySignal(
            direction=direction,
            sl=float(c["fire_sl"][i]),
            tp=float(c["fire_tp"][i]),
            tag=str(c["fire_code"][i]),
            # TP1/TP2/TP3 for the shared exit model; ignored unless RunConfig.exit_plan is set
            meta={
                "targets": (
                    float(c["fire_tp"][i]),
                    float(c["fire_tp2"][i]),
                    float(c["fire_tp3"][i]),
                )
            },
        )
