"""Frozen legacy 1m snapshot: scope, hashes, verification (INTRADAY-5M-DATASET-PLAN.md s2)."""

import os
import stat
from datetime import date

import duckdb
import pandas as pd
import pytest

from signal_engine.backtest.data_readiness import intraday_freeze as fz


def _store(path, rows):
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE market_data (symbol VARCHAR, exchange VARCHAR, interval VARCHAR, "
                "timestamp BIGINT, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
                "volume BIGINT, oi BIGINT, created_at TIMESTAMP)")
    con.executemany("INSERT INTO market_data VALUES (?, ?, ?, ?, 1, 2, 0.5, 1.5, 10, 0, "
                    "TIMESTAMP '2026-01-01 00:00:00')", rows)
    con.close()


def _nse(path):
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE nse_daily_raw (symbol VARCHAR, session DATE, series VARCHAR, "
                "last DOUBLE)")
    con.executemany("INSERT INTO nse_daily_raw VALUES (?, ?, ?, ?)", [
        ("AAA", date(2017, 1, 2), "EQ", 1.5), ("AAA", date(2017, 1, 2), "BL", 9.0),
        ("ZZZ", date(2017, 1, 2), "EQ", 3.0)])
    con.close()


@pytest.fixture
def stores(tmp_path):
    t17 = fz._epoch(date(2017, 1, 2)) + 9 * 3600 + 15 * 60
    t18 = fz._epoch(date(2018, 1, 1)) + 9 * 3600 + 15 * 60
    late = fz._epoch(date(2026, 10, 2)) + 9 * 3600 + 15 * 60
    _store(tmp_path / "live.duckdb", [
        ("AAA", "NSE", "1m", t17), ("AAA", "NSE", "1m", t17 + 60), ("AAA", "NSE", "1m", t18),
        ("AAA", "NSE", "5m", t17), ("AAA", "BSE", "1m", t17), ("AAA", "NSE", "1m", late),
        ("ZZZ", "NSE", "1m", t17)])
    _nse(tmp_path / "nse.duckdb")
    return tmp_path


def _freeze(tmp_path):
    return fz.freeze(tmp_path / "live.duckdb", tmp_path / "out", ["AAA"], tmp_path / "nse.duckdb",
                     "nse-daily-test")


def test_copies_only_the_scope_into_per_year_files(stores):
    folder = _freeze(stores)
    m = fz.load(folder)
    assert m["row_count"] == 3 and m["symbol_count"] == 1
    assert sorted(f["path"] for f in m["files"]) == [
        "bars/AAA/2017.parquet", "bars/AAA/2018.parquet", "reference/nse_last.parquet"]
    assert len(pd.read_parquet(folder / "bars/AAA/2017.parquet")) == 2
    ref = pd.read_parquet(folder / "reference/nse_last.parquet")
    assert ref.symbol.tolist() == ["AAA"] and ref["last"].tolist() == [1.5]
    assert m["source_id"] == fz.SOURCE_ID and m["root_hash"] == fz.root_hash(m["files"])


def test_files_are_read_only_and_verify(stores):
    folder = _freeze(stores)
    assert fz.verify(folder) == []
    mode = os.stat(folder / "bars/AAA/2017.parquet").st_mode
    assert not mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)


def test_a_changed_file_fails_verification(stores):
    folder = _freeze(stores)
    target = folder / "bars/AAA/2018.parquet"
    os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    target.write_bytes(b"tampered")
    assert fz.verify(folder) == ["bars/AAA/2018.parquet hash mismatch"]


def test_root_hash_does_not_depend_on_file_order():
    a = {"path": "a", "sha256": "1", "rows": 1}
    b = {"path": "b", "sha256": "2", "rows": 2}
    assert fz.root_hash([a, b]) == fz.root_hash([b, a])
    assert fz.root_hash([a, b]) != fz.root_hash([a, {**b, "rows": 3}])


def test_no_symbols_is_refused(stores):
    with pytest.raises(fz.FreezeError):
        fz.freeze(stores / "live.duckdb", stores / "out", [], stores / "nse.duckdb", "x")
