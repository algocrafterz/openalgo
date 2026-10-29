"""Build 5-minute candles from recorded WebSocket quote ticks. Pure: no I/O, no clock.

Flattrade's feed is a conflated quote stream (one update per change, not every trade), so a
bar built from last-traded prices can miss an extreme that printed between two updates. The
quote also carries the running day high and day low. When the day high rises during a bar,
the bar's high is exactly that new day high (the day high is the maximum over all trades so
far), and likewise for the low. `high_aug` and `low_aug` apply that, `high` and `low` do not,
so the comparison can say how much the day extremes recover.

A bar's volume is the rise in the cumulative day volume between the last quote before the bar
and the last quote inside it. If the feed went quiet for a whole bar, the volume lands on the
next bar that has a quote, and `ticks` is 0 for the silent one.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas as pd

from signal_engine.timeutils import IST

SESSION_OPEN = dt.time(9, 15)
SESSION_CLOSE = dt.time(15, 30)

TIME_SOURCES = ("arrival", "exchange")

BAR_COLUMNS = [
    "open",
    "high",
    "low",
    "close",
    "volume",
    "high_aug",
    "low_aug",
    "ticks",
    "first_tick",
    "last_tick",
]


@dataclass(frozen=True)
class Tick:
    symbol: str
    arrival: pd.Timestamp  # IST, when this machine received the quote
    exchange_time: pd.Timestamp | None  # IST, the feed's last-trade time when it gave one
    ltp: float
    volume: int  # cumulative day volume
    day_high: float | None
    day_low: float | None


def parse_exchange_time(raw, arrival: pd.Timestamp) -> pd.Timestamp | None:
    """The feed's last-trade time as an IST timestamp, or None when it is absent or unreadable.

    Accepts "HH:MM:SS" (dated by the arrival day), "DD-MM-YYYY HH:MM:SS" and epoch seconds.
    """
    if raw in (None, "", 0):
        return None
    try:
        if isinstance(raw, (int, float)):
            return pd.Timestamp(raw, unit="s", tz="UTC").tz_convert(IST)
        text = str(raw).strip()
        for fmt in ("%H:%M:%S", "%d-%m-%Y %H:%M:%S"):
            try:
                parsed = dt.datetime.strptime(text, fmt)
            except ValueError:
                continue
            if fmt == "%H:%M:%S":
                parsed = dt.datetime.combine(arrival.date(), parsed.time())
            return pd.Timestamp(parsed).tz_localize(IST)
    except (ValueError, OverflowError):
        return None
    return None


def _in_session(ts: pd.Timestamp) -> bool:
    return SESSION_OPEN <= ts.time() < SESSION_CLOSE


def _stamp(tick: Tick, time_source: str) -> pd.Timestamp:
    if time_source == "exchange" and tick.exchange_time is not None:
        return tick.exchange_time
    return tick.arrival


def build_bars(
    ticks: list[Tick], time_source: str = "arrival", interval_min: int = 5
) -> pd.DataFrame:
    """Candles for one symbol, indexed by bar open time (IST). Empty frame when no tick counts."""
    if time_source not in TIME_SOURCES:
        raise ValueError(f"time_source must be one of {TIME_SOURCES}, got {time_source!r}")
    stamped = [
        (_stamp(t, time_source), t)
        for t in sorted(ticks, key=lambda t: t.arrival)
        if t.ltp and t.ltp > 0
    ]
    stamped = [(ts, t) for ts, t in stamped if _in_session(ts)]
    if not stamped:
        empty = pd.DataFrame(columns=BAR_COLUMNS, index=pd.DatetimeIndex([], tz=IST))
        numeric = [c for c in BAR_COLUMNS if c not in ("first_tick", "last_tick")]
        return empty.astype(dict.fromkeys(numeric, "float64"))

    freq = f"{interval_min}min"
    rows: dict[pd.Timestamp, list[tuple[pd.Timestamp, Tick]]] = {}
    for ts, t in stamped:
        rows.setdefault(ts.floor(freq), []).append((ts, t))

    out = {}
    prev_volume = 0
    prev_day_high = prev_day_low = None
    for start in sorted(rows):
        group = [t for _, t in rows[start]]
        prices = [t.ltp for t in group]
        last = group[-1]
        high, low = max(prices), min(prices)
        high_aug, low_aug = high, low
        if (
            last.day_high is not None
            and prev_day_high is not None
            and last.day_high > prev_day_high
        ):
            high_aug = max(high, last.day_high)
        if last.day_low is not None and prev_day_low is not None and last.day_low < prev_day_low:
            low_aug = min(low, last.day_low)
        out[start] = {
            "open": group[0].ltp,
            "high": high,
            "low": low,
            "close": last.ltp,
            "volume": max(last.volume - prev_volume, 0),
            "high_aug": high_aug,
            "low_aug": low_aug,
            "ticks": len(group),
            "first_tick": rows[start][0][0],
            "last_tick": rows[start][-1][0],
        }
        prev_volume = max(prev_volume, last.volume)
        if last.day_high is not None:
            prev_day_high = last.day_high
        if last.day_low is not None:
            prev_day_low = last.day_low

    frame = pd.DataFrame.from_dict(out, orient="index", columns=BAR_COLUMNS)
    frame.index = pd.DatetimeIndex(frame.index).tz_convert(IST)
    full = pd.date_range(frame.index[0], frame.index[-1], freq=freq, tz=IST)
    frame = frame.reindex(full)
    frame["ticks"] = frame["ticks"].fillna(0).astype(int)
    return frame
