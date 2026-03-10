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
        "max_position_size": 0,           # disabled — pure risk-based sizing
        "daily_loss_limit": 0.03,
        "weekly_loss_limit": 0.06,
        "monthly_loss_limit": 0.10,
        "max_open_positions": 3,
        "max_trades_per_day": 5,
        "min_entry_price": 0,
        "max_entry_price": 0,
        "slippage_factor": 0.0,
        "max_portfolio_heat": 0.06,
        "margin_multiplier": {"MIS": 0.20, "NRML": 0.25, "CNC": 1.0},
        "max_capital_utilization": 0.0,  # disabled by default; enable per test
        "default_product": "MIS",
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


class TestMaxPositionSizeCapLegacy:
    """max_position_size still works when explicitly enabled."""

    def test_quantity_capped_by_max_position_size(self):
        # max_position_value = 100000 * 0.20 = 20000, max_qty = 200
        engine = _engine(risk_per_trade=0.50, max_position_size=0.20)
        qty = engine.calculate_quantity(
            _make_signal(entry=100, sl=90, tp=130), capital=100_000,
        )
        assert qty == 200

    def test_cap_returns_zero_when_stock_too_expensive(self):
        # capital=10000, max_position_size=0.20, max_value=2000
        # entry=3000 -> max_qty=floor(2000/3000)=0 -> return 0
        engine = _engine(max_position_size=0.20)
        qty = engine.calculate_quantity(
            _make_signal(entry=3000, sl=2990, tp=3030), capital=10_000,
        )
        assert qty == 0


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
        engine.record_trade()
        assert engine.trades_today == 1
        assert engine.open_positions == 1

    def test_accumulates_trades(self):
        engine = _engine()
        engine.record_trade()
        engine.record_trade()
        assert engine.trades_today == 2
        assert engine.open_positions == 2


class TestRecordClose:
    def test_loss_updates_counters(self):
        engine = _engine()
        engine.open_positions = 1
        engine.record_close(pnl=-500)
        assert engine.open_positions == 0
        assert engine.daily_realised_loss == 500
        assert engine.weekly_realised_loss == 500
        assert engine.monthly_realised_loss == 500

    def test_profit_does_not_add_loss(self):
        engine = _engine()
        engine.open_positions = 1
        engine.record_close(pnl=1000)
        assert engine.open_positions == 0
        assert engine.daily_realised_loss == 0

    def test_open_positions_never_negative(self):
        engine = _engine()
        engine.open_positions = 0
        engine.record_close(pnl=100)
        assert engine.open_positions == 0


class TestDailyReset:
    def test_counters_reset_on_new_day(self):
        engine = _engine()
        engine.record_trade()
        engine.record_close(pnl=-500)
        assert engine.daily_realised_loss == 500

        engine._current_day = -1
        assert engine.check_exposure() is True
        assert engine.trades_today == 0
        assert engine.daily_realised_loss == 0


