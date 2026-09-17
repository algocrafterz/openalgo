"""Unit tests for the Turtle Soup failed-breakout fade intraday adapter.

Same style as `test_open_drive.py`: build 1-minute bars by hand, drive
`prepare()`/`entry()` directly, assert on the arithmetic. All data is synthetic.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from signal_engine.backtest.strategies.turtle_soup import TurtleSoup, TurtleSoupParams
from signal_engine.backtest.types import Ctx

IST = "Asia/Kolkata"


def _bars(day: str, start: str, rows: list[tuple]) -> pd.DataFrame:
    idx = pd.date_range(f"{day} {start}", periods=len(rows), freq="1min", tz=IST)
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=idx)


def _context_day(high: float = 112.0, low: float = 108.0) -> pd.DataFrame:
    """A prior session that sets PDH=112 / PDL=108 for the next day."""
    rows = [(110.0, high, low, 110.0, 100)] + [(110.0, 110.5, 109.5, 110.0, 50)] * 9
    return _bars("2026-06-01", "09:15", rows)


def _prepared_ctx(day2_rows: list[tuple], p: TurtleSoupParams, day2_start: str = "09:35"):
    df = pd.concat([_context_day(), _bars("2026-06-02", day2_start, day2_rows)]).sort_index()
    strat = TurtleSoup()
    strat.reset_symbol(p)
    prepared = strat.prepare(df, p)
    return strat, Ctx(prepared, "TEST")


def test_failed_break_above_pdh_fires_short_fade():
    rows = [(112.5, 113.0, 112.2, 111.0, 50)]   # trades above 112, closes back below it
    p = TurtleSoupParams()
    strat, c = _prepared_ctx(rows, p)
    i = len(c.index) - 1
    sig = strat.entry(c, i, p, -1)
    assert sig is not None
    assert sig.direction == -1
    assert sig.tag == "PDH_FADE"
    assert sig.sl > c["Close"][i]
    assert sig.tp < c["Close"][i]


def test_clean_break_that_holds_does_not_fire():
    rows = [(111.5, 114.0, 111.4, 113.5, 50)]   # trades above 112 AND closes above it - real break
    p = TurtleSoupParams()
    strat, c = _prepared_ctx(rows, p)
    i = len(c.index) - 1
    assert strat.entry(c, i, p, -1) is None


def test_failed_break_below_pdl_fires_long_fade():
    rows = [(108.5, 108.6, 107.5, 109.0, 50)]   # trades below 108, closes back above it
    p = TurtleSoupParams()
    strat, c = _prepared_ctx(rows, p)
    i = len(c.index) - 1
    sig = strat.entry(c, i, p, 1)
    assert sig is not None
    assert sig.direction == 1
    assert sig.tag == "PDL_FADE"
    assert sig.sl < c["Close"][i]
    assert sig.tp > c["Close"][i]


def test_cooldown_blocks_immediate_re_fire():
    rows = [
        (112.5, 113.0, 112.2, 111.0, 50),   # bar0: fires
        (111.5, 113.2, 111.4, 111.2, 50),   # bar1: same pattern, should be blocked by cooldown
    ]
    p = TurtleSoupParams(cooldown_bars=6)
    strat, c = _prepared_ctx(rows, p)
    last = len(c.index) - 1
    first = last - 1
    sig = strat.entry(c, first, p, -1)
    assert sig is not None
    strat.on_entry(c, first, p, sig)
    assert strat.entry(c, last, p, -1) is None


def test_min_entry_min_filter_blocks_early_bars():
    rows = [(112.5, 113.0, 112.2, 111.0, 50)]
    p = TurtleSoupParams()   # default min_entry_min = 09:30
    strat, c = _prepared_ctx(rows, p, day2_start="09:15")   # bar prints at 09:15, before the gate
    i = len(c.index) - 1
    assert c["mins"][i] < p.min_entry_min
    assert strat.entry(c, i, p, -1) is None


def test_adx_filter_blocks_fade_on_strong_trend_day():
    idx = pd.date_range("2026-06-02 09:35", periods=3, freq="1min", tz=IST)
    df = pd.DataFrame({
        "Close": [110.0, 110.0, 111.0],
        "High": [110.0, 110.0, 113.0],
        "Low": [110.0, 110.0, 112.2],
        "mins": [575, 576, 577],
        "pdh": [112.0, 112.0, 112.0],
        "pdl": [108.0, 108.0, 108.0],
        "ib_done": [1.0, 1.0, 1.0],
        "ibh": [np.nan, np.nan, np.nan],
        "ibl": [np.nan, np.nan, np.nan],
        "atr": [1.0, 1.0, 1.0],
        "adx": [40.0, 40.0, 40.0],   # strong trend
        "day": pd.to_datetime(["2026-06-02"] * 3).date,
    }, index=idx)
    c = Ctx(df, "TEST")

    strat_filtered = TurtleSoup()
    strat_filtered.reset_symbol(None)
    p_filtered = TurtleSoupParams(use_adx_filter=True, max_adx=25.0)
    assert strat_filtered.entry(c, 2, p_filtered, -1) is None

    strat_plain = TurtleSoup()
    strat_plain.reset_symbol(None)
    p_plain = TurtleSoupParams(use_adx_filter=False)
    assert strat_plain.entry(c, 2, p_plain, -1) is not None
