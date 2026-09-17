"""Unit tests for the IBS mean-reversion swing adapter.

Same approach as the existing intraday strategy tests: build small daily-bar frames
by hand, drive `prepare()`/`entry_at_open()`/`custom_exit()` directly, assert on the
arithmetic - not a full `simulate_swing()` run. All data below is synthetic.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from signal_engine.backtest.strategies.ibs_meanrev import IbsMeanRev, IbsMeanRevParams
from signal_engine.backtest.types import Ctx, Position


def _daily(rows: list[tuple]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="1D")
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=idx)


def _prepared_ctx(rows: list[tuple], p: IbsMeanRevParams):
    strat = IbsMeanRev()
    prepared = strat.prepare(_daily(rows), p)
    return strat, Ctx(prepared, "TEST")


def test_low_prior_ibs_triggers_buy_signal_next_open():
    rows = [
        (100.0, 105.0, 95.0, 100.0, 1000),   # context
        (98.0, 104.0, 96.0, 97.0, 500),      # weak close: IBS = (97-96)/(104-96) = 0.125
        (97.5, 98.0, 97.0, 97.8, 500),       # entry day
    ]
    p = IbsMeanRevParams()
    strat, c = _prepared_ctx(rows, p)
    i = len(c.index) - 1
    sig = strat.entry_at_open(c, i, p, 1)
    assert sig is not None
    assert sig.direction == 1
    assert sig.sl < c["Open"][i]
    assert sig.tp == float(np.inf)


def test_high_prior_ibs_blocks_entry():
    rows = [
        (100.0, 105.0, 95.0, 100.0, 1000),
        (98.0, 104.0, 96.0, 103.0, 500),     # strong close: IBS = (103-96)/8 = 0.875
        (103.5, 104.0, 103.0, 103.8, 500),
    ]
    p = IbsMeanRevParams()
    strat, c = _prepared_ctx(rows, p)
    i = len(c.index) - 1
    assert strat.entry_at_open(c, i, p, 1) is None


def test_short_direction_never_fires_long_only():
    rows = [
        (100.0, 105.0, 95.0, 100.0, 1000),
        (98.0, 104.0, 96.0, 97.0, 500),      # would qualify long
        (97.5, 98.0, 97.0, 97.8, 500),
    ]
    p = IbsMeanRevParams()
    strat, c = _prepared_ctx(rows, p)
    i = len(c.index) - 1
    assert strat.entry_at_open(c, i, p, -1) is None


def test_trend_filter_blocks_weak_close_below_its_own_sma():
    rows = [
        (110.0, 110.0, 110.0, 110.0, 100),
        (104.0, 104.0, 104.0, 104.0, 100),   # SMA(2)=107, Close 104 < 107 -> below SMA
        (99.0, 106.0, 98.0, 99.0, 100),      # weak close, IBS=(99-98)/8=0.125, still below its SMA
        (99.5, 100.0, 99.0, 99.8, 100),      # entry day
    ]
    p = IbsMeanRevParams(use_trend_filter=True, trend_sma_len=2, require_above_sma=True)
    strat, c = _prepared_ctx(rows, p)
    i = len(c.index) - 1
    assert strat.entry_at_open(c, i, p, 1) is None


def test_trend_filter_allows_weak_close_that_stays_above_its_own_sma():
    rows = [
        (100.0, 101.0, 99.0, 100.0, 100),
        (100.0, 101.0, 99.0, 100.0, 100),
        (100.0, 101.0, 99.0, 100.0, 100),
        (100.0, 101.0, 99.0, 100.0, 100),
        # rejection bar: wide upper wick, closes barely UP -> IBS low, still above the 5-day SMA
        (100.5, 110.0, 99.0, 100.3, 100),
        (100.5, 101.0, 100.0, 100.8, 100),   # entry day
    ]
    p = IbsMeanRevParams(use_trend_filter=True, trend_sma_len=5, require_above_sma=True)
    strat, c = _prepared_ctx(rows, p)
    i = len(c.index) - 1
    ibs_signal_day = (100.3 - 99.0) / (110.0 - 99.0)
    assert ibs_signal_day < p.ibs_buy_max
    sig = strat.entry_at_open(c, i, p, 1)
    assert sig is not None
    assert sig.direction == 1


def test_custom_exit_fires_on_high_ibs_close():
    rows = [
        (100.0, 105.0, 95.0, 100.0, 1000),
        (98.0, 104.0, 96.0, 97.0, 500),
        (97.5, 106.0, 97.0, 105.5, 500),     # strong close: IBS = (105.5-97)/9 ~= 0.944
    ]
    p = IbsMeanRevParams()
    strat, c = _prepared_ctx(rows, p)
    i = len(c.index) - 1
    pos = Position(direction=1, entry=97.5, signal_price=97.0, sl=90.0, sl_eff=90.0,
                   tp=float(np.inf), risk=7.5, tag="IBS_LOW", entry_bar=i - 1)
    assert strat.custom_exit(c, i, p, pos) == "IBS_HIGH"


def test_custom_exit_time_stops_after_max_hold_days():
    rows = [
        (100.0, 105.0, 95.0, 100.0, 1000),
        (98.0, 104.0, 96.0, 97.0, 500),
        (97.5, 99.0, 97.0, 98.0, 500),       # mid-range close: neither exit condition
    ]
    p = IbsMeanRevParams(max_hold_days=1)
    strat, c = _prepared_ctx(rows, p)
    i = len(c.index) - 1
    pos = Position(direction=1, entry=97.5, signal_price=97.0, sl=90.0, sl_eff=90.0,
                   tp=float(np.inf), risk=7.5, tag="IBS_LOW", entry_bar=i - 2)
    assert strat.custom_exit(c, i, p, pos) == "TIME_EXIT"


def test_custom_exit_holds_when_neither_condition_is_met():
    rows = [
        (100.0, 105.0, 95.0, 100.0, 1000),
        (98.0, 104.0, 96.0, 97.0, 500),
        (97.5, 99.0, 97.0, 98.0, 500),       # mid-range close
    ]
    p = IbsMeanRevParams(max_hold_days=5)
    strat, c = _prepared_ctx(rows, p)
    i = len(c.index) - 1
    pos = Position(direction=1, entry=97.5, signal_price=97.0, sl=90.0, sl_eff=90.0,
                   tp=float(np.inf), risk=7.5, tag="IBS_LOW", entry_bar=i - 1)
    assert strat.custom_exit(c, i, p, pos) is None
