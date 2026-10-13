"""Bounded parameter tuning: a small grid, in-sample only, ranked by its stable region.

A strategy's parameters are bound to its frozen spec, so a recorded run can never vary them. This
module is the one place that does, under four rules that keep the selection honest:

  IN-SAMPLE ONLY  Every combination runs through the real `execute_backtest` (published data,
                  eligibility, profile caps, statutory costs, tick grid) with the data load cut at
                  the in-sample end: the first (1 - holdout_fraction) of the dataset's sessions.
                  The default hold-out (40%) is wider than the recorded run's (30%, G3.1), so the
                  recorded run's out-of-sample period lies inside sessions no tuning has read.
                  The sessions between the two cuts (10% by default) are a buffer, not waste: the
                  recorded run fixes its out-of-sample start from its own sessions and trades
                  (`default_oos_start` falls back to trade days), so the start is not known when
                  tuning runs, and the buffer keeps it clear of the tuning period. The recorded
                  run's in-sample statistics still use those sessions.
                  `separation_failures` makes a recorded run invalid when it does not.
  BOUNDED GRID    At most MAX_COMBINATIONS, each parameter an explicit ascending list of values
                  of the spec's own type. No search heuristic and no random draws: reproducible.
  STABLE REGION   A combination's region is itself and every usable grid neighbour (at most one
                  step away in each parameter). The selection maximises the region's WORST net R
                  per trade (then the region's size and median), not the single best result,
                  so a lone spike next to poor neighbours loses to a plateau. The record says
                  when the two differ.
  EVERY TRIAL     All combinations are added to the idea's trial ledger in one external entry
                  that names this record, so the deflated Sharpe and the hurdles of the later
                  recorded run account for the search. The per-session P&L of every distinct
                  combination gives PBO (CSCV) on the in-sample period. It is a diagnostic: it
                  measures picking the single best combination, not the region selection, and
                  no gate reads it.

The output is `ideas/<idea>/tuning/<tuning_id>/tuning.json`, written once. Adopting the selected
values is a new strategy version (the strategy's defaults and the spec's parameters) and a normal
recorded run, which evaluates them once on the untouched hold-out.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import statistics
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from signal_engine.backtest import stats
from signal_engine.research.intake.fingerprints import params_fingerprint
from signal_engine.timeutils import IST
from utils.logging import get_logger

logger = get_logger(__name__)

TUNING_DIR = "tuning"
RECORD = "tuning.json"
EVIDENCE_VERSION = 1
MAX_COMBINATIONS = 64
#: Share of the dataset's sessions no tuning reads; at least the recorded run's G3.1 hold-out.
DEFAULT_HOLDOUT = 0.40
MIN_HOLDOUT, MAX_HOLDOUT = 0.30, 0.50
#: A combination with fewer in-sample trades than this is unusable (the G2.1 floor).
MIN_TRADES = 30
MAX_PBO_SLICES = 16
PBO_METHOD = (
    "CSCV (Bailey et al.) over in-sample session net R of the distinct combinations; measures "
    "picking the single best combination, not the stable-region selection"
)
OBJECTIVE = "mean net R per trade at 1.0x cost (statutory model), in-sample sessions only"
_GRID_KEYS = ("parameters", "min_trades", "holdout_fraction")


class TuningError(Exception):
    """The grid or the tuning inputs are not acceptable. Nothing is run or written."""


@dataclass(frozen=True)
class Grid:
    parameters: dict[str, tuple[Any, ...]]
    min_trades: int = MIN_TRADES
    holdout_fraction: float = DEFAULT_HOLDOUT

    def combinations(self) -> list[dict[str, Any]]:
        names = list(self.parameters)
        return [
            dict(zip(names, values, strict=True))
            for values in itertools.product(*self.parameters.values())
        ]

    def indices(self) -> list[tuple[int, ...]]:
        return list(itertools.product(*(range(len(v)) for v in self.parameters.values())))


def _same_type(value: Any, base: Any) -> bool:
    if isinstance(base, bool) or isinstance(value, bool):
        return isinstance(value, bool) and isinstance(base, bool)
    if isinstance(base, int):
        return isinstance(value, int)
    if isinstance(base, float):
        return isinstance(value, (int, float)) and math.isfinite(float(value))
    return type(value) is type(base)


def _check_values(name: str, values: Any, base: Any) -> str | None:
    if not isinstance(values, list) or len(values) < 2:
        return f"'{name}' needs a list of at least two values"
    if not all(_same_type(v, base) for v in values):
        return f"'{name}' values must have the spec's type ({type(base).__name__})"
    if len(set(values)) != len(values):
        return f"'{name}' repeats a value"
    if not isinstance(base, (bool, str)) and list(values) != sorted(values):
        return f"'{name}' values must be in ascending order"
    return None


def parse_grid(raw: Any, spec_parameters: Mapping[str, Any]) -> Grid:
    """Validate a grid against the frozen spec's parameters. Every problem is listed."""
    if not isinstance(raw, Mapping):
        raise TuningError("the grid must be a mapping with a 'parameters' block")
    problems = [f"unknown key '{k}'" for k in sorted(set(raw) - set(_GRID_KEYS))]
    block = raw.get("parameters")
    if not isinstance(block, Mapping) or not block:
        raise TuningError("; ".join([*problems, "'parameters' must map names to value lists"]))
    params: dict[str, tuple[Any, ...]] = {}
    for name, values in block.items():
        if name not in spec_parameters:
            problems.append(f"'{name}' is not a parameter of the frozen spec")
            continue
        base = spec_parameters[name]
        problem = _check_values(name, values, base)
        if problem:
            problems.append(problem)
            continue
        params[name] = tuple(float(v) if isinstance(base, float) else v for v in values)
    min_trades = raw.get("min_trades", MIN_TRADES)
    if isinstance(min_trades, bool) or not isinstance(min_trades, int) or min_trades < 1:
        problems.append("min_trades must be a positive integer")
    holdout = raw.get("holdout_fraction", DEFAULT_HOLDOUT)
    if (
        isinstance(holdout, bool)
        or not isinstance(holdout, (int, float))
        or not (MIN_HOLDOUT <= float(holdout) <= MAX_HOLDOUT)
    ):
        problems.append(f"holdout_fraction must be between {MIN_HOLDOUT} and {MAX_HOLDOUT}")
    total = math.prod(len(v) for v in params.values()) if params else 0
    if total > MAX_COMBINATIONS:
        problems.append(f"{total} combinations exceed the bound of {MAX_COMBINATIONS}")
    if problems:
        raise TuningError("; ".join(problems))
    return Grid(params, int(min_trades), float(holdout))


