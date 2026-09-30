"""Session bookkeeping: session date and bar slot.

`slot` counts PRESENT bars since the session open, not clock time, exactly as klVolFactor
does (pine 1098): a missing bar shifts every later slot down by one rather than leaving a
hole. That keeps the Python slot index identical to the one the Pine script accumulates.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

#: End of continuous trading for F&O-eligible stocks. NSE's Closing Auction Session (SEBI
#: circular 16 Jan 2026, NSE/CMTR/73362, live 3 Aug 2026) runs 15:15-15:35 for these stocks as a
#: single-price auction with a reference price from the 15:00-15:15 VWAP. In Historify it shows
#: as no volume 15:16-15:20 from 2026-08-03 and, from about 2026-09-04, one bar stamped 15:28 or
#: 15:29 holding roughly half of the day's volume at the closing price. Before August 2026 the
#: same names traded continuously to 15:30. Intraday level profiles describe CONTINUOUS
#: trading, so they use bars before 15:15 in every era; this keeps old and new sessions
#: comparable and stops one auction print from pinning the POC to the close.
CONTINUOUS_END = dt.time(15, 15)


def continuous_session(df: pd.DataFrame) -> pd.DataFrame:
    """Bars of the continuous market only: stamped before 15:15 (IST index required)."""
    return df[df.index.time < CONTINUOUS_END]


def add_session(df: pd.DataFrame) -> pd.DataFrame:
    """Return `day`, `slot` and `is_first_bar` aligned to df.index.

    The index must be tz-aware IST so `day` is the local exchange date.
    """
    day = pd.Series(df.index.date, index=df.index, name="day")
    slot = day.groupby(day).cumcount().rename("slot")
    return pd.DataFrame({"day": day, "slot": slot, "is_first_bar": slot == 0})
