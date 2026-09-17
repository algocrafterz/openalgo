"""Snapshot storage and transition detection (store.py), plus the fetcher's content-based
table identification (fetcher.py) - see signal_engine/analysis/breakingtrade/.

The fetcher tests deliberately never launch a browser: the part that can silently go wrong is
picking the right table out of a page full of them, and that is a pure function over the
structure Playwright hands back.
"""

from datetime import datetime

import pandas as pd
import pytest

from signal_engine.analysis.breakingtrade import fetcher, store
from signal_engine.analysis.breakingtrade.extractor import Snapshot


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Every test gets its own database file - these tests must never touch the real one."""
    monkeypatch.setattr(store, "_DB_PATH", str(tmp_path / "breakingtrade.db"))


def _snapshot(captured_at, symbols, day_type="Trend"):
    frame = pd.DataFrame(
        [
            {
                "symbol": symbol,
                "sector": "PHARMA",
                "price": 100.0,
                "change_pct": -1.5,
                "ib_pct": 30.0,
                "day_type": day_type,
                "day_type_dir": "down",
                "tpo_pos": "below_va",
                "tpo_pos_prev": "below_pdl",
                "tail": "sell_tail",
            }
            for symbol in symbols
        ]
    )
    return Snapshot(kind="market_profile", captured_at=captured_at, frame=frame)


class _FakeScan:
    def __init__(self, name, direction="down"):
        self.name = name
        self.direction = direction


class _FakeResult:
    def __init__(self, name, symbols):
        self.scan = _FakeScan(name)
        self.matches = pd.DataFrame({"symbol": symbols})


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_save_snapshot_writes_every_row():
    saved = store.save_snapshot(_snapshot(datetime(2026, 9, 3, 12, 31), ["ZYDUSLIFE", "VEDL"]))
    assert saved == 2
    assert len(store.history()) == 2


def test_save_snapshot_is_idempotent_for_the_same_poll():
    snapshot = _snapshot(datetime(2026, 9, 3, 12, 31), ["ZYDUSLIFE"])
    store.save_snapshot(snapshot)
    store.save_snapshot(snapshot)
    assert len(store.history()) == 1


def test_save_snapshot_rejects_undated_snapshot():
    with pytest.raises(ValueError):
        store.save_snapshot(_snapshot(None, ["ZYDUSLIFE"]))


def test_history_filters_by_symbol():
    store.save_snapshot(_snapshot(datetime(2026, 9, 3, 12, 31), ["ZYDUSLIFE", "VEDL"]))
    assert list(store.history(symbol="VEDL")["symbol"]) == ["VEDL"]


# ---------------------------------------------------------------------------
# BTST candidate persistence (live vs retrospective comparison)
# ---------------------------------------------------------------------------


def _candidates_frame(rows):
    """rows: list of (symbol, delivery_pct, change_pct), already in rank order."""
    return pd.DataFrame(
        [{"symbol": s, "delivery_pct": d, "change_pct": c} for s, d, c in rows]
    )


def test_save_and_read_back_a_candidate_list():
    frame = _candidates_frame([("TCS", 0.8, 2.1), ("INFY", 0.7, 1.5)])
    written = store.save_btst_candidates("2026-09-09", "live", frame)

    assert written == 2
    read_back = store.btst_candidates_for("2026-09-09", "live")
    assert list(read_back["symbol"]) == ["TCS", "INFY"]
    assert list(read_back["rank"]) == [0, 1]


def test_live_and_retrospective_are_independent():
    store.save_btst_candidates("2026-09-09", "live", _candidates_frame([("TCS", 0.8, 2.1)]))
    store.save_btst_candidates(
        "2026-09-09", "retrospective",
        _candidates_frame([("TCS", 0.8, 2.1), ("WIPRO", 0.6, 1.1)]),
    )

    assert len(store.btst_candidates_for("2026-09-09", "live")) == 1
    assert len(store.btst_candidates_for("2026-09-09", "retrospective")) == 2


def test_saving_again_for_the_same_day_replaces_not_appends():
    store.save_btst_candidates("2026-09-09", "live", _candidates_frame([("TCS", 0.8, 2.1)]))
    store.save_btst_candidates("2026-09-09", "live", _candidates_frame([("WIPRO", 0.6, 1.1)]))

    read_back = store.btst_candidates_for("2026-09-09", "live")
    assert list(read_back["symbol"]) == ["WIPRO"]


def test_empty_candidate_list_clears_any_previous_save():
    store.save_btst_candidates("2026-09-09", "live", _candidates_frame([("TCS", 0.8, 2.1)]))
    written = store.save_btst_candidates("2026-09-09", "live", pd.DataFrame())

    assert written == 0
    assert store.btst_candidates_for("2026-09-09", "live").empty


# ---------------------------------------------------------------------------
# Transition detection - the reason snapshots are stored at all
# ---------------------------------------------------------------------------


def test_first_poll_reports_nothing_as_new():
    """With no baseline every match would look like an event - that would fire a burst of
    false alerts on startup."""
    results = [_FakeResult("Breakaway Below PDL", ["ZYDUSLIFE", "VEDL"])]
    assert store.record_hits(results, datetime(2026, 9, 3, 12, 1)) == {}


def test_second_poll_reports_only_the_newly_appeared_symbol():
    store.record_hits(
        [_FakeResult("Breakaway Below PDL", ["ZYDUSLIFE"])], datetime(2026, 9, 3, 12, 1)
    )
    new = store.record_hits(
        [_FakeResult("Breakaway Below PDL", ["ZYDUSLIFE", "VEDL"])],
        datetime(2026, 9, 3, 12, 31),
    )
    assert new == {"Breakaway Below PDL": ["VEDL"]}


def test_symbol_still_matching_is_not_reported_again():
    new = {}
    for stamp in (datetime(2026, 9, 3, 12, 1), datetime(2026, 9, 3, 12, 31)):
        new = store.record_hits([_FakeResult("Breakaway Below PDL", ["ZYDUSLIFE"])], stamp)
    assert new == {}


def test_a_symbol_that_leaves_and_returns_SAME_DAY_does_not_count_as_new_again():
    """2026-09-17 regression: comparing only against the immediately preceding poll let a
    symbol that flickered out and back in re-qualify as "new" every time, firing a duplicate
    watchlist alert (BHARATFORG, 11:31 and 12:31, same scan, same day). Dedup must be scoped
    to the whole trading day, not just the last poll."""
    store.record_hits([_FakeResult("X", ["ZYDUSLIFE"])], datetime(2026, 9, 3, 12, 1))
    store.record_hits([_FakeResult("X", ["VEDL"])], datetime(2026, 9, 3, 12, 31))
    new = store.record_hits([_FakeResult("X", ["ZYDUSLIFE"])], datetime(2026, 9, 3, 13, 1))
    assert new == {}


def test_a_symbol_that_leaves_and_returns_a_LATER_DAY_counts_as_new_again():
    """The dedup window resets at midnight - a fresh trading day starts with a clean slate.
    Day 2's own first poll is exempt from "new" reporting regardless (the cold-start rule -
    see test_first_poll_reports_nothing_as_new), so the reset is proven on day 2's SECOND
    poll instead."""
    store.record_hits([_FakeResult("X", ["ZYDUSLIFE"])], datetime(2026, 9, 3, 12, 1))
    store.record_hits([_FakeResult("X", ["VEDL"])], datetime(2026, 9, 4, 9, 20))  # day 2 poll 1
    new = store.record_hits([_FakeResult("X", ["ZYDUSLIFE"])], datetime(2026, 9, 4, 9, 50))
    assert new == {"X": ["ZYDUSLIFE"]}


def test_hits_are_tracked_per_scan_not_globally():
    store.record_hits([_FakeResult("Scan A", ["ZYDUSLIFE"])], datetime(2026, 9, 3, 12, 1))
    new = store.record_hits(
        [_FakeResult("Scan A", ["ZYDUSLIFE"]), _FakeResult("Scan B", ["ZYDUSLIFE"])],
        datetime(2026, 9, 3, 12, 31),
    )
    assert new == {"Scan B": ["ZYDUSLIFE"]}


# ---------------------------------------------------------------------------
# Fetcher table identification
# ---------------------------------------------------------------------------


def test_picks_the_scanner_table_out_of_a_page_full_of_tables():
    """The live page carries a dozen layout and glossary tables; the scanner one is
    identified by its columns, never by a CSS class or id."""
    tables = [
        {"head": ["Previous Day POC", "Yesterday's Point of Control..."], "body": [["a", "b"]]},
        {"head": ["Avg.Range", "168.51"], "body": [["x", "y"]]},
        {
            "head": ["Name", "Sector", "Opening", "IB %", "OpenDrive", "Day Type", "TPO Pos"],
            "body": [["ZYDUSLIFE", "Pharma", "In Value", "36.97", "—", "Trend ↓", "Near Lo"]],
        },
    ]
    frame = fetcher._pick_scanner_table(tables)
    assert list(frame["Name"]) == ["ZYDUSLIFE"]
    assert "Day Type" in frame.columns


def test_ragged_rows_are_dropped_rather_than_shifting_columns():
    tables = [
        {
            "head": ["Name", "Sector", "Opening", "IB %", "OpenDrive", "Day Type"],
            "body": [
                ["ZYDUSLIFE", "Pharma", "In Value", "36.97", "—", "Trend ↓"],
                ["BROKEN", "Pharma"],  # a colspan/spacer row must not shift the good one
            ],
        }
    ]
    frame = fetcher._pick_scanner_table(tables)
    assert len(frame) == 1
    assert frame.iloc[0]["IB %"] == "36.97"


def test_raises_when_no_table_looks_like_a_scanner():
    with pytest.raises(fetcher.FetchError):
        fetcher._pick_scanner_table([{"head": ["Foo", "Bar"], "body": [["1", "2"]]}])


def test_raises_when_the_scanner_table_has_no_rows():
    """A header with an empty body means the page had not finished rendering."""
    with pytest.raises(fetcher.FetchError):
        fetcher._pick_scanner_table(
            [{"head": ["Name", "Opening", "IB %", "OpenDrive", "Day Type"], "body": []}]
        )


def test_unknown_scanner_name_is_rejected():
    with pytest.raises(ValueError):
        fetcher.fetch_snapshot("not_a_scanner")
