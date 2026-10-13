"""Regime inputs: NIFTY daily closes from Historify (broker data) with Yahoo filling gaps.

Historify holds NIFTY only as 1-minute bars and holds no India VIX, so the loader derives
session closes from the minute bars and takes VIX (and any NIFTY days the broker store
lacks, such as the 200-day warm-up before its first bar) from Yahoo. Yahoo is injected so
no test touches the network.
"""

import duckdb
import pandas as pd
import pytest

from signal_engine.backtest import regime_data as rd

IST = "Asia/Kolkata"


def _epoch(day, hm):
    return int(pd.Timestamp(f"{day} {hm}", tz=IST).timestamp())


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "h.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE market_data (symbol VARCHAR, exchange VARCHAR, interval VARCHAR, "
        "timestamp BIGINT, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
        "volume BIGINT, oi BIGINT)"
    )
    rows = []

    def session(day, last_close, minutes=375, symbol="NIFTY"):
        stamps = pd.date_range(f"{day} 09:15", periods=minutes, freq="min", tz=IST)
        for i, ts in enumerate(stamps):
            price = last_close if i == minutes - 1 else last_close - 5
            rows.append(
                (symbol, "NSE_INDEX", "1m", int(ts.timestamp()), price, price, price, price, 0, 0)
            )

    session("2024-01-02", 21000.0)
    session("2024-01-03", 21100.0)
    session("2024-01-04", 21200.0, minutes=60)  # partial session, must be skipped
    # a post-close artefact bar must not become the close
    rows.append(("NIFTY", "NSE_INDEX", "1m", _epoch("2024-01-03", "15:45"), 1, 1, 1, 1, 0, 0))
    session("2024-01-05", 5.0, symbol="OTHER")
    frame = pd.DataFrame(
        rows,
        columns=[
            "symbol",
            "exchange",
            "interval",
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "oi",
        ],
    )
    con.register("frame", frame)
    con.execute("INSERT INTO market_data SELECT * FROM frame")
    con.close()
    return path


class TestHistorifyCloses:
    def test_uses_last_session_bar_and_ist_dates(self, db):
        close = rd.daily_close_from_historify(db)
        assert close.loc["2024-01-02"] == 21000.0
        assert close.loc["2024-01-03"] == 21100.0
        assert close.index.tz is None

    def test_skips_incomplete_sessions(self, db):
        close = rd.daily_close_from_historify(db)
        assert pd.Timestamp("2024-01-04") not in close.index

    def test_missing_symbol_gives_empty_series(self, db):
        assert rd.daily_close_from_historify(db, symbol="NOPE").empty


def _yahoo(series_by_ticker):
    def fetch(ticker, start):
        return series_by_ticker[ticker].copy()

    return fetch


def _series(start, values):
    return pd.Series(values, index=pd.bdate_range(start, periods=len(values)), dtype=float)


