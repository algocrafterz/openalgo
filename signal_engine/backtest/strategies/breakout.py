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

import numpy as np
import pandas as pd

from signal_engine.backtest import data
from signal_engine.backtest import indicators as ind
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal
from signal_engine.features.instruments import tick_size
from signal_engine.pipeline.strategies.breakout import BreakoutStrategy
from signal_engine.strategy_core.breakout import BreakoutParams
from signal_engine.strategy_core.exits import TP1_ONLY
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
    #: Spec v1.0 item 15: at most 2 filled entries per symbol and direction per day, so at most 4
    #: a day; a re-entry only after the position is flat (the engine arms only when flat).
    run_overrides = {
        "skip_open_minutes": 0,
        "entry_cutoff_min": 15 * 60 + 30,
        "max_entries_per_direction": 2,
        "max_trades_per_day": 4,
        "max_entry_slippage_r": 0.5,  # spec item 18 bad-fill model
        "exit_plan": TP1_ONLY,  # spec item 12: tp1_only, TP1 filled at the touching bar close
        "time_exit_at_open": True,  # spec item 19: 14:45 bar open, DATA_GAP when no bar exists
    }

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
        # The same Strategy object the pipeline runs in replay, paper and live. The input
        # builder is looked up at call time so a patched module attribute still applies.
        strategy = BreakoutStrategy(
            p,
            tick_source=self._tick,
            inputs_builder=lambda *a, **k: build_breakout_inputs(*a, **k),
        )
        fires = strategy.evaluate(symbol, bars, self._minutes(symbol, bars))

        d["fire_dir"] = 0.0
        d["fire_sl"] = np.nan
        d["fire_tp"] = np.nan
        d["fire_tp2"] = np.nan
        d["fire_tp3"] = np.nan
        d["fire_code"] = ""
        if fires:
            idx = pd.DatetimeIndex([f.bar_time for f in fires])
            d.loc[idx, "fire_dir"] = [float(f.direction) for f in fires]
            d.loc[idx, "fire_sl"] = [f.sl for f in fires]
            d.loc[idx, "fire_tp"] = [f.targets[0] for f in fires]
            d.loc[idx, "fire_tp2"] = [f.targets[1] for f in fires]
            d.loc[idx, "fire_tp3"] = [f.targets[2] for f in fires]
            d.loc[idx, "fire_code"] = [f.tag for f in fires]
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
