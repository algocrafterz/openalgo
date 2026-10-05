"""Intake command line: check, resolve, confirm, ask, clear-questions, block, unblock,
set-current, trials, and the idea lifecycle commands stage, state and classify, plus the one-off
migrate-classification.

    uv run python -m signal_engine.research.intake check IDEA-012
    uv run python -m signal_engine.research.intake confirm IDEA-012 A-004 A-005
    uv run python -m signal_engine.research.intake stage IDEA-012 paper --version v2
    uv run python -m signal_engine.research.intake state IDEA-012 shelved --revisit-when "..."
    uv run python -m signal_engine.research.intake classify IDEA-012 --category Breakout --tags a,b
    uv run python -m signal_engine.research.intake migrate-classification [--dry-run]

`check` never writes. Exit codes: 0 success (for `check`, Backtest Ready), 1 refused (not
ready, spec problem, or a lifecycle rule), 2 bad input (unknown idea, id, version, stage,
state or category, or a missing required argument).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import yaml

from signal_engine.backtest.record_run import idea_folders
from signal_engine.research import build_registry
from signal_engine.research.intake import (
    decisions,
    execution,
    frontmatter,
    ledger,
    lifecycle,
    state,
    taxonomy,
    trials,
    versions,
)
from signal_engine.research.intake.profiles import ProfileError
from signal_engine.research.intake.spec import SpecError, load_spec
from signal_engine.timeutils import IST

DEFAULT_ROOT = Path(__file__).resolve().parents[2]


def _out(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def _working_ledger(folder: Path, data):
    return ledger.resolve(
        data,
        execution.load_overrides(folder),
        existing=ledger.load(folder),
        floor_next_id=ledger.sealed_next_id(folder),
    )


def _report(idea: str, data, led: ledger.Ledger) -> state.StateResult:
    result = state.intake_state(data, led)
    _out(f"{idea}: {result.state}")
    for reason in result.reasons:
        _out(f"  - {reason}")
    for question in led.open_questions:
        _out(f"  ? {question}")
    if result.unconfirmed_medium:
        _out(f"  {len(result.unconfirmed_medium)} medium assumptions recorded, not confirmed")
    return result


def _cmd_check(idea, folder, data, args) -> int:
    result = _report(idea, data, _working_ledger(folder, data))
    return 0 if result.state == state.BACKTEST_READY else 1


def _cmd_resolve(idea, folder, data, args) -> int:
    led = _working_ledger(folder, data)
    ledger.save(folder, led)
    _report(idea, data, led)
    return 0


def _cmd_confirm(idea, folder, data, args) -> int:
    led = ledger.confirm(_working_ledger(folder, data), args.ids)
    ledger.save(folder, led)
    _report(idea, data, led)
    return 0


def _cmd_ask(idea, folder, data, args) -> int:
    led = _working_ledger(folder, data)
    led = replace(led, open_questions=(*led.open_questions, args.question))
    ledger.save(folder, led)
    _report(idea, data, led)
    return 0


def _cmd_clear(idea, folder, data, args) -> int:
    led = replace(_working_ledger(folder, data), open_questions=())
    ledger.save(folder, led)
    _report(idea, data, led)
    return 0


def _cmd_block(idea, folder, data, args) -> int:
    led = replace(_working_ledger(folder, data), blocked_reason=args.reason)
    ledger.save(folder, led)
    _report(idea, data, led)
    return 0


def _cmd_unblock(idea, folder, data, args) -> int:
    led = replace(_working_ledger(folder, data), blocked_reason=None)
    ledger.save(folder, led)
    _report(idea, data, led)
    return 0


def _cmd_set_current(idea, folder, data, args) -> int:
    versions.set_pointer(folder, "current_version", args.version)
    _out(f"{idea}: current_version is now {args.version}")
    return 0


STAGE_LOG = "stage-log.yaml"


class _Refused(Exception):
    """A lifecycle rule refuses the command (exit 1)."""


class _BadInput(Exception):
    """The command line is wrong (exit 2)."""


def _append_stage_log(folder: Path, entry: dict) -> None:
    """Append one entry to stage-log.yaml as text; earlier text is never rewritten."""
    path = folder / STAGE_LOG
    existing = path.read_bytes().decode("utf-8") if path.exists() else ""
    block = yaml.safe_dump([entry], sort_keys=False, allow_unicode=True, width=10**9)
    prefix = "\n" if existing and not existing.endswith("\n") else ""
    with path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(prefix + block)


def _need_reason(reason: str | None, what: str) -> str:
    if reason is None or not reason.strip():
        raise _BadInput(f"{what} needs --reason")
    return reason.strip()


def _promotion(idea: str, folder: Path, args) -> tuple[dict, str]:
    """Check backtest -> paper. Returns the keys to set and the promoted run id."""
    if not args.version:
        raise _BadInput("moving to paper needs --version (a registered strategy version)")
    if args.version not in {v.version for v in versions.load_versions(folder)}:
        raise _BadInput(f"{args.version} is not a registered version of {idea}")
    runs, problems = build_registry.load_runs(folder)
    if problems:
        raise _Refused("run records are invalid:\n" + "\n".join(f"  - {p}" for p in problems))
    if args.run and not any(r["run_id"] == args.run for r in runs):
        raise _BadInput(f"{args.run} is not a run of {idea}")
    run, reasons = lifecycle.find_promotable_run(runs, args.version, args.run)
    if run is None:
        raise _Refused("no run qualifies to promote:\n" + "\n".join(f"  - {r}" for r in reasons))
    keys = {
        "stage": "paper",
        "promoted_version": args.version,
        "promoted_run_id": run["run_id"],
    }
    return keys, run["run_id"]


def _recheck_promotion(idea: str, folder: Path, data: dict) -> str:
    """A return to paper needs the same evidence as a first promotion: the stored promoted
    version and run must still qualify. Returns the run id."""
    version, run_id = data.get("promoted_version"), data.get("promoted_run_id")
    if not version or not run_id:
        raise _Refused(
            f"{idea} has no promoted_version and promoted_run_id on record; "
            "move it to backtest and promote a qualifying run instead"
        )
    runs, problems = build_registry.load_runs(folder)
    if problems:
        raise _Refused("run records are invalid:\n" + "\n".join(f"  - {p}" for p in problems))
    run, reasons = lifecycle.find_promotable_run(runs, str(version), str(run_id))
    if run is None:
        raise _Refused(
            "no run qualifies to return to paper:\n" + "\n".join(f"  - {r}" for r in reasons)
        )
    return run["run_id"]


def _check_move(idea: str, folder: Path, current: str, target: str, data: dict, args) -> None:
    order = lifecycle.STAGES
    if current == target:
        raise _Refused(f"{idea} is already at stage {target}")
    if order.index(target) < order.index(current):
        _need_reason(args.reason, "moving to an earlier stage")
        if target == "live":
            raise _Refused(lifecycle.LIVE_BLOCKED)
        return
    state_now = data.get("research_state", lifecycle.DEFAULT_RESEARCH_STATE)
    if state_now in lifecycle.BLOCKING_STATES:
        raise _Refused(f"{idea} is {state_now}: change research_state before moving forward")
    if order.index(target) != order.index(current) + 1:
        raise _Refused(
            f"stages move one step at a time: {current} is followed by "
            f"{order[order.index(current) + 1]}"
        )
    if target == "live":
        raise _Refused(lifecycle.LIVE_BLOCKED)
    if target == "retired":
        _need_reason(args.reason, "retiring a live idea")
    if target == "backtest" and load_spec(folder) is None:
        raise _Refused(f"{idea}: spec.yaml is required before backtest (copy _template/spec.yaml)")


def _cmd_stage(idea: str, folder: Path, args) -> int:
    data = frontmatter.read(folder)
    current = data.get("stage")
    if current not in lifecycle.STAGES:
        raise _BadInput(f"{idea} has no valid stage in its frontmatter")
    target = args.stage
    _check_move(idea, folder, current, target, data, args)
    updates: dict = {"stage": target}
    run_id = None
    if target == "paper" and current == "backtest":
        updates, run_id = _promotion(idea, folder, args)
    if target == "paper" and current in ("live", "retired"):
        run_id = _recheck_promotion(idea, folder, data)
    reason = args.reason.strip() if args.reason and args.reason.strip() else None
    # The log is appended first: a crash between the two writes then leaves a log entry with no
    # stage change (visible and harmless), never a stage change with no log entry.
    _append_stage_log(
        folder,
        {
            "date_ist": datetime.now(IST).isoformat(timespec="seconds"),
            "from": current,
            "to": target,
            "reason": reason,
            "run_id": run_id,
        },
    )
    try:
        frontmatter.set_keys(folder, updates)
    except (frontmatter.FrontmatterError, OSError) as exc:
        raise _BadInput(
            f"stage-log.yaml already records {current} -> {target} but idea.md could not be "
            f"updated ({exc}); the stage is unchanged, so remove that last log entry or retry"
        ) from exc
    _out(f"{idea}: stage {current} -> {target}")
    return 0


def _cmd_state(idea: str, folder: Path, args) -> int:
    if args.research_state == "shelved" and not (args.revisit_when and args.revisit_when.strip()):
        raise _BadInput("shelved needs --revisit-when")
    basis = (args.rejection_basis or "").strip() or None
    if args.research_state == "rejected":
        if basis is None:
            raise _BadInput(
                f"rejected needs --rejection-basis, one of {', '.join(build_registry.REJECTION_BASES)}"
            )
        if basis not in build_registry.REJECTION_BASES:
            raise _BadInput(
                f"rejection basis '{basis}' is not one of {', '.join(build_registry.REJECTION_BASES)}"
            )
    elif basis is not None:
        raise _BadInput("--rejection-basis only applies to the rejected state")
    reason = (args.block_reason or "").strip() or None
    if args.research_state == "blocked":
        if reason not in build_registry.BLOCK_REASONS:
            raise _BadInput(
                f"blocked needs --block-reason, one of {', '.join(build_registry.BLOCK_REASONS)}"
            )
    elif reason is not None:
        raise _BadInput("--block-reason only applies to the blocked state")
    updates: dict = {"research_state": args.research_state}
    if args.revisit_when is not None and args.revisit_when.strip():
        updates["revisit_when"] = args.revisit_when.strip()
    if basis is not None:
        updates["rejection_basis"] = basis
    if reason is not None:
        updates["block_reason"] = reason
    frontmatter.set_keys(folder, updates)
    if args.research_state != "rejected":
        frontmatter.remove_keys(folder, ["rejection_basis"])
    if args.research_state != "blocked":
        frontmatter.remove_keys(folder, ["block_reason"])
    _out(f"{idea}: research_state is now {args.research_state}")
    return 0


def _cmd_classify(idea: str, folder: Path, args, root: Path) -> int:
    if args.category is None and args.tags is None:
        raise _BadInput("classify needs --category and/or --tags")
    updates: dict = {}
    if args.category is not None:
        categories = taxonomy.load_categories(taxonomy.taxonomy_path(root))
        if args.category not in categories:
            raise _BadInput(
                f"category '{args.category}' is not in the taxonomy: {', '.join(categories)}"
            )
        updates["strategy_category"] = args.category
    if args.tags is not None:
        updates["strategy_tags"] = taxonomy.parse_tags(args.tags)
    frontmatter.set_keys(folder, updates)
    _out(f"{idea}: classification updated")
    return 0


def _migrate_one(folder: Path, categories: tuple[str, ...]) -> tuple[dict, list[str]]:
    """The frontmatter updates migrating one idea needs, and the notes to report."""
    fm = frontmatter.read(folder)
    updates: dict = {}
    notes: list[str] = []
    if "category" in fm:
        legacy = fm["category"]
        target = taxonomy.LEGACY_CATEGORY_MAP.get(legacy)
        if target is None:
            notes.append(f"unknown legacy category '{legacy}', using Other")
            target = taxonomy.DEFAULT_CATEGORY
        if target not in categories:
            notes.append(f"'{target}' is not in the taxonomy, using Other")
            target = taxonomy.DEFAULT_CATEGORY
        existing = fm.get("strategy_category")
        if existing is not None and existing != target:
            notes.append(f"kept existing strategy_category '{existing}'")
        elif existing is None:
            updates["strategy_category"] = target
    if "tags" in fm:
        legacy_tags = fm["tags"] if isinstance(fm["tags"], list) else [fm["tags"]]
        current = fm.get("strategy_tags") or []
        merged = taxonomy.normalise_tags(
            [*current, *(str(t) for t in legacy_tags if t is not None)]
        )
        if merged != current:
            updates["strategy_tags"] = merged
    return updates, notes


def _cmd_migrate_classification(root: Path, dry_run: bool) -> int:
    categories = taxonomy.load_categories(taxonomy.taxonomy_path(root))
    changed = failed = 0
    for idea, folder in idea_folders(root).items():
        try:
            fm = frontmatter.read(folder)
            legacy = [k for k in ("category", "tags") if k in fm]
            if not legacy:
                _out(f"{idea}: nothing to migrate")
                continue
            updates, notes = _migrate_one(folder, categories)
            if not dry_run:
                if updates:
                    frontmatter.set_keys(folder, updates)
                frontmatter.remove_keys(folder, legacy)
        except (frontmatter.FrontmatterError, taxonomy.TaxonomyError, OSError) as exc:
            failed += 1
            _out(f"{idea}: error: {exc}")
            continue
        changed += 1
        detail = ", ".join(f"{k}={v}" for k, v in updates.items()) or "legacy keys dropped"
        suffix = f" ({'; '.join(notes)})" if notes else ""
        verb = "would migrate" if dry_run else "migrated"
        _out(f"{idea}: {verb}: {detail}{suffix}")
    label = "would change" if dry_run else "changed"
    _out(f"summary: {changed} {label}, {failed} unreadable")
    return 1 if failed else 0


def _lifecycle_command(args, folder: Path, root: Path) -> int:
    try:
        if args.command == "stage":
            return _cmd_stage(args.idea, folder, args)
        if args.command == "state":
            return _cmd_state(args.idea, folder, args)
        return _cmd_classify(args.idea, folder, args, root)
    except _Refused as exc:
        _out(f"refused: {exc}")
        return 1
    except (_BadInput, taxonomy.TaxonomyError, frontmatter.FrontmatterError) as exc:
        _out(f"error: {exc}")
        return 2
    except (versions.VersionError, SpecError) as exc:
        _out(f"error: {exc}")
        return 2


def _trial_line(t: trials.Trial) -> str:
    if t.kind == trials.MATERIAL:
        return f"  {t.trial}  material  run {t.run_id}  {t.created_ist}"
    return f"  {t.trial}  external  x{t.count}  {t.reason}  {t.created_ist}"


def _summary(idea: str, c: trials.TrialCount) -> str:
    return f"{idea}: trials: material {c.material}, external {c.external}, total {c.total}"


def _cmd_decide(idea: str, folder: Path, args) -> int:
    try:
        entry = decisions.record(folder, args.run, args.decision, args.note or "")
    except decisions.DecisionError as exc:
        _out(f"refused: {exc}")
        return 1
    _out(f"{idea}: {entry['run_id']} marked {entry['decision']}")
    return 0


def _cmd_trials(idea: str, folder: Path, args) -> int:
    try:
        if args.action == "show":
            entries = trials.load_trials(folder)
            _out(_summary(idea, trials.count(folder)))
            for entry in entries:
                _out(_trial_line(entry))
            return 0
        if not (args.reason and args.reason.strip()):
            _out("error: trials add needs --reason")
            return 2
        if not (args.n and args.n.isascii() and args.n.isdigit() and int(args.n) >= 1):
            _out(f"error: N must be a positive integer, got {args.n!r}")
            return 2
        result = trials.add_external(folder, int(args.n), args.reason)
        _out(_summary(idea, result.count))
        return 0
    except trials.TrialError as exc:
        _out(f"refused: {exc}")
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m signal_engine.research.intake")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, helptext in (
        ("check", "print the intake state; writes nothing"),
        ("resolve", "write or refresh assumptions.yaml"),
        ("clear-questions", "clear the open questions"),
        ("unblock", "clear the blocked reason"),
    ):
        sub.add_parser(name, help=helptext).add_argument("idea")
    confirm = sub.add_parser("confirm", help="confirm assumptions by id")
    confirm.add_argument("idea")
    confirm.add_argument("ids", nargs="+")
    ask = sub.add_parser("ask", help="record an open question")
    ask.add_argument("idea")
    ask.add_argument("question")
    block = sub.add_parser("block", help="mark the idea Backtest Blocked")
    block.add_argument("idea")
    block.add_argument("reason")
    current = sub.add_parser("set-current", help="set the idea's current_version")
    current.add_argument("idea")
    current.add_argument("version")
    stage = sub.add_parser("stage", help="move the idea to another stage")
    stage.add_argument("idea")
    stage.add_argument("stage", choices=lifecycle.STAGES)
    stage.add_argument("--version", help="registered strategy version to promote (paper)")
    stage.add_argument("--run", help="run id to promote (default: the latest qualifying run)")
    stage.add_argument("--reason", help="why (required for retiring and for earlier stages)")
    research = sub.add_parser("state", help="set the idea's research_state")
    research.add_argument("idea")
    research.add_argument("research_state", choices=lifecycle.RESEARCH_STATES)
    research.add_argument("--revisit-when", dest="revisit_when")
    research.add_argument("--rejection-basis", dest="rejection_basis")
    research.add_argument("--block-reason", dest="block_reason")
    classify = sub.add_parser("classify", help="set strategy_category and strategy_tags")
    classify.add_argument("idea")
    classify.add_argument("--category")
    classify.add_argument("--tags", help="comma-separated; replaces the idea's tags")
    trial_cmd = sub.add_parser("trials", help="show or extend the idea's trial ledger")
    trial_cmd.add_argument("action", choices=("show", "add"))
    trial_cmd.add_argument("idea")
    trial_cmd.add_argument("n", nargs="?", help="external trial count (add)")
    trial_cmd.add_argument("--reason", help="why the variants were searched outside (add)")
    decide = sub.add_parser(
        "decide", help="record keep or reject for a run (never affects the verdict)"
    )
    decide.add_argument("idea")
    decide.add_argument("run")
    decide.add_argument("decision", choices=decisions.DECISIONS)
    decide.add_argument("--note", help="why")
    migrate = sub.add_parser(
        "migrate-classification",
        help="move legacy category/tags to strategy_category/strategy_tags",
    )
    migrate.add_argument("--dry-run", action="store_true", help="print the changes, write nothing")
    return parser


_LIFECYCLE = ("stage", "state", "classify")
_COMMANDS = {
    "check": _cmd_check,
    "resolve": _cmd_resolve,
    "confirm": _cmd_confirm,
    "ask": _cmd_ask,
    "clear-questions": _cmd_clear,
    "block": _cmd_block,
    "unblock": _cmd_unblock,
    "set-current": _cmd_set_current,
}


def main(argv: list[str], root: Path | None = None) -> int:
    """Run one intake command. Returns the exit code."""
    args = _parser().parse_args(argv)
    if args.command == "migrate-classification":
        try:
            return _cmd_migrate_classification(
                root if root is not None else DEFAULT_ROOT, args.dry_run
            )
        except taxonomy.TaxonomyError as exc:
            _out(f"error: {exc}")
            return 2
    folders = idea_folders(root if root is not None else DEFAULT_ROOT)
    if args.idea not in folders:
        _out(f"unknown idea {args.idea}")
        return 2
    folder = folders[args.idea]
    if args.command == "trials":
        return _cmd_trials(args.idea, folder, args)
    if args.command == "decide":
        return _cmd_decide(args.idea, folder, args)
    if args.command in _LIFECYCLE:
        return _lifecycle_command(args, folder, root if root is not None else DEFAULT_ROOT)
    try:
        data = load_spec(folder)
        if data is None:
            _out(f"{args.idea}: spec.yaml is required (copy research/ideas/_template/spec.yaml)")
            return 1
        return _COMMANDS[args.command](args.idea, folder, data, args)
    except SpecError as exc:
        _out(str(exc))
        return 1
    except (
        ledger.LedgerError,
        versions.VersionError,
        execution.ExecutionError,
        ProfileError,
    ) as exc:
        _out(f"error: {exc}")
        return 2