class TestLoadInputs:
    def test_broker_data_wins_and_yahoo_fills_the_rest(self, db):
        yahoo_nifty = _series("2023-12-29", [20950, 20980, 21001, 21102, 21203])
        vix = _series("2023-12-29", [13, 14, 15, 16, 17])
        inputs = rd.load_regime_inputs(db, fetch=_yahoo({"^NSEI": yahoo_nifty, "^INDIAVIX": vix}))
        close = inputs.close
        assert close.loc["2024-01-02"] == 21000.0  # broker, not Yahoo's 21001
        assert close.loc["2023-12-29"] == 20950.0  # only Yahoo has it
        assert close.loc["2024-01-04"] == 21203.0  # broker session was partial
        assert inputs.source["nifty_historify_days"] == 2
        assert inputs.source["nifty_yahoo_days"] == 3
        assert inputs.vix.loc["2024-01-03"] == 16

    def test_cross_check_reports_agreement_on_overlap(self, db):
        yahoo_nifty = _series("2023-12-29", [20950, 20980, 21001, 21102, 21203])
        vix = _series("2023-12-29", [13] * 5)
        inputs = rd.load_regime_inputs(db, fetch=_yahoo({"^NSEI": yahoo_nifty, "^INDIAVIX": vix}))
        check = inputs.source["nifty_cross_check"]
        assert check["days"] == 2
        assert check["max_abs_pct_diff"] < 0.01
        assert check["share_within_0_1pct"] == 1.0

    def test_vix_failure_gives_no_vix_and_says_so(self, db):
        def fetch(ticker, start):
            if ticker == "^INDIAVIX":
                raise RuntimeError("offline")
            return _series("2023-12-29", [20950, 20980, 21001, 21102, 21203])

        inputs = rd.load_regime_inputs(db, fetch=fetch)
        assert inputs.vix is None
        assert inputs.source["vix_source"] == "unavailable"

    def test_no_nifty_at_all_raises(self, tmp_path):
        path = tmp_path / "empty.duckdb"
        con = duckdb.connect(str(path))
        con.execute(
            "CREATE TABLE market_data (symbol VARCHAR, exchange VARCHAR, interval VARCHAR, "
            "timestamp BIGINT, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
            "volume BIGINT, oi BIGINT)"
        )
        con.close()

        def fetch(ticker, start):
            raise RuntimeError("offline")

        with pytest.raises(RuntimeError, match="NIFTY"):
            rd.load_regime_inputs(path, fetch=fetch)

    def test_source_records_ranges(self, db):
        yahoo_nifty = _series("2023-12-29", [20950, 20980, 21001, 21102, 21203])
        vix = _series("2023-12-29", [13] * 5)
        inputs = rd.load_regime_inputs(db, fetch=_yahoo({"^NSEI": yahoo_nifty, "^INDIAVIX": vix}))
        assert inputs.source["nifty_from"] == "2023-12-29"
        assert inputs.source["nifty_to"] == "2024-01-04"
        assert inputs.source["vix_source"] == "yahoo ^INDIAVIX"


class TestIndependentInputs:
    """NIFTY from the broker store only: no Yahoo, no substitute basket, VIX optional."""

    def test_uses_only_the_broker_store(self, db):
        inputs = rd.load_independent_regime_inputs(db)
        assert list(inputs.close.index.strftime("%Y-%m-%d")) == ["2024-01-02", "2024-01-03"]
        assert inputs.close.loc["2024-01-03"] == 21100.0  # the partial session is skipped
        assert inputs.vix is None

    def test_the_source_record_says_what_it_is_and_what_it_is_not(self, db):
        source = rd.load_independent_regime_inputs(db).source
        assert source["nifty_source"].startswith("historify")
        assert source["external_sources"] == "none"
        assert source["vix_source"] == "unavailable"
        assert source["nifty_from"] == "2024-01-02" and source["nifty_to"] == "2024-01-03"
        assert source["nifty_sessions"] == 2

    def test_the_series_is_pinned_by_a_hash_of_its_values(self, db):
        a = rd.load_independent_regime_inputs(db).source["nifty_sha256"]
        b = rd.load_independent_regime_inputs(db).source["nifty_sha256"]
        assert a == b and len(a) == 64

    def test_no_nifty_in_the_store_is_a_plain_unavailable_error(self, tmp_path):
        con = duckdb.connect(str(tmp_path / "empty.duckdb"))
        con.execute(
            "CREATE TABLE market_data (symbol VARCHAR, exchange VARCHAR, interval VARCHAR, "
            "timestamp BIGINT, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
            "volume BIGINT, oi BIGINT)"
        )
        con.close()
        with pytest.raises(rd.RegimeDataUnavailable, match="NIFTY"):
            rd.load_independent_regime_inputs(tmp_path / "empty.duckdb")

    def test_a_missing_store_is_unavailable_not_a_crash(self, tmp_path):
        with pytest.raises(rd.RegimeDataUnavailable):
            rd.load_independent_regime_inputs(tmp_path / "does-not-exist.duckdb")

    def test_no_network_is_touched(self, db, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("the independent loader must not fetch from the network")

        monkeypatch.setattr(rd, "fetch_yahoo_close", boom)
        rd.load_independent_regime_inputs(db)
