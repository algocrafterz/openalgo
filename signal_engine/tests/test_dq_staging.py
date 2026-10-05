"""Staging fetch: broker bars land in a separate DuckDB, never in the landing store."""
from __future__ import annotations

import duckdb
import pandas as pd
import pytest

from signal_engine.backtest.data_readiness import staging
from signal_engine.tests.dq_fixtures import ts


def _bars(days, hhmm="05:30", close=100.0):
    return [{"timestamp": ts(d, hhmm), "open": close, "high": close + 1, "low": close - 1,
             "close": close, "volume": 1000} for d in days]


class FakeBroker:
    """Answers requests from a dict of symbol -> rows; records every request."""

    def __init__(self, data, fail=()):
        self.data, self.fail, self.calls = data, set(fail), []

    def __call__(self, symbol, exchange, interval, start, end):
        self.calls.append((symbol, start, end))
        if symbol in self.fail:
            return False, {"status": "error", "message": "boom"}, 500
        lo = pd.Timestamp(start, tz="Asia/Kolkata")
        hi = pd.Timestamp(end, tz="Asia/Kolkata") + pd.Timedelta(days=1)
        rows = [r for r in self.data.get(symbol, [])
                if lo.timestamp() <= r["timestamp"] < hi.timestamp()]
        return True, {"status": "success", "data": rows}, 200


def _stage(tmp_path, broker, symbols, start="2024-01-01", end="2024-01-10", **kw):
    con = staging.open_staging(tmp_path / "stage.duckdb")
    out = staging.fetch_into_staging(con, symbols, start, end, interval="D", exchange="NSE",
                                     source="fake", fetch=broker, **kw)
    return con, out


def test_refuses_the_landing_store():
    with pytest.raises(staging.StagingError):
        staging.open_staging(staging.LANDING_STORE)
    with pytest.raises(staging.StagingError):
        staging.open_staging(staging.LANDING_STORE.resolve())


def test_rows_land_with_the_landing_schema(tmp_path):
    broker = FakeBroker({"AAA": _bars(["2024-01-02", "2024-01-03"])})
    con, out = _stage(tmp_path, broker, ["AAA"])
    df = con.execute("select * from market_data order by timestamp").df()
    assert list(df.columns) == ["symbol", "exchange", "interval", "timestamp", "open", "high",
                                "low", "close", "volume", "oi", "created_at"]
    assert len(df) == 2 and set(df.symbol) == {"AAA"} and set(df.interval) == {"D"}
    assert df.created_at.notna().all()
    assert out[0].rows == 2 and out[0].chunks_ok == 1 and out[0].chunks_failed == 0


def test_prices_are_stored_exactly_as_received(tmp_path):
    broker = FakeBroker({"AAA": _bars(["2024-01-02"], close=123.456)})
    con, _ = _stage(tmp_path, broker, ["AAA"])
    assert con.execute("select close from market_data").fetchone()[0] == 123.456


def test_requests_stay_below_the_broker_chunk(tmp_path):
    broker = FakeBroker({"AAA": _bars(["2024-01-02"])})
    _stage(tmp_path, broker, ["AAA"], start="2015-01-01", end="2024-01-10")
    spans = [(pd.Timestamp(e) - pd.Timestamp(s)).days + 1 for _, s, e in broker.calls]
    assert len(spans) > 1 and max(spans) <= staging.SAFE_DAYS["D"]


def test_failed_chunk_is_logged_not_swallowed(tmp_path):
    broker = FakeBroker({"AAA": _bars(["2024-01-02"])}, fail={"BBB"})
    con, out = _stage(tmp_path, broker, ["AAA", "BBB"])
    by = {s.symbol: s for s in out}
    assert by["BBB"].chunks_failed == 1 and by["BBB"].errors
    log = con.execute("select symbol, status from fetch_log order by symbol").fetchall()
    assert ("BBB", "error") in log and ("AAA", "ok") in log


def test_duplicate_timestamps_reject_the_chunk(tmp_path):
    rows = _bars(["2024-01-02"]) * 2
    con, out = _stage(tmp_path, FakeBroker({"AAA": rows}), ["AAA"])
    assert con.execute("select count(*) from market_data").fetchone()[0] == 0
    assert out[0].chunks_failed == 1
    assert "duplicate" in con.execute("select detail from fetch_log").fetchone()[0]


def test_missing_price_column_rejects_the_chunk(tmp_path):
    rows = [{k: v for k, v in r.items() if k != "close"} for r in _bars(["2024-01-02"])]
    _, out = _stage(tmp_path, FakeBroker({"AAA": rows}), ["AAA"])
    assert out[0].chunks_failed == 1


