"""The bug tracker: one JSON file, written only through this module.

Every observation from the EOD review, the paper review and the execution-health check that
needs follow-up becomes one entry here, so a defect seen on four days is one bug with four
occurrences, not four paragraphs in four reports. The OpenAlgo page at /logs/bugs reads the
same file (blueprints/bug_tracker.py); it never writes it.
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
import tempfile
import time
from contextlib import contextmanager
from datetime import date, datetime

from signal_engine.timeutils import IST

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # Linux and macOS
    msvcrt = None

TRACKER_PATH = os.path.join(os.path.dirname(__file__), "bugs.json")

KINDS = ("BUG", "EXECUTION", "DATA", "TUNING-IDEA")
SEVERITIES = ("HIGH", "MEDIUM", "LOW")
STATUSES = ("open", "in_progress", "fixed", "verified", "wontfix")
#: A status that no longer needs work. "fixed" is not one: a fix is proven only by a clean session.
CLOSED_STATUSES = ("verified", "wontfix")

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class TrackerError(ValueError):
    """An entry or a change the tracker refuses."""


class TrackerBusy(TrackerError):
    """Another process held the tracker lock for longer than LOCK_TIMEOUT."""


class AmbiguousMatch(TrackerError):
    """A finding matches existing bugs, but not unambiguously. Nothing was written."""

    def __init__(self, message: str, candidates: list[str]):
        super().__init__(message)
        self.candidates = candidates


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


#: Longest a writer waits for another writer, in seconds. Writes take milliseconds, so a wait this
#: long means the holder is stuck, and the caller gets a plain "busy" refusal instead of a hang.
LOCK_TIMEOUT = 10.0
_LOCK_POLL = 0.02
_BUSY = (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK, errno.EDEADLK)


def _try_lock(handle) -> bool:
    """One non-blocking attempt at the exclusive lock. True when it is ours.

    Never waits inside the operating system: a greenlet that did would stop the whole eventlet
    worker, and the holder (an HTTP request on the same hub) could then never release it.
    """
    try:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError as exc:
        if exc.errno in _BUSY:
            return False
        raise


def _unlock(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle, fcntl.LOCK_UN)
    elif msvcrt is not None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def _locked(path: str):
    """Hold an exclusive cross-process lock for a read-modify-write, so no writer loses an entry.

    Works between processes (the CLI, the cron `apply`, the web server) and between threads, on
    Linux (flock) and Windows (msvcrt byte lock). The lock dies with its holder, so a killed
    process never leaves it stuck. While waiting it sleeps with time.sleep, which eventlet turns
    into a yield, so the rest of the server keeps running.
    """
    deadline = time.monotonic() + LOCK_TIMEOUT
    with open(path + ".lock", "a") as handle:
        while not _try_lock(handle):
            if time.monotonic() >= deadline:
                raise TrackerBusy(
                    "The bug tracker is being updated by another process. Try again in a moment."
                )
            time.sleep(_LOCK_POLL)
        try:
            yield
        finally:
            _unlock(handle)


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


def _replace(tmp: str, path: str) -> None:
    """os.replace, retried on Windows, where a reader holding the file open refuses the swap."""
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 19:
                raise
            time.sleep(0.05)


def _save(data: dict, path: str) -> None:
    """Write atomically: a reader never sees a half-written file, and a crash keeps the old one.

    The new content goes to a temporary file in the same folder, is flushed to disk, and only then
    replaces the tracker in one step. The tracker keeps its existing permissions.
    """
    folder = os.path.dirname(path) or "."
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".bugs-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        _replace(tmp, path)
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


def _validate_new(*, title, kind, severity, status, seen, impact) -> None:
    if not title.strip() or not impact.strip():
        raise TrackerError("title and impact are required")
    _check_choice("kind", kind, KINDS)
    _check_choice("severity", severity, SEVERITIES)
    _check_choice("status", status, STATUSES)
    _check_date(seen)


def _append_bug(
    data: dict,
    *,
    title: str,
    kind: str,
    severity: str,
    area: str,
    seen: str,
    impact: str,
    strategies: list[str] | None,
    evidence: list[str] | None,
    root_cause: str,
    sources: list[str] | None,
    status: str,
    plain: str,
) -> dict:
    """Add a new bug to the loaded tracker, refusing a title an open bug already has. Not saved."""
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
        #: One or two sentences for a trader: what is wrong and what it means for them.
        "plain": plain.strip(),
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
    return bug


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
    plain: str = "",
    path: str | None = None,
) -> dict:
    """Record a new bug and return it. Refuses a title that an open bug already has."""
    path = path or TRACKER_PATH
    _validate_new(
        title=title, kind=kind, severity=severity, status=status, seen=seen, impact=impact
    )
    with _locked(path):
        data = load(path)
        bug = _append_bug(
            data,
            title=title,
            kind=kind,
            severity=severity,
            area=area,
            seen=seen,
            impact=impact,
            strategies=strategies,
            evidence=evidence,
            root_cause=root_cause,
            sources=sources,
            status=status,
            plain=plain,
        )
        _save(data, path)
    return bug


def _fixed_on(bug: dict) -> str | None:
    """IST date of the latest change to fixed, from the history; None when never fixed."""
    days = [h["at"][:10] for h in bug.get("history", []) if h.get("status") == "fixed"]
    return max(days) if days else None


def _record_seen(
    data: dict, bug: dict, day: str, evidence: list[str] | None, sources: list[str] | None
) -> None:
    """Record that `bug` happened again on `day` in the loaded tracker. Not saved."""
    fixed_on = _fixed_on(bug)
    if day not in bug["occurrences"]:
        bug["occurrences"] = sorted([*bug["occurrences"], day])
    bug["last_seen"] = max(bug["occurrences"])
    bug["first_seen"] = min(bug["occurrences"])
    bug["evidence"] = [*bug["evidence"], *(evidence or [])]
    bug["sources"] = [*bug["sources"], *[s for s in (sources or []) if s not in bug["sources"]]]
    note = f"seen on {day}"
    if bug["status"] in ("fixed", "verified"):
        if fixed_on is None or day > fixed_on:
            note += f": recurred after being {bug['status']}, reopened"
            bug["status"] = "open"
        else:
            note += f": on or before the fix ({fixed_on}), stays {bug['status']}"
    bug["updated_at"] = _now()
    bug["history"].append({"at": bug["updated_at"], "status": bug["status"], "note": note})


def seen(
    bug_id: str,
    day: str,
    *,
    evidence: list[str] | None = None,
    sources: list[str] | None = None,
    path: str | None = None,
) -> dict:
    """Record that a bug happened again on `day`. A fixed or verified bug seen again reopens.

    Only a session after the fix reopens it: a re-run of an earlier day (BUG-001 was reopened by
    a 2026-10-31 re-run of 10-30, the session that found it) is evidence of the old code.
    """
    path = path or TRACKER_PATH
    _check_date(day)
    with _locked(path):
        data = load(path)
        bug = _find(data, bug_id)
        _record_seen(data, bug, day, evidence, sources)
        _save(data, path)
    return bug


def _same_title(data: dict, title: str) -> list[dict]:
    return [
        b
        for b in data["bugs"]
        if b["title"].strip().lower() == title.strip().lower()
        and b["status"] not in CLOSED_STATUSES
    ]


def report(
    *,
    source_key: str,
    title: str,
    kind: str,
    severity: str,
    area: str,
    day: str,
    impact: str,
    evidence: list[str] | None = None,
    sources: list[str] | None = None,
    plain: str = "",
    path: str | None = None,
) -> dict:
    """Record a finding: a new bug, or a new day on the bug that already tracks it.

    The whole decision is made under one lock hold, so two writers (web requests, the CLI) cannot
    both see "no bug yet" or both record the same day. `source_key` is an exact entry in a bug's
    `sources` and is the primary identity. Without it, an open bug with the same title is used
    only when it is the one candidate and its kind and component agree; anything less certain
    raises AmbiguousMatch and writes nothing.

    Returns {"created", "recorded", "bug"}. `recorded` is False when `day` was already on the bug.
    """
    path = path or TRACKER_PATH
    _validate_new(title=title, kind=kind, severity=severity, status="open", seen=day, impact=impact)
    if source_key not in (sources or []):
        raise TrackerError("the source key must be one of the sources")
    with _locked(path):
        data = load(path)
        keyed = [b for b in data["bugs"] if source_key in b.get("sources", [])]
        if len(keyed) > 1:
            ids = sorted(b["id"] for b in keyed)
            raise AmbiguousMatch(f"{', '.join(ids)} all track this finding", ids)
        existing = keyed[0] if keyed else None
        if existing is None:
            titled = _same_title(data, title)
            if titled:
                fits = [b for b in titled if b["kind"] == kind and b["area"] == area.strip()]
                if len(titled) > 1 or len(fits) != 1:
                    ids = sorted(b["id"] for b in titled)
                    raise AmbiguousMatch(
                        f"{', '.join(ids)} already {'tracks' if len(ids) == 1 else 'track'} a bug "
                        "with this title, but not clearly the same problem",
                        ids,
                    )
                existing = fits[0]
        if existing is not None:
            if day in existing["occurrences"] and source_key in existing["sources"]:
                return {"created": False, "recorded": False, "bug": existing}
            _record_seen(data, existing, day, evidence, sources)
            _save(data, path)
            return {"created": False, "recorded": True, "bug": existing}
        bug = _append_bug(
            data,
            title=title,
            kind=kind,
            severity=severity,
            area=area,
            seen=day,
            impact=impact,
            strategies=None,
            evidence=evidence,
            root_cause="",
            sources=sources,
            status="open",
            plain=plain,
        )
        _save(data, path)
        return {"created": True, "recorded": True, "bug": bug}


def update(
    bug_id: str, *, status: str | None = None, note: str = "", path: str | None = None, **fields
) -> dict:
    """Change a bug's status or text fields. "fixed" needs a fix, "verified" a verification."""
    path = path or TRACKER_PATH
    editable = {
        "title",
        "plain",
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
                    plain=item.get("plain", ""),
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
