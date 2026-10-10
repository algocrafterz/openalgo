"""Append-only attestations for gates a program cannot decide alone (G0.1 now, G0.7 next).

A gate such as G0.1 (same logic in every mode) rests on a code review plus automated evidence at
one commit. An attestation records that review so a run can rely on it only while it still holds:

  RECORD.   One JSON line in `attestations/ledger.jsonl`: gate, exact commit, covered paths, the
            strategy classes it covers, the evidence (checks run, results, notes), the reviewer
            and date. Each line carries `prev_sha256` (the previous line's hash) and its own
            `content_sha256`, so the ledger is an append-only chain: an edit, a deletion or a
            reordering breaks it.
  VALIDITY. A run may rely on a record only if (1) the chain is intact, (2) the record covers the
            run's strategy class, (3) its commit is an ancestor of (or equal to) the run's commit
            and (4) nothing under any covered path changed between the two commits or is
            uncommitted now. Otherwise the gate is not evaluated, which blocks EDGE.
  LIMITS.   The hashes detect edits and the git checks detect code that moved on. They do not
            prove the review was right; the reviewer and the notes are in the evidence text so a
            reader can judge it, and a later record may add a second reviewer.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from signal_engine.backtest.cost_contribution import seal

SCHEMA = "attestation/1"
GENESIS = "0" * 64
LEDGER_NAME = Path("attestations") / "ledger.jsonl"
REQUIRED = (
    "schema", "id", "gate", "commit", "covered_paths", "strategies", "evidence", "reviewer",
    "date", "prev_sha256", "content_sha256",
)  # fmt: skip

VALID, MISSING, STALE, INVALID = "valid", "missing", "stale", "invalid"


class AttestationError(ValueError):
    """The record cannot be appended."""


def ledger_path(root: str | Path) -> Path:
    return Path(root) / LEDGER_NAME


def _git(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, timeout=60, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _top(paths: Sequence[str]) -> list[str]:
    return [f":(top){p}" for p in paths]


def _body(record: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k != "content_sha256"}


def load(path: str | Path) -> list[dict[str, Any]]:
    """The ledger's records in order. A missing file is an empty ledger; a bad line raises."""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise AttestationError(f"line {n} is not JSON: {exc}") from exc
        if not isinstance(record, dict):
            raise AttestationError(f"line {n} is not a record")
        out.append(record)
    return out


def chain_problems(records: Sequence[Mapping[str, Any]]) -> list[str]:
    """Why the ledger cannot be trusted (empty when the chain, ids and fields are sound)."""
    problems: list[str] = []
    prev, seen = GENESIS, set()
    for i, r in enumerate(records, 1):
        missing = [k for k in REQUIRED if k not in r]
        if missing:
            problems.append(f"record {i} lacks {', '.join(missing)}")
            continue
        try:
            sound = r["content_sha256"] == seal(_body(r))
        except (TypeError, ValueError):
            sound = False
        if not sound:
            problems.append(f"record {r['id']} does not match its content hash")
        if r["prev_sha256"] != prev:
            problems.append(f"record {r['id']} does not follow the previous record")
        if r["id"] in seen:
            problems.append(f"record id {r['id']} is repeated")
        if r["schema"] != SCHEMA:
            problems.append(f"record {r['id']} has schema {r['schema']!r}")
        seen.add(r["id"])
        prev = r["content_sha256"]
    return problems


def append(
    ledger: str | Path,
    *,
    root: str | Path,
    gate: str,
    commit: str,
    covered_paths: Sequence[str],
    strategies: Sequence[str],
    evidence: Mapping[str, Any],
    reviewer: str,
    on: date | None = None,
) -> dict[str, Any]:
    """Add one record to the end of the ledger and return it. Never rewrites an earlier line.

    Refuses when the commit is unknown, a covered path does not exist at that commit or has
    uncommitted changes, or the evidence, reviewer or strategies are empty."""
    root = Path(root)
    if not (gate and covered_paths and strategies and evidence and reviewer.strip()):
        raise AttestationError("gate, covered paths, strategies, evidence and reviewer are required")
    resolved = _git(root, "rev-parse", "--verify", f"{commit}^{{commit}}")
    if resolved is None or resolved.returncode != 0:
        raise AttestationError(f"commit {commit!r} is not known to this repository")
    sha = resolved.stdout.decode().strip()
    for p in covered_paths:
        listed = _git(root, "ls-tree", "-r", "--name-only", sha, "--", *_top([p]))
        if listed is None or listed.returncode != 0 or not listed.stdout.strip():
            raise AttestationError(f"covered path {p!r} does not exist at {sha[:9]}")
    dirty = _git(root, "status", "--porcelain", "--untracked-files=all", "--", *_top(covered_paths))
    if dirty is None or dirty.returncode != 0 or dirty.stdout.strip():
        raise AttestationError("a covered path has uncommitted changes; commit them first")
    existing = load(ledger)
    problems = chain_problems(existing)
    if problems:
        raise AttestationError("the ledger is not sound: " + "; ".join(problems))
    same = sum(1 for r in existing if r["gate"] == gate)
    body = {
        "schema": SCHEMA,
        "id": f"{gate}-{same + 1:03d}",
        "gate": gate,
        "commit": sha,
        "covered_paths": list(covered_paths),
        "strategies": list(strategies),
        "evidence": json.loads(json.dumps(evidence)),
        "reviewer": reviewer.strip(),
        "date": (on or date.today()).isoformat(),
        "prev_sha256": existing[-1]["content_sha256"] if existing else GENESIS,
    }
    record = {**body, "content_sha256": seal(body)}
    path = Path(ledger)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
    return record


