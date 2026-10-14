"""The BreakingTrade poller must make market-hours decisions on IST, not the host clock.

Each case freezes one real instant, builds a host clock that reads it in UTC or in
America/New_York, and runs the real `_watch` loop for exactly one iteration with every
collaborator (browser, alerts, tp_watch, logging, sleep) stubbed. Only the clock decisions are
exercised: auto-stop at 15:20 IST, the TP-watch window from 09:15 IST, and the scheduled poll
marks. On the old naive code every one of these fails on a non-IST host, because the host
wall clock lands on the wrong side of the boundary.
"""

from __future__ import annotations

import datetime as datetime_module
from argparse import Namespace
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from signal_engine.analysis.breakingtrade import __main__ as poller
from signal_engine.analysis.breakingtrade import alerts, fetcher, review, tp_watch
from signal_engine.timeutils import IST

HOSTS = [pytest.param("UTC", id="utc-host"), pytest.param("America/New_York", id="ny-host")]


def _ist(hour: int, minute: int, second: int = 0) -> datetime:
    """A Wednesday (2026-10-14) trading-day instant given as an IST wall-clock time."""
    return datetime(2026, 10, 14, hour, minute, second, tzinfo=IST)


def _host_clock(instant: datetime, host_tz: str):
    """A datetime class whose now()/today() read `instant` as seen from a host in `host_tz`."""
    zone = ZoneInfo(host_tz)

    class HostDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return instant.astimezone(zone).replace(tzinfo=None)
            return instant.astimezone(tz)

        @classmethod
        def today(cls):
            return cls.now()

    return HostDatetime


class _Scenario:
    """Runs one poller iteration at `instant` and records which decisions it made."""

    def __init__(self, monkeypatch, instant: datetime, host_tz: str):
        self.fetches = 0
        self.tp_checks = 0
        self.slept = False
        self.stopped_alert = False

        monkeypatch.setattr(datetime_module, "datetime", _host_clock(instant, host_tz))
        monkeypatch.setattr(poller.signal, "signal", lambda *a, **k: None)
        monkeypatch.setattr(poller, "_setup_logging", lambda: MagicMock())
        monkeypatch.setattr(poller, "_stored_polls", lambda day: set())
        monkeypatch.setattr(poller, "_signals_sent_today", lambda: 0)
        monkeypatch.setattr(poller, "_fetch_and_report", self._fetch)
        monkeypatch.setattr(poller, "sleep", self._sleep)
        monkeypatch.setattr(review, "trading_mode", lambda: ("analyze", True))
        monkeypatch.setattr(tp_watch, "check", self._tp_check)
        for name in ("prime_mode_cache", "alert_started", "alert_health"):
            monkeypatch.setattr(alerts, name, MagicMock())
        monkeypatch.setattr(alerts, "alert_stopped", self._alert_stopped)
        monkeypatch.setattr(fetcher, "ScannerSession", _FakeSession)

    def _fetch(self, *args, **kwargs):
        self.fetches += 1

    def _tp_check(self, now):
        self.tp_checks += 1
        return 0

    def _sleep(self, seconds):
        self.slept = True
        raise KeyboardInterrupt

    def _alert_stopped(self, polls, signals):
        self.stopped_alert = True

    def run(self) -> int:
        return poller._watch(Namespace(debug_dir=None, store=False, volume=False))


class _FakeSession:
    def __init__(self, debug_dir=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize("host_tz", HOSTS)
class TestAutoStopBoundary:
    def test_one_second_before_the_stop_time_keeps_polling(self, monkeypatch, host_tz):
        run = _Scenario(monkeypatch, _ist(15, 19, 59), host_tz)
        run.run()
        assert run.slept is True  # reached the end of a normal iteration
        assert run.stopped_alert is True  # the loop ended only because the test's sleep ended it

    def test_at_the_stop_time_the_poller_stops_before_doing_any_work(self, monkeypatch, host_tz):
        run = _Scenario(monkeypatch, _ist(15, 20, 0), host_tz)
        assert run.run() == 0
        assert run.slept is False  # stopped at the top of the iteration, never reached sleep
        assert run.tp_checks == 0
        assert run.fetches == 0

    def test_after_the_stop_time_it_also_stops(self, monkeypatch, host_tz):
        run = _Scenario(monkeypatch, _ist(15, 45, 0), host_tz)
        run.run()
        assert run.slept is False


@pytest.mark.parametrize("host_tz", HOSTS)
class TestTpWatchWindow:
    def test_before_the_open_nothing_is_watched(self, monkeypatch, host_tz):
        run = _Scenario(monkeypatch, _ist(9, 14, 59), host_tz)
        run.run()
        assert run.tp_checks == 0

    def test_from_the_open_the_watch_runs(self, monkeypatch, host_tz):
        run = _Scenario(monkeypatch, _ist(9, 15, 0), host_tz)
        run.run()
        assert run.tp_checks == 1

    def test_mid_session_the_watch_runs(self, monkeypatch, host_tz):
        run = _Scenario(monkeypatch, _ist(12, 30, 0), host_tz)
        run.run()
        assert run.tp_checks == 1


@pytest.mark.parametrize("host_tz", HOSTS)
class TestPollWindows:
    def test_before_the_first_mark_nothing_is_fetched(self, monkeypatch, host_tz):
        run = _Scenario(monkeypatch, _ist(9, 19, 30), host_tz)
        run.run()
        assert run.fetches == 0

    def test_at_the_first_mark_a_poll_is_fetched(self, monkeypatch, host_tz):
        run = _Scenario(monkeypatch, _ist(9, 20, 30), host_tz)
        run.run()
        assert run.fetches == 1

    def test_inside_the_lunch_gap_nothing_is_fetched(self, monkeypatch, host_tz):
        run = _Scenario(monkeypatch, _ist(13, 30, 0), host_tz)
        run.run()
        assert run.fetches == 0

    def test_the_last_mark_of_the_day_is_fetched(self, monkeypatch, host_tz):
        run = _Scenario(monkeypatch, _ist(15, 10, 30), host_tz)
        run.run()
        assert run.fetches == 1

    def test_an_off_mark_minute_is_not_fetched(self, monkeypatch, host_tz):
        run = _Scenario(monkeypatch, _ist(12, 55, 0), host_tz)
        run.run()
        assert run.fetches == 0


@pytest.mark.parametrize("host_tz", HOSTS)
def test_ist_now_is_naive_ist_wall_clock_on_any_host(monkeypatch, host_tz):
    monkeypatch.setattr(datetime_module, "datetime", _host_clock(_ist(15, 20, 0), host_tz))
    now = poller._ist_now()
    assert now == datetime(2026, 10, 14, 15, 20, 0)
    assert now.tzinfo is None
