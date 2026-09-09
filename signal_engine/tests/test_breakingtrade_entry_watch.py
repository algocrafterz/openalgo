"""Multi-poll entry-confirmation retry (entry_watch.py).

Regression guard for 2026-09-09: __main__.py's _emit_trade_signals() only ever checked a scan
pick on the single poll it first appeared on, but trigger.entry_trigger() needs a LATER bar to
close beyond the signal bar's level - a confirmation that essentially never exists yet at the
moment a symbol is first flagged. Replaying that day's real scan output found 22 of 31 flagged
symbols would have genuinely confirmed if simply rechecked on later polls. entry_watch.check()
is that recheck, bounded by CONFIRMATION_WINDOW so a stale pick eventually stops being retried.
"""

from datetime import datetime, timedelta

import pandas as pd
import pytest

from signal_engine.analysis.breakingtrade import alerts, entry_watch, store, trigger


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", str(tmp_path / "breakingtrade.db"))
    monkeypatch.setattr(alerts, "send", lambda text, kind=None, monospace=False: (True, 999))


def _record_hit(symbol, direction, scan, captured_at, is_new=1):
    with store._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO scan_hits (captured_at, scan, direction, symbol, is_new) "
            "VALUES (?, ?, ?, ?, ?)",
            (store._iso(captured_at), scan, direction, symbol, is_new),
        )


def _bars(start, rows):
    """rows: list of (high, low, close), one per 5-minute bar from `start` (today's date)."""
    base = pd.Timestamp(f"2026-09-09 {start}")
    return pd.DataFrame(
        [
            {"timestamp": base + pd.Timedelta(minutes=5 * i), "high": h, "low": low, "close": c}
            for i, (h, low, c) in enumerate(rows)
        ]
    )


#: A full Initial Balance (09:15-10:15, 12 bars, ranging 95-105) followed by one bar that
#: closes above the IB high - exactly what trigger.entry_trigger() needs to confirm a LONG.
_CONFIRMING_BARS = _bars("09:15", [(105, 95, 100)] * 12 + [(112, 99, 111)])
#: Same IB, but nothing after it ever closes beyond the level - never confirms.
_UNCONFIRMED_BARS = _bars("09:15", [(105, 95, 100)] * 12 + [(101, 99, 100)] * 3)


class TestPendingPicks:
    def test_fresh_new_hit_is_pending(self):
        captured_at = datetime(2026, 9, 9, 10, 25)
        _record_hit("TCS", "up", "The Breakdown", captured_at)

        picks = entry_watch._pending_picks(captured_at + timedelta(minutes=5))

        assert [p[0] for p in picks] == ["TCS"]

    def test_non_new_hit_is_not_pending(self):
        captured_at = datetime(2026, 9, 9, 10, 25)
        _record_hit("TCS", "up", "The Breakdown", captured_at, is_new=0)

        assert entry_watch._pending_picks(captured_at + timedelta(minutes=5)) == []

    def test_pick_older_than_the_confirmation_window_expires(self):
        captured_at = datetime(2026, 9, 9, 9, 20)
        _record_hit("TCS", "up", "The Breakdown", captured_at)

        now = captured_at + entry_watch.CONFIRMATION_WINDOW + timedelta(minutes=1)

        assert entry_watch._pending_picks(now) == []

    def test_pick_just_inside_the_window_is_still_pending(self):
        captured_at = datetime(2026, 9, 9, 9, 20)
        _record_hit("TCS", "up", "The Breakdown", captured_at)

        now = captured_at + entry_watch.CONFIRMATION_WINDOW - timedelta(minutes=1)

        assert [p[0] for p in entry_watch._pending_picks(now)] == ["TCS"]

    def test_already_confirmed_pick_drops_out(self):
        captured_at = datetime(2026, 9, 9, 10, 25)
        _record_hit("TCS", "up", "The Breakdown", captured_at)
        alerts.record("trade_signal", "already confirmed", symbol="TCS",
                      direction="up", scan="BREAKINGTRADE")

        assert entry_watch._pending_picks(captured_at + timedelta(minutes=5)) == []

    def test_first_seen_is_the_earliest_poll_the_symbol_appeared_new(self):
        first = datetime(2026, 9, 9, 9, 20)
        _record_hit("TCS", "up", "The Breakdown", first)
        # A later poll where it is somehow flagged new again (e.g. a different scan) must not
        # push first_seen forward - the confirmation window is anchored to when it was FIRST
        # called, not most recently.
        _record_hit("TCS", "up", "The Breakdown", first + timedelta(minutes=15))

        picks = entry_watch._pending_picks(first + timedelta(minutes=20))

        assert picks[0][3] == first


