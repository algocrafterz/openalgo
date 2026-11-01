"""Bug tracker CLI: the only way entries are written. Page: http://localhost:5000/logs/bugs

    uv run python -m signal_engine.bugs list [--all] [--json]
    uv run python -m signal_engine.bugs show BUG-001
    uv run python -m signal_engine.bugs add --title ... --kind BUG --severity HIGH --area signal_engine \
        --seen 2026-10-30 --impact ... [--strategy S]... [--evidence E]... [--root-cause ...] [--source P]...
    uv run python -m signal_engine.bugs seen BUG-001 --date 2026-10-31 [--evidence E]... [--source P]...
    uv run python -m signal_engine.bugs update BUG-001 [--status fixed] [--fix ...] [--commit SHA]
        [--verification ...] [--root-cause ...] [--severity ...] [--note ...]
    uv run python -m signal_engine.bugs note BUG-001 --text ...
    uv run python -m signal_engine.bugs apply FILE --origin eod-cron-2026-10-31   # add/seen/note only
"""

import argparse
import json
import sys

from signal_engine.bugs import store


def _line(bug: dict) -> str:
    days = len(bug["occurrences"])
    return (
        f"{bug['id']}  {bug['status']:<11} {bug['severity']:<6} {bug['kind']:<11} "
        f"{bug['last_seen']}  x{days:<2} {bug['title']}"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="signal_engine.bugs", description="OpenAlgo bug tracker")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="open bugs, most severe first")
    listing.add_argument("--all", action="store_true", help="include verified and wontfix")
    listing.add_argument("--json", action="store_true")

    show = sub.add_parser("show")
    show.add_argument("id")

    add = sub.add_parser("add")
    add.add_argument("--title", required=True)
    add.add_argument("--kind", required=True, choices=store.KINDS)
    add.add_argument("--severity", required=True, choices=store.SEVERITIES)
    add.add_argument(
        "--area", required=True, help="signal_engine, openalgo-core, sandbox, broker, data, ops"
    )
    add.add_argument("--seen", required=True, help="IST trading day, YYYY-MM-DD")
    add.add_argument("--impact", required=True, help="what it costs a trader, in plain words")
    add.add_argument(
        "--plain", default="", help="one or two plain sentences: what is wrong, what it means"
    )
    add.add_argument("--strategy", action="append", default=[])
    add.add_argument("--evidence", action="append", default=[])
    add.add_argument("--root-cause", default="")
    add.add_argument("--source", action="append", default=[])
    add.add_argument("--status", default="open", choices=store.STATUSES)

    seen = sub.add_parser("seen", help="the bug happened again")
    seen.add_argument("id")
    seen.add_argument("--date", required=True)
    seen.add_argument("--evidence", action="append", default=[])
    seen.add_argument("--source", action="append", default=[])

    update = sub.add_parser("update")
    update.add_argument("id")
    update.add_argument("--status", choices=store.STATUSES)
    update.add_argument("--severity", choices=store.SEVERITIES)
    update.add_argument("--title")
    update.add_argument("--plain")
    update.add_argument("--area")
    update.add_argument("--impact")
    update.add_argument("--root-cause")
    update.add_argument("--fix")
    update.add_argument("--commit", dest="fix_commit")
    update.add_argument("--verification")
    update.add_argument("--note", default="")

    note = sub.add_parser("note", help="history note, status unchanged")
    note.add_argument("id")
    note.add_argument("--text", required=True)

    apply = sub.add_parser("apply", help="apply a JSON list of add/seen/note actions")
    apply.add_argument(
        "file", help="a JSON list, or a review report (.md) with a bug-tracker block"
    )
    apply.add_argument("--origin", required=True)
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "list":
            bugs = store.load()["bugs"]
            if not args.all:
                bugs = [b for b in bugs if b["status"] not in store.CLOSED_STATUSES]
            rank = {s: i for i, s in enumerate(store.SEVERITIES)}
            bugs.sort(key=lambda b: (rank[b["severity"]], b["id"]))
            if args.json:
                print(json.dumps(bugs, indent=2, ensure_ascii=False))
            else:
                for bug in bugs:
                    print(_line(bug))
                print(f"{len(bugs)} bug(s)")
        elif args.command == "show":
            print(json.dumps(store._find(store.load(), args.id), indent=2, ensure_ascii=False))
        elif args.command == "add":
            bug = store.add(
                title=args.title,
                kind=args.kind,
                severity=args.severity,
                area=args.area,
                seen=args.seen,
                impact=args.impact,
                strategies=args.strategy,
                evidence=args.evidence,
                plain=args.plain,
                root_cause=args.root_cause,
                sources=args.source,
                status=args.status,
            )
            print(_line(bug))
        elif args.command == "seen":
            print(
                _line(store.seen(args.id, args.date, evidence=args.evidence, sources=args.source))
            )
        elif args.command == "update":
            bug = store.update(
                args.id,
                status=args.status,
                note=args.note,
                severity=args.severity,
                title=args.title,
                plain=args.plain,
                area=args.area,
                impact=args.impact,
                root_cause=args.root_cause,
                fix=args.fix,
                fix_commit=args.fix_commit,
                verification=args.verification,
            )
            print(_line(bug))
        elif args.command == "note":
            print(_line(store.note(args.id, args.text)))
        elif args.command == "apply":
            with open(args.file, encoding="utf-8") as handle:
                text = handle.read()
            # A review report (.md) carries its actions in a fenced bug-tracker block.
            actions = (
                store.actions_from_report(text) if args.file.endswith(".md") else json.loads(text)
            )
            if actions is None:
                print(f"refused: {args.file} has no bug-tracker block", file=sys.stderr)
                return 3
            for line in store.apply_actions(actions, origin=args.origin):
                print(line)
    except store.TrackerError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
