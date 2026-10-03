"""Trial ledger: an append-only record of the material configurations an idea has evaluated.

`ideas/<idea>/trials.yaml` is a block-style list. A trial follows the acceptance-criteria
definition: an evaluated change to strategy logic, parameter set, execution assumption or
cost. Two kinds of entry exist:

- `material`: `{trial, kind, run_id, fingerprints: {rules, params, execution,
  cost}, created_ist}`, recorded once per DISTINCT four-fingerprint tuple. A dataset, data-range,
  universe-membership (a data refresh adding a symbol), engine or deployment-profile change
  alone, or an identical re-run, adds nothing.
- `external`: `{trial, kind, count, reason, created_ist}` for variants searched outside the
  system.

`n_trials = material entries + sum of external counts`. New entries are appended to the
file's text; existing bytes are never rewritten.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from signal_engine.research.intake.versions import _is_block_style, require_aware
from signal_engine.timeutils import IST

TRIALS_FILE = "trials.yaml"
#: The tuple a trial is keyed on. The universe fingerprint is deliberately NOT part of it: it
#: includes the sorted symbol list, so a routine data refresh would otherwise create a new trial
#: on a re-run of an identical frozen spec. A universe selection change reaches the ledger through
#: a code (engine) change or an explicit external trial (`trials add IDEA-n N --reason TEXT`).
MATERIAL_KEYS = ("rules", "params", "execution", "cost")
MATERIAL = "material"
EXTERNAL = "external"

_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")


class TrialError(Exception):
    """trials.yaml is invalid, or a trial being recorded is not acceptable."""


@dataclass(frozen=True)
class Trial:
    """One ledger entry. Material entries carry run_id and fingerprints; external ones
    carry count and reason."""

    trial: int
    kind: str
    created_ist: str
    run_id: str | None = None
    fingerprints: Mapping[str, str] | None = None
    count: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class TrialCount:
    material: int
    external: int
    total: int


@dataclass(frozen=True)
class TrialResult:
    trial: int | None
    created: bool
    count: TrialCount


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_fingerprints(path: str, number: int, value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise TrialError(f"{path}: trial {number} needs a fingerprints mapping")
    for key in MATERIAL_KEYS:
        fp = value.get(key)
        if not (isinstance(fp, str) and _FINGERPRINT.fullmatch(fp)):
            raise TrialError(f"{path}: trial {number} has an invalid {key} fingerprint")
    return {key: value[key] for key in MATERIAL_KEYS}


def _check_row(path: str, row: Any) -> Trial:
    if not isinstance(row, dict):
        raise TrialError(f"{path}: every entry must be a mapping")
    number = row.get("trial")
    if not (_is_int(number) and number >= 1):
        raise TrialError(f"{path}: invalid trial number {number!r}")
    stamp = row.get("created_ist")
    if not isinstance(stamp, str):
        raise TrialError(f"{path}: trial {number} needs a text created_ist")
    kind = row.get("kind")
    if kind == MATERIAL:
        run_id = row.get("run_id")
        if not (isinstance(run_id, str) and run_id.strip()):
            raise TrialError(f"{path}: trial {number} needs a run_id")
        fps = _check_fingerprints(path, number, row.get("fingerprints"))
        return Trial(number, kind, stamp, run_id=run_id, fingerprints=fps)
    if kind == EXTERNAL:
        count, reason = row.get("count"), row.get("reason")
        if not (_is_int(count) and count >= 1):
            raise TrialError(f"{path}: trial {number} needs a positive count")
        if not (isinstance(reason, str) and reason.strip()):
            raise TrialError(f"{path}: trial {number} needs a non-empty reason")
        return Trial(number, kind, stamp, count=count, reason=reason)
    raise TrialError(f"{path}: trial {number} has an invalid kind {kind!r}")


def _parse(text: str, source: str) -> list[Trial]:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TrialError(f"{source}: trials.yaml is unreadable ({exc})") from exc
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TrialError(f"{source}: trials.yaml must be a list")
    found: list[Trial] = []
    for row in raw:
        entry = _check_row(source, row)
        if any(entry.trial == t.trial for t in found):
            raise TrialError(f"{source}: duplicate trial number {entry.trial}")
        if found and entry.trial < found[-1].trial:
            raise TrialError(f"{source}: trial {entry.trial} is out of order")
        if entry.kind == MATERIAL and any(
            t.kind == MATERIAL and t.fingerprints == entry.fingerprints for t in found
        ):
            raise TrialError(f"{source}: duplicate material configuration on trial {entry.trial}")
        found.append(entry)
    return found


def load_trials(folder: Path) -> list[Trial]:
    """The idea's trials in file order. Raises TrialError for an invalid file."""
    path = folder / TRIALS_FILE
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TrialError(f"{path}: trials.yaml is unreadable ({exc})") from exc
    return _parse(text, str(path))


