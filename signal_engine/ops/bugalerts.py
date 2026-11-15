"""Bug alerts (R3.3): a short Telegram line for the few bug events that need the owner.

Events, all HIGH only: a new bug, a reopened bug, a bug overdue (open more than 3 days, once a
week), and a fixed bug nobody verified for 7 days (about 5 sessions, once a week). One alert per
bug and event, at most MAX_ALERTS per run with an "and N more" line. Nothing here changes a bug.
Already-sent events are kept in reports/bug-alert-state.json so a new or reopened alert is
sent once, not once per day. New and reopened events stay alertable for LOOKBACK_BUSINESS_DAYS
business days, so a send that failed or was held back by the cap is retried on the following
days, across a weekend or a holiday too. Dry-run prints what
would be sent and sends nothing.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, timedelta

from signal_engine.bugs import store
from signal_engine.ops import alerts
from signal_engine.ops.model import REPORTS

MAX_ALERTS = 5
UNVERIFIED_DAYS = 7
#: New and reopened events stay alertable for this many business days (the run day included when
#: it is one), so an alert held back by the cap or a failed send on a Friday is still retried on
#: Monday. Weekend and holiday days in between do not count. The sent-key state dedupes it; an
#: event older than the window is never alerted.
LOOKBACK_BUSINESS_DAYS = 3
LOOKBACK_DAYS = LOOKBACK_BUSINESS_DAYS  # old name, kept for importers
_MAX_WALK_DAYS = 30  # bounds the walk back whatever the calendar says
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


def _is_business_day(day: date) -> bool:
    """Weekday and not an NSE holiday (OpenAlgo's calendar). Never raises: Mon-Fri on any failure."""
    if day.weekday() > 4:
        return False
    try:
        from signal_engine.ops import automation  # lazy: keeps this module's import light

        return bool(automation.is_trading_day(day.isoformat()))
    except Exception:  # noqa: BLE001 - an unavailable calendar must not stop an alert
        return True


def _window(day: str) -> set[str]:
    """Every calendar day from `day` back to the LOOKBACK_BUSINESS_DAYS-th business day.

    Weekend and holiday days inside the span are included (a bug first seen on a Sunday is still
    new on Monday) but do not use up the count.
    """
    current = date.fromisoformat(day)
    found: set[str] = set()
    counted = 0
    for _ in range(_MAX_WALK_DAYS):
        found.add(current.isoformat())
        if _is_business_day(current):
            counted += 1
            if counted >= LOOKBACK_BUSINESS_DAYS:
                break
        current -= timedelta(days=1)
    return found


def _what(bug: dict) -> str:
    return " ".join(str(bug.get("plain") or bug.get("impact") or "").split())[:160]


def events(day: str, path: str | None = None) -> list[tuple[str, str, str]]:
    """(kind, condition, text) for every alertable event on `day`, most urgent first."""
    found: list[tuple[str, str, str]] = []
    bugs = [b for b in store.load(path)["bugs"] if b.get("severity") == "HIGH"]
    recent = _window(day)
    for bug in bugs:
        bid, title = bug["id"], bug["title"][:100]
        if bug["status"] in ("open", "in_progress") and (
            bug["first_seen"] in recent or str(bug.get("created_at", ""))[:10] in recent
        ):
            found.append(("bug-new", bid, f"New HIGH bug {bid}: {title}. {_what(bug)}"))
    for bug in bugs:
        reopens = _reopens(bug)
        if bug["status"] == "open" and reopens and reopens[-1] in recent:
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
    """Send (or, in dry-run, print) the day's bug alerts. Returns one line per event handled.

    A real run holds an exclusive cross-process lock from reading the state to saving it, so two
    simultaneous runs cannot both send the same alert. If the other run holds it longer than the
    tracker's lock timeout, this run sends nothing and the next one picks the alerts up.
    """
    if dry_run:
        return _run(day, dry_run, path, folder)
    state = _state_path(folder)
    os.makedirs(os.path.dirname(state), exist_ok=True)
    try:
        with store._locked(state):
            return _run(day, dry_run, path, folder)
    except store.TrackerBusy:
        _warn(
            f"bug alerts for {day} SKIPPED: another bug-alert run holds the lock; nothing was "
            "sent, the next run inside the window delivers"
        )
        return ["skipped: another bug-alert run is in progress"]


def _warn(message: str) -> None:
    """One WARNING line on stderr (close_day.sh sends stderr to ops_cron.log). Never raises."""
    try:
        print(f"WARNING: {message}", file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def _next_business_day(day: str) -> str:
    current = date.fromisoformat(day)
    for _ in range(_MAX_WALK_DAYS):
        current += timedelta(days=1)
        if _is_business_day(current):
            break
    return current.isoformat()


def _expiring(day: str, unsent: list[tuple[str, str, str]], path: str | None) -> list[str]:
    """Keys of unsent new/reopened events that will not be alertable on the next business day."""
    pending = [e for e in unsent if e[0] in ("bug-new", "bug-reopened")]
    if not pending:
        return []
    still = {f"{k}|{c}" for k, c, _ in events(_next_business_day(day), path)}
    return [f"{k}|{c}" for k, c, _ in pending if f"{k}|{c}" not in still]


def _run(day: str, dry_run: bool, path: str | None, folder: str | None) -> list[str]:
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
        keys = ", ".join(f"{k}|{c}" for k, c, _ in rest)
        lines.append(
            f"SUPPRESSED by the {MAX_ALERTS}-alert cap ({len(rest)}, not sent, retried next run): {keys}"
        )
        _warn(
            f"bug alerts for {day}: {len(rest)} event(s) over the {MAX_ALERTS}-alert cap were "
            f"NOT sent (retried next run inside the window): {keys}"
        )
    unsent = [e for e in todo if f"{e[0]}|{e[1]}" not in sent]
    gone = _expiring(day, unsent, path)
    if gone:
        lines.append(
            f"EXPIRING UNSENT (never sent, leaves the window next business day): {', '.join(gone)}"
        )
        _warn(
            f"bug alerts for {day}: EXPIRING UNSENT, never sent and no longer alertable after "
            f"today: {', '.join(gone)}"
        )
    if sent != sent_before:
        _save(sent, folder)
    if rest:
        lines.append(f"and {len(rest)} more")  # stays the last line
    return lines
