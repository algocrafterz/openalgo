"""Staged TP / runner-SL trailing for BreakingTrade (tp_watch.py) - the highest-risk module in
this feature, since a wrong ExitQtyPct directly mis-sizes a real partial exit. The quantity
math is tested independently of the polling logic for that reason.
"""

import sqlite3
from datetime import datetime

import pytest

from signal_engine import parser
from signal_engine.analysis.breakingtrade import alerts, eod_summary, store, tp_watch


@pytest.fixture(autouse=True)
def isolated_dbs(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", str(tmp_path / "breakingtrade.db"))
    monkeypatch.setattr("signal_engine.analysis.breakingtrade.flip_watch._TRADES_DB",
                         str(tmp_path / "trades.db"))
    # Delivery must succeed by default here, unlike other test files - _last_level_hit's dedup
    # is gated on delivered=1 (see its docstring), so a permanently-undelivered mock would make
    # every "does it dedup" test re-fire forever regardless of whether the logic is correct.
    monkeypatch.setattr(alerts, "send", lambda text, kind=None, monospace=False: (True, 999))


def _make_trades_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT, direction TEXT, symbol TEXT,
            entry REAL, sl REAL, tp REAL, status TEXT, executed_at TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO trades (strategy, direction, symbol, entry, sl, tp, status, executed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _open_long(symbol="TCS", entry=2283.1, sl=2270.0, tp1=2296.1, executed_at="2026-09-07 09:55:04"):
    """tp1 = entry + 1.0 * (entry - sl), i.e. R = 13.1, matching trigger.stop_level()'s
    relationship between entry/sl/TP1."""
    from signal_engine.analysis.breakingtrade import flip_watch

    _make_trades_db(
        flip_watch._TRADES_DB,
        [("BREAKINGTRADE", "LONG", symbol, entry, sl, tp1, "SUCCESS", executed_at)],
    )
    return entry, sl, tp1


def _open_both_strategies_long(
    symbol="TCS", entry=2283.1, sl=2270.0, tp1=2296.1, executed_at="2026-09-07 09:55:04",
):
    """Same symbol, same entry/sl/tp1, open under BOTH strategies at once - the real
    2026-09-23 BANDHANBNK scenario this whole fix exists for."""
    from signal_engine.analysis.breakingtrade import flip_watch

    _make_trades_db(
        flip_watch._TRADES_DB,
        [
            ("BREAKINGTRADE", "LONG", symbol, entry, sl, tp1, "SUCCESS", executed_at),
            ("BREAKINGTRADE-WATCHLIST", "LONG", symbol, entry, sl, tp1, "SUCCESS", executed_at),
        ],
    )
    return entry, sl, tp1


class TestLevelSequence:
    def test_first_level_is_tp1(self):
        assert tp_watch._next_level(None) == "TP1"

    def test_progresses_through_the_sequence(self):
        assert tp_watch._next_level("TP1") == "TP1.5"
        assert tp_watch._next_level("TP1.5") == "TP2"

    def test_no_level_after_the_last_one(self):
        assert tp_watch._next_level("TP2") is None

    def test_unknown_level_returns_none_rather_than_crashing(self):
        assert tp_watch._next_level("TP99") is None


class TestExpectedPrice:
    def test_long_targets_ladder_upward_from_entry(self):
        entry, tp1 = 2283.1, 2296.1  # R = 13.0
        assert tp_watch._expected_price(entry, tp1, "TP1", "LONG") == pytest.approx(2296.1)
        assert tp_watch._expected_price(entry, tp1, "TP1.5", "LONG") == pytest.approx(2302.6)
        assert tp_watch._expected_price(entry, tp1, "TP2", "LONG") == pytest.approx(2309.1)

    def test_short_targets_ladder_downward_from_entry(self):
        entry, tp1 = 12694.0, 12656.5  # R = 37.5, short
        assert tp_watch._expected_price(entry, tp1, "TP1", "SHORT") == pytest.approx(12656.5)
        assert tp_watch._expected_price(entry, tp1, "TP2", "SHORT") == pytest.approx(12619.0)


class TestExitQtyPctConversion:
    """The math that actually sizes each partial exit. DEFAULT_SPLIT (0.50, 0.30, 0.20) means
    50%/30%/20% of the ORIGINAL position - verified by hand against what main.py's
    _resolve_exit_qty() does with each value (applies it to whatever REMAINS, not the original)."""

    def test_first_level_is_the_split_fraction_directly(self):
        # Nothing has been booked yet, so "% of remaining" == "% of original".
        assert tp_watch._exit_qty_pct_of_remaining(tp_watch.DEFAULT_SPLIT, 0) == pytest.approx(50.0)

    def test_second_level_is_rescaled_against_what_remains(self):
        # 30% of original, but only 50% of the position is still open (100 - 50 already exited)
        # -> 30/50 = 60% of what remains.
        assert tp_watch._exit_qty_pct_of_remaining(tp_watch.DEFAULT_SPLIT, 1) == pytest.approx(60.0)

    def test_final_level_always_closes_everything_left(self):
        # The literal 20% split value is irrelevant here - the last level must close 100% of
        # whatever remains, matching "no next TP level = full exit" everywhere else in main.py.
        assert tp_watch._exit_qty_pct_of_remaining(tp_watch.DEFAULT_SPLIT, 2) == pytest.approx(100.0)

    def test_a_50_50_split_halves_then_closes(self):
        split = (0.5, 0.5)
        assert tp_watch._exit_qty_pct_of_remaining(split, 0) == pytest.approx(50.0)
        assert tp_watch._exit_qty_pct_of_remaining(split, 1) == pytest.approx(100.0)

    def test_sequential_application_reconstructs_the_original_split(self):
        """End-to-end proof: applying each converted ExitQtyPct in turn, against a shrinking
        remainder exactly like main.py does, must reproduce DEFAULT_SPLIT's original fractions."""
        remaining = 1.0
        exited_fractions = []
        for i in range(len(tp_watch.DEFAULT_SPLIT)):
            pct = tp_watch._exit_qty_pct_of_remaining(tp_watch.DEFAULT_SPLIT, i) / 100.0
            exited_now = remaining * pct
            exited_fractions.append(exited_now)
            remaining -= exited_now
        assert exited_fractions == pytest.approx(list(tp_watch.DEFAULT_SPLIT))
        assert remaining == pytest.approx(0.0)


class TestLastLevelHit:
    def test_no_alerts_means_no_level_hit_yet(self):
        assert tp_watch._last_level_hit("TCS", "2026-09-07 09:55:04", "BREAKINGTRADE") is None

    def test_returns_the_highest_level_recorded(self):
        tp_watch._send_tp_hit("TCS", "LONG", 2296.1, "TP1", 50.0, "BREAKINGTRADE", "trade_signal")
        tp_watch._send_tp_hit("TCS", "LONG", 2302.6, "TP1.5", 60.0, "BREAKINGTRADE", "trade_signal")
        assert tp_watch._last_level_hit("TCS", "2026-09-07 09:55:04", "BREAKINGTRADE") == "TP1.5"


class TestCheck:
    def test_no_open_positions_sends_nothing(self):
        assert tp_watch.check(datetime(2026, 9, 7, 10, 25)) == 0

    def test_price_below_tp1_sends_nothing(self, monkeypatch):
        _open_long()
        monkeypatch.setattr(eod_summary, "fetch_ltp", lambda symbol, exchange="NSE": 2290.0)
        assert tp_watch.check(datetime(2026, 9, 7, 10, 25)) == 0

    def test_tp1_reached_sends_one_exit_at_50_percent(self, monkeypatch):
        entry, sl, tp1 = _open_long()
        monkeypatch.setattr(eod_summary, "fetch_ltp", lambda symbol, exchange="NSE": tp1)

        sent = tp_watch.check(datetime(2026, 9, 7, 10, 25))

        assert sent == 1
        with alerts._connect() as conn:
            message = conn.execute(
                "SELECT message FROM alerts WHERE kind = 'tp_hit'"
            ).fetchone()[0]
        assert "BREAKINGTRADE EXIT" in message
        assert "TPLevel: TP1" in message
        assert "ExitQtyPct: 50.0" in message

    def test_does_not_resend_tp1_once_already_hit(self, monkeypatch):
        entry, sl, tp1 = _open_long()
        monkeypatch.setattr(eod_summary, "fetch_ltp", lambda symbol, exchange="NSE": tp1)

        first = tp_watch.check(datetime(2026, 9, 7, 10, 25))
        second = tp_watch.check(datetime(2026, 9, 7, 10, 30))

        assert first == 1
        assert second == 0  # price is still only at TP1, TP1 already fired

    def test_progresses_to_tp1_5_on_a_later_poll(self, monkeypatch):
        entry, sl, tp1 = _open_long()
        r = tp1 - entry
        tp1_5_price = entry + 1.5 * r

        monkeypatch.setattr(eod_summary, "fetch_ltp", lambda symbol, exchange="NSE": tp1)
        tp_watch.check(datetime(2026, 9, 7, 10, 25))

        monkeypatch.setattr(eod_summary, "fetch_ltp", lambda symbol, exchange="NSE": tp1_5_price)
        sent = tp_watch.check(datetime(2026, 9, 7, 10, 40))

        assert sent == 1
        with alerts._connect() as conn:
            # rowid, not created_at - record() truncates to the second, and both check() calls
            # in this test can land in the same second, tying on created_at.
            message = conn.execute(
                "SELECT message FROM alerts WHERE kind = 'tp_hit' ORDER BY rowid DESC LIMIT 1"
            ).fetchone()[0]
        assert "TPLevel: TP1.5" in message
        assert "ExitQtyPct: 60.0" in message

    def test_missing_quote_sends_nothing(self, monkeypatch):
        _open_long()
        monkeypatch.setattr(eod_summary, "fetch_ltp", lambda symbol, exchange="NSE": None)
        assert tp_watch.check(datetime(2026, 9, 7, 10, 25)) == 0

    def test_undelivered_tp_hit_is_retried_not_silently_dropped(self, monkeypatch):
        """If Telegram is down when TP1 fires, the engine never got the exit instruction - the
        next poll must try again, not treat the failed attempt as done."""
        entry, sl, tp1 = _open_long()
        monkeypatch.setattr(eod_summary, "fetch_ltp", lambda symbol, exchange="NSE": tp1)
        monkeypatch.setattr(alerts, "send", lambda text, kind=None, monospace=False: (False, None))

        first = tp_watch.check(datetime(2026, 9, 7, 10, 25))
        second = tp_watch.check(datetime(2026, 9, 7, 10, 30))

        assert first == 1
        assert second == 1  # retried - TP1 was never actually delivered

    def test_short_position_fires_on_price_falling_to_target(self, monkeypatch):
        from signal_engine.analysis.breakingtrade import flip_watch

        _make_trades_db(
            flip_watch._TRADES_DB,
            [("BREAKINGTRADE", "SHORT", "MARUTI", 12694.0, 12750.0, 12656.5, "SUCCESS",
              "2026-09-07 09:20:50")],
        )
        monkeypatch.setattr(eod_summary, "fetch_ltp", lambda symbol, exchange="NSE": 12656.5)

        assert tp_watch.check(datetime(2026, 9, 7, 10, 25)) == 1


class TestBothStrategiesWatched:
    """2026-09-23: this module only ever watched BREAKINGTRADE - a BREAKINGTRADE-WATCHLIST
    position had no mechanism to ever report reaching its own stated TP. Fixed by watching
    both strategies, each on its own channel/kind and its own independent TP-ladder progress.
    """

    def test_a_watchlist_position_gets_its_own_tp_hit_alert(self, monkeypatch):
        from signal_engine.analysis.breakingtrade import flip_watch

        entry, sl, tp1 = 345.85, 341.20, 350.50
        _make_trades_db(
            flip_watch._TRADES_DB,
            [("BREAKINGTRADE-WATCHLIST", "LONG", "PFC", entry, sl, tp1, "SUCCESS",
              "2026-09-07 09:55:04")],
        )
        monkeypatch.setattr(eod_summary, "fetch_ltp", lambda symbol, exchange="NSE": tp1)

        sent = tp_watch.check(datetime(2026, 9, 7, 10, 25))

        assert sent == 1
        with alerts._connect() as conn:
            kind, strategy, message = conn.execute(
                "SELECT kind, strategy, message FROM alerts WHERE kind = 'tp_hit'"
            ).fetchone()
        assert strategy == "BREAKINGTRADE-WATCHLIST"
        assert "BREAKINGTRADE-WATCHLIST EXIT" in message

    def test_two_strategies_on_the_same_symbol_progress_independently(self, monkeypatch):
        """The real 2026-09-23 BANDHANBNK scenario: both strategies open on one symbol.
        Reaching TP1 must advance only the strategy that actually reached it."""
        entry, sl, tp1 = _open_both_strategies_long()
        monkeypatch.setattr(eod_summary, "fetch_ltp", lambda symbol, exchange="NSE": tp1)

        sent = tp_watch.check(datetime(2026, 9, 7, 10, 25))

        # Both positions are at TP1 simultaneously - one alert each, not one shared/deduped.
        assert sent == 2
        with alerts._connect() as conn:
            rows = conn.execute(
                "SELECT strategy, scan FROM alerts WHERE kind = 'tp_hit' ORDER BY strategy"
            ).fetchall()
        assert rows == [("BREAKINGTRADE", "TP1"), ("BREAKINGTRADE-WATCHLIST", "TP1")]

    def test_a_watchlist_exit_routes_to_the_watchlist_send_kind(self, monkeypatch):
        from signal_engine.analysis.breakingtrade import flip_watch

        entry, sl, tp1 = 345.85, 341.20, 350.50
        _make_trades_db(
            flip_watch._TRADES_DB,
            [("BREAKINGTRADE-WATCHLIST", "LONG", "PFC", entry, sl, tp1, "SUCCESS",
              "2026-09-07 09:55:04")],
        )
        monkeypatch.setattr(eod_summary, "fetch_ltp", lambda symbol, exchange="NSE": tp1)
        sent_kinds = []
        monkeypatch.setattr(
            alerts, "send",
            lambda text, kind=None, monospace=False: (sent_kinds.append(kind), (True, 999))[1],
        )

        tp_watch.check(datetime(2026, 9, 7, 10, 25))

        assert sent_kinds == ["trade_signal_watchlist"]


class TestMessageParsesCorrectly:
    """The whole feature is worthless if the message this module sends can't actually be
    parsed by the engine - this is the integration seam with parser.py/models.py."""

    def test_tp_hit_message_parses_as_a_valid_exit_signal(self):
        message = "\n".join(
            [
                "BREAKINGTRADE EXIT",
                "Symbol: TCS",
                "Entry: 0.0",
                "SL: 0.0",
                "TP: 2296.1",
                "TPLevel: TP1",
                "ExitQtyPct: 50.0",
            ]
        )
        signal = parser.parse(message)
        assert signal is not None
        assert signal.symbol == "TCS"
        assert signal.tp_level == "TP1"
        assert signal.exit_qty_pct == pytest.approx(0.5)  # parser converts 0-100 -> 0.0-1.0

    def test_watchlist_tp_hit_message_parses_too(self):
        """The strategy name in the first line changes for BREAKINGTRADE-WATCHLIST -
        confirm the longer, hyphenated name doesn't trip up _parse_header()."""
        message = "\n".join(
            [
                "BREAKINGTRADE-WATCHLIST EXIT",
                "Symbol: PFC",
                "Entry: 0.0",
                "SL: 0.0",
                "TP: 350.5",
                "TPLevel: TP1",
                "ExitQtyPct: 50.0",
            ]
        )
        signal = parser.parse(message)
        assert signal is not None
        assert signal.strategy == "BREAKINGTRADE-WATCHLIST"
        assert signal.symbol == "PFC"
        assert signal.tp_level == "TP1"
