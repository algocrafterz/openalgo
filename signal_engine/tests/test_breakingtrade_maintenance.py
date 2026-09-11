"""L4: the two BreakingTrade stores that grow without limit now have a retention pass.

breakingtrade.db was 26 MB (one snapshots row per symbol per poll, every trading day since
inception) and data/breakingtrade_profile 125 MB, of which 124 MB is Chromium cache and none
is data. Logs are rotated and retained; these two were not.
"""

import os
import sqlite3
from datetime import date, timedelta

import pytest

from signal_engine.analysis.breakingtrade import maintenance


def _db(tmp_path, rows):
    path = str(tmp_path / "bt.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE snapshots (captured_at TEXT, symbol TEXT)")
    conn.execute("CREATE TABLE scan_hits (captured_at TEXT, scan TEXT)")
    conn.execute("CREATE TABLE btst_candidates (trade_day TEXT, symbol TEXT)")
    conn.executemany("INSERT INTO snapshots VALUES (?, ?)", rows)
    conn.execute("INSERT INTO btst_candidates VALUES ('2020-01-01', 'OLD')")
    conn.commit()
    conn.close()
    return path


def _iso(days_ago):
    return (date.today() - timedelta(days=days_ago)).isoformat() + "T10:00:00"


class TestPruneDatabase:
    def test_rows_past_the_window_are_deleted(self, tmp_path):
        path = _db(tmp_path, [(_iso(400), "OLD"), (_iso(5), "NEW")])
        deleted = maintenance.prune_database(path, retention_days=120)
        assert deleted["snapshots"] == 1
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT symbol FROM snapshots").fetchall() == [("NEW",)]

    def test_rows_inside_the_window_are_kept(self, tmp_path):
        path = _db(tmp_path, [(_iso(10), "A"), (_iso(20), "B")])
        assert maintenance.prune_database(path, retention_days=120)["snapshots"] == 0

    def test_the_scored_record_is_never_pruned(self, tmp_path):
        """btst_candidates is the experiment's outcome table - small, and losing it loses
        the experiment."""
        path = _db(tmp_path, [(_iso(400), "OLD")])
        maintenance.prune_database(path, retention_days=120)
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT COUNT(*) FROM btst_candidates").fetchone()[0] == 1

    def test_a_missing_database_is_not_an_error(self, tmp_path):
        assert maintenance.prune_database(str(tmp_path / "nope.db")) == {}

    def test_a_table_that_does_not_exist_is_skipped(self, tmp_path):
        path = str(tmp_path / "partial.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE snapshots (captured_at TEXT, symbol TEXT)")
        conn.commit()
        conn.close()
        deleted = maintenance.prune_database(path, retention_days=120)
        assert "scan_hits" not in deleted

    def test_vacuum_can_be_skipped(self, tmp_path):
        path = _db(tmp_path, [(_iso(400), "OLD")])
        maintenance.prune_database(path, retention_days=120, vacuum=False)  # must not raise


class TestPruneBrowserCache:
    def _profile(self, tmp_path):
        root = tmp_path / "profile"
        default = root / "Default"
        for sub in ("Cache", "Code Cache", "Service Worker/CacheStorage"):
            d = default / sub
            d.mkdir(parents=True)
            (d / "blob").write_bytes(b"x" * 1024)
        (default / "Cookies").write_bytes(b"session")
        (default / "Local Storage").mkdir()
        (default / "Local Storage" / "leveldb").write_bytes(b"session")
        return root

    def test_cache_directories_are_removed(self, tmp_path):
        root = self._profile(tmp_path)
        freed = maintenance.prune_browser_cache(str(root))
        assert freed >= 3 * 1024
        assert not (root / "Default" / "Cache").exists()
        assert not (root / "Default" / "Code Cache").exists()

    def test_the_login_session_survives(self, tmp_path):
        """Removing these means the next poll lands on a login page instead of the scanner."""
        root = self._profile(tmp_path)
        maintenance.prune_browser_cache(str(root))
        assert (root / "Default" / "Cookies").exists()
        assert (root / "Default" / "Local Storage" / "leveldb").exists()

    def test_a_missing_profile_is_not_an_error(self, tmp_path):
        assert maintenance.prune_browser_cache(str(tmp_path / "nope")) == 0


class TestRunReport:
    def test_dry_run_changes_nothing_and_reports_sizes(self, tmp_path, monkeypatch):
        path = _db(tmp_path, [(_iso(400), "OLD")])
        monkeypatch.setattr(maintenance.store, "_DB_PATH", path)
        monkeypatch.setattr(maintenance.fetcher, "PROFILE_DIR", str(tmp_path / "nope"))
        report = maintenance.run(dry_run=True)
        assert "dry run" in report
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1

    def test_run_reports_what_it_removed(self, tmp_path, monkeypatch):
        path = _db(tmp_path, [(_iso(400), "OLD")])
        monkeypatch.setattr(maintenance.store, "_DB_PATH", path)
        monkeypatch.setattr(maintenance.fetcher, "PROFILE_DIR", str(tmp_path / "nope"))
        assert "snapshots -1" in maintenance.run()
