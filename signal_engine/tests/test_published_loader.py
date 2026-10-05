"""Daily loader over a published dataset version, and version resolution (Phase 4)."""
from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from signal_engine.backtest import data
from signal_engine.backtest.data_readiness import snapshot
from signal_engine.tests.dq_fixtures import weekdays
from signal_engine.tests.published_fixtures import publish_fixture

DAYS = [date.fromisoformat(d) for d in weekdays("2025-01-01", "2025-06-30")]


@pytest.fixture
def version(tmp_path):
    # BBB has a genuine 30% one-day fall: a published (already adjusted) series keeps it.
    def rows(s, i, d):
        base = 100.0 + i * 0.1
        return base * (0.7 if s == "BBB" and i >= 60 else 1.0)
    return publish_fixture(tmp_path, ("AAA", "BBB"), DAYS, rows=rows)


def test_frames_match_the_published_bars(version):
    root, git_dir, vid, _ = version
    with snapshot.open_version(vid, root=root, git_dir=git_dir) as con:
        frames = data.from_published_daily(con, min_sessions=10)
        bars = con.execute("SELECT symbol, timestamp, close FROM market_data "
                           "WHERE symbol = 'AAA' ORDER BY timestamp").df()
    a = frames["AAA"]
    assert list(a.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert len(a) == len(bars) == len(DAYS)
    assert a.index[0] == pd.Timestamp(DAYS[0], tz="Asia/Kolkata")
    assert a["Close"].iloc[-1] == pytest.approx(bars["close"].iloc[-1], rel=1e-6)


def test_no_heuristic_split_adjustment_on_published_data(version):
    root, git_dir, vid, _ = version
    with snapshot.open_version(vid, root=root, git_dir=git_dir) as con:
        b = data.from_published_daily(con, symbols=["BBB"], min_sessions=10)["BBB"]
    # the 30% fall on session 60 must survive (the legacy loader would rescale it as a split)
    assert b["Close"].iloc[60] / b["Close"].iloc[59] == pytest.approx(0.7 * 106.0 / 105.9, rel=1e-4)


def test_end_limits_the_sessions(version):
    root, git_dir, vid, _ = version
    with snapshot.open_version(vid, root=root, git_dir=git_dir) as con:
        a = data.from_published_daily(con, symbols=["AAA"], end=DAYS[49].isoformat(),
                                      min_sessions=10)["AAA"]
    assert len(a) == 50


def test_min_sessions_drops_short_symbols(version):
    root, git_dir, vid, _ = version
    with snapshot.open_version(vid, root=root, git_dir=git_dir) as con:
        frames = data.from_published_daily(con, min_sessions=len(DAYS) + 1, symbols=["AAA"],
                                           allow_empty=True)
    assert frames == {}


def test_symbol_order_is_deterministic(version):
    root, git_dir, vid, _ = version
    with snapshot.open_version(vid, root=root, git_dir=git_dir) as con:
        assert list(data.from_published_daily(con, min_sessions=10)) == ["AAA", "BBB"]


class TestResolveVersion:
    def test_latest_published_is_resolved(self, version):
        root, git_dir, vid, _ = version
        assert snapshot.resolve_version(None, root=root, git_dir=git_dir)["dataset_version"] == vid

    def test_explicit_version_is_resolved(self, version):
        root, git_dir, vid, _ = version
        assert snapshot.resolve_version(vid, root=root, git_dir=git_dir)["root_hash"]

    def test_missing_version_is_refused(self, version):
        root, git_dir, _, _ = version
        with pytest.raises(snapshot.SnapshotError, match="not found"):
            snapshot.resolve_version("nope", root=root, git_dir=git_dir)

    def test_non_published_status_is_refused(self, version):
        root, git_dir, vid, _ = version
        g = git_dir / f"{vid}.json"
        doc = json.loads(g.read_text())
        doc["status"] = "legacy"
        g.write_text(json.dumps(doc))
        with pytest.raises(snapshot.SnapshotError):
            snapshot.resolve_version(vid, root=root, git_dir=git_dir)

    def test_no_published_version_at_all(self, tmp_path):
        with pytest.raises(snapshot.SnapshotError, match="no published"):
            snapshot.resolve_version(None, root=tmp_path / "r", git_dir=tmp_path / "g")


def test_open_version_can_scope_symbols_and_end(version):
    root, git_dir, vid, _ = version
    end_ts = int(pd.Timestamp(DAYS[9]).timestamp())
    with snapshot.open_version(vid, root=root, git_dir=git_dir, symbols=["AAA"],
                               end_ts=end_ts) as con:
        n, syms = con.execute("SELECT COUNT(*), COUNT(DISTINCT symbol) FROM market_data").fetchone()
    assert (n, syms) == (10, 1)


def test_end_with_a_closing_time_does_not_read_the_next_session(version):
    """Review H1: record_run passes 'YYYY-MM-DD 23:59:59'; the next session must stay out."""
    root, git_dir, vid, _ = version
    with snapshot.open_version(vid, root=root, git_dir=git_dir) as con:
        a = data.from_published_daily(con, symbols=["AAA"], end=f"{DAYS[49]} 23:59:59",
                                      min_sessions=10)["AAA"]
    assert len(a) == 50 and a.index[-1].date() == DAYS[49]


def test_no_symbol_with_enough_history_is_a_typed_error(version):
    root, git_dir, vid, _ = version
    with snapshot.open_version(vid, root=root, git_dir=git_dir) as con, \
            pytest.raises(data.NoPublishedData):
        data.from_published_daily(con, symbols=["AAA"], min_sessions=len(DAYS) + 1)


def test_open_version_refuses_an_unexpected_root_hash(version):
    """Review L6: the opened version must be the one the run recorded."""
    root, git_dir, vid, _ = version
    with pytest.raises(snapshot.SnapshotError, match="root hash"), \
            snapshot.open_version(vid, root=root, git_dir=git_dir, expected_root="0" * 64):
        pass
