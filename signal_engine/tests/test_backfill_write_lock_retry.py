"""BUG-030: a chunk write that hits the DuckDB file lock is retried, not failed.

2026-11-17 15:41:55: database.historify_db.get_connection gave up after 3 tries over ~1.5 s with
duckdb.IOException 'Could not set lock on file ... Conflicting lock is held in ...' (re-raised
unchanged by upsert_market_data), so COALINDIA's chunk failed. The 16:01 retry recovered.
"""

import duckdb
import pytest

from signal_engine.backtest import backfill as bf

LOCK_TEXT = ('IO Error: Could not set lock on file "db/historify.duckdb": '
             'Conflicting lock is held in /usr/bin/python3.12 (PID 1234)')


def _lock():
    return duckdb.IOException(LOCK_TEXT)


@pytest.fixture
def sleeps(monkeypatch):
    rec = []
    monkeypatch.setattr(bf, "_sleep", rec.append, raising=False)
    return rec


def _writer(errors):
    calls = []

    def write(df, sym, exchange, interval):
        calls.append(sym)
        if len(calls) <= len(errors):
            raise errors[len(calls) - 1]
        return 42

    return write, calls


def test_lock_twice_then_success(sleeps):
    write, calls = _writer([_lock(), _lock()])
    assert bf._write_with_lock_retry(write, "df", "COALINDIA", "NSE", "1m") == 42
    assert len(calls) == 3
    assert sleeps == [2, 4]


def test_persistent_lock_raises_after_five_attempts(sleeps):
    err = _lock()
    write, calls = _writer([err] * 9)
    with pytest.raises(duckdb.IOException) as ei:
        bf._write_with_lock_retry(write, "df", "COALINDIA", "NSE", "1m")
    assert ei.value is err
    assert len(calls) == 5
    assert sleeps == [2, 4, 8, 16]


def test_other_error_is_not_retried(sleeps):
    write, calls = _writer([duckdb.IOException("IO Error: disk full")])
    with pytest.raises(duckdb.IOException, match="disk full"):
        bf._write_with_lock_retry(write, "df", "COALINDIA", "NSE", "1m")
    assert len(calls) == 1
    assert sleeps == []


def test_non_duckdb_error_is_not_retried(sleeps):
    write, calls = _writer([ValueError("Could not set lock")])
    with pytest.raises(ValueError):
        bf._write_with_lock_retry(write, "df", "COALINDIA", "NSE", "1m")
    assert len(calls) == 1 and sleeps == []


def test_backfill_chunk_survives_a_transient_lock(monkeypatch, sleeps):
    import database.historify_db as hdb
    import services.history_service as hs

    write, calls = _writer([_lock()])
    row = {"timestamp": 1700000000, "open": 1.0, "high": 2.0, "low": 1.0, "close": 1.5,
           "volume": 10, "oi": 0}
    monkeypatch.setattr(hs, "get_history_with_auth", lambda *a, **k: (True, {"data": [row]}, 200))
    monkeypatch.setattr(hdb, "upsert_market_data", write)
    monkeypatch.setattr(bf.time, "sleep", lambda s: None)
    res = bf.backfill(["COALINDIA"], "2026-11-15", "2026-11-17", session=("k", None, "b"))
    assert res[0].chunks_ok == 1 and res[0].chunks_failed == 0 and res[0].stored == 42
    assert len(calls) == 2 and sleeps == [2]
