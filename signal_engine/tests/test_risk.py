"""Tests for risk engine and position sizing."""

import pytest

from signal_engine.models import Direction, Signal
from signal_engine.risk import RiskEngine
from signal_engine.risk_store import RiskStore
from signal_engine.tests.conftest import make_signal as _make_signal


def _engine(**overrides) -> RiskEngine:
    defaults = {
        "risk_per_trade": 0.01,
        "sizing_mode": "fixed_fractional",
        "pct_of_capital": 0.05,
        "daily_loss_limit": 0.03,
        "weekly_loss_limit": 0.06,
        "monthly_loss_limit": 0.10,
        "max_open_positions": 3,
        "max_trades_per_day": 5,
        "min_entry_price": 0,
        "max_entry_price": 0,
        "slippage_factor": 0.0,
        "default_product": "MIS",
        "max_positions_per_symbol": 1,
        "max_positions_per_sector": 2,
        "sectors": {"BANKING": ["HDFCBANK", "SBIN"], "IT": ["TCS", "INFY"]},
    }
    defaults.update(overrides)
    return RiskEngine(**defaults)


class TestFixedFractionalSizing:
    def test_basic_calculation(self):
        # risk_amount = 100000 * 0.01 = 1000, risk_per_share = 15, qty = 66
        engine = _engine()
        qty = engine.calculate_quantity(_make_signal(entry=2500, sl=2485), capital=100_000)
        assert qty == 66

    def test_with_different_capital(self):
        # risk_amount = 200000 * 0.01 = 2000, risk_per_share = 15, qty = 133
        engine = _engine()
        qty = engine.calculate_quantity(_make_signal(entry=2500, sl=2485), capital=200_000)
        assert qty == 133

    def test_short_direction(self):
        # risk_per_share = 30, qty = floor(1000/30) = 33
        engine = _engine()
        qty = engine.calculate_quantity(
            _make_signal(direction=Direction.SHORT, entry=3800, sl=3830, tp=3750),
            capital=100_000,
        )
        assert qty == 33

    def test_returns_zero_when_risk_exceeds_budget(self):
        # risk_per_share = 2000, qty = floor(1000/2000) = 0 -> skip
        engine = _engine()
        qty = engine.calculate_quantity(_make_signal(entry=2500, sl=500), capital=100_000)
        assert qty == 0


class TestRiskFullyHonored:
    """Core guarantee: every trade risks exactly risk_per_trade % of capital."""

    def test_small_capital_expensive_stock(self):
        # The JINDALSTEL case: capital=10000, entry=1187.9, sl=1182.16
        # risk_amount = 100, risk_per_share = 5.74
        # qty = floor(100/5.74) = 17, actual_risk = 17*5.74 = 97.58 (~1%)
        engine = _engine()
        qty = engine.calculate_quantity(
            _make_signal(entry=1187.9, sl=1182.16, tp=1194.79), capital=10_000,
        )
        assert qty == 17
        actual_risk = qty * abs(1187.9 - 1182.16)
        assert actual_risk == pytest.approx(97.58, abs=0.01)
        assert actual_risk / 10_000 == pytest.approx(0.01, abs=0.003)

    def test_small_capital_cheap_stock(self):
        # capital=10000, entry=50, sl=48, risk_per_share=2
        # risk_amount = 100, qty = floor(100/2) = 50
        # actual_risk = 50*2 = 100 = exactly 1%
        engine = _engine()
        qty = engine.calculate_quantity(
            _make_signal(entry=50, sl=48, tp=56), capital=10_000,
        )
        assert qty == 50
        actual_risk = qty * 2
        assert actual_risk == 100  # exactly 1% of 10000

    def test_small_capital_tight_sl(self):
        # capital=10000, entry=500, sl=498, risk_per_share=2
        # risk_amount = 100, qty = floor(100/2) = 50
        # position_value = 50*500 = 25000 (250% of capital!)
        # This is fine — SL controls the risk, not position value.
        # Broker margin requirements are the external constraint.
        engine = _engine()
        qty = engine.calculate_quantity(
            _make_signal(entry=500, sl=498, tp=506), capital=10_000,
        )
        assert qty == 50
        position_value = qty * 500
        assert position_value == 25_000  # 250% of capital — risk is still only 1%
        actual_risk = qty * 2
        assert actual_risk == 100  # 1% of capital

    def test_small_capital_wide_sl(self):
        # capital=10000, entry=500, sl=450, risk_per_share=50
        # risk_amount = 100, qty = floor(100/50) = 2
        # position_value = 2*500 = 1000 (10% of capital)
        engine = _engine()
        qty = engine.calculate_quantity(
            _make_signal(entry=500, sl=450, tp=600), capital=10_000,
        )
        assert qty == 2
        actual_risk = qty * 50
        assert actual_risk == 100  # 1% of capital

    def test_large_capital(self):
        # capital=500000, entry=1187.9, sl=1182.16, risk_per_share=5.74
        # risk_amount = 5000, qty = floor(5000/5.74) = 871
        engine = _engine()
        qty = engine.calculate_quantity(
            _make_signal(entry=1187.9, sl=1182.16, tp=1194.79), capital=500_000,
        )
        assert qty == 871
        actual_risk = qty * 5.74
        assert actual_risk == pytest.approx(5000, abs=6)  # ~1%


