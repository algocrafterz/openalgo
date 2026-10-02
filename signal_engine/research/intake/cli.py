"""Intake command line: check, resolve, confirm, ask, clear-questions, block, unblock, set-current.

    uv run python -m signal_engine.research.intake check IDEA-012
    uv run python -m signal_engine.research.intake confirm IDEA-012 A-004 A-005

`check` never writes. Exit codes: 0 success (for `check`, Backtest Ready), 1 not ready or
spec problem, 2 bad input (unknown idea, id or version).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from signal_engine.backtest.record_run import idea_folders
from signal_engine.research.intake import execution, ledger, state, versions
from signal_engine.research.intake.profiles import ProfileError
from signal_engine.research.intake.spec import SpecError, load_spec

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
    return parser


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
    folders = idea_folders(root if root is not None else DEFAULT_ROOT)
    if args.idea not in folders:
        _out(f"unknown idea {args.idea}")
        return 2
    folder = folders[args.idea]
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
