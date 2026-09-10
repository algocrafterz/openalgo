"""H4: the symbol/sector concentration caps isolate per strategy in ANALYZE.

BREAKINGTRADE and BREAKINGTRADE-WATCHLIST are designed to trade the SAME name at the same
time with different entry philosophies, specifically so paper P&L can compare them. With a
single global max_positions_per_symbol=1 the watchlist (which fires on the scan-hit poll,
before plan_trade's confirming close exists) always took the symbol slot and the confirmed
signal was declined "symbol concentration limit" — so the head-to-head the design exists to
produce could not run.

In LIVE the caps stay pooled: two strategies in one real name is genuine correlated exposure
no matter which one triggered it.
"""

from signal_engine.tests.risk_fixtures import _engine

CONFIRMED = "BREAKINGTRADE"
WATCHLIST = "BREAKINGTRADE-WATCHLIST"


class TestAnalyzeIsolatesSymbolCap:
    def test_second_strategy_may_take_the_same_symbol(self):
        engine = _engine(trade_mode="analyze", max_positions_per_symbol=1)
        engine.record_trade(strategy=WATCHLIST, symbol="TATAMOTORS")
        assert engine.can_trade_symbol("TATAMOTORS", CONFIRMED) is True

    def test_same_strategy_is_still_capped(self):
        engine = _engine(trade_mode="analyze", max_positions_per_symbol=1)
        engine.record_trade(strategy=WATCHLIST, symbol="TATAMOTORS")
        assert engine.can_trade_symbol("TATAMOTORS", WATCHLIST) is False

    def test_close_releases_only_that_strategys_count(self):
        engine = _engine(trade_mode="analyze", max_positions_per_symbol=1)
        engine.record_trade(strategy=WATCHLIST, symbol="TATAMOTORS")
        engine.record_trade(strategy=CONFIRMED, symbol="TATAMOTORS")
        engine.record_close(100, strategy=WATCHLIST, symbol="TATAMOTORS")
        assert engine.can_trade_symbol("TATAMOTORS", WATCHLIST) is True
        assert engine.can_trade_symbol("TATAMOTORS", CONFIRMED) is False


class TestAnalyzeIsolatesSectorCap:
    def test_second_strategy_may_take_the_same_sector(self):
        engine = _engine(trade_mode="analyze", max_positions_per_sector=1)
        engine.record_trade(strategy=WATCHLIST, symbol="HDFCBANK")
        assert engine.can_trade_sector("SBIN", CONFIRMED) is True

    def test_same_strategy_is_still_capped(self):
        engine = _engine(trade_mode="analyze", max_positions_per_sector=1)
        engine.record_trade(strategy=WATCHLIST, symbol="HDFCBANK")
        assert engine.can_trade_sector("SBIN", WATCHLIST) is False


class TestLiveStillPoolsTheCaps:
    def test_symbol_cap_is_shared_across_strategies(self):
        engine = _engine(trade_mode="live", max_positions_per_symbol=1)
        engine.record_trade(strategy=WATCHLIST, symbol="TATAMOTORS")
        assert engine.can_trade_symbol("TATAMOTORS", CONFIRMED) is False

    def test_sector_cap_is_shared_across_strategies(self):
        engine = _engine(trade_mode="live", max_positions_per_sector=1)
        engine.record_trade(strategy=WATCHLIST, symbol="HDFCBANK")
        assert engine.can_trade_sector("SBIN", CONFIRMED) is False