class TestPctOfCapitalSizing:
    def test_basic_calculation(self):
        # allocation = 100000 * 0.05 = 5000, qty = floor(5000/2500) = 2
        engine = _engine(sizing_mode="pct_of_capital")
        qty = engine.calculate_quantity(_make_signal(entry=2500), capital=100_000)
        assert qty == 2

    def test_with_different_capital(self):
        # allocation = 200000 * 0.05 = 10000, qty = floor(10000/2500) = 4
        engine = _engine(sizing_mode="pct_of_capital")
        qty = engine.calculate_quantity(_make_signal(entry=2500), capital=200_000)
        assert qty == 4

    def test_returns_zero_when_too_expensive(self):
        # allocation = 100000 * 0.01 = 1000, entry = 50000
        # qty = floor(1000/50000) = 0 -> skip
        engine = _engine(sizing_mode="pct_of_capital", pct_of_capital=0.01)
        qty = engine.calculate_quantity(_make_signal(entry=50_000), capital=100_000)
        assert qty == 0


class TestDayStartCapital:
    """use_day_start_capital: caches first capital fetch for equal risk per trade."""

    def test_caches_first_capital(self):
        engine = _engine(use_day_start_capital=True)
        # First call caches 100K
        assert engine.get_sizing_capital(100_000) == 100_000
        # Second call with different live capital returns cached value
        assert engine.get_sizing_capital(80_000) == 100_000
        assert engine.get_sizing_capital(50_000) == 100_000

    def test_disabled_returns_live_capital(self):
        engine = _engine(use_day_start_capital=False)
        assert engine.get_sizing_capital(100_000) == 100_000
        assert engine.get_sizing_capital(80_000) == 80_000
        assert engine.get_sizing_capital(50_000) == 50_000

    def test_equal_sizing_across_trades(self):
        # With day-start capital, all trades get same qty
        engine = _engine(use_day_start_capital=True)
        signal = _make_signal(entry=380, sl=377.57, tp=385)
        # Simulate: first trade gets 100K, subsequent get less (margin blocked)
        capitals = [100_000, 80_000, 60_000, 40_000, 20_000]
        quantities = []
        for cap in capitals:
            sizing_cap = engine.get_sizing_capital(cap)
            qty = engine.calculate_quantity(signal, capital=sizing_cap)
            quantities.append(qty)
        # All should be identical (using cached 100K)
        assert all(q == quantities[0] for q in quantities)
        # risk = 100K * 1% = 1000, risk_per_share = 2.43, qty = floor(1000/2.43) = 411
        assert quantities[0] == 411

    def test_resets_on_new_day(self):
        engine = _engine(use_day_start_capital=True)
        engine.get_sizing_capital(100_000)
        assert engine._day_start_capital == 100_000

        # Simulate new day
        engine._day_start_capital = 0.0

        # New day, new capital
        engine.get_sizing_capital(120_000)
        assert engine._day_start_capital == 120_000

    def test_different_capital_levels(self):
        # 15K capital: risk=150, risk_per_share=15, qty=10
        e1 = _engine(use_day_start_capital=True)
        cap1 = e1.get_sizing_capital(15_000)
        assert e1.calculate_quantity(_make_signal(entry=2500, sl=2485), capital=cap1) == 10

        # 1L capital: risk=1000, qty=66
        e2 = _engine(use_day_start_capital=True)
        cap2 = e2.get_sizing_capital(100_000)
        assert e2.calculate_quantity(_make_signal(entry=2500, sl=2485), capital=cap2) == 66


