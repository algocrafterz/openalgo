"""Unattended upkeep around the daily workflow: an overnight retry and a dead-man check.

Both only use what already exists. The retry runs the same fixed actions as the page's Run again
button (signal_engine/ops/actions.py). The dead-man check reads the phase status files and sends the
one Telegram line when a checkpoint that should have run did not. Neither touches orders, positions,
config or code.

    uv run python -m signal_engine.ops retry-failed          08:56 weekdays: redo yesterday's failed backfill
    uv run python -m signal_engine.ops deadman PHASE         PHASE is morning (09:40) or close (16:20)
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timedelta

from signal_engine.ops import actions
from signal_engine.ops.model import REPORTS

DB = os.path.join(actions.REPO, "db", "openalgo.db")
#: The retry must end before the market is near: backfill reads the broker.
RETRY_BEFORE = "09:10"


def is_trading_day(day: str, *, db_path: str | None = None) -> bool:
    """A weekday that is not an NSE holiday in OpenAlgo's own calendar."""
    if datetime.strptime(day, "%Y-%m-%d").weekday() > 4:
        return False
    try:
        con = sqlite3.connect(f"file:{db_path or DB}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute(
                "SELECT 1 FROM market_holidays h JOIN market_holiday_exchanges e "
                "ON e.holiday_id = h.id WHERE h.holiday_date = ? AND e.exchange_code = 'NSE' "
                "LIMIT 1",
                (day,),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return True  # an unreadable calendar must not silence the check
    return row is None


def _latest_exit(day: str, step: str, folder: str) -> int | None:
    """The newest recorded exit code of a close-day step, or None when it never ran."""
    path = os.path.join(folder, f"close-day-{day}-status.tsv")
    try:
        with open(path, encoding="utf-8") as handle:
            rows = [line.rstrip("\n").split("\t") for line in handle][1:]
    except OSError:
        return None
    codes = [int(r[3]) for r in rows if len(r) == 4 and r[0] == step and r[3].lstrip("-").isdigit()]
    return codes[-1] if codes else None


def _previous_day(today: str, folder: str) -> str | None:
    """The newest earlier day that has a close-day status file, within the retry window."""
    limit = datetime.strptime(today, "%Y-%m-%d").date() - timedelta(days=actions.MAX_AGE_DAYS)
    days = sorted(
        (
            name[len("close-day-") : -len("-status.tsv")]
            for name in os.listdir(folder)
            if name.startswith("close-day-") and name.endswith("-status.tsv")
        ),
        reverse=True,
    )
    for day in days:
        if day < today and datetime.strptime(day, "%Y-%m-%d").date() >= limit:
            return day
    return None


def _already_tried(day: str, state_dir: str | None) -> bool:
    folder = state_dir or actions.STATE_DIR
    if not os.path.isdir(folder):
        return False
    for name in os.listdir(folder):
        if name.endswith(".json"):
            state = actions.read_state(name[:-5], folder)
            if state and state.get("auto") and state.get("action") == "backfill":
                if state.get("day") == day:
                    return True
    return False


def retry_failed(
    *, now: datetime | None = None, folder: str | None = None, state_dir: str | None = None
) -> list[str]:
    """Re-run the last session's data download if it failed. At most once per day, and only before
    the market is near. Returns one line per decision for the cron log."""
    now = now or actions.now_ist()
    folder = folder or REPORTS
    today = now.strftime("%Y-%m-%d")
    if not is_trading_day(today):
        return [f"{today} is not a trading day - nothing to retry."]
    if now.strftime("%H:%M") >= RETRY_BEFORE:
        return [f"It is {now.strftime('%H:%M')}, too close to the open - not retrying."]
    day = _previous_day(today, folder) if os.path.isdir(folder) else None
    if day is None:
        return ["No recent close-day record - nothing to retry."]
    code = _latest_exit(day, "backfill", folder)
    if code is None or code == 0:
        return [f"{day}: the data download {'did not run' if code is None else 'finished'}."]
    if _already_tried(day, state_dir):
        return [f"{day}: already retried once - leaving it for you."]
    if actions.running(state_dir):
        return ["Another run is in progress - not retrying now."]
    reason = actions.refusal("backfill", day, now=now, folder=folder)
    if reason:
        return [f"{day}: {reason}"]
    run_id = f"{int(time.time())}-backfill"
    rc = actions.run("backfill", day, run_id, state_dir=state_dir, auto=True)
    return [f"{day}: retried the data download, {'done' if rc == 0 else 'failed again'}."]


#: What each dead-man phase expects to find, and how it reads to a trader when missing.
EXPECTED = {
    "morning": (
        ("open-day", "The 09:12 readiness check did not run today."),
        ("liveness", "The 09:27 after-the-open check did not run today."),
    ),
    "close": (("close-day", "The 15:36 end-of-day checks did not run today."),),
}


def deadman(kind: str, day: str, *, folder: str | None = None) -> str | None:
    """The alert text when a checkpoint that should have run left no record, else None."""
    folder = folder or REPORTS
    if kind not in EXPECTED or not is_trading_day(day):
        return None
    missing = [
        text
        for phase, text in EXPECTED[kind]
        if not os.path.exists(os.path.join(folder, f"{phase}-{day}-status.tsv"))
    ]
    if not missing:
        return None
    return (
        f"{' '.join(missing)} The scheduler may have stopped or the PC started late. "
        "Check the machine, then run it by hand. Details: /logs/workflow"
    )
