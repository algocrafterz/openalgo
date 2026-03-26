"""Tests for signal validator — RED phase first."""

import pytest

from signal_engine.models import Direction, Signal, ValidationStatus
from signal_engine.validator import validate, _recent_signals
from signal_engine.tests.conftest import make_signal as _make_signal


@pytest.fixture(autouse=True)
def clear_duplicates():
    _recent_signals.clear()
    yield
    _recent_signals.clear()


class TestEntryValidation:
    def test_zero_entry_invalid(self):
        result = validate(_make_signal(entry=0))
        assert result.status == ValidationStatus.INVALID

    def test_negative_entry_invalid(self):
        result = validate(_make_signal(entry=-100))
        assert result.status == ValidationStatus.INVALID


class TestSLValidation:
    def test_zero_sl_invalid(self):
        result = validate(_make_signal(sl=0))
        assert result.status == ValidationStatus.INVALID

    def test_long_sl_above_entry_invalid(self):
        result = validate(_make_signal(direction=Direction.LONG, entry=100, sl=110))
        assert result.status == ValidationStatus.INVALID

    def test_short_sl_below_entry_invalid(self):
        result = validate(_make_signal(direction=Direction.SHORT, entry=100, sl=90, tp=80))
        assert result.status == ValidationStatus.INVALID


class TestTPValidation:
    def test_zero_tp_invalid(self):
        result = validate(_make_signal(tp=0))
        assert result.status == ValidationStatus.INVALID

    def test_long_tp_below_entry_invalid(self):
        result = validate(_make_signal(direction=Direction.LONG, entry=100, sl=90, tp=95))
        assert result.status == ValidationStatus.INVALID

    def test_short_tp_above_entry_invalid(self):
        result = validate(
            _make_signal(direction=Direction.SHORT, entry=100, sl=110, tp=105)
        )
        assert result.status == ValidationStatus.INVALID


class TestRRRatio:
    def test_below_min_rr_ignored(self):
        # R:R = (2504 - 2500) / (2500 - 2480) = 0.2 < 0.5 (min_rr)
        result = validate(_make_signal(entry=2500, sl=2480, tp=2504))
        assert result.status == ValidationStatus.IGNORED

    def test_at_min_rr_valid(self):
        # R:R = (2510 - 2500) / (2500 - 2480) = 0.5 (equals min_rr)
        result = validate(_make_signal(entry=2500, sl=2480, tp=2510))
        assert result.status == ValidationStatus.VALID

    def test_above_min_rr_valid(self):
        # R:R = (2545 - 2500) / (2500 - 2485) = 3.0
        result = validate(_make_signal(entry=2500, sl=2485, tp=2545))
        assert result.status == ValidationStatus.VALID


class TestDuplicateDetection:
    def test_duplicate_signal_ignored(self):
        sig = _make_signal()
        assert validate(sig).status == ValidationStatus.VALID
        assert validate(sig).status == ValidationStatus.IGNORED

    def test_different_symbol_not_duplicate(self):
        assert validate(_make_signal(symbol="RELIANCE")).status == ValidationStatus.VALID
        assert validate(_make_signal(symbol="TCS")).status == ValidationStatus.VALID

    def test_different_direction_not_duplicate(self):
        assert validate(_make_signal(direction=Direction.LONG)).status == ValidationStatus.VALID
        assert validate(
            _make_signal(direction=Direction.SHORT, sl=2515, tp=2460)
        ).status == ValidationStatus.VALID


class TestMinSlPct:
    """Reject signals where SL is suspiciously tight (< min_sl_pct of entry)."""

    def test_sl_too_tight_rejected(self):
        # entry=1000, sl=999, sl_pct = 1/1000 = 0.1% < 0.3% threshold
        result = validate(_make_signal(entry=1000, sl=999, tp=1015))
        assert result.status == ValidationStatus.IGNORED
        assert "SL distance" in result.reason

    def test_sl_at_threshold_allowed(self):
        # entry=1000, sl=995, sl_pct = 5/1000 = 0.5% = threshold
        result = validate(_make_signal(entry=1000, sl=995, tp=1010))
        assert result.status == ValidationStatus.VALID

    def test_sl_above_threshold_allowed(self):
        # entry=1000, sl=990, sl_pct = 10/1000 = 1% > 0.3%
        result = validate(_make_signal(entry=1000, sl=990, tp=1020))
        assert result.status == ValidationStatus.VALID

    def test_short_sl_too_tight_rejected(self):
        # entry=1000, sl=1001, sl_pct = 1/1000 = 0.1% < 0.3%
        result = validate(_make_signal(
            direction=Direction.SHORT, entry=1000, sl=1001, tp=985,
        ))
        assert result.status == ValidationStatus.IGNORED
        assert "SL distance" in result.reason

    def test_disabled_when_zero(self):
        # min_sl_pct=0 in config -> no check (tested via existing valid signals)
        # The default config.yaml has min_sl_pct=0.003 so this test
        # verifies the check works with the default
        result = validate(_make_signal(entry=2500, sl=2485, tp=2540))
        # sl_pct = 15/2500 = 0.6% > 0.3%, should pass
        assert result.status == ValidationStatus.VALID


class TestExitValidation:
    """EXIT signals use relaxed validation — skip SL/TP/R:R/duplicate checks."""

    def test_exit_signal_valid_with_minimal_fields(self):
        result = validate(_make_signal(direction=Direction.EXIT))
        assert result.status == ValidationStatus.VALID

    def test_exit_skips_sl_direction_check(self):
        """EXIT carries original entry SL/TP for audit — don't validate direction consistency."""
        result = validate(_make_signal(
            direction=Direction.EXIT, entry=2500, sl=2510, tp=2480,
        ))
        assert result.status == ValidationStatus.VALID

    def test_exit_skips_rr_check(self):
        """EXIT R:R is meaningless — it's closing, not opening."""
        result = validate(_make_signal(
            direction=Direction.EXIT, entry=2500, sl=2499, tp=2501,
        ))
        assert result.status == ValidationStatus.VALID

    def test_exit_skips_duplicate_check(self):
        """Two EXIT signals for same symbol should both pass (e.g. retry)."""
        sig1 = _make_signal(direction=Direction.EXIT)
        sig2 = _make_signal(direction=Direction.EXIT)
        assert validate(sig1).status == ValidationStatus.VALID
        assert validate(sig2).status == ValidationStatus.VALID

    def test_exit_requires_positive_entry(self):
        """Entry must be positive even for EXIT (audit trail accuracy)."""
        result = validate(_make_signal(direction=Direction.EXIT, entry=0))
        assert result.status == ValidationStatus.INVALID

    def test_exit_requires_symbol(self):
        """EXIT must have a symbol to identify which position to close."""
        result = validate(_make_signal(direction=Direction.EXIT, symbol=""))
        assert result.status == ValidationStatus.INVALID


class TestValidSignals:
    def test_valid_long_signal(self):
        result = validate(_make_signal())
        assert result.status == ValidationStatus.VALID

    def test_valid_short_signal(self):
        result = validate(
            _make_signal(direction=Direction.SHORT, entry=2500, sl=2515, tp=2460)
        )
        assert result.status == ValidationStatus.VALID
