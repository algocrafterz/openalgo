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


# ---- optional lineage and cost columns --------------------------------------------------

import hashlib  # noqa: E402

from signal_engine.backtest.trades_io import TRADE_COLUMNS_EXTENDED  # noqa: E402

LINEAGE = ["signal_id", "order_id", "entry_fill_id", "exit_fill_id", "position_id"]
COST_COLS = [
    "cost_brokerage_bps",
    "cost_stt_bps",
    "cost_exchange_bps",
    "cost_gst_bps",
    "cost_sebi_bps",
    "cost_stamp_bps",
    "cost_slippage_bps",
    "modeled_cost_bps",
]
#: sha256 of the parquet bytes of `_pinned_fixture()` computed before the optional columns existed.
_PINNED_SHA = "ed0d02c3b759f29a7866c602e211b38675b16f5142dc074a076ce9bfad525c48"


def _pinned_fixture() -> list[Trade]:
    return [
        _trade(0),
        _trade(1, direction=-1, r_gross=-1.0, r_net=-1.1, reason="SL", exit=99.0),
    ]


def _lineage_trades() -> list[Trade]:
    out = []
    for i, t in enumerate(_pinned_fixture(), start=1):
        object.__setattr__(t, "signal_id", f"S{i}")
        object.__setattr__(t, "order_id", f"O|S{i}")
        object.__setattr__(t, "entry_fill_id", f"F|O|S{i}|e1")
        object.__setattr__(t, "exit_fill_id", f"F|O|S{i}|x1")
        object.__setattr__(t, "position_id", f"P|F|O|S{i}|e1")
        out.append(t)
    return out


def _components(n: int) -> pd.DataFrame:
    data = {c: np.full(n, 1.0 + k) for k, c in enumerate(COST_COLS)}
    return pd.DataFrame(data)


def test_trade_columns_unchanged_and_extended_is_superset() -> None:
    assert len(TRADE_COLUMNS) == 14
    assert TRADE_COLUMNS_EXTENDED == TRADE_COLUMNS + LINEAGE + COST_COLS


def test_backward_compatible_bytes_pinned() -> None:
    data = trades_parquet_bytes(_pinned_fixture())
    assert hashlib.sha256(data).hexdigest() == _PINNED_SHA


def test_no_extras_by_default() -> None:
    df = trades_to_frame(_pinned_fixture())
    assert list(df.columns) == TRADE_COLUMNS


def test_all_none_lineage_is_omitted() -> None:
    trades = _pinned_fixture()
    for t in trades:
        for name in LINEAGE:
            object.__setattr__(t, name, None)
    assert list(trades_to_frame(trades).columns) == TRADE_COLUMNS
    assert hashlib.sha256(trades_parquet_bytes(trades)).hexdigest() == _PINNED_SHA


def test_lineage_columns_present_after_base() -> None:
    df = trades_to_frame(_lineage_trades())
    assert list(df.columns) == TRADE_COLUMNS + LINEAGE
    assert list(df["signal_id"]) == ["S1", "S2"]
    assert list(df["position_id"]) == ["P|F|O|S1|e1", "P|F|O|S2|e1"]


def test_partial_lineage_keeps_none_rows() -> None:
    trades = _lineage_trades()
    object.__setattr__(trades[1], "exit_fill_id", None)
    df = trades_to_frame(trades)
    assert df["exit_fill_id"].iloc[0] == "F|O|S1|x1"
    assert df["exit_fill_id"].iloc[1] is None


def test_cost_columns_present_after_base() -> None:
    df = trades_to_frame(_pinned_fixture(), components=_components(2))
    assert list(df.columns) == TRADE_COLUMNS + COST_COLS
    assert all(df[c].dtype == "float64" for c in COST_COLS)
    assert df["cost_stt_bps"].iloc[0] == 2.0


def test_lineage_then_cost_order() -> None:
    df = trades_to_frame(_lineage_trades(), components=_components(2))
    assert list(df.columns) == TRADE_COLUMNS_EXTENDED


def test_components_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        trades_to_frame(_pinned_fixture(), components=_components(3))


def test_extended_parquet_round_trip_and_determinism(tmp_path: Path) -> None:
    trades = _lineage_trades()
    comps = _components(2)
    first = trades_parquet_bytes(trades, components=comps)
    assert first == trades_parquet_bytes(trades, components=comps)
    assert first != trades_parquet_bytes(_pinned_fixture())
    path = tmp_path / "t.parquet"
    write_trades_parquet(trades, path, components=comps)
    assert path.read_bytes() == first
    back = read_trades_parquet(path)
    assert list(back.columns) == TRADE_COLUMNS_EXTENDED
    assert list(back["order_id"]) == ["O|S1", "O|S2"]
    assert back["modeled_cost_bps"].iloc[0] == 8.0
    assert not pq.ParquetFile(io.BytesIO(first)).schema_arrow.metadata


def test_read_base_file_returns_base_columns(tmp_path: Path) -> None:
    path = tmp_path / "t.parquet"
    write_trades_parquet(_pinned_fixture(), path)
    assert list(read_trades_parquet(path).columns) == TRADE_COLUMNS
