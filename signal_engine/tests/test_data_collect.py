"""DataCollector: counts reported by data.py without changing any returned data."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pandas.testing as pdt

from signal_engine.backtest import data
from signal_engine.backtest.data_collect import DataCollector

IST = "Asia/Kolkata"


def _frame(stamps: list[str], mods: dict[int, dict] | None = None) -> pd.DataFrame:
    idx = pd.DatetimeIndex(pd.to_datetime(stamps)).tz_localize(IST)
    n = len(idx)
    px = 100.0 + np.arange(n) * 0.1
    df = pd.DataFrame(
        {"Open": px, "High": px + 0.5, "Low": px - 0.5, "Close": px + 0.1, "Volume": 10},
        index=idx,
    )
    for i, cols in (mods or {}).items():
        for k, v in cols.items():
            df.iloc[i, df.columns.get_loc(k)] = v
    return df


def _crafted() -> pd.DataFrame:
    stamps = [
        "2025-01-02 03:00",  # outside
        "2025-01-02 09:00",  # outside
        "2025-01-02 09:15",
        "2025-01-02 09:16",
        "2025-01-02 09:16",  # duplicate
        "2025-01-02 09:17",  # impossible (high below low)
        "2025-01-02 09:18",
        "2025-01-02 09:18",  # duplicate
        "2025-01-02 09:19",  # impossible (low zero)
        "2025-01-02 15:29",
        "2025-01-02 15:30",  # outside
        "2025-01-02 15:45",  # outside
    ]
    return _frame(stamps, {5: {"High": 1.0, "Low": 500.0}, 8: {"Low": 0.0}})


def _independent_counts(df: pd.DataFrame) -> dict:
    seen, dup = set(), 0
    kept = []
    for ts, row in df.iterrows():
        if ts in seen:
            dup += 1
            continue
        seen.add(ts)
        kept.append((ts, row))
    out = imp = 0
    for ts, r in kept:
        hhmm = ts.strftime("%H:%M")
        if hhmm < "09:15" or hhmm > "15:29":
            out += 1
            continue
        o, h, lo, c = r["Open"], r["High"], r["Low"], r["Close"]
        if not (h >= lo and h >= o and h >= c and lo <= o and lo <= c and lo > 0):
            imp += 1
    return {"duplicate_ts": dup, "outside_hours": out, "impossible": imp}


def test_clean_session_counts_are_exact():
    df = _crafted()
    col = DataCollector()
    data._clean_session(df, collector=col, symbol="AAA")
    assert col.clean_dropped["AAA"] == _independent_counts(df)
    assert col.clean_dropped["AAA"] == {"duplicate_ts": 2, "outside_hours": 4, "impossible": 2}


def test_clean_session_parity_with_and_without_collector():
    df = _crafted()
    plain = data._clean_session(df.copy())
    withc = data._clean_session(df.copy(), collector=DataCollector(), symbol="AAA")
    pdt.assert_frame_equal(plain, withc)
    assert plain.to_json() == withc.to_json()


def test_clean_session_without_symbol_does_not_record():
    col = DataCollector()
    data._clean_session(_crafted(), collector=col)
    assert col.clean_dropped == {}


def test_clean_session_clean_input_records_nothing():
    df = _frame(["2025-01-02 09:15", "2025-01-02 09:16"])
    col = DataCollector()
    data._clean_session(df, collector=col, symbol="AAA")
    assert col.clean_dropped == {}


def test_counts_accumulate_across_calls():
    col = DataCollector()
    data._clean_session(_crafted(), collector=col, symbol="AAA")
    data._clean_session(_crafted(), collector=col, symbol="AAA")
    assert col.clean_dropped["AAA"]["duplicate_ts"] == 4


def test_to_dict_is_json_safe_sorted_and_deterministic():
    col = DataCollector(interval="5m", symbols_loaded=2)
    col.record_clean("ZZZ", duplicate_ts=1)
    col.record_clean("AAA", impossible=3)
    col.record_split_days("BBB", [dt.date(2025, 3, 2), dt.date(2025, 1, 5)])
    col.record_min_sessions("QQQ", 7)
    d = col.to_dict()
    assert list(d["clean_dropped"]) == ["AAA", "ZZZ"]
    assert d["clean_dropped"]["AAA"] == {"duplicate_ts": 0, "outside_hours": 0, "impossible": 3}
    assert d["split_days_dropped"] == {"BBB": ["2025-01-05", "2025-03-02"]}
    assert d["symbols_dropped_min_sessions"] == {"QQQ": 7}
    assert d["symbols_loaded"] == 2 and d["interval"] == "5m"
    assert json.dumps(d, sort_keys=True) == json.dumps(col.to_dict(), sort_keys=True)


def test_merge_sums_and_does_not_mutate():
    a = DataCollector(interval="5m", symbols_loaded=1)
    a.record_clean("AAA", duplicate_ts=1)
    b = DataCollector(interval="5m", symbols_loaded=2)
    b.record_clean("AAA", duplicate_ts=2, impossible=1)
    b.record_split_days("BBB", [dt.date(2025, 1, 5)])
    m = a.merge(b)
    assert m.clean_dropped["AAA"] == {"duplicate_ts": 3, "outside_hours": 0, "impossible": 1}
    assert m.symbols_loaded == 3 and m.split_days_dropped == {"BBB": [dt.date(2025, 1, 5)]}
    assert a.clean_dropped["AAA"]["duplicate_ts"] == 1 and a.symbols_loaded == 1


# ---------------------------------------------------------------- historify

DAY_EPOCH = int(pd.Timestamp("2025-01-01 09:15", tz=IST).timestamp())


def _make_db(path: Path, split: bool) -> None:
    con = duckdb.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE market_data (symbol VARCHAR, interval VARCHAR, timestamp BIGINT, "
            "open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT)"
        )
        rows = []
        for sym in ("AAA", "BBB", "SHORT"):
            ndays = 2 if sym == "SHORT" else 4
            for d in range(ndays):
                base = DAY_EPOCH + d * 86400
                px = 100.0
                if sym == "BBB" and split and d >= 2:
                    px = 20.0  # 1:5 split before day index 2
                for m in range(10):
                    rows.append((sym, "1m", base + m * 60, px, px + 1, px - 1, px, 5))
                if sym == "AAA" and d == 0:
                    rows.append((sym, "1m", base, px, px + 1, px - 1, px, 5))  # duplicate
                    rows.append((sym, "1m", base - 3600, px, px + 1, px - 1, px, 5))  # outside
                    rows.append((sym, "1m", base + 11 * 60, px, px - 5, px - 1, px, 5))  # bad
        for d in range(6):
            ts = DAY_EPOCH + d * 86400
            px = 100.0 if d < 3 else 20.0
            rows.append(("DDD", "D", ts, px, px + 1, px - 1, px, 5))
            rows.append(("DDD", "D", ts, px, px + 1, px - 1, px, 5))  # duplicate
        rows.append(("DDD", "D", DAY_EPOCH + 9 * 86400, 100.0, 90.0, 95.0, 100.0, 5))  # bad
        con.executemany("INSERT INTO market_data VALUES (?,?,?,?,?,?,?,?)", rows)
    finally:
        con.close()


def test_from_historify_collector_parity_and_counts(tmp_path):
    db = tmp_path / "h.duckdb"
    _make_db(db, split=True)
    kw = {"symbols": ["AAA", "BBB", "SHORT"], "interval": "1m", "min_sessions": 4, "db_path": db}
    plain = data.from_historify(**kw)
    col = DataCollector()
    withc = data.from_historify(**kw, collector=col)
    assert plain.keys() == withc.keys() == {"AAA"}
    for s in plain:
        pdt.assert_frame_equal(plain[s], withc[s])
        assert plain[s].to_json() == withc[s].to_json()
    assert col.clean_dropped["AAA"] == {"duplicate_ts": 1, "outside_hours": 1, "impossible": 1}
    assert col.split_days_dropped == {"BBB": [dt.date(2025, 1, 3)]}
    # BBB loses its split day, leaving 3 sessions; SHORT has 2; both vs min_sessions=4
    assert col.symbols_dropped_min_sessions == {"BBB": 3, "SHORT": 2}
    assert col.symbols_loaded == 1
    assert col.interval == "1m"


def test_from_historify_split_days_not_recorded_when_not_dropping(tmp_path):
    db = tmp_path / "h.duckdb"
    _make_db(db, split=True)
    col = DataCollector()
    data.from_historify(
        symbols=["AAA", "BBB"],
        interval="1m",
        min_sessions=1,
        db_path=db,
        drop_split_days=False,
        collector=col,
    )
    assert col.split_days_dropped == {}


def test_from_historify_daily_collector_parity_and_counts(tmp_path):
    db = tmp_path / "h.duckdb"
    _make_db(db, split=True)
    kw = {"symbols": ["DDD"], "min_sessions": 1, "db_path": db}
    plain = data.from_historify_daily(**kw)
    col = DataCollector()
    withc = data.from_historify_daily(**kw, collector=col)
    pdt.assert_frame_equal(plain["DDD"], withc["DDD"])
    assert col.clean_dropped["DDD"] == {"duplicate_ts": 6, "outside_hours": 0, "impossible": 1}
    assert col.symbols_loaded == 1 and col.interval == "1d"


def test_from_historify_daily_min_sessions_drop_recorded(tmp_path):
    db = tmp_path / "h.duckdb"
    _make_db(db, split=True)
    col = DataCollector()
    try:
        data.from_historify_daily(symbols=["DDD"], min_sessions=50, db_path=db, collector=col)
    except RuntimeError:
        pass
    assert col.symbols_dropped_min_sessions == {"DDD": 6}


def test_rows_seen_recorded_before_cleaning_and_accumulates():
    col = DataCollector()
    data._clean_session(_crafted(), collector=col, symbol="AAA")
    assert col.rows_seen == {"AAA": 12}
    data._clean_session(_frame(["2025-01-02 09:15"]), collector=col, symbol="AAA")
    assert col.rows_seen == {"AAA": 13}
    assert col.to_dict()["rows_seen"] == {"AAA": 13}
    other = DataCollector()
    other.record_seen("BBB", 4)
    assert col.merge(other).rows_seen == {"AAA": 13, "BBB": 4}


def test_from_historify_rows_seen_is_raw_row_count(tmp_path):
    db = tmp_path / "h.duckdb"
    _make_db(db, split=False)
    col = DataCollector()
    data.from_historify(
        symbols=["AAA", "SHORT"], interval="5m", min_sessions=1, db_path=db, collector=col
    )
    assert col.rows_seen == {"AAA": 4 * 10 + 3, "SHORT": 2 * 10}
