"""Open-day late-wake check: was the PC resumed or booted after 08:30 IST on a trading day (BUG-024)?"""

from datetime import datetime

import pytest

from signal_engine.ops import readiness, wakecheck
from signal_engine.ops.model import FAIL, IST, NOT_VERIFIED, OK, WARN

DAY = "2026-11-06"
REAL_LAST_WAKE = wakecheck.last_wake  # conftest stubs the module attribute for every test


def at(hh, mm=0, day=DAY):
    y, m, d = (int(x) for x in day.split("-"))
    return datetime(y, m, d, hh, mm, 0, tzinfo=IST)


def run(when, trading=True):
    return wakecheck.late_wake_check(DAY, provider=lambda: when, is_trading=lambda d: trading)


def test_wake_before_0830_is_ok():
    assert run(at(8, 15)).status == OK


def test_wake_exactly_at_0830_is_ok():
    assert run(at(8, 30)).status == OK


def test_wake_in_the_grace_window_is_a_warn():
    check = run(at(8, 45))
    assert check.status == WARN and "08:45" in check.evidence


def test_the_2026_11_06_wake_at_0939_is_a_fail():
    check = run(at(9, 39))
    assert check.status == FAIL and "09:39" in check.evidence and "BUG-024" not in check.name


def test_a_wake_on_an_earlier_day_is_ok():
    assert run(at(23, 0, day="2026-11-05")).status == OK


def test_not_a_trading_day_is_ok_and_not_judged():
    assert run(at(9, 39), trading=False).status == OK


def test_unknown_resume_time_is_not_verified_never_raises():
    assert run(None).status == NOT_VERIFIED


def test_a_raising_provider_is_not_verified():
    def boom():
        raise RuntimeError("x")

    check = wakecheck.late_wake_check(DAY, provider=boom, is_trading=lambda d: True)
    assert check.status == NOT_VERIFIED


def test_a_utc_time_is_converted_to_ist():
    from datetime import timezone

    utc = datetime(2026, 11, 6, 4, 9, 21, tzinfo=timezone.utc)  # 09:39:21 IST
    assert run(utc).status == FAIL


def test_parse_powershell_output():
    got = wakecheck.parse_output("2026-11-06T04:09:21.0000000Z\r\n")
    assert got == datetime(2026, 11, 6, 9, 39, 21, tzinfo=IST)
    assert wakecheck.parse_output("") is None
    assert wakecheck.parse_output("garbage") is None


def test_default_provider_without_powershell_is_none(monkeypatch):
    monkeypatch.setattr(wakecheck.shutil, "which", lambda n: None)
    monkeypatch.setattr(wakecheck.os.path, "exists", lambda p: False)
    assert REAL_LAST_WAKE() is None


def test_default_provider_survives_a_failing_powershell(monkeypatch):
    def fail(*a, **k):
        raise OSError("no")

    monkeypatch.setattr(wakecheck.shutil, "which", lambda n: "/x/powershell.exe")
    monkeypatch.setattr(wakecheck.subprocess, "run", fail)
    assert REAL_LAST_WAKE() is None


def test_readiness_open_day_includes_the_check(monkeypatch):
    from signal_engine.ops.model import Check

    for name in ("_services", "_health", "_engine_startup", "_poller_started", "_clock",
                 "_price_history", "_overnight_errors", "_yesterday_close", "_late_wake"):
        monkeypatch.setattr(readiness, name, lambda *a, _n=name, **k: Check(_n, OK, "x"))
    for name in ("_broker_and_mode", "_ports", "_disk_and_databases", "_morning_tasks"):
        monkeypatch.setattr(readiness, name, lambda *a, **k: [])
    monkeypatch.setattr(readiness, "_calendar_coverage", lambda day: Check("cal", OK, "x"))
    got = {}
    monkeypatch.setattr(readiness, "write_phase", lambda p, d, c, s: got.update(c=c))
    readiness.open_day(DAY)
    assert "_late_wake" in [c.name for c in got["c"]]


def test_plain_sentence_exists():
    from signal_engine.ops.plain import plain_for

    assert plain_for("PC wake time", OK)
    assert "asleep" in plain_for("PC wake time", FAIL) or "late" in plain_for("PC wake time", FAIL)
