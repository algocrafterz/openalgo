"""ORB-PY as a pipeline Strategy: strategy_core.orb wrapped in the protocol, nothing more.

The rules live in `strategy_core.orb` and reproduce the Pine ORB build that TradingView ran for
paper alerts. This file calls the core on closed 5-minute bars and reshapes each accepted entry into
a Fire.

The signal bar is the ENTRY bar, as in the Pine: the order goes out after that bar closes, and the
levels are priced from that bar's open, which the Pine printed as "Entry". `entry_ref` is therefore
the entry bar's open, not its close.

The Fire carries four targets (tp1, tp1.5, tp2, tp3). The Signal gets tp1 as `tp`; tp1.5, tp2 and
tp3 travel in the context, where the TargetExitDriver reads tp1.5. The tag has the family "ORB15",
which names no key level, so the runner stop after a partial exit stays on the TP1 buffer rule,
exactly as for a Pine ORB alert.
"""

from __future__ import annotations

import pandas as pd

from signal_engine.pipeline.strategy import Fire
from signal_engine.strategy_core.orb import OrbEntry, OrbParams, evaluate_frame

TAG = "ORB-PY"
#: The daily EMA(20) behind the higher-timeframe vote settles to within about 5% of its seed in 30 sessions.
DEFAULT_WARMUP_SESSIONS = 30


class OrbStrategy:
    interval_min = 5
    needs_1m = False

    def __init__(
        self, params: OrbParams | None = None, warmup_sessions: int = DEFAULT_WARMUP_SESSIONS
    ) -> None:
        self.name = TAG
        self.params = params or OrbParams()
        self.warmup_sessions = warmup_sessions
        self.entry_window_min = (self.params.min_entry_min, self.params.entry_cutoff_min)

    def evaluate(self, symbol: str, df5m: pd.DataFrame, df1m: pd.DataFrame) -> list[Fire]:
        frame = evaluate_frame(df5m.astype("float64"), self.params)
        return [self._fire(symbol, e) for e in frame.entries]

    @staticmethod
    def _fire(symbol: str, e: OrbEntry) -> Fire:
        side = "LONG" if e.direction == 1 else "SHORT"
        return Fire(
            symbol=symbol,
            bar_time=e.bar_time,
            direction=e.direction,
            entry_ref=e.entry,
            sl=e.sl,
            targets=(e.tp1, e.tp1_5, e.tp2, e.tp3),
            tag=f"ORB15-{side}",
            context={
                "tp1_5": e.tp1_5,
                "tp2": e.tp2,
                "tp3": e.tp3,
                "orb_high_level": e.orb_high,
                "orb_low_level": e.orb_low,
                "breakout_time": f"{e.breakout_time:%H:%M}",
                "votes_against": e.votes_against,
                "atr": e.atr,
            },
        )
