"""Policy for the unattended auto-fix run (signal_engine/analysis/autofix.sh).

Pure decisions only, so they can be tested without git, Claude or the tracker file:
which bug may be attempted, and whether the change a run produced stays inside what an
unattended run may touch. The shell script owns git, the worktree and the Claude call.

Scope (Tier 1): LOW and MEDIUM bugs of kind BUG or DATA, fixed only in operations, reporting,
tracker and test code. Entries, exits, stops, sizing, order paths, risk rules, config and the
OpenAlgo core are never inside this scope; those need the owner (resolve skill, Tier 2).

    uv run python -m signal_engine.bugs.autofix pick            print the next bug id, or nothing
    uv run python -m signal_engine.bugs.autofix check < numstat  exit 0 if the change is in scope
"""

from __future__ import annotations

import argparse
import sys

from signal_engine.bugs import store

ELIGIBLE_KINDS = ("BUG", "DATA")
ELIGIBLE_SEVERITIES = ("LOW", "MEDIUM")
ELIGIBLE_AREAS = ("ops", "data", "signal_engine")
MAX_ATTEMPTS = 2
ATTEMPT_PREFIX = "autofix attempt"

#: A change may touch only these paths ...
ALLOWED_PREFIXES = (
    "signal_engine/ops/",
    "signal_engine/analysis/",
    "signal_engine/bugs/store.py",
    "signal_engine/bugs/__main__.py",
    "signal_engine/tests/",
    "test/",
    "blueprints/ops_",
    "blueprints/bug_tracker.py",
)
#: ... except these, which stay with the owner even inside an allowed folder.
DENIED_PREFIXES = (
    "signal_engine/analysis/breakingtrade/",
    "signal_engine/analysis/autofix.sh",
)
DENIED_NAMES = ("config.yaml", ".env", "bugs.json", "bugs.json.lock", "autofix.py")
TEST_PREFIXES = ("signal_engine/tests/", "test/")
MAX_FILES = 8
MAX_CHANGED_LINES = 300


def attempts(bug: dict) -> int:
    """How many unattended attempts the bug's history records."""
    return sum(
        1 for h in bug.get("history", []) if str(h.get("note", "")).startswith(ATTEMPT_PREFIX)
    )


def eligible(bug: dict) -> bool:
    return (
        bug["status"] == "open"
        and bug["kind"] in ELIGIBLE_KINDS
        and bug["severity"] in ELIGIBLE_SEVERITIES
        and bug["area"] in ELIGIBLE_AREAS
        and attempts(bug) < MAX_ATTEMPTS
    )


def pick_next(bugs: list[dict]) -> dict | None:
    """The eligible bug seen on the most days, then the oldest. MEDIUM before LOW."""
    rank = {"MEDIUM": 0, "LOW": 1}
    pool = [b for b in bugs if eligible(b)]
    pool.sort(key=lambda b: (rank[b["severity"]], -len(b["occurrences"]), b["id"]))
    return pool[0] if pool else None


def is_test_path(path: str) -> bool:
    return path.startswith(TEST_PREFIXES)


def violations(changes: list[tuple[str, int, int]]) -> list[str]:
    """Why a change is out of scope. `changes` holds (path, added lines, deleted lines).

    Empty means the change may be committed to the side branch.
    """
    if not changes:
        return ["the run changed nothing"]
    problems: list[str] = []
    if len(changes) > MAX_FILES:
        problems.append(f"{len(changes)} files changed (limit {MAX_FILES})")
    total = sum(a + d for _, a, d in changes)
    if total > MAX_CHANGED_LINES:
        problems.append(f"{total} lines changed (limit {MAX_CHANGED_LINES})")
    for path, _added, deleted in changes:
        name = path.rsplit("/", 1)[-1]
        if path.startswith(DENIED_PREFIXES) or name in DENIED_NAMES:
            problems.append(f"{path} is reserved for the owner")
        elif not path.startswith(ALLOWED_PREFIXES):
            problems.append(f"{path} is outside the allowed folders")
        elif is_test_path(path) and deleted:
            problems.append(f"{path} deletes {deleted} existing test line(s)")
    if not any(is_test_path(p) for p, _, _ in changes):
        problems.append("no test was added")
    return problems


def parse_numstat(text: str) -> list[tuple[str, int, int]]:
    """Parse `git diff --numstat`. A binary file counts as a change over the size limit."""
    out = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        if added == "-" or deleted == "-":
            out.append((path, MAX_CHANGED_LINES + 1, 0))
        else:
            out.append((path, int(added), int(deleted)))
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="signal_engine.bugs.autofix")
    parser.add_argument("command", choices=("pick", "check"))
    args = parser.parse_args(argv)
    if args.command == "pick":
        bug = pick_next(store.load()["bugs"])
        if bug:
            print(bug["id"])
        return 0
    problems = violations(parse_numstat(sys.stdin.read()))
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
