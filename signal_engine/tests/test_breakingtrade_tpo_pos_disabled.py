"""BUG-021, Option B: before about 09:45 the vendor shows relative-to-open text, so tpo_pos is empty.

The snapshot must still be accepted; only the scans and score legs that need tpo_pos switch
themselves off, visibly. Everything else keeps working. The scans resume when classes appear.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd
import pytest

from signal_engine.analysis.breakingtrade import (
    __main__ as poller,
)
from signal_engine.analysis.breakingtrade import (
    critical,
    emerging,
    extractor,
    scans,
    scorer,
    store,
)

AT = datetime(2026, 11, 13, 10, 45)

TPO_SCANS = {
    "The Runaway",
    "The Breakdown",
    "Value Migration Up",
    "Value Migration Down",
    "The Gap-Up Trap",
    "Neutral Day Resolution Up",
    "Neutral Day Resolution Down",
    "Live Print in Formation Up",
    "Live Print in Formation Down",
}


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch, tmp_path):
    """No state, database or alert sender leaks between tests or reaches the live ones."""
    monkeypatch.setattr(scans, "_tpo_state", None)
    monkeypatch.setattr(store, "_DB_PATH", str(tmp_path / "breakingtrade.db"))
    sent = []  # the banners the poller records (nothing is delivered)
    monkeypatch.setattr(critical, "issues", critical.CriticalIssues(record=sent.append))
    return sent


def _frame(n=25, **overrides):
    base = {
        "symbol": [f"S{i}" for i in range(n)],
        "sector": "IT",
        "price": 100.0,
        "change_pct": 1.0,
        "ib_pct": 40.0,
        "opening": "gap_up",
        "open_type": "open_drive",
        "open_type_dir": "up",
        "tail": None,
        "day_type": "Trend",
        "day_type_dir": "up",
        "tpo_pos": None,
        "tpo_pos_count": None,
        "tpo_pos_prev": "above_pdh",
    }
    base.update(overrides)
    return pd.DataFrame(base)


def test_empty_tpo_pos_snapshot_is_accepted():
    extractor.check_populated(_frame(), "market_profile")


def test_other_empty_required_column_is_still_refused():
    with pytest.raises(extractor.SnapshotFormatError):
        extractor.check_populated(_frame(day_type=None), "market_profile")
    with pytest.raises(extractor.SnapshotFormatError):
        extractor.check_populated(_frame(tpo_pos_prev=None), "market_profile")


def test_tpo_pos_is_not_in_always_populated():
    assert "tpo_pos" not in extractor._ALWAYS_POPULATED


def test_tpo_scans_are_disabled_with_reason_and_do_not_raise(caplog):
    # rows that would have matched The Runaway and Live Print Up with a value-area reading
    frame = _frame(tpo_pos=None)
    with caplog.at_level(logging.WARNING):
        results = scans.run_scans(frame, volume=None, captured_at=AT)
    assert len(results) == len(scans.SCANS)
    by_name = {r.scan.name: r for r in results}
    for name in TPO_SCANS:
        result = by_name[name]
        assert result.status == "DISABLED"
        assert result.disabled_reason == scans.TPO_POS_DISABLED_REASON
        assert result.disabled_reason == (
            "tpo_pos not yet classified (vendor shows relative-to-open text until about 09:45)"
        )
        assert result.matches.empty
    assert {s.name for s in scans.SCANS if s.needs_tpo_pos} == TPO_SCANS
    warnings = [r for r in caplog.records if "scans disabled" in r.getMessage()]
    assert len(warnings) == 1  # once per run, not once per scan
    assert "0/25 rows" in warnings[0].getMessage()


def test_unaffected_scans_still_match_without_tpo_pos():
    frame = _frame(tail="buy_tail")
    results = {r.scan.name: r for r in scans.run_scans(frame, captured_at=AT)}
    breakaway = results["Breakaway Above PDH"]
    assert breakaway.status != "DISABLED"
    assert breakaway.disabled_reason is None
    assert len(breakaway.matches) == len(frame)

    rescue_frame = _frame(
        opening="gap_down", open_type="rejection", open_type_dir="up", tail="buy_tail"
    )
    rescue = {r.scan.name: r for r in scans.run_scans(rescue_frame, captured_at=AT)}
    assert len(rescue["Gap-Down Rescue"].matches) == len(rescue_frame)


def test_tpo_scans_run_normally_when_tpo_pos_is_present():
    frame = _frame(tpo_pos="above_va")
    results = {r.scan.name: r for r in scans.run_scans(frame, captured_at=AT)}
    assert results["The Runaway"].status != "DISABLED"
    assert len(results["The Runaway"].matches) == len(frame)


def test_scorer_works_without_tpo_pos_and_drops_exactly_the_tpo_leg():
    with_tpo = pd.Series(
        {
            "day_type": "Trend",
            "day_type_dir": "up",
            "open_type": "open_drive",
            "open_type_dir": "up",
            "opening": "gap_up",
            "ib_pct": 40.0,
            "tpo_pos": "tpo_ext_high",
            "tpo_pos_prev": None,
        }
    )
    without = with_tpo.copy()
    without["tpo_pos"] = None
    a = scorer._score_row(with_tpo)
    b = scorer._score_row(without)
    assert a["net_score"] - b["net_score"] == pytest.approx(2.5)
    assert b["confirming_legs"] == a["confirming_legs"] - 1
    assert b["net_score"] > 0


def test_rank_runs_without_tpo_pos_and_adds_no_per_call_warning(caplog):
    frame = _frame(tail="buy_tail")
    with caplog.at_level(logging.WARNING):
        watchlist = scorer.rank(frame)
    assert len(watchlist.bullish) > 0
    assert not caplog.records


def test_emerging_candidates_is_empty_and_does_not_raise_without_tpo_pos(caplog):
    frame = _frame()
    with caplog.at_level(logging.WARNING):
        picks = emerging.candidates(frame, None, AT, require_volume=False)
    assert picks.empty
    assert not caplog.records  # the notice is logged by run_scans on a state change only


def _scan_messages(caplog):
    return [r for r in caplog.records if "BreakingTrade" in r.getMessage()]


def test_partly_populated_column_runs_the_nine_scans_and_returns_only_classified_rows():
    frame = _frame(n=10, tpo_pos=[None] * 7 + ["above_va"] * 3)
    results = {r.scan.name: r for r in scans.run_scans(frame, captured_at=AT)}
    for name in TPO_SCANS:
        assert results[name].status != "DISABLED"
        assert results[name].disabled_reason is None
    assert list(results["The Runaway"].matches["symbol"]) == ["S7", "S8", "S9"]


def test_second_poll_hits_are_new_after_a_disabled_poll():
    early = scans.run_scans(_frame(tpo_pos=None), captured_at=AT)
    assert all(r.matches.empty for r in early if r.scan.needs_tpo_pos)
    first = AT - timedelta(minutes=15)
    baseline = store.has_snapshot_before(first)
    store.record_hits(early, first, baseline_exists=baseline)

    later = scans.run_scans(_frame(tpo_pos="above_va"), captured_at=AT)
    new = store.record_hits(later, AT, baseline_exists=True)
    assert len(new["The Runaway"]) == 25


def test_disabled_notice_is_logged_once_then_reenabled_info(caplog):
    with caplog.at_level(logging.INFO):
        scans.run_scans(_frame(tpo_pos=None), captured_at=AT)
        scans.run_scans(_frame(tpo_pos=None), captured_at=AT)
        assert len(_scan_messages(caplog)) == 1
        assert _scan_messages(caplog)[0].levelno == logging.WARNING
        scans.run_scans(_frame(n=228, tpo_pos="above_va"), captured_at=AT)
        scans.run_scans(_frame(n=228, tpo_pos="above_va"), captured_at=AT)
    messages = _scan_messages(caplog)
    assert len(messages) == 2
    assert messages[1].levelno == logging.INFO
    assert "classified again on 228/228 rows; 9 scans re-enabled" in messages[1].getMessage()


def test_no_notice_when_the_first_poll_is_already_classified(caplog):
    with caplog.at_level(logging.INFO):
        scans.run_scans(_frame(tpo_pos="above_va"), captured_at=AT)
    assert not _scan_messages(caplog)


def test_empty_column_after_ten_raises_one_alert_per_day(_fresh_state):
    sent = _fresh_state
    empty = _frame(tpo_pos=None)
    poller._tpo_pos_watch(empty, datetime(2026, 11, 13, 9, 59))
    assert sent == []  # expected before 10:00
    poller._tpo_pos_watch(empty, datetime(2026, 11, 13, 10, 0))
    poller._tpo_pos_watch(empty, datetime(2026, 11, 13, 10, 30))
    assert len(sent) == 1
    assert "still not showing where price sits against today's value area" in sent[0]
    assert "9 of the 12 scans are off" in sent[0]
    poller._tpo_pos_watch(_frame(tpo_pos="above_va"), datetime(2026, 11, 13, 10, 45))
    assert len(sent) == 2 and sent[1].startswith("RECOVERED")
    poller._tpo_pos_watch(_frame(tpo_pos="above_va"), datetime(2026, 11, 13, 11, 0))
    assert len(sent) == 2  # recovery is sent once


def test_alert_repeats_on_the_next_day(monkeypatch, _fresh_state):
    sent = _fresh_state
    day = {"now": datetime(2026, 11, 13, 10, 5)}
    monkeypatch.setattr(
        critical, "issues", critical.CriticalIssues(record=sent.append, clock=lambda: day["now"])
    )
    empty = _frame(tpo_pos=None)
    poller._tpo_pos_watch(empty, day["now"])
    day["now"] = datetime(2026, 11, 16, 10, 5)
    poller._tpo_pos_watch(empty, day["now"])
    assert len(sent) == 2


def test_classified_poll_after_ten_sends_nothing(_fresh_state):
    poller._tpo_pos_watch(_frame(tpo_pos="above_va"), datetime(2026, 11, 13, 10, 30))
    assert _fresh_state == []


def test_unaffected_scans_match_the_same_with_an_empty_or_missing_column():
    frame = _frame(tail="buy_tail")
    with_none = {r.scan.name: r for r in scans.run_scans(frame, captured_at=AT)}
    without_col = {
        r.scan.name: r for r in scans.run_scans(frame.drop(columns=["tpo_pos"]), captured_at=AT)
    }
    for scan in scans.SCANS:
        if not scan.needs_tpo_pos:
            a, b = with_none[scan.name], without_col[scan.name]
            assert a.status == b.status
            assert list(a.matches["symbol"]) == list(b.matches["symbol"])
