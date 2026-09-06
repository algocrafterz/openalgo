"""BTST paper ledger (paper.py) - hypothetical entries, next-session settlement, scoring."""

from datetime import datetime

import pandas as pd
import pytest

from signal_engine.analysis.breakingtrade import extractor, paper, store


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", str(tmp_path / "breakingtrade.db"))


def _watchlist(symbol="SWIGGY", price=100.0):
    return pd.DataFrame([{"symbol": symbol, "price": price}])


def _store_session(day: str, symbol: str, price: float):
    """Write one end-of-day snapshot row, which is what settlement reads."""
    frame = pd.DataFrame([{"symbol": symbol, "price": price, "day_type": "Normal Var"}])
    store.save_snapshot(
        extractor.Snapshot(
            kind="market_profile",
            captured_at=datetime.strptime(f"{day} 15:30", "%Y-%m-%d %H:%M"),
            frame=frame,
        )
    )


def test_entries_are_opened_at_the_snapshot_price():
    assert paper.record_entries(_watchlist(price=250.0), datetime(2026, 9, 4, 14, 50)) == 1
    trade = paper.ledger().iloc[0]
    assert trade["entry_price"] == 250.0
    assert trade["status"] == "open"


def test_the_same_list_twice_does_not_double_count():
    """A re-run of the 14:50 poll must not open the position again."""
    when = datetime(2026, 9, 4, 14, 50)
    paper.record_entries(_watchlist(), when)
    paper.record_entries(_watchlist(), when)
    assert len(paper.ledger()) == 1


def test_empty_list_opens_nothing():
    assert paper.record_entries(pd.DataFrame(), datetime(2026, 9, 4, 14, 50)) == 0


def test_trade_settles_against_the_next_session():
    paper.record_entries(_watchlist(price=100.0), datetime(2026, 9, 4, 14, 50))
    _store_session("2026-09-07", "SWIGGY", 110.0)
    assert paper.settle_open_trades() == 1
    trade = paper.ledger().iloc[0]
    assert trade["status"] == "closed"
    assert trade["return_pct"] == pytest.approx(10.0)


def test_trade_stays_open_until_the_next_session_exists():
    """Settling against the entry day itself would score a trade that never happened."""
    paper.record_entries(_watchlist(price=100.0), datetime(2026, 9, 4, 14, 50))
    _store_session("2026-09-04", "SWIGGY", 105.0)  # same day, not a valid exit
    assert paper.settle_open_trades() == 0
    assert paper.ledger().iloc[0]["status"] == "open"


def test_a_loss_is_recorded_as_a_loss():
    paper.record_entries(_watchlist(price=100.0), datetime(2026, 9, 4, 14, 50))
    _store_session("2026-09-07", "SWIGGY", 96.0)
    paper.settle_open_trades()
    assert paper.ledger().iloc[0]["return_pct"] == pytest.approx(-4.0)


def test_missing_exit_price_voids_rather_than_guesses():
    paper.record_entries(_watchlist(symbol="GONE", price=100.0), datetime(2026, 9, 4, 14, 50))
    _store_session("2026-09-07", "SOMETHINGELSE", 110.0)
    paper.settle_open_trades()
    assert paper.ledger().iloc[0]["status"] == "void"


def test_cost_assumption_is_not_silently_zero():
    """A gross figure that ignores costs flatters every result; the constant must be real."""
    assert paper.ROUND_TRIP_COST_PCT > 0
