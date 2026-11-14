"""Open-day readiness: a bad previous close-day is a WARN, and it never touches today's checks.

A history job that failed yesterday (data download, reports, review) must not turn the morning RED.
The checks of today (broker session, price feed, OpenAlgo health, engine start, databases) keep
their own FAIL, and a WARN from yesterday never lowers or hides one of them.
"""

from pathlib import Path

import pytest

from signal_engine.ops import model, readiness
from signal_engine.ops.model import FAIL, OK, WARN, Check

TODAY = "2026-11-13"
PREV = "2026-11-12"
pytestmark = pytest.mark.filterwarnings("ignore")


def _status(folder, rows):
    body = "step\tstarted\tended\texit\n" + "".join(
        f"{n}\t15:36:00\t15:36:09\t{c}\n" for n, c in rows
    )
    Path(folder, f"close-day-{PREV}-status.tsv").write_text(body)


@pytest.fixture
def reports(tmp_path, monkeypatch):
    monkeypatch.setattr(readiness, "REPORTS", str(tmp_path))
    monkeypatch.setattr(readiness, "_previous_weekday", lambda day: PREV)
    return tmp_path


def _open_day(monkeypatch, today_overrides=None):
    """open_day with every check of today stubbed healthy, except the ones in `today_overrides`.

    _yesterday_close is NOT stubbed: it reads the tmp reports folder. Returns (checks, verdict).
    """
    healthy = {
        "_services": Check("services (openalgoctl status)", OK, "running"),
        "_health": Check("OpenAlgo health", OK, "pass"),
        "_engine_startup": Check("engine startup", OK, "ok"),
        "_poller_started": Check("BreakingTrade poller", OK, "ok"),
        "_clock": Check("clock (NTP)", OK, "yes"),
        "_price_history": Check(
            "price history for today", OK, "stubbed: the real store is not read"
        ),
        "_overnight_errors": Check("errors since the last close", OK, "none"),
    }
    healthy.update(today_overrides or {})
    for name, check in healthy.items():
        monkeypatch.setattr(readiness, name, lambda *a, _c=check, **k: _c)
    lists = {
        "_broker_and_mode": [
            Check("broker session (funds API)", OK, "capital"),
            Check("trading mode", OK, "analyze"),
        ],
        "_ports": [Check("websocket proxy :8765", OK, "up"), Check("ZeroMQ bus :5555", OK, "up")],
        "_disk_and_databases": [Check("disk", OK, "100 GB"), Check("trades.db", OK, "readable")],
        "_morning_tasks": [Check("task openAlgoAutoStart", OK, "ran")],
    }
    for name, value in (today_overrides or {}).items():
        if name in lists:
            lists[name] = value
    for name, value in lists.items():
        monkeypatch.setattr(readiness, name, lambda *a, _v=value, **k: list(_v))
    monkeypatch.setattr(readiness, "_calendar_coverage", lambda day: Check("NSE calendar", OK, "x"))
    captured = {}
    monkeypatch.setattr(
        readiness, "write_phase", lambda phase, day, checks, summary: captured.update(c=checks)
    )
    readiness.open_day(TODAY)
    return captured["c"], model.verdict(captured["c"])


def test_a_failed_previous_day_step_is_a_warn_that_names_the_cause_and_the_next_action(reports):
    _status(reports, [("eod", 0), ("backfill", 1), ("value_areas", 0)])
    check = readiness._yesterday_close(TODAY)
    assert check.status == WARN
    assert "backfill" in check.evidence  # the cause
    # The download feeds the history the engine warms up from: the text must say so and must
    # not claim that trading is unaffected.
    assert "Old price history can change what the strategies see" in check.evidence
    assert "does not block trading" not in check.evidence and "not affected" not in check.evidence
    assert "recovery" in check.evidence and "Daily Workflow page" in check.evidence  # next action


def test_a_recovered_previous_day_step_is_no_longer_a_warning(reports):
    _status(reports, [("backfill", 1), ("backfill", 0)])  # the retry appended a good row
    assert readiness._yesterday_close(TODAY).status == OK


def test_a_missing_previous_status_file_is_a_warn(reports):
    assert readiness._yesterday_close(TODAY).status == WARN