class TestPriceFilter:
    """Filter stocks by price band to control tradeable universe."""

    def test_reject_below_min_price(self):
        engine = _engine(min_entry_price=50, max_entry_price=1500)
        qty = engine.calculate_quantity(
            _make_signal(entry=30, sl=28, tp=35), capital=100_000,
        )
        assert qty == 0

    def test_reject_above_max_price(self):
        engine = _engine(min_entry_price=50, max_entry_price=1500)
        qty = engine.calculate_quantity(
            _make_signal(entry=2000, sl=1990, tp=2030), capital=100_000,
        )
        assert qty == 0

    def test_allow_within_range(self):
        engine = _engine(min_entry_price=50, max_entry_price=1500)
        qty = engine.calculate_quantity(
            _make_signal(entry=500, sl=490, tp=520), capital=100_000,
        )
        assert qty == 100  # floor(1000/10) = 100

    def test_allow_at_boundaries(self):
        engine = _engine(min_entry_price=50, max_entry_price=1500)
        # At min boundary
        qty = engine.calculate_quantity(
            _make_signal(entry=50, sl=48, tp=55), capital=100_000,
        )
        assert qty == 500  # floor(1000/2) = 500
        # At max boundary
        qty = engine.calculate_quantity(
            _make_signal(entry=1500, sl=1490, tp=1530), capital=100_000,
        )
        assert qty == 100  # floor(1000/10) = 100

    def test_disabled_when_zero(self):
        # Default: both 0 = no filter
        engine = _engine()
        qty = engine.calculate_quantity(
            _make_signal(entry=5000, sl=4990, tp=5030), capital=100_000,
        )
        assert qty == 100  # floor(1000/10) = 100

    def test_only_min_set(self):
        engine = _engine(min_entry_price=100, max_entry_price=0)
        # Below min -> reject
        qty = engine.calculate_quantity(
            _make_signal(entry=50, sl=48, tp=55), capital=100_000,
        )
        assert qty == 0
        # Above min -> allow (no max)
        qty = engine.calculate_quantity(
            _make_signal(entry=5000, sl=4990, tp=5030), capital=100_000,
        )
        assert qty == 100

    def test_only_max_set(self):
        engine = _engine(min_entry_price=0, max_entry_price=500)
        # Below max -> allow
        qty = engine.calculate_quantity(
            _make_signal(entry=200, sl=190, tp=220), capital=100_000,
        )
        assert qty == 100
        # Above max -> reject
        qty = engine.calculate_quantity(
            _make_signal(entry=600, sl=590, tp=620), capital=100_000,
        )
        assert qty == 0


class TestZeroRiskPerShareReturnsZero:
    """Bug fix: risk_per_share=0 or entry<=0 must return 0, not 1."""

    def test_fixed_fractional_zero_risk_returns_zero(self):
        # entry == sl -> risk_per_share = 0 -> should skip, not force 1 share
        engine = _engine()
        qty = engine.calculate_quantity(
            _make_signal(entry=100, sl=100, tp=110), capital=100_000,
        )
        assert qty == 0

    def test_pct_of_capital_zero_entry_returns_zero(self):
        # entry = 0 -> division by zero -> should skip
        engine = _engine(sizing_mode="pct_of_capital")
        qty = engine.calculate_quantity(
            _make_signal(entry=0, sl=0, tp=0), capital=100_000,
        )
        assert qty == 0


class TestSlippageBuffer:
    """Slippage widens effective risk per share, reducing qty."""

    def test_slippage_reduces_quantity(self):
        # Without slippage: risk/share=10, qty=floor(1000/10)=100
        # With 5% slippage: adj_risk=10*1.05=10.5, qty=floor(1000/10.5)=95
        engine = _engine(slippage_factor=0.05)
        qty = engine.calculate_quantity(
            _make_signal(entry=500, sl=490, tp=520), capital=100_000,
        )
        assert qty == 95

    def test_zero_slippage_unchanged(self):
        # slippage_factor=0 -> same as before
        engine = _engine(slippage_factor=0)
        qty = engine.calculate_quantity(
            _make_signal(entry=500, sl=490, tp=520), capital=100_000,
        )
        assert qty == 100

    def test_slippage_with_small_capital(self):
        # capital=10000, risk=100, risk/share=5.74
        # adj_risk = 5.74 * 1.05 = 6.027
        # qty = floor(100/6.027) = 16 (was 17 without slippage)
        engine = _engine(slippage_factor=0.05)
        qty = engine.calculate_quantity(
            _make_signal(entry=1187.9, sl=1182.16, tp=1194.79), capital=10_000,
        )
        assert qty == 16

    def test_slippage_only_affects_fixed_fractional(self):
        # pct_of_capital mode is not risk-based, slippage doesn't apply
        engine = _engine(sizing_mode="pct_of_capital", slippage_factor=0.05)
        qty = engine.calculate_quantity(
            _make_signal(entry=2500, sl=2485, tp=2540), capital=100_000,
        )
        assert qty == 2  # same as without slippage