def in_sample_end(sessions: Iterable[date], holdout_fraction: float) -> date:
    """The last session tuning may read: the one before the first held-out session."""
    ordered = sorted(set(sessions))
    cut = int(len(ordered) * (1.0 - holdout_fraction))
    if cut < 1 or cut >= len(ordered):
        raise TuningError(f"{len(ordered)} sessions cannot be split at {holdout_fraction}")
    return ordered[cut - 1]


def score(trades: Sequence[Any], min_trades: int) -> dict[str, Any]:
    """Trade count, mean net R and net bps of one combination, and whether it is usable."""
    n = len(trades)
    r = [float(t.r_net) for t in trades if t.r_net is not None and math.isfinite(t.r_net)]
    return {
        "trades": n,
        "net_r": float(np.mean(r)) if r else None,
        "net_bps": float(np.mean([t.net_bps for t in trades])) if n else None,
        "usable": n >= min_trades and len(r) == n,
    }


def neighbourhood(grid: Grid, results: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Each combination's region: itself and its usable neighbours (every combination at most one
    step away in each parameter), summarised by its worst and median net R. None when unusable."""
    idx = grid.indices()
    out = []
    for i, here in enumerate(idx):
        if not results[i]["usable"]:
            out.append({"region_worst_net_r": None, "region_median_net_r": None, "region_size": 0})
            continue
        region = [
            results[j]["net_r"]
            for j, there in enumerate(idx)
            if results[j]["usable"]
            and all(abs(a - b) <= 1 for a, b in zip(here, there, strict=True))
        ]
        out.append(
            {
                "region_worst_net_r": float(min(region)),
                "region_median_net_r": float(statistics.median(region)),
                "region_size": len(region),
            }
        )
    return out


def select(results: Sequence[Mapping[str, Any]]) -> int | None:
    """The combination whose region is best: highest worst-case net R, then the larger region
    (an interior point has evidence on every side), then highest median, then its own net R,
    then grid order."""
    usable = [i for i, r in enumerate(results) if r["region_worst_net_r"] is not None]
    if not usable:
        return None
    return max(
        usable,
        key=lambda i: (
            results[i]["region_worst_net_r"],
            results[i]["region_size"],
            results[i]["region_median_net_r"],
            results[i]["net_r"],
            -i,
        ),
    )


def _day(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value)[:10])


def session_matrix(per_combo: Sequence[Sequence[Any]], sessions: Iterable[date]) -> np.ndarray:
    """(sessions x combinations) net R per entry session, zero on a session without a trade."""
    pos = {d: k for k, d in enumerate(sorted(set(sessions)))}
    m = np.zeros((len(pos), len(per_combo)))
    for j, trades in enumerate(per_combo):
        for t in trades:
            k = pos.get(_day(t.day))
            if k is not None and t.r_net is not None and math.isfinite(t.r_net):
                m[k, j] += float(t.r_net)
    return m


def pbo(matrix: np.ndarray) -> dict[str, Any]:
    """PBO by CSCV over the distinct combinations, or why it was not computed.

    Identical columns (a parameter with no effect in-sample) are one strategy, not rivals: kept,
    they push the training winner's out-of-sample rank down and report overfitting where no
    selection happened. The value describes picking the single best combination by mean session
    net R, not the stable-region selection, so it is a diagnostic that no gate reads.
    """
    periods = matrix.shape[0]
    distinct = np.unique(matrix, axis=1) if matrix.size else matrix
    combos = distinct.shape[1]
    label = {"role": "diagnostic", "method": PBO_METHOD, "distinct_combinations": combos}
    slices = min(MAX_PBO_SLICES, periods - periods % 2)
    if combos < 2 or slices < 2:
        reason = f"{combos} distinct combinations over {periods} sessions"
        return {"value": None, "reason": reason, **label}
    return {"value": stats.pbo_cscv(distinct, n_slices=slices), "slices": slices, **label}


def evaluate_grid(
    grid: Grid,
    run_combination: Callable[[dict[str, Any]], Sequence[Any]],
    sessions: Iterable[date],
    is_end: date,
) -> dict[str, Any]:
    """Run every combination, score it, rank it by region and compute PBO.

    `run_combination(values) -> trades` must read no session after `is_end`; a trade dated after
    it stops the tuning, because it means the hold-out was read.
    """
    is_sessions = [d for d in sessions if d <= is_end]
    per_combo, rows = [], []
    for values in grid.combinations():
        trades = list(run_combination(values))
        late = [t for t in trades if _day(t.day) > is_end]
        if late:
            raise TuningError(f"{values}: {len(late)} trades after the in-sample end {is_end}")
        per_combo.append(trades)
        rows.append({"values": values, **score(trades, grid.min_trades)})
    rows = [{**r, **g} for r, g in zip(rows, neighbourhood(grid, rows), strict=True)]
    chosen = select(rows)
    usable = [i for i, r in enumerate(rows) if r["usable"]]
    best = max(usable, key=lambda i: rows[i]["net_r"]) if usable else None
    return {
        "results": rows,
        "selected": None if chosen is None else {"index": chosen, **rows[chosen]},
        "best_single": None if best is None else {"index": best, **rows[best]},
        "selected_is_not_the_single_best": chosen is not None and chosen != best,
        "pbo": pbo(session_matrix(per_combo, is_sessions)),
    }


def _records(folder: Path) -> list[Path]:
    return sorted((Path(folder) / TUNING_DIR).glob(f"*/{RECORD}"))


def has_tuning(folder: Path) -> bool:
    return bool(_records(folder))


def separation_failures(folder: Path, oos_start: str | None) -> list[dict[str, str]]:
    """Validity failures for a recorded run whose out-of-sample start overlaps a tuning period.

    A tuning record of the idea that read any session on or after the run's out-of-sample start
    has seen the period the run validates on, whichever values it selected.
    """
    out = []
    for path in _records(folder):
        try:
            is_end = str(json.loads(path.read_text(encoding="utf-8"))["in_sample_end"])
        except (OSError, ValueError, KeyError, TypeError):
            detail = (
                f"tuning {path.parent.name}: the record is unreadable, so separation is unknown"
            )
            out.append({"check": "tuning_separation", "detail": detail})
            continue
        if oos_start is None or is_end >= str(oos_start):
            out.append(
                {
                    "check": "tuning_separation",
                    "detail": (
                        f"tuning {path.parent.name} read sessions to {is_end}, on or after this "
                        f"run's out-of-sample start {oos_start}: the hold-out is not untouched"
                    ),
                }
            )
    return out


def _next_id(folder: Path, day: str) -> str:
    base = Path(folder) / TUNING_DIR
    taken = {p.name for p in base.glob(f"{day}-*")} if base.exists() else set()
    n = 1
    while f"{day}-{n:03d}" in taken:
        n += 1
    return f"{day}-{n:03d}"


def _with_fingerprints(value: Any, spec_params: Mapping[str, Any]) -> Any:
    def one(row: Mapping[str, Any]) -> dict[str, Any]:
        return {**row, "params_fingerprint": params_fingerprint({**spec_params, **row["values"]})}

    if value is None:
        return None
    return [one(r) for r in value] if isinstance(value, list) else one(value)


def run_tuning(
    root: Path,
    idea_id: str,
    grid_raw: Any,
    *,
    now: datetime,
    execute: Callable[..., Any] | None = None,
    supported: Iterable[str] | None = None,
    dataset_version: str | None = None,
    frozen: Any = None,
    git_state_fn: Callable[[Path], Mapping[str, Any]] | None = None,
) -> Path:
    """Tune `idea_id` on its in-sample sessions and write the tuning record. Returns its path.

    Raises KeyError for an unknown idea and TuningError for an unacceptable grid, before anything
    runs. The trial ledger is written only after the record exists.
    """
    from signal_engine.backtest import manifest as manifest_mod
    from signal_engine.backtest import record_run as rr
    from signal_engine.research.intake import freeze as intake_freeze
    from signal_engine.research.intake import trials as trials_mod
    from signal_engine.research.intake.versions import require_aware

    require_aware(now)
    folders = rr.idea_folders(root)
    if idea_id not in folders:
        raise KeyError(idea_id)
    folder = folders[idea_id]
    idea = rr._parse_frontmatter(folder / "idea.md")
    key = rr.strategy_key(idea, supported if supported is not None else rr.supported_keys())
    if key is None:
        raise rr.Unsupported(f"{idea_id}: no trade-list engine to tune")
    if frozen is None:
        frozen = intake_freeze.freeze(folder, idea_id=idea_id, register_version=False)
    spec_params = dict(frozen.intent.get("parameters") or {})
    grid = parse_grid(grid_raw, spec_params)
    resolution = rr.prepare_data(folder, frozen, dataset_version=dataset_version)
    cov = resolution.manifest["coverage"]
    sessions = list(
        resolution.calendar.expected_sessions(
            date.fromisoformat(cov["start"]), date.fromisoformat(cov["end"])
        )
    )
    is_end = in_sample_end(sessions, grid.holdout_fraction)
    base = rr.frozen_settings(frozen)
    execute = execute or rr.execute_backtest

    def run_combination(values: dict[str, Any]) -> list:
        logger.info("Tuning %s: %s", idea_id, values)
        settings = {
            **base,
            "parameters": {**spec_params, **values},
            rr.PARAMETER_OVERRIDES: dict(values),
        }
        return execute(
            key, settings=settings, end=is_end.isoformat(), dataset=resolution, run_truncation=False
        ).trades

    result = evaluate_grid(grid, run_combination, sessions, is_end)
    stamp = now.astimezone(IST)
    tuning_id = _next_id(folder, stamp.strftime("%Y-%m-%d"))
    git = (git_state_fn or manifest_mod.git_state)(root)
    record = {
        "version": EVIDENCE_VERSION,
        "tuning_id": tuning_id,
        "idea_id": idea_id,
        "strategy_key": key,
        "created_ist": stamp.strftime("%Y-%m-%d %H:%M:%S"),
        "objective": OBJECTIVE,
        "grid": {name: list(values) for name, values in grid.parameters.items()},
        "min_trades": grid.min_trades,
        "holdout_fraction": grid.holdout_fraction,
        "in_sample_end": is_end.isoformat(),
        "held_out_from": sorted(d for d in sessions if d > is_end)[0].isoformat(),
        "dataset_version": resolution.version_id,
        "dataset_fingerprint": resolution.run_fingerprint(resolution.eligible, is_end.isoformat()),
        "spec_parameters": spec_params,
        "rules_fingerprint": frozen.rules_fingerprint,
        "code_commit": git.get("code_commit"),
        "code_dirty": git.get("code_dirty"),
        "combinations": len(result["results"]),
        "results": _with_fingerprints(result["results"], spec_params),
        "selected": _with_fingerprints(result["selected"], spec_params),
        "best_single": _with_fingerprints(result["best_single"], spec_params),
        "selected_is_not_the_single_best": result["selected_is_not_the_single_best"],
        "pbo": result["pbo"],
    }
    out = Path(folder) / TUNING_DIR / tuning_id
    out.mkdir(parents=True, exist_ok=False)
    path = out / RECORD
    text = json.dumps(record, indent=2, sort_keys=True, default=str) + "\n"
    with path.open("x", encoding="utf-8") as fh:
        fh.write(text)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    trials_mod.add_external(
        folder,
        len(result["results"]),
        f"parameter grid {tuning_id}: {len(result['results'])} configurations evaluated on "
        f"in-sample sessions to {is_end.isoformat()} ({TUNING_DIR}/{tuning_id}/{RECORD}, "
        f"sha256 {digest})",
        now=now,
    )
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded in-sample parameter tuning")
    parser.add_argument("idea_id", help="for example IDEA-031")
    parser.add_argument("--grid", required=True, type=Path, help="YAML grid file")
    parser.add_argument("--dataset-version", default=None)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        raw = yaml.safe_load(args.grid.read_text(encoding="utf-8"))
        path = run_tuning(
            args.root,
            args.idea_id,
            raw,
            now=datetime.now(IST),
            dataset_version=args.dataset_version,
        )
    except (TuningError, KeyError, OSError, yaml.YAMLError):
        logger.exception("Tuning of %s failed", args.idea_id)
        return 1
    sys.stdout.write(f"{path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
