"""Higher-timeframe close, as breakout.pine reads it.

Pine (line 3597): `request.security(tickerid, klConfirmTF, close[1], lookahead_off)`.
On a confirmed realtime bar the forming HTF bar returns `close[1]`, the close of the
PREVIOUS completed HTF bucket, for every chart bar inside the current bucket. That is
what live alerts see, so it is what this reproduces. (On replayed history TradingView
delivers an HTF value only once its bucket has closed, which lags one further bucket;
that is not the live behaviour and is deliberately not modelled.)

Buckets are anchored to the session open (09:15 IST) and never span midnight: the first
bucket of a day reads the previous day's last bucket close.
"""

from __future__ import annotations

import pandas as pd

SESSION_OPEN_MINUTE = 9 * 60 + 15


def htf_prev_close(df: pd.DataFrame, minutes: int = 15) -> pd.Series:
    """Close of the previous completed `minutes`-minute bucket, per bar.

    NaN before the first bucket has a predecessor. A bucket's close is its last present
    bar, so a missing bar does not break the series.
    """
    minute_of_day = df.index.hour * 60 + df.index.minute
    bucket = (minute_of_day - SESSION_OPEN_MINUTE) // minutes
    keys = pd.MultiIndex.from_arrays([df.index.date, bucket], names=["day", "bucket"])

    bucket_close = df["Close"].groupby(keys, sort=False).last()
    prev = bucket_close.shift(1)
    return pd.Series(prev.reindex(keys).values, index=df.index, name="htf_prev_close")
