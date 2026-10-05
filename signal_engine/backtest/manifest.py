"""Run manifest: schema validation, artifact verification and provenance helpers.

Implements lifecycle doc section 2.1 and 2.8. The manifest is built by the caller and
written by `artifacts.RunFolder.commit`; this module checks it, rehashes a finished
folder against it, and captures the dataset, environment and git state that go into it.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import subprocess
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from signal_engine.backtest import data as _data

MANIFEST_NAME = "manifest.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPO_ROOT = Path(__file__).resolve().parents[2]

MANIFEST_KEYS: tuple[str, ...] = (
    "schema",
    "run_id",
    "created_ist",
    "idea_id",
    "strategy",
    "fingerprints",
    "period",
    "dataset",
    "universe",
    "calendar_version",
    "deployment_profile",
    "session",
    "engine",
    "environment",
    "random",
    "parent_run_id",
    "change_classes",
    "missing_capabilities",
    "reproducible",
    "reproducibility",
    "artifacts",
)

#: Keys a schema 4 manifest adds (plan Phase 4): the published dataset version, the preflight, the
#: symbol counts, the run status and whether the run counted as a trial. Schema 3 manifests (runs
#: recorded before Phase 4) stay valid without them.
SCHEMA4_KEYS: tuple[str, ...] = (
    "preflight",
    "symbol_counts",
    "exclusions",
    "data_policy_version",
    "run_status",
    "invalid_reason",
    "trial_counted",
)
SCHEMAS: tuple[int, ...] = (3, 4)

NULLABLE: tuple[str, ...] = (
    "calendar_version",
    "session.exchange",
    "session.broker_cutoff",
    "session.force_flat",
    "strategy.branch_label",
    "dataset.corporate_action_policy",
    "universe.version",
    "parent_run_id",
    "random.monte_carlo",
)

#: Nulls that stand for a capability that is not built yet, so each needs a matching
#: `missing_capabilities` entry. `parent_run_id` (a first run) and `strategy.branch_label`
#: (a plain v<n> version) are legitimately null and need no explanation.
CAPABILITY_NULLABLE: tuple[str, ...] = tuple(
    p for p in NULLABLE if p not in ("parent_run_id", "strategy.branch_label")
)

FINGERPRINT_KEYS: tuple[str, ...] = (
    "rules",
    "params",
    "strategy",
    "execution",
    "cost",
    "universe",
    "profile",
    "dataset",
)
REPRODUCIBILITY_KEYS: tuple[str, ...] = (
    "strategy_version",
    "strategy_fingerprint",
    "dataset_fingerprint",
    "execution_config",
    "deployment_profile",
    "engine_version",
    "code_commit",
    "resolved_assumptions",
)

#: Required sub-keys of each object-valued top-level key (nullable ones may hold null).
_SUBKEYS: dict[str, tuple[str, ...]] = {
    "strategy": ("version", "parent_version", "branch_label"),
    "period": ("start", "end"),
    "dataset": (
        "id",
        "source",
        "interval",
        "data_as_of",
        "symbol_count",
        "fingerprint",
        "corporate_action_policy",
    ),
    "universe": ("definition", "symbols_sha256", "version"),
    "deployment_profile": ("name", "product", "sha256"),
    "session": ("exchange", "strategy_window", "broker_cutoff", "force_flat"),
    "engine": ("engine_version", "code_commit", "code_dirty", "dirty_paths"),
    "environment": ("python", "packages"),
    "random": ("seed", "monte_carlo"),
}
_STR_KEYS = ("run_id", "created_ist", "idea_id")


def _get(m: Mapping[str, Any], dotted: str) -> tuple[bool, Any]:
    """Return (present, value) for a dotted path."""
    cur: Any = m
    for part in dotted.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _check_structure(m: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    schema = m.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, int) or schema not in SCHEMAS:
        problems.append("schema must be the integer 3 or 4")
    elif schema >= 4:
        problems.extend(f"missing key: {k}" for k in SCHEMA4_KEYS if k not in m)
        if m.get("run_status") not in ("VALID", "INVALID_DATA"):
            problems.append("run_status must be VALID or INVALID_DATA")
        if not isinstance(m.get("trial_counted"), bool):
            problems.append("trial_counted must be a boolean")
        elif m.get("run_status") == "INVALID_DATA" and m.get("trial_counted"):
            problems.append("an INVALID_DATA run can never count as a trial")
        # A schema 4 run always resolved one published version (plan D20): never null.
        for key in ("preflight", "symbol_counts"):
            if key in m and not isinstance(m[key], dict):
                problems.append(f"{key} must be an object in a schema 4 manifest")
        dataset = m.get("dataset")
        if not (isinstance(dataset, dict) and isinstance(dataset.get("version_id"), str)):
            problems.append("dataset.version_id must be recorded in a schema 4 manifest")
    for key in _STR_KEYS:
        if key in m and not isinstance(m[key], str):
            problems.append(f"{key} must be a string")
    for key in ("change_classes", "missing_capabilities"):
        if key in m and not (isinstance(m[key], list) and all(isinstance(x, str) for x in m[key])):
            problems.append(f"{key} must be a list of strings")
    if "reproducible" in m and not isinstance(m["reproducible"], bool):
        problems.append("reproducible must be a bool")
    if "artifacts" in m:
        arts = m["artifacts"]
        if not isinstance(arts, Mapping) or not all(
            isinstance(k, str) and isinstance(v, str) and _SHA256.match(v) for k, v in arts.items()
        ):
            problems.append("artifacts must map relative path to sha256 hex")
    for key, subs in _SUBKEYS.items():
        if key not in m:
            continue
        if not isinstance(m[key], Mapping):
            problems.append(f"{key} must be an object")
            continue
        problems.extend(f"{key}.{sub} is missing" for sub in subs if sub not in m[key])
    fps = m.get("fingerprints")
    if "fingerprints" in m:
        if not isinstance(fps, Mapping):
            problems.append("fingerprints must be an object")
        else:
            for name in FINGERPRINT_KEYS:
                if name not in fps:
                    problems.append(f"fingerprints.{name} is missing")
                elif not (isinstance(fps[name], str) and _SHA256.match(fps[name])):
                    problems.append(f"fingerprints.{name} must be a sha256 hex string")
    rep = m.get("reproducibility")
    if "reproducibility" in m:
        if not isinstance(rep, Mapping):
            problems.append("reproducibility must be an object")
        else:
            for name in REPRODUCIBILITY_KEYS:
                if name not in rep:
                    problems.append(f"reproducibility.{name} is missing")
                elif not isinstance(rep[name], bool):
                    problems.append(f"reproducibility.{name} must be a bool")
    return problems


def validate_manifest(m: Mapping[str, Any]) -> list[str]:
    """Return a list of problems with a manifest dict. An empty list means it is valid."""
    if not isinstance(m, Mapping):
        return ["manifest must be an object"]
    problems = [f"missing key: {k}" for k in MANIFEST_KEYS if k not in m]
    problems.extend(_check_structure(m))
    caps = m.get("missing_capabilities")
    caps_text = [c for c in caps if isinstance(c, str)] if isinstance(caps, list) else []
    for dotted in CAPABILITY_NULLABLE:
        present, value = _get(m, dotted)
        if present and value is None:
            leaf = dotted.rsplit(".", 1)[-1]
            if not any(leaf in c for c in caps_text):
                problems.append(f"{dotted} is null with no matching missing_capabilities entry")
    # A first strategy version has no parent; the doc leaves this null without a capability gap.
    nullable = set(NULLABLE) | {"strategy.parent_version"}
    for key, subs in _SUBKEYS.items():
        obj = m.get(key)
        if isinstance(obj, Mapping):
            for sub in subs:
                if f"{key}.{sub}" not in nullable and sub in obj and obj[sub] is None:
                    problems.append(f"{key}.{sub} must not be null")
    for key in MANIFEST_KEYS:
        if key not in nullable and key in m and m[key] is None:
            problems.append(f"{key} must not be null")
    return problems


def manifest_sha256(folder: Path) -> str:
    """Return the sha256 hex of a run folder's manifest.json bytes."""
    return hashlib.sha256((Path(folder) / MANIFEST_NAME).read_bytes()).hexdigest()


