"""Declined signals must leave a row, not just a log line.

Every early return in `_handle_entry` — blacklist, T2T, exposure limit, price filter, capital,
qty=0 — exits BEFORE save(), so until now a declined signal existed only in the log and a
Telegram message. `trades.db` held the trades that happened and nothing about the ones that
did not.

That is tolerable while the question is "did my fills work". It is not tolerable for the
paper week starting 2026-09-07, whose entire purpose is "what would this strategy have done":
with max_open_positions at 2 on Rs35k, a meaningful share of signals will be declined, and
which ones and why IS the finding. A week of EOD reviews that cannot see the declines would
mistake a capital constraint for a signal-quality result.
"""

import sqlite3

import pytest

from signal_engine.db import DECLINED_ORDER_ID, save_declined
from signal_engine.models import Direction, Signal


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "trades.db"
    monkeypatch.setattr("signal_engine.db._DB_PATH", str(path))
    return path


def _signal(**over):
    base = {
        "strategy": "BREAKOUT", "direction": Direction.LONG, "symbol": "LICHSGFIN",
        "entry": 564.2, "sl": 562.61, "tp": 566.59, "sig_id": "LICHSGFIN-20260904-1045",
        "raw_message": "BREAKOUT LONG | LICHSGFIN", "context": {"trigger": "IBH-RT", "score": "10"},
    }
    base.update(over)
    return Signal(**base)


def _rows(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM trades")]
    finally:
        conn.close()


class TestSaveDeclined:
    def test_declined_signal_is_persisted(self, db):
        save_declined(_signal(), stage="risk_gates", reason="max_open_positions reached (2/2)")
        rows = _rows(db)
        assert len(rows) == 1
        assert rows[0]["symbol"] == "LICHSGFIN"
        assert rows[0]["strategy"] == "BREAKOUT"

    def test_status_marks_it_as_never_sent(self, db):
        """DECLINED is distinct from REJECTED: REJECTED means the BROKER refused an order
        that was actually sent. DECLINED means no order ever left the engine."""
        save_declined(_signal(), stage="price_filter", reason="entry 118.70 below min 300")
        assert _rows(db)[0]["status"] == "DECLINED"

    def test_quantity_is_zero_and_order_id_is_the_sentinel(self, db):
        save_declined(_signal(), stage="sizing", reason="qty=0")
        row = _rows(db)[0]
        assert row["quantity"] == 0
        assert row["order_id"] == DECLINED_ORDER_ID

    def test_stage_and_reason_are_both_recoverable(self, db):
        """The stage says WHICH gate; the reason says why. EOD analysis needs both."""
        save_declined(_signal(), stage="risk_gates", reason="daily loss limit hit")
        msg = _rows(db)[0]["message"]
        assert "risk_gates" in msg
        assert "daily loss limit hit" in msg

    def test_signal_context_and_sig_id_survive(self, db):
        """A declined signal still carries its entry criteria — that is the whole point:
        it lets EOD ask whether the DECLINED ones would have been the winners."""
        save_declined(_signal(), stage="risk_gates", reason="slot full")
        row = _rows(db)[0]
        assert row["sig_id"] == "LICHSGFIN-20260904-1045"
        assert "IBH-RT" in row["context"]

    def test_prices_are_preserved_so_the_outcome_can_be_scored_later(self, db):
        save_declined(_signal(), stage="risk_gates", reason="slot full")
        row = _rows(db)[0]
        assert row["entry"] == pytest.approx(564.2)
        assert row["sl"] == pytest.approx(562.61)
        assert row["tp"] == pytest.approx(566.59)

    def test_a_failure_to_persist_never_propagates(self, db, monkeypatch):
        """Persistence is bookkeeping. It must not be able to break signal handling."""
        monkeypatch.setattr(
            "signal_engine.db._get_connection",
            lambda: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
        )
        save_declined(_signal(), stage="risk_gates", reason="slot full")  # must not raise


class TestLedgerIgnoresDeclined:
    def test_declined_rows_do_not_become_positions(self, db):
        """Otherwise every decline shows up as a NO_FILL — a reconciliation error that is
        not one — and buries the real unfilled orders."""
        from signal_engine.analysis.ledger import build_ledger, load_engine_events

        save_declined(_signal(), stage="risk_gates", reason="slot full")
        events = load_engine_events(db_path=str(db))
        assert build_ledger(events) == []

    def test_declined_rows_are_still_loadable_for_analysis(self, db):
        from signal_engine.analysis.ledger import load_declined

        save_declined(_signal(), stage="risk_gates", reason="slot full")
        declined = load_declined(db_path=str(db))
        assert len(declined) == 1
        assert declined[0]["symbol"] == "LICHSGFIN"
        assert "slot full" in declined[0]["message"]
