"""One position per stock PER STRATEGY - the same in both modes.

A strategy must never take a second position in a name it already holds: that is averaging
into a position its own rules already sized once, and it doubles that strategy's exposure
without a second decision behind it.

Two DIFFERENT strategies may both hold the same name. They reached it by different logic,
with their own stop and target, and each is a position a trader would genuinely have taken -
which is also what lets BREAKINGTRADE and BREAKINGTRADE-WATCHLIST be compared on the names
they both call.

The same rule applies in LIVE, because analyze has to mirror live to be evidence. The
exposure that creates IS real: two strategies long the same stock is double the position in
that name. `risk.max_positions_per_sector` is the control for correlated risk, and it is
currently off (0).
"""

from signal_engine.tests.risk_fixtures import _engine

CONFIRMED = "BREAKINGTRADE"
WATCHLIST = "BREAKINGTRADE-WATCHLIST"


class TestTheSameStrategyCannotDoubleUp:
    def test_a_strategy_cannot_re_enter_a_name_it_already_holds(self):
        engine = _engine(trade_mode="analyze", max_positions_per_symbol=1)
        engine.record_trade(strategy=WATCHLIST, symbol="TATAMOTORS")
        assert engine.can_trade_symbol("TATAMOTORS", WATCHLIST) is False

    def test_the_same_is_true_in_live(self):
        engine = _engine(trade_mode="live", max_positions_per_symbol=1)
        engine.record_trade(strategy=WATCHLIST, symbol="TATAMOTORS")
        assert engine.can_trade_symbol("TATAMOTORS", WATCHLIST) is False


class TestADifferentStrategyMay:
    def test_another_strategy_may_hold_the_same_stock_in_analyze(self):
        engine = _engine(trade_mode="analyze", max_positions_per_symbol=1)
        engine.record_trade(strategy=WATCHLIST, symbol="TATAMOTORS")
        assert engine.can_trade_symbol("TATAMOTORS", CONFIRMED) is True

    def test_another_strategy_may_hold_the_same_stock_in_live(self):
        """Same rule in both modes - analyze has to mirror live to be evidence."""
        engine = _engine(trade_mode="live", max_positions_per_symbol=1)
        engine.record_trade(strategy=WATCHLIST, symbol="TATAMOTORS")
        assert engine.can_trade_symbol("TATAMOTORS", CONFIRMED) is True

    def test_each_strategy_keeps_its_own_count(self):
        engine = _engine(trade_mode="analyze", max_positions_per_symbol=1)
        engine.record_trade(strategy=WATCHLIST, symbol="TATAMOTORS")
        engine.record_trade(strategy=CONFIRMED, symbol="TATAMOTORS")
        assert engine.can_trade_symbol("TATAMOTORS", WATCHLIST) is False
        assert engine.can_trade_symbol("TATAMOTORS", CONFIRMED) is False

    def test_closing_one_frees_only_that_strategy(self):
        engine = _engine(trade_mode="analyze", max_positions_per_symbol=1)
        engine.record_trade(strategy=WATCHLIST, symbol="TATAMOTORS")
        engine.record_trade(strategy=CONFIRMED, symbol="TATAMOTORS")
        engine.record_close(100.0, strategy=WATCHLIST, symbol="TATAMOTORS")
        assert engine.can_trade_symbol("TATAMOTORS", WATCHLIST) is True
        assert engine.can_trade_symbol("TATAMOTORS", CONFIRMED) is False

    def test_a_different_stock_is_unaffected(self):
        engine = _engine(trade_mode="analyze", max_positions_per_symbol=1)
        engine.record_trade(strategy=WATCHLIST, symbol="TATAMOTORS")
        assert engine.can_trade_symbol("SBIN", CONFIRMED) is True

    def test_a_rejected_entry_frees_the_stock_too(self):
        engine = _engine(trade_mode="analyze", max_positions_per_symbol=1)
        engine.record_trade(strategy=WATCHLIST, symbol="TATAMOTORS")
        engine.record_rejection(strategy=WATCHLIST, symbol="TATAMOTORS")
        assert engine.can_trade_symbol("TATAMOTORS", WATCHLIST) is True

    def test_zero_still_means_unlimited(self):
        engine = _engine(trade_mode="analyze", max_positions_per_symbol=0)
        engine.record_trade(strategy=WATCHLIST, symbol="TATAMOTORS")
        assert engine.can_trade_symbol("TATAMOTORS", WATCHLIST) is True


class TestSectorCapFollowsTheSameRule:
    def test_a_strategy_is_capped_within_its_own_sector_budget(self):
        engine = _engine(trade_mode="analyze", max_positions_per_sector=1)
        engine.record_trade(strategy=WATCHLIST, symbol="HDFCBANK")
        assert engine.can_trade_sector("SBIN", WATCHLIST) is False

    def test_another_strategy_has_its_own_sector_budget(self):
        engine = _engine(trade_mode="analyze", max_positions_per_sector=1)
        engine.record_trade(strategy=WATCHLIST, symbol="HDFCBANK")
        assert engine.can_trade_sector("SBIN", CONFIRMED) is True

    def test_a_different_sector_is_unaffected(self):
        engine = _engine(trade_mode="analyze", max_positions_per_sector=1)
        engine.record_trade(strategy=WATCHLIST, symbol="HDFCBANK")
        assert engine.can_trade_sector("TCS", WATCHLIST) is True