def verify_artifacts(folder: Path) -> list[str]:
    """Rehash a run folder against its manifest. An empty list means the folder is clean."""
    folder = Path(folder)
    try:
        manifest = json.loads((folder / MANIFEST_NAME).read_text(encoding="utf-8"))
        artifacts = manifest["artifacts"]
        if not isinstance(artifacts, dict):
            raise TypeError("artifacts is not an object")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [f"manifest unreadable: {type(exc).__name__}: {exc}"]
    problems: list[str] = []
    for rel, digest in sorted(artifacts.items()):
        path = folder / rel
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            problems.append(f"missing artifact: {rel}")
            continue
        if actual != digest:
            problems.append(f"hash mismatch: {rel}")
    on_disk = {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()} - {
        MANIFEST_NAME
    }
    problems.extend(f"unlisted file: {rel}" for rel in sorted(on_disk - set(artifacts)))
    return problems


_STORED_INTERVAL = {"1m": "1m", "5m": "1m", "15m": "1m", "1h": "1m", "1d": "D"}


def stored_interval(bar_size: str) -> str:
    """The `market_data.interval` the loaders read for a strategy bar size.

    Intraday runs read 1m rows and resample; daily runs read `D` rows.

    Raises:
        ValueError: The bar size is not one the loaders support.
    """
    try:
        return _STORED_INTERVAL[bar_size]
    except (KeyError, TypeError):
        raise ValueError(
            f"unsupported bar size {bar_size!r}; use one of {sorted(_STORED_INTERVAL)}"
        ) from None


