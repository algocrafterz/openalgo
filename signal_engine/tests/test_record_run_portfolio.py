"""record_run._portfolio_stage: the daily book of a fixed-notional swing run."""

import dataclasses
import math
from types import SimpleNamespace

import pandas as pd

from signal_engine.backtest import record_run
from signal_engine.backtest.types import Trade
from signal_engine.timeutils import IST

DAYS = [d.date() for d in pd.bdate_range("2024-01-01", "2024-01-31")]
CAL = SimpleNamespace(trading_days=frozenset(DAYS))


def _trade(symbol, entry, exit_, p0, p1):
    return Trade(
        symbol=symbol,
        day=pd.Timestamp(entry),
        direction=1,
        tag="t",
        entry_time=pd.Timestamp(entry, tz=IST),
        entry=p0,
        signal_price=p0,
        sl=math.nan,
        tp=math.nan,
        risk=math.nan,
        exit_time=pd.Timestamp(exit_, tz=IST),
        exit=p1,
        reason="REBALANCE_EXIT",
        r_gross=math.nan,
        r_net=math.nan,
        gross_ret=p1 / p0 - 1,
        net_ret=p1 / p0 - 1 - 0.0024,
    )


def _daily(symbols):
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz=IST) for d in DAYS])
    return {
        s: pd.DataFrame({"Close": [100.0 + i for i in range(len(DAYS))]}, index=idx)
        for s in symbols
    }


MODEL = SimpleNamespace(fixed_notional=100000.0)
TRADES = [
    _trade("A", "2024-01-02", "2024-01-15", 101.0, 112.0),
    _trade("B", "2024-01-03", "2024-01-31", 102.0, 122.0),
]


def test_swing_fixed_notional_run_gets_a_book_and_daily_returns():
    block, returns = record_run._portfolio_stage(
        "swing",
        TRADES,
        MODEL,
        {"max_positions": 2},
        None,
        CAL,
        ["2024-01-01", "2024-01-31"],
        _daily,
    )
    assert block["capital_rs"] == 200000.0
    assert abs(block["reconciliation"]["difference_rs"]) < 1e-6
    assert len(returns) == block["stats"]["sessions"] == len(DAYS) - 1


def test_not_applicable_or_unavailable_cases():
    args = (TRADES, MODEL, {"max_positions": 2}, None, CAL, ["2024-01-01", "2024-01-31"], _daily)
    assert record_run._portfolio_stage("intraday", *args) == (None, None)
    assert record_run._portfolio_stage("swing", TRADES, None, *args[2:]) == (None, None)
    block, returns = record_run._portfolio_stage("swing", TRADES, MODEL, {}, *args[3:])
    assert "max_positions" in block["unavailable"] and returns is None
    late = [dataclasses.replace(TRADES[0], exit_time=pd.Timestamp("2024-03-01", tz=IST))]
    block, returns = record_run._portfolio_stage("swing", late, MODEL, *args[2:])
    assert "outside" in block["unavailable"] and returns is None
