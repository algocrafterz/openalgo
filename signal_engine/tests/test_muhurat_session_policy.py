"""Special (Muhurat) sessions are not used as trading history, in backtests or live.

A Muhurat session is a short ceremonial session: on 2025-10-21 the store holds a median 72
one-minute bars per symbol (375 on a regular day) and about 22% of a regular day's volume. Its
value area, volume baseline or previous-day levels are not comparable with a regular session's,
so the policy (approved 2026-10-23) is: drop special-session bars where the shared loader cleans
sessions (`data._clean_session`, used by backtests and by the runner's warm-up alike), and let
the runner's "previous session" be the last REGULAR session. The bars stay in the store.
"""

import datetime as dt

import duckdb
import pandas as pd
import pytest

from signal_engine.backtest import data
from signal_engine.backtest.data_collect import DataCollector
from signal_engine.features.session import drop_special_sessions, special_sessions
from signal_engine.features.value_area import prev_session_value_area
from signal_engine.pipeline.context import HistorifyLoader

IST = "Asia/Kolkata"
D = dt.date


def _bars(day: str, level: float, start="09:15", end="15:29") -> pd.DataFrame:
    idx = pd.date_range(f"{day} {start}", f"{day} {end}", freq="1min", tz=IST)
    return pd.DataFrame(
        {"Open": level, "High": level + 0.5, "Low": level - 0.5, "Close": level, "Volume": 100.0},
        index=idx,
    )


@pytest.fixture
def store(tmp_path):
    """A real Historify-shaped DuckDB store; put() writes 1m bars for one symbol."""
    path = str(tmp_path / "h.duckdb")
    con = duckdb.connect(path)
    con.execute("CREATE TABLE market_data (symbol VARCHAR, exchange VARCHAR, interval VARCHAR, "
                "timestamp BIGINT, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT)")
    con.close()

    def put(symbol, frames):
        rows = []
        for f in frames:
            for ts, r in f.iterrows():
                rows.append((symbol, "NSE", "1m", int(ts.timestamp()), r.Open, r.High, r.Low, r.Close, int(r.Volume)))
        c = duckdb.connect(path)
        c.executemany("INSERT INTO market_data VALUES (?,?,?,?,?,?,?,?,?)", rows)
        c.close()

    return path, put


def _muhurat_2025():
    return _bars("2025-10-21", 200.0, start="13:45", end="14:44")  # a different price on purpose


class TestTheSharedHelper:
    def test_the_calendar_lists_the_muhurat_sessions(self):
        assert {D(2025, 10, 21), D(2026, 11, 8)} <= special_sessions()

    def test_only_special_session_bars_are_dropped(self):
        df = pd.concat([_bars("2025-10-20", 100.0), _muhurat_2025(), _bars("2025-10-23", 101.0)])
        out = drop_special_sessions(df)
        assert sorted(set(out.index.date)) == [D(2025, 10, 20), D(2025, 10, 23)]
        assert len(out) == 2 * 375


class TestTheSharedLoader:
    def test_clean_session_drops_muhurat_bars_and_counts_them(self):
        df = pd.concat([_bars("2025-10-20", 100.0), _muhurat_2025()])
        collector = DataCollector()
        out = data._clean_session(df, collector, "SBIN")
        assert set(out.index.date) == {D(2025, 10, 20)}
        assert collector.clean_dropped["SBIN"]["outside_hours"] == 60

    def test_from_historify_returns_no_muhurat_session(self, store):
        path, put = store
        put("SBIN", [_bars("2025-10-17", 99.0), _bars("2025-10-20", 100.0), _muhurat_2025(), _bars("2025-10-23", 101.0)])
        for interval in ("1m", "5m"):
            frames = data.from_historify(["SBIN"], interval=interval, min_sessions=1, db_path=path)
            assert D(2025, 10, 21) not in set(frames["SBIN"].index.date)

    def test_the_backtest_reads_the_last_regular_session_as_previous(self, store):
        path, put = store
        put("SBIN", [_bars("2025-10-20", 100.0), _muhurat_2025(), _bars("2025-10-23", 101.0)])
        one = data.from_historify(["SBIN"], interval="1m", min_sessions=1, db_path=path)["SBIN"]
        day = one[pd.Index(one.index.date) == D(2025, 10, 23)]
        pva = prev_session_value_area(one, day.index, tick=0.05)
        assert abs(pva["ppoc"].iloc[-1] - 100.0) < 1.0  # 10-20's level, not the Muhurat's 200


