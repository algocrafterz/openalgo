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


def sig_id(symbol: str, bar_time: pd.Timestamp) -> str:
    """Stable per-trade key, same shape the Pine alerts used: TCS-20260930-0955."""
    t = _ist(bar_time)
    return f"{symbol}-{t:%Y%m%d}-{t:%H%M}"


def fire_to_signal(
    fire: Fire, product: str, exchange: str = "NSE", strategy: str = "BREAKOUT"
) -> Signal:
    if not fire.targets:
        raise ValueError(f"{fire.symbol}: fire has no targets")
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
        tp=fire.targets[0],
        exchange=exchange,
        product=product,
        time=f"{t:%H:%M}",
        sig_id=sig_id(fire.symbol, fire.bar_time),
        context=context,
    )
