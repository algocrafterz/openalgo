"""Cost stress and symbol/year contribution: report-only diagnostics on a recorded trade list.

Design (owner-approved 2026-10-09):

  SOURCE.     The recorded `evidence/trades.parquet` of one run. Nothing is re-simulated, no
              strategy is run, no trial is created, and the run record is never modified.
  COST STRESS. Net R per trade at 1.0x, 1.5x and 2.0x of the recorded cost, in two readings:
              `all_costs` scales every modelled cost component (statutory charges and
              slippage), `statutory_only` scales the statutory charges and keeps slippage at
              1.0x. Cost in R is the recorded `gross_r - net_r`, split by the recorded bps
              components, so the 1.0x row must equal the recorded result (checked, and stored as
              `reconciliation`).
  CONTRIBUTION. Net R by symbol and by IST entry year. The denominator of every share is the
              aggregate (sum) net R of all trades with a defined R, stated in the block. When
              that aggregate is negative a positive share means the group added to the loss, so
              each row also carries `share_of_abs_total` (denominator: sum of |net R|), which
              is always defined. A zero aggregate gives no share at all. Removal tests drop the
              best symbol or year (highest total net R) and recompute the rest. They are
              descriptive and say nothing about future performance.
  GATING.     None. The block is report only and changes no gate.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from signal_engine.timeutils import IST

SCHEMA = "cost_contribution/1"
MULTIPLIERS = (1.0, 1.5, 2.0)
TOP_N = 5
_COMPONENTS = (
    "cost_brokerage_bps",
    "cost_stt_bps",
    "cost_exchange_bps",
    "cost_gst_bps",
    "cost_sebi_bps",
    "cost_stamp_bps",
    "cost_slippage_bps",
)
_REQUIRED = (
    "symbol",
    "entry_time",
    "gross_r",
    "net_r",
    "gross_bps",
    "net_bps",
    "cost_bps",
    *_COMPONENTS,
)


class CostContributionError(ValueError):
    """The trade list cannot support the diagnostic."""


def _check(trades: pd.DataFrame) -> None:
    missing = [c for c in _REQUIRED if c not in trades.columns]
    if missing:
        raise CostContributionError(f"trade list lacks columns: {', '.join(missing)}")
    if trades.empty:
        raise CostContributionError("trade list is empty")


def _num(x: float) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), 6)


def _stress_row(t: pd.DataFrame, mult: float, scale_slippage: bool) -> dict[str, Any]:
    """Result at `mult` x the recorded cost. `t` holds only trades with a defined R."""
    cost_r = (t["gross_r"] - t["net_r"]).to_numpy(float)
    cost_bps = t["cost_bps"].to_numpy(float)
    if scale_slippage:
        factor = np.full(len(t), mult)
    else:
        slip_bps = t["cost_slippage_bps"].to_numpy(float)
        with np.errstate(divide="ignore", invalid="ignore"):
            slip_share = np.where(cost_bps > 0, slip_bps / cost_bps, 0.0)
        factor = mult * (1.0 - slip_share) + slip_share
    net_r = t["gross_r"].to_numpy(float) - cost_r * factor
    net_bps = t["gross_bps"].to_numpy(float) - cost_bps * factor
    return {
        "multiplier": mult,
        "trades": int(len(t)),
        "net_r_per_trade": _num(net_r.mean()),
        "total_net_r": _num(net_r.sum()),
        "net_bps_per_trade": _num(net_bps.mean()),
        "win_rate": _num((net_r > 0).mean()),
        "mean_cost_bps": _num((cost_bps * factor).mean()),
    }


def cost_stress(trades: pd.DataFrame) -> dict[str, Any]:
    """Net R, net bps and trade count at each cost multiplier, plus the 1.0x reconciliation."""
    _check(trades)
    defined = trades[trades["net_r"].notna() & trades["gross_r"].notna()]
    comp_sum = trades[list(_COMPONENTS)].sum(axis=1)
    out: dict[str, Any] = {
        "trades_total": int(len(trades)),
        "trades_with_defined_r": int(len(defined)),
        "metric": "net R per trade (primary), net bps per trade, trade count",
        "readings": {
            "all_costs": "every modelled cost component scaled, slippage included",
            "statutory_only": "statutory charges scaled, slippage kept at 1.0x",
        },
        "statutory_basis": (
            "slippage is the recorded cost_slippage_bps; statutory is the recorded cost_bps "
            "minus slippage, so the 1.0x row equals the recorded result"
        ),
        "component_sum_gap_bps": {
            "max": _num((comp_sum - trades["cost_bps"]).abs().max()),
            "mean": _num((comp_sum - trades["cost_bps"]).abs().mean()),
            "note": "modelled component sum versus the recorded cost_bps used for net R",
        },
    }
    for name, scale_slip in (("all_costs", True), ("statutory_only", False)):
        out[name] = [_stress_row(defined, m, scale_slip) for m in MULTIPLIERS]
    base = out["all_costs"][0]
    recorded_r = float(defined["net_r"].mean())
    recorded_bps = float(defined["net_bps"].mean())
    out["reconciliation"] = {
        "recorded_net_r_per_trade": _num(recorded_r),
        "recomputed_net_r_per_trade": base["net_r_per_trade"],
        "recorded_net_bps_per_trade": _num(recorded_bps),
        "recomputed_net_bps_per_trade": base["net_bps_per_trade"],
        "matches": bool(
            abs(recorded_r - base["net_r_per_trade"]) <= 1e-5
            and abs(recorded_bps - base["net_bps_per_trade"]) <= 1e-5
            and out["statutory_only"][0]["net_r_per_trade"] == base["net_r_per_trade"]
        ),
    }
    return out


def _group_table(t: pd.DataFrame, key: str, aggregate: float) -> list[dict[str, Any]]:
    abs_total = float(t["net_r"].abs().sum())
    g = t.groupby(key)["net_r"].agg(["sum", "mean", "count"]).reset_index()
    rows = []
    for rec in g.sort_values("sum", ascending=False).itertuples(index=False):
        total = float(rec[1])
        rows.append(
            {
                key: rec[0] if key == "symbol" else int(rec[0]),
                "trades": int(rec[3]),
                "total_net_r": _num(total),
                "net_r_per_trade": _num(rec[2]),
                "share_of_aggregate": _num(total / aggregate) if aggregate != 0 else None,
                "share_of_abs_total": _num(total / abs_total) if abs_total > 0 else None,
            }
        )
    return rows


def _removal(t: pd.DataFrame, key: str, value: Any) -> dict[str, Any]:
    rest = t[t[key] != value]
    return {
        "removed": value,
        "remaining_trades": int(len(rest)),
        "remaining_total_net_r": _num(rest["net_r"].sum()) if len(rest) else None,
        "remaining_net_r_per_trade": _num(rest["net_r"].mean()) if len(rest) else None,
    }


def contribution(trades: pd.DataFrame) -> dict[str, Any]:
    """Net R by symbol and by IST entry year, with shares, top-five and removal tests."""
    _check(trades)
    t = trades[trades["net_r"].notna()].copy()
    if t.empty:
        raise CostContributionError("no trade has a defined net R")
    t["year"] = pd.to_datetime(t["entry_time"], utc=True).dt.tz_convert(IST).dt.year
    aggregate = float(t["net_r"].sum())
    if aggregate < 0:
        note = (
            "aggregate is negative: a positive share means the group added to the loss and a "
            "negative share means it offset the loss; read total_net_r and share_of_abs_total "
            "alongside it"
        )
    elif aggregate == 0:
        note = "aggregate is zero: shares of the aggregate are undefined"
    else:
        note = "aggregate is positive: a share is the group's part of the profit"
    out: dict[str, Any] = {
        "trades": int(len(t)),
        "aggregate_total_net_r": _num(aggregate),
        "aggregate_net_r_per_trade": _num(t["net_r"].mean()),
        "share_denominator": "aggregate_total_net_r (sum of net R over all trades with a defined R)",
        "aggregate_negative": bool(aggregate < 0),
        "share_note": note,
        "share_of_abs_total_denominator": "sum of |net R| over the same trades",
    }
    for name, key in (("by_symbol", "symbol"), ("by_year", "year")):
        table = _group_table(t, key, aggregate)
        out[name] = table
        out[f"{name}_groups"] = len(table)
        best = table[0]
        removal = _removal(t, key, best[key])
        removal["best_total_net_r"] = best["total_net_r"]
        out[f"remove_best_{key}"] = removal
    top = out["by_symbol"][:TOP_N]
    top_total = float(sum(r["total_net_r"] for r in top))
    out["top_five_symbols"] = {
        "symbols": [r["symbol"] for r in top],
        "total_net_r": _num(top_total),
        "share_of_aggregate": _num(top_total / aggregate) if aggregate != 0 else None,
        "ranking": "highest total net R first",
    }
    out["symbols_positive"] = int(sum(1 for r in out["by_symbol"] if r["total_net_r"] > 0))
    out["years_positive"] = int(sum(1 for r in out["by_year"] if r["total_net_r"] > 0))
    return out


def seal(body: dict[str, Any]) -> str:
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def build_evidence(trades: pd.DataFrame, source: dict[str, Any]) -> dict[str, Any]:
    """The sealed `evidence/cost_stress.json` payload. `source` names the run it came from."""
    body = {
        "schema": SCHEMA,
        "report_only": True,
        "diagnostic_not_trials": True,
        "source": source,
        "cost_stress": cost_stress(trades),
        "contribution": contribution(trades),
    }
    return {**body, "content_sha256": seal(body)}


def validate_evidence(evidence: dict[str, Any]) -> list[str]:
    """Problems found in a stored payload; empty when it is sound and its seal holds."""
    problems = []
    if evidence.get("schema") != SCHEMA:
        problems.append(f"schema is {evidence.get('schema')!r}, expected {SCHEMA!r}")
    body = {k: v for k, v in evidence.items() if k != "content_sha256"}
    if evidence.get("content_sha256") != seal(body):
        problems.append("content_sha256 does not match the payload")
    if evidence.get("report_only") is not True:
        problems.append("report_only must be true")
    if evidence.get("diagnostic_not_trials") is not True:
        problems.append("diagnostic_not_trials must be true")
    if not evidence.get("cost_stress", {}).get("reconciliation", {}).get("matches"):
        problems.append("the 1.0x result does not reconcile with the recorded trades")
    return problems


def evidence_for_run(run_dir: str | Path) -> dict[str, Any]:
    """Build the payload from a recorded run folder, reading its trades and manifest only."""
    run = Path(run_dir)
    path = run / "evidence" / "trades.parquet"
    manifest = run / "manifest.json"
    source = {
        "run_dir": run.name,
        "trades_parquet_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()
        if manifest.exists()
        else None,
    }
    return build_evidence(pd.read_parquet(path), source)


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(description="Cost stress and contribution on a recorded run")
    p.add_argument("run_dir")
    p.add_argument("--out", required=True, help="where to write cost_stress.json")
    args = p.parse_args(argv)
    evidence = evidence_for_run(args.run_dir)
    problems = validate_evidence(evidence)
    Path(args.out).write_text(json.dumps(evidence, indent=1, allow_nan=False))
    sys.stdout.write(f"wrote {args.out}; problems: {problems or 'none'}\n")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
