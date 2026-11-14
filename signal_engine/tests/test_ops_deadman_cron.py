"""Dead-man content checks and the cron audit (every OpenAlgo line, old and new dead-man time)."""

from datetime import datetime

import pytest

from signal_engine.ops import automation, jobs, model, verdict
from signal_engine.timeutils import IST

DAY = "2026-11-12"  # a Thursday
LATE = datetime(2026, 11, 12, 21, 40, tzinfo=IST)
pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture(autouse=True)
def _calendar(tmp_path, monkeypatch):
    monkeypatch.setattr(automation, "DB", str(tmp_path / "no-such.db"))  # unreadable: trading day


def _phase(folder, phase, *, exit_overrides=None, skip=(), manifest="completed", reports=True):
    exit_overrides = exit_overrides or {}
    rows = [(n, exit_overrides.get(n, 0)) for n in model.CRITICAL_STEPS[phase] if n not in skip]
    if phase == "close-day":
        rows.insert(-1, ("review", exit_overrides.get("review", 0)))
    (folder / f"{phase}-{DAY}-status.tsv").write_text(
        "step\tstarted\tended\texit\n" + "".join(f"{n}\t15:36:00\t15:36:09\t{c}\n" for n, c in rows)
    )
    if manifest:
        model.manifest_start(
            phase, DAY, pid=1_999_999_999 if manifest == "dead" else 1, folder=str(folder)
        )
        for n, c in rows:
            model.manifest_step(phase, DAY, n, "15:36:00", "15:36:09", c, str(folder))
        if manifest in ("completed", "failed", "timed_out", "interrupted"):
            model.manifest_finish(
                phase, DAY, "auto" if manifest == "completed" else manifest, folder=str(folder)
            )
    if reports:
        for n in automation.PHASE_REPORTS[phase]:
            model.write_phase(n, DAY, [model.Check("a", model.OK, "x")], "s", folder=str(folder))
    if phase == "close-day":
        (folder / f"eod-{DAY}-claude-review.md").write_text(
            '```close-day-verdict\n{"verdict": "AMBER", "line": "Net 1; watch."}\n```\n'
            "```bug-tracker\n[]\n```\n"
        )


def _judge(folder, phase):
    return automation.judge_phase(phase, DAY, str(folder), now=LATE)


# ---- the four states ---------------------------------------------------------------------------


def test_healthy_close_day(tmp_path):
    _phase(tmp_path, "close-day")
    got = _judge(tmp_path, "close-day")
    assert got["state"] == "healthy", got
    assert automation.deadman("close", DAY, folder=str(tmp_path), now=LATE) is None


def test_missing_phase_has_no_manifest_and_no_status(tmp_path):
    got = _judge(tmp_path, "close-day")
    assert got["state"] == "missing"
    text = automation.deadman("close", DAY, folder=str(tmp_path), now=LATE)
    assert "did not run today (missing)" in text
    assert "scheduler may have stopped" in text


def test_header_only_status_file_is_incomplete_not_healthy(tmp_path):
    (tmp_path / f"open-day-{DAY}-status.tsv").write_text("step\tstarted\tended\texit\n")
    got = _judge(tmp_path, "open-day")
    assert got["state"] == "incomplete"
    assert "no step was recorded" in got["reasons"]


def test_interrupted_manifest_is_incomplete(tmp_path):
    _phase(tmp_path, "close-day", manifest="interrupted")
    got = _judge(tmp_path, "close-day")
    assert got["state"] == "incomplete"
    assert any("interrupted" in r for r in got["reasons"])


def test_running_manifest_with_a_dead_pid_is_incomplete(tmp_path):
    _phase(tmp_path, "close-day", manifest="dead")
    assert _judge(tmp_path, "close-day")["state"] == "incomplete"


def test_failed_manifest_is_failed(tmp_path):
    _phase(tmp_path, "close-day", exit_overrides={"backfill": 1}, manifest="completed")
    got = _judge(tmp_path, "close-day")
    assert got["state"] == "failed"
    assert "step backfill failed" in got["reasons"]
    assert "failed" in automation.deadman("close", DAY, folder=str(tmp_path), now=LATE)


def test_missing_critical_step_is_incomplete_and_named(tmp_path):
    _phase(tmp_path, "close-day", skip=("track",))
    got = _judge(tmp_path, "close-day")
    assert got["state"] == "incomplete"
    assert "step track has no record" in got["reasons"]


