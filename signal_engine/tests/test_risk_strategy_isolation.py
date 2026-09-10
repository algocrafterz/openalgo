"""Per-strategy capital/position/trade/loss isolation (2026-09-10).

RiskEngine used to track one shared set of counters for every strategy sharing the
sandbox. These tests lock in the split: each strategy now gets its own day-start
capital, its own open_positions/trades_today slots, and its own loss counters,
so one strategy's activity can never starve or contaminate another's.
"""

from datetime import date

from signal_engine.risk_store import RiskStore
from signal_engine.tests.conftest import make_signal as _make_signal
from signal_engine.tests.risk_fixtures import _engine

ORB = "ORB"
BREAKOUT = "BREAKOUT"


class TestDayStartCapitalIsolation:
    def test_two_strategies_cache_independently(self):
        engine = _engine(use_day_start_capital=True)
        assert engine.get_sizing_capital(100_000, ORB) == 100_000
        assert engine.get_sizing_capital(100_000, BREAKOUT) == 100_000

        # A later live-capital change must not disturb either cached value.
        assert engine.get_sizing_capital(50_000, ORB) == 100_000
        assert engine.get_sizing_capital(50_000, BREAKOUT) == 100_000

    def test_strategies_can_cache_different_capital_the_same_day(self):
        # Not realistic in production (same sandbox override feeds every strategy) but the
        # engine itself must not force one strategy's cached value onto another.
        engine = _engine(use_day_start_capital=True)
        assert engine.get_sizing_capital(60_000, ORB) == 60_000
        assert engine.get_sizing_capital(90_000, BREAKOUT) == 90_000
        assert engine.get_sizing_capital(10_000, ORB) == 60_000
        assert engine.get_sizing_capital(10_000, BREAKOUT) == 90_000


class TestPositionAndTradeSlotIsolation:
    def test_one_strategy_at_max_positions_does_not_block_another(self):
        engine = _engine(max_open_positions=1)
        engine.record_trade(strategy=ORB, symbol="RELIANCE")
        assert engine.check_exposure(ORB) is False
        assert engine.check_exposure(BREAKOUT) is True

    def test_one_strategy_at_max_trades_does_not_block_another(self):
        engine = _engine(max_trades_per_day=1)
        engine.record_trade(strategy=ORB, symbol="RELIANCE")
        engine.record_close(pnl=100.0, strategy=ORB, symbol="RELIANCE")
        assert engine.check_exposure(ORB) is False
        assert engine.check_exposure(BREAKOUT) is True

    def test_open_positions_counted_separately(self):
        engine = _engine()
        engine.record_trade(strategy=ORB, symbol="RELIANCE")
        engine.record_trade(strategy=BREAKOUT, symbol="TCS")
        engine.record_trade(strategy=BREAKOUT, symbol="INFY")
        assert engine.open_positions_for(ORB) == 1
        assert engine.open_positions_for(BREAKOUT) == 2
        assert engine.total_open_positions() == 3


class TestLossCounterIsolation:
    def test_one_strategys_loss_does_not_touch_another(self):
        engine = _engine()
        engine.record_trade(strategy=ORB, symbol="RELIANCE")
        engine.record_close(pnl=-500.0, strategy=ORB, symbol="RELIANCE")
        assert engine._state(ORB).daily_realised_loss == 500.0
        assert engine._state(BREAKOUT).daily_realised_loss == 0.0

    def test_untouched_strategy_never_blocked_by_anothers_daily_limit(self):
        engine = _engine(daily_loss_limit=0.01)
        state = engine._state(ORB)
        state.last_known_capital = 100_000
        state.daily_realised_loss = 5_000.0  # way past ORB's own 1% limit
        assert engine.check_exposure(ORB) is False
        assert engine.check_exposure(BREAKOUT) is True


class TestFreshStrategyStartsZeroed:
    def test_untouched_strategy_has_zero_state_not_an_error(self):
        engine = _engine()
        assert engine.open_positions_for("NEVER-SEEN-BEFORE") == 0
        assert engine.trades_today_for("NEVER-SEEN-BEFORE") == 0
        assert engine._state("NEVER-SEEN-BEFORE").daily_realised_loss == 0.0

    def test_untouched_strategy_is_independent_from_a_touched_one(self):
        engine = _engine()
        engine.record_trade(strategy=ORB, symbol="RELIANCE")
        assert engine.open_positions_for("BREAKINGTRADE") == 0


class TestCalculateQuantityUsesSignalsOwnStrategy:
    """calculate_quantity takes no strategy arg — it must size against
    signal.strategy's own last_known_capital, independent of any other strategy."""

    def test_two_strategies_size_off_their_own_capital(self):
        engine = _engine(use_day_start_capital=True)
        orb_capital = engine.get_sizing_capital(100_000, ORB)
        breakout_capital = engine.get_sizing_capital(15_000, BREAKOUT)

        orb_qty = engine.calculate_quantity(
            _make_signal(strategy=ORB, entry=2500, sl=2485),
            capital=orb_capital,
        )
        breakout_qty = engine.calculate_quantity(
            _make_signal(strategy=BREAKOUT, entry=2500, sl=2485),
            capital=breakout_capital,
        )

        assert orb_qty == 66  # floor(1000/15)
        assert breakout_qty == 10  # floor(150/15)
        assert engine.last_known_capital_for(ORB) == 100_000
        assert engine.last_known_capital_for(BREAKOUT) == 15_000


class TestRiskStorePerStrategyRoundTrip:
    def test_two_strategies_same_mode_and_date_do_not_clobber_each_other(self, tmp_path):
        store = RiskStore(str(tmp_path / "risk.db"))
        today = date(2026, 3, 10)
        store.save(ORB, "live", today, trades_today=2, daily_loss=100.0, open_positions=1)
        store.save(BREAKOUT, "live", today, trades_today=5, daily_loss=900.0, open_positions=3)

        orb_row = store.load(ORB, "live", today)
        breakout_row = store.load(BREAKOUT, "live", today)

        assert orb_row["trades_today"] == 2
        assert orb_row["daily_loss"] == 100.0
        assert breakout_row["trades_today"] == 5
        assert breakout_row["daily_loss"] == 900.0

    def test_strategies_for_returns_every_strategy_with_a_row(self, tmp_path):
        store = RiskStore(str(tmp_path / "risk.db"))
        today = date(2026, 3, 10)
        store.save(ORB, "live", today, trades_today=1, daily_loss=0.0, open_positions=0)
        store.save(BREAKOUT, "live", today, trades_today=1, daily_loss=0.0, open_positions=0)

        assert set(store.strategies_for("live", today)) == {ORB, BREAKOUT}

    def test_strategy_with_no_row_yet_loads_as_zero(self, tmp_path):
        store = RiskStore(str(tmp_path / "risk.db"))
        today = date(2026, 3, 10)
        store.save(ORB, "live", today, trades_today=9, daily_loss=500.0, open_positions=2)

        row = store.load(BREAKOUT, "live", today)
        assert row["trades_today"] == 0
        assert row["daily_loss"] == 0.0
        assert row["open_positions"] == 0
