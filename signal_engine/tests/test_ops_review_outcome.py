"""The close-day review is judged by what it produced, never by its exit code alone."""

import json

import pytest

from signal_engine.bugs import store
from signal_engine.ops import autotrack, jobs, verdict
from signal_engine.ops.model import OK, Check, write_phase

pytestmark = pytest.mark.filterwarnings("ignore")

DAY = "2026-11-13"
TRACKER = "```bug-tracker\n[]\n```\n"


def _block(v="AMBER", line="Net 1,200 after charges; one thing to watch."):
    return f"text\n```close-day-verdict\n{json.dumps({'verdict': v, 'line': line})}\n```\n"


def _status(folder, review_exit, extra=""):
    rows = [
        ("eod", 0),
        ("backfill", 0),
        ("execution_health", 0),
        ("logs", 0),
        ("value_areas", 0),
        ("jobs", 0),
        ("track", 0),
        ("review", review_exit),
        ("notify", 0),
    ]
    text = "step\tstarted\tended\texit\n" + "".join(
        f"{n}\t15:40:00\t15:41:00\t{c}\n" for n, c in rows
    )
    (folder / f"close-day-{DAY}-status.tsv").write_text(text + extra)


def _report(folder, body):
    (folder / f"eod-{DAY}-claude-review.md").write_text(body)


def _attempt(folder, name, out="", err="", code=None):
    d = folder / "review-attempts"
    d.mkdir(exist_ok=True)
    (d / f"{name}.out").write_text(out)
    (d / f"{name}.err").write_text(err)
    if code is not None:
        (d / f"{name}.exit").write_text(f"{code}\n")
    (d / f"{DAY}-latest.txt").write_text(name + "\n")


# ---- the four outcomes ------------------------------------------------------------------------


def test_complete_report_with_nonzero_exit_follows_the_verdict(tmp_path):
    _status(tmp_path, 1)
    _report(tmp_path, _block("AMBER") + TRACKER)
    got = verdict.review_outcome(DAY, str(tmp_path))
    assert got["outcome"] == "verdict"
    assert got["verdict"] == "AMBER"
    assert got["exit"] == 1
    assert "diagnostic" in got["note"]
    text = verdict.message("close-day", DAY, str(tmp_path))
    assert "AMBER" in text
    assert "Steps failed: review" not in text
    assert "exit code" not in text  # diagnostic: on the workflow page, not in the trader line


def test_a_green_verdict_with_a_nonzero_exit_stays_green(tmp_path):
    _status(tmp_path, 1)
    _report(tmp_path, _block("GREEN", "Net 900; nothing needs attention.") + TRACKER)
    got = verdict.review_outcome(DAY, str(tmp_path))
    assert (got["outcome"], got["verdict"]) == ("verdict", "GREEN")


def test_no_report_is_no_verdict_and_never_green(tmp_path):
    _status(tmp_path, 0)
    got = verdict.review_outcome(DAY, str(tmp_path))
    assert got["outcome"] == "no_verdict"
    assert got["verdict"] == "AMBER"
    assert "no verdict" in got["text"]


def test_a_report_without_a_block_is_no_verdict_even_with_exit_zero(tmp_path):
    _status(tmp_path, 0)
    _report(tmp_path, "The review wrote prose and stopped.\n" + TRACKER)
    got = verdict.review_outcome(DAY, str(tmp_path))
    assert (got["outcome"], got["verdict"]) == ("no_verdict", "AMBER")


def test_a_block_with_a_bad_value_is_no_verdict(tmp_path):
    _status(tmp_path, 0)
    _report(tmp_path, '```close-day-verdict\n{"verdict": "FINE", "line": "x"}\n```\n')
    assert verdict.review_outcome(DAY, str(tmp_path))["outcome"] == "no_verdict"


def test_green_with_exit_zero_but_incomplete_evidence_is_lowered_to_amber(tmp_path):
    _status(tmp_path, 0)
    # no bug-tracker block: the review did not finish its last section
    _report(tmp_path, _block("GREEN", "Net 900; nothing needs attention."))
    got = verdict.review_outcome(DAY, str(tmp_path))
    assert (got["outcome"], got["verdict"], got["cause"]) == ("verdict", "AMBER", "incomplete")
    assert "not GREEN" in got["line"]


