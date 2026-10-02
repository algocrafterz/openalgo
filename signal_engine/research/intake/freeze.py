"""Freeze: the gate between intake and a run, and the source of a strategy's identity.

Freezing refuses unless the idea is Backtest Ready. It returns the effective intent, the
resolved configuration (built only from ledger entries, each leaf carrying its ledger id),
the profile, the sealed ledger, the strategy fingerprints and the strategy version. Freeze
registers the version in `versions.yaml`: a known fingerprint reuses its version, an unknown
one appends the next. Nothing is written when freeze refuses.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from signal_engine.research.intake import (
    execution,
    fingerprints,
    state,
    versions,
)
from signal_engine.research.intake import (
    ledger as ledger_mod,
)
from signal_engine.research.intake.profiles import load_profile
from signal_engine.research.intake.spec import SpecError, load_spec, validate_spec


class FreezeError(Exception):
    """The idea is not ready to freeze, or the resolved configuration is not fully traced."""


@dataclass(frozen=True)
class FrozenSpec:
    """Everything a run needs to know about the strategy it is running."""

    intent: dict[str, Any]
    resolved: dict[str, dict[str, Any]]
    profile: dict[str, Any]
    profile_name: str
    profile_sha256: str
    ledger: tuple[ledger_mod.Assumption, ...]
    unconfirmed_medium: tuple[ledger_mod.Assumption, ...]
    rules_fingerprint: str
    params_fingerprint: str
    strategy_fingerprint: str
    version: str | None
    parent_version: str | None
    version_created: bool


def build_resolved(
    ledger: ledger_mod.Ledger, field_names: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """The resolved configuration as `{field: {value, ledger_id}}`, read only from the ledger.

    Raises FreezeError naming any field that has no ledger entry, so no value can enter a run
    without one.
    """
    resolved: dict[str, dict[str, Any]] = {}
    missing = []
    for name in field_names:
        if not ledger.has(name):
            missing.append(name)
            continue
        entry = ledger.get(name)
        resolved[name] = {"value": copy.deepcopy(entry.value), "ledger_id": entry.id}
    if missing:
        raise FreezeError(f"no ledger entry for: {', '.join(sorted(missing))}")
    return resolved


def effective_intent(spec: Mapping[str, Any], ledger: ledger_mod.Ledger) -> dict[str, Any]:
    """The spec with every value the platform inferred written in, so the fingerprint covers
    the strategy as it will actually be simulated."""
    intent = copy.deepcopy(dict(spec))
    if ledger.has("execution.product"):
        intent["product"] = ledger.get("execution.product").value
    timing = dict(intent.get("timing") or {})
    for key in ("signal_evaluation", "order_submission", "fill_model"):
        if ledger.has(f"timing.{key}"):
            timing[key] = ledger.get(f"timing.{key}").value
    if timing:
        intent["timing"] = timing
    portfolio = intent.get("portfolio")
    if isinstance(portfolio, dict) and ledger.has("portfolio.scope"):
        intent["portfolio"] = {**portfolio, "scope": ledger.get("portfolio.scope").value}
    re_entry = intent.get("re_entry")
    if isinstance(re_entry, dict) and ledger.has("re_entry.trigger"):
        intent["re_entry"] = {**re_entry, "trigger": ledger.get("re_entry.trigger").value}
    return intent


def _refuse(idea: str, result: state.StateResult) -> None:
    if result.state == state.BACKTEST_BLOCKED:
        raise FreezeError(f"{idea}: BACKTEST_BLOCKED, {result.reasons[0]}")
    detail = "\n".join(f"- {r}" for r in result.reasons)
    if result.state in (state.DRAFT, state.NEEDS_CLARIFICATION):
        raise SpecError(f"{idea}: spec.yaml is incomplete ({result.state}):\n{detail}")
    raise FreezeError(
        f"{idea}: ASSUMPTION_REVIEW, confirm these before freezing "
        f"(python -m signal_engine.research.intake confirm):\n{detail}"
    )


def register(
    folder: Path,
    frozen: FrozenSpec,
    *,
    parent: str | None = None,
    label: str | None = None,
    note: str = "",
    now: datetime | None = None,
    dry_run: bool = False,
) -> FrozenSpec:
    """Resolve the frozen strategy's version, appending to versions.yaml unless `dry_run`.

    `freeze(register_version=False)` followed by `register(...)` lets a caller register the
    version only once its run has succeeded; `dry_run=True` checks the parent and label early
    without writing. Raises VersionError for an unacceptable parent or label.
    """
    versions.require_aware(now)
    found = versions.resolve_version(
        folder,
        frozen.strategy_fingerprint,
        default_parent=versions.read_pointers(folder)["current_version"],
        parent=parent,
        label=label,
        note=note,
        now=now,
        dry_run=dry_run,
    )
    return replace(
        frozen, version=found.version, parent_version=found.parent, version_created=found.created
    )


def freeze(
    folder: Path,
    *,
    idea_id: str | None = None,
    profiles_dir: Path | None = None,
    register_version: bool = True,
    parent_version: str | None = None,
    version_label: str | None = None,
    note: str = "",
    now: datetime | None = None,
) -> FrozenSpec:
    """Freeze an idea. Raises SpecError for a missing or incomplete spec and FreezeError for a
    blocked idea or unconfirmed High-materiality assumptions."""
    versions.require_aware(now)
    idea = idea_id or folder.name
    data = load_spec(folder)
    if data is None:
        raise SpecError(
            f"{idea}: spec.yaml is required before this idea is backtested "
            "(copy research/ideas/_template/spec.yaml and complete every field)"
        )
    overrides = execution.load_overrides(folder)
    led = ledger_mod.resolve(
        data,
        overrides,
        existing=ledger_mod.load(folder),
        profiles_dir=profiles_dir,
        floor_next_id=ledger_mod.sealed_next_id(folder),
    )
    result = state.intake_state(data, led, profiles_dir)
    if result.state != state.BACKTEST_READY:
        _refuse(idea, result)
    profile = load_profile(data["profile"], profiles_dir)
    config = execution.resolve_execution(data, profile, overrides).as_dict()
    resolved = build_resolved(led, config)
    stray = {e.field for e in led.entries if e.section in ("execution", "profile")} - set(resolved)
    if stray:
        raise FreezeError(f"ledger entries not in the resolved configuration: {sorted(stray)}")
    intent = effective_intent(data, led)
    rules = fingerprints.rules_fingerprint(intent)
    params = fingerprints.params_fingerprint(intent.get("parameters"))
    strategy = fingerprints.strategy_fingerprint(rules, params)
    frozen = FrozenSpec(
        intent=intent,
        resolved=resolved,
        profile=profile.as_dict(),
        profile_name=profile.name,
        profile_sha256=profile.sha256,
        ledger=led.entries,
        unconfirmed_medium=result.unconfirmed_medium,
        rules_fingerprint=rules,
        params_fingerprint=params,
        strategy_fingerprint=strategy,
        version=None,
        parent_version=None,
        version_created=False,
    )
    if not register_version:
        return frozen
    return register(folder, frozen, parent=parent_version, label=version_label, note=note, now=now)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FreezeError(f"{path.name} unreadable in the run folder: {exc}") from exc


def from_run_folder(run_folder: Path) -> FrozenSpec:
    """Rebuild the FrozenSpec a run was made from, using only that run's folder.

    The idea's current spec.yaml is never read and nothing is written. The rules, params and
    strategy fingerprints are recomputed from the stored intent and must equal the manifest's,
    so a tampered or non-canonical spec is refused. Raises FreezeError when the artifacts do not
    verify, a file is unreadable or the fingerprints differ.
    """
    from signal_engine.backtest import manifest as manifest_mod

    folder = Path(run_folder)
    problems = manifest_mod.verify_artifacts(folder)
    if problems:
        raise FreezeError(f"run folder {folder.name} does not verify: {'; '.join(problems)}")
    manifest = _read_json(folder / "manifest.json")
    config = _read_json(folder / "execution" / "config.json")
    assumptions = _read_json(folder / "assumptions.json")
    strategy = _read_json(folder / "strategy" / "strategy.json")
    try:
        intent = yaml.safe_load((folder / "strategy" / "spec.yaml").read_text(encoding="utf-8"))
        entries = tuple(ledger_mod.Assumption(**raw) for raw in assumptions["entries"])
        stored = manifest["fingerprints"]
        profile_meta = manifest["deployment_profile"]
        resolved, profile = config["resolved"], config["profile"]
        version, parent_version = strategy["version"], strategy["parent_version"]
        profile_name, profile_sha = profile_meta["name"], profile_meta["sha256"]
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
        raise FreezeError(f"run folder {folder.name} is malformed: {exc}") from exc
    if not isinstance(intent, dict):
        raise FreezeError(f"run folder {folder.name}: strategy/spec.yaml is not a mapping")
    rules = fingerprints.rules_fingerprint(intent)
    params = fingerprints.params_fingerprint(intent.get("parameters"))
    strategy_fp = fingerprints.strategy_fingerprint(rules, params)
    for name, got in (("rules", rules), ("params", params), ("strategy", strategy_fp)):
        if stored.get(name) != got:
            raise FreezeError(
                f"run folder {folder.name}: {name} fingerprint of strategy/spec.yaml does not "
                "match the manifest (tampered or non-canonical spec)"
            )
    return FrozenSpec(
        intent=intent,
        resolved=resolved,
        profile=profile,
        profile_name=profile_name,
        profile_sha256=profile_sha,
        ledger=entries,
        unconfirmed_medium=tuple(
            e
            for e in entries
            if e.materiality == "medium" and e.source != "user" and not e.user_confirmed
        ),
        rules_fingerprint=rules,
        params_fingerprint=params,
        strategy_fingerprint=strategy_fp,
        version=version,
        parent_version=parent_version,
        version_created=False,
    )
