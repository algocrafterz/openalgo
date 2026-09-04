"""Turn a scan selection into a concrete trade plan - entry, stop, targets - in Python.

WHY THIS EXISTS RATHER THAN A PINE ALERT

The obvious route is to let breakout.pine time these entries, since it already computes the
Initial Balance and value area and already speaks signal_engine's alert contract. It cannot
work here, for one structural reason: TradingView has no API for creating alerts. The scanner's
selection is DIFFERENT EVERY DAY - NBCC today, LICHSGFIN tomorrow - so a Pine route would mean
hand-creating alerts each morning for whatever the scanner surfaced, which is exactly the
manual step this whole exercise exists to remove. Everything needed to do it in Python is
already here: OpenAlgo serves the bars, and signal_engine places the orders.

WHAT THIS DELIBERATELY DOES NOT DO

It does not place an order, and it does not decide to trade. It produces a PROPOSED plan for a
human to look at. Nothing here should be wired to execution until validate.py shows the
selection beats its random control - see validate.py's docstring for why that bar exists and
how far the current evidence is from clearing it.

THE RULES, AND WHERE THEY COME FROM

  Entry   The scan is selection, not timing: it says a name is in play, not that this instant
          is the price. So entry waits for the first 5-minute bar to CLOSE beyond the signal
          bar's extreme in the scan's direction. Market order at that close, not a resting
          limit at the pullback the guide suggests - this book measured 30-50% fill rates on
          limit entries into momentum, and an unfilled entry on the trades that ran is the
          most expensive kind of miss.

  Stop    Beyond the level that INVALIDATES the setup (the broken IB extreme), pushed a
          further fraction of ATR past it. The buffer is the whole point: a stop resting
          exactly on the obvious structural level is resting where everyone else's is, which
          is where price gets pushed to go looking for them.

  Targets 1x / 1.5x / 2x the IB range - the guide's own target ladder.

  Split   Set by Day Type, because the guide gives the two day types opposite instructions.
          A Trend day is "hold with a trailing stop - never fade", so most of the position is
          left on for the runner. A Normal Variation is "trade the breakout, take profit
          early", so most comes off at the front. One split for both is what either caps the
          runners or gives back the quick ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time

import pandas as pd

# Initial Balance is the first hour: periods A and B.
IB_START = time(9, 15)
IB_END = time(10, 15)

# Fraction of ATR placed BEYOND the structural level, so the stop does not sit on the same
# obvious level as everyone else's.
STOP_ATR_BUFFER = 0.25

# Target ladder as multiples of the IB range (the guide's own 1x / 1.5x / 2x).
TARGET_IB_MULTIPLES = (1.0, 1.5, 2.0)

# Day Type -> how much of the position leaves at TP1 / TP2 / TP3.
TREND_SPLIT = (0.25, 0.25, 0.50)  # "hold with trailing stop - never fade"
DEFAULT_SPLIT = (0.50, 0.30, 0.20)  # "trade the breakout, take profit early"
RUNNER_DAY_TYPES = {"Trend", "Double Distribution"}


@dataclass
class TradePlan:
    symbol: str
    direction: str  # "up" | "down"
    day_type: str | None
    entry: float
    stop: float
    targets: list
    split: tuple
    ib_high: float
    ib_low: float
    atr: float
    risk_per_share: float
    triggered_at: datetime | None = None
    notes: list = field(default_factory=list)

    @property
    def action(self) -> str:
        return "BUY" if self.direction == "up" else "SELL"

    @property
    def reward_risk(self) -> float | None:
        """R:R to the FINAL target - what the plan is worth if the runner runs."""
        if not self.risk_per_share or not self.targets:
            return None
        return round(abs(self.targets[-1] - self.entry) / self.risk_per_share, 2)


def initial_balance(bars: pd.DataFrame) -> tuple:
    """(high, low) of the 09:15-10:15 Initial Balance. (None, None) if absent."""
    if bars.empty or "timestamp" not in bars.columns:
        return None, None
    stamps = pd.to_datetime(bars["timestamp"])
    window = bars[(stamps.dt.time >= IB_START) & (stamps.dt.time < IB_END)]
    if window.empty:
        return None, None
    return float(window["high"].max()), float(window["low"].min())


def entry_trigger(bars: pd.DataFrame, signal_time: datetime, direction: str) -> tuple:
    """First bar CLOSING beyond the signal bar's extreme. (price, bar_time) or (None, None).

    Waiting for a close rather than a touch is deliberate: an intrabar poke through a level and
    straight back is the most common false break there is, and a touch-triggered entry buys
    every one of them.
    """
    stamps = pd.to_datetime(bars["timestamp"])
    before = bars[stamps <= signal_time]
    after = bars[stamps > signal_time]
    if before.empty or after.empty:
        return None, None

    signal_bar = before.iloc[-1]
    level = float(signal_bar["high"]) if direction == "up" else float(signal_bar["low"])

    for bar in after.itertuples():
        close = float(bar.close)
        if (direction == "up" and close > level) or (direction == "down" and close < level):
            return close, pd.to_datetime(bar.timestamp).to_pydatetime()
    return None, None


def stop_level(
    direction: str, ib_high: float, ib_low: float, atr: float, tail_level: float = None
) -> float:
    """Beyond the level that invalidates the setup, plus an ATR buffer.

    Prefers the TAIL when the scanner reported one. A tail is where price was pushed to and
    firmly rejected, and the guide names it directly as the stop: "Buy/Sell Tail = defined-risk
    stop just beyond the tail". It sits closer than the IB extreme, so the same rupee risk buys
    a larger position - and it is the level the auction itself defended, which the IB edge on a
    wide-range day may not be. Falls back to the broken IB extreme when there is no tail.
    """
    buffer = STOP_ATR_BUFFER * atr
    if tail_level is not None:
        return (tail_level - buffer) if direction == "up" else (tail_level + buffer)
    return (ib_low - buffer) if direction == "up" else (ib_high + buffer)


def target_levels(entry: float, ib_range: float, direction: str) -> list:
    sign = 1 if direction == "up" else -1
    return [round(entry + sign * multiple * ib_range, 2) for multiple in TARGET_IB_MULTIPLES]


def target_split(day_type: str | None) -> tuple:
    return TREND_SPLIT if day_type in RUNNER_DAY_TYPES else DEFAULT_SPLIT


def plan_trade(
    symbol: str,
    direction: str,
    day_type: str | None,
    bars: pd.DataFrame,
    signal_time: datetime,
    atr: float,
) -> TradePlan | None:
    """Build the proposed plan, or None when the trigger has not fired yet."""
    ib_high, ib_low = initial_balance(bars)
    if ib_high is None or not atr:
        return None

    ib_range = ib_high - ib_low
    if ib_range <= 0:
        return None

    entry, triggered_at = entry_trigger(bars, signal_time, direction)
    if entry is None:
        return None

    stop = stop_level(direction, ib_high, ib_low, atr)
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0:
        return None

    notes = []
    if day_type in RUNNER_DAY_TYPES:
        notes.append("trend day: most of the position rides the runner, trail rather than book")
    if risk_per_share > 2 * atr:
        notes.append("stop is wider than 2 ATR - the IB is far away, so size will be small")

    return TradePlan(
        symbol=symbol,
        direction=direction,
        day_type=day_type,
        entry=round(entry, 2),
        stop=round(stop, 2),
        targets=target_levels(entry, ib_range, direction),
        split=target_split(day_type),
        ib_high=round(ib_high, 2),
        ib_low=round(ib_low, 2),
        atr=round(atr, 2),
        risk_per_share=round(risk_per_share, 2),
        triggered_at=triggered_at,
        notes=notes,
    )
