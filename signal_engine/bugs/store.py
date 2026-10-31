"""The bug tracker: one JSON file, written only through this module.

Every observation from the EOD review, the paper review and the execution-health check that
needs follow-up becomes one entry here, so a defect seen on four days is one bug with four
occurrences, not four paragraphs in four reports. The OpenAlgo page at /logs/bugs reads the
same file (blueprints/bug_tracker.py); it never writes it.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import date, datetime

from signal_engine.timeutils import IST

try:
    import fcntl
except ImportError:  # Windows: no advisory lock, the single writer is the CLI anyway
    fcntl = None

TRACKER_PATH = os.path.join(os.path.dirname(__file__), "bugs.json")

KINDS = ("BUG", "EXECUTION", "DATA", "TUNING-IDEA")
SEVERITIES = ("HIGH", "MEDIUM", "LOW")
STATUSES = ("open", "in_progress", "fixed", "verified", "wontfix")
#: A status that no longer needs work. "fixed" is not one: a fix is proven only by a clean session.
CLOSED_STATUSES = ("verified", "wontfix")

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class TrackerError(ValueError):
    """An entry or a change the tracker refuses."""


def _now() -> str:
    return datetime.now(IST).replace(microsecond=0).isoformat()


def _check_date(value: str) -> str:
    if not _DATE.match(value or ""):
        raise TrackerError(f"date must be YYYY-MM-DD, got {value!r}")
    date.fromisoformat(value)
    return value


def _check_choice(name: str, value: str, choices: tuple) -> str:
    if value not in choices:
        raise TrackerError(f"{name} must be one of {', '.join(choices)}, got {value!r}")
    return value


@contextmanager
def _locked(path: str):
    """Hold an exclusive lock for a read-modify-write, so two writers never lose an entry."""
    with open(path + ".lock", "a") as handle:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_UN)


def load(path: str | None = None) -> dict:
    """The whole tracker. A missing file is an empty tracker."""
    path = path or TRACKER_PATH
    if not os.path.exists(path):
        return {"version": 1, "bugs": []}
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not isinstance(data.get("bugs"), list):
        raise TrackerError(f"{path} is not a bug tracker file")
    return data


def _save(data: dict, path: str) -> None:
    """Write atomically: a reader never sees a half-written file."""
    folder = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".bugs-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _find(data: dict, bug_id: str) -> dict:
    for bug in data["bugs"]:
        if bug["id"] == bug_id:
            return bug
    raise TrackerError(f"no bug {bug_id}")


def _next_id(data: dict) -> str:
    numbers = [
        int(b["id"].split("-")[1]) for b in data["bugs"] if b.get("id", "").startswith("BUG-")
    ]
    return f"BUG-{(max(numbers) + 1) if numbers else 1:03d}"


def add(
    *,
    title: str,
    kind: str,
    severity: str,
    area: str,
    seen: str,
    impact: str,
    strategies: list[str] | None = None,
    evidence: list[str] | None = None,
    root_cause: str = "",
    sources: list[str] | None = None,
    status: str = "open",
    path: str | None = None,
) -> dict:
    """Record a new bug and return it. Refuses a title that an open bug already has."""
    path = path or TRACKER_PATH
    if not title.strip() or not impact.strip():
        raise TrackerError("title and impact are required")
    _check_choice("kind", kind, KINDS)
    _check_choice("severity", severity, SEVERITIES)
    _check_choice("status", status, STATUSES)
    _check_date(seen)
    with _locked(path):
        data = load(path)
        for bug in data["bugs"]:
            if (
                bug["title"].strip().lower() == title.strip().lower()
                and bug["status"] not in CLOSED_STATUSES
            ):
                raise TrackerError(
                    f"{bug['id']} already tracks this; record a new occurrence with 'seen'"
                )
        now = _now()
        bug = {
            "id": _next_id(data),
            "title": title.strip(),
            "kind": kind,
            "severity": severity,
            "status": status,
            "area": area.strip(),
            "strategies": sorted(set(strategies or [])),
            "first_seen": seen,
            "last_seen": seen,
            "occurrences": [seen],
            "impact": impact.strip(),
            "evidence": list(evidence or []),
            "root_cause": root_cause.strip(),
            "fix": "",
            "fix_commit": "",
            "verification": "",
            "sources": list(sources or []),
            "history": [{"at": now, "status": status, "note": "recorded"}],
            "created_at": now,
            "updated_at": now,
        }
        data["bugs"].append(bug)
        _save(data, path)
    return bug


def seen(
    bug_id: str,
    day: str,
    *,
    evidence: list[str] | None = None,
    sources: list[str] | None = None,
    path: str | None = None,
) -> dict:
    """Record that a bug happened again on `day`. A fixed or verified bug seen again reopens."""
    path = path or TRACKER_PATH
    _check_date(day)
    with _locked(path):
        data = load(path)
        bug = _find(data, bug_id)
        if day not in bug["occurrences"]:
            bug["occurrences"] = sorted([*bug["occurrences"], day])
        bug["last_seen"] = max(bug["occurrences"])
        bug["first_seen"] = min(bug["occurrences"])
        bug["evidence"] = [*bug["evidence"], *(evidence or [])]
        bug["sources"] = [*bug["sources"], *[s for s in (sources or []) if s not in bug["sources"]]]
        note = f"seen on {day}"
        if bug["status"] in ("fixed", "verified"):
            note += f": recurred after being {bug['status']}, reopened"
            bug["status"] = "open"
        bug["updated_at"] = _now()
        bug["history"].append({"at": bug["updated_at"], "status": bug["status"], "note": note})
        _save(data, path)
    return bug


def update(
    bug_id: str, *, status: str | None = None, note: str = "", path: str | None = None, **fields
) -> dict:
    """Change a bug's status or text fields. "fixed" needs a fix, "verified" a verification."""
    path = path or TRACKER_PATH
    editable = {
        "title",
        "severity",
        "area",
        "impact",
        "root_cause",
        "fix",
        "fix_commit",
        "verification",
    }
    unknown = set(fields) - editable
    if unknown:
        raise TrackerError(f"cannot edit {', '.join(sorted(unknown))}")
    if "severity" in fields and fields["severity"] is not None:
        _check_choice("severity", fields["severity"], SEVERITIES)
    if status is not None:
        _check_choice("status", status, STATUSES)
    with _locked(path):
        data = load(path)
        bug = _find(data, bug_id)
        for name, value in fields.items():
            if value is not None:
                bug[name] = value.strip()
        if status == "fixed" and not bug["fix"]:
            raise TrackerError("a fixed bug needs --fix describing the change")
        if status == "verified" and not bug["verification"]:
            raise TrackerError(
                "a verified bug needs --verification naming the clean session or test"
            )
        if status is not None:
            bug["status"] = status
        bug["updated_at"] = _now()
        bug["history"].append(
            {"at": bug["updated_at"], "status": bug["status"], "note": note or "updated"}
        )
        _save(data, path)
    return bug


