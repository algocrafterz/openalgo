"""F5: folder resolved at call time, a damaged check report, and one source for the clock times."""

import json

import pytest
from flask import Flask

import blueprints.ops_workflow as mod
from limiter import limiter
from signal_engine.ops import automation, workflow_status

pytestmark = pytest.mark.filterwarnings("ignore")
DAY = "2026-11-02"


def _seed(folder):
    (folder / f"open-day-{DAY}-status.tsv").write_text(
        "step\tstarted\tended\texit\ncheck\t09:12:00\t09:12:09\t0\n"
    )


# (a) call-time folder -----------------------------------------------------------------------


def test_the_default_folder_is_read_when_called_not_when_imported(tmp_path, monkeypatch):
    _seed(tmp_path)
    monkeypatch.setattr(mod, "REPORTS_DIR", str(tmp_path))
    assert mod.list_days() == [DAY]
    assert mod.day_view(DAY)["phases"][0]["steps"][0]["step"] == "check"
    (tmp_path / f"eod-{DAY}-claude-review.md").write_text("review")
    assert mod.read_report(f"eod-{DAY}-claude-review.md") == "review"
    assert mod.close_day_verdict(DAY) is None


# (b) damaged check report -------------------------------------------------------------------


def test_a_corrupt_check_file_is_not_checked_and_does_not_break_the_day(tmp_path):
    _seed(tmp_path)
    (tmp_path / f"open-day-{DAY}.json").write_text("{not json")
    (tmp_path / f"logs-{DAY}.json").write_text(json.dumps({"verdict": "GREEN", "checks": []}))
    assert mod._checked("open-day", DAY, str(tmp_path)) is None
    view = mod.day_view(DAY, str(tmp_path))
    assert "open-day" not in view["checks"] and view["checks"]["logs"]["verdict"] == "GREEN"


def test_a_binary_check_file_is_not_checked_either(tmp_path):
    (tmp_path / f"jobs-{DAY}.json").write_bytes(b"\xff\xfe\x00\x80")
    assert mod._checked("jobs", DAY, str(tmp_path)) is None


# (c) one source of times --------------------------------------------------------------------


def test_the_day_payload_carries_the_schedule(tmp_path):
    _seed(tmp_path)
    schedule = mod.day_view(DAY, str(tmp_path))["schedule"]
    assert schedule["phases"] == {"open-day": "09:12", "liveness": "09:27", "close-day": "15:36"}
    assert schedule["margin_minutes"] == 10


def test_a_schedule_that_cannot_be_read_is_absent_not_null(tmp_path, monkeypatch):
    _seed(tmp_path)

    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(workflow_status, "schedule", boom)
    assert "schedule" not in mod.day_view(DAY, str(tmp_path))


def test_the_scheduler_times_come_from_the_one_source():
    for _kind, phases in automation.EXPECTED.items():
        for phase, label, hhmm in phases:
            assert hhmm == workflow_status.DUE[phase] and hhmm in label
    assert automation.DUE_MARGIN_MINUTES == workflow_status.MARGIN_MINUTES


def test_changing_the_source_changes_status_and_scheduler(monkeypatch):
    monkeypatch.setitem(workflow_status.DUE, "open-day", "09:15")
    assert workflow_status.schedule()["phases"]["open-day"] == "09:15"
    assert (
        workflow_status._due_at(DAY, "open-day", workflow_status.model.now_ist()).strftime("%H:%M")
        == "09:25"
    )


def test_the_route_returns_the_schedule_with_and_without_a_day(tmp_path, monkeypatch):
    import blueprints.auth as auth_bp_module
    import utils.session as session_utils

    monkeypatch.setattr(mod, "REPORTS_DIR", str(tmp_path))
    app = Flask(__name__)
    app.secret_key = "t"
    app.register_blueprint(mod.ops_workflow_bp)
    app.register_blueprint(auth_bp_module.auth_bp)
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user"], s["logged_in"] = "tester", True
        s["login_time"] = "2026-01-01T09:00:00+05:30"
    empty = client.get("/api/ops/workflow").get_json()
    assert empty["data"]["schedule"]["phases"]["open-day"] == "09:12"
    _seed(tmp_path)
    full = client.get(f"/api/ops/workflow?day={DAY}").get_json()
    assert full["days"] == [DAY] and full["data"]["schedule"]["phases"]["close-day"] == "15:36"
