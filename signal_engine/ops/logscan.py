"""Close-day step `logs`: today's errors grouped by signature, against the previous 5 sessions.

Owns error analysis for the day; the review reads its output and never re-counts. NEW = not seen
in the previous 5 sessions; RISING = more than twice the previous daily average and at least 5.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, timedelta

from signal_engine.ops import logs
from signal_engine.ops.model import FAIL, OK, WARN, Check, write_phase

#: Signatures that touch order flow or a known failure mode get named in the report.
FLAGS = {
    "rate limit": re.compile(r"429|too many requests|rate.?limit", re.I),
    "timeout": re.compile(r"timeout|timed out|readtimeout", re.I),
    "reconnect": re.compile(r"connection to remote host was lost|reconnect|disconnected", re.I),
    "db locked": re.compile(r"database is locked", re.I),
    "fd exhaustion": re.compile(r"too many open files", re.I),
    "token": re.compile(r"token (expired|invalid)|session expired|invalid session", re.I),
    "greenlet/lock": re.compile(
        r"greenlet|cannot switch to a different thread|has no attribute 'lock'", re.I
    ),
    "order rejected": re.compile(r"reject", re.I),
    "unconfirmed exit": re.compile(r"exit uncertain|unconfirmed|tracking blind", re.I),
}


def _sessions_before(day: str, count: int) -> list[str]:
    """The `count` weekdays before `day` (holidays read as empty days, which only lowers a baseline)."""
    current, found = date.fromisoformat(day), []
    while len(found) < count:
        current -= timedelta(days=1)
        if current.weekday() < 5:
            found.append(current.isoformat())
    return found


def _counts(day: str) -> tuple[Counter, dict]:
    counts, first = Counter(), {}
    for source in (logs.engine_errors(day), logs.app_errors(day)):
        for when, logger_name, message, symbol in source:
            sig = logs.signature(logger_name, message, symbol)
            counts[sig] += 1
            first.setdefault(sig, when)
    return counts, first


def classify(today: Counter, history: list[Counter]) -> dict[str, str]:
    """NEW / RISING / recurring for each of today's signatures."""
    result = {}
    for sig, n in today.items():
        before = [h.get(sig, 0) for h in history]
        if not any(before):
            result[sig] = "NEW"
        elif n >= 5 and n > 2 * (sum(before) / len(before)):
            result[sig] = "RISING"
        else:
            result[sig] = "recurring"
    return result


def run(day: str) -> str:
    today, first = _counts(day)
    history = [_counts(d)[0] for d in _sessions_before(day, 5)]
    kinds = classify(today, history)
    rows = sorted(today.items(), key=lambda item: (kinds[item[0]] == "recurring", -item[1]))
    checks = []
    for sig, n in rows[:40]:
        flags = [name for name, pattern in FLAGS.items() if pattern.search(sig)]
        status = WARN if kinds[sig] in ("NEW", "RISING") else OK
        if kinds[sig] != "recurring" and {"fd exhaustion", "greenlet/lock", "db locked"} & set(
            flags
        ):
            status = FAIL
        checks.append(
            Check(
                f"{kinds[sig]} x{n}",
                status,
                f"first {first[sig]}; {sig}" + (f" [{', '.join(flags)}]" if flags else ""),
            )
        )
    new = sum(1 for k in kinds.values() if k == "NEW")
    rising = sum(1 for k in kinds.values() if k == "RISING")
    summary = (
        f"{sum(today.values())} error records in {len(today)} signatures (signal_engine errors_{day}.jsonl "
        f"and log/errors.jsonl); {new} NEW, {rising} RISING against the previous 5 weekdays. "
        "log/errors.jsonl keeps only its last 1000 records, so older baselines can be short."
    )
    return write_phase(
        "logs", day, checks or [Check("errors", OK, "no error records today")], summary
    )