def test_green_on_a_report_stopped_at_the_deadline_is_lowered(tmp_path):
    _status(tmp_path, 124)
    _report(
        tmp_path,
        _block("GREEN") + TRACKER + "\n> Review stopped at the 15:58 deadline: incomplete.\n",
    )
    got = verdict.review_outcome(DAY, str(tmp_path))
    assert got["verdict"] == "AMBER"


def test_green_with_a_failed_critical_step_is_lowered(tmp_path):
    _status(tmp_path, 0, extra="")
    text = (
        (tmp_path / f"close-day-{DAY}-status.tsv")
        .read_text()
        .replace("eod\t15:40:00\t15:41:00\t0", "eod\t15:40:00\t15:41:00\t2")
    )
    (tmp_path / f"close-day-{DAY}-status.tsv").write_text(text)
    _report(tmp_path, _block("GREEN", "Net 900; nothing needs attention.") + TRACKER)
    got = verdict.review_outcome(DAY, str(tmp_path))
    assert (got["verdict"], got["cause"]) == ("AMBER", "critical_step")
    assert "eod" in got["line"]


def test_usage_limit_text_is_reported_as_cannot_run(tmp_path):
    _status(tmp_path, 1)
    _report(
        tmp_path,
        "Warning: this sign-in ends between 2026-11-13 and 2026-11-16.\n"
        "You've hit your 5-hour limit · resets 6:30pm (Asia/Kolkata)\n\n"
        "## Bug tracker updates (applied by eod_claude_review.sh)\n\n- refused\n",
    )
    got = verdict.review_outcome(DAY, str(tmp_path))
    assert (got["outcome"], got["cause"], got["verdict"]) == (
        "limit_or_auth",
        "usage_limit",
        "AMBER",
    )
    assert "usage limit" in got["text"]
    assert "18:35" in got["text"]  # the next action


def test_sign_in_expiry_text_is_reported_as_cannot_run(tmp_path):
    _status(tmp_path, 1)
    _attempt(tmp_path, f"{DAY}-154500", out="Not logged in. Please run /login", code=1)
    got = verdict.review_outcome(DAY, str(tmp_path))
    assert (got["outcome"], got["cause"]) == ("limit_or_auth", "auth")
    assert "claude auth login" in got["text"]


def test_the_healthy_sign_in_warning_alone_is_not_an_auth_failure(tmp_path):
    # The warning that a working run prints must not match: only a failed run is tested at all,
    # and the warning has no failure phrase.
    _status(tmp_path, 1)
    _report(
        tmp_path,
        "Warning: this sign-in ends between 2026-11-13 and 2026-11-16 (in 2 days at the earliest).\n",
    )
    got = verdict.review_outcome(DAY, str(tmp_path))
    assert got["outcome"] == "failed"


def test_nonzero_exit_without_a_verdict_or_a_known_text_is_a_genuine_failure(tmp_path):
    _status(tmp_path, 1)
    _attempt(tmp_path, f"{DAY}-154500", out="", err="Segmentation fault", code=1)
    got = verdict.review_outcome(DAY, str(tmp_path))
    assert got["outcome"] == "failed"
    assert "review-attempts" in got["text"]  # where to look
    assert "failed" in got["text"]


def test_a_long_output_is_never_read_as_a_limit_failure(tmp_path):
    _status(tmp_path, 1)
    _attempt(tmp_path, f"{DAY}-154500", out="x" * 5000 + " you hit your limit", code=1)
    assert verdict.review_outcome(DAY, str(tmp_path))["outcome"] == "failed"


def test_deadline_without_a_verdict_says_it_ran_out_of_time(tmp_path):
    _status(tmp_path, 124)
    _report(tmp_path, "partial text\n\n> Review stopped at the 15:58 deadline: incomplete.\n")
    got = verdict.review_outcome(DAY, str(tmp_path))
    assert (got["outcome"], got["cause"]) == ("no_verdict", "deadline")
    assert "ran out of time" in got["text"]