def note(bug_id: str, text: str, *, path: str | None = None) -> dict:
    """Add a history note without changing the status (for example: evidence toward verifying)."""
    path = path or TRACKER_PATH
    if not text.strip():
        raise TrackerError("a note needs text")
    with _locked(path):
        data = load(path)
        bug = _find(data, bug_id)
        bug["updated_at"] = _now()
        bug["history"].append(
            {"at": bug["updated_at"], "status": bug["status"], "note": text.strip()}
        )
        _save(data, path)
    return bug


#: What an unattended review may do. Status changes (fixed, verified, wontfix) need a person.
UNATTENDED_ACTIONS = ("add", "seen", "note")


def apply_actions(actions: list, *, origin: str, path: str | None = None) -> list[str]:
    """Apply a review's proposed tracker changes one by one; a bad entry is reported, not fatal.

    Returns one line per action. Used by the 15:40 EOD cron, whose Claude run is read-only and
    hands its findings over as a JSON block (signal_engine/analysis/eod_claude_review.sh).
    """
    results = []
    for item in actions if isinstance(actions, list) else []:
        action = item.get("action") if isinstance(item, dict) else None
        try:
            if action not in UNATTENDED_ACTIONS:
                raise TrackerError(f"action {action!r} is not allowed unattended")
            if action == "add":
                bug = add(
                    title=item.get("title", ""),
                    kind=item.get("kind", ""),
                    severity=item.get("severity", ""),
                    area=item.get("area", ""),
                    seen=item.get("date", ""),
                    impact=item.get("impact", ""),
                    strategies=item.get("strategies"),
                    evidence=item.get("evidence"),
                    root_cause=item.get("root_cause", ""),
                    sources=item.get("sources"),
                    path=path,
                )
                results.append(f"added {bug['id']}: {bug['title']} ({origin})")
            elif action == "seen":
                bug = seen(
                    item.get("id", ""),
                    item.get("date", ""),
                    evidence=item.get("evidence"),
                    sources=item.get("sources"),
                    path=path,
                )
                results.append(
                    f"seen {bug['id']} on {item.get('date')} -> {bug['status']} ({origin})"
                )
            else:
                bug = note(item.get("id", ""), f"[{origin}] {item.get('text', '')}", path=path)
                results.append(f"noted {bug['id']} ({origin})")
        except TrackerError as exc:
            results.append(f"refused {action}: {exc}")
    return results


_BLOCK = re.compile(r"```bug-tracker\s*\n(.*?)```", re.S)


def actions_from_report(text: str) -> list | None:
    """The last bug-tracker JSON block in a review report; None when there is none."""
    blocks = _BLOCK.findall(text)
    if not blocks:
        return None
    actions = json.loads(blocks[-1])
    if not isinstance(actions, list):
        raise TrackerError("the bug-tracker block must hold a JSON list")
    return actions