class TestExposureChecks:
    def test_within_limits(self):
        engine = _engine()
        assert engine.check_exposure() is True

    def test_daily_loss_limit_breached(self):
        engine = _engine()
        engine._last_known_capital = 100_000
        engine.daily_realised_loss = 3100
        assert engine.check_exposure() is False

    def test_weekly_loss_limit_breached(self):
        engine = _engine()
        engine._last_known_capital = 100_000
        engine.weekly_realised_loss = 6100
        assert engine.check_exposure() is False

    def test_monthly_loss_limit_breached(self):
        engine = _engine()
        engine._last_known_capital = 100_000
        engine.monthly_realised_loss = 10100
        assert engine.check_exposure() is False

    def test_max_open_positions_breached(self):
        engine = _engine()
        engine.open_positions = 3
        assert engine.check_exposure() is False

    def test_max_trades_per_day_breached(self):
        engine = _engine()
        engine.trades_today = 5
        assert engine.check_exposure() is False


class TestRecordTrade:
    def test_increments_counters(self):
        engine = _engine()
        engine.record_trade(symbol="RELIANCE")
        assert engine.trades_today == 1
        assert engine.open_positions == 1

    def test_accumulates_trades(self):
        engine = _engine()
        engine.record_trade(symbol="RELIANCE")
        engine.record_trade(symbol="TCS")
        assert engine.trades_today == 2
        assert engine.open_positions == 2


class TestRecordRejection:
    def test_rejection_frees_slot_and_uncounts_trade(self):
        engine = _engine()
        engine.record_trade(symbol="IIFL")
        assert engine.trades_today == 1
        assert engine.open_positions == 1

        engine.record_rejection(symbol="IIFL")
        assert engine.open_positions == 0
        assert engine.trades_today == 0  # broker rejection: slot AND trade count restored

    def test_rejection_does_not_go_negative(self):
        engine = _engine()
        engine.record_rejection(symbol="POONAWALLA")
        assert engine.trades_today == 0
        assert engine.open_positions == 0

    def test_rejection_does_not_touch_loss_counters(self):
        engine = _engine()
        engine.record_trade(symbol="ITCHOTELS")
        engine.record_rejection(symbol="ITCHOTELS")
        assert engine.daily_realised_loss == 0
        assert engine.weekly_realised_loss == 0


class TestRecordClose:
    def test_loss_updates_counters(self):
        engine = _engine()
        engine.open_positions = 1
        engine.record_close(pnl=-500, symbol="RELIANCE")
        assert engine.open_positions == 0
        assert engine.daily_realised_loss == 500
        assert engine.weekly_realised_loss == 500
        assert engine.monthly_realised_loss == 500

    def test_profit_does_not_add_loss(self):
        engine = _engine()
        engine.open_positions = 1
        engine.record_close(pnl=1000, symbol="RELIANCE")
        assert engine.open_positions == 0
        assert engine.daily_realised_loss == 0

    def test_open_positions_never_negative(self):
        engine = _engine()
        engine.open_positions = 0
        engine.record_close(pnl=100, symbol="RELIANCE")
        assert engine.open_positions == 0


class TestDailyReset:
    def test_counters_reset_on_new_day(self):
        engine = _engine()
        engine.record_trade(symbol="RELIANCE")
        engine.record_close(pnl=-500, symbol="RELIANCE")
        assert engine.daily_realised_loss == 500

        engine._current_day = -1
        assert engine.check_exposure() is True
        assert engine.trades_today == 0
        assert engine.daily_realised_loss == 0


class TestUnrealisedDrawdown:
    def test_initial_unrealised_loss_is_zero(self):
        engine = _engine()
        assert engine.unrealised_loss == 0.0

    def test_update_unrealised_sets_value(self):
        engine = _engine()
        engine.update_unrealised(300.0)
        assert engine.unrealised_loss == 300.0

    def test_update_unrealised_replaces_not_accumulates(self):
        engine = _engine()
        engine.update_unrealised(300.0)
        engine.update_unrealised(500.0)
        assert engine.unrealised_loss == 500.0

    def test_combined_loss_blocks_when_over_daily_limit(self):
        # daily_loss_limit=0.03, capital=100_000 -> limit=3000
        # realised=2000, unrealised=1001 -> combined=3001 -> block
        engine = _engine(daily_loss_limit=0.03)
        engine._last_known_capital = 100_000
        engine.daily_realised_loss = 2000.0
        engine.update_unrealised(1001.0)
        assert engine.check_exposure() is False

    def test_combined_loss_allows_when_under_daily_limit(self):
        # realised=2000, unrealised=999 -> combined=2999 < 3000 -> allow
        engine = _engine(daily_loss_limit=0.03)
        engine._last_known_capital = 100_000
        engine.daily_realised_loss = 2000.0
        engine.update_unrealised(999.0)
        assert engine.check_exposure() is True

    def test_unrealised_zero_does_not_affect_existing_checks(self):
        # No unrealised loss -> behaves same as before
        engine = _engine(daily_loss_limit=0.03)
        engine._last_known_capital = 100_000
        engine.daily_realised_loss = 3100.0
        engine.update_unrealised(0.0)
        assert engine.check_exposure() is False

    def test_unrealised_loss_reset_on_new_day(self):
        engine = _engine()
        engine.update_unrealised(500.0)
        engine._current_day = -1
        engine.check_exposure()
        assert engine.unrealised_loss == 0.0