class TestTheRecordedBacktestPath:
    """Recorded runs read a published dataset version through intraday_access.read_bars, not
    from_historify; the same policy must hold there, without reporting a false data gap."""

    def test_a_published_muhurat_session_is_not_loaded_or_reported_missing(self, monkeypatch):
        from signal_engine.backtest.data_readiness import intraday_access as ia
        from signal_engine.tests.test_intraday_contract_enforcement import (
            FULL,
            _con,
            _contract,
            _quality,
            _rows,
        )

        days = [D(2025, 10, 20), D(2025, 10, 21), D(2025, 10, 23)]
        monkeypatch.setattr(ia, "load_quality", lambda m, root=None: _quality("AAA", days))
        rows = [r for d in days for r in _rows("AAA", d, FULL)]  # the Muhurat even passes the contract
        collector = DataCollector()
        out = ia.read_bars(_con(rows), {"dataset_version": "v", "frequency": "5m"}, _contract(),
                           ["AAA"], "5m", None, collector=collector)["AAA"]
        assert sorted(set(out.index.date)) == [D(2025, 10, 20), D(2025, 10, 23)]
        assert D(2025, 10, 21) not in collector.empty_sessions


class TestTheLiveWarmUp:
    def test_the_day_after_a_weekday_muhurat_and_a_holiday_reads_the_last_regular_session(self, store):
        # 2025-10-21 Muhurat (a holiday with a short session), 2025-10-22 holiday.
        path, put = store
        put("SBIN", [_bars("2025-10-17", 99.0), _bars("2025-10-20", 100.0), _muhurat_2025()])
        loader = HistorifyLoader(db_path=path)
        h5, h1, failed = loader.load(["SBIN"], D(2025, 10, 23), 2, 2)
        assert failed == [], loader.rejected
        assert max(h1["SBIN"].index.date) == D(2025, 10, 20)
        assert D(2025, 10, 21) not in set(h5["SBIN"].index.date)

    def test_the_monday_after_a_sunday_muhurat_reads_friday(self, store):
        path, put = store
        put("SBIN", [_bars("2026-11-05", 99.0), _bars("2026-11-06", 100.0),
                     _bars("2026-11-08", 200.0, start="13:45", end="14:44")])
        loader = HistorifyLoader(db_path=path)
        _h5, h1, failed = loader.load(["SBIN"], D(2026, 11, 9), 2, 2)
        assert failed == [], loader.rejected
        assert max(h1["SBIN"].index.date) == D(2026, 11, 6)

    def test_the_monday_after_a_sunday_muhurat_without_friday_is_stale(self, store):
        path, put = store
        put("SBIN", [_bars("2026-11-05", 99.0), _bars("2026-11-08", 200.0, start="13:45", end="14:44")])
        loader = HistorifyLoader(db_path=path)
        _h5, _h1, failed = loader.load(["SBIN"], D(2026, 11, 9), 2, 2)
        assert failed == ["SBIN"]
        assert "previous session 2026-11-06" in loader.rejected["SBIN"]

    def test_a_regular_monday_reads_friday(self, store):
        path, put = store
        put("SBIN", [_bars("2026-10-15", 99.0), _bars("2026-10-16", 100.0)])
        assert HistorifyLoader(db_path=path).load(["SBIN"], D(2026, 10, 19), 2, 2)[2] == []

    def test_the_day_after_a_regular_holiday_reads_the_session_before_it(self, store):
        path, put = store
        put("SBIN", [_bars("2026-10-16", 99.0), _bars("2026-10-19", 100.0)])  # 2026-10-20 holiday
        assert HistorifyLoader(db_path=path).load(["SBIN"], D(2026, 10, 21), 2, 2)[2] == []
