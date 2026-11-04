"""BUG-018: a read of the Historify store waits for the app's lock instead of crashing.

2026-11-04 15:36: the OpenAlgo app held db/historify.duckdb, the backfill's first read raised
"Could not set lock", the daily refresh and the verify died on it, and 211 symbols stayed a session
behind. Two writers cannot be tested at once here, so the connection call is replaced.
"""

import duckdb
import pytest

from signal_engine.backtest import backfill


def _locked():
    return duckdb.IOException(
        'IO Error: Could not set lock on file "db/historify.duckdb": Conflicting lock is held'
    )


def test_a_lock_that_clears_is_waited_out(monkeypatch):
    calls = []

    def connect(path, read_only):
        calls.append(path)
        if len(calls) < 3:
            raise _locked()
        return "connection"

    monkeypatch.setattr(duckdb, "connect", connect)
    monkeypatch.setattr(backfill.time, "sleep", lambda s: None)
    assert backfill._connect_read_only("x.duckdb", wait=60, poll=1) == "connection"
    assert len(calls) == 3


def test_a_lock_that_never_clears_is_raised_after_the_wait(monkeypatch):
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(backfill.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(backfill.time, "sleep", lambda s: None)
    monkeypatch.setattr(duckdb, "connect", lambda path, read_only: (_ for _ in ()).throw(_locked()))
    with pytest.raises(duckdb.IOException, match="lock"):
        backfill._connect_read_only("x.duckdb", wait=30, poll=1)


def test_any_other_error_is_raised_at_once(monkeypatch):
    calls = []

    def connect(path, read_only):
        calls.append(1)
        raise duckdb.IOException("IO Error: Cannot open file: No such file")

    monkeypatch.setattr(duckdb, "connect", connect)
    with pytest.raises(duckdb.IOException, match="Cannot open"):
        backfill._connect_read_only("missing.duckdb", wait=60, poll=1)
    assert len(calls) == 1


def test_every_read_in_backfill_goes_through_the_waiting_helper():
    import inspect

    source = inspect.getsource(backfill)
    body = source.replace("return duckdb.connect(db_path, read_only=True)", "")
    assert "duckdb.connect(" not in body
