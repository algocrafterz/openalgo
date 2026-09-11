"""L6: errors_*.jsonl carries no icons, and keeps the fields the debugging workflow uses.

loguru's serialize=True renders the whole record including level.icon, which put a literal
emoji into every line — against the project's no-icons rule, and for nothing, since
level.name already says the same thing.
"""

import json
import os
from datetime import date, timedelta

import pytest

from signal_engine import logger_setup


class _Rec(dict):
    pass


def _record(message="boom", level="ERROR", exception=None):
    import datetime as _dt

    class _L:
        name = level
        icon = "X"

    class _F:
        name = "risk.py"

    class _P:
        id = 42

    class _T:
        id = 7

    return {
        "time": _dt.datetime(2026, 9, 11, 10, 30),
        "level": _L(), "name": "signal_engine.risk", "module": "risk",
        "function": "check_exposure", "file": _F(), "line": 501,
        "message": message, "extra": {"symbol": "SBIN"},
        "process": _P(), "thread": _T(), "exception": exception,
    }


class TestRecordShape:
    def test_no_icon_field_anywhere(self):
        payload = json.dumps(logger_setup._error_record(_record()))
        assert "icon" not in payload

    def test_keeps_the_fields_the_debugging_workflow_reads(self):
        out = logger_setup._error_record(_record())
        for key in ("time", "level", "logger", "module", "file", "message", "symbol"):
            assert key in out
        assert out["file"] == "risk.py:501"
        assert out["level"] == "ERROR"
        assert out["symbol"] == "SBIN"

    def test_no_exception_is_null_not_missing(self):
        assert logger_setup._error_record(_record())["exception"] is None

    def test_the_whole_record_is_json_serialisable(self):
        json.dumps(logger_setup._error_record(_record()), default=str)


class TestRetention:
    def test_files_past_the_window_are_removed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(logger_setup, "_ERROR_LOG_DIR", str(tmp_path))
        old = tmp_path / f"errors_{(date.today() - timedelta(days=200)).isoformat()}.jsonl"
        recent = tmp_path / f"errors_{date.today().isoformat()}.jsonl"
        old.write_text("{}")
        recent.write_text("{}")
        logger_setup._prune_error_logs()
        assert not old.exists()
        assert recent.exists()

    def test_unrelated_files_are_left_alone(self, tmp_path, monkeypatch):
        monkeypatch.setattr(logger_setup, "_ERROR_LOG_DIR", str(tmp_path))
        other = tmp_path / "signal_engine_analyze_2020-01-01.log"
        other.write_text("x")
        logger_setup._prune_error_logs()
        assert other.exists()

    def test_a_malformed_name_is_skipped_not_crashed_on(self, tmp_path, monkeypatch):
        monkeypatch.setattr(logger_setup, "_ERROR_LOG_DIR", str(tmp_path))
        (tmp_path / "errors_not-a-date.jsonl").write_text("x")
        logger_setup._prune_error_logs()  # must not raise


class TestSinkNeverRaises:
    def test_a_broken_record_is_swallowed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(logger_setup, "_ERROR_LOG_DIR", str(tmp_path))

        class _Msg:
            record = {"nonsense": True}

        logger_setup._error_jsonl_sink(_Msg())  # a logging failure must not become THE failure

    def test_a_good_record_lands_as_one_json_line(self, tmp_path, monkeypatch):
        monkeypatch.setattr(logger_setup, "_ERROR_LOG_DIR", str(tmp_path))
        monkeypatch.setattr(logger_setup, "_last_error_log_date", None)

        class _Msg:
            record = _record("kaboom")

        logger_setup._error_jsonl_sink(_Msg())
        path = tmp_path / "errors_2026-09-11.jsonl"
        line = json.loads(path.read_text().strip())
        assert line["message"] == "kaboom"
        assert "icon" not in path.read_text()
