"""Rebuild the daily portfolio of a sealed fixed-notional run, and re-score the gates it changes.

A run recorded before `record_run` captured the portfolio book has no portfolio return or
drawdown, and its G2.3, G2.6 and G2.7 were computed on each trade's whole result booked on its
entry session (for a monthly ranked book: one observation per rebalance). Its G3.2 compared
out-of-sample against an in-sample set that still held the trades purged at the cut.

This reads only frozen inputs: the run's `evidence/trades.parquet`, the published dataset
version the manifest names (its root hash is checked) and the official calendar (its version
must equal the manifest's). It writes two sidecars into `evaluations/<run_id>/` of the idea and
never touches the run folder or the run record:

  <stamp>-portfolio.json       kind reconstructed_portfolio: book, statistics, session counts
  <stamp>-session-gates.json   kind corrected_gate_evaluation: G2.3, G2.6, G2.7, G3.2 as
                               recorded and as re-scored by the current evaluator

No trial is counted, no threshold changes and the run verdict is not rewritten.

    uv run python -m signal_engine.backtest.reconstruct_portfolio <run.json> [--write]
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from signal_engine.backtest import book_series
from signal_engine.backtest.types import Trade
from signal_engine.timeutils import IST

#: The gates this module re-scores; every other recorded row stays the sealed run's.
RESCORED = ("G2.3", "G2.6", "G2.7", "G3.2")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ist_date(value: Any):
    return pd.Timestamp(value).tz_convert(IST).date()


def trades_from_ledger(frame: pd.DataFrame) -> list[Trade]:
    """`Trade` objects from a no-stop ledger, so the evaluator scores the sealed trades."""
    if frame["gross_r"].notna().any():
        raise book_series.BookError(
            "the ledger has stop-based trades; only no-stop fixed-notional runs are rebuilt"
        )
    trades = []
    for row in frame.itertuples(index=False):
        trades.append(
            Trade(
                symbol=row.symbol,
                day=pd.Timestamp(_ist_date(row.entry_time)),
                direction=-1 if row.side == "SELL" else 1,
                tag="ledger",
                entry_time=row.entry_time,
                entry=float(row.entry_price),
                signal_price=float(row.entry_price),
                sl=math.nan,
                tp=math.nan,
                risk=math.nan,
                exit_time=row.exit_time,
                exit=float(row.exit_price),
                reason=str(row.exit_reason),
                r_gross=math.nan,
                r_net=math.nan,
                gross_ret=float(row.gross_bps) / 1e4,
                net_ret=float(row.net_bps) / 1e4,
            )
        )
    return trades


def load_closes(manifest: dict[str, Any], symbols: Sequence[str], end: str) -> pd.DataFrame:
    """Adjusted daily closes of `symbols` from the run's own published dataset version."""
    from signal_engine.backtest import data
    from signal_engine.backtest.data_readiness import snapshot

    dataset = manifest["dataset"]
    with snapshot.open_version(
        dataset["version_id"], symbols=list(symbols), expected_root=dataset["root_hash"]
    ) as con:
        frames = data.from_published_daily(con, symbols=list(symbols), end=end, min_sessions=0)
    closes = pd.DataFrame({s: f["Close"] for s, f in frames.items()})
    closes.index = [_ist_date(i) for i in closes.index]
    return closes


def _official_calendar(manifest: dict[str, Any]) -> Any:
    from signal_engine.backtest.data_readiness.calendar import load_calendar

    calendar = load_calendar()
    recorded = manifest["dataset"]["calendar_version"]
    if calendar.version != recorded:
        raise book_series.BookError(
            f"official calendar is {calendar.version[:12]}, the run used {recorded[:12]}"
        )
    return calendar


def _slots(record: dict[str, Any]) -> int:
    slots = ((record.get("params") or {}).get("strategy") or {}).get("max_positions")
    if not slots:
        raise book_series.BookError("the run declares no max_positions, so no book size")
    return int(slots)


def _rows(evaluation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["gate"]: r for d in evaluation["dimensions"] for r in d["rows"]}


def _rescore(record: dict[str, Any], trades: list[Trade], returns: Sequence[float]) -> dict:
    from signal_engine.backtest.evaluation import evaluate

    # Only the inputs RESCORED reads are passed; every other row is kept from the record.
    split = record.get("split") or {}
    profile = evaluate(
        trades,
        metrics=record["metrics"],
        n_trials=record["n_trials"],
        horizon=record["evaluation"]["horizon"],
        cost_bps=record["costs"]["round_trip_bps"],
        cost_model=record["costs"].get("model"),
        oos_start=split.get("oos_start"),
        position_caps=record.get("position_caps"),
        params=record.get("params"),
        split=split or None,
        run_date=record.get("date"),
        portfolio_returns=list(returns),
    )
    recorded, corrected = _rows(record["evaluation"]), _rows(profile)
    keys = ("status", "value", "threshold", "evidence")
    return {
        g: {"recorded": recorded[g], "corrected": {k: corrected[g][k] for k in keys}}
        for g in RESCORED
    }


