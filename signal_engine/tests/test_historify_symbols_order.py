"""The symbol order the loaders return must not depend on the database's tie order.

`historify_symbols` ranks by history length, and most symbols share the same count. The order flows
into the trade list, so an unordered tie makes two runs on identical data produce the same trades
in a different order, and a row-by-row reproduction then reports a false mismatch.
"""

from __future__ import annotations

import duckdb
import pytest

from signal_engine.backtest import data


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "h.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE market_data (symbol VARCHAR, interval VARCHAR, timestamp BIGINT)")
    rows = []
    for name in reversed([f"S{i:02d}" for i in range(40)]):  # inserted in reverse order
        rows += [(name, "D", t) for t in range(5)]
    rows += [("LONG", "D", t) for t in range(9)]  # a longer history
    rows += [("OTHER", "1m", t) for t in range(7)]  # a different interval
    con.executemany("INSERT INTO market_data VALUES (?, ?, ?)", rows)
    con.close()
    return db


def test_longest_history_comes_first(store):
    assert data.historify_symbols("D", db_path=store)[0] == "LONG"


def test_equal_histories_are_in_symbol_order(store):
    got = data.historify_symbols("D", db_path=store)
    assert got[1:] == [f"S{i:02d}" for i in range(40)]


def test_only_the_requested_interval_is_listed(store):
    assert "OTHER" not in data.historify_symbols("D", db_path=store)
    assert data.historify_symbols("1m", db_path=store) == ["OTHER"]


def test_repeated_calls_agree(store):
    first = data.historify_symbols("D", db_path=store)
    assert all(data.historify_symbols("D", db_path=store) == first for _ in range(5))
