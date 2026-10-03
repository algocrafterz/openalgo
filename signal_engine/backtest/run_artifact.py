"""Build a run's immutable folder and the flat schema-3 record that indexes it.

Implements lifecycle doc sections 2.1 to 2.7. `record_run.record_idea` calls
`prepare_run_artifact` once the backtest has executed and the v2 record is built; it builds and
validates everything in memory, then `PreparedRun.commit` writes the folder atomically through
`artifacts.RunFolder` and the caller places the flat record afterwards.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

import yaml

from signal_engine.backtest import manifest as manifest_mod
from signal_engine.backtest import next_action as next_action_mod
from signal_engine.backtest import promotion as promotion_mod
from signal_engine.backtest import verdict as verdict_mod
from signal_engine.backtest.artifacts import RunFolder, _encode
from signal_engine.backtest.change_class import classify, first_run_classes
from signal_engine.backtest.dq_run import POLICY as DATA_POLICY
from signal_engine.backtest.fingerprints import canonical_json, component_fingerprints, sha256_hex
from signal_engine.backtest.session import session_model
from signal_engine.backtest.trades_io import (
    LEDGER_SCHEMAS,
    frame_parquet_bytes,
    trades_parquet_bytes,
)
from signal_engine.research.intake import ledger as ledger_mod
from signal_engine.research.intake import profiles as profiles_mod
from signal_engine.research.intake.versions import require_aware
from signal_engine.timeutils import IST

ENGINE_VERSION = "1.0"
SCHEMA = 3
#: Packages whose versions are pinned into the manifest (names as in uv.lock).
ENVIRONMENT_PACKAGES = ("pandas", "numpy", "pyarrow", "duckdb", "pyyaml")
_CONFIG_BASENAMES = ("config.yaml", "uv.lock", "pyproject.toml")
#: Top-level directories whose Python files can change a backtest result.
_CODE_ROOTS = ("signal_engine", "utils")
_MAX_LISTED_PATHS = 10
_PLAIN_VERSION = re.compile(r"^v\d+$")
_RUN_NUMBER = re.compile(r"-v(\d+)$")
#: Nullable manifest paths that engine step 5 fills in.
_STEP5_PATHS = (
    "calendar_version",
    "session.exchange",
    "session.broker_cutoff",
    "dataset.corporate_action_policy",
    "universe.version",
    "random.monte_carlo",
)
_REPORT_BLOCKS = (
    "position_caps",
    "regimes",
    "cost_model",
    "split",
    "baseline",
    "cost_sensitivity",
)


class DirtyTreeError(Exception):
    """Result-affecting code or configuration is uncommitted, so the run cannot be reproduced."""


def _is_result_affecting(path: str) -> bool:
    pure = PurePosixPath(path)
    if pure.name in _CONFIG_BASENAMES or "research/profiles/" in path:
        return True
    parts = pure.parts
    return pure.suffix == ".py" and bool(parts) and parts[0] in _CODE_ROOTS and "tests" not in parts


def result_affecting_paths(paths: Iterable[str]) -> list[str]:
    """The dirty paths that can change a backtest result.

    Python under `signal_engine/` or `utils/` (outside any `tests` directory), config.yaml,
    uv.lock, pyproject.toml and deployment profiles count. Docs, generated registry files, run
    output, specs, tests and code of other packages (blueprints, database, broker, frontend)
    do not.
    """
    return [path for path in paths if _is_result_affecting(path)]


def check_clean(git: Mapping[str, Any], allow_dirty: bool) -> dict[str, Any]:
    """Reduce a `git_state` to the run's engine provenance, refusing a dirty tree.

    Raises:
        DirtyTreeError: Result-affecting paths are dirty, or the commit is unknown, and
            `allow_dirty` is false.
    """
    counted = result_affecting_paths(git.get("dirty_paths", []))
    commit = git.get("code_commit", "unknown")
    dirty = bool(counted) or commit == "unknown"
    if dirty and not allow_dirty:
        shown = counted[:_MAX_LISTED_PATHS]
        more = len(counted) - len(shown)
        detail = ", ".join(shown) + (f" (+{more} more)" if more > 0 else "")
        if commit == "unknown":
            detail = (detail + "; " if detail else "") + "git state is unknown"
        raise DirtyTreeError(
            f"working tree has uncommitted result-affecting changes: {detail}. "
            "Commit them, or pass --allow-dirty to record a non-reproducible run."
        )
    return {"code_commit": commit, "code_dirty": dirty, "dirty_paths": counted}


def _created(now: datetime) -> str:
    require_aware(now)
    return now.astimezone(IST).isoformat()


def git_changed_during_run(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    """True when HEAD moved or new result-affecting paths became dirty between two reads."""
    if before.get("code_commit") != after.get("code_commit"):
        return True
    old = set(result_affecting_paths(before.get("dirty_paths", [])))
    return bool(set(result_affecting_paths(after.get("dirty_paths", []))) - old)


def settle_git(
    before: Mapping[str, Any], after: Mapping[str, Any], allow_dirty: bool
) -> dict[str, Any]:
    """The engine provenance after a second git read taken at the end of the run.

    Raises:
        DirtyTreeError: The tree changed during the run and `allow_dirty` is false.
    """
    if not git_changed_during_run(before, after):
        return check_clean(after, allow_dirty)
    if not allow_dirty:
        raise DirtyTreeError("working tree changed during the run")
    union = sorted(
        set(result_affecting_paths(before.get("dirty_paths", [])))
        | set(result_affecting_paths(after.get("dirty_paths", [])))
    )
    return {
        "code_commit": before.get("code_commit", "unknown"),
        "code_dirty": True,
        "dirty_paths": union,
    }


def _run_order(manifest: Mapping[str, Any]) -> tuple[str, int]:
    match = _RUN_NUMBER.search(str(manifest.get("run_id", "")))
    return str(manifest.get("created_ist", "")), int(match.group(1)) if match else 0


def _load_manifests(runs_dir: Path) -> dict[str, dict[str, Any]]:
    """Manifests of every run folder that has one; a legacy flat-only run is absent."""
    found: dict[str, dict[str, Any]] = {}
    if not runs_dir.is_dir():
        return found
    for folder in sorted(p for p in runs_dir.iterdir() if p.is_dir() and p.name[0] != "."):
        path = folder / "manifest.json"
        if path.is_file():
            found[folder.name] = json.loads(path.read_text(encoding="utf-8"))
    return found


def reference_manifest(
    runs_dir: Path, *, parent_run_id: str | None, version: str | None, parent_version: str | None
) -> dict[str, Any] | None:
    """The manifest a new run is compared with: the named parent run (none if it has no
    manifest), else the latest run of the same strategy version, else the latest run of the
    parent version, else none."""
    manifests = _load_manifests(runs_dir)
    if parent_run_id is not None:
        # A named parent with no manifest (a legacy flat-only run) leaves no reference:
        # falling back to another run would compare against something the caller did not name.
        return manifests.get(parent_run_id)
    for wanted in (version, parent_version):
        if wanted is None:
            continue
        same = [m for m in manifests.values() if m.get("strategy", {}).get("version") == wanted]
        if same:
            return max(same, key=_run_order)
    return None


def _symbols_fingerprint(
    symbols: Sequence[str], interval: str, as_of: str, fn: Callable[[Sequence[str], str, str], str]
) -> str:
    if not symbols:
        return sha256_hex(canonical_json([]))
    return fn(sorted(symbols), interval, as_of)


def _null_notes(manifest: Mapping[str, Any]) -> list[str]:
    """One note per manifest path that is still null, with its reason.

    A run that carries the data and session layer (it has a `calendar_version`) fills every
    step-5 path, so only `random.monte_carlo` and, for CNC, the MIS-only session fields remain.
    A run without the layer keeps the original "not captured until engine step 5" notes.
    """
    supplied = manifest.get("calendar_version") is not None
    product = (manifest.get("deployment_profile") or {}).get("product")
    notes = []
    for dotted in _STEP5_PATHS:
        present, value = manifest_mod._get(manifest, dotted)
        if not (present and value is None):
            continue
        if dotted == "session.broker_cutoff" and supplied and product != "MIS":
            notes.append(f"{dotted} is null: {product} delivery has no intraday broker cutoff")
        elif supplied and dotted != "random.monte_carlo":
            notes.append(f"{dotted} not captured: the run supplied no value")
        else:
            notes.append(f"{dotted} not captured until engine step 5")
    if manifest["session"]["force_flat"] is None:
        if supplied and product == "CNC":
            notes.append(
                "session.force_flat is null: CNC delivery has no force-flat, positions carry forward"
            )
        else:
            notes.append(
                "session.force_flat is null: the profile has no MIS square-off for this run"
            )
    return notes


def _json_safe(value: Any) -> Any:
    """`value` as plain JSON types (numpy scalars and dates converted), NaN refused later."""

    def default(obj: Any) -> Any:
        if hasattr(obj, "item") and callable(obj.item):
            return obj.item()
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        return str(obj)

    return json.loads(json.dumps(value, default=default))


def _profile_rules(frozen: Any, product: str) -> Any:
    """The frozen profile's `ProductRules` for MIS, or None (CNC has no intraday cutoffs)."""
    block = ((getattr(frozen, "profile", None) or {}).get("products") or {}).get(product)
    if product != "MIS" or not isinstance(block, Mapping):
        return None
    problems: list[str] = []
    rules = profiles_mod._parse_mis(block, problems)
    return None if problems else rules