class TestPortfolioHeat:
    def test_initial_heat_is_zero(self):
        engine = _engine()
        assert engine.portfolio_heat == 0.0

    def test_add_heat_accumulates(self):
        engine = _engine()
        engine.add_heat(qty=10, risk_per_share=50.0)
        assert engine.portfolio_heat == 500.0

    def test_add_heat_multiple_positions(self):
        engine = _engine()
        engine.add_heat(qty=10, risk_per_share=50.0)
        engine.add_heat(qty=5, risk_per_share=20.0)
        assert engine.portfolio_heat == 600.0

    def test_remove_heat_decreases(self):
        engine = _engine()
        engine.add_heat(qty=10, risk_per_share=50.0)
        engine.remove_heat(qty=10, risk_per_share=50.0)
        assert engine.portfolio_heat == 0.0

    def test_remove_heat_never_goes_negative(self):
        engine = _engine()
        engine.add_heat(qty=5, risk_per_share=10.0)
        engine.remove_heat(qty=100, risk_per_share=100.0)
        assert engine.portfolio_heat == 0.0

    def test_check_exposure_blocks_when_heat_exceeds_limit(self):
        # max_portfolio_heat=0.06, capital=100_000 -> limit=6000
        # heat = 6001 -> block
        engine = _engine(max_portfolio_heat=0.06)
        engine._last_known_capital = 100_000
        engine.portfolio_heat = 6001.0
        assert engine.check_exposure() is False

    def test_check_exposure_allows_at_limit_boundary(self):
        # heat exactly at limit (5999 < 6000) -> allow
        engine = _engine(max_portfolio_heat=0.06)
        engine._last_known_capital = 100_000
        engine.portfolio_heat = 5999.0
        assert engine.check_exposure() is True

    def test_check_exposure_skips_heat_when_no_capital(self):
        # capital=0 -> heat check skipped
        engine = _engine(max_portfolio_heat=0.06)
        engine._last_known_capital = 0
        engine.portfolio_heat = 99999.0
        assert engine.check_exposure() is True

    def test_heat_reset_on_new_day(self):
        engine = _engine()
        engine.add_heat(qty=10, risk_per_share=50.0)
        assert engine.portfolio_heat == 500.0
        engine._current_day = -1
        engine.check_exposure()
        assert engine.portfolio_heat == 0.0


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


class TestMarginAwareQtyCap:
    """Margin-aware quantity cap: position value must be affordable after leverage.

    These tests use max_capital_utilization=0 to isolate just the affordability cap.
    The remaining-budget cap is tested separately in TestCapitalUtilization.
    """

    def test_cnc_caps_qty_to_affordable(self):
        # capital=10000, entry=1188, CNC margin=1.0 (no leverage)
        # risk-based: risk_amount=100, risk/share=5.74, qty=17
        # affordable for CNC: floor(10000/(1188*1.0))=8
        # margin cap should reduce 17 -> 8
        engine = _engine(
            margin_multiplier={"MIS": 0.20, "NRML": 0.25, "CNC": 1.0},
            max_capital_utilization=0.0,  # disabled so only affordability cap applies
            default_product="CNC",
        )
        qty = engine.calculate_quantity(
            _make_signal(entry=1188.0, sl=1182.26, tp=1194.74), capital=10_000
        )
        assert qty == 8

    def test_mis_does_not_cap(self):
        # capital=10000, entry=1188, MIS margin=0.20 (5x leverage)
        # risk-based: risk_amount=100, risk/share=5.74, qty=17
        # affordable for MIS: floor(10000/(1188*0.20))=42
        # 17 < 42, so no cap applied
        engine = _engine(
            margin_multiplier={"MIS": 0.20, "NRML": 0.25, "CNC": 1.0},
            max_capital_utilization=0.0,  # disabled so only affordability cap applies
            default_product="MIS",
        )
        qty = engine.calculate_quantity(
            _make_signal(entry=1188.0, sl=1182.26, tp=1194.74), capital=10_000
        )
        assert qty == 17

    def test_unknown_product_defaults_to_full_margin(self):
        # Product "FUTURES" not in dict -> defaults to margin_rate=1.0 (full margin)
        # capital=10000, entry=1188, margin_rate=1.0
        # affordable: floor(10000/(1188*1.0))=8
        # risk-based qty=17 -> capped to 8
        engine = _engine(
            margin_multiplier={"MIS": 0.20, "NRML": 0.25, "CNC": 1.0},
            max_capital_utilization=0.0,  # disabled so only affordability cap applies
            default_product="FUTURES",
        )
        qty = engine.calculate_quantity(
            _make_signal(entry=1188.0, sl=1182.26, tp=1194.74), capital=10_000
        )
        assert qty == 8

    def test_margin_cap_logs_when_reduced(self):
        # Verify the cap actually reduces qty (check return value)
        engine = _engine(
            margin_multiplier={"MIS": 0.20, "NRML": 0.25, "CNC": 1.0},
            max_capital_utilization=0.0,  # disabled so only affordability cap applies
            default_product="CNC",
        )
        qty = engine.calculate_quantity(
            _make_signal(entry=1188.0, sl=1182.26, tp=1194.74), capital=10_000
        )
        # qty should be 8 (capped from 17), not 17 (uncapped risk-based)
        assert qty < 17
        assert qty == 8

    def test_zero_entry_returns_zero(self):
        # entry=0 -> margin calc would divide by zero -> must return 0 safely
        engine = _engine(
            margin_multiplier={"MIS": 0.20, "NRML": 0.25, "CNC": 1.0},
            max_capital_utilization=0.0,
            default_product="MIS",
        )
        qty = engine.calculate_quantity(
            _make_signal(entry=0, sl=0, tp=0), capital=10_000
        )
        assert qty == 0

    def test_signal_product_overrides_default(self):
        # signal.product = "CNC" should override default_product = "MIS"
        engine = _engine(
            margin_multiplier={"MIS": 0.20, "NRML": 0.25, "CNC": 1.0},
            max_capital_utilization=0.0,  # disabled so only affordability cap applies
            default_product="MIS",
        )
        qty = engine.calculate_quantity(
            _make_signal(entry=1188.0, sl=1182.26, tp=1194.74, product="CNC"),
            capital=10_000,
        )
        # With CNC: affordable=8, risk-based=17 -> capped to 8
        assert qty == 8


