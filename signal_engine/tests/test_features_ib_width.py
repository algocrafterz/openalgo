"""IB width as a percent of the average daily range (features/ib_width.py).

The definition reproduces the BreakingTrade website's IB% column: the 09:15-10:15 range divided
by the mean high-low range of the previous six completed sessions, times 100. It is fitted, not
documented by the site; see the 2026-10-ib-pct-library experiment README.
"""

import numpy as np
import pandas as pd

from signal_engine.features.ib_width import ib_pct_at, ib_width

IST = "Asia/Kolkata"


def day_bars(day, high, low, n=75, freq="5min"):
    """A flat session: every bar spans [low, high]."""
    idx = pd.date_range(f"{day} 09:15", periods=n, freq=freq, tz=IST)
    mid = (high + low) / 2
    return pd.DataFrame(
        {"Open": mid, "High": high, "Low": low, "Close": mid, "Volume": 1.0}, index=idx
    )


def history(ranges, start="2026-09-01"):
    """One session per business day, each with the given high-low range around 100."""
    days = pd.bdate_range(start, periods=len(ranges))
    return pd.concat(
        [day_bars(d.date(), 100 + r / 2, 100 - r / 2) for d, r in zip(days, ranges, strict=True)]
    )


def ib_day(day, ib_rows, after=(101.0, 99.0), freq="5min"):
    """IB bars from (high, low) rows starting 09:15, then flat bars to the close."""
    idx = pd.date_range(f"{day} 09:15", periods=75, freq=freq, tz=IST)
    rows = list(ib_rows) + [after] * (75 - len(ib_rows))
    h, lo = (list(x) for x in zip(*rows, strict=True))
    c = [(a + b) / 2 for a, b in rows]
    return pd.DataFrame({"Open": c, "High": h, "Low": lo, "Close": c, "Volume": 1.0}, index=idx)


def six_prior_then(today):
    # prior ranges 10..60, mean 35
    return pd.concat([history([10, 20, 30, 40, 50, 60], start="2026-09-21"), today])


class TestIbWidth:
    def test_final_ib_pct_is_ib_range_over_mean_of_six_prior_ranges(self):
        today = ib_day("2026-09-29", [(110, 100)] * 12)  # IB range 10
        out = ib_width(six_prior_then(today))
        last = out.iloc[-1]
        assert last["ib_range"] == 10
        assert last["adr"] == 35
        assert np.isclose(last["ib_pct"], 100 * 10 / 35)

    def test_partial_ib_grows_while_the_window_is_open_then_freezes(self):
        rows = [(101, 100)] * 6 + [(107, 100)] * 6  # range 1 until 09:45, 7 from 09:45
        out = ib_width(six_prior_then(ib_day("2026-09-29", rows, after=(140, 60))))
        today = out[out.index.date == pd.Timestamp("2026-09-29").date()]
        assert np.isclose(today["ib_pct"].iloc[0], 100 / 35)
        assert np.isclose(today["ib_pct"].iloc[11], 700 / 35)
        # bars after 10:15 span 60..140 but the IB is frozen
        assert np.isclose(today["ib_pct"].iloc[-1], 700 / 35)
        assert not today["ib_done"].iloc[11] and today["ib_done"].iloc[12]

    def test_missing_ib_bars_use_the_bars_present(self):
        today = ib_day("2026-09-29", [(105, 100)] * 12).drop(
            pd.Timestamp("2026-09-29 09:30", tz=IST)
        )
        out = ib_width(six_prior_then(today))
        assert out.iloc[-1]["ib_range"] == 5
        assert out.iloc[-1]["ib_bars"] == 11

    def test_min_ib_bars_blanks_a_thin_ib(self):
        today = ib_day("2026-09-29", [(105, 100)] * 12).iloc[[0, 1, 20, 30]]
        out = ib_width(six_prior_then(today), min_ib_bars=6)
        assert out["ib_pct"].iloc[-4:].isna().all()

    def test_holidays_and_gap_days_count_sessions_not_calendar_days(self):
        # a week-long gap between sessions: the six prior SESSIONS still define the average
        prior = history([10, 20, 30, 40, 50, 60], start="2026-09-01")
        today = ib_day("2026-09-21", [(170, 100)] * 12)  # gaps up far from the prior closes
        out = ib_width(pd.concat([prior, today]))
        assert out.iloc[-1]["adr"] == 35
        assert np.isclose(out.iloc[-1]["ib_pct"], 200.0)

    def test_insufficient_history_is_nan(self):
        prior = history([10, 20, 30, 40, 50], start="2026-09-21")  # five sessions only
        out = ib_width(pd.concat([prior, ib_day("2026-09-28", [(110, 100)] * 12)]))
        assert out["ib_pct"].isna().all()

    def test_n_is_configurable(self):
        today = ib_day("2026-09-29", [(110, 100)] * 12)
        out = ib_width(six_prior_then(today), n=2)  # last two prior ranges 50, 60
        assert out.iloc[-1]["adr"] == 55

    def test_a_bar_never_sees_its_own_future(self):
        df = six_prior_then(ib_day("2026-09-29", [(101, 100)] * 6 + [(109, 100)] * 6))
        full = ib_width(df)
        for cut in range(len(df) - 75, len(df)):
            part = ib_width(df.iloc[: cut + 1])
            pd.testing.assert_series_equal(
                part["ib_pct"], full["ib_pct"].iloc[: cut + 1], check_names=False
            )


class TestIbPctAt:
    def test_reads_only_bars_closed_by_the_given_time(self):
        rows = [(101, 100)] * 6 + [(107, 100)] * 6
        df = six_prior_then(ib_day("2026-09-29", rows))
        at = pd.Timestamp("2026-09-29 09:45", tz=IST)  # the 09:45 bar has not closed yet
        assert np.isclose(ib_pct_at(df, at, bar_minutes=5), 100 / 35)
        at = pd.Timestamp("2026-09-29 09:50", tz=IST)
        assert np.isclose(ib_pct_at(df, at, bar_minutes=5), 700 / 35)

    def test_none_before_the_session_or_without_history(self):
        df = six_prior_then(ib_day("2026-09-29", [(110, 100)] * 12))
        assert ib_pct_at(df, pd.Timestamp("2026-09-29 09:15", tz=IST), bar_minutes=5) is None
        short = history([10, 20], start="2026-09-21")
        assert ib_pct_at(short, pd.Timestamp("2026-09-22 12:00", tz=IST), bar_minutes=5) is None