class TestSectorCorrelation:
    def test_can_trade_sector_initially_true(self):
        engine = _engine(max_positions_per_sector=2)
        assert engine.can_trade_sector("HDFCBANK") is True

    def test_blocks_third_banking_stock(self):
        engine = _engine(max_positions_per_sector=2)
        engine.record_trade(symbol="HDFCBANK")
        engine.record_trade(symbol="SBIN")
        assert engine.can_trade_sector("SBIN") is False

    def test_allows_different_sector(self):
        engine = _engine(max_positions_per_sector=2)
        engine.record_trade(symbol="HDFCBANK")
        engine.record_trade(symbol="SBIN")
        # IT sector should still be open
        assert engine.can_trade_sector("TCS") is True

    def test_unmapped_symbol_allowed(self):
        engine = _engine(max_positions_per_sector=1)
        engine.record_trade(symbol="HDFCBANK")
        # UNKNOWN is not in any sector -> allowed
        assert engine.can_trade_sector("UNKNOWN") is True

    def test_disabled_when_zero(self):
        engine = _engine(max_positions_per_sector=0)
        engine.record_trade(symbol="HDFCBANK")
        engine.record_trade(symbol="SBIN")
        # limit disabled -> always True
        assert engine.can_trade_sector("HDFCBANK") is True

    def test_close_reopens_sector_slot(self):
        engine = _engine(max_positions_per_sector=1)
        engine.record_trade(symbol="HDFCBANK")
        assert engine.can_trade_sector("SBIN") is False
        engine.open_positions = 1
        engine.record_close(pnl=100.0, symbol="HDFCBANK")
        assert engine.can_trade_sector("SBIN") is True

    def test_reset_clears_sector_counts(self):
        engine = _engine(max_positions_per_sector=1)
        engine.record_trade(symbol="HDFCBANK")
        assert engine.can_trade_sector("SBIN") is False
        engine._current_day = -1
        engine.check_exposure()
        assert engine.can_trade_sector("SBIN") is True


class TestCorrelationRisk:
    def test_can_trade_symbol_initially_true(self):
        engine = _engine(max_positions_per_symbol=1)
        assert engine.can_trade_symbol("RELIANCE") is True

    def test_blocks_duplicate_symbol(self):
        engine = _engine(max_positions_per_symbol=1)
        engine.record_trade(symbol="RELIANCE")
        assert engine.can_trade_symbol("RELIANCE") is False

    def test_allows_different_symbol(self):
        engine = _engine(max_positions_per_symbol=1)
        engine.record_trade(symbol="RELIANCE")
        assert engine.can_trade_symbol("TCS") is True

    def test_close_reopens_slot(self):
        engine = _engine(max_positions_per_symbol=1)
        engine.record_trade(symbol="RELIANCE")
        engine.open_positions = 1
        assert engine.can_trade_symbol("RELIANCE") is False
        engine.record_close(pnl=100.0, symbol="RELIANCE")
        assert engine.can_trade_symbol("RELIANCE") is True

    def test_disabled_when_zero(self):
        engine = _engine(max_positions_per_symbol=0)
        engine.record_trade(symbol="RELIANCE")
        engine.record_trade(symbol="RELIANCE")
        assert engine.can_trade_symbol("RELIANCE") is True

    def test_multiple_allowed_when_configured(self):
        engine = _engine(max_positions_per_symbol=2)
        engine.record_trade(symbol="RELIANCE")
        assert engine.can_trade_symbol("RELIANCE") is True
        engine.record_trade(symbol="RELIANCE")
        assert engine.can_trade_symbol("RELIANCE") is False

    def test_reset_clears_symbol_counts(self):
        engine = _engine(max_positions_per_symbol=1)
        engine.record_trade(symbol="RELIANCE")
        assert engine.can_trade_symbol("RELIANCE") is False
        engine._current_day = -1
        engine.check_exposure()
        assert engine.can_trade_symbol("RELIANCE") is True