def universe_version(membership: Mapping[str, Any] | None, universe_frame: Any) -> str | None:
    """`eligibility-<sha8>` of the eligibility policy and the universe records, or None."""
    if universe_frame is None:
        return None
    policy = (membership or {}).get("eligibility_policy")
    records = json.loads(universe_frame.to_json(orient="records"))
    digest = sha256_hex(canonical_json({"policy": policy, "universe": records}))
    return "eligibility-" + digest[:8]


def _assumptions(frozen: Any, folder: Path, created: str) -> dict[str, Any]:
    entries = [e.as_dict() for e in frozen.ledger]
    ids = [int(e["id"].split("-")[1]) for e in entries]
    next_id = max(max(ids, default=0) + 1, ledger_mod.sealed_next_id(folder))
    return {"sealed_ist": created, "next_id": next_id, "entries": entries}


def _strategy_json(idea_id: str, frozen: Any) -> dict[str, Any]:
    intent = frozen.intent
    return {
        "strategy_id": idea_id,
        "name": intent.get("name"),
        "version": frozen.version,
        "parent_version": frozen.parent_version,
        "branch_label": _branch_label(frozen.version),
        "strategy_fingerprint": frozen.strategy_fingerprint,
        "rules_fingerprint": frozen.rules_fingerprint,
        "params_fingerprint": frozen.params_fingerprint,
        "style": intent.get("style"),
        "direction": intent.get("direction"),
        "timeframe": intent.get("timeframe"),
        "product": intent.get("product"),
    }


