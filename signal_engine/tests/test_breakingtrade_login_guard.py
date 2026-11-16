"""The BreakingTrade poller must not hammer the vendor's sign-in (account locked 2026-11-10)."""

from __future__ import annotations

from datetime import datetime, timedelta

from signal_engine.analysis.breakingtrade import fetcher, login_guard
from signal_engine.tests.test_breakingtrade_poller_slots import _at, _every_20s, _Run


def _t(minute: int, hour: int = 10, day: int = 11) -> datetime:
    return datetime(2026, 11, day, hour, minute)


class TestLoginGuard:
    def test_first_attempt_is_allowed(self, tmp_path):
        assert login_guard.reserve_attempt(_t(0), str(tmp_path / "g.json")) is None

    def test_second_attempt_inside_the_gap_is_refused(self, tmp_path):
        path = str(tmp_path / "g.json")
        login_guard.reserve_attempt(_t(0), path)
        assert login_guard.reserve_attempt(_t(10), path) is not None

    def test_attempt_after_the_gap_is_allowed(self, tmp_path):
        path = str(tmp_path / "g.json")
        login_guard.reserve_attempt(_t(0), path)
        gap = login_guard.MIN_GAP_MINUTES
        later = _t(0) + timedelta(minutes=gap + 1)
        assert login_guard.reserve_attempt(later, path) is None

    def test_daily_cap(self, tmp_path):
        path = str(tmp_path / "g.json")
        now = _t(0, hour=9)
        for i in range(login_guard.MAX_PER_DAY):
            at = now + timedelta(minutes=(login_guard.MIN_GAP_MINUTES + 1) * i)
            assert login_guard.reserve_attempt(at, path) is None
        late = now + timedelta(hours=6)
        assert login_guard.reserve_attempt(late, path) is not None

    def test_a_rejected_sign_in_blocks_the_rest_of_the_day_only(self, tmp_path):
        path = str(tmp_path / "g.json")
        login_guard.reserve_attempt(_t(0), path)
        login_guard.record_failure(_t(1), path)
        assert login_guard.reserve_attempt(_t(0, hour=14), path) is not None
        assert login_guard.reserve_attempt(_t(0, day=12), path) is None


class TestPollerRetries:
    def _run(self, monkeypatch, tmp_path, ticks, exc):
        monkeypatch.setattr(
            "signal_engine.analysis.breakingtrade.__main__._INSTANCE_LOCK_PATH",
            str(tmp_path / "p.lock"),
        )
        run = _Run(monkeypatch, ticks)

        def boom(*args, **kwargs):
            run.fetches += 1
            raise exc

        monkeypatch.setattr("signal_engine.analysis.breakingtrade.__main__._fetch_and_report", boom)
        run.run()
        return run

    def test_login_required_is_attempted_once_per_mark(self, monkeypatch, tmp_path):
        run = self._run(
            monkeypatch,
            tmp_path,
            _every_20s(_at(12, 16, 10), 13),
            fetcher.LoginRequired("rejected"),
        )
        assert run.fetches == 1

    def test_other_failures_are_capped_and_spaced(self, monkeypatch, tmp_path):
        run = self._run(
            monkeypatch, tmp_path, _every_20s(_at(12, 16, 10), 13), RuntimeError("page")
        )
        assert run.fetches == 2


class TestStaleSnapshotGate:
    def test_fresh_snapshot_passes(self):
        from signal_engine.analysis.breakingtrade import __main__ as poller

        poller.check_fresh(datetime(2026, 11, 11, 11, 14), datetime(2026, 11, 11, 11, 16))

    def test_old_snapshot_in_market_hours_is_refused(self):
        import pytest

        from signal_engine.analysis.breakingtrade import __main__ as poller

        with pytest.raises(poller.StaleSnapshot):
            poller.check_fresh(datetime(2026, 11, 11, 10, 40), datetime(2026, 11, 11, 11, 16))

    def test_yesterdays_table_is_refused_at_the_open(self):
        import pytest

        from signal_engine.analysis.breakingtrade import __main__ as poller

        with pytest.raises(poller.StaleSnapshot):
            poller.check_fresh(datetime(2026, 11, 10, 15, 30), datetime(2026, 11, 11, 9, 20))

    def test_not_enforced_after_close(self):
        from signal_engine.analysis.breakingtrade import __main__ as poller

        poller.check_fresh(datetime(2026, 11, 11, 15, 30), datetime(2026, 11, 11, 18, 0))

    def test_stale_poll_sends_no_signal_and_keeps_the_browser(self, monkeypatch, tmp_path):
        from signal_engine.analysis.breakingtrade import __main__ as poller

        monkeypatch.setattr(poller, "_INSTANCE_LOCK_PATH", str(tmp_path / "p.lock"))
        run = _Run(monkeypatch, _every_20s(_at(12, 16, 10), 13))

        def stale(*args, **kwargs):
            run.fetches += 1
            poller.check_fresh(_at(11, 0), _at(12, 16))

        monkeypatch.setattr(poller, "_fetch_and_report", stale)
        run.run()
        assert run.fetches == 2


class TestFormatFailureKeepsBrowser:
    def test_format_error_does_not_close_the_session(self, monkeypatch, tmp_path):
        from signal_engine.analysis.breakingtrade import __main__ as poller
        from signal_engine.analysis.breakingtrade.extractor import SnapshotFormatError

        closed = []

        class Session:
            def __init__(self, debug_dir=None):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                closed.append(1)
                return False

        monkeypatch.setattr(poller, "_INSTANCE_LOCK_PATH", str(tmp_path / "p.lock"))
        run = _Run(monkeypatch, _every_20s(_at(12, 16, 10), 13))
        monkeypatch.setattr(fetcher, "ScannerSession", Session)

        def bad(*args, **kwargs):
            run.fetches += 1
            raise SnapshotFormatError("columns renamed")

        monkeypatch.setattr(poller, "_fetch_and_report", bad)
        run.run()
        # Only the shutdown close; no mid-day teardown that would force another sign-in.
        assert closed == [1]


