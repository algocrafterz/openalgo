"""Loader dtype policy."""

import numpy as np
import pandas as pd

from signal_engine.backtest import data


def test_downcast_keeps_prices_exact():
    df = pd.DataFrame(
        {
            "Open": [95.2, 100.0],
            "High": [96.0, 101.0],
            "Low": [95.2, 99.0],
            "Close": [95.19999713410674, 100.5],
            "Volume": [-5.0, 1234.0],
        }
    )
    out = data._downcast(df)
    for col in ("Open", "High", "Low", "Close"):
        assert out[col].dtype == np.float64
    assert out["Low"].iloc[0] == 95.2
    assert out["Close"].iloc[0] == 95.19999713410674
    assert out["Volume"].dtype == np.uint32
    assert list(out["Volume"]) == [0, 1234]
