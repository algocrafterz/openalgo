"""Bug alerts (R3.3): a short Telegram line for the few bug events that need the owner.

Events, all HIGH only: a new bug, a reopened bug, a bug overdue (open more than 3 days, once a
week), and a fixed bug nobody verified for 7 days (about 5 sessions, once a week). One alert per
bug and event, at most MAX_ALERTS per run with an "and N more" line. Nothing here changes a bug.
Already-sent events are kept in reports/bug-alert-state.json so a new or reopened alert is
sent once, not once per day. Dry-run prints what would be sent and sends nothing.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, timedelta

from signal_engine.bugs import store
from signal_engine.ops import alerts
from signal_engine.ops.model import REPORTS

MAX_ALERTS = 5
UNVERIFIED_DAYS = 7
STATE_FILE = "bug-alert-state.json"


def _state_path(folder: str | None) -> str:
    return os.path.join(folder or REPORTS, STATE_FILE)


def _load(folder: str | None) -> set[str]:
    try:
        with open(_state_path(folder), encoding="utf-8") as handle:
            return set(json.load(handle).get("sent", []))
    except (OSError, ValueError, AttributeError):
        return set()


def _save(sent: set[str], folder: str | None) -> None:
    path = _state_path(folder)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".bug-alert-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"sent": sorted(sent)[-2000:]}, handle, indent=2)
    os.replace(tmp, path)


def _week(day: str) -> str:
    year, week, _ = date.fromisoformat(day).isocalendar()
    return f"{year}-W{week:02d}"


def _reopens(bug: dict) -> list[str]:
    return [
        h["at"][:10]
        for h in bug.get("history", [])
        if "reopened automatically" in h.get("note", "")
        or ("recurred after being" in h.get("note", "") and "reopened" in h.get("note", ""))
    ]


def _what(bug: dict) -> str:
    return " ".join(str(bug.get("plain") or bug.get("impact") or "").split())[:160]


def events(day: str, path: str | None = None) -> list[tuple[str, str, str]]:
    """(kind, condition, text) for every alertable event on `day`, most urgent first."""
    found: list[tuple[str, str, str]] = []
    bugs = [b for b in store.load(path)["bugs"] if b.get("severity") == "HIGH"]
    for bug in bugs:
        bid, title = bug["id"], bug["title"][:100]
        if bug["status"] in ("open", "in_progress") and (
            bug["first_seen"] == day or str(bug.get("created_at", ""))[:10] == day
        ):
            found.append(("bug-new", bid, f"New HIGH bug {bid}: {title}. {_what(bug)}"))
    for bug in bugs:
        reopens = _reopens(bug)
        if bug["status"] == "open" and reopens and reopens[-1] == day:
            found.append(
                (
                    "bug-reopened",
                    f"{bug['id']}:reopen{len(reopens)}",
                    f"HIGH bug {bug['id']} came back after it was fixed "
                    f"(reopened {len(reopens)} time(s)): {bug['title'][:100]}.",
                )
            )
    for bug in bugs:
        if store.is_overdue(bug, day):
            found.append(
                (
                    "bug-overdue",
                    f"{bug['id']}:{_week(day)}",
                    f"HIGH bug {bug['id']} is overdue: {store.age_days(bug, day)} days "
                    f"since first seen, still {bug['status']}. {bug['title'][:100]}.",
                )
            )
    for bug in bugs:
        fixed = store.fixed_since(bug)
        if store.awaiting_verification(bug) and fixed:
            waiting = (date.fromisoformat(day) - date.fromisoformat(fixed)).days
            if waiting >= UNVERIFIED_DAYS:
                found.append(
                    (
                        "bug-unverified",
                        f"{bug['id']}:{_week(day)}",
                        f"HIGH bug {bug['id']} was fixed {waiting} days ago and is still not "
                        f"verified: {bug['title'][:100]}.",
                    )
                )
    return found


def run(
    day: str, dry_run: bool = True, *, path: str | None = None, folder: str | None = None
) -> list[str]:
    """Send (or, in dry-run, print) the day's bug alerts. Returns one line per event handled."""
    sent_before = _load(folder)
    todo = [e for e in events(day, path) if f"{e[0]}|{e[1]}" not in sent_before]
    shown, rest = todo[:MAX_ALERTS], todo[MAX_ALERTS:]
    lines, sent = [], set(sent_before)
    for index, (kind, condition, text) in enumerate(shown):
        if rest and index == len(shown) - 1:
            text += f"\nand {len(rest)} more."
        if dry_run:
            lines.append(f"[dry-run] would send {kind} {condition}: {text}")
            continue
        ok = alerts.deliver(
            text, kind=kind, phase="bugs", condition=condition, severity="RED", folder=folder
        )
        lines.append(f"{'sent' if ok else 'NOT sent'} {kind} {condition}")
        if ok:
            sent.add(f"{kind}|{condition}")
    if rest:
        lines.append(f"and {len(rest)} more")
    if sent != sent_before:
        _save(sent, folder)
    return lines