def test_the_latest_attempt_exit_beats_a_stale_status_row(tmp_path):
    # status.tsv still says review exit 1 (the 15:45 run); the 18:35 attempt exited 0.
    _status(tmp_path, 1)
    _attempt(tmp_path, f"{DAY}-153700", out="old", code=1)
    _attempt(tmp_path, f"{DAY}-183500", out="new", code=0)
    _report(tmp_path, _block("AMBER") + TRACKER)
    got = verdict.review_outcome(DAY, str(tmp_path))
    assert got["exit"] == 0
    assert got["note"] == ""
    assert got["attempt"] == f"{DAY}-183500"


def test_attempt_lookup_survives_a_missing_pointer(tmp_path):
    _attempt(tmp_path, f"{DAY}-100000", out="a", code=0)
    _attempt(tmp_path, f"{DAY}-110000", out="b", code=1)
    (tmp_path / "review-attempts" / f"{DAY}-latest.txt").unlink()
    got = verdict.latest_attempt(DAY, str(tmp_path))
    assert got["id"] == f"{DAY}-110000"
    assert got["exit"] == 1


def test_a_killed_attempt_has_no_exit_code(tmp_path):
    _attempt(tmp_path, f"{DAY}-100000", out="a", code=None)
    assert verdict.latest_attempt(DAY, str(tmp_path))["exit"] is None


# ---- message wording ---------------------------------------------------------------------------


def test_the_four_telegram_lines_are_distinct_and_name_the_next_action(tmp_path):
    _status(tmp_path, 1)
    lines = {}
    _report(tmp_path, _block("AMBER") + TRACKER)
    lines["verdict"] = verdict.message("close-day", DAY, str(tmp_path))
    (tmp_path / f"eod-{DAY}-claude-review.md").unlink()
    _status(tmp_path, 0)  # exited cleanly but wrote nothing
    lines["none"] = verdict.message("close-day", DAY, str(tmp_path))
    _status(tmp_path, 1)
    _report(tmp_path, "You've hit your session limit · resets 5:30pm (Asia/Kolkata)\n")
    lines["limit"] = verdict.message("close-day", DAY, str(tmp_path))
    _report(tmp_path, "boom\n")
    lines["failed"] = verdict.message("close-day", DAY, str(tmp_path))
    assert len(set(lines.values())) == 4
    assert "no verdict" in lines["none"] and "Next:" in lines["none"]
    assert "usage limit" in lines["limit"] and "Next:" in lines["limit"]
    assert "failed" in lines["failed"] and "Next:" in lines["failed"]
    for text in lines.values():
        assert "HTTP" not in text and "exit 1" not in text


def test_other_failed_steps_are_still_listed_but_not_the_review(tmp_path):
    _status(tmp_path, 1)
    p = tmp_path / f"close-day-{DAY}-status.tsv"
    p.write_text(
        p.read_text().replace("backfill\t15:40:00\t15:41:00\t0", "backfill\t15:40:00\t15:41:00\t1")
    )
    _report(tmp_path, _block("AMBER") + TRACKER)
    text = verdict.message("close-day", DAY, str(tmp_path))
    assert "Steps failed: backfill." in text


def test_a_step_run_again_uses_its_newest_row(tmp_path):
    _status(tmp_path, 1, extra="review\t18:35:00\t18:50:00\t0\n")
    _report(tmp_path, _block("AMBER") + TRACKER)
    assert verdict.latest_steps("close-day", DAY, str(tmp_path))["review"] == 0
    assert verdict._failed_steps("close-day", DAY, str(tmp_path)) == []


def test_old_status_files_without_attempt_files_still_read(tmp_path):
    _status(tmp_path, 1)
    _report(tmp_path, _block("AMBER") + TRACKER)
    got = verdict.review_outcome(DAY, str(tmp_path))
    assert got["exit_source"] == "status" and got["exit"] == 1


# ---- the other consumers -----------------------------------------------------------------------


def _jobs_env(folder, review_exit, report):
    _status(folder, review_exit)
    if report is not None:
        _report(folder, report)


def test_jobs_audit_does_not_fail_the_phase_for_a_review_with_a_verdict(tmp_path):
    _jobs_env(tmp_path, 1, _block("AMBER") + TRACKER)
    check = next(c for c in jobs._phase_checks(DAY, str(tmp_path)) if c.name == "phase close-day")
    assert check.status != "FAIL"
    assert "review: verdict" in check.evidence


