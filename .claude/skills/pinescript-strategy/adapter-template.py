"""Backtest adapter template - copy to signal_engine/backtest/strategies/<name>.py.

The adapter answers only "would this bar produce an entry, and with what stop and
target". The engine owns sessions, trade caps, next-bar fills, stop/target resolution,
costs and bookkeeping.

Run it with:
    uv run --group analysis python -m signal_engine.backtest <name> --full
(after adding the strategy to REGISTRY in signal_engine/backtest/__main__.py)
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from signal_engine.backtest import indicators as ind
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal, Position


@dataclass(frozen=True)
class MyParams:
    """Strategy-specific inputs ONLY.

    Session, cost and trade-cap settings live in RunConfig, not here. Defaults must
    match the PineScript's defaults so MyParams() reproduces what actually trades.
    """

    fast_len: int = 9
    atr_mult: float = 1.0
    tp_r: float = 2.0
    use_vwap: bool = True

    def with_(self, **kw) -> "MyParams":
        return replace(self, **kw)


class MyStrategy(Strategy):
    name = "my strategy"
    tag = "MYSTRAT"                                   # matches strategies.py
    pine = "signal_engine/pinescripts/.../my.pine"    # so a reader can diff the two

    def __init__(self):
        self._reset()

    def _reset(self):
        """All mutable state in one place, so both reset hooks stay honest."""
        self.armed = False

    def reset_symbol(self, p):
        self._reset()

    def reset_session(self, p):
        self._reset()

    def prepare_key(self, p) -> tuple:
        # ONLY the fields prepare() reads. A narrow key makes ablation() much cheaper;
        # a wrong one silently reuses stale indicator columns, so list every length.
        return (p.fast_len,)

    def prepare(self, df: pd.DataFrame, p: MyParams) -> pd.DataFrame:
        """Attach every column entry() reads. Called once per symbol.

        Anything derived from a completed higher-timeframe or previous-day value MUST
        be shifted here. add_session_columns supplies day / mins / new_session /
        from_open, which the engine requires.
        """
        df = ind.add_session_columns(df)
        day = df["day"]
        df["ema_f"] = ind.ema(df["Close"], p.fast_len)
        df["atr"] = ind.atr(df)
        df["vwap"] = ind.session_vwap(df, day)
        # Drop the warmup rows so entry() never sees a half-formed indicator.
        return df.dropna(subset=["atr", "ema_f"])

    def on_bar(self, c: Ctx, i: int, p: MyParams) -> None:
        """Optional multi-step state machine, e.g. "level broke, now await the pullback".

        Runs after position management and before entry(). Bar i is CLOSED.
        """

    def entry(self, c: Ctx, i: int, p: MyParams, direction: int) -> EntrySignal | None:
        """Return a signal to fill at bar i+1's open, or None.

        The engine has already checked the session window, the per-day trade cap and
        the direction permissions. NEVER read c[...][i + 1].
        """
        is_long = direction == 1
        close, ema_f, atr_i = c["Close"], c["ema_f"], c["atr"][i]

        if p.use_vwap and not ((close[i] > c["vwap"][i]) if is_long else (close[i] < c["vwap"][i])):
            return None
        if not ((close[i] > ema_f[i]) if is_long else (close[i] < ema_f[i])):
            return None

        sl = close[i] - atr_i * p.atr_mult if is_long else close[i] + atr_i * p.atr_mult
        risk = abs(close[i] - sl)
        # Guard NaN EXPLICITLY. `risk <= 0` is False when risk is NaN, so a comparison-
        # only guard lets a NaN stop straight through and the setup is lost silently.
        if risk <= 0 or np.isnan(risk):
            return None
        tp = close[i] + risk * p.tp_r if is_long else close[i] - risk * p.tp_r
        return EntrySignal(direction=direction, sl=float(sl), tp=float(tp), tag="MY-SETUP")

    def on_entry(self, c: Ctx, i: int, p: MyParams, sig: EntrySignal) -> None:
        """Consume the setup so it cannot arm a second entry."""
        self.armed = False

    def trail(self, c: Ctx, i: int, p: MyParams, pos: Position) -> float | None:
        """New stop level, or None. The engine only ever ratchets it."""
        return None

    def custom_exit(self, c: Ctx, i: int, p: MyParams, pos: Position) -> str | None:
        """Exit reason to close at this bar's close, or None. Checked after stop/target."""
        return None
