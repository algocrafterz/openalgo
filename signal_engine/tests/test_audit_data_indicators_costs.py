"""Audit fixes: intraday end inclusivity, no silent symbol drops, naive-index guard, NaN-risk costs."""

from __future__ import annotations

import datetime as dt

import duckdb
import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import costs, data, indicators
from signal_engine.backtest.data_collect import DataCollector

IST = "Asia/Kolkata"
DAY0 = int(pd.Timestamp("2025-01-01 09:15", tz=IST).timestamp())


def _db(path, extra_rows=()):
    con = duckdb.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE market_data (symbol VARCHAR, interval VARCHAR, timestamp BIGINT, "
            "open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT)"
        )
        rows = []
        for d in range(3):
            for m in range(10):
                rows.append(("AAA", "1m", DAY0 + d * 86400 + m * 60, 100.0, 101.0, 99.0, 100.0, 5))
        # BAD: every bar is outside session hours, so cleaning empties it
        for m in range(10):
            rows.append(("BAD", "1m", DAY0 - 6 * 3600 + m * 60, 100.0, 101.0, 99.0, 100.0, 5))
        rows.extend(extra_rows)
        con.executemany("INSERT INTO market_data VALUES (?,?,?,?,?,?,?,?)", rows)
    finally:
        con.close()


def test_intraday_end_date_is_inclusive(tmp_path):
    db = tmp_path / "h.duckdb"
    _db(db)
    out = data.from_historify(symbols=["AAA"], interval="1m", end="2025-01-02",
                              min_sessions=1, db_path=db)
    days = sorted(set(out["AAA"].index.date))
    assert days == [dt.date(2025, 1, 1), dt.date(2025, 1, 2)]


def test_intraday_end_with_time_part_does_not_reach_next_session(tmp_path):
    db = tmp_path / "h.duckdb"
    _db(db)
    out = data.from_historify(symbols=["AAA"], interval="1m", end="2025-01-02 23:59:59",
                              min_sessions=1, db_path=db)
    assert sorted(set(out["AAA"].index.date)) == [dt.date(2025, 1, 1), dt.date(2025, 1, 2)]


def test_empty_and_fully_cleaned_symbols_are_recorded(tmp_path):
    db = tmp_path / "h.duckdb"
    _db(db)
    col = DataCollector()
    out = data.from_historify(symbols=["AAA", "BAD", "NOPE"], interval="1m",
                              min_sessions=1, db_path=db, collector=col)
    assert set(out) == {"AAA"}
    assert col.symbols_dropped_min_sessions.get("BAD") == 0
    assert col.symbols_dropped_min_sessions.get("NOPE") == 0


def _naive(stamps):
    idx = pd.DatetimeIndex(pd.to_datetime(stamps))
    return pd.DataFrame({"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5, "Volume": 1.0}, index=idx)


def test_add_session_columns_rejects_naive_index():
    df = _naive(["2025-01-01 09:15", "2025-01-01 09:20"])
    with pytest.raises(ValueError, match="tz-naive"):
        indicators.add_session_columns(df)


def test_add_session_columns_does_not_mutate_caller_index():
    df = _naive(["2025-01-01 09:15", "2025-01-01 09:20"]).tz_localize("UTC")
    before = df.index.copy()
    out = indicators.add_session_columns(df)
    assert df.index.equals(before) and str(df.index.tz) == "UTC"
    assert out["mins"].iloc[0] == 14 * 60 + 45  # 09:15 UTC -> 14:45 IST


def _trade(risk, entry=100.0, direction=1):
    from signal_engine.backtest.types import Trade

    return Trade(symbol="X", day=dt.date(2025, 1, 1), direction=direction, tag="t",
                 entry_time=pd.Timestamp("2025-01-01 10:00", tz=IST), entry=entry,
                 signal_price=entry, sl=float("nan"), tp=float("nan"), risk=risk)


def _model(**kw):
    base = {"capital": 100000.0, "risk_per_trade": 0.01, "leverage": 5.0, "product": "MIS",
            "exchange": "NSE", "slippage_bps_per_side": 2.0}
    return costs.CostModel(**{**base, **kw})


def test_decompose_trades_no_stop_without_fixed_notional_raises():
    t = _trade(float("nan"))
    with pytest.raises(costs.CostModelUnavailable):
        _model().notional(t)
    with pytest.raises(costs.CostModelUnavailable):
        costs.decompose_trades([t], _model())


def test_decompose_trades_no_stop_with_fixed_notional_is_finite():
    out = costs.decompose_trades([_trade(float("nan"))], _model(fixed_notional=50000.0))
    assert np.isfinite(out["modeled_cost_bps"]).all()


def test_decompose_trades_matches_per_trade_components_for_stop_trades():
    ts = [_trade(1.0), _trade(2.0, direction=-1)]
    m = _model()
    out = costs.decompose_trades(ts, m)
    for i, t in enumerate(ts):
        assert out["modeled_cost_bps"].iloc[i] == pytest.approx(m.components(t)["modeled_cost_bps"])


def _daily_db(path, sessions=12):
    """Daily bars stamped 05:30 IST, as Historify stores them (verified on the real store)."""
    con = duckdb.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE market_data (symbol VARCHAR, interval VARCHAR, timestamp BIGINT, "
            "open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT)"
        )
        midnight = int(pd.Timestamp("2025-01-01", tz=IST).timestamp())
        rows = [("AAA", "D", midnight + 19800 + d * 86400, 100.0, 101.0, 99.0, 100.0, 5)
                for d in range(sessions)]
        con.executemany("INSERT INTO market_data VALUES (?,?,?,?,?,?,?,?)", rows)
    finally:
        con.close()


def test_daily_end_date_is_inclusive(tmp_path):
    """Daily bars carry a 05:30 stamp, so a midnight bound would silently drop the end session."""
    db = tmp_path / "d.duckdb"
    _daily_db(db)
    out = data.from_historify_daily(symbols=["AAA"], end="2025-01-10", min_sessions=1, db_path=db)
    assert max(out["AAA"].index.date) == dt.date(2025, 1, 10)
