"""The researcher's keep / reject decision on a run.

A decision is a human judgement recorded next to the run, in `decisions.yaml` at the idea's
folder. It is append-only, and it lives outside the sealed run folder. Nothing that calculates a
verdict, a promotion decision or a next action reads it (a test enforces that), so it can never
change a result.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from signal_engine.timeutils import IST

DECISIONS_FILE = "decisions.yaml"
DECISIONS = ("keep", "reject")


class DecisionError(Exception):
    """The decision is not keep or reject, or the run does not exist."""


def _run_exists(folder: Path, run_id: str) -> bool:
    runs = folder / "runs"
    return (runs / run_id).is_dir() or (runs / f"{run_id}.json").is_file()


def load(folder: Path) -> list[dict[str, Any]]:
    """Every recorded decision, oldest first. Empty when none was recorded."""
    path = folder / DECISIONS_FILE
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if not isinstance(data, list):
        raise DecisionError(f"{path}: must be a list of decisions")
    return data


def latest(folder: Path, run_id: str) -> dict[str, Any] | None:
    """The most recent decision for `run_id`, or None."""
    for entry in reversed(load(folder)):
        if entry.get("run_id") == run_id:
            return entry
    return None


def record(
    folder: Path, run_id: str, decision: str, note: str = "", now: datetime | None = None
) -> dict[str, Any]:
    """Append one decision. Earlier text is never rewritten; a later entry supersedes it."""
    if decision not in DECISIONS:
        raise DecisionError(f"decision must be keep or reject, got {decision!r}")
    if not _run_exists(folder, run_id):
        raise DecisionError(f"no run {run_id} in {folder}")
    entry: dict[str, Any] = {
        "run_id": run_id,
        "decision": decision,
        "decided_ist": (now or datetime.now(IST)).strftime("%Y-%m-%d %H:%M:%S"),
    }
    if note.strip():
        entry["note"] = note.strip()
    path = folder / DECISIONS_FILE
    existing = path.read_bytes().decode("utf-8") if path.exists() else ""
    block = yaml.safe_dump([entry], sort_keys=False, allow_unicode=True, width=10**9)
    prefix = "\n" if existing and not existing.endswith("\n") else ""
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(prefix + block)
    return entry
