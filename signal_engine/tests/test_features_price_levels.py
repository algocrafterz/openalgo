"""Reusable price indicators and session levels (features/price.py, features/levels.py).

Semantics follow the Pine builtins the strategies use: Wilder RMA for ATR, EMA seeded on the
first value, VWAP anchored per session on the chosen source, previous-day levels and ADR
read from PRIOR sessions only.
"""

import numpy as np
import pandas as pd
import pytest

from signal_engine.features.levels import (
    adr_prev,
    day_extremes,
    initial_balance,
    opening_range,
    prev_day_hl,
)
from signal_engine.features.price import atr, clv, ema, ema_slope, rma, session_vwap, true_range

IST = "Asia/Kolkata"


def bars(day, rows, start="09:15", freq="5min"):
    """rows are (high, low, close, volume); open = close."""
    idx = pd.date_range(f"{day} {start}", periods=len(rows), freq=freq, tz=IST)
    h, lo, c, v = (list(x) for x in zip(*rows, strict=True))
    return pd.DataFrame(
        {"Open": c, "High": h, "Low": lo, "Close": c, "Volume": [float(x) for x in v]}, index=idx
    )


class TestPrice:
    def test_rma_is_wilder_smoothing(self):
        s = pd.Series([2.0, 4.0, 6.0])
        expected = s.ewm(alpha=1 / 2, adjust=False).mean()
        pd.testing.assert_series_equal(rma(s, 2), expected)

    def test_true_range_uses_gap_from_previous_close(self):
        df = bars("2026-09-29", [(11, 9, 10, 1), (15, 13, 14, 1)])
        tr = true_range(df)
        assert tr.iloc[0] == 2  # first bar: high - low
        assert tr.iloc[1] == 5  # |15 - 10| beats high - low = 2

    def test_atr_is_rma_of_true_range(self):
        df = bars("2026-09-29", [(11, 9, 10, 1), (15, 13, 14, 1), (16, 12, 15, 1)])
        pd.testing.assert_series_equal(atr(df, 2), rma(true_range(df), 2))

    def test_ema_is_seeded_on_first_value(self):
        s = pd.Series([10.0, 20.0])
        e = ema(s, 3)  # alpha = 0.5
        assert e.iloc[0] == 10.0
        assert e.iloc[1] == pytest.approx(15.0)

    def test_ema_slope_is_first_difference(self):
        s = pd.Series([10.0, 20.0, 20.0])
        sl = ema_slope(s, 3)
        assert sl.iloc[0] == 0  # Pine: nz(ema[1], ema) on the first bar
        assert sl.iloc[1] == pytest.approx(5.0)

    def test_session_vwap_defaults_to_close_and_resets_each_session(self):
        df = pd.concat(
            [
                bars("2026-09-28", [(11, 9, 10, 1), (25, 15, 20, 3)]),
                bars("2026-09-29", [(31, 29, 30, 2)]),
            ]
        )
        v = session_vwap(df)
        assert list(v) == pytest.approx([10.0, (10 * 1 + 20 * 3) / 4, 30.0])

    def test_session_vwap_can_use_typical_price(self):
        df = bars("2026-09-29", [(12, 6, 9, 1)])
        assert session_vwap(df, source="hlc3").iloc[0] == pytest.approx(9.0)

    def test_session_vwap_zero_volume_is_nan(self):
        df = bars("2026-09-29", [(11, 9, 10, 0)])
        assert np.isnan(session_vwap(df).iloc[0])

    def test_clv_position_in_bar_and_flat_bar(self):
        df = bars("2026-09-29", [(12, 8, 11, 1), (10, 10, 10, 1)])
        assert list(clv(df)) == pytest.approx([0.75, 0.5])

    def test_atr_and_vwap_are_causal(self):
        df = pd.concat(
            [bars("2026-09-28", [(11, 9, 10, 1)] * 6), bars("2026-09-29", [(12, 8, 11, 2)] * 6)]
        )
        cut = df.iloc[:-3]
        pd.testing.assert_series_equal(atr(cut), atr(df).iloc[: len(cut)])
        pd.testing.assert_series_equal(session_vwap(cut), session_vwap(df).iloc[: len(cut)])


