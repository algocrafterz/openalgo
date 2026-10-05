"""De-duplicate the daily bars in the Historify store, and record the change as a revision.

A daily bar is stamped 05:30 IST (midnight UTC). Some symbols also hold copies of the same session
stamped at 11:00 and 16:30 IST (re-downloads made after the session).
`from_historify_daily` normalises to the IST date and keeps the LAST row per date, so a late copy -
which can be a near-empty fragment - displaces the real bar.

The rule here is data-only and independent of any strategy: on an IST date that has a canonically
stamped bar, every other `D` row of that symbol and date is removed. A date with no canonical bar is
left alone and reported. Every removed row is written to a CSV next to a JSON manifest holding the
rule and the counts, so the change can be audited or undone. Removing rows changes the dataset
fingerprint, so a run on the revised store is a run on a new dataset.

    uv run python -m signal_engine.backtest.data_revision            # dry run
    uv run python -m signal_engine.backtest.data_revision --apply
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb
import pandas as pd

from signal_engine.backtest import data as _data

#: IST time of day of a canonical daily bar. Times are formatted in IST whatever the DuckDB session
#: time zone is.
CANONICAL_TIME = "05:30"

RULE = (
    "interval D: on an IST date that has a bar stamped 05:30 IST, remove the symbol's other D rows "
    "for that date; dates without a 05:30 bar are not touched"
)

_KEYS = "symbol, exchange, timestamp"

_PLAN_SQL = f"""
WITH d AS (
    SELECT symbol, exchange, timestamp, open, high, low, close, volume,
           CAST(timezone('Asia/Kolkata', to_timestamp(timestamp)) AS DATE) AS ist_date,
           strftime(timezone('Asia/Kolkata', to_timestamp(timestamp)), '%H:%M') AS ist_time
    FROM market_data WHERE interval = 'D'
),
keep AS (
    SELECT symbol, exchange, ist_date, timestamp AS kept_timestamp,
           open AS k_open, high AS k_high, low AS k_low, close AS k_close, volume AS k_volume
    FROM d WHERE ist_time = '{CANONICAL_TIME}'
)
SELECT d.symbol, d.exchange, d.timestamp, d.open, d.high, d.low, d.close, d.volume,
       d.ist_date, d.ist_time, keep.kept_timestamp,
       NOT (d.open = k_open AND d.high = k_high AND d.low = k_low AND d.close = k_close
            AND d.volume = k_volume) AS differs
FROM d JOIN keep USING (symbol, exchange, ist_date)
WHERE d.ist_time <> '{CANONICAL_TIME}'
ORDER BY d.symbol, d.timestamp
"""

_UNRESOLVED_SQL = f"""
WITH d AS (
    SELECT symbol, exchange,
           CAST(timezone('Asia/Kolkata', to_timestamp(timestamp)) AS DATE) AS ist_date,
           strftime(timezone('Asia/Kolkata', to_timestamp(timestamp)), '%H:%M') AS ist_time
    FROM market_data WHERE interval = 'D'
)
SELECT symbol, exchange, CAST(ist_date AS VARCHAR) FROM d
GROUP BY symbol, exchange, ist_date
HAVING COUNT(*) > 1 AND SUM(CASE WHEN ist_time = '{CANONICAL_TIME}' THEN 1 ELSE 0 END) = 0
ORDER BY 1, 3
"""


def _connect(db_path, read_only: bool):
    return duckdb.connect(
        str(db_path or _data.HISTORIFY_DB), read_only=read_only, config=_data._DUCKDB_CONFIG
    )


def plan_daily_dedup(db_path=None) -> pd.DataFrame:
    """The rows the rule would remove, with whether each differs from the row that is kept."""
    con = _connect(db_path, True)
    try:
        return con.execute(_PLAN_SQL).df()
    finally:
        con.close()


def unresolved_dates(db_path=None) -> list[tuple[str, str, str]]:
    """Symbol-dates with several D rows and no canonical one: reported, never modified."""
    con = _connect(db_path, True)
    try:
        return [tuple(r) for r in con.execute(_UNRESOLVED_SQL).fetchall()]
    finally:
        con.close()


def apply_daily_dedup(
    db_path, out_dir, *, name: str, applied_at: str, dry_run: bool = False
) -> dict:
    """Remove the planned rows, after writing them and a manifest to `out_dir`.

    `applied_at` is an IST label supplied by the caller. A dry run reports the same manifest and
    writes and deletes nothing.
    """
    plan = plan_daily_dedup(db_path)
    unresolved = unresolved_dates(db_path)
    con = _connect(db_path, True)
    try:
        rows_before = con.execute("SELECT COUNT(*) FROM market_data").fetchone()[0]
    finally:
        con.close()
    by_symbol = {k: int(v) for k, v in plan.groupby("symbol").size().items()}
    manifest = {
        "name": name,
        "rule": RULE,
        "applied_at": applied_at,
        "dry_run": dry_run,
        "rows_before": int(rows_before),
        "rows_after": int(rows_before) - len(plan),
        "removed_rows": int(len(plan)),
        "removed_differing_rows": int(plan["differs"].sum()) if len(plan) else 0,
        "by_symbol": by_symbol,
        "unresolved_dates": [list(u) for u in unresolved],
    }
    if dry_run or plan.empty:
        return manifest
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    plan.assign(ist_date=plan["ist_date"].astype(str)).to_csv(
        out / f"{name}-removed-rows.csv", index=False
    )
    (out / f"{name}.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    con = _connect(db_path, False)
    try:
        con.register("doomed", plan[["symbol", "exchange", "timestamp"]])
        con.execute(
            "DELETE FROM market_data WHERE interval = 'D' AND (symbol, exchange, timestamp) IN "
            "(SELECT symbol, exchange, timestamp FROM doomed)"
        )
        manifest["rows_after"] = int(con.execute("SELECT COUNT(*) FROM market_data").fetchone()[0])
    finally:
        con.close()
    if manifest["rows_before"] - manifest["rows_after"] != len(plan):
        raise RuntimeError("de-duplication removed an unexpected number of rows")
    (out / f"{name}.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="delete the rows (default is a dry run)")
    ap.add_argument("--db", default=None)
    ap.add_argument("--out", default="signal_engine/research/data_revisions")
    ap.add_argument("--name", default="historify-daily-dedup")
    args = ap.parse_args(argv)
    now = pd.Timestamp.now(tz="Asia/Kolkata").strftime("%Y-%m-%d %H:%M IST")
    m = apply_daily_dedup(args.db, args.out, name=args.name, applied_at=now, dry_run=not args.apply)
    print(json.dumps({k: v for k, v in m.items() if k != "unresolved_dates"}, indent=2))
    print(f"unresolved dates: {len(m['unresolved_dates'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
