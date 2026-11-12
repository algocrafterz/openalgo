"""Run-again actions: what may start, the exact commands, and how a run is recorded."""

import json
import sys
from datetime import datetime

import pytest

from signal_engine.ops import actions
from signal_engine.timeutils import IST

DAY = "2026-11-11"
AFTER_CLOSE = datetime(2026, 11, 11, 16, 5, tzinfo=IST)
BEFORE_CLOSE = datetime(2026, 11, 11, 11, 0, tzinfo=IST)


@pytest.fixture
def env(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / f"close-day-{DAY}-status.tsv").write_text(
        "step\tstarted\tended\texit\nbackfill\t15:36:03\t17:56:08\t1\n"
    )
    monkeypatch.setattr(actions, "REPORTS", str(reports))
    monkeypatch.setattr(actions, "STATE_DIR", str(tmp_path / "state"))
    return reports


def test_every_action_has_a_fixed_command_and_no_other_name_is_accepted():
    for key in actions.ACTIONS:
        cmd = actions.command(key, DAY)
        assert cmd[0] in ("bash", sys.executable)
        assert all(isinstance(part, str) for part in cmd)
    for bad in ("rm", "../x", "backfill; ls", ""):
        with pytest.raises(KeyError):
            actions.command(bad, DAY)
    with pytest.raises(ValueError):
        actions.command("logs", "2026-11-11; ls")


def test_an_action_is_refused_before_close_today_but_allowed_after(env):
    assert "after 15:36" in actions.refusal("logs", DAY, now=BEFORE_CLOSE)
    assert actions.refusal("logs", DAY, now=AFTER_CLOSE) is None


def test_an_action_is_refused_for_unknown_old_future_or_unstarted_days(env):
    assert actions.refusal("nope", DAY, now=AFTER_CLOSE)
    assert actions.refusal("logs", "bad", now=AFTER_CLOSE)
    assert "did not start" in actions.refusal("logs", "2026-11-09", now=AFTER_CLOSE)
    (env / "close-day-2026-11-02-status.tsv").write_text("step\tstarted\tended\texit\n")
    assert "last 7 days" in actions.refusal("logs", "2026-11-02", now=AFTER_CLOSE)
    (env / "close-day-2026-11-13-status.tsv").write_text("step\tstarted\tended\texit\n")
    assert "not happened" in actions.refusal("logs", "2026-11-13", now=AFTER_CLOSE)


def test_the_review_is_refused_when_it_already_has_a_verdict(env):
    assert actions.refusal("review", DAY, now=AFTER_CLOSE) is None
    (env / f"eod-{DAY}-claude-review.md").write_text("x\n```close-day-verdict\n{}\n```\n")
    assert "already has a verdict" in actions.refusal("review", DAY, now=AFTER_CLOSE)


def _fake(monkeypatch, code, text="out"):
    monkeypatch.setattr(
        actions,
        "command",
        lambda action, day: [sys.executable, "-c", f"print({text!r}); raise SystemExit({code})"],
    )


def test_a_successful_run_is_recorded_in_state_and_the_status_file(env, monkeypatch):
    _fake(monkeypatch, 0)
    assert actions.run("backfill", DAY, "1762843500-backfill") == 0
    state = actions.read_state("1762843500-backfill")
    assert state["outcome"] == "ok" and state["exit"] == 0
    assert "out" in state["tail"]
    rows = (env / f"close-day-{DAY}-status.tsv").read_text().splitlines()
    names = [r.split("\t")[0] for r in rows[1:]]
    # the failed row is kept; the page shows the latest of each step. The backfill is followed by
    # the value areas it feeds and the scheduled-jobs audit that reads every step.
    assert names == ["backfill", "backfill", "value_areas", "jobs"]
    assert rows[2].endswith("\t0")
    assert "Also done" in state["message"]


def test_follow_ups_run_only_after_success_and_never_for_the_audit_itself(env, monkeypatch):
    _fake(monkeypatch, 3)
    actions.run("backfill", DAY, "1762843508-backfill")
    names = [
        r.split("\t")[0] for r in (env / f"close-day-{DAY}-status.tsv").read_text().splitlines()
    ]
    assert names == ["step", "backfill", "backfill"]  # header, the old failure, the new failure
    _fake(monkeypatch, 0)
    actions.run("jobs", DAY, "1762843509-jobs")
    names = [
        r.split("\t")[0] for r in (env / f"close-day-{DAY}-status.tsv").read_text().splitlines()
    ]
    assert names.count("jobs") == 1  # the audit does not chain another audit


def test_a_failed_run_is_reported_as_failed(env, monkeypatch):
    _fake(monkeypatch, 3)
    assert actions.run("backfill", DAY, "1762843501-backfill") == 1
    state = actions.read_state("1762843501-backfill")
    assert state["outcome"] == "failed" and state["exit"] == 3


def test_a_check_that_reports_red_by_design_still_counts_as_run(env, monkeypatch):
    _fake(monkeypatch, 1)
    assert actions.run("jobs", DAY, "1762843502-jobs") == 0
    assert actions.read_state("1762843502-jobs")["outcome"] == "ok"
    _fake(monkeypatch, 1)
    assert actions.run("backfill", DAY, "1762843503-backfill") == 1  # exit 1 is a failure here


def test_a_failed_health_report_never_replaces_the_good_one(env, monkeypatch):
    good = env / f"execution-health-{DAY}.md"
    good.write_text("good report")
    _fake(monkeypatch, 2, text="partial")
    actions.run("execution_health", DAY, "1762843504-execution_health")
    assert good.read_text() == "good report"
    assert not (env / f"execution-health-{DAY}.md.part").exists()
    _fake(monkeypatch, 0, text="new report")
    actions.run("execution_health", DAY, "1762843505-execution_health")
    assert "new report" in good.read_text()


def test_a_run_whose_process_died_without_finishing_reads_as_failed(env):
    actions.write_state(
        "1762843506-eod",
        {"id": "1762843506-eod", "action": "eod", "day": DAY, "pid": 2**22 + 5, "started": "t"},
    )
    state = actions.read_state("1762843506-eod")
    assert state["outcome"] == "failed" and "stopped before" in state["message"]


def test_running_reports_only_a_live_unfinished_run(env):
    import os

    assert actions.running() is None
    run_id = f"{int(datetime.now().timestamp())}-eod"
    actions.write_state(run_id, {"id": run_id, "action": "eod", "day": DAY, "pid": os.getpid()})
    assert actions.running()["id"] == run_id
    actions.write_state(
        run_id, {"id": run_id, "pid": os.getpid(), "ended": "x", "outcome": "ok", "exit": 0}
    )
    assert actions.running() is None


def test_state_ids_are_whitelisted(env):
    assert actions.read_state("../../etc/passwd") is None
    assert actions.read_state("x") is None
    assert json.dumps(actions.latest_run()) == "null"


def test_old_run_files_are_pruned_and_recent_ones_kept(env):
    import os
    import time

    actions.write_state("1762843507-eod", {"id": "1762843507-eod"})
    old = os.path.join(actions.STATE_DIR, "1700000000-eod.json")
    with open(old, "w") as handle:
        handle.write("{}")
    os.utime(old, (time.time() - 9 * 86400,) * 2)
    assert actions.prune() == 1
    assert not os.path.exists(old)
    assert os.path.exists(os.path.join(actions.STATE_DIR, "1762843507-eod.json"))
