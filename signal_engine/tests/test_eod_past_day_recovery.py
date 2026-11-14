"""EOD report for a past day (the morning recovery): saved tradebook only, no funds reconciliation.

The broker's tradebook and funds answer for the current session only. A recovery run for the
previous trading day must therefore read the snapshot saved at 15:36 and must not call the broker.
"""

import asyncio
import json
from datetime import date

import pytest

from signal_engine.analysis import __main__ as analysis_main
from signal_engine.analysis import eod_review

PAST = "2026-11-12"
TODAY = date(2026, 11, 13)


@pytest.fixture
def snaps(tmp_path, monkeypatch):
    monkeypatch.setattr(analysis_main, "SNAP_DIR", str(tmp_path))
    monkeypatch.setattr(analysis_main, "ist_today", lambda: TODAY)

    async def _must_not_fetch():
        raise AssertionError("the broker tradebook was called for a past day")

    monkeypatch.setattr(analysis_main, "_fetch_tradebook", _must_not_fetch)
    return tmp_path


def test_a_past_day_reads_the_saved_snapshot_and_never_calls_the_broker(snaps):
    saved = snaps / f"tradebook_{PAST}.json"
    saved.write_text(json.dumps([{"orderid": "1"}]))
    got = analysis_main.snapshot(date.fromisoformat(PAST))
    assert got == str(saved)
    assert json.loads(saved.read_text()) == [{"orderid": "1"}]  # untouched


def test_a_past_day_without_a_saved_snapshot_is_reported_not_fetched_and_writes_nothing(snaps):
    assert analysis_main.snapshot(date.fromisoformat(PAST)) is None
    assert not list(snaps.glob("tradebook_*"))  # today's file was not created either


def test_the_funds_reconciliation_is_not_applicable_for_a_past_day_and_calls_no_broker(
    monkeypatch,
):
    from signal_engine import reconcile

    monkeypatch.setattr(eod_review, "ist_today", lambda: TODAY)

    async def _must_not_reconcile(mode, day):
        raise AssertionError("the funds API was called for a past day")

    monkeypatch.setattr(reconcile, "reconcile_day", _must_not_reconcile)
    got = asyncio.run(eod_review.check_reconciliation("analyze", PAST))
    assert got.startswith("SKIPPED") and "skipped_not_applicable" in got


def test_the_funds_reconciliation_still_runs_for_today(monkeypatch):
    from signal_engine import reconcile

    monkeypatch.setattr(eod_review, "ist_today", lambda: TODAY)
    called = []

    class Result:
        comparable = False

        def summary(self):
            return "not comparable"

    async def _reconcile(mode, day):
        called.append((mode, day))
        return Result()

    monkeypatch.setattr(reconcile, "reconcile_day", _reconcile)
    got = asyncio.run(eod_review.check_reconciliation("analyze", TODAY.isoformat()))
    assert called == [("analyze", "2026-11-13")] and got.startswith("SKIPPED")
