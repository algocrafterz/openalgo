"""Ctx keeps prepared columns at their own dtype and interns the session date."""

import datetime as dt

import numpy as np
import pandas as pd

from signal_engine.backtest.types import Ctx


def test_ctx_keeps_float64_columns():
    idx = pd.date_range("2026-03-02 09:15", periods=4, freq="5min")
    d1, d2 = dt.date(2026, 3, 2), dt.date(2026, 3, 3)
    df = pd.DataFrame(
        {
            "Open": [95.2, 95.19999713410674, 1.0 / 3.0, 100.1],
            "High": [96.0, 96.1, 96.2, 96.3],
            "Low": [95.2, 95.1, 95.0, 94.9],
            "Close": [95.5, 95.6, 95.7, 95.8],
            "vwap": [95.19999713410674, 95.3, 95.4, 95.5],
            "Volume": np.array([10, 20, 30, 40], dtype=np.uint32),
            "day": [d1, d1, d2, d2],
        },
        index=idx,
    )
    c = Ctx(df, symbol="X")
    for col in ("Open", "High", "Low", "Close", "vwap"):
        assert c.a[col].dtype == np.float64
        assert np.array_equal(c.a[col], df[col].to_numpy())
    assert c.a["Volume"].dtype == np.uint32
    assert list(c.a["day"]) == [d1, d1, d2, d2]
    assert c.a["day"][0] is c.a["day"][1]
    assert c.a["day"][2] is c.a["day"][3]


def test_ctx_without_day_column():
    df = pd.DataFrame({"Close": [1.0, 2.0]})
    assert Ctx(df).a["day"] is None


def test_ctx_missing_day_stays_missing():
    d1, d2 = dt.date(2026, 3, 2), dt.date(2026, 3, 3)
    for missing in (None, pd.NaT):
        df = pd.DataFrame(
            {"Close": [1.0, 2.0, 3.0], "day": pd.Series([d1, missing, d2], dtype=object)}
        )
        days = Ctx(df).a["day"]
        assert days[0] == d1 and days[2] == d2
        assert days[1] is None


def test_ctx_datetime64_day_keeps_dtype():
    df = pd.DataFrame({"Close": [1.0, 2.0], "day": pd.to_datetime(["2026-03-02", "2026-03-03"])})
    assert Ctx(df).a["day"].dtype == np.dtype("datetime64[ns]")
