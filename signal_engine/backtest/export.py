"""Export backtest results for analysis outside the OpenAlgo UI.

The run record under `research/ideas/<idea>/runs/<run_id>.json` is the canonical result:
this module never invents a second format. It derives flat CSV views from a record, and
per-trade rows from a fresh execution (trade lists are not stored in records, so they
cannot be rebuilt from one).

    uv run python -m signal_engine.backtest.export --run research/ideas/<idea>/runs/<id>.json
    uv run python -m signal_engine.backtest.export --idea IDEA-023 --gzip

Output goes to `data/exports/` by default, which is gitignored: a per-trade file can hold
hundreds of thousands of rows, and it must not match the `runs/*.json` pattern the
registry reads.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from signal_engine.backtest import record_run as rr
from signal_engine.backtest.metrics import trades_frame
from signal_engine.research.build_registry import _parse_frontmatter
from utils.logging import get_logger

logger = get_logger(__name__)

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "data" / "exports"

#: Record sections that hold a flat mapping of metric name to value.
_METRIC_SECTIONS = ("metrics", "in_sample", "out_of_sample")


def metrics_rows(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a record's metric blocks into `section, metric, value` rows."""
    rows: list[dict[str, Any]] = []
    for section in _METRIC_SECTIONS:
        block = record.get(section)
        if isinstance(block, dict):
            rows.extend({"section": section, "metric": k, "value": v} for k, v in block.items())
    for cap in record.get("position_caps") or []:
        section = f"position_caps.{cap['max_positions']}"
        rows.extend(
            {"section": section, "metric": k, "value": v}
            for k, v in cap.items()
            if k != "max_positions"
        )
    return rows


def series_rows(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Equity and drawdown curves joined on date, in calendar order."""
    series = record.get("series") or {}
    equity = dict(series.get("equity_r") or [])
    drawdown = dict(series.get("drawdown_r") or [])
    return [
        {"date": d, "equity_r": equity.get(d), "drawdown_r": drawdown.get(d)}
        for d in sorted(equity.keys() | drawdown.keys())
    ]


def trade_rows(trades: Iterable) -> list[dict[str, Any]]:
    """Per-trade rows in the order the trades closed, with ISO string dates."""
    frame = trades_frame(list(trades))
    if frame.empty:
        return []
    for col in ("day", "entry_time", "exit_time"):
        frame[col] = [pd.Timestamp(v).isoformat() for v in frame[col]]
    frame["day"] = frame["day"].str[:10]
    return frame.to_dict("records")


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: Sequence[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=list(columns)).to_csv(path, index=False)
    return path


def write_trades(trades: Iterable, path: Path) -> Path:
    """Write per-trade rows to `path`; a `.gz` suffix compresses the file."""
    rows = trade_rows(trades)
    if not rows:
        raise ValueError("no trades to export")
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def export_record(record_path: Path, out_dir: Path) -> dict[str, Path]:
    """Copy a run record to `out_dir` and derive its metrics and series CSVs."""
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"{record_path} is not valid JSON: {exc}") from exc
    run_id = record["run_id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{run_id}.json"
    shutil.copyfile(record_path, target)
    return {
        "json": target,
        "metrics": _write_csv(
            out_dir / f"{run_id}.metrics.csv",
            metrics_rows(record),
            ("section", "metric", "value"),
        ),
        "series": _write_csv(
            out_dir / f"{run_id}.series.csv",
            series_rows(record),
            ("date", "equity_r", "drawdown_r"),
        ),
    }


def export_idea_trades(
    root: Path,
    idea_id: str,
    out_dir: Path,
    *,
    execute: Callable[[str], rr.Execution],
    supported: Iterable[str],
    gzip_output: bool,
) -> Path:
    """Re-execute an idea's backtest and write its per-trade rows.

    Raises KeyError for an unknown idea, rr.Unsupported when it has no trade-list engine,
    and ValueError when the run produced no trades.
    """
    folders = rr.idea_folders(root)
    if idea_id not in folders:
        raise KeyError(idea_id)
    idea = _parse_frontmatter(folders[idea_id] / "idea.md")
    key = rr.strategy_key(idea, supported)
    if key is None:
        raise rr.Unsupported(f"{idea_id}: no trade-list engine for {idea.get('backtest_module')!r}")
    execution = execute(key)
    suffix = ".trades.csv.gz" if gzip_output else ".trades.csv"
    return write_trades(execution.trades, out_dir / f"{idea_id}{suffix}")


def main(
    argv: Sequence[str] | None = None,
    *,
    execute: Callable[[str], rr.Execution] | None = None,
    supported: Iterable[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="signal_engine.backtest.export")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--run", type=Path, help="a stored run record (.json)")
    target.add_argument("--idea", help="re-run an idea and export per-trade rows, e.g. IDEA-023")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory")
    parser.add_argument("--gzip", action="store_true", help="compress the per-trade file")
    args = parser.parse_args(argv)

    try:
        if args.run is not None:
            written = list(export_record(args.run, args.out).values())
        else:
            keys = set(supported) if supported is not None else rr.supported_keys()
            written = [
                export_idea_trades(
                    args.root,
                    args.idea,
                    args.out,
                    execute=execute or rr.execute_backtest,
                    supported=keys,
                    gzip_output=args.gzip,
                )
            ]
    except (OSError, KeyError, ValueError, rr.Unsupported):
        logger.exception("Export failed")
        return 1
    for path in written:
        sys.stdout.write(f"wrote {path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