def test_three_empty_symbols_in_a_row_stop_the_fetch(tmp_path):
    with pytest.raises(staging.StagingError, match="dead"):
        _stage(tmp_path, FakeBroker({}), ["A", "B", "C", "D"])


def test_resume_skips_completed_symbols(tmp_path):
    broker = FakeBroker({"AAA": _bars(["2024-01-02"]), "BBB": _bars(["2024-01-02"])})
    con = staging.open_staging(tmp_path / "stage.duckdb")
    kw = {"interval": "D", "exchange": "NSE", "source": "fake", "fetch": broker}
    staging.fetch_into_staging(con, ["AAA"], "2024-01-01", "2024-01-10", **kw)
    broker.calls.clear()
    staging.fetch_into_staging(con, ["AAA", "BBB"], "2024-01-01", "2024-01-10", **kw)
    assert [c[0] for c in broker.calls] == ["BBB"]
    assert con.execute("select count(*) from market_data").fetchone()[0] == 2


def test_symbol_with_a_failed_chunk_is_not_marked_complete(tmp_path):
    broker = FakeBroker({"AAA": _bars(["2024-01-02"])}, fail={"AAA"})
    con = staging.open_staging(tmp_path / "stage.duckdb")
    kw = {"interval": "D", "exchange": "NSE", "source": "fake", "fetch": broker}
    staging.fetch_into_staging(con, ["AAA", "OK1"], "2024-01-01", "2024-01-10", **kw)
    broker.fail.clear()
    broker.calls.clear()
    staging.fetch_into_staging(con, ["AAA"], "2024-01-01", "2024-01-10", **kw)
    assert [c[0] for c in broker.calls] == ["AAA"]


def test_meta_records_source_and_range(tmp_path):
    con, _ = _stage(tmp_path, FakeBroker({"AAA": _bars(["2024-01-02"])}), ["AAA"])
    meta = dict(con.execute("select key, value from stage_meta").fetchall())
    assert meta["source"] == "fake" and meta["interval"] == "D"
    assert meta["requested_start"] == "2024-01-01" and meta["requested_end"] == "2024-01-10"
    assert "fetched_at_ist" in meta


def test_staging_file_is_a_plain_duckdb(tmp_path):
    con, _ = _stage(tmp_path, FakeBroker({"AAA": _bars(["2024-01-02"])}), ["AAA"])
    con.close()
    ro = duckdb.connect(str(tmp_path / "stage.duckdb"), read_only=True)
    assert ro.execute("select count(*) from market_data").fetchone()[0] == 1
    ro.close()


# ---- review findings (2026-10-04) ----------------------------------------------------------

def test_landing_store_refused_from_another_working_directory(tmp_path, monkeypatch):
    real = staging.LANDING_STORE
    monkeypatch.chdir(tmp_path)
    import os
    with pytest.raises(staging.StagingError):
        staging.open_staging(os.path.relpath(real, tmp_path))


def test_existing_non_staging_duckdb_is_refused(tmp_path):
    other = tmp_path / "other.duckdb"
    duckdb.connect(str(other)).close()
    with pytest.raises(staging.StagingError, match="not a staging file"):
        staging.open_staging(other)


def test_symbol_with_only_empty_chunks_is_not_complete(tmp_path):
    broker = FakeBroker({"AAA": _bars(["2024-01-02"])})
    con = staging.open_staging(tmp_path / "stage.duckdb")
    kw = {"interval": "D", "exchange": "NSE", "source": "fake", "fetch": broker}
    staging.fetch_into_staging(con, ["EMPTY", "AAA"], "2024-01-01", "2024-01-10", **kw)
    broker.calls.clear()
    staging.fetch_into_staging(con, ["EMPTY", "AAA"], "2024-01-01", "2024-01-10", **kw)
    assert [c[0] for c in broker.calls] == ["EMPTY"]


def test_resume_with_a_different_range_is_refused(tmp_path):
    broker = FakeBroker({"AAA": _bars(["2024-01-02"])})
    con = staging.open_staging(tmp_path / "stage.duckdb")
    kw = {"interval": "D", "exchange": "NSE", "source": "fake", "fetch": broker}
    staging.fetch_into_staging(con, ["AAA"], "2024-01-01", "2024-01-10", **kw)
    with pytest.raises(staging.StagingError, match="range"):
        staging.fetch_into_staging(con, ["AAA"], "2023-01-01", "2024-01-10", **kw)


def test_fractional_volume_rejects_the_chunk(tmp_path):
    rows = _bars(["2024-01-02"])
    rows[0]["volume"] = 10.5
    con, out = _stage(tmp_path, FakeBroker({"AAA": rows}), ["AAA"])
    assert out[0].chunks_failed == 1
    assert con.execute("select count(*) from market_data").fetchone()[0] == 0
