"""Session and higher-timeframe inputs, ported from breakout.pine.

HTF close mirrors `request.security(tickerid, "15", close[1], lookahead_off)` (pine 3597) as
it behaves on a confirmed realtime bar: every 5m bar inside 15m bucket B reads the close of
bucket B-1. The 09:15 bucket of a day reads the previous day's last bucket.
"""

import numpy as np
import pandas as pd
import pytest

from signal_engine.features.htf import htf_prev_close
from signal_engine.features.session import add_session, continuous_session

IST = "Asia/Kolkata"


def make_day(day: str, closes, start="09:15", freq="5min", volume=100.0) -> pd.DataFrame:
    idx = pd.date_range(f"{day} {start}", periods=len(closes), freq=freq, tz=IST)
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame(
        {"Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": float(volume)}, index=idx
    )


class TestAddSession:
    def test_slot_counts_bars_from_session_open(self):
        df = pd.concat([make_day("2026-09-28", [1, 2, 3]), make_day("2026-09-29", [4, 5])])
        s = add_session(df)
        assert list(s["slot"]) == [0, 1, 2, 0, 1]
        assert list(s["is_first_bar"]) == [True, False, False, True, False]

    def test_day_column_is_local_session_date(self):
        s = add_session(make_day("2026-09-29", [1, 2]))
        assert set(s["day"]) == {pd.Timestamp("2026-09-29").date()}

    def test_slot_counts_present_bars_not_clock_time(self):
        df = make_day("2026-09-29", [1, 2, 3, 4])
        df = df.drop(df.index[1])  # a missing bar must not leave a hole in slot numbering
        assert list(add_session(df)["slot"]) == [0, 1, 2]


class TestContinuousSession:
    def test_bars_from_the_closing_auction_window_are_removed(self):
        idx = pd.DatetimeIndex(
            [pd.Timestamp(f"2026-09-29 {t}", tz=IST) for t in ("09:15", "15:14", "15:15", "15:29")]
        )
        df = pd.DataFrame({"Close": [1.0, 2.0, 3.0, 4.0]}, index=idx)
        assert list(continuous_session(df)["Close"]) == [1.0, 2.0]  # 15:14 kept, 15:15 dropped


class TestHtfPrevClose:
    def test_bar_reads_close_of_previous_bucket(self):
        # buckets: 09:15-09:25 (closes 1,2,3), 09:30-09:40 (4,5,6), 09:45 (7)
        df = make_day("2026-09-29", [1, 2, 3, 4, 5, 6, 7])
        h = htf_prev_close(df)
        assert h.iloc[:3].isna().all()  # first bucket of the first day has no history
        assert list(h.iloc[3:6]) == [3.0, 3.0, 3.0]
        assert h.iloc[6] == 6.0

    def test_first_bucket_reads_previous_days_last_bucket(self):
        df = pd.concat(
            [make_day("2026-09-28", [1, 2, 3, 4, 5, 6]), make_day("2026-09-29", [7, 8, 9])]
        )
        h = htf_prev_close(df)
        assert list(h.iloc[6:9]) == [6.0, 6.0, 6.0]

    def test_bucket_close_is_last_available_bar(self):
        df = make_day("2026-09-29", [1, 2, 3, 4, 5, 6, 7])
        df = df.drop(df.index[2])  # 09:25 missing -> bucket 0 closes on the 09:20 bar
        h = htf_prev_close(df)
        assert h.iloc[-1] == 6.0
        assert h.loc[df.index[2]] == 2.0  # 09:30 bar, previous bucket last close = 09:20

    def test_output_is_aligned_to_input_index(self):
        df = make_day("2026-09-29", np.arange(20))
        assert htf_prev_close(df).index.equals(df.index)

    @pytest.mark.parametrize("cut", [4, 7, 11, 19])
    def test_causal_truncation_does_not_change_history(self, cut):
        df = pd.concat(
            [make_day("2026-09-28", np.arange(1, 25)), make_day("2026-09-29", np.arange(30, 52))]
        )
        full = htf_prev_close(df)
        part = htf_prev_close(df.iloc[: len(df) - cut])
        pd.testing.assert_series_equal(part, full.iloc[: len(part)], check_names=False)
