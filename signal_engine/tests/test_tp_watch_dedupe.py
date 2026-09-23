"""The TP-level dedupe compared two timestamp formats and so never matched anything.

trades.db writes `executed_at` with datetime.isoformat() -> "2026-09-11T11:46:43.327851".
breakingtrade.db writes `created_at` with a space          -> "2026-09-11 14:52:16".

_last_level_hit() filtered with `created_at >= executed_at` as a STRING comparison. At index
10 that is ' ' (0x20) against 'T' (0x54), so a LATER alert always compares as SMALLER and the
filter matched nothing. _last_level_hit() therefore always returned None, _next_level(None)
always returned "TP1", and the watcher re-sent TP1 on every poll for as long as the position
was open - five times for AXISBANK on 2026-09-11, 14:52 to 14:54.

It was masked that day because the position had already been closed by the no-progress gate,
so the engine answered "no open position" to all five. With a live position each one carries
ExitQtyPct 50, so the engine would have exited half the remaining position five times over.
Structurally incapable of working since it was written - the fetch_bars UTC failure mode.
"""

from datetime import datetime
from unittest.mock import patch

import pytest

from signal_engine.analysis.breakingtrade import tp_watch


@pytest.fixture
def store(tmp_path, monkeypatch):
    from signal_engine.analysis.breakingtrade import alerts
    from signal_engine.analysis.breakingtrade import store as _store

    monkeypatch.setattr(_store, "_DB_PATH", str(tmp_path / "bt.db"))
    with alerts._connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS alerts (
            created_at TEXT, kind TEXT, symbol TEXT, direction TEXT, scan TEXT,
            message TEXT, delivered INTEGER, message_id INTEGER, strategy TEXT)""")
    return alerts


def _record(alerts, created_at, level, delivered=1, symbol="AXISBANK", strategy="BREAKINGTRADE"):
    with alerts._connect() as conn:
        conn.execute(
            "INSERT INTO alerts (created_at, kind, symbol, direction, scan, message, delivered, "
            "strategy) VALUES (?, 'tp_hit', ?, 'LONG', ?, '', ?, ?)",
            (created_at, symbol, level, delivered, strategy),
        )
        conn.commit()


ENTRY_ISO = "2026-09-11T11:46:43.327851"   # as trades.db writes it
ALERT_SPACE = "2026-09-11 14:52:16"        # as breakingtrade.db writes it


class TestLastLevelHitAcrossTimestampFormats:
    def test_a_space_separated_alert_matches_an_iso_entry_time(self, store):
        """The whole bug: these two describe the same day, three hours apart."""
        _record(store, ALERT_SPACE, "TP1")
        assert tp_watch._last_level_hit("AXISBANK", ENTRY_ISO, "BREAKINGTRADE") == "TP1"

    def test_an_alert_from_before_the_entry_is_still_excluded(self):
        """The filter has a real job - a previous position's TP must not count."""
        pass  # covered by test_an_earlier_alert_is_ignored below

    def test_an_earlier_alert_is_ignored(self, store):
        _record(store, "2026-09-11 09:30:00", "TP1")
        assert tp_watch._last_level_hit("AXISBANK", ENTRY_ISO, "BREAKINGTRADE") is None

    def test_the_highest_level_wins(self, store):
        _record(store, ALERT_SPACE, "TP1")
        _record(store, "2026-09-11 14:55:00", "TP1.5")
        assert tp_watch._last_level_hit("AXISBANK", ENTRY_ISO, "BREAKINGTRADE") == "TP1.5"

    def test_an_undelivered_alert_does_not_count(self, store):
        """If Telegram never delivered it, the engine never got the exit instruction."""
        _record(store, ALERT_SPACE, "TP1", delivered=0)
        assert tp_watch._last_level_hit("AXISBANK", ENTRY_ISO, "BREAKINGTRADE") is None

    def test_another_symbol_does_not_count(self, store):
        _record(store, ALERT_SPACE, "TP1", symbol="SBIN")
        assert tp_watch._last_level_hit("AXISBANK", ENTRY_ISO, "BREAKINGTRADE") is None

    def test_both_formats_work_for_the_entry_side_too(self, store):
        _record(store, ALERT_SPACE, "TP1")
        assert tp_watch._last_level_hit("AXISBANK", "2026-09-11 11:46:43", "BREAKINGTRADE") == "TP1"

    def test_an_unparseable_entry_time_does_not_crash(self, store):
        _record(store, ALERT_SPACE, "TP1")
        tp_watch._last_level_hit("AXISBANK", "not-a-timestamp", "BREAKINGTRADE")


class TestTheLadderAdvances:
    def test_after_tp1_the_next_level_is_tp1_5(self, store):
        _record(store, ALERT_SPACE, "TP1")
        last = tp_watch._last_level_hit("AXISBANK", ENTRY_ISO, "BREAKINGTRADE")
        assert tp_watch._next_level(last) == "TP1.5"

    def test_after_the_final_level_nothing_more_is_sent(self, store):
        for ts, lvl in (("2026-09-11 14:52:16", "TP1"),
                        ("2026-09-11 14:55:00", "TP1.5"),
                        ("2026-09-11 14:58:00", "TP2")):
            _record(store, ts, lvl)
        last = tp_watch._last_level_hit("AXISBANK", ENTRY_ISO, "BREAKINGTRADE")
        assert tp_watch._next_level(last) is None


class TestStrategyDisambiguation:
    """2026-09-23: BREAKINGTRADE and BREAKINGTRADE-WATCHLIST can both be open on the same
    symbol at once (confirmed real: BANDHANBNK, 2026-09-23) - each must track its OWN TP
    progress, not a shared one keyed by symbol alone."""

    def test_two_strategies_on_the_same_symbol_track_independent_progress(self, store):
        _record(store, ALERT_SPACE, "TP1", strategy="BREAKINGTRADE")
        _record(store, ALERT_SPACE, "TP1", strategy="BREAKINGTRADE-WATCHLIST")
        _record(store, "2026-09-11 14:55:00", "TP1.5", strategy="BREAKINGTRADE-WATCHLIST")

        assert tp_watch._last_level_hit("AXISBANK", ENTRY_ISO, "BREAKINGTRADE") == "TP1"
        assert tp_watch._last_level_hit("AXISBANK", ENTRY_ISO, "BREAKINGTRADE-WATCHLIST") == "TP1.5"
