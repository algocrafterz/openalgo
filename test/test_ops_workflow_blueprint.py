"""/logs/workflow reads phase status files and reports; it never serves a path outside the folder."""

import json

from blueprints.ops_workflow import day_view, list_days, read_report


def _seed(folder):
    (folder / "close-day-2026-10-30-status.tsv").write_text(
        "step\tstarted\tended\texit\neod\t15:36:00\t15:36:05\t0\n"
    )
    (folder / "open-day-2026-11-02-status.tsv").write_text(
        "step\tstarted\tended\texit\ncheck\t09:12:00\t09:12:09\t1\n"
    )
    (folder / "open-day-2026-11-02.json").write_text(json.dumps({"verdict": "RED", "checks": []}))
    (folder / "open-day-2026-11-02.md").write_text("# open-day")
    (folder / "eod-2026-10-30-claude-review.md").write_text("review")


def test_days_are_listed_newest_first(tmp_path):
    _seed(tmp_path)
    assert list_days(str(tmp_path)) == ["2026-11-02", "2026-10-30"]


def test_a_day_shows_steps_checks_and_reports(tmp_path):
    _seed(tmp_path)
    view = day_view("2026-11-02", str(tmp_path))
    phases = {p["phase"]: p["steps"] for p in view["phases"]}
    assert phases["open-day"] == [
        {"step": "check", "started": "09:12:00", "ended": "09:12:09", "exit": 1}
    ]
    assert phases["close-day"] is None
    assert view["checks"]["open-day"]["verdict"] == "RED"
    assert view["reports"] == ["open-day-2026-11-02.md"]


def test_reports_are_whitelisted_file_names_only(tmp_path):
    _seed(tmp_path)
    assert read_report("eod-2026-10-30-claude-review.md", str(tmp_path)) == "review"
    for name in (
        "../../.env",
        "/etc/passwd",
        "eod-2026-10-30-claude-review.md/../x.md",
        "bugs.json",
        "",
    ):
        assert read_report(name, str(tmp_path)) is None


def test_the_close_day_verdict_line_is_read_from_the_review(tmp_path):
    _seed(tmp_path)
    (tmp_path / "eod-2026-10-30-claude-review.md").write_text(
        'text\n```close-day-verdict\n{"verdict": "RED", "line": "Net -2,149 after charges."}\n```\n'
    )
    assert day_view("2026-10-30", str(tmp_path))["verdict"] == {
        "verdict": "RED",
        "line": "Net -2,149 after charges.",
    }
    assert day_view("2026-11-02", str(tmp_path))["verdict"] is None


# ---- R4.1: run states, recurring failures, last notification ---------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

from signal_engine.ops import workflow_status  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def _manifest(folder, phase, day, state, started, ended=None, note=""):
    (folder / f"{phase}-{day}-run.json").write_text(
        json.dumps(
            {
                "phase": phase,
                "day": day,
                "state": state,
                "started_at": started,
                "ended_at": ended,
                "note": note,
                "pid": 0,
                "steps": [],
            }
        )
    )


def _checks(folder, group, day, *failed):
    rows = [{"name": n, "status": "FAIL", "evidence": "x"} for n in failed]
    rows.append({"name": "fine", "status": "OK", "evidence": "x"})
    (folder / f"{group}-{day}.json").write_text(json.dumps({"verdict": "RED", "checks": rows}))
    (folder / f"open-day-{day}-status.tsv").write_text("step\tstarted\tended\texit\n")


def test_run_states_expected_versus_completed(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow_status, "_is_trading_day", lambda d: True)
    day = "2026-11-12"
    _manifest(
        tmp_path, "open-day", day, "completed", f"{day}T09:12:03+05:30", f"{day}T09:13:00+05:30"
    )
    _manifest(
        tmp_path, "liveness", day, "completed", f"{day}T09:50:00+05:30", f"{day}T09:51:00+05:30"
    )
    (tmp_path / f"close-day-{day}-status.tsv").write_text("step\tstarted\tended\texit\n")
    now = datetime(2026, 11, 12, 18, 0, tzinfo=IST)
    rows = {r["phase"]: r for r in workflow_status.run_status(day, str(tmp_path), now)}
    assert rows["open-day"]["state"] == "completed" and rows["open-day"]["late"] is False
    assert rows["liveness"]["state"] == "completed" and rows["liveness"]["late"] is True
    assert rows["close-day"]["state"] == "legacy"
    # no record, due time passed -> missed; before due -> not_due
    empty = tmp_path / "e"
    empty.mkdir()
    r = workflow_status.run_status(day, str(empty), now)
    assert {x["state"] for x in r} == {"missed"}
    early = datetime(2026, 11, 12, 9, 0, tzinfo=IST)
    assert {x["state"] for x in workflow_status.run_status(day, str(empty), early)} == {"not_due"}


