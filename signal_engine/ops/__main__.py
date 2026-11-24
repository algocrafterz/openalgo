"""Ops phase steps. Each command owns one duty and writes one output (reports/<step>-<day>.md/.json).

    uv run python -m signal_engine.ops open-day [--day D]       readiness before the open
    uv run python -m signal_engine.ops liveness [--day D]       the first minutes after the open
    uv run python -m signal_engine.ops logs [--day D]           error signatures vs the last 5 sessions
    uv run python -m signal_engine.ops jobs [--day D]           scheduled-job audit
    uv run python -m signal_engine.ops value-areas [--day D]    store the day's VAH/POC/VAL, check coverage
    uv run python -m signal_engine.ops track [--day D] [--logs [--write]]   record FAIL checks; log bugs dry-run unless --write
    uv run python -m signal_engine.ops retry-failed             overnight retry of yesterday's failed backfill (installed 08:56 line)
    uv run python -m signal_engine.ops recover                  08:36: retry only the open steps of the previous trading day
    uv run python -m signal_engine.ops close-day-retry [--day D] 16:01: retry only the open steps of today (close_day.sh --retry)
    uv run python -m signal_engine.ops deadman PHASE            print and exit 1 when a checkpoint is missing, incomplete or failed
    uv run python -m signal_engine.ops run-action ACTION --day D --id RUN   run a step again (the page)

Nothing here sends to Telegram. The results are the reports, the run manifests and /logs/workflow.

Exit status: 0 GREEN or AMBER, 1 RED, so a phase status file shows a failed check as a failed step.
"""

import argparse
import sys
from datetime import datetime

from signal_engine.ops import (
    actions,
    automation,
    autotrack,
    jobs,
    logscan,
    readiness,
    recovery,
    value_areas,
    verdict,
)
from signal_engine.ops.model import read_phase
from signal_engine.timeutils import IST


def _previous_day_line(day: str) -> None:
    """The morning dead-man's extra line: what the previous trading day left open and what the
    08:36 recovery did. WARN level, never RED, and it never changes the dead-man's own result."""
    if not automation.is_trading_day(day):
        return
    line = recovery.previous_day_line(day, folder=automation.REPORTS)
    print(f"previous day [{line['level']}] {line['outcome']}: {line['text']}")


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
            "track",
            "run-action",
            "retry-failed",
            "recover",
            "close-day-retry",
            "deadman",
        ),
    )
    parser.add_argument(
        "phase", nargs="?", help="deadman: morning or close; run-action: the action"
    )
    parser.add_argument("--day", default=datetime.now(IST).strftime("%Y-%m-%d"))
    parser.add_argument("--at", help="liveness only: end of the window, HH:MM (default: now)")
    parser.add_argument("--id", help="run-action only: the run id the page is watching")
    parser.add_argument("--logs", action="store_true", help="track only: detect recurring errors")
    parser.add_argument("--write", action="store_true", help="track --logs only: write the bugs")
    args = parser.parse_args(argv)

    if args.command == "retry-failed":
        for line in automation.retry_failed():
            print(line)
        return 0

    if args.command == "recover":
        return recovery.recover_main()

    if args.command == "close-day-retry":
        return recovery.retry_main(args.day)

    if args.command == "deadman":
        if args.phase not in automation.EXPECTED:
            parser.error("deadman needs a phase: morning or close")
        report = automation.deadman_report(args.phase, args.day)
        text = automation.deadman(args.phase, args.day)
        state = automation.worst_state(report)
        if args.phase == "morning" and report:  # only once the morning checkpoints are due
            _previous_day_line(args.day)
        if text is None:
            if not report:
                # A holiday, a weekend or a phase not yet due: nothing was checked.
                print(f"deadman {args.phase} {args.day}: nothing due, nothing checked")
                return 0
            print(f"deadman {args.phase} {args.day}: healthy, every due checkpoint is complete")
            return 0
        # Printed to logs/ops_cron.log; the exit status marks the finding in the cron log line.
        print(f"deadman {args.phase} {args.day} ({state}): {text}")
        return 1

    if args.command == "run-action":
        if args.phase not in actions.ACTIONS or not actions.RUN_ID.match(args.id or ""):
            parser.error("run-action needs a known action and --id")
        return actions.run(args.phase, args.day, args.id)

    if args.command == "track":
        lines = autotrack.record_failures(args.day)
        print(f"track {args.day}: {len(lines)} failed check(s)")
        for line in lines:
            print(f"  {line}")
        if args.logs:
            for line in logscan.detect(args.day, dry_run=not args.write):
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
