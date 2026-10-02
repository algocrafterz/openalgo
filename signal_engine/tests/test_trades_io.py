"""Tests for the trades.parquet writer and reader."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest

from signal_engine.backtest import tearsheet
from signal_engine.backtest.trades_io import (
    TRADE_COLUMNS,
    read_trades_parquet,
    trades_parquet_bytes,
    trades_to_frame,
    write_trades_parquet,
)
from signal_engine.backtest.types import Trade


def _trade(i: int = 0, **kw) -> Trade:
    base = {
        "symbol": f"SYM{i}",
        "day": "2026-01-05",
        "direction": 1,
        "tag": "t",
        "entry_time": pd.Timestamp("2026-01-05 09:30"),
        "entry": 100.0,
        "signal_price": 100.0,
        "sl": 99.0,
        "tp": 102.0,
        "risk": 1.0,
        "exit_time": pd.Timestamp("2026-01-05 10:00"),
        "exit": 102.0,
        "reason": "TP",
        "r_gross": 2.0,
        "r_net": 1.8,
    }
    base.update(kw)
    return Trade(**base)


def test_schema_and_order() -> None:
    df = trades_to_frame([_trade(0), _trade(1)])
    assert list(df.columns) == TRADE_COLUMNS
    assert list(df["trade_id"]) == ["T-000001", "T-000002"]
    assert list(df["symbol"]) == ["SYM0", "SYM1"]
    assert df["quantity"].isna().all()
    assert df["exit_reason"].iloc[0] == "TP"


def test_side_mapping() -> None:
    df = trades_to_frame([_trade(direction=1), _trade(direction=-1)])
    assert list(df["side"]) == ["BUY", "SELL"]


def test_ist_handling() -> None:
    aware_utc = pd.Timestamp("2026-01-05 04:00", tz="UTC")  # 09:30 IST
    df = trades_to_frame(
        [
            _trade(entry_time=pd.Timestamp("2026-01-05 09:30"), exit_time="2026-01-05 10:00"),
            _trade(entry_time=aware_utc, exit_time=None),
        ]
    )
    assert str(df["entry_time"].dt.tz) == "Asia/Kolkata"
    assert df["entry_time"].iloc[0].hour == 9 and df["entry_time"].iloc[0].minute == 30
    assert df["entry_time"].iloc[1].hour == 9 and df["entry_time"].iloc[1].minute == 30
    assert df["exit_time"].iloc[0] == pd.Timestamp("2026-01-05 10:00", tz="Asia/Kolkata")
    assert pd.isna(df["exit_time"].iloc[1])


def test_bps_math() -> None:
    df = trades_to_frame([_trade(risk=1.0, signal_price=100.0, r_gross=2.0, r_net=1.8)])
    assert df["gross_bps"].iloc[0] == pytest.approx(200.0)
    assert df["net_bps"].iloc[0] == pytest.approx(180.0)
    assert df["cost_bps"].iloc[0] == pytest.approx(20.0)


def test_matches_tearsheet_headline() -> None:
    trades = [
        _trade(0, risk=1.0, r_gross=2.0, r_net=1.8),
        _trade(1, risk=2.5, signal_price=250.0, r_gross=-1.0, r_net=-1.1),
        _trade(2, risk=0.7, signal_price=80.0, r_gross=0.3, r_net=0.2, direction=-1),
    ]
    df = trades_to_frame(trades)
    head = tearsheet._core(trades)
    assert df["gross_bps"].mean() == pytest.approx(head["gross_bps"], abs=1e-3)
    assert df["net_bps"].mean() == pytest.approx(head["net_bps"], abs=1e-3)


def test_zero_rows_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "t.parquet"
    write_trades_parquet([], path)
    schema = pq.read_schema(path)
    assert schema.names == TRADE_COLUMNS
    df = read_trades_parquet(path)
    assert len(df) == 0 and list(df.columns) == TRADE_COLUMNS
    assert str(df["entry_time"].dt.tz) == "Asia/Kolkata"
    assert df["gross_bps"].dtype == np.float64
    assert df["trade_id"].dtype == object
    assert df["quantity"].dtype == np.float64


def test_round_trip_values(tmp_path: Path) -> None:
    path = tmp_path / "t.parquet"
    trades = [_trade(0), _trade(1, exit_time=None, direction=-1)]
    write_trades_parquet(trades, path)
    back = read_trades_parquet(path)
    assert str(back["exit_time"].dt.tz) == "Asia/Kolkata"
    assert back["entry_time"].iloc[0] == pd.Timestamp("2026-01-05 09:30", tz="Asia/Kolkata")
    assert pd.isna(back["exit_time"].iloc[1])
    assert list(back["side"]) == ["BUY", "SELL"]
    assert back["quantity"].isna().all()


def test_bytes_deterministic() -> None:
    trades = [_trade(0), _trade(1)]
    first = trades_parquet_bytes(trades)
    assert first == trades_parquet_bytes(trades)
    assert first[:4] == b"PAR1"
    schema = pq.ParquetFile(io.BytesIO(first)).schema_arrow
    assert not schema.metadata  # no pandas/library version metadata embedded


def test_bytes_match_written_file(tmp_path: Path) -> None:
    trades = [_trade(0)]
    path = tmp_path / "t.parquet"
    write_trades_parquet(trades, path)
    assert path.read_bytes() == trades_parquet_bytes(trades)