def test_interrupted_and_non_trading_day(tmp_path, monkeypatch):
    day = "2026-11-12"
    _manifest(tmp_path, "open-day", day, "running", f"{day}T09:12:00+05:30")
    now = datetime(2026, 11, 12, 18, 0, tzinfo=IST)  # pid 0 is not a live process id
    monkeypatch.setattr(workflow_status, "_is_trading_day", lambda d: True)
    rows = workflow_status.run_status(day, str(tmp_path), now)
    assert rows[0]["state"] in ("interrupted", "running")
    monkeypatch.setattr(workflow_status, "_is_trading_day", lambda d: False)
    assert workflow_status.run_status(day, str(tmp_path), now)[1]["state"] == "not_expected"


def test_recurrence_counts_last_ten_sessions(tmp_path):
    days = [f"2026-11-{d:02d}" for d in range(2, 14)]  # 12 sessions
    for d in days:
        _checks(tmp_path, "open-day", d)  # status file marks the session
    for d in days[:2]:  # outside the 10-session window ending on the last day
        _checks(tmp_path, "logs", d, "Error spike")
    for d in days[5:8] + days[-1:]:
        _checks(tmp_path, "logs", d, f"Error spike {d}")  # name carries the date
    out = workflow_status.recurrence(days[-1], str(tmp_path))
    assert out["window"] == 10
    item = next(i for i in out["items"] if i["group"] == "logs")
    assert item["count"] == 4
    assert item["first"] == days[5] and item["last"] == days[-1]
    assert item["failing_today"] is True
    # window ends at an earlier day: only sessions up to it count
    older = workflow_status.recurrence(days[1], str(tmp_path))
    assert older["window"] == 2 and older["items"][0]["count"] == 2


def test_notifications_summary_is_clipped_and_secret_free(tmp_path):
    assert workflow_status.notifications(str(tmp_path))["available"] is False
    state = {
        "version": 1,
        "last_mode": {"mode": "live", "at": "x"},
        "token": "SECRET-TOKEN",
        "chat_id": "12345",
        "keys": {
            "alert|open-day|" + "c" * 300: {
                "status": "red",
                "last_sent_at": "2026-11-12T09:13:00+05:30",
            },
            "alert|liveness|ok": {
                "status": "ok",
                "last_sent_at": "2026-11-12T09:30:00+05:30",
                "recovered_at": "2026-11-12T09:30:00+05:30",
            },
        },
        "log": [
            {"at": "2026-11-12T09:13:00+05:30", "key": "a", "outcome": "sent", "attempts": 1},
            {
                "at": "2026-11-12T09:30:00+05:30",
                "key": "alert|liveness|ok",
                "outcome": "failed_after_retries",
                "attempts": 3,
            },
        ],
    }
    (tmp_path / "telegram-state.json").write_text(json.dumps(state))
    out = workflow_status.notifications(str(tmp_path))
    text = json.dumps(out)
    assert "SECRET-TOKEN" not in text and "12345" not in text
    assert out["last_result"] == "failed_after_retries"
    assert out["last_sent_at"] == "2026-11-12T09:30:00+05:30"
    assert out["last_recovery_key"] == "alert|liveness|ok"
    assert len(out["open_red"]) == 1 and len(out["open_red"][0]) <= 120


def test_damaged_alert_state_is_not_renamed(tmp_path):
    (tmp_path / "telegram-state.json").write_text("{not json")
    out = workflow_status.notifications(str(tmp_path))
    assert out["available"] is False
    assert (tmp_path / "telegram-state.json").exists()
    assert not (tmp_path / "telegram-state.json.corrupt").exists()


def test_day_view_adds_fields_and_keeps_old_ones(tmp_path):
    _seed(tmp_path)
    view = day_view("2026-11-02", str(tmp_path))
    for key in ("phases", "checks", "reports", "verdict", "review", "runs"):
        assert key in view
    assert {r["phase"] for r in view["run_status"]} == {"open-day", "liveness", "close-day"}
    assert view["recurrence"]["window"] >= 1
    assert view["notifications"]["available"] is False