def three_days():
    return pd.concat(
        [
            bars("2026-09-28", [(110, 90, 100, 1), (120, 95, 105, 1)]),  # range 30 (90..120)
            bars("2026-09-29", [(130, 100, 110, 1), (140, 120, 135, 1)]),  # range 40 (100..140)
            bars("2026-09-30", [(150, 130, 145, 1), (160, 140, 155, 1)]),  # range 30
        ]
    )


def day_of(df, iso):
    return (pd.Series(df.index.date, index=df.index) == pd.Timestamp(iso).date()).to_numpy()


class TestPrevDayLevels:
    def test_each_day_sees_the_previous_sessions_extremes(self):
        df = three_days()
        pdh, pdl = prev_day_hl(df)
        assert (
            pdh[day_of(df, "2026-09-29")].eq(120).all()
            and pdl[day_of(df, "2026-09-29")].eq(90).all()
        )
        assert (
            pdh[day_of(df, "2026-09-30")].eq(140).all()
            and pdl[day_of(df, "2026-09-30")].eq(100).all()
        )

    def test_first_day_has_no_previous(self):
        df = three_days()
        pdh, pdl = prev_day_hl(df)
        assert pdh[day_of(df, "2026-09-28")].isna().all()
        assert pdl[day_of(df, "2026-09-28")].isna().all()

    def test_causal_truncation(self):
        df = three_days()
        pdh, _ = prev_day_hl(df)
        cut_pdh, _ = prev_day_hl(df.iloc[:-1])
        pd.testing.assert_series_equal(cut_pdh, pdh.iloc[: len(cut_pdh)])


class TestAdr:
    def test_mean_of_prior_n_daily_ranges_excluding_today(self):
        df = three_days()
        out = adr_prev(df, n=2)
        assert out[day_of(df, "2026-09-30")].eq(35.0).all()  # ranges 30 and 40
        assert out[day_of(df, "2026-09-29")].isna().all()  # only one prior day
        assert out[day_of(df, "2026-09-28")].isna().all()


class TestDayExtremes:
    def test_running_session_high_low_include_the_current_bar(self):
        df = three_days()
        hi, lo = day_extremes(df)
        d = df[day_of(df, "2026-09-29")]
        assert list(hi[d.index]) == [130, 140]
        assert list(lo[d.index]) == [100, 100]


def ib_day():
    # 09:15..10:30 in 5m bars = 16 bars; IB window is 09:15-10:15 (12 bars)
    rows = [(100 + i, 90 + i, 95 + i, 1) for i in range(16)]
    return bars("2026-09-29", rows)


class TestInitialBalance:
    def test_running_extremes_freeze_when_the_window_ends(self):
        df = ib_day()
        ib = initial_balance(df, minutes=60)
        assert ib["ib_high"].iloc[0] == 100 and ib["ib_low"].iloc[0] == 90
        assert (
            ib["ib_high"].iloc[11] == 111 and ib["ib_low"].iloc[11] == 90
        )  # 10:10 bar, last in window
        assert ib["ib_high"].iloc[15] == 111  # frozen after 10:15

    def test_done_flag_turns_on_at_the_first_bar_after_the_window(self):
        ib = initial_balance(ib_day(), minutes=60)
        assert not ib["ib_done"].iloc[11]
        assert ib["ib_done"].iloc[12]  # 10:15

    def test_resets_each_session(self):
        df = pd.concat([ib_day(), bars("2026-09-30", [(500, 490, 495, 1)] * 2)])
        ib = initial_balance(df, minutes=60)
        assert ib["ib_high"].iloc[-1] == 500


class TestOpeningRange:
    def test_nan_until_the_range_has_formed_then_frozen(self):
        rows = [
            (100, 90, 95, 1),
            (105, 92, 100, 1),
            (103, 88, 90, 1),
            (120, 80, 100, 1),
            (130, 70, 100, 1),
        ]
        orr = opening_range(bars("2026-09-29", rows), minutes=15)
        assert orr["orb_high"].iloc[:3].isna().all()  # 09:15, 09:20, 09:25 still forming
        assert orr["orb_high"].iloc[3] == 105 and orr["orb_low"].iloc[3] == 88
        assert orr["orb_high"].iloc[4] == 105  # later, wider bars do not move it
