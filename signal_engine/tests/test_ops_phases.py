"""signal_engine/ops: verdicts, log signatures, schedule parsing and the one Telegram line."""

import json
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
