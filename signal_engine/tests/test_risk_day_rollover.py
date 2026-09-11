"""M2: the daily rollover must be checked wherever a counter is first touched.

_maybe_reset_daily() was called only from check_exposure(). main._handle_entry() calls
get_sizing_capital() first, and smoke_test calls it without ever calling check_exposure — so
across a midnight-IST rollover on a long-running process the first signal of the new day read
(and re-cached) the PREVIOUS day's day_start_capital before anything reset it.
"""

from signal_engine.tests.conftest import make_signal as _make_signal
from signal_engine.tests.risk_fixtures import _engine

STRATEGY = "ORB"


def _stale_day(engine):
    """Pretend the engine has been running since yesterday."""
    engine._current_day = -1


class TestGetSizingCapitalResetsFirst:
    def test_new_day_recaches_capital_instead_of_returning_yesterdays(self):
        engine = _engine(use_day_start_capital=True)
        engine.get_sizing_capital(100_000, STRATEGY)
        assert engine.get_sizing_capital(250_000, STRATEGY) == 100_000  # same day: cached
        _stale_day(engine)
        assert engine.get_sizing_capital(250_000, STRATEGY) == 250_000  # new day: refreshed

    def test_new_day_clears_position_and_trade_counters(self):
        engine = _engine()
        engine.record_trade(strategy=STRATEGY, symbol="RELIANCE")
        _stale_day(engine)
        engine.get_sizing_capital(100_000, STRATEGY)
        assert engine.open_positions_for(STRATEGY) == 0
        assert engine.trades_today_for(STRATEGY) == 0


class TestCalculateQuantityResetsFirst:
    def test_new_day_clears_counters_before_sizing(self):
        engine = _engine()
        engine.record_trade(strategy=STRATEGY, symbol="RELIANCE")
        _stale_day(engine)
        engine.calculate_quantity(_make_signal(strategy=STRATEGY), capital=100_000)
        assert engine.open_positions_for(STRATEGY) == 0

    def test_new_day_clears_the_symbol_concentration_count(self):
        engine = _engine(max_positions_per_symbol=1)
        engine.record_trade(strategy=STRATEGY, symbol="RELIANCE")
        assert engine.can_trade_symbol("RELIANCE", STRATEGY) is False
        _stale_day(engine)
        engine.calculate_quantity(_make_signal(strategy=STRATEGY), capital=100_000)
        assert engine.can_trade_symbol("RELIANCE", STRATEGY) is True


class TestSameDayIsUntouched:
    def test_counters_survive_within_the_day(self):
        engine = _engine()
        engine.record_trade(strategy=STRATEGY, symbol="RELIANCE")
        engine.get_sizing_capital(100_000, STRATEGY)
        engine.calculate_quantity(_make_signal(strategy=STRATEGY), capital=100_000)
        engine.check_exposure(STRATEGY)
        assert engine.trades_today_for(STRATEGY) == 1