def _summary(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "commit": record["commit"],
        "reviewer": record["reviewer"],
        "date": record["date"],
        "covered_paths": list(record["covered_paths"]),
        "checks": len((record.get("evidence") or {}).get("checks", [])),
    }


def verify(
    gate: str,
    strategy: str | None,
    run_commit: str | None,
    root: str | Path,
    dirty_paths: Sequence[str] = (),
) -> dict[str, Any]:
    """Whether a run at `run_commit` may rely on an attestation of `gate` for `strategy`.

    Returns `{status, reasons, record}` with status valid, missing, stale or invalid."""
    out: dict[str, Any] = {"gate": gate, "status": MISSING, "reasons": [], "record": None}
    try:
        records = load(ledger_path(root))
    except AttestationError as exc:
        return {**out, "status": INVALID, "reasons": [f"the ledger cannot be read: {exc}"]}
    if not records:
        return {**out, "reasons": ["no attestation ledger"]}
    problems = chain_problems(records)
    if problems:
        return {**out, "status": INVALID, "reasons": ["the ledger chain is broken: " + "; ".join(problems)]}
    mine = [r for r in records if r["gate"] == gate and strategy in r["strategies"]]
    if not mine:
        return {**out, "reasons": [f"no {gate} attestation covers strategy {strategy!r}"]}
    if not run_commit or run_commit == "unknown":
        return {**out, "status": INVALID, "reasons": ["the run's commit is unknown"]}
    last: dict[str, Any] | None = None
    for record in reversed(mine):
        result = _check(record, run_commit, Path(root), dirty_paths)
        summary = {**_summary(record), "modes": ((record.get("evidence") or {}).get("modes") or {}).get(strategy)}
        if result["status"] == VALID:
            return {**out, **result, "record": summary}
        last = {**out, **result, "record": summary}
    assert last is not None
    return last


def _check(record: Mapping[str, Any], run_commit: str, root: Path, dirty: Sequence[str]) -> dict[str, Any]:
    known = _git(root, "rev-parse", "--verify", f"{record['commit']}^{{commit}}")
    if known is None or known.returncode != 0:
        return {"status": INVALID, "reasons": [f"commit {record['commit'][:9]} is not in this repository"]}
    ancestor = _git(root, "merge-base", "--is-ancestor", record["commit"], run_commit)
    if ancestor is None:
        return {"status": INVALID, "reasons": ["git is unavailable"]}
    if ancestor.returncode != 0:
        return {
            "status": INVALID,
            "reasons": [f"commit {record['commit'][:9]} is not an ancestor of the run's commit"],
        }
    diff = _git(root, "diff", "--name-only", record["commit"], run_commit, "--", *_top(record["covered_paths"]))
    if diff is None or diff.returncode != 0:
        return {"status": INVALID, "reasons": ["git could not compare the commits"]}
    changed = [ln for ln in diff.stdout.decode().splitlines() if ln.strip()]
    prefixes = [p.rstrip("/") for p in record["covered_paths"]]
    uncommitted = [d for d in dirty if any(d == p or d.startswith(p + "/") for p in prefixes)]
    reasons = []
    if changed:
        reasons.append(
            f"{len(changed)} covered file(s) changed since the attested commit, e.g. {changed[0]}"
        )
    if uncommitted:
        reasons.append(f"{len(uncommitted)} covered file(s) have uncommitted changes, e.g. {uncommitted[0]}")
    return {"status": STALE if reasons else VALID, "reasons": reasons}


def verify_for_run(
    *,
    root: str | Path,
    strategy_class: str | None,
    run_commit: str | None,
    dirty_paths: Sequence[str] = (),
    gate: str = "G0.1",
    check: Callable[..., dict[str, Any]] = verify,
) -> dict[str, Any]:
    """The lookup a recording does for its own run."""
    return check(gate, strategy_class, run_commit, root, dirty_paths)