@pytest.mark.parametrize("phase", ["open-day", "liveness"])
def test_morning_phase_missing_notify_is_incomplete(tmp_path, phase):
    _phase(tmp_path, phase, skip=("notify",))
    assert "step notify has no record" in _judge(tmp_path, phase)["reasons"]


def test_red_verdict_is_failed_even_when_every_step_ran(tmp_path):
    _phase(tmp_path, "open-day")
    model.write_phase(
        "open-day", DAY, [model.Check("services", model.FAIL, "down")], "s", folder=str(tmp_path)
    )
    got = _judge(tmp_path, "open-day")
    assert got["state"] == "failed"
    assert "the open-day check reported RED" in got["reasons"]


def test_the_check_step_exiting_one_with_a_red_report_is_not_a_crash(tmp_path):
    _phase(tmp_path, "open-day", exit_overrides={"check": 1})
    model.write_phase(
        "open-day", DAY, [model.Check("services", model.FAIL, "down")], "s", folder=str(tmp_path)
    )
    got = _judge(tmp_path, "open-day")
    assert "step check failed" not in got["reasons"]
    assert got["state"] == "failed"  # because the verdict is RED


def test_report_missing_or_unparseable_is_incomplete(tmp_path):
    _phase(tmp_path, "close-day")
    (tmp_path / f"jobs-{DAY}.json").unlink()
    (tmp_path / f"logs-{DAY}.json").write_text("{ not json")
    got = _judge(tmp_path, "close-day")
    assert got["state"] == "incomplete"
    assert "the jobs report is missing" in got["reasons"]
    assert "the logs report cannot be read" in got["reasons"]


def test_close_day_red_review_verdict_is_failed(tmp_path):
    _phase(tmp_path, "close-day")
    (tmp_path / f"eod-{DAY}-claude-review.md").write_text(
        '```close-day-verdict\n{"verdict": "RED", "line": "Loss."}\n```\n```bug-tracker\n[]\n```\n'
    )
    assert _judge(tmp_path, "close-day")["state"] == "failed"


def test_close_day_without_a_review_verdict_is_incomplete_with_the_review_text(tmp_path):
    _phase(tmp_path, "close-day")
    (tmp_path / f"eod-{DAY}-claude-review.md").write_text("You've hit your 5-hour limit\n")
    got = _judge(tmp_path, "close-day")
    assert got["state"] == "incomplete"
    assert any("usage limit" in r for r in got["reasons"])


def test_a_review_exit_code_alone_does_not_make_close_day_failed(tmp_path):
    _phase(tmp_path, "close-day", exit_overrides={"review": 1})
    assert _judge(tmp_path, "close-day")["state"] == "healthy"


def test_a_review_only_timeout_is_ignored_once_a_later_attempt_holds_a_verdict(tmp_path):
    # The review hit the 15:58 deadline (exit 124); the catch-up then wrote a valid verdict.
    _phase(tmp_path, "close-day", manifest="timed_out", exit_overrides={"review": 124})
    got = _judge(tmp_path, "close-day")
    assert got["state"] == "healthy", got


def test_a_review_only_timeout_without_a_verdict_is_still_not_healthy(tmp_path):
    _phase(tmp_path, "close-day", manifest="timed_out", exit_overrides={"review": 124})
    (tmp_path / f"eod-{DAY}-claude-review.md").write_text("stopped\n")
    assert _judge(tmp_path, "close-day")["state"] != "healthy"


def test_a_timeout_in_any_other_step_still_counts_as_failed(tmp_path):
    step = next(n for n in model.CRITICAL_STEPS["close-day"] if n != "review")
    _phase(tmp_path, "close-day", manifest="timed_out", exit_overrides={step: 124})
    got = _judge(tmp_path, "close-day")
    assert got["state"] == "failed", got
    assert "timed_out" in " ".join(got["reasons"])


def test_a_timeout_in_the_review_and_another_step_still_counts_as_failed(tmp_path):
    step = next(n for n in model.CRITICAL_STEPS["close-day"] if n != "review")
    _phase(tmp_path, "close-day", manifest="timed_out", exit_overrides={"review": 124, step: 124})
    assert _judge(tmp_path, "close-day")["state"] == "failed"


def test_legacy_day_without_a_manifest_falls_back_to_the_status_file(tmp_path):
    _phase(tmp_path, "close-day", manifest=None)
    assert _judge(tmp_path, "close-day")["state"] == "healthy"
    _phase(tmp_path, "close-day", manifest=None, exit_overrides={"eod": 2})
    assert _judge(tmp_path, "close-day")["state"] == "failed"


