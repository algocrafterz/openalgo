"""Overnight retry and the dead-man check: when they act, and when they stay quiet."""

import json
import os
import sqlite3
import time
from datetime import datetime

import pytest

from signal_engine.ops import actions, automation
from signal_engine.timeutils import IST

TODAY = "2026-11-12"  # a Thursday
PREV = "2026-11-11"
MORNING = datetime(2026, 11, 12, 8, 56, tzinfo=IST)
LATE = datetime(2026, 11, 12, 21, 40, tzinfo=IST)  # after every phase is due


@pytest.fixture
def env(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    reports.mkdir()
    monkeypatch.setattr(actions, "REPORTS", str(reports))
    monkeypatch.setattr(actions, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(automation, "REPORTS", str(reports))
    db = tmp_path / "openalgo.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE market_holidays (id INTEGER PRIMARY KEY, holiday_date TEXT, description TEXT);
        CREATE TABLE market_holiday_exchanges (holiday_id INTEGER, exchange_code TEXT);
        INSERT INTO market_holidays VALUES (1, '2026-11-10', 'Diwali Balipratipada');
        INSERT INTO market_holiday_exchanges VALUES (1, 'NSE');
        """
    )
    con.commit()
    con.close()
    monkeypatch.setattr(automation, "DB", str(db))
    return reports


def _status(reports, day, *rows):
    body = "step\tstarted\tended\texit\n" + "".join(f"{r}\n" for r in rows)
    (reports / f"close-day-{day}-status.tsv").write_text(body)


def _calls(monkeypatch, rc=0):
    calls = []

    def fake_run(action, day, run_id, *, state_dir=None, auto=False):
        calls.append((action, day, auto))
        return rc

    monkeypatch.setattr(actions, "run", fake_run)
    return calls


# --- trading day ---------------------------------------------------------------------------


def test_weekends_and_nse_holidays_are_not_trading_days(env):
    assert automation.is_trading_day("2026-11-12")
    assert not automation.is_trading_day("2026-11-14")  # Saturday
    assert not automation.is_trading_day("2026-11-10")  # Diwali Balipratipada


def test_an_unreadable_calendar_counts_as_a_trading_day(env, monkeypatch):
    monkeypatch.setattr(automation, "DB", "/nonexistent/openalgo.db")
    assert automation.is_trading_day("2026-11-12")


# --- overnight retry -----------------------------------------------------------------------


def test_a_failed_backfill_is_retried_once_in_the_morning(env, monkeypatch):
    _status(env, PREV, "backfill\t15:36:03\t17:56:08\t1")
    calls = _calls(monkeypatch)
    lines = automation.retry_failed(now=MORNING, folder=str(env))
    assert calls == [("backfill", PREV, True)]
    assert "retried the data download, done" in lines[0]


def test_a_retry_that_fails_again_says_so(env, monkeypatch):
    _status(env, PREV, "backfill\t15:36:03\t17:56:08\t1")
    _calls(monkeypatch, rc=1)
    assert "failed again" in automation.retry_failed(now=MORNING, folder=str(env))[0]


def test_nothing_is_retried_when_the_backfill_worked_or_never_ran(env, monkeypatch):
    calls = _calls(monkeypatch)
    _status(env, PREV, "backfill\t15:36:03\t15:43:24\t0")
    assert "finished" in automation.retry_failed(now=MORNING, folder=str(env))[0]
    _status(env, PREV, "eod\t15:36:03\t15:36:16\t0")
    assert "did not run" in automation.retry_failed(now=MORNING, folder=str(env))[0]
    assert calls == []


def test_the_latest_row_decides_so_a_fixed_backfill_is_left_alone(env, monkeypatch):
    _status(env, PREV, "backfill\t15:36:03\t17:56:08\t1", "backfill\t18:00:00\t18:07:00\t0")
    calls = _calls(monkeypatch)
    automation.retry_failed(now=MORNING, folder=str(env))
    assert calls == []


def test_it_does_not_retry_near_the_open_or_on_a_holiday(env, monkeypatch):
    _status(env, PREV, "backfill\t15:36:03\t17:56:08\t1")
    calls = _calls(monkeypatch)
    late = datetime(2026, 11, 12, 9, 11, tzinfo=IST)
    assert "too close" in automation.retry_failed(now=late, folder=str(env))[0]
    holiday = datetime(2026, 11, 10, 8, 56, tzinfo=IST)
    assert "not a trading day" in automation.retry_failed(now=holiday, folder=str(env))[0]
    assert calls == []


def test_a_day_is_retried_at_most_once(env, monkeypatch):
    _status(env, PREV, "backfill\t15:36:03\t17:56:08\t1")
    folder = actions.STATE_DIR
    os.makedirs(folder)
    run_id = f"{int(time.time()) - 3600}-backfill"
    with open(os.path.join(folder, f"{run_id}.json"), "w") as handle:
        json.dump(
            {
                "id": run_id,
                "action": "backfill",
                "day": PREV,
                "auto": True,
                "pid": 1,
                "ended": "x",
                "outcome": "failed",
                "exit": 1,
            },
            handle,
        )
    calls = _calls(monkeypatch)
    assert "already retried" in automation.retry_failed(now=MORNING, folder=str(env))[0]
    assert calls == []


def test_a_human_run_does_not_use_up_the_automatic_retry(env, monkeypatch):
    _status(env, PREV, "backfill\t15:36:03\t17:56:08\t1")
    folder = actions.STATE_DIR
    os.makedirs(folder)
    run_id = f"{int(time.time()) - 3600}-backfill"
    with open(os.path.join(folder, f"{run_id}.json"), "w") as handle:
        json.dump(
            {
                "id": run_id,
                "action": "backfill",
                "day": PREV,
                "auto": False,
                "pid": 1,
                "ended": "x",
                "outcome": "failed",
                "exit": 1,
            },
            handle,
        )
    calls = _calls(monkeypatch)
    automation.retry_failed(now=MORNING, folder=str(env))
    assert calls == [("backfill", PREV, True)]


# --- dead-man ------------------------------------------------------------------------------


def _complete_phase(folder, phase, day=TODAY):
    """A phase that finished cleanly: status rows, manifest and reports (content is tested in
    test_ops_deadman_cron.py)."""
    from signal_engine.ops import model

    steps = model.CRITICAL_STEPS[phase]
    rows = "".join(f"{n}\t09:12:00\t09:12:09\t0\n" for n in steps)
    (folder / f"{phase}-{day}-status.tsv").write_text("step\tstarted\tended\texit\n" + rows)
    model.write_phase(phase, day, [model.Check("a", model.OK, "x")], "s", folder=str(folder))


def test_a_missing_morning_record_is_reported_in_plain_words(env):
    _complete_phase(env, "open-day")
    text = automation.deadman("morning", TODAY, folder=str(env), now=LATE)
    assert "09:27 after-the-open check did not run" in text
    assert "09:12" not in text  # that one finished cleanly
    assert "exit" not in text and "cron" not in text.lower()


def test_nothing_is_reported_when_every_record_exists_or_it_is_no_trading_day(env):
    for phase in ("open-day", "liveness"):
        _complete_phase(env, phase)
    assert automation.deadman("morning", TODAY, folder=str(env), now=LATE) is None
    assert automation.deadman("morning", "2026-11-10", folder=str(env)) is None  # holiday
    assert automation.deadman("close", "2026-11-14", folder=str(env)) is None  # Saturday
    assert automation.deadman("nonsense", TODAY, folder=str(env)) is None


def test_a_missing_close_record_is_reported(env):
    assert "15:36 end-of-day checks did not run" in automation.deadman(
        "close", TODAY, folder=str(env), now=LATE
    )
