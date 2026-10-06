"""The random-entry baseline exits where the trade did: at the open when the trade exited at the open."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest.baseline import random_entry_baseline
from signal_engine.backtest.types import Trade


def frames():
    idx = pd.bdate_range("2026-01-05", periods=300, tz="Asia/Kolkata")
    opens = 100.0 + np.arange(300) * 0.1
    closes = opens + 1.0  # every close sits 1.0 above its open
    return {"A": pd.DataFrame({"Open": opens, "Close": closes}, index=idx)}, idx


def trade(idx, exit_at_open):
    return Trade(
        symbol="A",
        day=idx[50].date(),
        direction=1,
        tag="t",
        entry_time=idx[50],
        entry=105.0,
        signal_price=105.0,
        sl=float("nan"),
        tp=float("inf"),
        risk=float("nan"),
        exit_time=idx[60],
        exit=106.0,
        reason="REBALANCE",
        r_gross=float("nan"),
        r_net=float("nan"),
        gross_ret=0.01,
        net_ret=0.0076,
        exit_at_open=exit_at_open,
    )


def test_an_open_exit_is_matched_against_an_open_to_open_baseline():
    f, idx = frames()
    at_close = random_entry_baseline([trade(idx, False)], f, draws=200)
    at_open = random_entry_baseline([trade(idx, True)], f, draws=200)
    # same seed, same draws: the only difference is exiting at the open instead of the close,
    # which is 1.0 on a price near 100 to 130, so roughly 80 to 100 bps lower
    gap = at_close["gross_bps"] - at_open["gross_bps"]
    assert 70.0 < gap < 105.0


def test_a_trade_without_the_flag_keeps_the_old_open_to_close_baseline():
    f, idx = frames()
    t = trade(idx, False)
    out = random_entry_baseline([t], f, draws=200)
    assert out["method"].endswith("round-trip cost deducted")
    assert out["gross_bps"] > 70.0  # the +1.0 close premium is in the baseline