def test_jobs_audit_still_fails_a_genuinely_failed_review(tmp_path):
    _jobs_env(tmp_path, 1, "boom\n")
    check = next(c for c in jobs._phase_checks(DAY, str(tmp_path)) if c.name == "phase close-day")
    assert check.status == "FAIL"


def test_findings_follow_the_review_outcome(tmp_path):
    from blueprints.ops_findings import enumerate_findings

    def step_findings():
        return [
            f
            for f in enumerate_findings(DAY, str(tmp_path))
            if f["name"] == "review" and f["phase"] == "close-day"
        ]

    _jobs_env(tmp_path, 1, _block("AMBER") + TRACKER)
    assert step_findings() == []  # complete verdict: the exit code is not a finding
    _jobs_env(tmp_path, 1, "You've hit your 5-hour limit\n")
    found = step_findings()
    assert [f["status"] for f in found] == ["WARN"]
    assert "usage limit" in found[0]["plain"]
    _jobs_env(tmp_path, 0, _block("GREEN"))  # exit 0, but incomplete evidence
    assert [f["status"] for f in step_findings()] == ["WARN"]
    _jobs_env(tmp_path, 1, "boom\n")
    assert [f["status"] for f in step_findings()] == ["FAIL"]


def test_autotrack_records_only_a_genuine_review_failure(tmp_path):
    write_phase("open-day", DAY, [Check("x", OK, "ok")], "s", folder=str(tmp_path))
    path = str(tmp_path / "bugs.json")
    _jobs_env(tmp_path, 1, _block("AMBER") + TRACKER)
    assert autotrack.record_failures(DAY, folder=str(tmp_path), path=path) == []
    _jobs_env(tmp_path, 1, "You've hit your 5-hour limit\n")
    assert autotrack.record_failures(DAY, folder=str(tmp_path), path=path) == []
    _jobs_env(tmp_path, 1, "boom\n")
    lines = autotrack.record_failures(DAY, folder=str(tmp_path), path=path)
    assert len(lines) == 1 and "review" in lines[0]
    assert len(store.load(path)["bugs"]) == 1


def test_workflow_payload_carries_the_review_outcome(tmp_path):
    from blueprints.ops_workflow import day_view

    _jobs_env(tmp_path, 1, "You've hit your 5-hour limit\n")
    view = day_view(DAY, str(tmp_path))
    assert view["review"]["outcome"] == "limit_or_auth"
    assert view["verdict"] is None
    _jobs_env(tmp_path, 1, _block("GREEN"))  # incomplete: shown as AMBER, as Telegram shows it
    assert day_view(DAY, str(tmp_path))["verdict"]["verdict"] == "AMBER"
    assert day_view(DAY, str(tmp_path))["runs"]["close-day"]["state"] == "legacy"


def test_a_huge_attempt_file_is_read_only_at_its_head_and_tail(tmp_path):
    _status(tmp_path, 1)
    big = "head text\n" + "x" * 500_000 + "\ntail text"
    _attempt(tmp_path, f"{DAY}-154500", out=big, code=1)
    got = verdict.latest_attempt(DAY, str(tmp_path))
    assert len(got["out"]) < 140_000
    assert got["out"].startswith("head text")
    assert got["out"].endswith("tail text")
    assert verdict.review_outcome(DAY, str(tmp_path))["outcome"] == "failed"


def test_jobs_phase_check_warns_on_a_review_that_exited_zero_without_a_verdict(tmp_path):
    from signal_engine.ops.model import WARN

    _status(tmp_path, 0)
    _report(tmp_path, "text without any verdict block\n")
    checks = {c.name: c for c in jobs._phase_checks(DAY, folder=str(tmp_path))}
    assert checks["phase close-day"].status == WARN
    assert "no verdict" in checks["phase close-day"].evidence


def test_jobs_phase_check_stays_ok_on_a_review_with_a_verdict(tmp_path):
    from signal_engine.ops.model import OK

    _status(tmp_path, 0)
    _report(tmp_path, _block() + TRACKER)
    checks = {c.name: c for c in jobs._phase_checks(DAY, folder=str(tmp_path))}
    assert checks["phase close-day"].status == OK