def _count(entries: list[Trial]) -> TrialCount:
    material = sum(1 for t in entries if t.kind == MATERIAL)
    external = sum(t.count or 0 for t in entries if t.kind == EXTERNAL)
    return TrialCount(material, external, material + external)


def count(folder: Path) -> TrialCount:
    """Material entries, external trial total, and their sum."""
    return _count(load_trials(folder))


def _stamp(now: datetime | None) -> str:
    return (now or datetime.now(IST)).astimezone(IST).isoformat(timespec="seconds")


def _append(folder: Path, row: dict) -> None:
    path = folder / TRIALS_FILE
    existing = path.read_bytes().decode("utf-8") if path.exists() else ""
    if not _is_block_style(existing):
        raise TrialError(f"{path}: must be a block-style list to append to (one '- ' per entry)")
    block = yaml.safe_dump([row], sort_keys=False, allow_unicode=True, width=10**9)
    prefix = "\n" if existing and not existing.endswith("\n") else ""
    folder.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(prefix + block)


def _next_number(entries: list[Trial]) -> int:
    return max((t.trial for t in entries), default=0) + 1


def register_material(
    folder: Path,
    fingerprints: Mapping[str, str],
    run_id: str,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> TrialResult:
    """Record the four-fingerprint tuple once. A known tuple writes nothing, so a retry with
    the same run id (or another run of the same configuration) never double counts.

    Only rules, params, execution and cost are used; a missing one raises TrialError and other
    keys (universe, dataset, profile, ...) are ignored.
    With `dry_run` every check runs and the result says what would happen, but nothing is
    written.
    """
    require_aware(now)
    tuple_ = _check_fingerprints(str(folder / TRIALS_FILE), 0, dict(fingerprints))
    if not (isinstance(run_id, str) and run_id.strip()):
        raise TrialError("run_id must be non-empty text")
    entries = load_trials(folder)
    for entry in entries:
        if entry.kind == MATERIAL and entry.fingerprints == tuple_:
            return TrialResult(entry.trial, False, _count(entries))
    number = _next_number(entries)
    new = Trial(number, MATERIAL, _stamp(now), run_id=run_id, fingerprints=tuple_)
    if not dry_run:
        _append(
            folder,
            {
                "trial": number,
                "kind": MATERIAL,
                "run_id": run_id,
                "fingerprints": dict(tuple_),
                "created_ist": new.created_ist,
            },
        )
    return TrialResult(number, True, _count([*entries, new]))


def add_external(
    folder: Path, count: int, reason: str, *, now: datetime | None = None
) -> TrialResult:
    """Record `count` variants searched outside the system, with the reason."""
    require_aware(now)
    if not (_is_int(count) and count >= 1):
        raise TrialError(f"count must be a positive integer, got {count!r}")
    if not (isinstance(reason, str) and reason.strip()):
        raise TrialError("an external trial needs a non-empty reason")
    entries = load_trials(folder)
    number = _next_number(entries)
    stamp = _stamp(now)
    _append(
        folder,
        {
            "trial": number,
            "kind": EXTERNAL,
            "count": count,
            "reason": reason.strip(),
            "created_ist": stamp,
        },
    )
    new = Trial(number, EXTERNAL, stamp, count=count, reason=reason.strip())
    return TrialResult(number, True, _count([*entries, new]))
