"""Session clocks must follow IST, not the host timezone.

Each test pins one real instant and simulates a UTC host: a bare datetime.now() returns the
UTC wall clock, datetime.now(IST) returns the true IST time. The instants are chosen so the
UTC and IST readings fall on different days, weeks or sides of the market-hours window, which
makes every test fail on the old naive code regardless of the machine's actual timezone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timezone

import pytest

from signal_engine.analysis.breakingtrade import alerts, fetcher, store
from signal_engine.scripts import watchlist_screen as wls


def _utc_host(instant_utc: datetime):
    """A datetime class whose now() behaves like a UTC host clock frozen at `instant_utc`."""

    class UtcHostDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return instant_utc.replace(tzinfo=None)
            return instant_utc.astimezone(tz)

    return UtcHostDatetime


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


# Sunday 2026-10-18 20:00 UTC is Monday 2026-10-19 01:30 IST: a new day AND a new ISO week.
SUNDAY_NIGHT_UTC = _utc(2026, 10, 18, 20, 0)
# 2026-10-14 05:00 UTC is 10:30 IST (inside market hours); a UTC host reads 05:00 (pre-open).
MORNING_UTC = _utc(2026, 10, 14, 5, 0)
# 2026-10-14 11:00 UTC is 16:30 IST (after the close); a UTC host reads 11:00 (inside hours).
EVENING_UTC = _utc(2026, 10, 14, 11, 0)
# 2026-10-14 19:00 UTC is 2026-10-15 00:30 IST: UTC and IST disagree on the calendar date.
MIDNIGHT_SPLIT_UTC = _utc(2026, 10, 14, 19, 0)


class TestWatchlistKeys:
    def test_the_instants_really_split_utc_from_ist(self):
        assert SUNDAY_NIGHT_UTC.isocalendar().week == 42
        assert SUNDAY_NIGHT_UTC.astimezone(wls.IST).isocalendar().week == 43

    def test_trading_day_key_is_the_ist_date(self, monkeypatch):
        monkeypatch.setattr(wls, "datetime", _utc_host(SUNDAY_NIGHT_UTC))
        assert wls._today_key() == "2026-10-19"

    def test_weekly_key_is_the_ist_week(self, monkeypatch):
        monkeypatch.setattr(wls, "datetime", _utc_host(SUNDAY_NIGHT_UTC))
        assert wls._current_week_key() == "2026-W43"

    def test_an_explicit_now_is_used_as_given(self, monkeypatch):
        monkeypatch.setattr(wls, "datetime", _utc_host(SUNDAY_NIGHT_UTC))
        given = datetime(2026, 9, 13, 9, 0)
        assert wls._today_key(given) == "2026-09-13"
        assert wls._current_week_key(given) == "2026-W37"


class TestMarketHoursStamp:
    def test_ist_now_is_naive_ist_wall_clock(self, monkeypatch):
        monkeypatch.setattr(fetcher, "datetime", _utc_host(MORNING_UTC))
        now = fetcher._ist_now()
        assert now == datetime(2026, 10, 14, 10, 30)
        assert now.tzinfo is None

    def test_inside_market_hours_the_snapshot_is_stamped_now(self, monkeypatch):
        monkeypatch.setattr(fetcher, "datetime", _utc_host(MORNING_UTC))
        assert fetcher.session_stamp(fetcher._ist_now()) == datetime(2026, 10, 14, 10, 30)

    def test_after_the_close_the_snapshot_is_stamped_at_the_close(self, monkeypatch):
        monkeypatch.setattr(fetcher, "datetime", _utc_host(EVENING_UTC))
        assert fetcher.session_stamp(fetcher._ist_now()) == datetime(2026, 10, 14, 15, 30)


@pytest.fixture
def alert_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", str(tmp_path / "bt.db"))
    with alerts._connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS alerts (
            created_at TEXT, kind TEXT, symbol TEXT, direction TEXT, scan TEXT,
            message TEXT, delivered INTEGER, message_id INTEGER, strategy TEXT)"""
        )
    return tmp_path


class TestAlertDedupDay:
    def test_a_row_is_stamped_with_the_ist_time(self, alert_db, monkeypatch):
        monkeypatch.setattr(alerts, "datetime", _utc_host(MORNING_UTC))
        alerts.record("health", "msg", deliver=False)
        with alerts._connect() as conn:
            (created_at,) = conn.execute("SELECT created_at FROM alerts").fetchone()
        assert created_at == "2026-10-14 10:30:00"

    def test_once_per_day_gate_finds_a_row_written_by_default_clock(self, alert_db, monkeypatch):
        """UTC says 14 Oct, IST says 15 Oct. The gate is keyed by the IST trading day."""
        monkeypatch.setattr(alerts, "datetime", _utc_host(MIDNIGHT_SPLIT_UTC))
        alerts.record("btst", "msg", symbol="AAA", deliver=False)
        assert alerts._btst_names_sent_today("2026-10-15") == {"AAA"}
        assert alerts._btst_names_sent_today("2026-10-14") is None

    def test_a_caller_supplied_time_is_still_honoured(self, alert_db, monkeypatch):
        monkeypatch.setattr(alerts, "datetime", _utc_host(MIDNIGHT_SPLIT_UTC))
        alerts.record(
            "btst", "msg", symbol="AAA", deliver=False, when=datetime(2026, 9, 11, 14, 50)
        )
        assert alerts._btst_names_sent_today("2026-09-11") == {"AAA"}