def reconstruct(
    run_json: Path,
    *,
    closes_loader: Callable[..., pd.DataFrame] = load_closes,
    calendar: Any = None,
    now: datetime | None = None,
) -> tuple[dict, dict]:
    """The two sidecars for the sealed run at `run_json` (its folder sits beside it)."""
    from signal_engine.backtest.trades_io import read_trades_parquet

    run_dir = run_json.with_suffix("")
    record = json.loads(run_json.read_text())
    manifest = json.loads((run_dir / "manifest.json").read_text())
    trades_path = run_dir / "evidence" / "trades.parquet"
    frame = read_trades_parquet(trades_path)
    notional = (record.get("cost_model") or {}).get("fixed_notional")
    if not notional:
        raise book_series.BookError("the run has no fixed notional per position")
    calendar = calendar or _official_calendar(manifest)
    first = min(_ist_date(t) for t in frame["entry_time"])
    end = record["period"]["end"]
    sessions = list(calendar.expected_sessions(first, pd.Timestamp(end).date()))
    closes = closes_loader(manifest, sorted(frame["symbol"].unique()), end)
    block, returns = book_series.portfolio_block(
        frame,
        closes,
        sessions=sessions,
        notional=float(notional),
        slots=_slots(record),
        cost_column="cost_bps",
        alternate_cost_column="modeled_cost_bps",
    )
    head = {
        "run_id": record["run_id"],
        "evaluated_ist": (now or datetime.now(IST)).isoformat(),
        "new_trial": False,
        "sealed_run_unchanged": {
            "record_sha256": _sha256(run_json),
            "trades_sha256": _sha256(trades_path),
        },
        "inputs": {
            "trades": f"runs/{record['run_id']}/evidence/trades.parquet",
            "trades_count": len(frame),
            "dataset_version": manifest["dataset"]["version_id"],
            "dataset_root_hash": manifest["dataset"]["root_hash"],
            "calendar_version": manifest["dataset"]["calendar_version"],
        },
    }
    portfolio = {
        "kind": "reconstructed_portfolio",
        **head,
        "reason": (
            "the run was recorded before portfolio series were captured; its "
            "equity_bps is a running sum of per-trade bps, not an account"
        ),
        "cost_basis": (
            f"headline: the ledger's cost_bps ({record['costs']['model']}, "
            f"{record['costs']['round_trip_bps']:g} bps round trip); alternate: "
            "modeled_cost_bps (statutory charges plus slippage)"
        ),
        "session_counts": {
            "recorded_days": record["metrics"].get("days"),
            "recorded_days_meaning": (
                "distinct entry sessions: the record groups each trade's "
                "whole result on the session it opened"
            ),
            "entry_sessions": int(frame["entry_time"].map(_ist_date).nunique()),
            "test_sessions": block["stats"]["sessions"],
            "sessions_with_pnl": block["stats"]["sessions_with_pnl"],
            "sessions_with_position": block["stats"]["sessions_with_position"],
        },
        "portfolio": block,
    }
    gates = {
        "kind": "corrected_gate_evaluation",
        **head,
        "reason": (
            "G2.3, G2.6 and G2.7 re-scored on the daily marked-to-market portfolio "
            "returns over every session instead of trade results grouped by entry "
            "session; G3.2 compares out-of-sample with the purged in-sample set the "
            "record's in_sample block reports. No threshold, split or trade changed."
        ),
        "scope": f"{', '.join(RESCORED)} only; every other row is the sealed run's, unchanged",
        "gates": _rescore(record, trades_from_ledger(frame), returns),
    }
    return portfolio, gates


def write(run_json: Path, portfolio: dict, gates: dict) -> list[Path]:
    """Write both sidecars under the idea's `evaluations/<run_id>/`. Refuses to overwrite."""
    folder = run_json.parent.parent / "evaluations" / portfolio["run_id"]
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(portfolio["evaluated_ist"]).strftime("%Y%m%dT%H%M%S-ist")
    out = []
    for name, doc in (("portfolio", portfolio), ("session-gates", gates)):
        path = folder / f"{stamp}-{name}.json"
        with path.open("x") as fh:
            fh.write(json.dumps(doc, indent=2, allow_nan=False) + "\n")
        out.append(path)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("run_json", type=Path, help="research/ideas/<idea>/runs/<run_id>.json")
    p.add_argument("--write", action="store_true", help="write the sidecars (default: show)")
    args = p.parse_args(argv)
    portfolio, gates = reconstruct(args.run_json)
    summary = {
        "stats": portfolio["portfolio"]["stats"],
        "reconciliation": portfolio["portfolio"]["reconciliation"],
        "session_counts": portfolio["session_counts"],
        "gates": {g: v["corrected"]["status"] for g, v in gates["gates"].items()},
    }
    sys.stdout.write(json.dumps(summary, indent=1) + "\n")
    if args.write:
        for path in write(args.run_json, portfolio, gates):
            sys.stdout.write(f"wrote {path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
