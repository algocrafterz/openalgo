"""G5.1 Monte Carlo risk diagnostic on a recorded trade list: read-only, report only.

Design (owner-approved 2026-10-09):

  SOURCE.     A run's `evidence/trades.parquet`. Nothing is re-simulated, no gate or verdict is
              changed, no trial is created and the run record is never written.
  UNITS.      r_i is the net R of trade i at the stated cost multiplier (1.0 = recorded cost).
              R_d is the sum of r_i over the trades entered on IST session d.
  RESAMPLING. All methods build paths of T sessions (T = observed sessions) and N_PATHS paths.
                day       draw T sessions with replacement; a drawn session keeps all its trades
                          together (primary).
                block_L   circular block bootstrap of consecutive sessions, block length L.
                trade     keep each session's trade count and draw its trades iid from the whole
                          pool (secondary; breaks the dependence between same-day trades).
                recent    day bootstrap over the sessions of 2024 to 2026 only.
              Sessions are assumed exchangeable. Regimes, volatility clustering and extreme days
              enter only as the sample holds them.
  C1.         Normalised sequence of trades, no account: S_k = sum of R_j for j <= k, additive, in
              R. Reported: S_T, max drawdown max_k(max_{j<=k} S_j - S_k) in R (S_0 = 0), and
              P(S_T < 0).
  C2.         Account model, conditional on f, the share of day-start equity risked per R:
                E_0 = 1,  E_d = E_{d-1} * max(0, 1 + f * R_d)        (compounded daily).
              Ruin: min_d E_d <= 1 - theta (the boundary counts), theta = 0.25. Drawdown from the
              running peak is reported separately. Equity is evaluated at session end, so the
              intraday path is ignored. The rupee scale cancels, so no capital is needed.
  EXPOSURE.   Portfolio caps are NOT applied: the recorded trades had no portfolio-wide cap. The
              implied simultaneous risk, concurrent positions times f, is reported.
  NOT FACTS.  Results are conditional on the resampling model, f, theta and the compounding
              rule. They are not live-account ruin probabilities and say nothing about G5.2.
              A fixed seed and many paths remove sampling noise, not model uncertainty.
  GATING.     Standalone, the 25% drawdown and 5% ruin figures of G5.1 are shown as reference only.
              `attached_evidence` (opt-in `--monte-carlo` recording) binds the same report to one
              run's trades and risk per trade, and the G5.1 evaluator scores it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from signal_engine.backtest.cost_contribution import seal
from signal_engine.backtest.exposure_turnover import occupancy
from signal_engine.timeutils import IST

SCHEMA = "monte_carlo/1"
SEED = 20261009
N_PATHS = 10_000
CHUNK = 500
TRADE_CHUNK = 100
THETA = 0.25
RISK_FRACTIONS = (0.0025, 0.005, 0.01)
DEPLOYED_F = 0.01
BLOCK_LENGTHS = (5, 20)
COST_MULTIPLIERS = (1.5, 2.0)
RECENT_FROM = 2024
PERCENTILES = (5, 25, 50, 75, 95)
EPS = 1e-12
DD_REFERENCE = 0.25
RUIN_REFERENCE = 0.05
REQUIRED = ("symbol", "side", "entry_time", "exit_time", "gross_r", "net_r")


class MonteCarloError(ValueError):
    """The inputs cannot support the simulation."""


def _num(x: Any) -> float | None:
    return None if x is None or not np.isfinite(x) else round(float(x), 6)


# ---- sessions -------------------------------------------------------------------------------


class Sessions:
    """Trades grouped by IST entry session, in chronological order."""

    def __init__(self, trades: pd.DataFrame, cost_multiplier: float = 1.0):
        missing = [c for c in REQUIRED if c not in trades.columns]
        if missing:
            raise MonteCarloError(f"trade list lacks columns: {', '.join(missing)}")
        t = trades[trades["net_r"].notna() & trades["gross_r"].notna()]
        self.excluded_undefined_r = int(len(trades) - len(t))
        if t.empty:
            raise MonteCarloError("no trade has a defined R")
        entry = pd.to_datetime(t["entry_time"], utc=True)
        order = np.argsort(entry.to_numpy(), kind="stable")
        t, entry = t.iloc[order], entry.iloc[order]
        ist = entry.dt.tz_convert(IST)
        r = t["gross_r"] - cost_multiplier * (t["gross_r"] - t["net_r"])
        frame = pd.DataFrame(
            {"day": ist.dt.date.to_numpy(), "year": ist.dt.year.to_numpy(), "r": r.to_numpy()}
        )
        grouped = frame.groupby("day", sort=True)
        self.days = np.array(list(grouped.groups.keys()))
        self.day_r = grouped["r"].sum().to_numpy(float)
        self.counts = grouped["r"].size().to_numpy(int)
        self.year = grouped["year"].first().to_numpy(int)
        self.trade_r = frame.sort_values("day", kind="stable")["r"].to_numpy(float)
        self.starts = np.concatenate([[0], np.cumsum(self.counts)[:-1]])

    @property
    def n_days(self) -> int:
        return len(self.day_r)

    @property
    def n_trades(self) -> int:
        return len(self.trade_r)


# ---- path statistics (deterministic) ----------------------------------------------------------


def additive_stats(paths: np.ndarray) -> dict[str, np.ndarray]:
    """C1 on a (n_paths, T) matrix of session R: final S_T and max drawdown in R."""
    s = np.concatenate([np.zeros((paths.shape[0], 1)), np.cumsum(paths, axis=1)], axis=1)
    peak = np.maximum.accumulate(s, axis=1)
    return {"final": s[:, -1], "max_dd": (peak - s).max(axis=1)}


def equity_stats(paths: np.ndarray, f: float, theta: float = THETA) -> dict[str, np.ndarray]:
    """C2 on a (n_paths, T) matrix of session R: final equity, max drawdown and ruin flag."""
    growth = np.maximum(1.0 + f * paths, 0.0)
    e = np.concatenate([np.ones((paths.shape[0], 1)), np.cumprod(growth, axis=1)], axis=1)
    peak = np.maximum.accumulate(e, axis=1)
    return {
        "final": e[:, -1],
        "max_dd": ((peak - e) / peak).max(axis=1),
        "min_equity": e.min(axis=1),
        "ruin": e.min(axis=1) <= (1.0 - theta) + EPS,
    }


# ---- bootstrap generators -----------------------------------------------------------------------


def day_paths(
    rng: np.random.Generator, s: Sessions, n_paths: int, mask: np.ndarray | None = None
) -> Iterator[np.ndarray]:
    pool = s.day_r if mask is None else s.day_r[mask]
    for lo in range(0, n_paths, CHUNK):
        n = min(CHUNK, n_paths - lo)
        yield pool[rng.integers(0, len(pool), size=(n, len(pool)))]


def block_paths(
    rng: np.random.Generator, s: Sessions, n_paths: int, length: int
) -> Iterator[np.ndarray]:
    t = s.n_days
    n_blocks = -(-t // length)
    offsets = np.arange(length)
    for lo in range(0, n_paths, CHUNK):
        n = min(CHUNK, n_paths - lo)
        starts = rng.integers(0, t, size=(n, n_blocks))
        idx = ((starts[:, :, None] + offsets) % t).reshape(n, -1)[:, :t]
        yield s.day_r[idx]


def trade_paths(rng: np.random.Generator, s: Sessions, n_paths: int) -> Iterator[np.ndarray]:
    """Each session keeps its trade count; its trades are drawn iid from the whole pool."""
    for lo in range(0, n_paths, TRADE_CHUNK):
        n = min(TRADE_CHUNK, n_paths - lo)
        draws = s.trade_r[rng.integers(0, s.n_trades, size=(n, s.n_trades))]
        yield np.add.reduceat(draws, s.starts, axis=1)


# ---- one method ---------------------------------------------------------------------------------


def _summary(values: np.ndarray) -> dict[str, Any]:
    q = np.percentile(values, PERCENTILES)
    return {"mean": _num(values.mean()), **{f"p{p}": _num(v) for p, v in zip(PERCENTILES, q, strict=True)}}


def _prob(flags: np.ndarray) -> dict[str, Any]:
    p = float(np.mean(flags))
    n = len(flags)
    out = {"p": _num(p), "mcse": _num(np.sqrt(p * (1 - p) / n)), "paths": int(n)}
    if p in (0.0, 1.0):
        # the standard error is 0 here, which would read as certainty: give the rule-of-three
        # one-sided 95% bound on the opposite outcome instead
        out["bound95_other_outcome_at_most"] = _num(3.0 / n)
    return out


def run_method(
    chunks: Iterator[np.ndarray],
    fractions: Sequence[float],
    theta: float,
    deployed: float = DEPLOYED_F,
) -> dict[str, Any]:
    add_final, add_dd, worst = [], [], []
    eq: dict[float, dict[str, list]] = {f: {"final": [], "max_dd": [], "ruin": []} for f in fractions}
    for block in chunks:
        a = additive_stats(block)
        add_final.append(a["final"])
        add_dd.append(a["max_dd"])
        worst.append(block.min(axis=1))
        for f in fractions:
            e = equity_stats(block, f, theta)
            for k in ("final", "max_dd", "ruin"):
                eq[f][k].append(e[k])
    final = np.concatenate(add_final)
    out: dict[str, Any] = {
        "paths": int(len(final)),
        "c1_normalised_r": {
            "final_r": _summary(final),
            "max_drawdown_r": _summary(np.concatenate(add_dd)),
            "p_final_negative": _prob(final < 0),
        },
        "worst_session_r_in_path": _summary(np.concatenate(worst)),
        "c2_account": {},
    }
    for f in fractions:
        fin = np.concatenate(eq[f]["final"])
        dd = np.concatenate(eq[f]["max_dd"])
        ruin = np.concatenate(eq[f]["ruin"])
        out["c2_account"][f"{f:g}"] = {
            "risk_fraction": f,
            "deployed_reference": bool(abs(f - deployed) < 1e-12),
            "final_equity_multiple": _summary(fin),
            "max_drawdown_fraction": _summary(dd),
            "p_final_below_start": _prob(fin < 1.0),
            "p_ruin": _prob(ruin),
            "reference_only": {
                "p95_max_drawdown_within_25pct": bool(np.percentile(dd, 95) <= DD_REFERENCE),
                "p_ruin_below_5pct": bool(ruin.mean() < RUIN_REFERENCE),
                "note": "G5.1 reference values, informational; no gate is changed",
            },
        }
    return out


# ---- observed path and exposure ------------------------------------------------------------------


def observed(s: Sessions, fractions: Sequence[float], theta: float) -> dict[str, Any]:
    path = s.day_r[None, :]
    a = additive_stats(path)
    out: dict[str, Any] = {
        "sessions": s.n_days,
        "trades": s.n_trades,
        "sum_net_r": _num(s.day_r.sum()),
        "c1_final_r": _num(a["final"][0]),
        "c1_max_drawdown_r": _num(a["max_dd"][0]),
        "worst_session_r": _num(s.day_r.min()),
        "best_session_r": _num(s.day_r.max()),
        "c2_account": {},
    }
    for f in fractions:
        e = equity_stats(path, f, theta)
        eq = np.concatenate([[1.0], np.cumprod(np.maximum(1.0 + f * s.day_r, 0.0))])
        hit = np.flatnonzero(eq <= (1.0 - theta) + EPS)
        out["c2_account"][f"{f:g}"] = {
            "final_equity_multiple": _num(e["final"][0]),
            "max_drawdown_fraction": _num(e["max_dd"][0]),
            "ruined": bool(e["ruin"][0]),
            "first_session_at_or_below_ruin_level": int(hit[0]) if len(hit) else None,
        }
    return out


def implied_exposure(trades: pd.DataFrame, fractions: Sequence[float], s: Sessions) -> dict[str, Any]:
    occ = occupancy(trades.reset_index(drop=True), 1.0)
    peak = occ["peak"]["concurrent_positions"]
    p99 = occ["peak_concurrency_per_session"]["p99"]
    return {
        "caps_applied": False,
        "note": (
            "no portfolio-wide cap was applied; the recorded run had none. These are not "
            "live-account figures and say nothing about G5.2"
        ),
        "peak_concurrent_positions": int(peak),
        "p99_session_peak_concurrent_positions": _num(p99),
        "implied_peak_simultaneous_risk_fraction": {f"{f:g}": _num(peak * f) for f in fractions},
        "implied_p99_session_peak_risk_fraction": {f"{f:g}": _num(p99 * f) for f in fractions},
        "worst_observed_session_equity_change_fraction": {
            f"{f:g}": _num(f * s.day_r.min()) for f in fractions
        },
    }


# ---- report ---------------------------------------------------------------------------------------


DEFINITIONS = {
    "ruin": (
        "min over sessions of equity <= (1 - theta) x starting equity; the boundary counts as "
        "ruin; theta = 0.25 (STRATEGY-ACCEPTANCE-CRITERIA.md, G5.1, proposed 2026-10-02)"
    ),
    "equity": (
        "E_0 = 1; E_d = E_{d-1} x max(0, 1 + f x R_d); R_d = sum of net R of trades entered on "
        "IST session d; compounded daily on day-start equity; evaluated at session end"
    ),
    "c1": "S_k = cumulative sum of session R, additive; max drawdown from S_0 = 0",
    "drawdown_c2": "max over sessions of (running peak - E_d) / running peak",
    "mcse": "sqrt(p(1-p)/paths); when p is 0 or 1 it is 0 and the rule-of-three bound 3/paths is given for the opposite outcome",
    "conditional": (
        "all account-level figures are conditional on the resampling model, f, theta and the "
        "compounding rule; they are not live-account ruin probabilities"
    ),
}
LIMITATIONS = [
    "sessions are assumed exchangeable; regimes, volatility clustering and extreme days enter only as the sample holds them",
    "resampling can duplicate extreme sessions such as 2020-03-27",
    "portfolio caps are not applied and the recorded run had none; the implied exposure is reported; this says nothing about G5.2",
    "equity is evaluated at session end, so intraday drawdown is ignored",
    "a fixed seed and many paths remove sampling noise, not model uncertainty",
    "a bootstrap of a sample with a negative mean describes the sample; it tests no null",
]


def build_report(
    trades: pd.DataFrame,
    seed: int = SEED,
    n_paths: int = N_PATHS,
    fractions: Sequence[float] = RISK_FRACTIONS,
    theta: float = THETA,
    deployed_f: float = DEPLOYED_F,
) -> dict[str, Any]:
    fractions = sorted({*fractions, deployed_f})
    s = Sessions(trades)
    plan = (
        ["day", "trade", *[f"block_{n}" for n in BLOCK_LENGTHS], "recent_day"]
        + [f"day_cost_{m:g}x" for m in COST_MULTIPLIERS]
    )
    streams = dict(zip(plan, np.random.SeedSequence(seed).spawn(len(plan)), strict=True))
    rng = {k: np.random.default_rng(v) for k, v in streams.items()}
    results: dict[str, Any] = {
        "day": run_method(day_paths(rng["day"], s, n_paths), fractions, theta, deployed_f),
        "trade": run_method(trade_paths(rng["trade"], s, n_paths), fractions, theta, deployed_f),
    }
    for length in BLOCK_LENGTHS:
        key = f"block_{length}"
        results[key] = run_method(
            block_paths(rng[key], s, n_paths, length), fractions, theta, deployed_f
        )
    recent = s.year >= RECENT_FROM
    if recent.sum() >= 3:
        results["recent_day"] = run_method(
            day_paths(rng["recent_day"], s, n_paths, recent), fractions, theta, deployed_f
        )
        results["recent_day"]["sessions"] = int(recent.sum())
        results["recent_day"]["from_year"] = RECENT_FROM
    for m in COST_MULTIPLIERS:
        key = f"day_cost_{m:g}x"
        sm = Sessions(trades, cost_multiplier=m)
        results[key] = run_method(day_paths(rng[key], sm, n_paths), fractions, theta, deployed_f)
    return {
        "parameters": {
            "seed": seed,
            "seed_streams": "numpy SeedSequence(seed).spawn, one stream per method in plan order",
            "paths": n_paths,
            "chunk": CHUNK,
            "trade_chunk": TRADE_CHUNK,
            "sessions": s.n_days,
            "trades": s.n_trades,
            "trades_excluded_undefined_r": s.excluded_undefined_r,
            "theta": theta,
            "risk_fractions": list(fractions),
            "deployed_reference_fraction": deployed_f,
            "block_lengths": list(BLOCK_LENGTHS),
            "cost_multipliers": [1.0, *COST_MULTIPLIERS],
            "recent_from_year": RECENT_FROM,
        },
        "definitions": DEFINITIONS,
        "limitations": LIMITATIONS,
        "observed": observed(s, fractions, theta),
        "implied_exposure": implied_exposure(trades[trades["net_r"].notna()], fractions, s),
        "methods": results,
    }


def seal_report(report: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    body = {
        "schema": SCHEMA,
        "report_only": True,
        "diagnostic_not_trials": True,
        "source": source,
        "report": report,
    }
    return {**body, "content_sha256": seal(body)}


def build_evidence(trades: pd.DataFrame, source: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return seal_report(build_report(trades, **kw), source)


def validate_evidence(evidence: dict[str, Any]) -> list[str]:
    problems = []
    if evidence.get("schema") != SCHEMA:
        problems.append(f"schema is {evidence.get('schema')!r}, expected {SCHEMA!r}")
    body = {k: v for k, v in evidence.items() if k != "content_sha256"}
    if evidence.get("content_sha256") != seal(body):
        problems.append("content_sha256 does not match the payload")
    if evidence.get("report_only") is not True or evidence.get("diagnostic_not_trials") is not True:
        problems.append("report_only and diagnostic_not_trials must both be true")
    if "ruin" not in evidence.get("report", {}).get("definitions", {}):
        problems.append("the ruin definition is not recorded")
    return problems


def trade_fingerprint(trades: Sequence[Any]) -> str:
    """Order-independent fingerprint of the columns the simulation reads, from engine trades."""

    def num(v: Any) -> str:
        return f"{round(float(v), 10)}" if np.isfinite(v) else "nan"

    rows = sorted(
        f"{t.symbol}|{pd.Timestamp(t.entry_time).value}|{pd.Timestamp(t.exit_time).value}|"
        f"{num(t.r_gross)}|{num(t.r_net)}"
        for t in trades
    )
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def frame_from_trades(trades: Sequence[Any]) -> pd.DataFrame:
    """The simulation's input table, built from engine trades (R is NaN for a trade with no stop)."""
    return pd.DataFrame(
        {
            "symbol": [t.symbol for t in trades],
            "side": ["long" if t.direction == 1 else "short" for t in trades],
            "entry_time": [pd.Timestamp(t.entry_time) for t in trades],
            "exit_time": [pd.Timestamp(t.exit_time) for t in trades],
            "gross_r": [float(t.r_gross) for t in trades],
            "net_r": [float(t.r_net) for t in trades],
        }
    )


