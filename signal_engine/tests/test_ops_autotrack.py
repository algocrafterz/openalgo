"""The `track` step: FAIL checks reach the bug tracker in plain code, once per finding and day."""

import pytest

from signal_engine.bugs import store
from signal_engine.ops import autotrack
from signal_engine.ops.model import FAIL, OK, WARN, Check, write_phase

pytestmark = pytest.mark.filterwarnings("ignore")

DAY = "2026-11-11"


@pytest.fixture
def env(tmp_path):
    reports = tmp_path / "reports"
    write_phase(
        "liveness",
        DAY,
        [
            Check("first runner cycle", FAIL, "no 'pipeline cycle' line 09:20-09:40"),
            Check("stale prices", OK, "0 warnings"),
        ],
        "s",
        folder=str(reports),
    )
    write_phase(
        "open-day",
        DAY,
        [Check("errors since the last close", WARN, "31 records")],
        "s",
        folder=str(reports),
    )
    return str(reports), str(tmp_path / "bugs.json")


def test_only_fail_results_are_recorded(env):
    reports, path = env
    lines = autotrack.record_failures(DAY, folder=reports, path=path)
    assert lines == ["added BUG-001: first runner cycle"]
    bugs = store.load(path)["bugs"]
    assert len(bugs) == 1
    assert bugs[0]["status"] == "open"
    assert bugs[0]["severity"] == "MEDIUM"
    assert bugs[0]["occurrences"] == [DAY]
    assert any(s.startswith("workflow-finding:") for s in bugs[0]["sources"])


def test_running_the_step_twice_changes_nothing(env):
    reports, path = env
    autotrack.record_failures(DAY, folder=reports, path=path)
    before = store.load(path)
    lines = autotrack.record_failures(DAY, folder=reports, path=path)
    assert lines == ["unchanged BUG-001: first runner cycle"]
    assert store.load(path)["bugs"] == before["bugs"]


def test_the_same_failure_on_a_later_day_is_the_same_bug(env, tmp_path):
    reports, path = env
    autotrack.record_failures(DAY, folder=reports, path=path)
    write_phase(
        "liveness",
        "2026-11-12",
        [Check("first runner cycle", FAIL, "no 'pipeline cycle' line 09:20-09:40")],
        "s",
        folder=reports,
    )
    lines = autotrack.record_failures("2026-11-12", folder=reports, path=path)
    assert lines == ["seen BUG-001: first runner cycle"]
    bugs = store.load(path)["bugs"]
    assert len(bugs) == 1
    assert bugs[0]["occurrences"] == [DAY, "2026-11-12"]


def test_a_day_with_no_reports_records_nothing(tmp_path):
    path = str(tmp_path / "bugs.json")
    assert autotrack.record_failures(DAY, folder=str(tmp_path / "none"), path=path) == []
