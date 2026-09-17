"""Unit tests for the NR7/NR4 narrow-range breakout intraday adapter.

Same style as `test_turtle_soup.py`: build 1-minute bars by hand, drive
`prepare()`/`entry()` directly, assert on the arithmetic. All data is synthetic.
"""

from __future__ import annotations

import pandas as pd

from signal_engine.backtest.strategies.nr_breakout import NrBreakout, NrBreakoutParams
from signal_engine.backtest.types import Ctx

IST = "Asia/Kolkata"


def _bars(day: str, start: str, rows: list[tuple]) -> pd.DataFrame:
    idx = pd.date_range(f"{day} {start}", periods=len(rows), freq="1min", tz=IST)
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=idx)


def _flat_day(date: str, high: float, low: float) -> pd.DataFrame:
    """One session whose High/Low are set entirely by its first bar."""
    mid = (high + low) / 2
    rows = [(mid, high, low, mid, 100)] + [(mid, mid + 0.05, mid - 0.05, mid, 50)] * 9
    return _bars(date, "09:15", rows)


def _prepared_ctx(context_days: list[pd.DataFrame], test_rows: list[tuple],
                  p: NrBreakoutParams, test_day="2026-06-10", test_start="09:35"):
    df = pd.concat(context_days + [_bars(test_day, test_start, test_rows)]).sort_index()
    strat = NrBreakout()
    strat.reset_symbol(p)
    prepared = strat.prepare(df, p)
    return strat, Ctx(prepared, "TEST")


def _wide_then_narrow_context(narrow_high: float = 112.0, narrow_low: float = 108.0):
    """6 wide-range days (range=20) then a narrow day (range=4) - the narrow day
    qualifies as NR7 (its range is the smallest of the trailing 7, itself included)."""
    days = []
    for k in range(1, 7):
        days.append(_flat_day(f"2026-06-0{k}", 110.0, 90.0))     # range 20, wide
    days.append(_flat_day("2026-06-08", narrow_high, narrow_low))  # range 4, narrow
    return days


def test_narrow_range_breakout_fires_when_require_nr_true():
    context = _wide_then_narrow_context()
    rows = [(112.5, 113.0, 112.2, 112.8, 50)]   # breaks above the narrow day's high (112)
    p = NrBreakoutParams(nr_lookback=7, require_nr=True)
    strat, c = _prepared_ctx(context, rows, p)
    i = len(c.index) - 1
    assert c["is_nr_prev"][i] == 1.0
    sig = strat.entry(c, i, p, 1)
    assert sig is not None
    assert sig.direction == 1
    assert sig.tag == "NR_BRK"
    assert sig.sl < c["Close"][i]
    assert sig.tp > c["Close"][i]


def _widening_context():
    """6 days of increasing range (5..10), then a 7th day that is clearly the
    WIDEST (30) - that 7th day is not the narrowest of its own trailing window."""
    days = [_flat_day(f"2026-06-0{k}", 100.0 + k, 100.0) for k in range(1, 7)]  # ranges 1..6
    days.append(_flat_day("2026-06-07", 130.0, 100.0))                          # range 30
    return days


def test_breakout_after_a_wide_range_day_is_blocked_when_require_nr_true():
    days = _widening_context()
    rows = [(130.5, 131.0, 130.2, 130.8, 50)]   # breaks above yesterday's high (130)
    p = NrBreakoutParams(nr_lookback=7, require_nr=True)
    strat, c = _prepared_ctx(days, rows, p)
    i = len(c.index) - 1
    assert c["is_nr_prev"][i] == 0.0
    assert strat.entry(c, i, p, 1) is None


def test_require_nr_false_trades_the_same_break_regardless_of_regime():
    days = _widening_context()
    rows = [(130.5, 131.0, 130.2, 130.8, 50)]
    p = NrBreakoutParams(nr_lookback=7, require_nr=False)
    strat, c = _prepared_ctx(days, rows, p)
    i = len(c.index) - 1
    sig = strat.entry(c, i, p, 1)
    assert sig is not None


def test_short_side_breaks_below_narrow_days_low():
    context = _wide_then_narrow_context()
    rows = [(107.5, 107.8, 107.0, 107.2, 50)]   # breaks below the narrow day's low (108)
    p = NrBreakoutParams(nr_lookback=7, require_nr=True)
    strat, c = _prepared_ctx(context, rows, p)
    i = len(c.index) - 1
    sig = strat.entry(c, i, p, -1)
    assert sig is not None
    assert sig.direction == -1
    assert sig.sl > c["Close"][i]
    assert sig.tp < c["Close"][i]


def test_no_break_does_not_fire_even_on_a_qualifying_nr_day():
    context = _wide_then_narrow_context()
    rows = [(110.0, 111.5, 109.5, 110.5, 50)]   # stays inside 108-112, no break either way
    p = NrBreakoutParams(nr_lookback=7, require_nr=True)
    strat, c = _prepared_ctx(context, rows, p)
    i = len(c.index) - 1
    assert c["is_nr_prev"][i] == 1.0
    assert strat.entry(c, i, p, 1) is None
    assert strat.entry(c, i, p, -1) is None
