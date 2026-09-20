"""EOD regression check (eod_review.py): report aggregation, the individual file/DB-based
checks, and Telegram delivery routing.

check_trade_ledger, check_reconciliation and check_telegram_channels_live are exercised by
the existing ledger/reconcile/telegram-integration test suites (they call straight into
those modules) - these tests cover the parts unique to this module: reading the day's own
logs and alerts table, assembling the report, and picking the right Telegram destination.
"""

import sqlite3
from types import SimpleNamespace

import pytest

import signal_engine.config as _se_config
from signal_engine.analysis import eod_review
from signal_engine.analysis.breakingtrade import store
from signal_engine.config import TelegramChannel
from signal_engine.smoke_test import CheckResult


def _fake_settings(notify_channel=None):
    return SimpleNamespace(notify_channel=dict(notify_channel or {}))


# ---------------------------------------------------------------------------
# EODReport
# ---------------------------------------------------------------------------

class TestEODReport:
    def _report(self):
        r = eod_review.EODReport(day="2026-09-18", mode="analyze", phase="analyze")
        r.add("TRADE", CheckResult(name="Trade ledger integrity", passed=True, message="OK — 2 positions"))
        r.add("SIGNAL", CheckResult(name="Signal — notifier delivery", passed=False, message="1 issue"))
        return r

    def test_all_passed_is_false_when_any_check_fails(self):
        assert self._report().all_passed is False

    def test_counts(self):
        r = self._report()
        assert r.pass_count == 1
        assert r.fail_count == 1

    def test_all_passed_true_when_every_check_passes(self):
        r = eod_review.EODReport(day="2026-09-18", mode="live", phase="live")
        r.add("SYSTEM", CheckResult(name="System — config", passed=True, message="OK"))
        assert r.all_passed is True

    def test_telegram_digest_groups_by_category_and_shows_pass_fail(self):
        digest = self._report().telegram_digest()
        assert "[TRADE]" in digest
        assert "[SIGNAL]" in digest
        assert "[PASS] Trade ledger integrity: OK — 2 positions" in digest
        assert "[FAIL] Signal — notifier delivery: 1 issue" in digest
        assert "1 CHECK(S) FAILED (1/2)" in digest

    def test_telegram_digest_names_the_full_report_path_for_the_same_day(self):
        digest = self._report().telegram_digest()
        assert "eod-2026-09-18.md" in digest

    def test_telegram_digest_truncates_past_the_configured_length(self):
        r = eod_review.EODReport(day="2026-09-18", mode="analyze", phase="analyze")
        r.add("SYSTEM", CheckResult(name="System — huge", passed=False, message="x" * 5000))
        digest = r.telegram_digest(max_len=500)
        assert len(digest) <= 520
        assert digest.endswith("... (truncated)")


# ---------------------------------------------------------------------------
# Log-file checks
# ---------------------------------------------------------------------------

class TestSignalEngineErrors:
    def test_no_file_is_ok(self, tmp_path, monkeypatch):
        monkeypatch.setattr(eod_review, "_LOG_DIR", str(tmp_path))
        assert eod_review.check_signal_engine_errors("2026-09-18") == "OK — no signal_engine errors logged"

    def test_rows_present_raises_with_count_and_sample(self, tmp_path, monkeypatch):
        monkeypatch.setattr(eod_review, "_LOG_DIR", str(tmp_path))
        (tmp_path / "errors_2026-09-18.jsonl").write_text(
            '{"level": "ERROR", "message": "boom one"}\n'
            '{"level": "CRITICAL", "message": "boom two"}\n'
        )
        with pytest.raises(RuntimeError, match=r"2 error\(s\) logged \(1 CRITICAL\).*boom one"):
            eod_review.check_signal_engine_errors("2026-09-18")

    def test_malformed_lines_are_skipped_not_fatal(self, tmp_path, monkeypatch):
        monkeypatch.setattr(eod_review, "_LOG_DIR", str(tmp_path))
        (tmp_path / "errors_2026-09-18.jsonl").write_text("not json\n")
        assert eod_review.check_signal_engine_errors("2026-09-18") == "OK — no signal_engine errors logged"


