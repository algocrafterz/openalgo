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

    # Round-trip cost as basis points of the ENTRY notional (statutory charges plus
    # slippage), from `india_intraday_bps()` below at a ~Rs 1 lakh position. Both legs
    # are market orders, so slippage is charged twice and is the larger half.
    #
    # This was 10.0, which was too kind: the statutory charges alone are 8.2 bps at
    # that size, leaving under 1 bp per side for slippage on a momentum breakout. The
    # live cost study in the Q1 signal-performance note independently landed on ~19 bps.
    # Raising it makes every historical result worse, which is the honest direction.
    cost_bps: float = 16.0

    # signal_engine's validator rejects stops tighter than this fraction of price.
    # Modelling it here keeps the backtest honest about what would actually trade.
    min_sl_pct: float = 0.002

    def with_(self, **kw) -> RunConfig:
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
        # float64 -> float32: every strategy's prepare() adds its own indicator
        # columns (EMAs, ATR, RSI, ADX, VWAP, pivots, ...) on top of the 5 raw OHLCV
        # ones, computed via pandas rolling/ewm which default to float64. A
        # column-heavy strategy (e.g. ema9's ~26 added columns) can dwarf the raw
        # price data several times over across a full-universe, multi-year panel -
        # this is where that memory actually goes, not the original OHLCV. float32
        # still carries far more precision than a NSE tick (paise), so it does not
        # change which side of a comparison a signal lands on.
        self.a = {}
        for c in df.columns:
            if c == "day":
                continue
            arr = df[c].to_numpy()
            if arr.dtype == np.float64:
                arr = arr.astype(np.float32)
            self.a[c] = arr
        self.a["day"] = df["day"].to_numpy() if "day" in df.columns else None
        self.index = df.index
        self.n = len(df)
        self.symbol = symbol

    def __getitem__(self, k: str) -> np.ndarray:
        return self.a[k]

    def has(self, k: str) -> bool:
        return k in self.a


def india_intraday_bps(notional: float = 1_00_000.0, slippage_bps_per_side: float = 4.0,
                       exchange: str = "NSE") -> float:
    """Round-trip intraday cost in basis points of the ENTRY notional.

    Computed from `portfolio.costs.india_intraday` rather than restated here, so the
    backtest and the Portfolio Backtester cannot drift to different views of what a
    trade costs. The statutory part is exact; the slippage part is an assumption and
    is the larger of the two, which is why it is a named argument rather than a
    constant buried in the sum.

    4 bps per side is the working figure for a MARKET order on an F&O-liquid NSE name
    at 5-minute resolution. Both legs of these strategies are market orders - the
    entry on the alert and the exit on the stop or the timed close - so it is charged
    twice. `cost_sensitivity()` exists because this number, not the statutory one, is
    what decides a marginal strategy.
    """
    from portfolio.costs import india_intraday

    schedule = india_intraday(exchange)
    charges = schedule.charge(buy_value=notional, sell_value=notional, orders=2)
    statutory_bps = float(charges) / notional * 10_000.0
    return round(statutory_bps + 2.0 * slippage_bps_per_side, 2)
