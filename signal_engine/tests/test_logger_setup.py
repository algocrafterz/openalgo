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
        setup_logger()
        logger.info("routine poll")
        logger.error("order rejected")
        logger.complete()

        files = list((log_dir / "signal_engine" / "logs").glob("signal_engine_*.log"))
        text = files[0].read_text()
        assert "routine poll" in text
        assert "order rejected" in text
