"""EOD regression check (eod_review.py): report aggregation, the individual file/DB-based
checks, and the digest. Nothing is sent to Telegram.

check_trade_ledger, check_reconciliation and check_telegram_session_live are exercised by
the existing ledger/reconcile/telegram-integration test suites (they call straight into
those modules) - these tests cover the parts unique to this module: reading the day's own
logs and alerts table, and assembling the report.
"""

import sqlite3

import pytest

from signal_engine.analysis import eod_review
from signal_engine.analysis.breakingtrade import store
from signal_engine.smoke_test import CheckResult

# ---------------------------------------------------------------------------
# EODReport
# ---------------------------------------------------------------------------

class TestEODReport:
    def _report(self):
        r = eod_review.EODReport(day="2026-09-18", mode="analyze", phase="analyze")
        r.add("TRADE", CheckResult(name="Trade ledger integrity", passed=True, message="OK — 2 positions"))
        r.add("SIGNAL", CheckResult(name="Signal — BreakingTrade alert delivery", passed=False, message="1 issue"))
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

    def test_digest_groups_by_category_and_shows_pass_fail(self):
        digest = self._report().digest()
        assert "[TRADE]" in digest
        assert "[SIGNAL]" in digest
        assert "[PASS] Trade ledger integrity: OK — 2 positions" in digest
        assert "[FAIL] Signal — BreakingTrade alert delivery: 1 issue" in digest
        assert "1 CHECK(S) FAILED (1/2)" in digest

    def test_digest_names_the_full_report_path_for_the_same_day(self):
        digest = self._report().digest()
        assert "eod-2026-09-18.md" in digest

    def test_digest_truncates_past_the_configured_length(self):
        r = eod_review.EODReport(day="2026-09-18", mode="analyze", phase="analyze")
        r.add("SYSTEM", CheckResult(name="System — huge", passed=False, message="x" * 5000))
        digest = r.digest(max_len=500)
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

    def test_kinds_that_are_never_posted_are_not_counted(self):
        self._seed([("2026-09-18 10:00:00", 1)])
        conn = sqlite3.connect(store._DB_PATH)
        conn.execute(
            "INSERT INTO alerts (created_at, kind, symbol, direction, scan, message, delivered) "
            "VALUES ('2026-09-18 11:00:00', 'critical', 'X', 'LONG', 'scan', 'msg', 0)"
        )
        conn.commit()
        conn.close()
        assert eod_review.check_breakingtrade_alert_delivery("2026-09-18") == "OK — 1/1 BreakingTrade alerts delivered"

    def test_some_undelivered_raises(self):
        self._seed([("2026-09-18 10:00:00", 1), ("2026-09-18 11:00:00", 0)])
        with pytest.raises(RuntimeError, match=r"1/2 BreakingTrade alert\(s\) recorded but not delivered"):
            eod_review.check_breakingtrade_alert_delivery("2026-09-18")


# ---------------------------------------------------------------------------
# Execution integrity (duplicate fills, engine books vs fill-backed pages)
# ---------------------------------------------------------------------------

class TestExecutionIntegrity:
    def _result(self, *, gap=None, dups=()):
        from signal_engine.analysis import pages_vs_fills as pvf

        res = pvf.Result(day="2026-10-13", mode="analyze")
        res.engine_gap = dict(gap or {})
        res.duplicates = list(dups)
        return res

    def _check(self, monkeypatch, result, mode="analyze"):
        from signal_engine.analysis import pages_vs_fills as pvf

        monkeypatch.setattr(pvf, "run", lambda day, m: result)
        return eod_review.check_execution_integrity(mode, "2026-10-13")

    def test_clean_day_passes(self, monkeypatch):
        out = self._check(monkeypatch, self._result(gap={"ORB": (-1.4, -1.4)}))
        assert out.startswith("OK")

    def test_engine_books_off_the_pages_fail_naming_each_strategy(self, monkeypatch):
        """13 Oct: engine +5,140 vs broker +2,176; EVWAP +2,882.9 booked vs +671.2 filled."""
        res = self._result(gap={"EVWAP-EARLY-PY": (2882.9, 671.2), "BREAKOUT": (-854.7, -1606.7)})
        with pytest.raises(RuntimeError) as exc:
            self._check(monkeypatch, res)
        assert "EVWAP-EARLY-PY" in str(exc.value) and "BREAKOUT" in str(exc.value)

    def test_duplicate_fills_fail(self, monkeypatch):
        from signal_engine.analysis.pages_vs_fills import DuplicateFill

        dup = DuplicateFill("EVWAP-EARLY-PY", "SUNPHARMA", "SELL", 236, 3, "2026-10-13 13:56:46")
        with pytest.raises(RuntimeError) as exc:
            self._check(monkeypatch, self._result(dups=[dup]))
        assert "SUNPHARMA" in str(exc.value) and "3x" in str(exc.value)

    def test_unknown_mode_is_skipped(self, monkeypatch):
        out = self._check(monkeypatch, self._result(), mode="unknown")
        assert out.startswith("SKIPPED")
