"""Read and write the run's evidence/trades.parquet from engine `Trade` objects.

The engine is R-based: it carries no share count, so `quantity` is always null. bps use
the same conversion as the tearsheet headline, `bps = R x stop% x 100` with
`stop% = risk / signal_price * 100`. All timestamps are tz-aware IST.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from signal_engine.backtest.types import Trade

TRADE_COLUMNS: list[str] = [
    "trade_id",
    "symbol",
    "side",
    "entry_time",
    "exit_time",
    "entry_price",
    "exit_price",
    "quantity",
    "exit_reason",
    "gross_r",
    "net_r",
    "gross_bps",
    "net_bps",
    "cost_bps",
]

#: Named zone (not a fixed offset) so the parquet tz and the pandas dtype read "Asia/Kolkata".
IST_NAME = "Asia/Kolkata"
_TS = pa.timestamp("ns", tz=IST_NAME)
_SCHEMA = pa.schema(
    [
        ("trade_id", pa.string()),
        ("symbol", pa.string()),
        ("side", pa.string()),
        ("entry_time", _TS),
        ("exit_time", _TS),
        ("entry_price", pa.float64()),
        ("exit_price", pa.float64()),
        ("quantity", pa.float64()),
        ("exit_reason", pa.string()),
        ("gross_r", pa.float64()),
        ("net_r", pa.float64()),
        ("gross_bps", pa.float64()),
        ("net_bps", pa.float64()),
        ("cost_bps", pa.float64()),
    ]
)


def _to_ist(value: Any) -> pd.Timestamp:
    """Convert to a tz-aware IST Timestamp. Naive values are IST, never UTC. None -> NaT."""
    if value is None:
        return pd.NaT
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        return pd.NaT
    return ts.tz_localize(IST_NAME) if ts.tzinfo is None else ts.tz_convert(IST_NAME)


def _ts_series(values: list[pd.Timestamp]) -> pd.Series:
    return pd.Series(values, dtype=f"datetime64[ns, {IST_NAME}]")


def trades_to_frame(trades: Sequence[Trade]) -> pd.DataFrame:
    """Build the trades DataFrame in `TRADE_COLUMNS` order, one row per trade."""
    rows = list(trades)
    gross_r = [float(t.r_gross) for t in rows]
    net_r = [float(t.r_net) for t in rows]
    risk_pct = [float(t.risk_pct) for t in rows]
    gross_bps = [g * p * 100 for g, p in zip(gross_r, risk_pct, strict=True)]
    net_bps = [n * p * 100 for n, p in zip(net_r, risk_pct, strict=True)]
    data = {
        "trade_id": pd.Series([f"T-{i:06d}" for i in range(1, len(rows) + 1)], dtype=object),
        "symbol": pd.Series([t.symbol for t in rows], dtype=object),
        "side": pd.Series(["BUY" if t.direction == 1 else "SELL" for t in rows], dtype=object),
        "entry_time": _ts_series([_to_ist(t.entry_time) for t in rows]),
        "exit_time": _ts_series([_to_ist(t.exit_time) for t in rows]),
        "entry_price": pd.Series([float(t.entry) for t in rows], dtype="float64"),
        "exit_price": pd.Series([float(t.exit) for t in rows], dtype="float64"),
        "quantity": pd.Series([float("nan")] * len(rows), dtype="float64"),
        "exit_reason": pd.Series([t.reason for t in rows], dtype=object),
        "gross_r": pd.Series(gross_r, dtype="float64"),
        "net_r": pd.Series(net_r, dtype="float64"),
        "gross_bps": pd.Series(gross_bps, dtype="float64"),
        "net_bps": pd.Series(net_bps, dtype="float64"),
        "cost_bps": pd.Series(
            [g - n for g, n in zip(gross_bps, net_bps, strict=True)], dtype="float64"
        ),
    }
    return pd.DataFrame(data, columns=TRADE_COLUMNS)


def _to_table(trades: Sequence[Trade]) -> pa.Table:
    df = trades_to_frame(trades)
    arrays = []
    for field in _SCHEMA:
        col = df[field.name]
        if pa.types.is_floating(field.type):
            arrays.append(pa.array(col.to_numpy(), type=field.type, from_pandas=True))
        elif pa.types.is_timestamp(field.type):
            arrays.append(pa.array(col, type=field.type, from_pandas=True))
        else:
            arrays.append(pa.array(col.tolist(), type=field.type))
    return pa.Table.from_arrays(arrays, schema=_SCHEMA)


def trades_parquet_bytes(trades: Sequence[Trade]) -> bytes:
    """Serialise trades to deterministic parquet bytes (no pandas metadata, no stats drift)."""
    sink = io.BytesIO()
    pq.write_table(
        _to_table(trades),
        sink,
        compression="zstd",
        use_dictionary=False,
        write_statistics=False,
        data_page_version="2.0",
        store_schema=False,
    )
    return sink.getvalue()


def write_trades_parquet(trades: Sequence[Trade], path: Path) -> None:
    """Write trades to `path` (parent directory must exist)."""
    Path(path).write_bytes(trades_parquet_bytes(trades))


def read_trades_parquet(path: Path) -> pd.DataFrame:
    """Read a trades file; timestamps come back tz-aware in IST."""
    df = pq.read_table(path).to_pandas()
    for col in ("entry_time", "exit_time"):
        df[col] = pd.to_datetime(df[col], utc=True).dt.tz_convert(IST_NAME)
    return df[TRADE_COLUMNS]
