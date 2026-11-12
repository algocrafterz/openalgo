"""Step `track`: record the day's failed checks in the bug tracker without the AI review.

The close-day review used to be the only writer of the tracker, so a day on which the review
could not run (usage limit, expired sign-in, deadline) left every failure unrecorded. This step
is plain code over the stored phase reports. It is safe to run any number of times: each finding
has a stable key (`workflow-finding:<key>`, the same key the /logs/workflow "Report as bug"
action uses), and `store.report` records a day on a bug once.

Only FAIL results and failed steps are recorded. WARN, NEW and RISING results stay on the
workflow page. Severity is MEDIUM and is for the owner to review: nothing here can mean a wrong
order or an unprotected position by itself, and the status stays `open`.
"""

from __future__ import annotations

import os

from loguru import logger

from signal_engine.bugs import store
from signal_engine.ops.model import REPORTS

#: Finding group -> (kind, area). Anything not listed is an operations problem.
_KIND_AREA = {
    "open-day": ("EXECUTION", "ops"),
    "liveness": ("EXECUTION", "ops"),
    "logs": ("EXECUTION", "ops"),
    "jobs": ("EXECUTION", "ops"),
    "value-areas": ("DATA", "data"),
    "step": ("EXECUTION", "ops"),
    "execution": ("EXECUTION", "broker"),
}


def record_failures(day: str, *, folder: str | None = None, path: str | None = None) -> list[str]:
    """Record every FAIL finding of `day`. Returns one line per finding for the cron log."""
    # Imported here: the blueprint module pulls in Flask, which the other ops steps do not need.
    from blueprints.ops_findings import (
        MARKER,
        PHASE_LABEL,
        enumerate_findings,
        finding_key,
        finding_title,
    )

    folder = folder or REPORTS
    lines: list[str] = []
    for finding in enumerate_findings(day, folder):
        if finding["status"] != "FAIL":
            continue
        if finding["group"] == "jobs" and finding["name"].startswith("phase "):
            continue  # a summary of a step that is recorded on its own
        key = MARKER + finding_key(finding)
        kind, area = _KIND_AREA.get(finding["group"], ("EXECUTION", "ops"))
        sources = [key]
        report = finding["group"] if finding["group"] != "execution" else "execution-health"
        if os.path.exists(os.path.join(folder, f"{report}-{day}.md")):
            sources.append(f"signal_engine/reports/{report}-{day}.md")
        evidence = (
            f"Recorded automatically: {PHASE_LABEL[finding['phase']]} checkpoint on {day}"
            f"{' at ' + finding['checked_at'] if finding['checked_at'] else ''}; "
            f"{finding['name']}: {finding['evidence']}"
        )
        try:
            result = store.report(
                source_key=key,
                title=finding_title(finding),
                kind=kind,
                severity="MEDIUM",
                area=area,
                day=day,
                impact=(
                    "Recorded automatically from a failed daily check. The risk level is a "
                    "default and has not been reviewed."
                ),
                evidence=[evidence],
                sources=sources,
                plain=finding["plain"],
                path=path,
            )
        except store.AmbiguousMatch as exc:
            lines.append(f"skipped (ambiguous, {', '.join(exc.candidates)}): {finding['name']}")
        except store.TrackerError:
            logger.exception(f"The tracker refused the finding {finding['name']}")
            lines.append(f"refused: {finding['name']}")
        else:
            bug_id = result["bug"]["id"]
            if result["created"]:
                lines.append(f"added {bug_id}: {finding['name']}")
            elif result["recorded"]:
                lines.append(f"seen {bug_id}: {finding['name']}")
            else:
                lines.append(f"unchanged {bug_id}: {finding['name']}")
    return lines