class TestCapacityStatus:
    def test_initial_status(self):
        engine = _engine()
        status = engine.capacity_status()
        assert status == "0/3 positions open"

    def test_after_trade(self):
        engine = _engine()
        engine.open_positions = 2
        status = engine.capacity_status()
        assert status == "2/3 positions open"


class TestRestartSafeCounters:
    def test_counters_restored_from_store_on_init(self, tmp_path):
        db_path = str(tmp_path / "risk.db")
        store = RiskStore(db_path)
        from datetime import datetime, timezone, timedelta
        _IST = timezone(timedelta(hours=5, minutes=30))
        today = datetime.now(_IST).date()
        store.save("live", today, trades_today=3, daily_loss=1500.0, open_positions=2)

        engine = _engine(store=store, trade_mode="live")
        assert engine.trades_today == 3
        assert engine.daily_realised_loss == 1500.0
        assert engine.open_positions == 2

    def test_no_store_works_as_before(self):
        engine = _engine()
        assert engine.trades_today == 0
        assert engine.daily_realised_loss == 0.0

    def test_record_trade_persists_to_store(self, tmp_path):
        db_path = str(tmp_path / "risk.db")
        store = RiskStore(db_path)
        engine = _engine(store=store, trade_mode="live")
        engine._last_known_capital = 100_000
        engine.record_trade(symbol="RELIANCE")

        from datetime import datetime, timezone, timedelta
        _IST = timezone(timedelta(hours=5, minutes=30))
        today = datetime.now(_IST).date()
        row = store.load("live", today)
        assert row["trades_today"] == 1
        assert row["open_positions"] == 1

    def test_record_close_persists_to_store(self, tmp_path):
        db_path = str(tmp_path / "risk.db")
        store = RiskStore(db_path)
        engine = _engine(store=store, trade_mode="live")
        engine.open_positions = 1
        engine.record_close(pnl=-400.0, symbol="RELIANCE")

        from datetime import datetime, timezone, timedelta
        _IST = timezone(timedelta(hours=5, minutes=30))
        today = datetime.now(_IST).date()
        row = store.load("live", today)
        assert row["daily_loss"] == 400.0
        assert row["open_positions"] == 0

    def test_mode_isolation_live_vs_sandbox(self, tmp_path):
        db_path = str(tmp_path / "risk.db")
        store_live = RiskStore(db_path)
        store_sandbox = RiskStore(db_path)

        engine_live = _engine(store=store_live, trade_mode="live")
        engine_sandbox = _engine(store=store_sandbox, trade_mode="sandbox")

        engine_live.record_trade(symbol="RELIANCE")
        engine_live.record_trade(symbol="TCS")

        engine_sandbox.record_trade(symbol="RELIANCE")

        from datetime import datetime, timezone, timedelta
        _IST = timezone(timedelta(hours=5, minutes=30))
        today = datetime.now(_IST).date()
        live_row = store_live.load("live", today)
        sandbox_row = store_sandbox.load("sandbox", today)

        assert live_row["trades_today"] == 2
        assert sandbox_row["trades_today"] == 1

    def test_daily_reset_persists_zeroed_counters(self, tmp_path):
        db_path = str(tmp_path / "risk.db")
        store = RiskStore(db_path)
        engine = _engine(store=store, trade_mode="live")
        engine.record_trade(symbol="RELIANCE")
        engine.record_close(pnl=-200.0, symbol="RELIANCE")

        # Simulate new day
        engine._current_day = -1
        engine.check_exposure()

        from datetime import date, timezone
        from datetime import datetime
        today = datetime.now(timezone.utc).date()
        row = store.load("live", today)
        assert row["trades_today"] == 0
        assert row["daily_loss"] == 0.0
        assert row["open_positions"] == 0

    def test_engine_with_no_store_does_not_persist(self, tmp_path):
        # Just ensure no exception is raised when store=None
        engine = _engine()
        engine.record_trade(symbol="RELIANCE")
        engine.record_close(pnl=-100.0, symbol="RELIANCE")
        assert engine.trades_today == 1


