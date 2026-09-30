"""Check B (engine differential) must measure engine arithmetic, not the data's path.

The probe buys the open and exits on a timer, and is compared with the same quantity read
straight off the price panel. Its stop and target exist only because the engine requires
them, and the check is valid only if they can never bind. Two real days broke that premise
on the historify store and made the check fail by 0.2 bps:

  ANGELONE 2025-11-27  one bar printed 10x the price (a decimal-shift bad tick), which hit
                       the probe's 10x target and booked a +900 percent trade.
  YESBANK  2020-03-06  the moratorium crash took price below half its opening level, which
                       hit the probe's 50 percent stop.

The engine was right in both cases; the probe was not neutral. These tests reproduce both
on synthetic bars.
"""

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest.validation import engine_differential

TOLERANCE_BPS = 0.01


def _session(day: str, start_price: float, seed: int) -> pd.DataFrame:
    """One 75-bar 5-minute NSE session, 09:15 to 15:25, gently random."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(f"{day} 09:15", periods=75, freq="5min", tz="Asia/Kolkata")
    close = start_price * np.cumprod(1 + rng.normal(0, 0.0008, len(idx)))
    open_ = np.concatenate([[start_price], close[:-1]])
    high = np.maximum(open_, close) * 1.0004
    low = np.minimum(open_, close) * 0.9996
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": 1000.0}, index=idx
    )


def _frame(days: int = 6, start_price: float = 100.0) -> pd.DataFrame:
    dates = pd.bdate_range("2026-01-05", periods=days)
    return pd.concat([_session(d.strftime("%Y-%m-%d"), start_price, i) for i, d in enumerate(dates)])


def _assert_agrees(result: dict) -> None:
    assert result["count_match"]
    assert result["panel_trades"] == result["engine_trades"] == 6
    assert result["diff_bps"] < TOLERANCE_BPS


def test_clean_data_agrees():
    _assert_agrees(engine_differential({"CLEAN.NS": _frame()}))


def test_single_bar_decimal_shift_spike_does_not_leak_into_the_probe():
    df = _frame()
    row = df.index[3 * 75 + 40]  # mid-session bar of the fourth day
    df.loc[row, "High"] = df.loc[row, "Close"] * 11.0
    _assert_agrees(engine_differential({"SPIKE.NS": df}))


def test_intraday_crash_below_half_does_not_leak_into_the_probe():
    df = _frame()
    row = df.index[2 * 75 + 30]
    df.loc[row, "Low"] = df.loc[row, "Close"] * 0.3
    _assert_agrees(engine_differential({"CRASH.NS": df}))


@pytest.mark.parametrize("factor", [0.0001, 12.0, 500.0])
def test_extreme_prints_in_either_direction_are_neutral(factor):
    df = _frame()
    row = df.index[4 * 75 + 10]
    col = "Low" if factor < 1 else "High"
    df.loc[row, col] = df.loc[row, "Close"] * factor
    _assert_agrees(engine_differential({"EXTREME.NS": df}))
