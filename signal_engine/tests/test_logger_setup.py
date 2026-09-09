"""Logging shape the EOD debugging workflow depends on.

Going live means the first read after a bad session is the log, not the code. Two things have
to be true for that to work: an error must carry the frames that produced it, and errors must
be findable without scrolling a whole trading day of INFO lines.
"""

import json

import pytest
from loguru import logger

from signal_engine.logger_setup import setup_logger


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # _current_mode is a raw module global (see logger_setup.py's docstring on why) - reset it
    # so one test's set_mode() call can't leak into the next test's expectations.
    import signal_engine.logger_setup as logger_setup_module

    monkeypatch.setattr(logger_setup_module, "_current_mode", "unknown")
    yield tmp_path
    logger.remove()


class TestErrorSink:
    def test_errors_are_written_as_one_json_object_per_line(self, log_dir):
        setup_logger()
        logger.bind(symbol="SBIN").error("order rejected")
        logger.complete()

        files = list((log_dir / "signal_engine" / "logs").glob("errors_*.jsonl"))
        assert len(files) == 1, "expected exactly one errors jsonl sink"
        lines = [ln for ln in files[0].read_text().splitlines() if ln.strip()]
        assert len(lines) == 1
        record = json.loads(lines[0])["record"]
        assert record["message"] == "order rejected"
        assert record["level"]["name"] == "ERROR"
        assert record["extra"]["symbol"] == "SBIN"

    def test_info_does_not_reach_the_error_sink(self, log_dir):
        """The point of the sink is that a bad day is a short file, not a long one."""
        setup_logger()
        logger.info("routine poll")
        logger.warning("recoverable")
        logger.complete()

        files = list((log_dir / "signal_engine" / "logs").glob("errors_*.jsonl"))
        assert [ln for ln in files[0].read_text().splitlines() if ln.strip()] == []

    def test_exception_carries_its_traceback(self, log_dir):
        setup_logger()
        try:
            raise ValueError("margin shortfall")
        except ValueError:
            logger.exception("entry failed")
        logger.complete()

        files = list((log_dir / "signal_engine" / "logs").glob("errors_*.jsonl"))
        payload = json.loads(files[0].read_text().splitlines()[0])
        assert payload["record"]["exception"] is not None
        assert "ValueError" in payload["text"]
        assert "margin shortfall" in payload["text"]

    def test_full_log_still_receives_everything(self, log_dir):
        """No mode has been set (set_mode() not called) - lines land in the "unknown" file,
        the pre-mode-resolution startup window's sink. See TestModeSplitFiles for the
        live/analyze routing itself."""
        setup_logger()
        logger.info("routine poll")
        logger.error("order rejected")
        logger.complete()

        files = list((log_dir / "signal_engine" / "logs").glob("signal_engine_unknown_*.log"))
        assert len(files) == 1
        text = files[0].read_text()
        assert "routine poll" in text
        assert "order rejected" in text


class TestModeSplitFiles:
    """2026-09-10: analyze and live must never share a log file - see set_mode()'s docstring."""

    def test_live_mode_lines_land_in_the_live_file_only(self, log_dir):
        from signal_engine.logger_setup import set_mode

        setup_logger()
        set_mode("live")
        logger.info("live line")
        logger.complete()

        live_files = list((log_dir / "signal_engine" / "logs").glob("signal_engine_live_*.log"))
        analyze_files = list((log_dir / "signal_engine" / "logs").glob("signal_engine_analyze_*.log"))
        assert "live line" in live_files[0].read_text()
        assert analyze_files[0].read_text() == ""

    def test_analyze_mode_lines_land_in_the_analyze_file_only(self, log_dir):
        from signal_engine.logger_setup import set_mode

        setup_logger()
        set_mode("analyze")
        logger.info("analyze line")
        logger.complete()

        live_files = list((log_dir / "signal_engine" / "logs").glob("signal_engine_live_*.log"))
        analyze_files = list((log_dir / "signal_engine" / "logs").glob("signal_engine_analyze_*.log"))
        assert "analyze line" in analyze_files[0].read_text()
        assert live_files[0].read_text() == ""

    def test_switching_mode_mid_session_routes_subsequent_lines_only(self, log_dir):
        from signal_engine.logger_setup import set_mode

        setup_logger()
        set_mode("analyze")
        logger.info("before switch")
        set_mode("live")
        logger.info("after switch")
        logger.complete()

        live_text = list((log_dir / "signal_engine" / "logs").glob("signal_engine_live_*.log"))[0].read_text()
        analyze_text = list((log_dir / "signal_engine" / "logs").glob("signal_engine_analyze_*.log"))[0].read_text()
        assert "before switch" in analyze_text and "after switch" not in analyze_text
        assert "after switch" in live_text and "before switch" not in live_text
