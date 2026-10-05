"""Frozen, hashed snapshot of the legacy 1m store (INTRADAY-5M-DATASET-PLAN.md section 2).

The live `db/historify.duckdb` is read exactly once, read-only. Every later intraday build step
reads only the files written here and names this snapshot's root hash in its own output.

    <out>/legacy-1m-<YYYYMMDDTHHMMSS>-ist/
        bars/<SYMBOL>/<YEAR>.parquet   rows of market_data, unchanged, sorted by timestamp
        reference/nse_last.parquet     NSE last traded price per (symbol, session), the close
                                       reference before the closing auction (section 6.1)
        snapshot.json                  the manifest; its root_hash covers every file

    uv run python -m signal_engine.backtest.data_readiness.intraday_freeze \
        --daily-version nse-daily-20261005-002 --nse-raw db/staging/nse-D.duckdb
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from signal_engine.backtest import data as _data
from signal_engine.backtest.data_readiness import snapshot as _snapshot
from signal_engine.research.intake.fingerprints import canonical_json
from signal_engine.timeutils import IST
from utils.logging import get_logger

logger = get_logger(__name__)

_REPO = Path(__file__).resolve().parents[3]
DEFAULT_OUT = _REPO / "db" / "staging" / "intraday"
SOURCE_ID = "historify.duckdb:market_data:NSE:1m"
DESCRIPTION = ("legacy 1m store written by the Historify cron from AngelOne, plus the March 2026 "
               "Flattrade UI download; no row-level vendor field (created_at only)")
FIRST_DAY = date(2016, 10, 1)
LAST_DAY = date(2026, 10, 1)
_COLS = ["symbol", "timestamp", "open", "high", "low", "close", "volume", "created_at"]
_READ_ONLY = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH


class FreezeError(RuntimeError):
    """The snapshot cannot be taken or does not verify."""


def _epoch(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=IST).timestamp())


def window(first: date = FIRST_DAY, last: date = LAST_DAY) -> tuple[int, int]:
    """[start, end) epoch seconds covering IST sessions `first` to `last` inclusive."""
    return _epoch(first), _epoch(last + timedelta(days=1))


def _write(path: Path, df: pd.DataFrame) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), path, compression="zstd")
    os.chmod(path, _READ_ONLY)
    return _snapshot.sha256_file(path)


def root_hash(files: Sequence[dict]) -> str:
    body = sorted(({"path": f["path"], "sha256": f["sha256"], "rows": f["rows"]} for f in files),
                  key=lambda f: f["path"])
    return hashlib.sha256(canonical_json(body).encode()).hexdigest()


def _year(ts: pd.Series) -> pd.Series:
    return pd.to_datetime(ts, unit="s", utc=True).dt.tz_convert(IST).dt.year


def freeze(live_db: Path, out_root: Path, symbols: Sequence[str], nse_raw: Path,
           daily_version: str, *, first: date = FIRST_DAY, last: date = LAST_DAY) -> Path:
    """Copy the scoped rows of the live store into hashed parquet files and verify the copy."""
    if not symbols:
        raise FreezeError("no symbols to freeze")
    start, end = window(first, last)
    stamp = datetime.now(IST)
    folder = out_root / f"legacy-1m-{stamp.strftime('%Y%m%dT%H%M%S')}-ist"
    if folder.exists():
        raise FreezeError(f"{folder} already exists")
    scope = (f"exchange = 'NSE' AND interval = '1m' AND timestamp >= {start} "
             f"AND timestamp < {end}")
    sql = f"SELECT {', '.join(_COLS)} FROM market_data WHERE {scope} AND symbol = ? ORDER BY timestamp"
    in_scope = f"{scope} AND symbol IN (SELECT unnest(?::VARCHAR[]))"
    files: list[dict] = []
    con = duckdb.connect(str(live_db), read_only=True)
    try:
        for i, sym in enumerate(sorted(symbols)):
            df = con.execute(sql, [sym]).fetchdf()
            if df.empty:
                logger.warning(f"freeze: no 1m rows for {sym}")
                continue
            for yr, part in df.groupby(_year(df.timestamp), sort=True):
                rel = f"bars/{sym}/{int(yr)}.parquet"
                sha = _write(folder / rel, part.reset_index(drop=True))
                files.append({"path": rel, "symbol": sym, "year": int(yr), "rows": len(part),
                              "sha256": sha})
            if i % 25 == 0:
                logger.info(f"freeze: {i + 1} of {len(symbols)} symbols")
        live = con.execute(
            "SELECT symbol, year(to_timestamp(timestamp) AT TIME ZONE 'Asia/Kolkata') AS yr, "
            f"count(*) AS n FROM market_data WHERE {in_scope} GROUP BY ALL",
            [list(symbols)]).fetchall()
        span = con.execute(
            "SELECT min(timestamp), max(timestamp), min(created_at), max(created_at), count(*) "
            f"FROM market_data WHERE {in_scope}", [list(symbols)]).fetchone()
    finally:
        con.close()
    got = {(f["symbol"], f["year"]): f["rows"] for f in files}
    want = {(s, int(y)): int(n) for s, y, n in live}
    if got != want:
        diff = sorted(set(got.items()) ^ set(want.items()))[:5]
        raise FreezeError(f"snapshot row counts differ from the live store: {diff}")

    ref = duckdb.connect(str(nse_raw), read_only=True)
    try:
        last_px = ref.execute(
            "SELECT symbol, session, any_value(last) AS last FROM nse_daily_raw "
            "WHERE series IN ('EQ', 'BE') AND symbol IN (SELECT unnest(?::VARCHAR[])) "
            "AND session BETWEEN ? AND ? GROUP BY symbol, session ORDER BY symbol, session",
            [list(symbols), first, last]).fetchdf()
    finally:
        ref.close()
    ref_rel = "reference/nse_last.parquet"
    files.append({"path": ref_rel, "symbol": None, "year": None, "rows": len(last_px),
                  "sha256": _write(folder / ref_rel, last_px)})

    def to_ist(ts) -> str:
        return datetime.fromtimestamp(int(ts), IST).isoformat()

    manifest = {
        "kind": "intraday_legacy_snapshot",
        "source_id": SOURCE_ID,
        "source_path": str(live_db.resolve()),
        "description": DESCRIPTION,
        "snapshot_time_ist": stamp.isoformat(),
        "sql": sql,
        "scope": {"symbols_from": daily_version, "symbols": sorted(symbols),
                  "first_session": str(first), "last_session": str(last)},
        "nse_last_source": f"{nse_raw} sha256 {_snapshot.sha256_file(nse_raw)}",
        "row_count": int(span[4]),
        "symbol_count": len({f["symbol"] for f in files if f["symbol"]}),
        "first_timestamp_ist": to_ist(span[0]),
        "last_timestamp_ist": to_ist(span[1]),
        "created_at_range": [str(span[2]), str(span[3])],
        "files": files,
        "root_hash_algo": "sha256(canonical_json(sorted [{path, sha256, rows}]))",
        "root_hash": root_hash(files),
    }
    path = folder / "snapshot.json"
    path.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    os.chmod(path, _READ_ONLY)
    errors = verify(folder)
    if errors:
        raise FreezeError(f"snapshot does not verify: {errors[:3]}")
    logger.info(f"freeze: {manifest['row_count']} rows, {manifest['symbol_count']} symbols, "
                f"root {manifest['root_hash'][:12]}")
    return folder


def load(folder: Path) -> dict:
    return json.loads((folder / "snapshot.json").read_text(encoding="utf-8"))


def verify(folder: Path) -> list[str]:
    """Recompute every file hash and the root hash; an empty list means the snapshot is intact."""
    m = load(folder)
    errors = [f"{f['path']} hash mismatch" for f in m["files"]
              if not (folder / f["path"]).exists()
              or _snapshot.sha256_file(folder / f["path"]) != f["sha256"]]
    if root_hash(m["files"]) != m["root_hash"]:
        errors.append("root hash mismatch")
    return errors


def daily_symbols(version_id: str) -> list[str]:
    """The symbols of a published daily version (its partition index)."""
    m = _snapshot.resolve_version(version_id)
    index = pd.read_parquet(_snapshot._object_path(_snapshot.DEFAULT_ROOT, m["index_sha256"]))
    return sorted(index.symbol.unique())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="signal_engine.backtest.data_readiness.intraday_freeze")
    ap.add_argument("--daily-version", required=True)
    ap.add_argument("--nse-raw", required=True, type=Path)
    ap.add_argument("--live-db", type=Path, default=_REPO / _data.HISTORIFY_DB)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    folder = freeze(args.live_db, args.out, daily_symbols(args.daily_version), args.nse_raw,
                    args.daily_version)
    print(folder)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
