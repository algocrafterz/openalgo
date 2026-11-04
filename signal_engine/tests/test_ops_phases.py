"""signal_engine/ops: verdicts, log signatures, schedule parsing and the one Telegram line."""

import datetime
import json
import os
from collections import Counter

from signal_engine.ops import jobs, logs, logscan, verdict
from signal_engine.ops.model import FAIL, OK, WARN, Check, read_phase, write_phase


def test_verdict_is_the_worst_check(tmp_path):
    write_phase(
        "open-day",
        "2026-11-02",
        [Check("a", OK, "x"), Check("b", WARN, "y")],
        "s",
        folder=str(tmp_path),
    )
    assert read_phase("open-day", "2026-11-02", folder=str(tmp_path))["verdict"] == "AMBER"
    write_phase(
        "open-day",
        "2026-11-02",
        [Check("a", OK, "x"), Check("b", FAIL, "y")],
        "s",
        folder=str(tmp_path),
    )
    assert read_phase("open-day", "2026-11-02", folder=str(tmp_path))["verdict"] == "RED"


def test_signature_masks_numbers_and_the_symbol():
    a = logs.signature("signal_engine.executor", "Timeout sending order for KAYNES", "KAYNES")
    b = logs.signature("signal_engine.executor", "Timeout sending order for JSWENERGY", "JSWENERGY")
    assert a == b == "signal_engine.executor: Timeout sending order for <SYM>"
    assert logs.signature("x", "order 26103036923667 at 502.65") == "x: order # at #"


def test_new_and_rising_against_the_previous_sessions():
    today = Counter({"old": 3, "spike": 12, "fresh": 1})
    history = [Counter({"old": 3, "spike": 2})] * 5
    assert logscan.classify(today, history) == {
        "old": "recurring",
        "spike": "RISING",
        "fresh": "NEW",
    }


def test_schtasks_rows_are_read_by_header():
    text = (
        '"HostName","TaskName","Next Run Time","Status","Logon Mode","Last Run Time","Last Result"\r\n'
        '"GENIE","\\openAlgoAutoStart","02-11-2026 08:50:00","Ready","Interactive","30-10-2026 08:50:00","0"\r\n'
    )
    row = jobs.parse_schtasks(text)["openAlgoAutoStart"]
    assert jobs.task_check("openAlgoAutoStart", "08:50", row, "2026-10-30").status == OK
    assert jobs.task_check("openAlgoAutoStart", "08:50", row, "2026-11-02").status == FAIL
    assert jobs.task_check("missing", "08:50", None, "2026-11-02").status == FAIL


def test_morning_phases_send_only_when_red(tmp_path):
    folder = str(tmp_path)
    write_phase("open-day", "2026-11-02", [Check("ports", WARN, "slow")], "s", folder=folder)
    assert verdict.message("open-day", "2026-11-02", folder) is None
    write_phase(
        "open-day", "2026-11-02", [Check("broker session", FAIL, "no funds")], "s", folder=folder
    )
    assert "RED. Failed: broker session" in verdict.message("open-day", "2026-11-02", folder)
    assert "did not produce a result" in verdict.message("liveness", "2026-11-02", folder)


def test_close_day_sends_the_review_line_every_day(tmp_path):
    folder = str(tmp_path)
    assert "AMBER" in verdict.message("close-day", "2026-11-02", folder)  # no review at all
    report = tmp_path / "eod-2026-11-02-claude-review.md"
    block = json.dumps(
        {"verdict": "GREEN", "line": "Net +1,200 after charges; nothing needs attention."}
    )
    report.write_text(f"review\n```close-day-verdict\n{block}\n```\n```bug-tracker\n[]\n```\n")
    (tmp_path / "close-day-2026-11-02-status.tsv").write_text(
        "step\tstarted\tended\texit\neod\t1\t2\t0\nlogs\t2\t3\t1\n"
    )
    text = verdict.message("close-day", "2026-11-02", folder)
    assert text.startswith("Close-day 2026-11-02: GREEN. Net +1,200")
    assert "Steps failed: logs." in text


