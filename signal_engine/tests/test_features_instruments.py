"""Broker instrument metadata lookup (tick size), fail-fast when unknown."""

import sqlite3

import pytest

from signal_engine.features.instruments import safe_tick, tick_for_price, tick_size


class TestTickTiers:
    """NSE/CMTR/67133 tiers; a band edge belongs to the finer tier ("Rs 250 to 1,000")."""

    @pytest.mark.parametrize(
        "price,tick",
        [
            (100.0, 0.01),
            (249.99, 0.01),
            (250.0, 0.05),
            (1000.0, 0.05),
            (1000.05, 0.10),
            (5000.0, 0.10),
            (5000.05, 0.50),
            (10000.0, 0.50),
            (20000.0, 1.00),
            (20000.05, 5.00),
        ],
    )
    def test_tick_for_price_follows_the_nse_table(self, price, tick):
        assert tick_for_price(price) == tick

    def test_a_non_positive_price_is_rejected(self):
        with pytest.raises(ValueError, match="price"):
            tick_for_price(0.0)

    def test_safe_tick_is_the_coarsest_tick_the_month_could_carry(self):
        # last month's close may have been up to 1.5x today's price
        assert safe_tick(150.0) == 0.01  # 225 is still below Rs 250
        assert safe_tick(180.0) == 0.05  # 270 crosses into the 0.05 tier
        assert safe_tick(2000.0) == 0.10
        assert safe_tick(3500.0) == 0.50  # 5250 crosses into the 0.50 tier

    def test_safe_tick_is_never_finer_than_the_exact_tier(self):
        for price in (50, 200, 260, 900, 1001, 4900, 9000, 18000, 30000):
            assert safe_tick(price) >= tick_for_price(price)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "openalgo.db"
    con = sqlite3.connect(path)
    con.execute("create table symtoken (symbol text, exchange text, tick_size real)")
    con.executemany(
        "insert into symtoken values (?, ?, ?)",
        [("TCS", "NSE", 0.05), ("TCS", "BSE", 0.01), ("BADTICK", "NSE", 0.0)],
    )
    con.commit()
    con.close()
    return str(path)


def test_returns_the_broker_tick_for_the_exchange(db):
    assert tick_size("TCS", "NSE", db_path=db) == 0.05
    assert tick_size("TCS", "BSE", db_path=db) == 0.01


def test_defaults_to_nse(db):
    assert tick_size("TCS", db_path=db) == 0.05


def test_unknown_symbol_raises(db):
    with pytest.raises(LookupError, match="NOSUCH"):
        tick_size("NOSUCH", "NSE", db_path=db)


def test_non_positive_tick_raises(db):
    with pytest.raises(LookupError, match="BADTICK"):
        tick_size("BADTICK", "NSE", db_path=db)


def test_missing_database_raises(tmp_path):
    with pytest.raises(LookupError, match="database"):
        tick_size("TCS", "NSE", db_path=str(tmp_path / "absent.db"))