def attached_evidence(trades: Sequence[Any], deployed_f: float, **kw: Any) -> dict[str, Any]:
    """Evidence for a recording: the run's own trades, bound to them by a fingerprint.

    `deployed_f` is the run's recorded risk per trade. Nothing is re-simulated. The record names
    the exact trade list it was computed from, so it cannot be attached to another one.
    """
    source = {
        "kind": "recorded-run-trades",
        "trades": len(trades),
        "trade_fingerprint": trade_fingerprint(trades),
        "deployed_f": float(deployed_f),
    }
    return build_evidence(frame_from_trades(trades), source, deployed_f=float(deployed_f), **kw)


def evidence_for_run(run_dir: str | Path, **kw: Any) -> dict[str, Any]:
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
    return build_evidence(pd.read_parquet(path), source, **kw)


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(description="G5.1 Monte Carlo risk diagnostic (read only)")
    p.add_argument("run_dir")
    p.add_argument("--out", required=True, help="where to write monte_carlo.json")
    p.add_argument("--seed", type=int, default=SEED)
    p.add_argument("--paths", type=int, default=N_PATHS)
    args = p.parse_args(argv)
    evidence = evidence_for_run(args.run_dir, seed=args.seed, n_paths=args.paths)
    problems = validate_evidence(evidence)
    Path(args.out).write_text(json.dumps(evidence, indent=1, allow_nan=False))
    sys.stdout.write(f"wrote {args.out}; problems: {problems or 'none'}\n")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
