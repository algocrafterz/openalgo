"""Structure-flip watch (flip_watch.py): alert-only warning when an open BREAKINGTRADE
position's own setup reverses, plus the alerts.py plumbing it depends on (message_id capture,
telegram_link, find_last_alert).
"""

import sqlite3
from datetime import datetime

import pandas as pd
import pytest

from signal_engine.analysis.breakingtrade import alerts, flip_watch, store


@pytest.fixture(autouse=True)
def isolated_dbs(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", str(tmp_path / "breakingtrade.db"))
    monkeypatch.setattr(flip_watch, "_TRADES_DB", str(tmp_path / "trades.db"))
    # Never attempt a real network call from a test.
    monkeypatch.setattr(alerts, "send", lambda text, kind=None: (False, None))


def _make_trades_db(path, rows):
    """rows: list of (strategy, direction, symbol, entry, sl, tp, status, executed_at)."""
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


def _open_long(symbol="TCS", executed_at="2026-09-07 09:55:04"):
    return [("BREAKINGTRADE", "LONG", symbol, 2283.1, 2270.0, 2310.0, "SUCCESS", executed_at)]


def _snapshot(symbol="TCS", open_type_dir="down", tail="sell_tail"):
    return pd.DataFrame(
        [{"symbol": symbol, "open_type_dir": open_type_dir, "tail": tail}]
    )


def test_no_trades_db_returns_zero():
    """Nothing has ever traded - trades.db doesn't exist yet."""
    assert flip_watch.check(_snapshot(), datetime(2026, 9, 7, 10, 25)) == 0


def test_no_open_positions_returns_zero(tmp_path):
    _make_trades_db(flip_watch._TRADES_DB, [])
    assert flip_watch.check(_snapshot(), datetime(2026, 9, 7, 10, 25)) == 0


def test_matching_direction_sends_nothing(tmp_path):
    _make_trades_db(flip_watch._TRADES_DB, _open_long())
    still_up = _snapshot(open_type_dir="up", tail="buy_tail")
    assert flip_watch.check(still_up, datetime(2026, 9, 7, 10, 25)) == 0
    assert alerts.history().empty


def test_blank_direction_sends_nothing(tmp_path):
    """No directional read this poll (blank open_type_dir) is not evidence of a flip."""
    _make_trades_db(flip_watch._TRADES_DB, _open_long())
    blank = _snapshot(open_type_dir="", tail="")
    assert flip_watch.check(blank, datetime(2026, 9, 7, 10, 25)) == 0


def test_flip_sends_and_records_one_alert(tmp_path):
    _make_trades_db(flip_watch._TRADES_DB, _open_long())
    flipped = _snapshot(open_type_dir="down", tail="sell_tail")

    sent = flip_watch.check(flipped, datetime(2026, 9, 7, 10, 25))

    assert sent == 1
    stored = alerts.history()
    assert len(stored) == 1
    assert stored.iloc[0]["kind"] == "structure_flip"
    assert stored.iloc[0]["symbol"] == "TCS"


def test_flip_alerts_only_once_per_open_position(tmp_path):
    """A poll that keeps re-reading the same flipped state must not re-ping every 5 minutes."""
    _make_trades_db(flip_watch._TRADES_DB, _open_long())
    flipped = _snapshot(open_type_dir="down", tail="sell_tail")

    first = flip_watch.check(flipped, datetime(2026, 9, 7, 10, 25))
    second = flip_watch.check(flipped, datetime(2026, 9, 7, 10, 30))

    assert first == 1
    assert second == 0
    assert len(alerts.history()) == 1


def test_short_position_flips_on_up_read(tmp_path):
    """A SHORT is flipped by open_type_dir turning up, the mirror of the LONG case."""
    _make_trades_db(
        flip_watch._TRADES_DB,
        [("BREAKINGTRADE", "SHORT", "MARUTI", 12694.0, 12750.0, 12600.0, "SUCCESS",
          "2026-09-07 09:20:50")],
    )
    flipped = _snapshot(symbol="MARUTI", open_type_dir="up", tail="buy_tail")
    assert flip_watch.check(flipped, datetime(2026, 9, 7, 10, 25)) == 1


def test_flip_message_links_back_to_the_original_signal(monkeypatch, tmp_path):
    """The whole point of the reference: a reader should not have to search the channel."""
    monkeypatch.setattr(
        alerts,
        "_env",
        lambda: {
            "BREAKINGTRADE_BOT_TOKEN": "123:abc",
            "BREAKINGTRADE_CHAT_ID_INTRADAY": "-1004379472313",
        },
    )
    # The original trade_signal alert, as alert_trade_signal() would have recorded it.
    alerts.record(
        "trade_signal",
        "BREAKINGTRADE LONG\nSymbol: TCS\nEntry: 2283.1\nSL: 2270.0\nTP: 2310.0",
        symbol="TCS",
        direction="up",
        scan="BREAKINGTRADE",
        deliver=False,
        delivered=True,
        message_id=555,
    )
    # record() always stamps created_at with the real wall clock - backdate it to the fictional
    # entry time this test is exercising, so find_last_alert's before-cutoff can see it.
    with alerts._connect() as conn:
        conn.execute(
            "UPDATE alerts SET created_at = '2026-09-07 09:55:00' WHERE message_id = 555"
        )
    _make_trades_db(flip_watch._TRADES_DB, _open_long())
    flipped = _snapshot(open_type_dir="down", tail="sell_tail")

    flip_watch.check(flipped, datetime(2026, 9, 7, 10, 25))

    with alerts._connect() as conn:
        message = conn.execute(
            "SELECT message FROM alerts WHERE kind = 'structure_flip'"
        ).fetchone()[0]
    assert "https://t.me/c/4379472313/555" in message


# ---------------------------------------------------------------------------
# alerts.py plumbing the flip watch depends on
# ---------------------------------------------------------------------------


def test_telegram_link_strips_the_100_prefix(monkeypatch):
    monkeypatch.setattr(
        alerts,
        "_env",
        lambda: {
            "BREAKINGTRADE_BOT_TOKEN": "123:abc",
            "BREAKINGTRADE_CHAT_ID_INTRADAY": "-1004379472313",
        },
    )
    assert alerts.telegram_link("trade_signal", 555) == "https://t.me/c/4379472313/555"


def test_telegram_link_none_without_a_message_id(monkeypatch):
    monkeypatch.setattr(
        alerts,
        "_env",
        lambda: {
            "BREAKINGTRADE_BOT_TOKEN": "123:abc",
            "BREAKINGTRADE_CHAT_ID_INTRADAY": "-1004379472313",
        },
    )
    assert alerts.telegram_link("trade_signal", None) is None


def test_record_persists_message_id():
    alerts.record(
        "trade_signal", "text", symbol="TCS", deliver=False, delivered=True, message_id=42
    )
    with alerts._connect() as conn:
        stored = conn.execute("SELECT message_id FROM alerts WHERE symbol = 'TCS'").fetchone()
    assert stored[0] == 42


def test_find_last_alert_respects_the_before_cutoff():
    alerts.record(
        "trade_signal", "old", symbol="TCS", deliver=False, delivered=True, message_id=1,
    )
    with alerts._connect() as conn:
        conn.execute(
            "UPDATE alerts SET created_at = '2026-09-07 09:00:00' WHERE message_id = 1"
        )
    alerts.record(
        "trade_signal", "new", symbol="TCS", deliver=False, delivered=True, message_id=2,
    )
    with alerts._connect() as conn:
        conn.execute(
            "UPDATE alerts SET created_at = '2026-09-07 11:00:00' WHERE message_id = 2"
        )

    found = alerts.find_last_alert("TCS", "trade_signal", before=datetime(2026, 9, 7, 10, 0))
    assert found["message_id"] == 1