class TestCapitalUtilization:
    """Track committed margin across open positions to prevent over-commitment."""

    def test_add_margin_accumulates(self):
        engine = _engine(
            margin_multiplier={"MIS": 0.20, "NRML": 0.25, "CNC": 1.0},
            max_capital_utilization=0.80,
            default_product="MIS",
        )
        engine.add_margin(qty=10, entry_price=1000.0, product="MIS")
        engine.add_margin(qty=5, entry_price=2000.0, product="CNC")
        # MIS: 10*1000*0.20=2000, CNC: 5*2000*1.0=10000
        assert engine.committed_margin == pytest.approx(12000.0)

    def test_remove_margin_decreases(self):
        engine = _engine(
            margin_multiplier={"MIS": 0.20, "NRML": 0.25, "CNC": 1.0},
            max_capital_utilization=0.80,
            default_product="MIS",
        )
        engine.add_margin(qty=10, entry_price=1000.0, product="MIS")
        engine.remove_margin(qty=10, entry_price=1000.0, product="MIS")
        # MIS: 10*1000*0.20=2000 added, then 2000 removed
        assert engine.committed_margin == pytest.approx(0.0)

    def test_remove_margin_never_negative(self):
        engine = _engine(
            margin_multiplier={"MIS": 0.20, "NRML": 0.25, "CNC": 1.0},
            max_capital_utilization=0.80,
            default_product="MIS",
        )
        engine.add_margin(qty=5, entry_price=100.0, product="MIS")
        # Remove more than was added
        engine.remove_margin(qty=1000, entry_price=100.0, product="MIS")
        assert engine.committed_margin == 0.0

    def test_check_exposure_blocks_when_overcommitted(self):
        # capital=100000, max_utilization=0.80 -> limit=80000
        # committed_margin=80000 -> ratio=1.0 >= 0.80 -> block
        engine = _engine(
            margin_multiplier={"MIS": 0.20, "NRML": 0.25, "CNC": 1.0},
            max_capital_utilization=0.80,
            default_product="MIS",
        )
        engine._last_known_capital = 100_000
        engine.committed_margin = 80_000.0
        assert engine.check_exposure() is False

    def test_check_exposure_allows_under_limit(self):
        # capital=100000, max_utilization=0.80 -> limit=80000
        # committed_margin=70000 -> ratio=0.70 < 0.80 -> allow
        engine = _engine(
            margin_multiplier={"MIS": 0.20, "NRML": 0.25, "CNC": 1.0},
            max_capital_utilization=0.80,
            default_product="MIS",
        )
        engine._last_known_capital = 100_000
        engine.committed_margin = 70_000.0
        assert engine.check_exposure() is True

    def test_utilization_disabled_when_zero(self):
        # max_capital_utilization=0 -> skip the check entirely
        engine = _engine(
            margin_multiplier={"MIS": 0.20, "NRML": 0.25, "CNC": 1.0},
            max_capital_utilization=0.0,
            default_product="MIS",
        )
        engine._last_known_capital = 100_000
        engine.committed_margin = 999_000.0  # would fail if check ran
        assert engine.check_exposure() is True

    def test_remaining_margin_caps_qty(self):
        # capital=100000, max_utilization=0.80, already committed=70000
        # remaining budget = 80000-70000=10000
        # entry=1000, MIS margin=0.20 -> max_qty_by_margin=floor(10000/(1000*0.20))=50
        # risk-based gives, say, 100 -> capped to 50
        engine = _engine(
            risk_per_trade=0.50,  # high risk -> large qty without cap
            margin_multiplier={"MIS": 0.20, "NRML": 0.25, "CNC": 1.0},
            max_capital_utilization=0.80,
            default_product="MIS",
        )
        engine._last_known_capital = 100_000
        engine.committed_margin = 70_000.0
        qty = engine.calculate_quantity(
            _make_signal(entry=1000.0, sl=990.0, tp=1030.0), capital=100_000
        )
        # remaining=10000, max_qty_by_margin=floor(10000/(1000*0.20))=50
        assert qty == 50

    def test_committed_margin_resets_on_new_day(self):
        engine = _engine(
            margin_multiplier={"MIS": 0.20, "NRML": 0.25, "CNC": 1.0},
            max_capital_utilization=0.80,
            default_product="MIS",
        )
        engine.add_margin(qty=10, entry_price=1000.0, product="MIS")
        assert engine.committed_margin == pytest.approx(2000.0)
        engine._current_day = -1
        engine.check_exposure()
        assert engine.committed_margin == 0.0


