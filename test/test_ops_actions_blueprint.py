"""The run-again endpoint starts only listed actions, one at a time, and never takes a command."""

import os
from datetime import datetime

import pytest

from blueprints import ops_actions
from blueprints.ops_workflow import day_view
from signal_engine.ops import actions
from signal_engine.timeutils import IST

DAY = "2026-11-11"
AFTER = datetime(2026, 11, 11, 16, 5, tzinfo=IST)


@pytest.fixture
def env(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / f"close-day-{DAY}-status.tsv").write_text(
        "step\tstarted\tended\texit\nbackfill\t15:36:03\t17:56:08\t1\n"
    )
    monkeypatch.setattr(actions, "REPORTS", str(reports))
    monkeypatch.setattr(actions, "STATE_DIR", str(tmp_path / "state"))
    started = []

    class FakeProc:
        pid = os.getpid()

        def poll(self):
            return 0

    def fake_popen(argv, **kwargs):
        started.append((argv, kwargs))
        return FakeProc()

    monkeypatch.setattr(ops_actions.subprocess, "Popen", fake_popen)
    return reports, started


def test_the_catalogue_says_why_an_action_cannot_start(env):
    reports, _ = env
    rows = ops_actions.catalogue(DAY, now=AFTER, folder=str(reports))
    assert {r["key"] for r in rows} == set(actions.ACTIONS)
    assert all(r["available"] and r["reason"] == "" for r in rows)
    early = ops_actions.catalogue(DAY, now=datetime(2026, 11, 11, 10, 0, tzinfo=IST))
    assert all(not r["available"] and "15:36" in r["reason"] for r in early)


def test_start_launches_the_fixed_command_detached_and_not_through_a_shell(env):
    reports, started = env
    result = ops_actions.start("logs", DAY, now=AFTER, folder=str(reports))
    assert result["status"] == 202
    argv, kwargs = started[0]
    assert argv[1:4] == ["-m", "signal_engine.ops", "run-action"]
    assert argv[4:] == ["logs", "--day", DAY, "--id", result["run"]["id"]]
    assert kwargs["start_new_session"] is True
    assert "shell" not in kwargs
    assert result["run"]["action"] == "logs"


def test_a_second_run_is_refused_while_one_is_going(env):
    reports, started = env
    assert ops_actions.start("logs", DAY, now=AFTER, folder=str(reports))["status"] == 202
    second = ops_actions.start("jobs", DAY, now=AFTER, folder=str(reports))
    assert second["status"] == 409 and "still running" in second["error"]
    assert len(started) == 1


def test_start_refuses_unknown_actions_and_bad_days_without_starting_anything(env):
    reports, started = env
    for action, day in (("rm -rf", DAY), ("logs", "x; ls"), ("logs", "2026-01-01")):
        out = ops_actions.start(action, day, now=AFTER, folder=str(reports))
        assert out["status"] == 400
    assert started == []


def test_a_rerun_step_shows_its_latest_result_and_says_it_was_run_again(env):
    reports, _ = env
    actions.append_step(DAY, "backfill", "18:00:00", "18:07:00", 0, folder=str(reports))
    steps = {p["phase"]: p["steps"] for p in day_view(DAY, str(reports))["phases"]}["close-day"]
    assert steps == [
        {
            "step": "backfill",
            "started": "18:00:00",
            "ended": "18:07:00",
            "exit": 0,
            "reran": True,
        }
    ]
