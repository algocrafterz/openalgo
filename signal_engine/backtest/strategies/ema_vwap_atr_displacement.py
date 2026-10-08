"""IDEA-032 - EMA-VWAP ATR early displacement (5m, NSE equity, long and short).

    displacement = (EMA9(close) - session VWAP(hlc3)) / ATR14

    LONG  : previous displacement <= -0.50 and current displacement > -0.50
    SHORT : previous displacement >= +0.50 and current displacement < +0.50
    EXIT  : displacement reaches 0, the EMA9/VWAP crossover (long >= 0, short <= 0)

The signal is judged on the completed bar. Entry and exit both fill at the NEXT bar open
(RunConfig.custom_exit_fill = "next_open"). There is no stop and no target in Benchmark A, so the
engine carries each trade as a return on the entry price, never as R.

Zero is the crossover and only an exit reference. The +/-0.35 WATCH level is a chart aid and
never generates a trade. No ADX, DI, slope or other filter is applied.

EMA9 and ATR14 run continuously across sessions. VWAP resets each session, so the first bar of a
session has no previous displacement (the previous bar belongs to a different VWAP) and cannot
signal.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from signal_engine.backtest import indicators as ind
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.types import Ctx, EntrySignal, Position


@dataclass(frozen=True)
class EmaVwapAtrDisplacementParams:
    ema_len: int = 9
    atr_len: int = 14
    early_threshold: float = 0.50
    exit_threshold: float = 0.0

    def with_(self, **kw) -> EmaVwapAtrDisplacementParams:
        return replace(self, **kw)


class EmaVwapAtrDisplacement(Strategy):
    name = "EMA-VWAP ATR early displacement"
    tag = "EVAD"
    pine = ""

    # The 09:15 bar cannot signal (no previous displacement in its session), so the 09:20 bar is the
    # first that can. There is no strategy-specific entry cutoff: the entry cutoff equals the
    # profile standing time exit (14:45), the engine's own rule, so the last signal bar opens at
    # 14:40 and the last fill is the 14:45 open. Re-entry is off: at most one trade per direction
    # per symbol-day.
    run_overrides = {
        "skip_open_minutes": 0,
        "entry_cutoff_min": 14 * 60 + 45,
        "max_trades_per_day": 2,
        "custom_exit_fill": "next_open",
        "warmup_bars": 100,
    }

    def prepare_key(self, p) -> tuple:
        return (p.ema_len, p.atr_len)

    def prepare(self, df: pd.DataFrame, p: EmaVwapAtrDisplacementParams) -> pd.DataFrame:
        d = ind.add_session_columns(df)
        day = d["day"]
        d["ema_f"] = ind.ema(d["Close"], p.ema_len)
        d["vwap"] = ind.session_vwap(d, day)
        d["atr"] = ind.atr(d, p.atr_len)
        atr = d["atr"].where(d["atr"] > 0)
        d["disp"] = (d["ema_f"] - d["vwap"]) / atr
        same_session = day.eq(day.shift(1)).to_numpy()
        d["disp_prev"] = d["disp"].shift(1).where(same_session)
        return d

    # ---- entry -----------------------------------------------------------

    def signal(self, c: Ctx, i: int, p: EmaVwapAtrDisplacementParams, direction: int) -> bool:
        cur, prev = float(c["disp"][i]), float(c["disp_prev"][i])
        if np.isnan(cur) or np.isnan(prev):
            return False
        t = p.early_threshold
        if direction == 1:
            return prev <= -t and cur > -t
        return prev >= t and cur < t

    def entry(self, c: Ctx, i: int, p: EmaVwapAtrDisplacementParams, direction: int) -> EntrySignal | None:
        if not self.signal(c, i, p, direction):
            return None
        return EntrySignal(
            direction=direction,
            sl=None,
            tp=float("nan"),
            tag=self.tag,
            meta={"atr": float(c["atr"][i]), "disp": float(c["disp"][i])},
        )

    # ---- exit ------------------------------------------------------------

    def custom_exit(self, c: Ctx, i: int, p: EmaVwapAtrDisplacementParams, pos: Position) -> str | None:
        cur = float(c["disp"][i])
        if np.isnan(cur):
            return None
        z = p.exit_threshold
        if pos.direction == 1 and cur >= z:
            return "ZERO_CROSS"
        if pos.direction == -1 and cur <= z:
            return "ZERO_CROSS"
        return None
