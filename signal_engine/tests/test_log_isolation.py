"""The test suite must not be able to write to, or prune, the operator's real signal_engine/logs.

setup_logger() builds its sinks from a cwd-relative directory. Run from the repo root (as the
suite is), that is the live engine's log directory. conftest.py repoints it at import.
"""

import os
from datetime import date, timedelta

import pytest
from loguru import logger

import signal_engine.logger_setup as logger_setup

REAL_LOGS = os.path.realpath(
    os.path.join(os.path.dirname(os.path.abspath(logger_setup.__file__)), "logs")
)


def _inside_real_logs(path: str) -> bool:
    return os.path.commonpath([os.path.realpath(path), REAL_LOGS]) == REAL_LOGS


def _snapshot_real_logs() -> dict | None:
    """File sizes under the real logs directory, or None when it does not exist.

    The directory is untracked, so a fresh checkout or worktree has none. Absent is a state to
    compare, not an error: writing to the real logs would create it.
    """
    if not os.path.isdir(REAL_LOGS):
        return None
    return {n: os.path.getsize(os.path.join(REAL_LOGS, n)) for n in os.listdir(REAL_LOGS)}


@pytest.fixture
def at_repo_root(monkeypatch):
    """The cwd the suite normally runs from, where the relative path means the real logs."""
    monkeypatch.chdir(os.path.dirname(os.path.dirname(REAL_LOGS)))
    assert os.path.realpath(logger_setup.LOG_DIR) == REAL_LOGS
    yield
    logger.remove()


def test_production_paths_are_unchanged():
    assert logger_setup.LOG_DIR == "signal_engine/logs"
    assert logger_setup._ERROR_LOG_DIR == "signal_engine/logs"


def test_resolved_dirs_are_outside_real_logs(at_repo_root):
    assert not _inside_real_logs(logger_setup._log_dir())
    assert not _inside_real_logs(logger_setup._error_log_dir())


def test_setup_logger_from_repo_root_writes_nothing_to_real_logs(at_repo_root, monkeypatch):
    monkeypatch.setattr(logger_setup, "_current_mode", "unknown")
    before = _snapshot_real_logs()
    logger_setup.setup_logger()
    logger.info("log isolation probe")
    logger.error("log isolation probe error")
    logger.complete()
    after = _snapshot_real_logs()
    assert after == before


def test_error_pruning_cannot_touch_real_logs(at_repo_root, monkeypatch):
    old = date.today() - timedelta(days=logger_setup._ERROR_LOG_RETENTION_DAYS + 5)
    target_dir = logger_setup._error_log_dir()
    os.makedirs(target_dir, exist_ok=True)
    stale = os.path.join(target_dir, f"errors_{old.isoformat()}.jsonl")
    open(stale, "w").close()
    removed = []
    real_remove = os.remove
    monkeypatch.setattr(
        os, "remove", lambda p, *a, **k: (removed.append(os.path.realpath(p)), real_remove(p))[1]
    )
    logger_setup._prune_error_logs()
    assert removed == [os.path.realpath(stale)]
    assert not any(_inside_real_logs(p) for p in removed)


def test_relative_dir_elsewhere_is_left_alone(tmp_path, monkeypatch):
    """A test that chdirs into tmp_path keeps its own relative signal_engine/logs."""
    monkeypatch.chdir(tmp_path)
    assert logger_setup._log_dir() == "signal_engine/logs"
    assert logger_setup._error_log_dir() == "signal_engine/logs"
