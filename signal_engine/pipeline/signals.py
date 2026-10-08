"""Pure mapping from a strategy Fire to the pipeline's Signal. No I/O."""

from __future__ import annotations

import math

import pandas as pd

from signal_engine.models import Direction, Signal
from signal_engine.pipeline.strategy import Fire
from signal_engine.timeutils import IST


def _ist(ts: pd.Timestamp) -> pd.Timestamp:
    if ts.tzinfo is None:
        raise ValueError(f"bar time must be tz-aware, got naive {ts}")
    return ts.tz_convert(IST)


#: The tag whose sig_id keeps the exact shape the Pine alerts used.
PINE_TAG = "BREAKOUT"


def sig_id(symbol: str, bar_time: pd.Timestamp, strategy: str = PINE_TAG) -> str:
    """Stable per-trade key. Pine shape for the default tag: TCS-20260930-0955.

    Any other tag is prefixed (BREAKOUT-PY-TCS-20260930-0955). Two strategies can fire the same
    symbol on the same bar, and an unprefixed key would make the later one look already seen
    (runner dedupe reads trades.db) and would pair the wrong rows in the P&L ledger.
    """
    t = _ist(bar_time)
    base = f"{symbol}-{t:%Y%m%d}-{t:%H%M}"
    return base if strategy == PINE_TAG else f"{strategy}-{base}"


def fire_to_signal(
    fire: Fire, product: str, exchange: str = "NSE", strategy: str = "BREAKOUT"
) -> Signal:
    no_target = fire.context.get("take_profit") == "none"
    if not fire.targets and not no_target:
        raise ValueError(f"{fire.symbol}: fire has no targets")
    if fire.targets and no_target:
        raise ValueError(f"{fire.symbol}: fire says take_profit none but carries targets")
    t = _ist(fire.bar_time)
    context = {
        k: v for k, v in fire.context.items() if not (isinstance(v, float) and math.isnan(v))
    }
    context["trigger"] = fire.tag
    return Signal(
        strategy=strategy,
        direction=Direction.LONG if fire.direction == 1 else Direction.SHORT,
        symbol=fire.symbol,
        entry=fire.entry_ref,
        sl=fire.sl,
        tp=0.0 if no_target else fire.targets[0],  # 0 = no take-profit
        exchange=exchange,
        product=product,
        time=f"{t:%H:%M}",
        sig_id=sig_id(fire.symbol, fire.bar_time, strategy),
        context=context,
    )
