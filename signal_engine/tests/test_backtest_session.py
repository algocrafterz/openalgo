"""Derived exchange calendar and session model."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import session
from signal_engine.research.intake.profiles import load_profile

IST = "Asia/Kolkata"
PROFILE = load_profile("flattrade-nse-cash")


def _bdays(start: str, end: str) -> list[date]:
    return [d.date() for d in pd.bdate_range(start, end)]


def _intraday(days, bars=375) -> pd.DataFrame:
    parts = []
    for d in days:
        n = bars
        idx = pd.date_range(f"{d} 09:15", periods=n, freq="1min", tz=IST)
        parts.append(pd.DataFrame({"Close": 100.0, "Volume": 10.0}, index=idx))
    out = pd.concat(parts)
    return out


def _daily(days) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz=IST) for d in days])
    return pd.DataFrame({"Close": 100.0, "Volume": 10.0}, index=idx)


def _month_frames(holiday=date(2026, 3, 10), n=6, bars=375):
    days = [d for d in _bdays("2026-03-02", "2026-03-31") if d != holiday]
    return {f"S{i}": _intraday(days, bars=bars) for i in range(n)}, days


class TestDeriveCalendar:
    def test_holiday_is_not_a_trading_day(self):
        frames, days = _month_frames()
        cal = session.derive_calendar(frames)
        assert date(2026, 3, 10) not in cal.trading_days
        assert list(cal.trading_days) == sorted(days)
        assert cal.source == "derived_from_market_data"

    def test_weekends_and_sorted_dates(self):
        frames, _ = _month_frames()
        cal = session.derive_calendar(frames)
        assert all(d.weekday() < 5 for d in cal.trading_days)
        assert list(cal.trading_days) == sorted(cal.trading_days)
        assert isinstance(cal.trading_days, tuple)

    def test_threshold_80_percent_per_month(self):
        days = _bdays("2026-03-02", "2026-03-06")
        frames = {f"S{i}": _intraday(days, bars=5) for i in range(5)}
        # 4 of 5 symbols traded 03-06 (80%) -> counts; 3 of 5 on 03-05 -> does not
        frames["S4"] = _intraday(days[:3] + days[4:], bars=5)
        frames["S3"] = _intraday(days[:3], bars=5)
        frames["S2"] = _intraday(days[:3], bars=5)
        cal = session.derive_calendar(frames)
        assert date(2026, 3, 5) not in cal.trading_days  # 2 of 5 traded
        assert date(2026, 3, 6) not in cal.trading_days  # 2 of 5 traded
        assert date(2026, 3, 4) in cal.trading_days

    def test_exactly_80_percent_counts(self):
        days = _bdays("2026-03-02", "2026-03-03")
        frames = {f"S{i}": _intraday(days, bars=5) for i in range(4)}
        frames["S4"] = _intraday(days[:1], bars=5)
        cal = session.derive_calendar(frames)
        assert date(2026, 3, 3) in cal.trading_days

    def test_late_listed_symbol_does_not_count_against_earlier_months(self):
        feb = _bdays("2026-02-02", "2026-02-27")
        mar = _bdays("2026-03-02", "2026-03-31")
        frames = {"OLD1": _intraday(feb + mar, bars=375), "OLD2": _intraday(feb + mar, bars=375)}
        frames["NEW"] = _intraday(mar, bars=375)
        cal = session.derive_calendar(frames)
        assert set(feb) <= set(cal.trading_days)
        assert set(mar) <= set(cal.trading_days)

    def test_short_session_flagged_not_removed(self):
        days = _bdays("2026-03-02", "2026-03-06")
        frames = {}
        for i in range(4):
            full = _intraday(days[:2] + days[3:])
            short = _intraday([days[2]], bars=150)
            frames[f"S{i}"] = pd.concat([full, short]).sort_index()
        cal = session.derive_calendar(frames)
        assert days[2] in cal.trading_days
        assert cal.short_sessions == (days[2],)

    def test_early_last_bar_flagged(self):
        days = _bdays("2026-03-02", "2026-03-04")
        frames = {}
        for i in range(3):
            full = _intraday([days[0], days[2]], bars=375)
            idx = pd.date_range(f"{days[1]} 09:15", periods=300, freq="1min", tz=IST)
            early = pd.DataFrame({"Close": 1.0, "Volume": 1.0}, index=idx)
            frames[f"S{i}"] = pd.concat([full, early]).sort_index()
        cal = session.derive_calendar(frames)
        assert cal.short_sessions == (days[1],)

    def test_daily_frames_skip_short_test(self):
        days = _bdays("2026-03-02", "2026-03-31")
        frames = {f"S{i}": _daily(days) for i in range(3)}
        cal = session.derive_calendar(frames)
        assert cal.short_sessions == ()
        assert list(cal.trading_days) == days

    def test_as_of_cut(self):
        frames, days = _month_frames()
        cal = session.derive_calendar(frames, as_of=date(2026, 3, 13))
        assert max(cal.trading_days) == date(2026, 3, 13)
        cal_s = session.derive_calendar(frames, as_of="2026-03-13")
        assert cal_s == cal

    def test_version_format(self):
        frames, _ = _month_frames()
        v = session.derive_calendar(frames).version
        assert v.startswith("historify-derived-") and len(v) == len("historify-derived-") + 8
        int(v.rsplit("-", 1)[1], 16)

    def test_version_stable_when_bars_after_as_of_are_appended(self):
        frames, days = _month_frames()
        cut = date(2026, 3, 13)
        early = {k: v[v.index.date <= cut] for k, v in frames.items()}
        a = session.derive_calendar(early, as_of=cut)
        b = session.derive_calendar(frames, as_of=cut)
        assert a.version == b.version

    def test_version_changes_when_session_added_or_removed(self):
        frames, _ = _month_frames()
        base = session.derive_calendar(frames).version
        dropped = {k: v[v.index.date != date(2026, 3, 11)] for k, v in frames.items()}
        assert session.derive_calendar(dropped).version != base
        extra = {
            k: pd.concat([v, _intraday([date(2026, 3, 10)], bars=5)]).sort_index()
            for k, v in frames.items()
        }
        assert session.derive_calendar(extra).version != base

    def test_deterministic_and_frozen(self):
        frames, _ = _month_frames()
        a = session.derive_calendar(frames)
        assert a == session.derive_calendar(dict(reversed(list(frames.items()))))
        with pytest.raises(AttributeError):
            a.version = "x"

    def test_empty_frames(self):
        cal = session.derive_calendar({})
        assert cal.trading_days == () and cal.short_sessions == ()
        assert cal.version.startswith("historify-derived-")
        assert np.isfinite(len(cal.version))


class TestSessionModel:
    def _cal(self):
        frames, _ = _month_frames()
        return session.derive_calendar(frames)

    def test_mis_uses_profile_cutoffs(self):
        m = session.session_model(
            self._cal(),
            profile_rules=PROFILE.products["MIS"],
            product="MIS",
            strategy_window="09:20-14:45",
        )
        assert set(m) == {"exchange", "strategy_window", "broker_cutoff", "force_flat"}
        assert m["broker_cutoff"] == "14:55" and m["force_flat"] == "15:00"
        assert m["strategy_window"] == "09:20-14:45"
        assert "NSE" in m["exchange"] and "09:15-15:30" in m["exchange"]
        assert "derived" in m["exchange"]

    def test_cnc_has_no_cutoffs(self):
        m = session.session_model(
            self._cal(),
            profile_rules=PROFILE.products["CNC"],
            product="CNC",
            strategy_window=None,
        )
        assert m["broker_cutoff"] is None and m["force_flat"] is None
        assert m["strategy_window"] is None

    def test_mis_without_rules_gives_none(self):
        m = session.session_model(
            self._cal(), profile_rules=None, product="MIS", strategy_window="x"
        )
        assert m["broker_cutoff"] is None and m["force_flat"] is None
