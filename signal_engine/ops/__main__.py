"""Ops phase steps. Each command owns one duty and writes one output (reports/<step>-<day>.md/.json).

    uv run python -m signal_engine.ops open-day [--day D]       readiness before the open
    uv run python -m signal_engine.ops liveness [--day D]       the first minutes after the open
    uv run python -m signal_engine.ops logs [--day D]           error signatures vs the last 5 sessions
    uv run python -m signal_engine.ops jobs [--day D]           scheduled-job audit
    uv run python -m signal_engine.ops value-areas [--day D]    store the day's VAH/POC/VAL, check coverage
    uv run python -m signal_engine.ops track [--day D]          record the day's FAIL checks in the bug tracker
    uv run python -m signal_engine.ops retry-failed             overnight retry of yesterday's failed backfill
    uv run python -m signal_engine.ops deadman PHASE            alert when a checkpoint is missing, incomplete or failed
    uv run python -m signal_engine.ops run-action ACTION --day D --id RUN   run a step again (the page)
    uv run python -m signal_engine.ops notify PHASE [--day D]   the one Telegram message for a phase

Exit status: 0 GREEN or AMBER, 1 RED, so a phase status file shows a failed check as a failed step.
"""

import argparse
import sys
from datetime import datetime

from signal_engine.ops import (
    actions,
    alerts,
    automation,
    autotrack,
    jobs,
    logscan,
    readiness,
    value_areas,
    verdict,
)
from signal_engine.ops.model import read_phase
from signal_engine.timeutils import IST


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="signal_engine.ops")
    parser.add_argument(
        "command",
        choices=(
            "open-day",
            "liveness",
            "logs",
            "jobs",
            "value-areas",
            "notify",
            "track",
            "run-action",
            "retry-failed",
            "deadman",
        ),
    )
    parser.add_argument("phase", nargs="?", help="for notify: open-day, liveness or close-day")
    parser.add_argument("--day", default=datetime.now(IST).strftime("%Y-%m-%d"))
    parser.add_argument("--at", help="liveness only: end of the window, HH:MM (default: now)")
    parser.add_argument("--id", help="run-action only: the run id the page is watching")
    parser.add_argument("--dry-run", action="store_true", help="notify only: print, do not send")
    args = parser.parse_args(argv)

    if args.command == "notify":
        if args.phase not in ("open-day", "liveness", "close-day"):
            parser.error("notify needs a phase: open-day, liveness or close-day")
        text = verdict.message(args.phase, args.day)
        if text is None:
            print(f"{args.phase} {args.day}: nothing to send")
            if not args.dry_run and verdict.recover(args.phase, args.day):
                print(f"{args.phase} {args.day}: recovery line sent")
            return 0
        if args.dry_run:
            print(f"[dry-run] would send: {text}")
            return 0
        sent = verdict.send(args.phase, args.day)
        print(f"{'sent' if sent else 'NOT sent'}: {text}")
        return 0 if sent else 1

    if args.command == "retry-failed":
        for line in automation.retry_failed():
            print(line)
        return 0

    if args.command == "deadman":
        if args.phase not in automation.EXPECTED:
            parser.error("deadman needs a phase: morning or close")
        report = automation.deadman_report(args.phase, args.day)
        text = automation.deadman(args.phase, args.day)
        state = automation.worst_state(report)
        if text is None:
            if not report:
                # A holiday, a weekend or a phase not yet due: nothing was checked, so nothing is
                # healthy and no earlier RED is cleared.
                print(f"deadman {args.phase} {args.day}: nothing due, nothing checked")
                return 0
            print(f"deadman {args.phase} {args.day}: healthy, every due checkpoint is complete")
            if not args.dry_run and all(r["state"] == automation.HEALTHY for r in report):
                alerts.recover(kind="deadman", phase=args.phase)
            return 0
        if args.dry_run:
            print(f"[dry-run] would send ({state}): {text}")
            return 0
        sent = alerts.deliver(
            text, kind="deadman", phase=args.phase, condition=state, severity="RED"
        )
        print(f"{'sent' if sent else 'NOT sent'} ({state}): {text}")
        return 0 if sent else 1

    if args.command == "run-action":
        if args.phase not in actions.ACTIONS or not actions.RUN_ID.match(args.id or ""):
            parser.error("run-action needs a known action and --id")
        return actions.run(args.phase, args.day, args.id)

    if args.command == "track":
        lines = autotrack.record_failures(args.day)
        print(f"track {args.day}: {len(lines)} failed check(s)")
        for line in lines:
            print(f"  {line}")
        return 0

    if args.command == "open-day":
        path = readiness.open_day(args.day)
    elif args.command == "liveness":
        path = readiness.liveness(args.day, args.at or datetime.now(IST).strftime("%H:%M"))
    elif args.command == "logs":
        path = logscan.run(args.day)
    elif args.command == "value-areas":
        path = value_areas.run(args.day)
    else:
        path = jobs.run(args.day)
    result = read_phase(args.command, args.day)
    print(f"{args.command} {args.day}: {result['verdict']} -> {path}")
    return 1 if result["verdict"] == "RED" else 0


if __name__ == "__main__":
    sys.exit(main())