def dataset_fingerprint(
    symbols: Sequence[str], bar_size: str, as_of: str, db_path: Path | None = None
) -> str:
    """Digest the bars a run could have seen.

    `bar_size` is mapped to the stored interval the loaders read. Per symbol the digest holds
    the row count, first and last timestamp and the rounded (4 dp) sums of open, high, low,
    close and volume. Only rows with `timestamp` up to the end of `as_of` (IST) count, so later
    appends do not change the digest while an edited historical value does.
    """
    import duckdb

    interval = stored_interval(bar_size)
    end = pd.Timestamp(as_of, tz="Asia/Kolkata") + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    end_epoch = int(end.timestamp())
    names = sorted(set(symbols))
    found: dict[str, list[Any]] = {}
    if names:
        marks = ",".join("?" for _ in names)
        sql = (
            "SELECT symbol, COUNT(*), MIN(timestamp), MAX(timestamp), "
            "SUM(open), SUM(high), SUM(low), SUM(close), SUM(volume) "
            f"FROM market_data WHERE interval = ? AND timestamp <= ? AND symbol IN ({marks}) "
            "GROUP BY symbol"
        )
        con = duckdb.connect(
            str(db_path or _data.HISTORIFY_DB), read_only=True, config=_data._DUCKDB_CONFIG
        )
        try:
            for sym, n, first, last, *sums in con.execute(
                sql, [interval, end_epoch, *names]
            ).fetchall():
                found[sym] = [
                    sym,
                    int(n),
                    int(first),
                    int(last),
                    *(round(float(v or 0.0), 4) for v in sums),
                ]
        finally:
            con.close()
    rows = [found.get(s, [s, 0, None, None, 0.0, 0.0, 0.0, 0.0, 0.0]) for s in names]
    payload = json.dumps(rows, separators=(",", ":"), sort_keys=True, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def environment_snapshot(packages: Sequence[str], lock_path: Path | None = None) -> dict[str, Any]:
    """Return the Python version and pinned versions of `packages` from uv.lock.

    Raises:
        KeyError: A requested package is absent from the lock file.
    """
    lock = tomllib.loads(Path(lock_path or _REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    pinned = {_norm(p["name"]): p["version"] for p in lock.get("package", []) if "version" in p}
    out: dict[str, str] = {}
    for name in packages:
        if _norm(name) not in pinned:
            raise KeyError(f"package not in uv.lock: {name}")
        out[_norm(name)] = pinned[_norm(name)]
    return {"python": platform.python_version(), "packages": out}


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes] | None:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc if proc.returncode == 0 else None


def git_state(root: Path) -> dict[str, Any]:
    """Return {code_commit, code_dirty, dirty_paths} for the checkout at `root`.

    Dirty paths cover modified, added, deleted and untracked-but-not-ignored files. When git
    is unavailable or `root` is not a checkout the state is unknown and counts as dirty.
    """
    unknown = {"code_commit": "unknown", "code_dirty": True, "dirty_paths": []}
    head = _run_git(Path(root), "rev-parse", "HEAD")
    status = _run_git(Path(root), "status", "--porcelain", "-z", "--untracked-files=all")
    if head is None or status is None or not head.stdout.strip():
        return unknown
    paths: set[str] = set()
    entries = status.stdout.decode("utf-8", "surrogateescape").split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.add(entry[3:])
        if entry[0] in "RC":
            i += 1  # the original path of a rename or copy follows as its own field
    return {
        "code_commit": head.stdout.decode().strip(),
        "code_dirty": bool(paths),
        "dirty_paths": sorted(paths),
    }
