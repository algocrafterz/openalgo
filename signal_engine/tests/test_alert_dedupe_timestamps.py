"""Every "have I already done this?" check, across both timestamp formats.

trades.db writes datetime.isoformat() -> "2026-09-11T11:46:43.327851"
breakingtrade.db writes with a space  -> "2026-09-11 14:52:16"
At index 10, ' ' (0x20) sorts BELOW 'T' (0x54), so a LATER alert compared as SMALLER and
every raw-string `created_at >= ?` filter matched nothing. Three separate checks always
answered "no, not yet":

    tp_watch._last_level_hit    5x TP1 for AXISBANK on 2026-09-11 (each ExitQtyPct 50)
    flip_watch._already_warned  4x ADANIENSOL, 4x ADANIENT
    entry_watch._already_confirmed

They now share alerts.SINCE_CLAUSE, which parses both sides.
"""

import pytest

from signal_engine.analysis.breakingtrade import alerts, entry_watch, flip_watch, store, tp_watch

ENTRY_ISO = "2026-09-11T11:46:43.327851"   # trades.db
ALERT_SPACE = "2026-09-11 14:52:16"        # breakingtrade.db
EARLIER = "2026-09-11 09:30:00"


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", str(tmp_path / "bt.db"))
    with alerts._connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS alerts (
            created_at TEXT, kind TEXT, symbol TEXT, direction TEXT, scan TEXT,
            message TEXT, delivered INTEGER, message_id INTEGER, strategy TEXT)""")


def _record(kind, created_at, symbol="AXISBANK", scan="TP1", delivered=1, strategy="BREAKINGTRADE"):
    with alerts._connect() as conn:
        conn.execute(
            "INSERT INTO alerts (created_at, kind, symbol, direction, scan, message, delivered, "
            "strategy) VALUES (?, ?, ?, 'LONG', ?, '', ?, ?)",
            (created_at, kind, symbol, scan, delivered, strategy),
        )
        conn.commit()


class TestTheSharedClause:
    def test_it_parses_both_sides(self):
        assert "datetime(created_at)" in alerts.SINCE_CLAUSE
        assert "datetime(?)" in alerts.SINCE_CLAUSE


class TestFlipWarningFiresOnce:
    def test_a_warning_after_the_entry_is_seen(self):
        """The production bug: 4 flip alerts each for ADANIENSOL and ADANIENT."""
        _record("structure_flip", ALERT_SPACE, symbol="ADANIENSOL")
        assert flip_watch._already_warned("ADANIENSOL", ENTRY_ISO) is True

    def test_a_warning_from_before_the_entry_does_not_count(self):
        _record("structure_flip", EARLIER, symbol="ADANIENSOL")
        assert flip_watch._already_warned("ADANIENSOL", ENTRY_ISO) is False

    def test_no_warning_at_all_reads_as_not_warned(self):
        assert flip_watch._already_warned("ADANIENSOL", ENTRY_ISO) is False

    def test_another_symbol_does_not_count(self):
        _record("structure_flip", ALERT_SPACE, symbol="SBIN")
        assert flip_watch._already_warned("ADANIENSOL", ENTRY_ISO) is False


class TestEntryConfirmationFiresOnce:
    def test_a_confirmation_after_the_reference_is_seen(self):
        _record("trade_signal", ALERT_SPACE, symbol="TMPV")
        assert entry_watch._already_confirmed("TMPV", ENTRY_ISO) is True

    def test_one_from_before_does_not_count(self):
        _record("trade_signal", EARLIER, symbol="TMPV")
        assert entry_watch._already_confirmed("TMPV", ENTRY_ISO) is False


class TestTpLadderStillDedupes:
    def test_tp1_is_seen_so_the_ladder_advances(self):
        _record("tp_hit", ALERT_SPACE, scan="TP1")
        assert tp_watch._last_level_hit("AXISBANK", ENTRY_ISO, "BREAKINGTRADE") == "TP1"
        assert tp_watch._next_level("TP1") == "TP1.5"

    def test_an_undelivered_attempt_is_retried(self):
        _record("tp_hit", ALERT_SPACE, scan="TP1", delivered=0)
        assert tp_watch._last_level_hit("AXISBANK", ENTRY_ISO, "BREAKINGTRADE") is None
