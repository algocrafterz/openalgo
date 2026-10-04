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

LINEAGE_COLUMNS: list[str] = [
    "signal_id",
    "order_id",
    "entry_fill_id",
    "exit_fill_id",
    "position_id",
]
COST_COMPONENT_COLUMNS: list[str] = [
    "cost_brokerage_bps",
    "cost_stt_bps",
    "cost_exchange_bps",
    "cost_gst_bps",
    "cost_sebi_bps",
    "cost_stamp_bps",
    "cost_slippage_bps",
    "modeled_cost_bps",
]
#: Base columns, then lineage ids, then the modelled cost lines. The extras are optional.
TRADE_COLUMNS_EXTENDED: list[str] = TRADE_COLUMNS + LINEAGE_COLUMNS + COST_COMPONENT_COLUMNS

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


def trades_to_frame(
    trades: Sequence[Trade], *, components: pd.DataFrame | None = None
) -> pd.DataFrame:
    """Build the trades DataFrame in `TRADE_COLUMNS` order, one row per trade.

    Optional extras are appended after the base columns: the lineage ids (only when some trade
    carries a non-None value) and the modelled cost columns from `components`, a DataFrame
    aligned row-for-row to `trades`. With neither, the frame is exactly the base 14 columns.
    """
    rows = list(trades)
    if components is not None and len(components) != len(rows):
        raise ValueError(f"components has {len(components)} rows for {len(rows)} trades")
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
    columns = list(TRADE_COLUMNS)
    for name in LINEAGE_COLUMNS:
        values = [getattr(t, name, None) for t in rows]
        if any(v is not None for v in values):
            data[name] = pd.Series(values, dtype=object)
            columns.append(name)
    if components is not None:
        for name in COST_COMPONENT_COLUMNS:
            data[name] = pd.Series(components[name].to_numpy(dtype="float64"), dtype="float64")
            columns.append(name)
    return pd.DataFrame(data, columns=columns)


def _schema_for(columns: Sequence[str]) -> pa.Schema:
    extra = []
    for name in columns:
        if name in LINEAGE_COLUMNS:
            extra.append((name, pa.string()))
        elif name in COST_COMPONENT_COLUMNS:
            extra.append((name, pa.float64()))
    return pa.schema([*_SCHEMA, *extra]) if extra else _SCHEMA


def _to_table(trades: Sequence[Trade], components: pd.DataFrame | None = None) -> pa.Table:
    df = trades_to_frame(trades, components=components)
    schema = _schema_for(list(df.columns))
    arrays = []
    for field in schema:
        col = df[field.name]
        if pa.types.is_floating(field.type):
            arrays.append(pa.array(col.to_numpy(), type=field.type, from_pandas=True))
        elif pa.types.is_timestamp(field.type):
            arrays.append(pa.array(col, type=field.type, from_pandas=True))
        else:
            arrays.append(pa.array(col.tolist(), type=field.type))
    return pa.Table.from_arrays(arrays, schema=schema)


#: Rows per row group in the ledger files. With sorted ids and statistics, a lookup by id
#: touches one or two groups instead of decoding the whole file.
LEDGER_ROW_GROUP_SIZE = 50_000


def _parquet_bytes(table: pa.Table, *, prunable: bool = False) -> bytes:
    """Deterministic parquet bytes (no pandas metadata). `prunable` writes row-group statistics
    and bounded row groups, for ledger files that are read with filters; the trades table keeps
    its pinned single-group, statistics-free layout."""
    sink = io.BytesIO()
    extra = {"row_group_size": LEDGER_ROW_GROUP_SIZE} if prunable else {}
    pq.write_table(
        table,
        sink,
        compression="zstd",
        use_dictionary=False,
        write_statistics=prunable,
        data_page_version="2.0",
        store_schema=False,
        **extra,
    )
    return sink.getvalue()


def trades_parquet_bytes(
    trades: Sequence[Trade], *, components: pd.DataFrame | None = None
) -> bytes:
    """Serialise trades to deterministic parquet bytes (no pandas metadata, no stats drift)."""
    return _parquet_bytes(_to_table(trades, components))