def test_previous_day_bad_and_today_healthy_is_amber_not_red(reports, monkeypatch):
    _status(reports, [("backfill", 1), ("review", 1)])
    checks, result = _open_day(monkeypatch)
    previous = [c for c in checks if c.name.startswith("close-day")]
    assert [c.status for c in previous] == [WARN]
    assert result == "AMBER"
    assert not [c for c in checks if c.status == FAIL]


@pytest.mark.parametrize(
    "broken",
    [
        {"_health": Check("OpenAlgo health", FAIL, "no answer")},
        {"_services": Check("services (openalgoctl status)", FAIL, "stopped")},
        {"_engine_startup": Check("engine startup", FAIL, "no warm-up")},
        {"_broker_and_mode": [Check("broker session (funds API)", FAIL, "no session")]},
        {"_ports": [Check("websocket proxy :8765", FAIL, "refused")]},
        {"_disk_and_databases": [Check("trades.db", FAIL, "locked")]},
    ],
    ids=["health", "services", "engine", "broker session", "price feed", "database"],
)
def test_previous_day_healthy_and_a_current_day_critical_check_failing_is_red(
    reports, monkeypatch, broken
):
    _status(reports, [("eod", 0), ("backfill", 0)])
    checks, result = _open_day(monkeypatch, broken)
    assert result == "RED"
    assert [c.status for c in checks if c.name.startswith("close-day")] == [OK]


def test_a_previous_day_warn_never_lowers_or_masks_a_current_day_fail(reports, monkeypatch):
    _status(reports, [("backfill", 1)])
    broken = {"_health": Check("OpenAlgo health", FAIL, "no answer")}
    checks, result = _open_day(monkeypatch, broken)
    assert result == "RED"
    by_name = {c.name: c.status for c in checks}
    assert by_name["OpenAlgo health"] == FAIL  # still FAIL, not softened
    assert by_name["close-day 2026-11-12"] == WARN  # and still its own WARN


def test_the_previous_day_check_cannot_return_a_fail():
    """Source check: no Check built in _yesterday_close carries the FAIL status."""
    import inspect

    body = inspect.getsource(readiness._yesterday_close)
    assert ", FAIL," not in body and "FAIL)" not in body
    assert body.count("WARN") >= 3


def test_a_failed_data_step_stays_a_warn_names_the_data_risk_and_does_not_promise_a_recovery(
    reports,
):
    _status(reports, [("eod", 0), ("backfill", 1), ("value_areas", 1)])
    check = readiness._yesterday_close(TODAY)
    assert check.status == WARN  # the maintainer's choice: never FAIL for the previous day
    assert "market data download" in check.evidence and "value-area levels" in check.evidence
    assert "Old price history can change what the strategies see" in check.evidence
    for false_claim in ("not affected", "does not block", "will run", "retries them"):
        assert false_claim not in check.evidence
    assert "read what the 08:36 recovery did" in check.evidence  # it already had its turn at 09:12


def test_a_failed_report_step_makes_no_claim_about_trading_either_way(reports):
    _status(reports, [("track", 1)])
    check = readiness._yesterday_close(TODAY)
    assert check.status == WARN and "price history" not in check.evidence
    assert "not affected" not in check.evidence


def test_a_stale_data_step_never_hides_a_failing_check_of_today(reports, monkeypatch):
    _status(reports, [("backfill", 1)])
    checks, result = _open_day(
        monkeypatch, {"_broker_and_mode": [Check("broker session (funds API)", FAIL, "none")]}
    )
    assert result == "RED"
    assert [c.status for c in checks if c.name.startswith("close-day")] == [WARN]
    assert [c.name for c in checks if c.status == FAIL] == ["broker session (funds API)"]


def test_the_previous_close_day_skips_a_holiday(reports, monkeypatch):
    monkeypatch.undo()  # the fixture's patch of the weekday helper must not hide the holiday walk
    monkeypatch.setattr(readiness, "REPORTS", str(reports))
    seen = []
    monkeypatch.setattr(
        readiness.automation,
        "is_trading_day",
        lambda day, **k: seen.append(day) or day != "2026-11-10",
    )
    assert readiness._previous_close_day("2026-11-11") == "2026-11-09"  # Tuesday was a holiday
    assert seen == ["2026-11-10", "2026-11-09"]
