"""The 16:01 afternoon retry and the 08:36 morning recovery (signal_engine/ops/recovery.py).

Everything runs on tmp_path folders with a simulated command runner and a fake clock. No real
phase, backfill, eod, review, broker, OpenAlgo call or Telegram message is made: the runner never
starts a process, and alerts.deliver is replaced by a list.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from signal_engine.ops import automation, model
from signal_engine.ops.model import IST, OK, Check, write_phase

DAY = "2026-11-12"  # Thursday
RECOVER_TODAY = "2026-11-13"  # Friday: the previous trading day is DAY
ALL_STEPS = (
    "backfill",
    "eod",
    "execution_health",
    "logs",
    "value_areas",
    "jobs",
    "track",
    "review",
    "notify",
)
pytestmark = pytest.mark.filterwarnings("ignore")


def at(day, hhmm, sec=0):
    return datetime.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=IST) + timedelta(
        seconds=sec
    )


class Clock:
    def __init__(self, now):
        self.now = now
        self.sleeps = []

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)


REVIEW_OK = (
    'Review.\n```close-day-verdict\n{"verdict": "AMBER", "line": "Net 1; watch."}\n```\n'
    "```bug-tracker\n[]\n```\n"
)


class World:
    def __init__(self, tmp_path):
        self.folder = str(tmp_path / "reports")
        self.logs = str(tmp_path / "logs")
        self.snaps = tmp_path / "snaps"
        self.state = str(tmp_path / "state")
        os.makedirs(self.folder)
        self.snaps.mkdir()

    # -- seeding the 15:36 run ------------------------------------------------------------
    def seed(self, rows=None, *, state="failed", day=DAY, review=True, snapshot=True):
        """The close-day run of `day`: a status file, a finished manifest, the reports."""
        rows = dict.fromkeys(ALL_STEPS, 0) | (rows or {})
        status = "step\tstarted\tended\texit\n"
        model.manifest_start("close-day", day, pid=1, folder=self.folder)
        for step, code in rows.items():
            if code is None:
                continue
            status += f"{step}\t15:36:00\t15:36:09\t{code}\n"
            model.manifest_step("close-day", day, step, "15:36:00", "15:36:09", code, self.folder)
        Path(self.folder, f"close-day-{day}-status.tsv").write_text(status)
        model.manifest_finish("close-day", day, state, folder=self.folder)
        for name in ("logs", "value-areas", "jobs"):
            write_phase(name, day, [Check("a", OK, "x")], "s", folder=self.folder)
        if review:
            Path(self.folder, f"eod-{day}-claude-review.md").write_text(REVIEW_OK)
        if snapshot:
            (self.snaps / f"tradebook_{day}.json").write_text("[]")

    def status_text(self, day=DAY):
        return Path(self.folder, f"close-day-{day}-status.tsv").read_text()

    def manifest(self, phase, day=DAY):
        return json.loads(Path(self.folder, f"{phase}-{day}-run.json").read_text())


@pytest.fixture
def world(tmp_path, monkeypatch):
    # A readable calendar with no holiday: every weekday is a trading day. An unreadable calendar
    # makes the recovery skip itself (see the holiday-table tests below).
    monkeypatch.setattr(automation, "DB", _calendar(tmp_path, []))
    return World(tmp_path)


def test_final_deadman_a_retry_still_running_makes_the_day_incomplete(world):
    world.seed({"track": 1}, state="failed")
    model.manifest_start("close-day-retry", DAY, pid=os.getpid(), folder=world.folder)
    judged = automation.judge_phase("close-day", DAY, world.folder, now=at(DAY, "16:20"))
    assert any("retry is still running" in r for r in judged["reasons"])
    assert judged["state"] in ("incomplete", "failed")
    # on a day that was otherwise healthy, a running retry alone is incomplete
    world.seed({}, state="completed")
    judged = automation.judge_phase("close-day", DAY, world.folder, now=at(DAY, "16:20"))
    assert judged["state"] == "incomplete"


def test_a_retry_that_was_interrupted_is_incomplete(world):
    world.seed({}, state="completed")
    model.manifest_start("close-day-retry", DAY, pid=1_999_999_999, folder=world.folder)  # dead
    judged = automation.judge_phase("close-day", DAY, world.folder, now=at(DAY, "16:28"))
    assert judged["state"] == "incomplete"
    assert any("interrupted" in r for r in judged["reasons"])


def test_the_morning_recovery_is_the_later_attempt_and_can_supersede_a_failed_retry(world):
    world.seed({"track": 1}, state="failed")
    model.manifest_start("close-day-retry", DAY, pid=1, folder=world.folder)
    model.manifest_finish("close-day-retry", DAY, "failed", "unresolved: track", world.folder)
    model.manifest_start("close-day-recover", DAY, pid=1, folder=world.folder)
    model.manifest_finish("close-day-recover", DAY, "completed", "", world.folder)
    Path(world.folder, f"close-day-{DAY}-status.tsv").write_text(
        world.status_text() + "track\t08:40:00\t08:40:01\t0\n"
    )
    judged = automation.judge_phase("close-day", DAY, world.folder, now=at(RECOVER_TODAY, "09:40"))
    assert judged["state"] == "healthy", judged


def test_the_deadman_close_time_constant_is_1628():
    assert automation.FINAL_DEADMAN_CLOSE == "16:28"


# ---- (C) next-trading-day recovery ------------------------------------------------------------


def _calendar(tmp_path, holidays, name="cal.db"):
    db = tmp_path / name
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE market_holidays (id INTEGER PRIMARY KEY, holiday_date TEXT, description TEXT);"
        "CREATE TABLE market_holiday_exchanges (holiday_id INTEGER, exchange_code TEXT);"
    )
    for i, day in enumerate(holidays, 1):
        con.execute("INSERT INTO market_holidays VALUES (?, ?, 'h')", (i, day))
        con.execute("INSERT INTO market_holiday_exchanges VALUES (?, 'NSE')", (i,))
    con.commit()
    con.close()
    return str(db)
