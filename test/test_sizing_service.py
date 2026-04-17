"""Unit tests for services/sizing_service.py — TDD RED phase.

Run with:
    PYTHONPATH=/home/anand/github/openalgo uv run pytest test/test_sizing_service.py -v
"""
import math
import pytest

from services.sizing_service import (
    VALID_SIZING_MODES,
    SizingInput,
    SizingResult,
    calculate_position_size,
    validate_sizing_input,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ff_input():
    """Standard fixed_fractional input: 35 000 capital, entry 820, SL 815."""
    return SizingInput(
        capital=35_000.0,
        entry_price=820.0,
        stop_loss=815.0,
        sizing_mode="fixed_fractional",
        risk_per_trade=0.01,
        slippage_factor=0.0,
        max_sl_pct_for_sizing=0.0,
        target=830.0,
        side="BUY",
    )


@pytest.fixture
def pct_input():
    """Standard pct_of_capital input: 50 000 capital, entry 500."""
    return SizingInput(
        capital=50_000.0,
        entry_price=500.0,
        stop_loss=490.0,
        sizing_mode="pct_of_capital",
        pct_of_capital=0.10,
        target=520.0,
        side="BUY",
    )


# ---------------------------------------------------------------------------
# SizingInput dataclass
# ---------------------------------------------------------------------------

class TestSizingInput:
    def test_frozen_immutable(self, ff_input):
        with pytest.raises((AttributeError, TypeError)):
            ff_input.capital = 99_000.0  # type: ignore[misc]

    def test_default_slippage(self):
        inp = SizingInput(
            capital=10_000.0,
            entry_price=100.0,
            stop_loss=95.0,
            sizing_mode="fixed_fractional",
        )
        assert inp.slippage_factor == 0.0

    def test_default_side_is_buy(self):
        inp = SizingInput(
            capital=10_000.0, entry_price=100.0, stop_loss=95.0, sizing_mode="fixed_fractional"
        )
        assert inp.side == "BUY"


# ---------------------------------------------------------------------------
# SizingResult dataclass
# ---------------------------------------------------------------------------

class TestSizingResult:
    def test_frozen_immutable(self):
        r = SizingResult(
            quantity=10,
            raw_quantity=10,
            risk_amount=100.0,
            risk_pct_of_capital=0.01,
            reward_amount=200.0,
            position_value=1000.0,
            rr_ratio=2.0,
            sl_distance_pct=0.05,
            skip_reason=None,
        )
        with pytest.raises((AttributeError, TypeError)):
            r.quantity = 0  # type: ignore[misc]

    def test_warnings_default_empty(self):
        r = SizingResult(
            quantity=5,
            raw_quantity=5,
            risk_amount=50.0,
            risk_pct_of_capital=0.005,
            reward_amount=100.0,
            position_value=500.0,
            rr_ratio=2.0,
            sl_distance_pct=0.02,
            skip_reason=None,
        )
        assert r.warnings == []


# ---------------------------------------------------------------------------
# fixed_fractional mode
# ---------------------------------------------------------------------------

class TestFixedFractional:
    def test_basic_quantity(self, ff_input):
        """35 000 * 0.01 / 5.0 = 70 shares."""
        result = calculate_position_size(ff_input)
        assert result.quantity == 70

    def test_risk_amount(self, ff_input):
        result = calculate_position_size(ff_input)
        assert math.isclose(result.risk_amount, 350.0, rel_tol=1e-6)

    def test_risk_pct_of_capital(self, ff_input):
        result = calculate_position_size(ff_input)
        assert math.isclose(result.risk_pct_of_capital, 0.01, rel_tol=1e-4)

    def test_position_value(self, ff_input):
        result = calculate_position_size(ff_input)
        assert math.isclose(result.position_value, 70 * 820.0, rel_tol=1e-6)

    def test_rr_ratio_with_target(self, ff_input):
        """(830 - 820) / (820 - 815) = 2.0."""
        result = calculate_position_size(ff_input)
        assert math.isclose(result.rr_ratio, 2.0, rel_tol=1e-6)

    def test_rr_ratio_no_target(self):
        inp = SizingInput(
            capital=10_000.0, entry_price=100.0, stop_loss=95.0,
            sizing_mode="fixed_fractional", target=0.0,
        )
        result = calculate_position_size(inp)
        assert result.rr_ratio == 0.0

    def test_sl_distance_pct(self, ff_input):
        """(820 - 815) / 820 ≈ 0.006097."""
        result = calculate_position_size(ff_input)
        expected = abs(820.0 - 815.0) / 820.0
        assert math.isclose(result.sl_distance_pct, expected, rel_tol=1e-6)

    def test_with_slippage_factor(self):
        """10 000 * 0.01 = 100; risk_per_share = 5 * 1.1 = 5.5; qty = floor(100/5.5) = 18."""
        inp = SizingInput(
            capital=10_000.0,
            entry_price=100.0,
            stop_loss=95.0,
            sizing_mode="fixed_fractional",
            risk_per_trade=0.01,
            slippage_factor=0.10,
        )
        result = calculate_position_size(inp)
        assert result.quantity == 18

    def test_max_sl_pct_for_sizing_caps_risk_per_share(self):
        """SL distance is 10 but entry*0.01 = 1; capped risk/share = 1; qty = floor(100/1) = 100."""
        inp = SizingInput(
            capital=10_000.0,
            entry_price=100.0,
            stop_loss=90.0,
            sizing_mode="fixed_fractional",
            risk_per_trade=0.01,
            max_sl_pct_for_sizing=0.01,
        )
        result = calculate_position_size(inp)
        assert result.quantity == 100

    def test_max_sl_pct_not_applied_when_sl_within_cap(self):
        """SL distance 2, entry 100, cap 0.05 (= 5). SL distance < cap, no capping."""
        inp = SizingInput(
            capital=10_000.0,
            entry_price=100.0,
            stop_loss=98.0,
            sizing_mode="fixed_fractional",
            risk_per_trade=0.01,
            max_sl_pct_for_sizing=0.05,
        )
        result = calculate_position_size(inp)
        assert result.quantity == math.floor(100.0 / 2.0)

    def test_zero_qty_when_stock_too_expensive(self):
        """risk_amount=10, risk_per_share=100 => qty=0."""
        inp = SizingInput(
            capital=1_000.0,
            entry_price=10_000.0,
            stop_loss=9_000.0,
            sizing_mode="fixed_fractional",
            risk_per_trade=0.01,
        )
        result = calculate_position_size(inp)
        assert result.quantity == 0
        assert result.skip_reason is not None
        assert len(result.skip_reason) > 0

    def test_skip_reason_none_when_qty_positive(self, ff_input):
        result = calculate_position_size(ff_input)
        assert result.skip_reason is None

    def test_short_side_sl_above_entry(self):
        """SELL: entry=100, SL=105 (above entry). Risk/share=5."""
        inp = SizingInput(
            capital=10_000.0,
            entry_price=100.0,
            stop_loss=105.0,
            sizing_mode="fixed_fractional",
            risk_per_trade=0.01,
            side="SELL",
        )
        result = calculate_position_size(inp)
        assert result.quantity == 20  # floor(100/5)

    def test_zero_sl_distance_returns_zero_qty(self):
        """Entry == SL => division by zero scenario => qty = 0."""
        inp = SizingInput(
            capital=10_000.0,
            entry_price=100.0,
            stop_loss=100.0,
            sizing_mode="fixed_fractional",
            risk_per_trade=0.01,
        )
        result = calculate_position_size(inp)
        assert result.quantity == 0
        assert result.skip_reason is not None

    def test_negative_capital_zero_qty(self):
        inp = SizingInput(
            capital=0.0,
            entry_price=100.0,
            stop_loss=95.0,
            sizing_mode="fixed_fractional",
        )
        result = calculate_position_size(inp)
        assert result.quantity == 0

    def test_reward_amount_with_target(self):
        """qty=70, target=830, entry=820 => reward=70*10=700."""
        inp = SizingInput(
            capital=35_000.0,
            entry_price=820.0,
            stop_loss=815.0,
            sizing_mode="fixed_fractional",
            risk_per_trade=0.01,
            target=830.0,
        )
        result = calculate_position_size(inp)
        assert math.isclose(result.reward_amount, 70 * 10.0, rel_tol=1e-6)

    def test_invalid_mode_raises_value_error(self):
        with pytest.raises(ValueError, match="sizing_mode"):
            inp = SizingInput(
                capital=10_000.0,
                entry_price=100.0,
                stop_loss=95.0,
                sizing_mode="unknown_mode",
            )
            calculate_position_size(inp)

    def test_raw_quantity_equals_quantity_no_filters(self, ff_input):
        result = calculate_position_size(ff_input)
        assert result.raw_quantity == result.quantity

    def test_min_entry_price_filter_skips_trade(self):
        """entry_price 50 < min_entry_price 100 => skip."""
        inp = SizingInput(
            capital=10_000.0,
            entry_price=50.0,
            stop_loss=45.0,
            sizing_mode="fixed_fractional",
            min_entry_price=100.0,
        )
        result = calculate_position_size(inp)
        assert result.quantity == 0
        assert result.skip_reason is not None

    def test_max_entry_price_filter_skips_trade(self):
        """entry_price 200 > max_entry_price 100 => skip."""
        inp = SizingInput(
            capital=10_000.0,
            entry_price=200.0,
            stop_loss=195.0,
            sizing_mode="fixed_fractional",
            max_entry_price=100.0,
        )
        result = calculate_position_size(inp)
        assert result.quantity == 0
        assert result.skip_reason is not None

    def test_price_filters_zero_means_disabled(self):
        """min/max_entry_price=0 means disabled — no skip."""
        inp = SizingInput(
            capital=10_000.0,
            entry_price=50.0,
            stop_loss=45.0,
            sizing_mode="fixed_fractional",
            risk_per_trade=0.01,
            min_entry_price=0.0,
            max_entry_price=0.0,
        )
        result = calculate_position_size(inp)
        assert result.quantity > 0

    def test_large_capital_precision(self):
        """10_000_000 capital, risk 1%, SL distance 5 => 20000 shares."""
        inp = SizingInput(
            capital=10_000_000.0,
            entry_price=100.0,
            stop_loss=95.0,
            sizing_mode="fixed_fractional",
            risk_per_trade=0.01,
        )
        result = calculate_position_size(inp)
        assert result.quantity == 20_000

    def test_fractional_floor(self):
        """risk_amount=100, risk/share=3 => floor(33.33)=33."""
        inp = SizingInput(
            capital=10_000.0,
            entry_price=100.0,
            stop_loss=97.0,
            sizing_mode="fixed_fractional",
            risk_per_trade=0.01,
        )
        result = calculate_position_size(inp)
        assert result.quantity == 33


# ---------------------------------------------------------------------------
# pct_of_capital mode
# ---------------------------------------------------------------------------

class TestPctOfCapital:
    def test_basic_quantity(self, pct_input):
        """50 000 * 0.10 = 5 000; qty = floor(5000 / 500) = 10."""
        result = calculate_position_size(pct_input)
        assert result.quantity == 10

    def test_position_value(self, pct_input):
        result = calculate_position_size(pct_input)
        assert math.isclose(result.position_value, 10 * 500.0, rel_tol=1e-6)

    def test_zero_entry_returns_zero_qty(self):
        inp = SizingInput(
            capital=10_000.0,
            entry_price=0.0,
            stop_loss=0.0,
            sizing_mode="pct_of_capital",
            pct_of_capital=0.10,
        )
        result = calculate_position_size(inp)
        assert result.quantity == 0
        assert result.skip_reason is not None

    def test_reward_amount_with_target(self, pct_input):
        """qty=10, target=520, entry=500 => reward=10*20=200."""
        result = calculate_position_size(pct_input)
        assert math.isclose(result.reward_amount, 10 * 20.0, rel_tol=1e-6)

    def test_sl_distance_pct(self, pct_input):
        result = calculate_position_size(pct_input)
        expected = abs(500.0 - 490.0) / 500.0
        assert math.isclose(result.sl_distance_pct, expected, rel_tol=1e-6)

    def test_risk_amount_approximation(self, pct_input):
        """risk_amount = qty * sl_distance = 10 * 10 = 100."""
        result = calculate_position_size(pct_input)
        assert math.isclose(result.risk_amount, 10 * 10.0, rel_tol=1e-6)

    def test_fractional_floor(self):
        """50 000 * 0.10 / 300 = floor(16.67) = 16."""
        inp = SizingInput(
            capital=50_000.0,
            entry_price=300.0,
            stop_loss=290.0,
            sizing_mode="pct_of_capital",
            pct_of_capital=0.10,
        )
        result = calculate_position_size(inp)
        assert result.quantity == 16


# ---------------------------------------------------------------------------
# validate_sizing_input
# ---------------------------------------------------------------------------

class TestValidateSizingInput:
    def _valid_payload(self):
        return {
            "capital": 35_000.0,
            "entry_price": 820.0,
            "stop_loss": 815.0,
            "sizing_mode": "fixed_fractional",
            "risk_per_trade": 0.01,
            "pct_of_capital": None,
            "slippage_factor": 0.0,
            "max_sl_pct_for_sizing": 0.0,
            "min_entry_price": 0.0,
            "max_entry_price": 0.0,
            "target": 830.0,
            "side": "BUY",
        }

    def test_valid_payload_returns_sizing_input(self):
        ok, inp, err = validate_sizing_input(self._valid_payload())
        assert ok is True
        assert isinstance(inp, SizingInput)
        assert err is None

    def test_missing_entry_price_returns_error(self):
        data = self._valid_payload()
        del data["entry_price"]
        ok, inp, err = validate_sizing_input(data)
        assert ok is False
        assert err is not None

    def test_missing_stop_loss_returns_error(self):
        data = self._valid_payload()
        del data["stop_loss"]
        ok, inp, err = validate_sizing_input(data)
        assert ok is False
        assert err is not None

    def test_missing_sizing_mode_returns_error(self):
        data = self._valid_payload()
        del data["sizing_mode"]
        ok, inp, err = validate_sizing_input(data)
        assert ok is False
        assert err is not None

    def test_negative_entry_price_returns_error(self):
        data = self._valid_payload()
        data["entry_price"] = -10.0
        ok, inp, err = validate_sizing_input(data)
        assert ok is False
        assert err is not None

    def test_negative_capital_returns_error(self):
        data = self._valid_payload()
        data["capital"] = -1.0
        ok, inp, err = validate_sizing_input(data)
        assert ok is False
        assert err is not None

    def test_invalid_sizing_mode_returns_error(self):
        data = self._valid_payload()
        data["sizing_mode"] = "bad_mode"
        ok, inp, err = validate_sizing_input(data)
        assert ok is False
        assert err is not None

    def test_pct_of_capital_mode_without_pct_uses_zero(self):
        data = self._valid_payload()
        data["sizing_mode"] = "pct_of_capital"
        data["pct_of_capital"] = None
        ok, inp, err = validate_sizing_input(data)
        # None pct_of_capital should be treated as 0.0 (allowed, will produce qty=0)
        assert ok is True

    def test_capital_defaults_to_none_when_absent(self):
        """capital is optional — if absent, validate_sizing_input should still succeed
        with capital=0 or None placeholder (live fetch done at API layer)."""
        data = self._valid_payload()
        del data["capital"]
        ok, inp, err = validate_sizing_input(data)
        assert ok is True

    def test_target_optional(self):
        data = self._valid_payload()
        del data["target"]
        ok, inp, err = validate_sizing_input(data)
        assert ok is True

    def test_side_optional_defaults_to_buy(self):
        data = self._valid_payload()
        del data["side"]
        ok, inp, err = validate_sizing_input(data)
        assert ok is True
        assert inp.side == "BUY"

    def test_none_values_are_coerced_to_defaults(self):
        data = self._valid_payload()
        data["slippage_factor"] = None
        data["max_sl_pct_for_sizing"] = None
        ok, inp, err = validate_sizing_input(data)
        assert ok is True
        assert inp.slippage_factor == 0.0
        assert inp.max_sl_pct_for_sizing == 0.0
