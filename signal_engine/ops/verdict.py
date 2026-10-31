"""Step `notify`: the one Telegram message a phase may send.

open-day and liveness send only when their verdict is RED (AMBER is on the /logs workflow page).
close-day sends one line every trading day: the review's verdict, so silence means the
automation did not run. This is the only scheduled Telegram traffic besides failure alerts.
"""

from __future__ import annotations

import json
import os
import re

from signal_engine.ops.model import REPORTS, read_phase

_BLOCK = re.compile(r"```close-day-verdict\s*\n(.*?)```", re.S)


def close_day_verdict(day: str, folder: str = REPORTS) -> dict:
    """The review's verdict block, or AMBER with the reason when it is missing or unreadable."""
    path = os.path.join(folder, f"eod-{day}-claude-review.md")
    if not os.path.exists(path):
        return {"verdict": "AMBER", "line": "close-day review did not produce a report"}
    with open(path, encoding="utf-8") as handle:
        blocks = _BLOCK.findall(handle.read())
    try:
        data = json.loads(blocks[-1])
        if data.get("verdict") in ("GREEN", "AMBER", "RED") and data.get("line"):
            return {"verdict": data["verdict"], "line": str(data["line"])[:300]}
    except (IndexError, ValueError, AttributeError):
        pass
    return {"verdict": "AMBER", "line": "close-day review had no readable verdict block"}


def _failed_steps(phase: str, day: str, folder: str = REPORTS) -> list[str]:
    path = os.path.join(folder, f"{phase}-{day}-status.tsv")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return [
            ln.split("\t")[0]
            for ln in handle.read().splitlines()[1:]
            if ln and not ln.endswith("\t0")
        ]


def message(phase: str, day: str, folder: str = REPORTS) -> str | None:
    """The text to send for this phase and day, or None when nothing should be sent."""
    if phase == "close-day":
        found = close_day_verdict(day, folder)
        failed = _failed_steps("close-day", day, folder)
        steps = f" Steps failed: {', '.join(failed)}." if failed else ""
        return (
            f"Close-day {day}: {found['verdict']}. {found['line']}{steps} Details: /logs/workflow"
        )
    data = read_phase(phase, day, folder=folder)
    if data is None:
        return f"{phase} {day}: RED. The check did not produce a result. Details: /logs/workflow"
    if data["verdict"] != "RED":
        return None
    failed = [c["name"] for c in data["checks"] if c["status"] == "FAIL"]
    return f"{phase} {day}: RED. Failed: {', '.join(failed)}. Details: /logs/workflow"


def send(phase: str, day: str) -> bool:
    text = message(phase, day)
    if text is None:
        return False
    from signal_engine.analysis.eod_review import send_to_admin_channel

    return send_to_admin_channel(text, _phase())


def _phase() -> str:
    """The admin channel of OpenAlgo's current mode; analyze when it cannot be read."""
    import asyncio

    from signal_engine.api_client import fetch_trading_mode

    try:
        mode, _ = asyncio.run(fetch_trading_mode())
    except Exception:  # noqa: BLE001 - an unreadable mode must not stop a failure alert
        return "analyze"
    return "live" if mode == "live" else "analyze"
