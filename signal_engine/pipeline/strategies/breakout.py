"""BREAKOUT as a pipeline Strategy: strategy_core wrapped in the protocol, nothing more.

The entry rules live in `strategy_core.breakout`; this file only builds the input frame
from broker bars, calls the core, and reshapes each row into a Fire. The backtest adapter
calls the same object, so a rule change is made once.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace

import pandas as pd

from signal_engine.features.instruments import tick_size
from signal_engine.pipeline.strategy import Fire
from signal_engine.strategy_core.breakout import BreakoutParams, compute_signals
from signal_engine.strategy_core.inputs import build_breakout_inputs

#: ADR needs 14 sessions and the volume factor 13 prior sessions; the margin covers holidays
#: and lets EMA/ATR seeds settle. Verified against a full-history run by the P2 parity test.
DEFAULT_WARMUP_SESSIONS = 20

#: input-frame column -> context key read downstream (main.structural_runner_sl)
_LEVEL_KEYS = {
    "pdh": "pdh",
    "pdl": "pdl",
    "ib_high": "ibh",
    "ib_low": "ibl",
    "orb_high": "orh",
    "orb_low": "orl",
    "pvah": "pvah",
    "ppoc": "ppoc",
    "pval": "pval",
}
_ROW_KEYS = ("score", "conf", "chase", "headroom", "atr")


def _clean(value) -> float | int | None:
    v = float(value)
    return None if math.isnan(v) else v


class BreakoutStrategy:
    interval_min = 5
    needs_1m = True

    def __init__(
        self,
        params: BreakoutParams | None = None,
        tick_source: Callable[[str], float] = tick_size,
        warmup_sessions: int = DEFAULT_WARMUP_SESSIONS,
        inputs_builder: Callable[..., pd.DataFrame] | None = None,
        name: str = "BREAKOUT",
        spec_stamp: dict[str, str] | None = None,
    ) -> None:
        #: The strategy tag the Fires are sent under. The rules are identical for every tag;
        #: a second tag exists only to keep a second book (capital, slots, P&L) apart.
        self.name = name
        #: strategy_id, strategy_version and strategy_fingerprint of the frozen specification, carried
        #: in every signal's context and so into the ledger entry row (spec item 0.1 rule 4).
        self.spec_stamp = dict(spec_stamp or {})
        self.params = params or BreakoutParams()
        self._tick = tick_source
        self._build_inputs = inputs_builder or build_breakout_inputs
        self.warmup_sessions = warmup_sessions
        self.entry_window_min = (self.params.min_entry_min, self.params.entry_cutoff_min)

    def evaluate(self, symbol: str, df5m: pd.DataFrame, df1m: pd.DataFrame) -> list[Fire]:
        tick = self._tick(symbol)
        inputs = self._build_inputs(df5m.astype("float64"), df1m.astype("float64"), tick)
        rows = compute_signals(inputs, replace(self.params, tick=tick))
        levels = inputs.loc[rows.index, list(_LEVEL_KEYS)]
        return [
            self._fire(symbol, bar_time, row, levels.iloc[i], self.spec_stamp)
            for i, (bar_time, row) in enumerate(rows.iterrows())
        ]

    @staticmethod
    def _fire(
        symbol: str, bar_time: pd.Timestamp, row: pd.Series, bar: pd.Series, stamp: dict | None = None
    ) -> Fire:
        context: dict = dict(stamp or {})
        for key in _ROW_KEYS:
            v = _clean(row[key])
            if v is not None:
                context[key] = v
        for key in ("tp2", "tp3"):
            context[key] = float(row[key])
        # day type and the initial-balance extension that drove the counter-bias veto (spec item 6)
        context["day_type"] = str(row["day_type"])
        for key in ("ext_up", "ext_dn"):
            context[key] = float(row[key])
        for col, key in _LEVEL_KEYS.items():
            v = _clean(bar[col])
            if v is not None:
                context[key] = v
        return Fire(
            symbol=symbol,
            bar_time=bar_time,
            direction=int(row["direction"]),
            entry_ref=float(row["entry_ref"]),
            sl=float(row["sl"]),
            targets=(float(row["tp1"]), float(row["tp2"]), float(row["tp3"])),
            tag=str(row["code"]),
            context=context,
        )