class TestMaxSlPctForSizing:
    """max_sl_pct_for_sizing caps the effective SL distance used in position sizing.

    The real SL ORDER still goes at signal.sl — only qty calculation is affected.
    When actual SL > entry × max_sl_pct, qty is computed as if SL = entry × max_sl_pct.
    """

    def _engine_capped(self, cap: float = 0.015) -> RiskEngine:
        return _engine(slippage_factor=0.0, max_sl_pct_for_sizing=cap)

    def test_wide_sl_qty_is_capped(self):
        # JSWENERGY-like: entry=490.8, sl=470.29 (SL dist=20.51, 4.18% of entry)
        # With cap=1.5%: effective_sl = 490.8 × 0.015 = 7.362
        # risk_amount = 15000 × 0.01 = 150
        # qty = floor(150 / 7.362) = 20
        engine = _engine(risk_per_trade=0.01, slippage_factor=0.0, max_sl_pct_for_sizing=0.015)
        sig = _make_signal(entry=490.8, sl=470.29, tp=505.2)
        qty = engine.calculate_quantity(sig, capital=15_000)
        expected_sl = 490.8 * 0.015  # 7.362
        expected = int(150 / expected_sl)
        assert qty == expected

    def test_tight_sl_unchanged(self):
        # SL = 1.0% of entry < 1.5% cap → no capping
        # entry=500, sl=495 → dist=5, cap_dist=7.5 → actual dist used = 5
        engine = _engine(risk_per_trade=0.01, slippage_factor=0.0, max_sl_pct_for_sizing=0.015)
        sig = _make_signal(entry=500.0, sl=495.0, tp=515.0)
        qty_capped = engine.calculate_quantity(sig, capital=100_000)
        engine_no_cap = _engine(risk_per_trade=0.01, slippage_factor=0.0)
        qty_no_cap = engine_no_cap.calculate_quantity(sig, capital=100_000)
        assert qty_capped == qty_no_cap  # no change when SL within cap

    def test_cap_exactly_at_threshold_no_change(self):
        # SL = exactly cap → no capping applied (equal, not greater)
        # entry=500, cap=1.5%, sl_dist=7.5 → sl=492.5
        engine = _engine(risk_per_trade=0.01, slippage_factor=0.0, max_sl_pct_for_sizing=0.015)
        sl_at_cap = 500.0 - (500.0 * 0.015)  # = 492.5
        sig = _make_signal(entry=500.0, sl=sl_at_cap, tp=515.0)
        qty_capped = engine.calculate_quantity(sig, capital=100_000)
        engine_no_cap = _engine(risk_per_trade=0.01, slippage_factor=0.0)
        qty_no_cap = engine_no_cap.calculate_quantity(sig, capital=100_000)
        assert qty_capped == qty_no_cap

    def test_cap_disabled_when_zero(self):
        # max_sl_pct_for_sizing=0 means disabled — wide SL is not capped
        engine_cap0 = _engine(risk_per_trade=0.01, slippage_factor=0.0, max_sl_pct_for_sizing=0.0)
        engine_default = _engine(risk_per_trade=0.01, slippage_factor=0.0)
        sig = _make_signal(entry=490.8, sl=470.29, tp=505.2)
        assert engine_cap0.calculate_quantity(sig, capital=15_000) == \
               engine_default.calculate_quantity(sig, capital=15_000)

    def test_cap_improves_capital_utilisation(self):
        # With cap: wide-SL stock should produce more shares than without
        sig = _make_signal(entry=490.8, sl=470.29, tp=505.2)  # 4.18% SL
        engine_capped = _engine(risk_per_trade=0.01, slippage_factor=0.0, max_sl_pct_for_sizing=0.015)
        engine_uncapped = _engine(risk_per_trade=0.01, slippage_factor=0.0)
        qty_capped = engine_capped.calculate_quantity(sig, capital=15_000)
        qty_uncapped = engine_uncapped.calculate_quantity(sig, capital=15_000)
        assert qty_capped > qty_uncapped

    def test_short_direction_capping(self):
        # SHORT: entry=500, sl=520 → dist=20 (4% of entry)
        # With cap=1.5%: effective_dist=7.5 → qty=floor(1000/7.5)=133
        engine = _engine(risk_per_trade=0.01, slippage_factor=0.0, max_sl_pct_for_sizing=0.015)
        sig = _make_signal(entry=500.0, sl=520.0, tp=480.0, direction=Direction.SHORT)
        qty = engine.calculate_quantity(sig, capital=100_000)
        expected = int(1000 / (500.0 * 0.015))
        assert qty == expected

    def test_with_slippage_factor_combined(self):
        # Slippage applied after SL cap
        # entry=490.8, cap=1.5% → eff_sl=7.362, slip=10% → adjusted=8.098
        # risk_amount=150, qty=floor(150/8.098)=18
        engine = _engine(risk_per_trade=0.01, slippage_factor=0.10, max_sl_pct_for_sizing=0.015)
        sig = _make_signal(entry=490.8, sl=470.29, tp=505.2)
        qty = engine.calculate_quantity(sig, capital=15_000)
        expected_rps = 490.8 * 0.015 * 1.10
        expected = int(150 / expected_rps)
        assert qty == expected


