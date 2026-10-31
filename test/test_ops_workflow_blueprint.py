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