class TestRestartSafeCounters:
    def test_counters_restored_from_store_on_init(self, tmp_path):
        db_path = str(tmp_path / "risk.db")
        store = RiskStore(db_path)
        from datetime import date, timezone
        from datetime import datetime
        today = datetime.now(timezone.utc).date()
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
        engine.record_trade()

        from datetime import date, timezone
        from datetime import datetime
        today = datetime.now(timezone.utc).date()
        row = store.load("live", today)
        assert row["trades_today"] == 1
        assert row["open_positions"] == 1

    def test_record_close_persists_to_store(self, tmp_path):
        db_path = str(tmp_path / "risk.db")
        store = RiskStore(db_path)
        engine = _engine(store=store, trade_mode="live")
        engine.open_positions = 1
        engine.record_close(pnl=-400.0)

        from datetime import date, timezone
        from datetime import datetime
        today = datetime.now(timezone.utc).date()
        row = store.load("live", today)
        assert row["daily_loss"] == 400.0
        assert row["open_positions"] == 0

    def test_mode_isolation_live_vs_sandbox(self, tmp_path):
        db_path = str(tmp_path / "risk.db")
        store_live = RiskStore(db_path)
        store_sandbox = RiskStore(db_path)

        engine_live = _engine(store=store_live, trade_mode="live")
        engine_sandbox = _engine(store=store_sandbox, trade_mode="sandbox")

        engine_live.record_trade()
        engine_live.record_trade()

        engine_sandbox.record_trade()

        from datetime import date, timezone
        from datetime import datetime
        today = datetime.now(timezone.utc).date()
        live_row = store_live.load("live", today)
        sandbox_row = store_sandbox.load("sandbox", today)

        assert live_row["trades_today"] == 2
        assert sandbox_row["trades_today"] == 1

    def test_daily_reset_persists_zeroed_counters(self, tmp_path):
        db_path = str(tmp_path / "risk.db")
        store = RiskStore(db_path)
        engine = _engine(store=store, trade_mode="live")
        engine.record_trade()
        engine.record_close(pnl=-200.0)

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
        engine.record_trade()
        engine.record_close(pnl=-100.0)
        assert engine.trades_today == 1