def test_every_check_gets_a_plain_sentence_that_names_its_subject(tmp_path):
    from signal_engine.ops.plain import plain_for

    assert plain_for("clock (NTP)", OK) == "The PC clock is correct, so broker login codes work."
    assert plain_for("phase liveness", FAIL) == "The after-the-open checkpoint had a failed step or did not run."
    assert plain_for("task openAlgoAutoStart", FAIL).startswith("The Windows task openAlgoAutoStart")
    assert plain_for("anything", "NOT VERIFIED") == "Could not be checked this time."
    write_phase("open-day", "2026-11-02", [Check("disk", FAIL, "2.1 GB free")], "s", folder=str(tmp_path))
    data = read_phase("open-day", "2026-11-02", folder=str(tmp_path))
    assert data["checks"][0]["plain"] == "The disk is nearly full."
    report = (tmp_path / "open-day-2026-11-02.md").read_text()
    assert report.startswith("# Before the open, 2026-11-02: action needed (RED)")
    assert "1 of 1 checks need a look" in report


# --- BUG-013: a RED morning check is a report, not a failed job -------------------------------


def _status(folder, phase, day, rows):
    lines = "step\tstarted\tended\texit\n" + "".join(f"{r}\n" for r in rows)
    (folder / f"{phase}-{day}-status.tsv").write_text(lines)


def test_schtasks_is_found_by_full_path_when_path_has_no_windows_dirs(monkeypatch, tmp_path):
    fake = tmp_path / "schtasks.exe"
    fake.write_text("")
    monkeypatch.setattr(jobs.shutil, "which", lambda name: None)
    monkeypatch.setattr(jobs, "WINDOWS_SCHTASKS", str(fake))
    assert jobs.schtasks_exe() == str(fake)
    monkeypatch.setattr(jobs.shutil, "which", lambda name: "/usr/bin/schtasks.exe")
    assert jobs.schtasks_exe() == "/usr/bin/schtasks.exe"
    monkeypatch.setattr(jobs.shutil, "which", lambda name: None)
    monkeypatch.setattr(jobs, "WINDOWS_SCHTASKS", str(tmp_path / "absent.exe"))
    assert jobs.schtasks_exe() == "schtasks.exe"  # still unavailable: reported NOT VERIFIED


def test_a_red_morning_check_is_not_counted_as_a_failed_job(tmp_path):
    day = "2026-11-03"
    write_phase("open-day", day, [Check("yesterday", FAIL, "x")], "s", folder=str(tmp_path))
    _status(tmp_path, "open-day", day, ["check\t00:00:00\t00:00:01\t1", "notify\t00:00:01\t00:00:02\t0"])
    checks = {c.name: c for c in jobs._phase_checks(day, folder=str(tmp_path))}
    assert checks["phase open-day"].status == WARN
    assert "reported RED" in checks["phase open-day"].evidence


def test_a_crashed_morning_check_still_fails_the_job_audit(tmp_path):
    day = "2026-11-03"
    # exit 1 with no report: it crashed before writing one
    _status(tmp_path, "open-day", day, ["check\t00:00:00\t00:00:01\t1"])
    assert {c.name: c for c in jobs._phase_checks(day, folder=str(tmp_path))}["phase open-day"].status == FAIL
    # exit 2 is never a RED verdict, even when a report exists
    write_phase("liveness", day, [Check("x", FAIL, "x")], "s", folder=str(tmp_path))
    _status(tmp_path, "liveness", day, ["check\t00:00:00\t00:00:01\t2"])
    assert {c.name: c for c in jobs._phase_checks(day, folder=str(tmp_path))}["phase liveness"].status == FAIL
    # a RED report older than the failed run is a stale leftover, not this run's result
    write_phase("liveness", day, [Check("x", FAIL, "x")], "s", folder=str(tmp_path))
    old = datetime.datetime(2026, 11, 3, 12, 0, 0).timestamp()
    os.utime(tmp_path / f"liveness-{day}.json", (old, old))
    _status(tmp_path, "liveness", day, ["check\t23:59:00\t23:59:01\t1"])
    assert {c.name: c for c in jobs._phase_checks(day, folder=str(tmp_path))}["phase liveness"].status == FAIL