def test_refusal_is_read_only_and_matches_reserve(tmp_path):
    path = str(tmp_path / "guard.json")
    start = datetime(2026, 11, 11, 10, 0)
    assert login_guard.refusal(start, path) is None
    assert login_guard.refusal(start, path) is None  # asking claims nothing
    assert login_guard.reserve_attempt(start, path) is None
    assert "under 30 minutes" in login_guard.refusal(start + timedelta(minutes=5), path)
    assert login_guard.refusal(start + timedelta(minutes=31), path) is None


class TestExtraSignInAfterConfirmedDrop:
    """BUG-027: one extra sign-in (hard cap 4) after a confirmed session drop."""

    def _three(self, path):
        base = _t(0, hour=9)
        for i in range(login_guard.MAX_PER_DAY):
            at = base + timedelta(minutes=(login_guard.MIN_GAP_MINUTES + 1) * i)
            assert login_guard.reserve_attempt(at, path) is None
            login_guard.record_success(at + timedelta(minutes=1), path)
        return base + timedelta(minutes=(login_guard.MIN_GAP_MINUTES + 1) * 2)

    def test_fourth_allowed_after_confirmed_drop_fifth_refused(self, tmp_path):
        path = str(tmp_path / "g.json")
        third = self._three(path)
        drop = third + timedelta(minutes=login_guard.MIN_GAP_MINUTES + 5)
        assert login_guard.note_session_drop(drop, path) is True
        assert login_guard.reserve_attempt(drop, path) is None
        assert login_guard.MAX_PER_DAY == 3
        later = drop + timedelta(hours=3)
        login_guard.note_session_drop(later, path)
        reason = login_guard.reserve_attempt(later, path)
        assert reason is not None and "extra" in reason.lower()
        assert "used" in reason.lower()

    def test_fourth_refused_without_confirmed_drop(self, tmp_path):
        path = str(tmp_path / "g.json")
        third = self._three(path)
        reason = login_guard.reserve_attempt(third + timedelta(hours=3), path)
        assert "Already signed in 3 times today" in reason
        assert "not used" in reason.lower()

    def test_drop_inside_gap_of_last_sign_in_is_not_confirmed(self, tmp_path):
        path = str(tmp_path / "g.json")
        third = self._three(path)
        soon = third + timedelta(minutes=5)
        assert login_guard.note_session_drop(soon, path) is False
        later = third + timedelta(hours=3)
        assert "Already signed in 3 times" in login_guard.reserve_attempt(later, path)

    def test_drop_without_prior_success_is_not_confirmed(self, tmp_path):
        path = str(tmp_path / "g.json")
        login_guard.reserve_attempt(_t(0), path)
        assert login_guard.note_session_drop(_t(45), path) is False

    def test_gap_still_enforced_for_extra(self, tmp_path):
        path = str(tmp_path / "g.json")
        third = self._three(path)
        drop = third + timedelta(minutes=login_guard.MIN_GAP_MINUTES + 1)
        assert login_guard.note_session_drop(drop, path) is True
        assert login_guard.reserve_attempt(third + timedelta(minutes=10), path) is not None
        assert "under" in login_guard.refusal(third + timedelta(minutes=10), path)

    def test_old_format_state_loads(self, tmp_path):
        import json

        path = tmp_path / "g.json"
        path.write_text(json.dumps({"day": "2026-11-11", "attempts": ["2026-11-11T09:00:00"]}))
        assert login_guard.reserve_attempt(_t(0, hour=10), str(path)) is None
        info = json.loads(path.read_text())
        assert len(info["attempts"]) == 2

    def test_failed_day_still_blocks_extra(self, tmp_path):
        path = str(tmp_path / "g.json")
        third = self._three(path)
        login_guard.note_session_drop(third + timedelta(hours=1), path)
        login_guard.record_failure(third + timedelta(hours=1), path)
        assert "rejected" in login_guard.reserve_attempt(third + timedelta(hours=2), path)


class TestAttemptLogging:
    def test_logs_each_attempt_without_secrets(self, tmp_path, monkeypatch, caplog):
        secret = "S3CRET-PW-xyz"
        email = "user@example.com"
        gpath = str(tmp_path / "g.json")
        for name in ("reserve_attempt", "record_failure", "record_success", "attempt_info",
                     "note_session_drop", "refusal"):
            orig = getattr(login_guard, name)
            monkeypatch.setattr(
                login_guard, name, lambda *a, _o=orig, **k: _o(*a, path=gpath, **k)
            )
        monkeypatch.setenv("BREAKINGTRADE_PASSWORD", secret)
        monkeypatch.setenv("BREAKINGTRADE_EMAIL", email)
        monkeypatch.setattr(fetcher, "_credentials", lambda: (email, secret))
        monkeypatch.setattr(fetcher, "_is_signed_out", lambda page: True)
        monkeypatch.setattr(fetcher, "_save_cookies", lambda ctx: None)

        def boom(page, e, p):
            raise RuntimeError(f"bad {p}")

        monkeypatch.setattr(fetcher, "_attempt_login", boom)
        session = fetcher.ScannerSession()
        session._page = object()
        session._context = object()
        with caplog.at_level("INFO"):
            try:
                session._ensure_signed_in("http://x")
            except RuntimeError:
                pass
        out = caplog.text
        assert "sign-in attempt 1" in out
        assert "extra=no" in out
        assert "state_before=signed_out" in out
        assert "RuntimeError" in out
        assert secret not in out and email not in out