#: Arrow schemas of the four lineage ledgers (plan step 4). Fixed types keep an empty ledger
#: byte-compatible in schema with a full one.
LEDGER_SCHEMAS: dict[str, pa.Schema] = {
    "signals": pa.schema(
        [
            ("signal_id", pa.string()),
            ("symbol", pa.string()),
            ("bar_time", _TS),
            ("direction", pa.int64()),
            ("tag", pa.string()),
            ("signal_price", pa.float64()),
            ("stop", pa.float64()),
            ("target", pa.float64()),
            ("risk", pa.float64()),
            ("accepted", pa.bool_()),
            ("exclusion_id", pa.string()),
        ]
    ),
    "orders": pa.schema(
        [
            ("order_id", pa.string()),
            ("signal_id", pa.string()),
            ("symbol", pa.string()),
            ("submit_time", _TS),
            ("order_type", pa.string()),
            ("side", pa.string()),
            ("fill_bar_time", _TS),
            ("status", pa.string()),
            ("exclusion_id", pa.string()),
        ]
    ),
    "fills": pa.schema(
        [
            ("fill_id", pa.string()),
            ("order_id", pa.string()),
            ("position_id", pa.string()),
            ("symbol", pa.string()),
            ("fill_time", _TS),
            ("fill_price", pa.float64()),
            ("side", pa.string()),
            ("kind", pa.string()),
            ("exit_reason", pa.string()),
            ("leg", pa.int64()),
        ]
    ),
    "exclusions": pa.schema(
        [
            ("exclusion_id", pa.string()),
            ("scope", pa.string()),
            ("symbol", pa.string()),
            ("date", pa.date32()),
            ("bar_time", _TS),
            ("reason_code", pa.string()),
            ("detail", pa.string()),
            ("count", pa.int64()),
        ]
    ),
}


#: ledger name -> (time column, id column). Frames are sorted by (symbol, time, id) before
#: writing, so string ids, which start with the symbol, get tight min/max per row group.
_LEDGER_SORT = {
    "signals": ("bar_time", "signal_id"),
    "orders": ("submit_time", "order_id"),
    "fills": ("fill_time", "fill_id"),
    "exclusions": ("bar_time", "exclusion_id"),
}


def _ledger_name(schema: pa.Schema) -> str | None:
    return next((n for n, sch in LEDGER_SCHEMAS.items() if sch.equals(schema)), None)


def _column_array(col: pd.Series, field: pa.Field) -> pa.Array:
    if pa.types.is_date32(field.type):
        values = [None if pd.isna(v) else pd.Timestamp(v).date() for v in col.tolist()]
        return pa.array(values, type=field.type)
    if pa.types.is_string(field.type):
        return pa.array([None if pd.isna(v) else v for v in col.tolist()], type=field.type)
    return pa.array(col, type=field.type, from_pandas=True)


def frame_parquet_bytes(frame: pd.DataFrame, schema: pa.Schema | None = None) -> bytes:
    """Serialise any frame to deterministic parquet bytes, with the trades table's writer options.

    With `schema` (for example `LEDGER_SCHEMAS["fills"]`) the columns must match it by name and
    are cast to its types, so a zero-row frame still carries the full schema. Without it the
    schema is inferred from the frame and the pandas metadata is dropped. The index is never
    written. A frame written with one of `LEDGER_SCHEMAS` is sorted by (symbol, time, id) and
    written with row-group statistics and `LEDGER_ROW_GROUP_SIZE` rows per group, so id lookups
    with pyarrow filters read only the matching groups.
    """
    if schema is None:
        table = pa.Table.from_pandas(frame, preserve_index=False)
        return _parquet_bytes(table.replace_schema_metadata(None))
    if list(frame.columns) != schema.names:
        raise ValueError(f"frame columns {list(frame.columns)} do not match schema {schema.names}")
    ledger = _ledger_name(schema)
    if ledger is not None:
        time_col, id_col = _LEDGER_SORT[ledger]
        frame = frame.sort_values(["symbol", time_col, id_col], kind="mergesort")
    arrays = [_column_array(frame[field.name], field) for field in schema]
    return _parquet_bytes(pa.Table.from_arrays(arrays, schema=schema), prunable=ledger is not None)


def write_trades_parquet(
    trades: Sequence[Trade], path: Path, *, components: pd.DataFrame | None = None
) -> None:
    """Write trades to `path` (parent directory must exist)."""
    Path(path).write_bytes(trades_parquet_bytes(trades, components=components))


def read_trades_parquet(path: Path) -> pd.DataFrame:
    """Read a trades file; timestamps come back tz-aware in IST."""
    df = pq.read_table(path).to_pandas()
    for col in ("entry_time", "exit_time"):
        df[col] = pd.to_datetime(df[col], utc=True).dt.tz_convert(IST_NAME)
    return df


def read_ledger_parquet(path: Path) -> pd.DataFrame:
    """Read one of the four ledger files; every timestamp column comes back tz-aware in IST."""
    df = pq.read_table(path).to_pandas()
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = pd.to_datetime(df[col], utc=True).dt.tz_convert(IST_NAME)
    return df
