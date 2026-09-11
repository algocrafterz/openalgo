"""One position per stock, across every strategy, in both modes.

This was made per-strategy in ANALYZE on 2026-09-11 so the BREAKINGTRADE vs
BREAKINGTRADE-WATCHLIST comparison could run - the two are built to call the same names, and
a shared cap meant whichever fired first took the slot and the other was declined.

That is reverted. The requirement is the stronger one: a trader holds ONE position in a
stock, and analyze has to mirror live or it is not evidence. Two strategies both long the
same name is one position with two labels; counting it twice overstates the sample, doubles
the real exposure to that name, and produces a paper result live could never reproduce.

The cost is stated rather than hidden: the confirmed-vs-watchlist comparison cannot be run
by letting both trade the same stock. It has to be answered from the DECLINED rows (both
signals are recorded either way - see main._decline) or by running the two in separate
phases.
"""

from signal_engine.tests.risk_fixtures import _engine

CONFIRMED = "BREAKINGTRADE"
WATCHLIST = "BREAKINGTRADE-WATCHLIST"


class TestSymbolCapIsGlobal:
    def test_a_second_strategy_cannot_take_the_same_stock_in_analyze(self):
        engine = _engine(trade_mode="analyze", max_positions_per_symbol=1)
        engine.record_trade(strategy=WATCHLIST, symbol="TATAMOTORS")
        assert engine.can_trade_symbol("TATAMOTORS", CONFIRMED) is False

    def test_a_second_strategy_cannot_take_the_same_stock_in_live(self):
        engine = _engine(trade_mode="live", max_positions_per_symbol=1)
        engine.record_trade(strategy=WATCHLIST, symbol="TATAMOTORS")
        assert engine.can_trade_symbol("TATAMOTORS", CONFIRMED) is False

    def test_a_different_stock_is_unaffected(self):
        engine = _engine(trade_mode="analyze", max_positions_per_symbol=1)
        engine.record_trade(strategy=WATCHLIST, symbol="TATAMOTORS")
        assert engine.can_trade_symbol("SBIN", CONFIRMED) is True

    def test_closing_frees_the_stock_for_any_strategy(self):
        engine = _engine(trade_mode="analyze", max_positions_per_symbol=1)
        engine.record_trade(strategy=WATCHLIST, symbol="TATAMOTORS")
        engine.record_close(100.0, strategy=WATCHLIST, symbol="TATAMOTORS")
        assert engine.can_trade_symbol("TATAMOTORS", CONFIRMED) is True

    def test_a_rejected_entry_frees_the_stock_too(self):
        engine = _engine(trade_mode="analyze", max_positions_per_symbol=1)
        engine.record_trade(strategy=WATCHLIST, symbol="TATAMOTORS")
        engine.record_rejection(strategy=WATCHLIST, symbol="TATAMOTORS")
        assert engine.can_trade_symbol("TATAMOTORS", CONFIRMED) is True

    def test_zero_still_means_unlimited(self):
        engine = _engine(trade_mode="analyze", max_positions_per_symbol=0)
        engine.record_trade(strategy=WATCHLIST, symbol="TATAMOTORS")
        assert engine.can_trade_symbol("TATAMOTORS", CONFIRMED) is True


class TestSectorCapIsGlobalToo:
    def test_a_second_strategy_shares_the_sector_budget_in_analyze(self):
        engine = _engine(trade_mode="analyze", max_positions_per_sector=1)
        engine.record_trade(strategy=WATCHLIST, symbol="HDFCBANK")
        assert engine.can_trade_sector("SBIN", CONFIRMED) is False

    def test_a_different_sector_is_unaffected(self):
        engine = _engine(trade_mode="analyze", max_positions_per_sector=1)
        engine.record_trade(strategy=WATCHLIST, symbol="HDFCBANK")
        assert engine.can_trade_sector("TCS", CONFIRMED) is True