class TestOpenAlgoAppErrors:
    def test_filters_to_the_requested_day_only(self, tmp_path, monkeypatch):
        log_path = tmp_path / "log"
        log_path.mkdir()
        monkeypatch.chdir(tmp_path)
        (log_path / "errors.jsonl").write_text(
            '{"ts": "2026-09-17 10:00:00", "message": "yesterday"}\n'
            '{"ts": "2026-09-18 10:00:00", "message": "today"}\n'
        )
        with pytest.raises(RuntimeError, match=r"1 OpenAlgo app error\(s\) logged today.*today"):
            eod_review.check_openalgo_app_errors("2026-09-18")

    def test_no_rows_for_the_day_is_ok(self, tmp_path, monkeypatch):
        log_path = tmp_path / "log"
        log_path.mkdir()
        monkeypatch.chdir(tmp_path)
        (log_path / "errors.jsonl").write_text('{"ts": "2026-09-17 10:00:00", "message": "yesterday"}\n')
        assert eod_review.check_openalgo_app_errors("2026-09-18") == "OK — no OpenAlgo app errors logged today"

    def test_missing_file_is_ok(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert eod_review.check_openalgo_app_errors("2026-09-18") == "OK — no OpenAlgo app errors logged today"


class TestNotifierDelivery:
    def test_no_logs_at_all_is_ok(self, tmp_path, monkeypatch):
        monkeypatch.setattr(eod_review, "_LOG_DIR", str(tmp_path))
        assert eod_review.check_notifier_delivery("2026-09-18") == "OK — no notifier delivery failures logged"

    def test_a_failure_pattern_in_any_mode_log_is_caught(self, tmp_path, monkeypatch):
        monkeypatch.setattr(eod_review, "_LOG_DIR", str(tmp_path))
        (tmp_path / "signal_engine_live_2026-09-18.log").write_text(
            "2026-09-18 10:00:00 | INFO    | -            | notifier | all fine\n"
            "2026-09-18 10:01:00 | WARNING | -            | notifier | Notifier: failed to send message: timeout\n"
        )
        with pytest.raises(RuntimeError, match=r"1 notifier delivery issue\(s\)"):
            eod_review.check_notifier_delivery("2026-09-18")

    def test_clean_log_with_no_failure_pattern_is_ok(self, tmp_path, monkeypatch):
        monkeypatch.setattr(eod_review, "_LOG_DIR", str(tmp_path))
        (tmp_path / "signal_engine_analyze_2026-09-18.log").write_text(
            "2026-09-18 10:00:00 | INFO    | -            | notifier | delivered fine\n"
        )
        assert eod_review.check_notifier_delivery("2026-09-18") == "OK — no notifier delivery failures logged"


class TestResourceWarnings:
    def test_leak_signature_is_caught(self, tmp_path, monkeypatch):
        monkeypatch.setattr(eod_review, "_LOG_DIR", str(tmp_path))
        (tmp_path / "signal_engine_live_2026-09-18.log").write_text(
            "2026-09-18 10:00:00 | ERROR   | -            | db | sqlite3.OperationalError: database is locked\n"
        )
        with pytest.raises(RuntimeError, match=r"1 resource/FD warning\(s\)"):
            eod_review.check_resource_warnings("2026-09-18")

    def test_no_signatures_is_ok(self, tmp_path, monkeypatch):
        monkeypatch.setattr(eod_review, "_LOG_DIR", str(tmp_path))
        (tmp_path / "signal_engine_live_2026-09-18.log").write_text("2026-09-18 10:00:00 | INFO | - | x | fine\n")
        assert eod_review.check_resource_warnings("2026-09-18") == "OK — no resource/FD warnings logged"


# ---------------------------------------------------------------------------
# BreakingTrade alert delivery (breakingtrade.db)
# ---------------------------------------------------------------------------

class TestBreakingtradeAlertDelivery:
    @pytest.fixture(autouse=True)
    def isolated_db(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "_DB_PATH", str(tmp_path / "breakingtrade.db"))

    def _seed(self, rows):
        conn = sqlite3.connect(store._DB_PATH)
        conn.executescript(
            "CREATE TABLE alerts (created_at TEXT, kind TEXT, symbol TEXT, direction TEXT, "
            "scan TEXT, message TEXT, delivered INTEGER)"
        )
        conn.executemany(
            "INSERT INTO alerts (created_at, kind, symbol, direction, scan, message, delivered) "
            "VALUES (?, 'trade_signal', 'X', 'LONG', 'scan', 'msg', ?)",
            rows,
        )
        conn.commit()
        conn.close()

    def test_no_db_file_is_ok(self):
        assert eod_review.check_breakingtrade_alert_delivery("2026-09-18") == "OK — no breakingtrade.db yet"

    def test_no_alerts_today_is_ok(self):
        self._seed([("2026-09-17 10:00:00", 1)])
        assert eod_review.check_breakingtrade_alert_delivery("2026-09-18") == "OK — no BreakingTrade alerts today"

    def test_all_delivered_passes(self):
        self._seed([("2026-09-18 10:00:00", 1), ("2026-09-18 11:00:00", 1)])
        assert eod_review.check_breakingtrade_alert_delivery("2026-09-18") == "OK — 2/2 BreakingTrade alerts delivered"

    def test_some_undelivered_raises(self):
        self._seed([("2026-09-18 10:00:00", 1), ("2026-09-18 11:00:00", 0)])
        with pytest.raises(RuntimeError, match=r"1/2 BreakingTrade alert\(s\) recorded but not delivered"):
            eod_review.check_breakingtrade_alert_delivery("2026-09-18")


# ---------------------------------------------------------------------------
# Telegram delivery
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class TestSendToAdminChannel:
    def test_sends_to_the_current_phase_channel(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            "signal_engine.analysis.breakingtrade.alerts._env",
            lambda: {"BREAKINGTRADE_BOT_TOKEN": "123:abc"},
        )
        monkeypatch.setattr(
            _se_config, "settings",
            _fake_settings({"analyze": TelegramChannel(name="signal-engine-analyze", id=-1)}),
        )

        def fake_post(url, json, timeout):
            captured.update(json)
            return _FakeResponse()

        monkeypatch.setattr(eod_review.httpx, "post", fake_post)

        assert eod_review.send_to_admin_channel("hello", "analyze") is True
        assert captured["chat_id"] == "-1"
        assert captured["text"] == "```\nhello\n```"

    def test_falls_back_to_whichever_phase_is_configured(self, monkeypatch):
        """Mirrors notifier.py's _channel_for_phase(): a safety-relevant report must not be
        silently dropped just because only one phase's admin channel is set up."""
        captured = {}
        monkeypatch.setattr(
            "signal_engine.analysis.breakingtrade.alerts._env",
            lambda: {"BREAKINGTRADE_BOT_TOKEN": "123:abc"},
        )
        monkeypatch.setattr(
            _se_config, "settings",
            _fake_settings({"live": TelegramChannel(name="signal-engine-live", id=-2)}),
        )

        def fake_post(url, json, timeout):
            captured.update(json)
            return _FakeResponse()

        monkeypatch.setattr(eod_review.httpx, "post", fake_post)

        assert eod_review.send_to_admin_channel("hello", "analyze") is True
        assert captured["chat_id"] == "-2"

    def test_missing_token_does_not_attempt_a_network_call(self, monkeypatch):
        monkeypatch.setattr(
            "signal_engine.analysis.breakingtrade.alerts._env", lambda: {"BREAKINGTRADE_BOT_TOKEN": None}
        )
        monkeypatch.setattr(
            _se_config, "settings",
            _fake_settings({"analyze": TelegramChannel(name="signal-engine-analyze", id=-1)}),
        )
        monkeypatch.setattr(
            eod_review.httpx, "post", lambda *a, **k: pytest.fail("must not call the network")
        )

        assert eod_review.send_to_admin_channel("hello", "analyze") is False

    def test_missing_channel_does_not_attempt_a_network_call(self, monkeypatch):
        monkeypatch.setattr(
            "signal_engine.analysis.breakingtrade.alerts._env",
            lambda: {"BREAKINGTRADE_BOT_TOKEN": "123:abc"},
        )
        monkeypatch.setattr(_se_config, "settings", _fake_settings({}))
        monkeypatch.setattr(
            eod_review.httpx, "post", lambda *a, **k: pytest.fail("must not call the network")
        )

        assert eod_review.send_to_admin_channel("hello", "analyze") is False

    def test_http_failure_returns_false_without_raising(self, monkeypatch):
        monkeypatch.setattr(
            "signal_engine.analysis.breakingtrade.alerts._env",
            lambda: {"BREAKINGTRADE_BOT_TOKEN": "123:abc"},
        )
        monkeypatch.setattr(
            _se_config, "settings",
            _fake_settings({"analyze": TelegramChannel(name="signal-engine-analyze", id=-1)}),
        )
        monkeypatch.setattr(
            eod_review.httpx, "post", lambda *a, **k: _FakeResponse(status_code=400, text="bad request")
        )

        assert eod_review.send_to_admin_channel("hello", "analyze") is False