@pytest.mark.unit
class TestSoftBlacklist:
    """Soft-blacklist symbols get qty scaled by per-strategy multiplier.

    Use case: Q1→Q2 grade-flip stocks (e.g. CANBK, FEDERALBNK) where full block
    discards optionality. Risk engine reduces qty so the stock keeps
    participating but with limited downside.
    """

    def test_soft_listed_symbol_qty_halved_default(self):
        engine = _engine(
            soft_blacklist={"ORB": frozenset({"CANBK"})},
            soft_blacklist_multipliers={"ORB": 0.5},
        )
        # Baseline qty for this signal would be 66 (TestFixedFractionalSizing.test_basic_calculation)
        sig = _make_signal(symbol="CANBK", entry=2500, sl=2485)
        qty = engine.calculate_quantity(sig, capital=100_000)
        assert qty == 33  # floor(66 * 0.5)

    def test_non_soft_symbol_qty_unchanged(self):
        engine = _engine(
            soft_blacklist={"ORB": frozenset({"CANBK"})},
            soft_blacklist_multipliers={"ORB": 0.5},
        )
        sig = _make_signal(symbol="RELIANCE", entry=2500, sl=2485)
        qty = engine.calculate_quantity(sig, capital=100_000)
        assert qty == 66  # unchanged

    def test_soft_check_is_strategy_scoped(self):
        # Symbol soft-listed for ORB only — RSI-TP-MR signal must not scale
        engine = _engine(
            soft_blacklist={"ORB": frozenset({"CANBK"})},
            soft_blacklist_multipliers={"ORB": 0.5},
        )
        sig = _make_signal(strategy="RSI-TP-MR", symbol="CANBK", entry=2500, sl=2485)
        qty = engine.calculate_quantity(sig, capital=100_000)
        assert qty == 66  # not in RSI-TP-MR's soft set

    def test_soft_multiplier_one_is_noop(self):
        engine = _engine(
            soft_blacklist={"ORB": frozenset({"CANBK"})},
            soft_blacklist_multipliers={"ORB": 1.0},
        )
        sig = _make_signal(symbol="CANBK", entry=2500, sl=2485)
        qty = engine.calculate_quantity(sig, capital=100_000)
        assert qty == 66

    def test_soft_multiplier_zero_returns_zero(self):
        # 0 multiplier => qty rounds to 0 => skip via existing path
        engine = _engine(
            soft_blacklist={"ORB": frozenset({"CANBK"})},
            soft_blacklist_multipliers={"ORB": 0.0},
        )
        sig = _make_signal(symbol="CANBK", entry=2500, sl=2485)
        qty = engine.calculate_quantity(sig, capital=100_000)
        assert qty == 0

    def test_soft_scaling_after_slippage_buffer_preserves_risk_shape(self):
        # The 1% risk guarantee is computed on risk_per_share (slippage applied there).
        # Soft scaling reduces final share count but does NOT widen risk_per_share.
        # So actual risk = soft_qty * risk_per_share = (full_qty * 0.5) * risk_per_share
        # = exactly half of the configured 1% risk amount. Validate via comparison.
        engine_full = _engine(slippage_factor=0.10)
        engine_soft = _engine(
            slippage_factor=0.10,
            soft_blacklist={"ORB": frozenset({"CANBK"})},
            soft_blacklist_multipliers={"ORB": 0.5},
        )
        sig_full = _make_signal(symbol="RELIANCE", entry=2500, sl=2485)
        sig_soft = _make_signal(symbol="CANBK", entry=2500, sl=2485)
        qty_full = engine_full.calculate_quantity(sig_full, capital=100_000)
        qty_soft = engine_soft.calculate_quantity(sig_soft, capital=100_000)
        assert qty_soft == qty_full // 2

    def test_no_soft_blacklist_configured_is_noop(self):
        # When soft_blacklist is None / empty, behavior is identical to baseline.
        engine = _engine()
        sig = _make_signal(symbol="CANBK", entry=2500, sl=2485)
        qty = engine.calculate_quantity(sig, capital=100_000)
        assert qty == 66

    def test_default_multiplier_when_strategy_missing_from_multiplier_map(self):
        # Defensive: if soft_blacklist has the symbol but multiplier map is missing
        # the strategy key, fall back to 0.5.
        engine = _engine(
            soft_blacklist={"ORB": frozenset({"CANBK"})},
            soft_blacklist_multipliers={},  # explicitly empty
        )
        sig = _make_signal(symbol="CANBK", entry=2500, sl=2485)
        qty = engine.calculate_quantity(sig, capital=100_000)
        assert qty == 33  # 66 * 0.5
