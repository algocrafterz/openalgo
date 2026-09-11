"""One stock, one position per day - the BTST paper book was double-counting.

The closing-hour scan runs twice (14:50 and 15:10) and `record_entries()` opened a SEPARATE
paper trade each time, because the primary key is (strategy, symbol, entry_at) and the two
runs have different timestamps. AXISBANK on 2026-09-10:

    AXISBANK  entry 14:50  @ 1245.50   ->  exit 1250.00   +0.36%
    AXISBANK  entry 15:10  @ 1248.00   ->  exit 1250.00   +0.16%

Both settled today and both appeared in the EOD winners list, so "10 settled | 3 winners,
7 losers" described about six distinct stocks. A human takes ONE position per stock, so the
book must too - otherwise the win rate is weighted by which names happened to appear in both
runs rather than by how the calls performed.

The FIRST recommendation of the day is the position: it is the one a trader acting on the
alert would actually have taken.
"""

from datetime import datetime

import pandas as pd
import pytest

from signal_engine.analysis.breakingtrade import paper, store


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_DB_PATH", str(tmp_path / "bt.db"))


def _watchlist(*pairs):
    return pd.DataFrame([{"symbol": s, "price": p} for s, p in pairs])


class TestOnePositionPerStockPerDay:
    def test_a_second_scan_the_same_day_does_not_open_a_second_position(self):
        paper.record_entries(_watchlist(("AXISBANK", 1245.5)), datetime(2026, 9, 10, 14, 50))
        paper.record_entries(_watchlist(("AXISBANK", 1248.0)), datetime(2026, 9, 10, 15, 10))
        with paper._connect() as conn:
            rows = conn.execute("SELECT entry_at, entry_price FROM paper_trades").fetchall()
        assert len(rows) == 1

    def test_the_first_recommendation_of_the_day_is_the_one_kept(self):
        """It is the price a trader acting on the first alert would have got."""
        paper.record_entries(_watchlist(("AXISBANK", 1245.5)), datetime(2026, 9, 10, 14, 50))
        paper.record_entries(_watchlist(("AXISBANK", 1248.0)), datetime(2026, 9, 10, 15, 10))
        with paper._connect() as conn:
            price = conn.execute("SELECT entry_price FROM paper_trades").fetchone()[0]
        assert price == pytest.approx(1245.5)

    def test_the_count_returned_reflects_what_was_actually_opened(self):
        paper.record_entries(_watchlist(("AXISBANK", 1245.5)), datetime(2026, 9, 10, 14, 50))
        opened = paper.record_entries(
            _watchlist(("AXISBANK", 1248.0), ("GRASIM", 3318.5)),
            datetime(2026, 9, 10, 15, 10),
        )
        assert opened == 1  # GRASIM only

    def test_the_next_day_opens_a_fresh_position(self):
        """BTST is a daily call - the same name tomorrow is a new trade."""
        paper.record_entries(_watchlist(("AXISBANK", 1245.5)), datetime(2026, 9, 10, 14, 50))
        paper.record_entries(_watchlist(("AXISBANK", 1250.0)), datetime(2026, 9, 11, 14, 50))
        with paper._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 2

    def test_different_symbols_are_unaffected(self):
        opened = paper.record_entries(
            _watchlist(("AXISBANK", 1245.5), ("GRASIM", 3318.5)),
            datetime(2026, 9, 10, 14, 50),
        )
        assert opened == 2

    def test_a_second_strategy_may_hold_the_same_name(self):
        paper.record_entries(_watchlist(("AXISBANK", 1245.5)),
                             datetime(2026, 9, 10, 14, 50), strategy="BTST")
        paper.record_entries(_watchlist(("AXISBANK", 1245.5)),
                             datetime(2026, 9, 10, 14, 50), strategy="OTHER")
        with paper._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 2

    def test_zero_and_missing_prices_are_still_skipped(self):
        assert paper.record_entries(_watchlist(("AXISBANK", 0)),
                                    datetime(2026, 9, 10, 14, 50)) == 0

    def test_an_empty_watchlist_is_a_no_op(self):
        assert paper.record_entries(pd.DataFrame(), datetime(2026, 9, 10, 14, 50)) == 0


class TestTheIndexEnforcesItStructurally:
    def test_a_direct_duplicate_insert_is_refused(self):
        """Belt and braces: a future code path that bypasses record_entries must not be able
        to reintroduce the double-count."""
        import sqlite3

        paper.record_entries(_watchlist(("AXISBANK", 1245.5)), datetime(2026, 9, 10, 14, 50))
        with paper._connect() as conn, pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO paper_trades (strategy, symbol, entry_at, entry_price, status)"
                " VALUES ('BTST', 'AXISBANK', '2026-09-10 15:10:00', 1248.0, 'open')"
            )