def test_a_phase_not_yet_due_is_not_judged(tmp_path):
    early = datetime(2026, 11, 12, 9, 15, tzinfo=IST)  # 09:12 due, margin 10 min
    assert automation.deadman_report("morning", DAY, folder=str(tmp_path), now=early) == []
    mid = datetime(2026, 11, 12, 9, 25, tzinfo=IST)
    report = automation.deadman_report("morning", DAY, folder=str(tmp_path), now=mid)
    assert [r["phase"] for r in report] == ["open-day"]  # liveness (09:27) is not yet due


def test_alert_text_separates_the_three_bad_states(tmp_path):
    _phase(tmp_path, "open-day", skip=("notify",))  # incomplete
    _phase(tmp_path, "liveness", exit_overrides={"notify": 3})  # failed
    text = automation.deadman("morning", DAY, folder=str(tmp_path), now=LATE)
    assert "did not finish (incomplete)" in text
    assert "failed:" in text
    tmp2 = tmp_path / "b"
    tmp2.mkdir()
    assert "(missing)" in automation.deadman("morning", DAY, folder=str(tmp2), now=LATE)
    assert (
        automation.worst_state(
            automation.deadman_report("morning", DAY, folder=str(tmp_path), now=LATE)
        )
        == "failed"
    )


# ---- critical steps match the real scripts ------------------------------------------------------


def test_critical_steps_are_steps_the_scripts_really_run():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "signal_engine"
    close = (root / "analysis/close_day.sh").read_text()
    morning = (root / "ops/run_phase.sh").read_text()
    for name in model.CRITICAL_STEPS["close-day"]:
        assert f"step {name} " in close, name
    for name in model.CRITICAL_STEPS["open-day"]:
        assert f"step {name} " in morning, name


# ---- cron audit --------------------------------------------------------------------------------

BASE = "/home/anand/github/openalgo"
LOG = ">> /x.log 2>&1"
LINES = {
    "retry": f"56 8 * * 1-5 {BASE}/signal_engine/ops/run_upkeep.sh retry-failed {LOG}",
    "open": f"12 9 * * 1-5 {BASE}/signal_engine/ops/run_phase.sh open-day {LOG}",
    "live": f"27 9 * * 1-5 {BASE}/signal_engine/ops/run_phase.sh liveness {LOG}",
    "dm_morning": f"40 9 * * 1-5 {BASE}/signal_engine/ops/run_upkeep.sh deadman morning {LOG}",
    "close": f"36 15 * * 1-5 {BASE}/signal_engine/analysis/close_day.sh {LOG}",
    "dm_close": f"20 16 * * 1-5 {BASE}/signal_engine/ops/run_upkeep.sh deadman close {LOG}",
    "autofix": f"30 16 * * 1-5 {BASE}/signal_engine/analysis/autofix.sh {LOG}",
    "catch1": f"35 18 * * 1-5 {BASE}/signal_engine/analysis/review_catchup.sh {LOG}",
    "catch2": f"5 21 * * 1-5 {BASE}/signal_engine/analysis/review_catchup.sh {LOG}",
}
OTHER = "0 15 * * * /home/anand/github/gold-monitor/run_daily.sh\n# a comment\nMAILTO=x\n"


def _cron(**changes):
    lines = {**LINES, **changes}
    return OTHER + "\n".join(v for v in lines.values() if v) + "\n"


def test_the_audit_covers_all_nine_lines():
    assert len(jobs.CRON_LINES) == 9 == len(LINES)
    problems, notes = jobs.audit_cron(_cron())
    assert problems == []
    assert any("deadman close" in n for n in notes)  # still on the old time: noted, not RED


def test_the_old_deadman_time_passes_and_so_does_the_new_one():
    for time_fields in ("20 16", "30 21", "35 21"):
        line = f"{time_fields} * * 1-5 {BASE}/signal_engine/ops/run_upkeep.sh deadman close {LOG}"
        problems, _ = jobs.audit_cron(_cron(dm_close=line))
        assert problems == [], time_fields


def test_a_wrong_deadman_time_is_a_problem():
    line = f"50 22 * * 1-5 {BASE}/signal_engine/ops/run_upkeep.sh deadman close {LOG}"
    problems, _ = jobs.audit_cron(_cron(dm_close=line))
    assert len(problems) == 1 and "deadman close" in problems[0] and "22:50" in problems[0]