def _branch_label(version: str | None) -> str | None:
    return None if version is None or _PLAIN_VERSION.match(version) else version


def _derived(
    record: Mapping[str, Any],
    readiness_missing: Sequence[str],
    code_dirty: bool,
    reproducible: bool,
    data_quality: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Verdict, promotion and next action for a run, derived from its evaluation block.

    `data_quality` is the run's real report (status, affects_validity, items); without one the
    constant NOT_RUN stands in.
    """
    evaluation = record.get("evaluation") or {}
    validity = {"ok": True, "failures": []}
    data_quality = (
        dict(data_quality)
        if data_quality is not None
        else {"status": "NOT_RUN", "affects_validity": False, "items": []}
    )
    run_verdict = verdict_mod.derive(
        evaluation,
        validity=validity,
        data_quality=data_quality,
        readiness_missing=list(readiness_missing),
        code_dirty=code_dirty,
        reproducible=reproducible,
    )
    promo = promotion_mod.evaluate_promotion(
        evaluation,
        code_dirty=code_dirty,
        reproducible=reproducible,
        readiness_missing=list(readiness_missing),
    )
    action = next_action_mod.next_action(
        promo,
        evaluation,
        code_dirty=code_dirty,
        reproducible=reproducible,
        primary_gate=run_verdict.get("primary_failed_gate"),
    )
    return {
        "validity": validity,
        "data_quality": data_quality,
        "verdict": run_verdict,
        "promotion": promo,
        "next_action": action,
    }


def _trades_bytes(execution: Any) -> bytes:
    """The trades table: extended with the cost components when the run carries them."""
    components = getattr(execution, "cost_components", None)
    if components is None:
        return trades_parquet_bytes(execution.trades)
    return trades_parquet_bytes(execution.trades, components=components)


def ledger_summary(
    ledgers: Mapping[str, Any] | None, trades: Sequence, extra_problems: Sequence[str] = ()
) -> dict[str, Any] | None:
    """Counts of the lineage ledgers for report.json, or None when the run has none.

    `excluded_by_reason` sums the exclusion rows' `count` per reason code over every scope.
    `reconcile_problems` is the number of reconciliation problems found in the frames, and
    `problems` names them; both are empty for a run whose ledgers reconcile exactly.
    """
    if ledgers is None:
        return None
    from signal_engine.backtest.evaluation import reconcile_frames

    sig, orders, fills, exc = (
        ledgers["signals"],
        ledgers["orders"],
        ledgers["fills"],
        ledgers["exclusions"],
    )
    by_reason = {str(k): int(v) for k, v in exc.groupby("reason_code")["count"].sum().items()}
    problems = [*reconcile_frames(ledgers, trades), *extra_problems]
    return {
        "signals": int(len(sig)),
        "accepted": int(sig["accepted"].sum()),
        "excluded_by_reason": dict(sorted(by_reason.items())),
        "orders": int(len(orders)),
        "filled": int((orders["status"] == "FILLED").sum()),
        "dropped": int((orders["status"] == "DROPPED").sum()),
        "fills": int(len(fills)),
        "trades": int(len(trades)),
        "reconcile_problems": len(problems),
        "problems": problems,
    }


def _report(
    record: Mapping[str, Any],
    derived: Mapping[str, Any],
    ledgers: Mapping[str, Any] | None = None,
    trades: Sequence = (),
    ledger_problems: Sequence[str] = (),
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "metrics": record["metrics"],
        "series": record.get("series"),
        "evaluation": record.get("evaluation"),
        "validity": derived["validity"],
        "data_quality": derived["data_quality"],
        "verdict": derived["verdict"],
        "promotion": derived["promotion"],
        "next_action": derived["next_action"],
        "n_trials": record.get("n_trials"),
        "notes": record.get("notes", ""),
        "ledgers": ledger_summary(ledgers, trades, ledger_problems),
    }
    report.update({k: record[k] for k in _REPORT_BLOCKS if k in record})
    return report


def _reproducibility(
    frozen: Any, symbols: Sequence[str], engine: Mapping[str, Any]
) -> dict[str, bool]:
    return {
        "strategy_version": frozen.version is not None,
        "strategy_fingerprint": True,
        "dataset_fingerprint": bool(symbols),
        "execution_config": True,
        "deployment_profile": True,
        "engine_version": True,
        "code_commit": engine["code_commit"] != "unknown",
        "resolved_assumptions": True,
    }


def build_manifest(
    *,
    artifacts: dict[str, str],
    run_id: str,
    created: str,
    idea_id: str,
    frozen: Any,
    execution: Any,
    cost_model_json: Any,
    engine: Mapping[str, Any],
    dataset_fp: str,
    parent_run_id: str | None,
    change_classes: list[str],
    readiness_missing: Sequence[str],
) -> dict[str, Any]:
    """Assemble the schema-3 manifest (every key of `manifest.MANIFEST_KEYS`)."""
    symbols = sorted(execution.symbols)
    fingerprints = component_fingerprints(
        intent=frozen.intent,
        resolved=frozen.resolved,
        cost_model=cost_model_json or None,
        symbols=symbols,
        universe_definition=execution.data["universe"],
        profile_content=frozen.profile,
        dataset_fingerprint=dataset_fp,
    )
    fingerprints["profile"] = frozen.profile_sha256
    window = frozen.intent.get("session_window")
    squareoff = frozen.resolved.get("profile.mis_squareoff_min")
    force_flat = None
    if squareoff is not None:
        minutes = int(squareoff["value"])
        force_flat = f"{minutes // 60:02d}:{minutes % 60:02d}"
    calendar = getattr(execution, "calendar", None)
    membership = getattr(execution, "membership", None)
    session = {
        "exchange": None,
        "strategy_window": window,
        "broker_cutoff": None,
        "force_flat": force_flat,
    }
    if calendar is not None:
        product = frozen.resolved["execution.product"]["value"]
        rules = _profile_rules(frozen, product)
        if rules is None and squareoff is not None:
            # fall back to the resolved profile fields rather than nulling a known cutoff
            flat = int(squareoff["value"])
            margin = frozen.resolved.get("profile.squareoff_margin_min")
            rules = SimpleNamespace(
                force_flat_min=flat,
                latest_exit_bar_close_min=flat - int(margin["value"]) if margin else flat,
            )
        session = session_model(
            calendar,
            profile_rules=rules,
            product=product,
            strategy_window=window,
        )
    universe: dict[str, Any] = {
        "definition": execution.data["universe"],
        "symbols_sha256": sha256_hex(canonical_json(symbols)),
        "version": universe_version(membership, getattr(execution, "universe_frame", None)),
    }
    if membership is not None:
        universe["membership_source"] = membership.get("membership_source")
        universe["survivorship_bias"] = membership.get("survivorship_bias")
    has_data_report = getattr(execution, "data_report", None) is not None
    code_dirty = bool(engine["code_dirty"])
    reproducibility = _reproducibility(frozen, symbols, engine)
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "run_id": run_id,
        "created_ist": created,
        "idea_id": idea_id,
        "strategy": {
            "version": frozen.version,
            "parent_version": frozen.parent_version,
            "branch_label": _branch_label(frozen.version),
        },
        "fingerprints": fingerprints,
        "period": {"start": execution.period["start"], "end": execution.period["end"]},
        "dataset": {
            "id": "historify",
            "source": execution.data["source"],
            "interval": execution.data["bar_size"],
            "data_as_of": execution.data["as_of"],
            "symbol_count": len(symbols),
            "fingerprint": dataset_fp,
            "corporate_action_policy": (
                DATA_POLICY["corporate_actions"] if has_data_report else None
            ),
        },
        "universe": universe,
        "calendar_version": None if calendar is None else calendar.version,
        "deployment_profile": {
            "name": frozen.profile_name,
            "product": frozen.resolved["execution.product"]["value"],
            "sha256": frozen.profile_sha256,
        },
        "session": session,
        "engine": {
            "engine_version": ENGINE_VERSION,
            "code_commit": engine["code_commit"],
            "code_dirty": code_dirty,
            "dirty_paths": list(engine["dirty_paths"]),
        },
        "environment": manifest_mod.environment_snapshot(ENVIRONMENT_PACKAGES),
        "random": {"seed": 0, "monte_carlo": None},
        "parent_run_id": parent_run_id,
        "change_classes": change_classes,
        "missing_capabilities": [],
        "reproducible": (not code_dirty) and all(reproducibility.values()),
        "reproducibility": reproducibility,
        "artifacts": artifacts,
    }
    manifest["missing_capabilities"] = [*readiness_missing, *_null_notes(manifest)]
    return manifest


@dataclass(frozen=True)
class PreparedRun:
    """A run folder fully built and validated in memory; nothing is on disk yet."""

    run_id: str
    flat: dict[str, Any]
    manifest_sha256: str
    _files: Mapping[str, bytes]
    _manifest: Mapping[str, Any]

    def commit(self, runs_dir: Path) -> Path:
        """Write the run folder atomically and return its path.

        Raises:
            FileExistsError: The run id already exists.
        """
        folder = RunFolder(runs_dir, self.run_id)
        for rel, content in self._files.items():
            folder.add(rel, content)
        path = folder.commit(lambda artifacts: dict(self._manifest))
        if manifest_mod.manifest_sha256(path) != self.manifest_sha256:
            raise RuntimeError(f"committed manifest of {self.run_id} differs from the prepared one")
        return path


def prepare_run_artifact(
    *,
    runs_dir: Path,
    idea_folder: Path,
    idea_id: str,
    record: Mapping[str, Any],
    frozen: Any,
    execution: Any,
    cost_model_json: Any,
    engine: Mapping[str, Any],
    dataset_fingerprint_fn: Callable[[Sequence[str], str, str], str],
    readiness_missing: Sequence[str],
    parent_run_id: str | None,
    now: datetime,
) -> PreparedRun:
    """Build and validate everything a run folder and its flat record need, writing nothing.

    `frozen` carries the strategy version the manifest records (for a new strategy the dry-run
    version). Anything that can raise does so here, before the caller registers the version or
    touches the disk.

    Raises:
        ValueError: The manifest fails `validate_manifest`, or an artifact holds NaN.
    """
    run_id = record["run_id"]
    created = _created(now)
    code_dirty = bool(engine["code_dirty"])
    reproducible = (not code_dirty) and all(
        _reproducibility(frozen, sorted(execution.symbols), engine).values()
    )
    derived = _derived(
        record,
        readiness_missing,
        code_dirty,
        reproducible,
        getattr(execution, "data_quality", None),
    )
    ledgers = getattr(execution, "ledgers", None)
    files: dict[str, bytes] = {
        "assumptions.json": _encode(_assumptions(frozen, idea_folder, created)),
        "strategy/spec.yaml": _encode(yaml.safe_dump(frozen.intent, sort_keys=True)),
        "strategy/strategy.json": _encode(_strategy_json(idea_id, frozen)),
        "execution/config.json": _encode({"resolved": frozen.resolved, "profile": frozen.profile}),
        "evidence/trades.parquet": _encode(_trades_bytes(execution)),
        "report/report.json": _encode(
            _report(
                record,
                derived,
                ledgers,
                execution.trades,
                getattr(execution, "ledger_problems", ()),
            )
        ),
    }
    # The four ledgers are optional: a run without them stays valid.
    for name in sorted(ledgers or {}):
        files[f"evidence/{name}.parquet"] = _encode(
            frame_parquet_bytes(ledgers[name], LEDGER_SCHEMAS[name])
        )
    data_report = getattr(execution, "data_report", None)
    if data_report is not None:
        files["data_quality.json"] = _encode(_json_safe(data_report))
    universe_frame = getattr(execution, "universe_frame", None)
    if universe_frame is not None:
        files["execution/universe.parquet"] = _encode(frame_parquet_bytes(universe_frame))
    artifacts = {rel: hashlib.sha256(files[rel]).hexdigest() for rel in sorted(files)}
    dataset_fp = _symbols_fingerprint(
        execution.symbols,
        execution.data["bar_size"],
        execution.data["as_of"],
        dataset_fingerprint_fn,
    )
    reference = reference_manifest(
        runs_dir,
        parent_run_id=parent_run_id,
        version=frozen.version,
        parent_version=frozen.parent_version,
    )
    manifest = build_manifest(
        artifacts=artifacts,
        run_id=run_id,
        created=created,
        idea_id=idea_id,
        frozen=frozen,
        execution=execution,
        cost_model_json=cost_model_json,
        engine=engine,
        dataset_fp=dataset_fp,
        parent_run_id=parent_run_id,
        change_classes=[],
        readiness_missing=readiness_missing,
    )
    manifest["change_classes"] = (
        first_run_classes() if reference is None else classify(reference, manifest)
    )
    problems = manifest_mod.validate_manifest(manifest)
    if problems:
        raise ValueError("invalid run manifest: " + "; ".join(problems))
    manifest_sha = hashlib.sha256(_encode(manifest)).hexdigest()
    flat = {
        **record,
        "schema": SCHEMA,
        "artifact_dir": f"{run_id}/",
        "manifest_sha256": manifest_sha,
        "strategy_version": frozen.version,
        "strategy_fingerprint": frozen.strategy_fingerprint,
        "change_classes": manifest["change_classes"],
        "reproducible": manifest["reproducible"],
        "code_dirty": manifest["engine"]["code_dirty"],
        "promotion_eligible": manifest["reproducible"] and not manifest["engine"]["code_dirty"],
        "missing_capabilities": manifest["missing_capabilities"],
        "run_verdict": derived["verdict"]["run_verdict"],
        "promotion_decision": derived["promotion"]["stages"]["backtest"]["decision"],
        "next_action": derived["next_action"],
    }
    return PreparedRun(run_id, flat, manifest_sha, files, manifest)
