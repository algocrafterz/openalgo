"""Disk retention for the BreakingTrade collector's two unbounded stores.

Logs are rotated and retained (logger_setup.py); these two were not, and they are the ones
that actually grow:

  breakingtrade.db          26 MB and climbing - one `snapshots` row per symbol per poll,
                            roughly 200 symbols x ~40 polls a day, every trading day since
                            inception. Deleting rows alone does not shrink the file (SQLite
                            keeps freed pages), so a VACUUM follows.
  breakingtrade_profile/    125 MB, and none of it is data - it is the Chromium profile the
                            fetcher drives the vendor site with, and 124 MB of that is
                            Code Cache, Cache and Service Worker. The LOGIN SESSION lives in
                            Cookies / Local Storage / Login Data, which is why the profile is
                            persistent in the first place, so those are never touched.

Retention is deliberately long. `snapshots` is the evidence base for "are these scans worth
trading at all" (see store.py's module docstring) - the point of keeping it is a sample that
spans regimes, so this trims what is beyond useful analysis rather than what is beyond
convenient.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import date, timedelta

from signal_engine.analysis.breakingtrade import fetcher, store

#: Snapshot/scan-hit history kept, in days. A full quarter plus a margin: long enough that a
#: regime comparison is still possible offline, short enough that the file does not grow
#: without limit. Trade-outcome tables (btst_candidates, alerts) are NOT pruned - they are the
#: scored record, they are small, and losing them loses the experiment.
DEFAULT_RETENTION_DAYS = 120

#: Chromium profile subdirectories that are pure cache. Everything else under the profile -
#: Cookies, Local Storage, Login Data, Preferences - carries the logged-in session and must
#: survive, or the next poll lands on a login page.
_CACHE_SUBDIRS = (
    "Cache",
    "Code Cache",
    "GPUCache",
    "DawnWebGPUCache",
    "DawnGraphiteCache",
    "Service Worker/CacheStorage",
    "Service Worker/ScriptCache",
    "Shared Dictionary",
)

#: Tables pruned by captured_at. Keyed here rather than discovered so adding a table is a
#: deliberate decision about whether its history is evidence or noise.
_PRUNABLE = ("snapshots", "scan_hits")


def prune_database(
    db_path: str = None, retention_days: int = DEFAULT_RETENTION_DAYS, vacuum: bool = True
) -> dict:
    """Delete snapshot/scan-hit rows older than the window. Returns {table: rows_deleted}.

    VACUUM is what actually returns the space: SQLite marks deleted pages free and reuses
    them, so the file never shrinks on DELETE alone.
    """
    path = db_path or store._DB_PATH
    if not os.path.exists(path):
        return {}

    cutoff = (date.today() - timedelta(days=retention_days)).isoformat()
    deleted = {}
    conn = sqlite3.connect(path, timeout=30)
    try:
        existing = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        for table in _PRUNABLE:
            if table not in existing:
                continue
            cur = conn.execute(
                f"DELETE FROM {table} WHERE date(captured_at) < ?", (cutoff,)  # noqa: S608
            )
            deleted[table] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
        if vacuum and any(deleted.values()):
            conn.execute("VACUUM")
    finally:
        conn.close()
    return deleted


def prune_browser_cache(profile_dir: str = None) -> int:
    """Delete the Chromium profile's cache directories. Returns bytes freed.

    Never touches the session: Cookies, Local Storage and Login Data are what make the
    profile persistent, and removing them means the next poll hits a login page.
    """
    root = profile_dir or fetcher.PROFILE_DIR
    if not os.path.isdir(root):
        return 0

    freed = 0
    for profile in os.listdir(root):
        base = os.path.join(root, profile)
        if not os.path.isdir(base):
            continue
        for subdir in _CACHE_SUBDIRS:
            target = os.path.join(base, *subdir.split("/"))
            if not os.path.isdir(target):
                continue
            freed += _directory_size(target)
            shutil.rmtree(target, ignore_errors=True)
    return freed


def _directory_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def run(retention_days: int = DEFAULT_RETENTION_DAYS, dry_run: bool = False) -> str:
    """Both prunes, as a one-line report for the EOD job or the CLI."""
    if dry_run:
        db_size = os.path.getsize(store._DB_PATH) if os.path.exists(store._DB_PATH) else 0
        cache = _directory_size(fetcher.PROFILE_DIR) if os.path.isdir(fetcher.PROFILE_DIR) else 0
        return (
            f"[dry run] breakingtrade.db is {db_size / 1e6:.1f} MB, browser profile is "
            f"{cache / 1e6:.1f} MB; would prune rows older than {retention_days} days"
        )

    deleted = prune_database(retention_days=retention_days)
    freed = prune_browser_cache()
    rows = ", ".join(f"{table} -{count}" for table, count in sorted(deleted.items())) or "none"
    return f"Pruned rows: {rows}. Browser cache freed: {freed / 1e6:.1f} MB"