def test_yesterdays_reported_red_jobs_step_does_not_keep_open_day_red(tmp_path, monkeypatch):
    from signal_engine.ops import readiness

    prev = "2026-11-03"
    monkeypatch.setattr(readiness, "REPORTS", str(tmp_path))
    monkeypatch.setattr(readiness, "_previous_weekday", lambda day: prev)
    write_phase("jobs", prev, [Check("phase open-day", FAIL, "x")], "s", folder=str(tmp_path))
    _status(tmp_path, "close-day", prev, ["eod\t00:00:00\t00:00:01\t0", "jobs\t00:00:01\t00:00:02\t1"])
    check = readiness._yesterday_close("2026-11-04")
    assert check.status == WARN and "jobs" in check.evidence
    # a real failure in the same file is still a FAIL
    _status(tmp_path, "close-day", prev, ["eod\t00:00:00\t00:00:01\t2", "jobs\t00:00:01\t00:00:02\t1"])
    check = readiness._yesterday_close("2026-11-04")
    assert check.status == FAIL and "eod" in check.evidence
    # a jobs step that exited 1 with no report behind it is a crash
    (tmp_path / f"jobs-{prev}.json").unlink()
    _status(tmp_path, "close-day", prev, ["jobs\t00:00:01\t00:00:02\t1"])
    assert readiness._yesterday_close("2026-11-04").status == FAIL


# --- BUG-013 extension: logs and value_areas report RED the same way jobs does ---------------


def test_a_red_logs_or_value_areas_step_is_a_report_not_a_failed_job(tmp_path, monkeypatch):
    from signal_engine.ops import readiness

    prev = "2026-11-04"
    write_phase("logs", prev, [Check("new error", FAIL, "x")], "s", folder=str(tmp_path))
    write_phase("value-areas", prev, [Check("coverage", FAIL, "x")], "s", folder=str(tmp_path))
    rows = ["logs\t00:00:00\t00:00:01\t1", "value_areas\t00:00:01\t00:00:02\t1"]
    _status(tmp_path, "close-day", prev, rows)
    for step in ("logs", "value_areas"):
        assert jobs.reported_red("close-day", step, "00:00:00", "1", prev, str(tmp_path))
    # backfill has no report and exits 1 on a real failure: still a failure
    assert not jobs.reported_red("close-day", "backfill", "00:00:00", "1", prev, str(tmp_path))
    monkeypatch.setattr(readiness, "REPORTS", str(tmp_path))
    monkeypatch.setattr(readiness, "_previous_weekday", lambda day: prev)
    _status(tmp_path, "close-day", prev, [*rows, "backfill\t00:00:02\t00:00:03\t1"])
    check = readiness._yesterday_close("2026-11-05")
    assert check.status == FAIL and "backfill" in check.evidence and "logs" not in check.evidence


# --- BUG-014: the health endpoint's three answers ---------------------------------------------


def _health_with(monkeypatch, body=None, error=None):
    import io
    import json as _json

    from signal_engine.ops import readiness

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(url, timeout):
        if error:
            raise error
        return _Response(_json.dumps(body).encode())

    monkeypatch.setattr(readiness.urllib.request, "urlopen", fake_urlopen)
    return readiness._health()


def test_health_pass_is_ok(monkeypatch):
    assert _health_with(monkeypatch, {"status": "pass"}).status == OK


def test_health_warn_is_a_warning_not_a_failure(monkeypatch):
    from signal_engine.ops.plain import plain_for

    check = _health_with(monkeypatch, {"status": "warn"})
    assert check.status == WARN and "status=warn" in check.evidence
    assert "not answering" not in plain_for(check.name, check.status)


def test_health_fail_503_and_no_answer_stay_failures(monkeypatch):
    import urllib.error

    assert _health_with(monkeypatch, {"status": "fail"}).status == FAIL
    assert _health_with(monkeypatch, {"status": "surprise"}).status == FAIL
    err = urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None)
    assert _health_with(monkeypatch, error=err).status == FAIL
    assert _health_with(monkeypatch, error=urllib.error.URLError("refused")).status == FAIL