class TestCheck:
    def test_no_pending_picks_returns_zero(self):
        assert entry_watch.check(datetime(2026, 9, 9, 10, 25)) == 0

    def test_pending_pick_that_confirms_emits_a_trade_signal(self, monkeypatch):
        captured_at = datetime(2026, 9, 9, 9, 20)
        _record_hit("TCS", "up", "The Breakdown", captured_at)
        monkeypatch.setattr(
            "signal_engine.analysis.breakingtrade.validate.fetch_bars",
            lambda symbol, day, exchange="NSE": _CONFIRMING_BARS,
        )

        emitted = entry_watch.check(captured_at + timedelta(minutes=15))

        assert emitted == 1
        with alerts._connect() as conn:
            row = conn.execute(
                "SELECT symbol, message FROM alerts WHERE kind = 'trade_signal'"
            ).fetchone()
        assert row[0] == "TCS"
        assert "BREAKINGTRADE" in row[1]

    def test_pending_pick_that_has_not_confirmed_yet_emits_nothing(self, monkeypatch):
        captured_at = datetime(2026, 9, 9, 9, 20)
        _record_hit("TCS", "up", "The Breakdown", captured_at)
        monkeypatch.setattr(
            "signal_engine.analysis.breakingtrade.validate.fetch_bars",
            lambda symbol, day, exchange="NSE": _UNCONFIRMED_BARS,
        )

        assert entry_watch.check(captured_at + timedelta(minutes=15)) == 0

    def test_confirmed_pick_is_not_re_emitted_on_a_later_poll(self, monkeypatch):
        captured_at = datetime(2026, 9, 9, 9, 20)
        _record_hit("TCS", "up", "The Breakdown", captured_at)
        monkeypatch.setattr(
            "signal_engine.analysis.breakingtrade.validate.fetch_bars",
            lambda symbol, day, exchange="NSE": _CONFIRMING_BARS,
        )

        first = entry_watch.check(captured_at + timedelta(minutes=15))
        second = entry_watch.check(captured_at + timedelta(minutes=30))

        assert first == 1
        assert second == 0

    def test_empty_bars_are_skipped_without_raising(self, monkeypatch):
        captured_at = datetime(2026, 9, 9, 9, 20)
        _record_hit("TCS", "up", "The Breakdown", captured_at)
        monkeypatch.setattr(
            "signal_engine.analysis.breakingtrade.validate.fetch_bars",
            lambda symbol, day, exchange="NSE": pd.DataFrame(),
        )

        assert entry_watch.check(captured_at + timedelta(minutes=15)) == 0

    def test_fetch_failure_for_one_symbol_does_not_block_the_rest(self, monkeypatch):
        captured_at = datetime(2026, 9, 9, 9, 20)
        _record_hit("BROKEN", "up", "The Breakdown", captured_at)
        _record_hit("TCS", "up", "The Breakdown", captured_at)

        def _fetch(symbol, day, exchange="NSE"):
            if symbol == "BROKEN":
                raise RuntimeError("boom")
            return _CONFIRMING_BARS

        monkeypatch.setattr(
            "signal_engine.analysis.breakingtrade.validate.fetch_bars", _fetch
        )

        assert entry_watch.check(captured_at + timedelta(minutes=15)) == 1
