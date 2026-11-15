"""Read-only summaries for the Daily Workflow page: run states, recurring failures, last alert.

Nothing here writes a file, sends a message or touches trading code. It only reads the run
manifests (model.read_run), the day reports (`<check>-<day>.json`) and the Telegram state file
(alerts.load_state, called only after the file is checked to be readable, because load_state
renames a damaged file aside).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta

from signal_engine.ops import alerts, model

PHASES = ("open-day", "liveness", "close-day")
CHECKED = ("open-day", "liveness", "logs", "value-areas", "jobs")
#: Phase -> due HH:MM. The one source of the workflow's clock times: automation.EXPECTED is built
#: from it and the page reads it from the day payload's `schedule`. It describes when cron runs
#: the phases (run_phase.sh, close_day.sh); changing it here does not move a cron job.
DUE = {"open-day": "09:12", "liveness": "09:27", "close-day": "15:36"}
#: A phase is judged missed or late only after its due time plus this margin.
MARGIN_MINUTES = 10
WINDOW = 10
TEXT_MAX = 120
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MASK = re.compile(r"\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}(:\d{2})?")
_STATUS = re.compile(r"^(?:open-day|liveness|close-day)-(\d{4}-\d{2}-\d{2})-status\.tsv$")


def schedule() -> dict:
    """The phase times and margin the page shows, for the day payload's `schedule` key."""
    return {"phases": dict(DUE), "margin_minutes": MARGIN_MINUTES}


def _clip(value: object, limit: int = TEXT_MAX) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---- run states ----------------------------------------------------------------------------


def _is_trading_day(day: str) -> bool:
    try:
        from signal_engine.ops import automation

        return automation.is_trading_day(day)
    except Exception:  # an unreadable calendar must not hide a missed run
        return datetime.strptime(day, "%Y-%m-%d").weekday() <= 4


def is_trading_day(day: str) -> bool:
    """Public form of the trading-day rule used by run_status; never raises for a valid day."""
    return _is_trading_day(day)


def _due_at(day: str, phase: str, now: datetime) -> datetime:
    due = datetime.strptime(f"{day} {DUE[phase]}", "%Y-%m-%d %H:%M")
    return due.replace(tzinfo=now.tzinfo) + timedelta(minutes=MARGIN_MINUTES)


def run_status(day: str, folder: str, now: datetime | None = None) -> list[dict]:
    """One row per phase: the manifest state plus what a reader derives from it.

    `state` is what the manifest writer uses (running, completed, failed, interrupted, timed_out,
    legacy, unreadable) or one the reader derives: `missed` (trading day, due time plus margin
    passed, no record), `not_due` (no record yet, still before that time) and `not_expected`
    (no record on a non-trading day). `late` is True when the run started after its due time plus
    the margin.
    """
    now = now or model.now_ist()
    trading = _is_trading_day(day)
    out = []
    for phase in PHASES:
        run = model.read_run(phase, day, folder, now=now)
        state = run["state"]
        started = run.get("started_at")
        if state == model.MISSING:
            if not trading:
                state = "not_expected"
            elif now >= _due_at(day, phase, now):
                state = "missed"
            else:
                state = "not_due"
        late = False
        if started and state not in ("missed", "not_due", "not_expected"):
            try:
                begun = datetime.fromisoformat(str(started))
                if begun.tzinfo is None:
                    begun = begun.replace(tzinfo=now.tzinfo)
                late = begun > _due_at(day, phase, now)
            except ValueError:
                late = False
        out.append(
            {
                "phase": phase,
                "due": DUE[phase],
                "state": state,
                "late": late,
                "started_at": started,
                "ended_at": run.get("ended_at"),
                "note": _clip(run.get("note", "")),
            }
        )
    return out


# ---- recurring failures --------------------------------------------------------------------


def _norm(name: str) -> str:
    return " ".join(_MASK.sub("#", str(name)).split()).lower()


