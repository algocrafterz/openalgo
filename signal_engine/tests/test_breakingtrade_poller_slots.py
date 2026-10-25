"""Each scheduled BreakingTrade poll mark is fetched once, and a post-close start stays quiet.

Two defects seen in signal_engine/logs/breakingtrade_poller.log:

- 2026-10-23: the 12:16 mark was fetched four times (12:17, 12:18, 12:18, 12:19). A stored
  snapshot is stamped with the vendor's own table time, not the mark, so the "already stored?"
  check looked for "12:16", found only "12:17", and re-polled every loop until the catch-up
  grace ran out.
- 2026-10-20 and 2026-10-22: the Windows watchdog re-ran `poller.sh start` every 5 minutes after
  the 15:20 auto-stop. Each start sent a STARTED and a stopped Telegram message (44 and 30 health
  messages on those days) and did no work.

The real `_watch` loop runs against a scripted IST clock with every collaborator stubbed.
"""

from __future__ import annotations

from argparse import Namespace
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from signal_engine.analysis.breakingtrade import __main__ as poller
from signal_engine.analysis.breakingtrade import alerts, fetcher, review, tp_watch


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    """A Wednesday (2026-10-14) trading-day instant, naive IST like `_ist_now`."""
    return datetime(2026, 10, 14, hour, minute, second)


def _every_20s(start: datetime, count: int) -> list:
    return [start + timedelta(seconds=20 * i) for i in range(count)]


class _FakeSession:
    def __init__(self, debug_dir=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Run:
    """Drives `_watch` over a scripted clock; one loop iteration per clock tick."""

    def __init__(self, monkeypatch, ticks, stored=(), stamp_offset_minutes=1, fail_first=0):
        self.ticks = list(ticks)
        self.index = 0
        self.stored = set(stored)
        self.stamp_offset = stamp_offset_minutes
        self.fail_first = fail_first
        self.fetches = 0
        self.started_alert = MagicMock()
        self.stopped_alert = MagicMock()
        self.mode_check = MagicMock(return_value=("analyze", True))

        monkeypatch.setattr(poller, "_ist_now", lambda: self.ticks[self.index])
        monkeypatch.setattr(poller.signal, "signal", lambda *a, **k: None)
        monkeypatch.setattr(poller, "_setup_logging", lambda: MagicMock())
        monkeypatch.setattr(poller, "_stored_polls", lambda day: set(self.stored))
        monkeypatch.setattr(poller, "_signals_sent_today", lambda: 0)
        monkeypatch.setattr(poller, "_fetch_and_report", self._fetch)
        monkeypatch.setattr(poller, "sleep", self._sleep)
        monkeypatch.setattr(review, "trading_mode", self.mode_check)
        monkeypatch.setattr(tp_watch, "check", lambda now: 0)
        for name in ("prime_mode_cache", "alert_health"):
            monkeypatch.setattr(alerts, name, MagicMock())
        monkeypatch.setattr(alerts, "alert_started", self.started_alert)
        monkeypatch.setattr(alerts, "alert_stopped", self.stopped_alert)
        monkeypatch.setattr(fetcher, "ScannerSession", _FakeSession)

    def _fetch(self, *args, **kwargs):
        self.fetches += 1
        if self.fetches <= self.fail_first:
            raise RuntimeError("scanner page did not load")
        # The vendor stamps the table with its own time, which is not the scheduled mark.
        stamp = self.ticks[self.index] + timedelta(minutes=self.stamp_offset)
        self.stored.add(f"{stamp:%H:%M}")

    def _sleep(self, seconds):
        self.index += 1
        if self.index >= len(self.ticks):
            raise KeyboardInterrupt

    def run(self) -> int:
        return poller._watch(Namespace(debug_dir=None, store=True, volume=False))


class TestOneFetchPerMark:
    def test_a_late_vendor_stamp_does_not_repeat_the_mark(self, monkeypatch):
        # 12:16:50 through 12:20:50 - the whole catch-up grace of the 12:16 mark.
        run = _Run(monkeypatch, _every_20s(_at(12, 16, 50), 13), stamp_offset_minutes=1)
        run.run()
        assert run.fetches == 1

    def test_an_early_vendor_stamp_does_not_repeat_the_mark(self, monkeypatch):
        run = _Run(monkeypatch, _every_20s(_at(12, 16, 10), 13), stamp_offset_minutes=-1)
        run.run()
        assert run.fetches == 1

    def test_a_failed_poll_is_retried_inside_the_grace(self, monkeypatch):
        run = _Run(monkeypatch, _every_20s(_at(12, 16, 10), 13), fail_first=1)
        run.run()
        assert run.fetches == 2

    def test_a_restart_after_a_late_stamped_poll_does_not_repeat_it(self, monkeypatch):
        run = _Run(monkeypatch, _every_20s(_at(12, 18, 0), 4), stored={"12:17"})
        run.run()
        assert run.fetches == 0

    def test_a_restart_with_nothing_stored_still_catches_up(self, monkeypatch):
        run = _Run(monkeypatch, _every_20s(_at(12, 18, 0), 4))
        run.run()
        assert run.fetches == 1

    def test_the_next_mark_is_still_polled(self, monkeypatch):
        ticks = _every_20s(_at(12, 16, 0), 3) + _every_20s(_at(12, 31, 0), 3)
        run = _Run(monkeypatch, ticks)
        run.run()
        assert run.fetches == 2


class TestStartAfterTheStopTime:
    def test_a_post_close_start_sends_no_alerts_and_no_api_call(self, monkeypatch):
        run = _Run(monkeypatch, [_at(15, 25, 0)])
        assert run.run() == 0
        run.started_alert.assert_not_called()
        run.stopped_alert.assert_not_called()
        run.mode_check.assert_not_called()
        assert run.fetches == 0

    def test_a_poller_running_into_the_stop_time_still_reports_the_day(self, monkeypatch):
        run = _Run(monkeypatch, [_at(15, 19, 50), _at(15, 20, 10)])
        assert run.run() == 0
        run.started_alert.assert_called_once()
        run.stopped_alert.assert_called_once()
