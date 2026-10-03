"""Reproduce a recorded run from its folder alone and compare the result row for row.

`reproduce` rebuilds the frozen strategy from the run folder, checks the code and the dataset
are the ones the run used, reruns with `end=data_as_of`, and compares the new trades with
`evidence/trades.parquet`. A failed check is reported, never raised, and nothing is ever
reported as reproduced without every check passing. An attempt is stored beside the run, at
`runs/<run_id>.reproductions/<timestamp>-ist.json`, and never touches the run folder.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from signal_engine.backtest import manifest as manifest_mod
from signal_engine.backtest import record_run, run_artifact
from signal_engine.backtest.trades_io import read_trades_parquet, trades_to_frame
from signal_engine.research.build_registry import _parse_frontmatter
from signal_engine.research.intake import freeze as intake_freeze
from signal_engine.timeutils import IST
from utils.logging import get_logger

logger = get_logger(__name__)

#: Every check a complete attempt records. A run is reproducible only if all are present and true.
CHECKS = (
    "artifacts_verified",
    "manifest_valid",
    "manifest_reproducible",
    "code_unchanged",
    "rerun_completed",
    "dataset_unchanged",
    "calendar_unchanged",
    "trades_match",
)
#: Float tolerance for comparing trades, absolute.
TOLERANCE = 1e-9
_MAX_PATHS = 5


@dataclass(frozen=True)
class ReproductionReport:
    """The outcome of one reproduction attempt."""

    run_id: str
    reproducible: bool
    reasons: list[str]
    checks: dict[str, bool]
    compared_trades: int
    attempted_ist: str
    #: Informational only: columns the rerun has that the recorded file does not.
    notes: list[str] = field(default_factory=list)


class _Attempt:
    """Collects checks and reasons so a failed check never raises."""

    def __init__(self) -> None:
        self.checks: dict[str, bool] = {}
        self.reasons: list[str] = []
        self.notes: list[str] = []

    def record(self, name: str, ok: bool, *reasons: str) -> bool:
        self.checks[name] = ok
        if not ok:
            self.reasons.extend(reasons)
        return ok


def _load_manifest(folder: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _why_not_reproducible(manifest: dict[str, Any]) -> str:
    flags = manifest.get("reproducibility")
    missing = (
        sorted(k for k, v in flags.items() if v is not True) if isinstance(flags, dict) else []
    )
    engine = manifest.get("engine") if isinstance(manifest.get("engine"), dict) else {}
    parts = []
    if engine.get("code_dirty"):
        paths = ", ".join(engine.get("dirty_paths", [])[:_MAX_PATHS])
        parts.append(
            "code_dirty: the run was made from uncommitted code" + (f" ({paths})" if paths else "")
        )
    if missing:
        parts.append("not captured: " + ", ".join(missing))
    return "manifest says not reproducible: " + ("; ".join(parts) or "no reason recorded")


def _check_code(
    attempt: _Attempt,
    manifest: dict[str, Any],
    root: Path,
    git_state_fn: Callable[[Path], dict[str, Any]] | None,
) -> bool:
    git = (git_state_fn or manifest_mod.git_state)(root)
    reasons = []
    recorded = manifest["engine"]["code_commit"]
    current = git.get("code_commit", "unknown")
    if current != recorded:
        reasons.append(f"code commit differs: run used {recorded}, working copy is at {current}")
    dirty = run_artifact.result_affecting_paths(git.get("dirty_paths", []))
    if dirty:
        reasons.append("dirty working tree: " + ", ".join(dirty[:_MAX_PATHS]))
    return attempt.record("code_unchanged", not reasons, *reasons)


def _strategy_key(run_folder: Path, supported: Iterable[str] | None) -> str | None:
    idea = _parse_frontmatter(run_folder.parent.parent / "idea.md")
    return record_run.strategy_key(
        idea, supported if supported is not None else record_run.supported_keys()
    )


def _differences(old: pd.DataFrame, new: pd.DataFrame) -> list[str]:
    """Reasons the two trade tables differ, or an empty list when they match."""
    if list(old.columns) != list(new.columns):
        return [f"trade columns differ: {list(old.columns)} vs {list(new.columns)}"]
    if len(old) != len(new):
        return [f"trade count differs: {len(old)} recorded vs {len(new)} on rerun"]
    bad_cells = 0
    bad_rows: set[int] = set()
    first: tuple[int, str, Any, Any] | None = None
    for col in old.columns:
        a, b = old[col].reset_index(drop=True), new[col].reset_index(drop=True)
        if pd.api.types.is_float_dtype(a) and pd.api.types.is_float_dtype(b):
            av, bv = a.to_numpy(dtype=float), b.to_numpy(dtype=float)
            same = np.isclose(av, bv, rtol=0.0, atol=TOLERANCE, equal_nan=True)
        elif pd.api.types.is_datetime64_any_dtype(a) and pd.api.types.is_datetime64_any_dtype(b):
            au, bu = pd.to_datetime(a, utc=True), pd.to_datetime(b, utc=True)
            same = ((au == bu) | (au.isna() & bu.isna())).to_numpy()
        else:
            same = ((a == b) | (a.isna() & b.isna())).to_numpy()
        for i in np.flatnonzero(~same):
            bad_cells += 1
            bad_rows.add(int(i))
            if first is None or int(i) < first[0]:
                first = (int(i), col, a.iloc[i], b.iloc[i])
    if first is None:
        return []
    row, col, was, now = first
    return [
        f"trades differ: first at row {row} column {col} (recorded {was!r}, rerun {now!r}); "
        f"{bad_cells} differing cells in {len(bad_rows)} of {len(old)} rows"
    ]


def _compare_trades(attempt: _Attempt, folder: Path, execution: Any) -> int:
    try:
        old = read_trades_parquet(folder / "evidence" / "trades.parquet")
    except Exception as exc:
        logger.exception("Cannot read recorded trades for %s", folder.name)
        attempt.record("trades_match", False, f"recorded trades unreadable: {exc}")
        return 0
    if len(old) == 0:
        attempt.record(
            "trades_match", False, "recorded run has zero trades: there is nothing to compare"
        )
        return 0
    new = trades_to_frame(execution.trades, components=getattr(execution, "cost_components", None))
    # Always compare on the columns the recorded file has. Extra columns on the rerun are noted,
    # never compared; a column the rerun lacks stays a mismatch (reported by `_differences`).
    extra = [c for c in new.columns if c not in old.columns]
    if extra:
        attempt.notes.append(
            "rerun has columns the recorded file lacks (not compared): " + ", ".join(extra)
        )
    if set(old.columns) <= set(new.columns):
        new = new[list(old.columns)]
    reasons = _differences(old, new)
    attempt.record("trades_match", not reasons, *reasons)
    return min(len(old), len(new))


def _rerun(attempt: _Attempt, folder: Path, manifest: dict[str, Any], execute: Any, key: str):
    """Rebuild the frozen strategy and execute it up to `data_as_of`; None on any failure."""
    try:
        frozen = intake_freeze.from_run_folder(folder)
        execution = record_run._execute_frozen(
            execute, key, frozen, end=manifest["dataset"]["data_as_of"]
        )
    except Exception as exc:
        logger.exception("Reproduction rerun failed for %s", folder.name)
        attempt.record("rerun_completed", False, f"rerun failed: {type(exc).__name__}: {exc}")
        return None
    attempt.record("rerun_completed", True)
    return execution


def _check_dataset(
    attempt: _Attempt,
    manifest: dict[str, Any],
    execution: Any,
    fingerprint_fn: Callable[[Sequence[str], str, str], str] | None,
) -> None:
    dataset = manifest["dataset"]
    if dataset.get("symbol_count") == 0:
        attempt.record(
            "dataset_unchanged",
            False,
            "no symbols recorded in the manifest: the dataset cannot be verified",
        )
        return
    now_fp = run_artifact._symbols_fingerprint(
        execution.symbols,
        dataset["interval"],
        dataset["data_as_of"],
        fingerprint_fn or manifest_mod.dataset_fingerprint,
    )
    attempt.record(
        "dataset_unchanged",
        now_fp == dataset["fingerprint"],
        "dataset fingerprint changed: history was corrected or backfilled",
    )


def _check_calendar(attempt: _Attempt, manifest: dict[str, Any], execution: Any) -> None:
    """The rerun derives the same trading calendar the run recorded.

    Skipped as passed, with a note, when the manifest has no calendar (a run made before the
    session layer existed). A rerun that derives none when the manifest has one fails.
    """
    recorded = manifest.get("calendar_version")
    if recorded is None:
        attempt.notes.append("calendar not compared: the manifest records no calendar_version")
        attempt.record("calendar_unchanged", True)
        return
    calendar = getattr(execution, "calendar", None)
    now_version = None if calendar is None else calendar.version
    attempt.record(
        "calendar_unchanged",
        now_version == recorded,
        "trading calendar changed: the set of trading days in the data is not the one the run "
        f"used (recorded {recorded}, rerun {now_version}); sessions were added, removed or "
        "backfilled",
    )


def _write_report(runs_dir: Path, report: ReproductionReport, now: datetime) -> Path:
    target_dir = runs_dir / f"{report.run_id}.reproductions"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(IST).strftime("%Y%m%dT%H%M%S")
    text = json.dumps(asdict(report), indent=2, allow_nan=False) + "\n"
    for n in range(1, 1000):
        name = f"{stamp}-ist.json" if n == 1 else f"{stamp}-{n}-ist.json"
        try:
            with open(target_dir / name, "x", encoding="utf-8") as handle:
                handle.write(text)
        except FileExistsError:
            continue
        return target_dir / name
    raise FileExistsError(f"no free report name in {target_dir}")


def reproduce(
    run_folder: Path,
    *,
    root: Path,
    execute: Callable[..., Any],
    supported: Iterable[str] | None = None,
    git_state_fn: Callable[[Path], dict[str, Any]] | None = None,
    dataset_fingerprint_fn: Callable[[Sequence[str], str, str], str] | None = None,
    now: datetime | None = None,
    write_report: bool = True,
) -> ReproductionReport:
    """Reproduce the run in `run_folder` and report whether it matches.

    The checks run in order: artifacts verify, the manifest is a valid schema-3 manifest, the
    manifest says the run is reproducible, the code commit is the run's and the tree is clean.
    Only if all four pass is the strategy rerun (with `end=data_as_of`), after which the dataset
    fingerprint and the trades table are compared. A check that fails adds a reason; none raises.
    """
    run_artifact.require_aware(now)
    folder = Path(run_folder)
    record_run.validate_run_id(folder.name)
    stamp = now or datetime.now(IST)
    attempt = _Attempt()
    compared = 0
    manifest = _load_manifest(folder)
    problems = manifest_mod.verify_artifacts(folder)
    attempt.record("artifacts_verified", not problems, *problems)
    invalid = (
        ["manifest unreadable"] if manifest is None else manifest_mod.validate_manifest(manifest)
    )
    if manifest is not None and manifest.get("schema") != run_artifact.SCHEMA:
        invalid = [*invalid, f"manifest schema is {manifest.get('schema')!r}, expected 3"]
    attempt.record("manifest_valid", not invalid, *(f"manifest invalid: {p}" for p in invalid))
    if manifest is not None and not invalid:
        ok = manifest["reproducible"] is True
        attempt.record("manifest_reproducible", ok, _why_not_reproducible(manifest))
        _check_code(attempt, manifest, root, git_state_fn)
        gates = ("artifacts_verified", "manifest_reproducible", "code_unchanged")
        if all(attempt.checks[g] for g in gates):
            key = _strategy_key(folder, supported)
            if key is None:
                attempt.record(
                    "rerun_completed", False, "rerun failed: the idea has no trade-list strategy"
                )
            else:
                execution = _rerun(attempt, folder, manifest, execute, key)
                if execution is not None:
                    _check_dataset(attempt, manifest, execution, dataset_fingerprint_fn)
                    _check_calendar(attempt, manifest, execution)
                    compared = _compare_trades(attempt, folder, execution)
    ok = all(attempt.checks.get(name) is True for name in CHECKS)
    report = ReproductionReport(
        run_id=folder.name,
        reproducible=ok,
        reasons=attempt.reasons,
        checks=attempt.checks,
        compared_trades=compared,
        attempted_ist=stamp.astimezone(IST).isoformat(),
        notes=attempt.notes,
    )
    if write_report:
        _write_report(folder.parent, report, stamp)
    return report
