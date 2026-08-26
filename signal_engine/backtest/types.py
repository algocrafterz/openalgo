"""Core value types shared by the backtest engine and every strategy adapter."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np


@dataclass(frozen=True)
class RunConfig:
    """Engine-level settings. Independent of which strategy is being tested.

    These mirror the session/risk inputs every OpenAlgo intraday PineScript carries,
    so a strategy adapter only has to describe its own entry logic.
    """

    skip_open_minutes: int = 15
    entry_cutoff_min: int = 11 * 60        # 11:00 IST, no NEW entries after
    time_exit_min: int = 14 * 60 + 45      # 14:45 IST, must match config.yaml time_exit
    max_trades_per_day: int = 2
    one_trade_per_direction: bool = True
    allow_longs: bool = True
    allow_shorts: bool = True

    # Round-trip cost as basis points of notional (charges + slippage). For NSE
    # intraday equity ~8 bps of charges for a mid-size position, plus slippage.
    cost_bps: float = 10.0

    # signal_engine's validator rejects stops tighter than this fraction of price.
    # Modelling it here keeps the backtest honest about what would actually trade.
    min_sl_pct: float = 0.002

    def with_(self, **kw) -> "RunConfig":
        return replace(self, **kw)


@dataclass(frozen=True)
class EntrySignal:
    """What a strategy returns when it wants a position on the next bar's open."""

    direction: int          # +1 long, -1 short
    sl: float
    tp: float
    tag: str = ""           # setup name, carried into the trade log
    meta: dict = field(default_factory=dict)


@dataclass
class Position:
    direction: int
    entry: float            # actual fill (next bar open)
    signal_price: float     # close of the signal bar - what the alert advertises
    sl: float
    sl_eff: float           # effective stop, which a trail may ratchet
    tp: float
    risk: float
    tag: str
    entry_bar: int


@dataclass
class Trade:
    symbol: str
    day: Any
    direction: int
    tag: str
    entry_time: Any
    entry: float
    signal_price: float
    sl: float
    tp: float
    risk: float
    exit_time: Any = None
    exit: float = 0.0
    reason: str = ""
    r_gross: float = 0.0
    r_net: float = 0.0

    @property
    def risk_pct(self) -> float:
        """Stop distance as a percent of price. The bridge between R and bps."""
        return self.risk / self.signal_price * 100 if self.signal_price else np.nan


class Ctx:
    """Column-major view of one prepared symbol, for fast per-bar access.

    Strategies read `ctx["ema_f"][i]` rather than touching pandas in the hot loop.
    """

    __slots__ = ("a", "index", "n", "symbol")

    def __init__(self, df, symbol: str = ""):
        self.a = {c: df[c].to_numpy() for c in df.columns if c != "day"}
        self.a["day"] = df["day"].to_numpy() if "day" in df.columns else None
        self.index = df.index
        self.n = len(df)
        self.symbol = symbol

    def __getitem__(self, k: str) -> np.ndarray:
        return self.a[k]

    def has(self, k: str) -> bool:
        return k in self.a
