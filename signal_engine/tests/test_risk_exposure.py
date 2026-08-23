"""Exposure limits: slots, concentration, correlation, capacity, blacklist."""


from signal_engine.tests.conftest import make_signal as _make_signal

from signal_engine.tests.risk_fixtures import _engine


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
