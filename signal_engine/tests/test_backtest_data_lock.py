"""BUG-028: a transient DuckDB file lock must not fail the read-only historify loaders."""

import duckdb
import pytest

from signal_engine.backtest import data

# The real error from signal_engine_analyze_2026-11-13.log (10:15:22, EVWAP-EARLY-PY warm-up).
LOCK_MSG = (
    'IO Error: Could not set lock on file "/home/anand/github/openalgo/db/historify.duckdb": '
    "Conflicting lock is held in /usr/bin/python3.12 (PID 1858014). "
    "See also https://duckdb.org/docs/stable/connect/concurrency"
)


class _Con:
    def execute(self, *_a, **_k):
        return self

    def fetchall(self):
        return [("AAA",), ("BBB",)]

    def close(self):
        pass


def _patch(monkeypatch, fail_times, exc=None):
    calls = {"connect": 0, "sleep": []}

    def fake_connect(*_a, **_k):
        calls["connect"] += 1
        if calls["connect"] <= fail_times:
            raise (exc or duckdb.IOException(LOCK_MSG))
        return _Con()

    monkeypatch.setattr(duckdb, "connect", fake_connect)
    monkeypatch.setattr("time.sleep", lambda s: calls["sleep"].append(s))
    return calls


def test_symbols_survive_two_lock_errors(monkeypatch):
    calls = _patch(monkeypatch, 2)
    assert data.historify_symbols("1m", db_path="x.duckdb") == ["AAA", "BBB"]
    assert calls["connect"] == 3
    assert calls["sleep"] == [1.0, 2.0]


def test_persistent_lock_raises_after_bounded_retries(monkeypatch):
    calls = _patch(monkeypatch, 10**6)
    with pytest.raises(duckdb.IOException, match="Could not set lock"):
        data.historify_symbols("1m", db_path="x.duckdb")
    assert calls["connect"] == 5
    assert sum(calls["sleep"]) <= 12


def test_from_historify_retries_lock(monkeypatch):
    calls = _patch(monkeypatch, 10**6)
    with pytest.raises(duckdb.IOException, match="Could not set lock"):
        data.from_historify(["AAA"], db_path="x.duckdb")
    assert calls["connect"] == 5


def test_other_errors_raise_immediately(monkeypatch):
    calls = _patch(monkeypatch, 10**6, exc=duckdb.IOException("IO Error: disk on fire"))
    with pytest.raises(duckdb.IOException, match="disk on fire"):
        data.historify_symbols("1m", db_path="x.duckdb")
    assert calls["connect"] == 1
    assert calls["sleep"] == []