@pytest.mark.parametrize("key", list(LINES))
def test_each_missing_line_is_found(key):
    problems, _ = jobs.audit_cron(_cron(**{key: ""}))
    assert len(problems) == 1, (key, problems)
    # a catch-up line shares its script with the other one, so its message names the other time
    assert "no line found" in problems[0] or key.startswith("catch")


def test_a_moved_line_is_found():
    problems, _ = jobs.audit_cron(
        _cron(autofix=f"45 16 * * 1-5 {BASE}/signal_engine/analysis/autofix.sh {LOG}")
    )
    assert problems and "autofix" in problems[0] and "16:45" in problems[0]


def test_a_weekday_field_that_is_not_1_5_is_found():
    problems, _ = jobs.audit_cron(
        _cron(open=f"12 9 * * * {BASE}/signal_engine/ops/run_phase.sh open-day {LOG}")
    )
    assert problems and "weekday" in problems[0]


def test_both_catchup_lines_are_needed():
    problems, _ = jobs.audit_cron(_cron(catch2=""))
    assert problems == ["review catch-up 2: expected 21:05, no line found"]


def test_a_commented_line_does_not_count():
    problems, _ = jobs.audit_cron(_cron(autofix="# " + LINES["autofix"]))
    assert len(problems) == 1


def test_the_audit_check_is_red_only_for_problems(monkeypatch):
    class Done:
        stdout = _cron()

    monkeypatch.setattr(jobs.subprocess, "run", lambda *a, **k: Done)
    check = jobs._cron_check()
    assert check.status == "OK" and "9 cron lines" in check.evidence
    Done.stdout = _cron(open="")
    assert jobs._cron_check().status == "FAIL"


def test_the_installed_crontab_text_in_the_scripts_matches_the_audit():
    """The cron lines documented in the scripts' headers are accepted by the audit."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "signal_engine"
    text = ""
    for rel in (
        "ops/run_phase.sh",
        "ops/run_upkeep.sh",
        "analysis/close_day.sh",
        "analysis/review_catchup.sh",
    ):
        text += (
            "\n".join(
                ln[1:].strip()
                for ln in (root / rel).read_text().splitlines()
                if ln.startswith("#   ") and "* * 1-5" in ln
            )
            + "\n"
        )
    text += f"30 16 * * 1-5 {BASE}/signal_engine/analysis/autofix.sh x\n"
    problems, _ = jobs.audit_cron(text)
    assert problems == []


# ---- the dead-man command line: recovery only after something was checked ------------------


def _run_deadman(monkeypatch, phase, day, now):
    from signal_engine.ops import __main__ as cli
    from signal_engine.ops import actions, alerts

    calls = []
    monkeypatch.setattr(actions, "now_ist", lambda: now)
    monkeypatch.setattr(alerts, "recover", lambda **kw: calls.append(kw) or True)
    monkeypatch.setattr(alerts, "deliver", lambda *a, **kw: calls.append(("deliver", a)) or True)
    rc = cli.main(["deadman", phase, "--day", day])
    return rc, calls


def test_deadman_on_a_weekend_sends_and_recovers_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(model, "REPORTS", str(tmp_path))
    monkeypatch.setattr(automation, "REPORTS", str(tmp_path))
    rc, calls = _run_deadman(
        monkeypatch, "close", "2026-11-14", datetime(2026, 11, 14, 21, 40, tzinfo=IST)
    )
    assert (rc, calls) == (0, [])


def test_deadman_on_a_holiday_sends_and_recovers_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(automation, "is_trading_day", lambda day, **kw: False)
    rc, calls = _run_deadman(monkeypatch, "close", DAY, LATE)
    assert (rc, calls) == (0, [])


def test_deadman_before_the_phase_is_due_sends_and_recovers_nothing(tmp_path, monkeypatch):
    early = datetime(2026, 11, 12, 9, 5, tzinfo=IST)
    rc, calls = _run_deadman(monkeypatch, "morning", DAY, early)
    assert (rc, calls) == (0, [])


def test_deadman_recovers_when_every_due_checkpoint_is_healthy(tmp_path, monkeypatch):
    monkeypatch.setattr(model, "REPORTS", str(tmp_path))
    monkeypatch.setattr(automation, "REPORTS", str(tmp_path))
    monkeypatch.setattr(verdict, "REPORTS", str(tmp_path))
    _phase(tmp_path, "close-day")
    rc, calls = _run_deadman(monkeypatch, "close", DAY, LATE)
    assert rc == 0
    assert calls == [{"kind": "deadman", "phase": "close"}]
