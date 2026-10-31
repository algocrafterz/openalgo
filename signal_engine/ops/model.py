"""What every ops check returns, and how a phase report is written.

A phase (open-day, liveness, a close-day step) is a list of checks. Each check has one status and
the evidence for it; the phase verdict is the worst status. The report is markdown for people and
a JSON file for the notify step and the /logs workflow page, so nothing parses prose.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass

REPORTS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "reports")

OK, WARN, FAIL, NOT_VERIFIED = "OK", "WARN", "FAIL", "NOT VERIFIED"
_RANK = {OK: 0, NOT_VERIFIED: 1, WARN: 1, FAIL: 2}
VERDICT = {0: "GREEN", 1: "AMBER", 2: "RED"}


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    evidence: str


def verdict(checks: list[Check]) -> str:
    """GREEN when every check is OK, RED when any FAILed, AMBER otherwise."""
    return VERDICT[max((_RANK[c.status] for c in checks), default=0)]


def write_phase(
    phase: str, day: str, checks: list[Check], summary: str, *, folder: str = REPORTS
) -> str:
    """Write <phase>-<day>.md and .json; return the markdown path."""
    os.makedirs(folder, exist_ok=True)
    result = verdict(checks)
    rows = "\n".join(f"| {c.name} | {c.status} | {c.evidence.replace('|', '/')} |" for c in checks)
    text = (
        f"# {phase} {day} (IST): {result}\n\n{summary}\n\n"
        f"| Check | Status | Evidence |\n| --- | --- | --- |\n{rows}\n"
    )
    md = os.path.join(folder, f"{phase}-{day}.md")
    with open(md, "w", encoding="utf-8") as handle:
        handle.write(text)
    with open(os.path.join(folder, f"{phase}-{day}.json"), "w", encoding="utf-8") as handle:
        json.dump(
            {
                "phase": phase,
                "day": day,
                "verdict": result,
                "summary": summary,
                "checks": [asdict(c) for c in checks],
            },
            handle,
            indent=2,
        )
    return md


def read_phase(phase: str, day: str, *, folder: str = REPORTS) -> dict | None:
    path = os.path.join(folder, f"{phase}-{day}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)