def recent_days(day: str, folder: str, n: int = WINDOW) -> list[str]:
    """The n newest sessions (days with a status file) up to and including `day`, oldest first."""
    if not os.path.isdir(folder):
        return []
    days = {m.group(1) for name in os.listdir(folder) if (m := _STATUS.match(name))}
    days.add(day)
    return sorted(d for d in days if d <= day)[-n:]


def _failed_checks(day: str, folder: str) -> dict[tuple[str, str], str]:
    """{(group, normalised name): name} of the checks that failed on a day."""
    found: dict[tuple[str, str], str] = {}
    for group in CHECKED:
        path = os.path.join(folder, f"{group}-{day}.json")
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            checks = data.get("checks", []) if isinstance(data, dict) else []
        except (OSError, ValueError):
            continue
        for check in checks:
            if isinstance(check, dict) and check.get("status") == "FAIL" and check.get("name"):
                found[(group, _norm(check["name"]))] = str(check["name"])
    return found


def recurrence(day: str, folder: str, n: int = WINDOW) -> dict:
    """How often each check failed over the last n sessions ending at `day`.

    Returns {"window": sessions looked at, "items": [{group, name, count, first, last,
    failing_today}]} sorted most frequent first. Items cover every check that failed on any of
    those sessions; the page shows the count on today's failed rows and lists those with 2+.
    """
    days = recent_days(day, folder, n)
    seen: dict[tuple[str, str], dict] = {}
    for d in days:
        for key, name in _failed_checks(d, folder).items():
            item = seen.setdefault(
                key, {"group": key[0], "name": name, "count": 0, "first": d, "last": d}
            )
            item.update(name=name, last=d)
            item["count"] += 1
    items = sorted(seen.values(), key=lambda i: (-i["count"], i["group"], i["name"]))
    for item in items:
        item["failing_today"] = item["last"] == day
    return {"window": len(days), "items": items}


# ---- last notification and recovery --------------------------------------------------------


def _readable_state(folder: str) -> bool:
    try:
        with open(os.path.join(folder, alerts.STATE_FILE), encoding="utf-8") as handle:
            data = json.load(handle)
        return isinstance(data, dict) and isinstance(data.get("keys"), dict)
    except (OSError, ValueError):
        return False


def notifications(folder: str) -> dict:
    """The last alert and recovery, with no token, chat id or message text.

    `key` fields are the alert key (kind|phase|condition), clipped. Keys are never message bodies.
    """
    path = os.path.join(folder, alerts.STATE_FILE)
    if not os.path.exists(path):
        return {"available": False, "reason": "No alert has been recorded yet."}
    if not _readable_state(folder):
        return {"available": False, "reason": "The alert record could not be read."}
    state = alerts.load_state(folder)
    log = [e for e in state.get("log", []) if isinstance(e, dict)]
    keys = {k: v for k, v in state.get("keys", {}).items() if isinstance(v, dict)}
    last = log[-1] if log else None
    sent = [(v.get("last_sent_at"), k) for k, v in keys.items() if v.get("last_sent_at")]
    recovered = [(v.get("recovered_at"), k) for k, v in keys.items() if v.get("recovered_at")]
    last_sent = max(sent) if sent else None
    last_rec = max(recovered) if recovered else None
    return {
        "available": True,
        "last_attempt_at": _clip(last.get("at"), 40) if last else None,
        "last_result": _clip(last.get("outcome"), 40) if last else None,
        "last_key": _clip(last.get("key")) if last else None,
        "last_sent_at": last_sent[0] if last_sent else None,
        "last_sent_key": _clip(last_sent[1]) if last_sent else None,
        "open_red": sorted(_clip(k) for k, v in keys.items() if v.get("status") == "red"),
        "last_recovery_at": last_rec[0] if last_rec else None,
        "last_recovery_key": _clip(last_rec[1]) if last_rec else None,
    }
