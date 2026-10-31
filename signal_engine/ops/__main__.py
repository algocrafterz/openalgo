"""Ops phase steps. Each command owns one duty and writes one output (reports/<step>-<day>.md/.json).

    uv run python -m signal_engine.ops open-day [--day D]       readiness before the open
    uv run python -m signal_engine.ops liveness [--day D]       the first minutes after the open
    uv run python -m signal_engine.ops logs [--day D]           error signatures vs the last 5 sessions
    uv run python -m signal_engine.ops jobs [--day D]           scheduled-job audit
    uv run python -m signal_engine.ops value-areas [--day D]    store the day's VAH/POC/VAL, check coverage
    uv run python -m signal_engine.ops notify PHASE [--day D]   the one Telegram message for a phase

Exit status: 0 GREEN or AMBER, 1 RED, so a phase status file shows a failed check as a failed step.
"""

import argparse
import sys
from datetime import datetime

from signal_engine.ops import jobs, logscan, readiness, value_areas, verdict
from signal_engine.ops.model import read_phase
from signal_engine.timeutils import IST


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="signal_engine.ops")
    parser.add_argument(
        "command", choices=("open-day", "liveness", "logs", "jobs", "value-areas", "notify")
    )
    parser.add_argument("phase", nargs="?", help="for notify: open-day, liveness or close-day")
    parser.add_argument("--day", default=datetime.now(IST).strftime("%Y-%m-%d"))
    parser.add_argument("--at", help="liveness only: end of the window, HH:MM (default: now)")
    parser.add_argument("--dry-run", action="store_true", help="notify only: print, do not send")
    args = parser.parse_args(argv)

    if args.command == "notify":
        if args.phase not in ("open-day", "liveness", "close-day"):
            parser.error("notify needs a phase: open-day, liveness or close-day")
        text = verdict.message(args.phase, args.day)
        if text is None:
            print(f"{args.phase} {args.day}: nothing to send")
            return 0
        if args.dry_run:
            print(f"[dry-run] would send: {text}")
            return 0
        sent = verdict.send(args.phase, args.day)
        print(f"{'sent' if sent else 'NOT sent'}: {text}")
        return 0 if sent else 1

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
