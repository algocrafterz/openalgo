"""BREAKOUT-PY stop-distance floor at exactly 0.30%: float noise must not reject the signal.

The strategy places the stop at exactly close x 0.003. The validator computes
abs(entry - sl) / entry, which for many prices lands a few ULPs below 0.003 (for example
0.0029999999999999615). The strict '<' then rejected the signal as "0.3000% below minimum
0.3000%" (M&M 11:05, SHRIRAMFIN 11:20, SUNPHARMA 11:35 on 2026-10-14).

The tolerance (1e-9 on the ratio) only absorbs that noise. A stop that is genuinely tighter
than the floor is still rejected: 1e-9 of ratio is 0.0001% of the entry price.
"""

import dataclasses

import pytest

from signal_engine.config import settings
from signal_engine.models import Direction, ValidationStatus
from signal_engine.tests.conftest import make_signal as _make_signal
from signal_engine.validator import _SL_FLOOR_EPS, _recent_signals, validate

FLOOR = 0.003


@pytest.fixture(autouse=True)
def _floor_and_clean_dedup(monkeypatch):
    monkeypatch.setattr(
        "signal_engine.validator.settings",
        dataclasses.replace(settings, min_sl_pct=FLOOR, strategy_profiles={}),
    )
    _recent_signals.clear()
    yield
    _recent_signals.clear()


def _long(entry, sl):
    return _make_signal(direction=Direction.LONG, entry=entry, sl=sl, tp=entry * 1.02)


def _short(entry, sl):
    return _make_signal(direction=Direction.SHORT, entry=entry, sl=sl, tp=entry * 0.98)


def _noisy_long_prices(limit=40):
    """Entries whose exact 0.30% stop computes to a ratio strictly below the floor."""
    out = []
    price = 100.0
    while len(out) < limit and price < 6000:
        sl = price - price * FLOOR
        if abs(price - sl) / price < FLOOR:
            out.append((price, sl))
        price += 0.35
    return out


class TestFloatNoiseAtTheBoundary:
    def test_precondition_the_unfixed_comparison_would_reject_these(self):
        noisy = _noisy_long_prices()
        assert len(noisy) >= 20
        assert all(abs(p - sl) / p < FLOOR for p, sl in noisy)

    def test_exact_boundary_stops_pass_for_longs(self):
        for entry, sl in _noisy_long_prices():
            _recent_signals.clear()
            result = validate(_long(entry, sl))
            assert result.status == ValidationStatus.VALID, (entry, sl, result.reason)

    def test_exact_boundary_stops_pass_for_shorts(self):
        for entry, _ in _noisy_long_prices():
            _recent_signals.clear()
            sl = entry + entry * FLOOR
            result = validate(_short(entry, sl))
            assert result.status == ValidationStatus.VALID, (entry, sl, result.reason)


class TestGenuinelyTightStopsStillRejected:
    @pytest.mark.parametrize("pct", [0.299, 0.2999, 0.29999, 0.1, 0.01])
    def test_below_floor_is_rejected(self, pct):
        entry = 1000.0
        result = validate(_long(entry, entry - entry * pct / 100))
        assert result.status == ValidationStatus.IGNORED
        assert "SL distance" in result.reason

    def test_just_beyond_the_tolerance_is_rejected(self):
        entry = 1000.0
        ratio = FLOOR - 5 * _SL_FLOOR_EPS
        assert validate(_long(entry, entry - entry * ratio)).status == ValidationStatus.IGNORED

    def test_just_inside_the_tolerance_is_admitted(self):
        entry = 1000.0
        ratio = FLOOR - 0.5 * _SL_FLOOR_EPS
        assert validate(_long(entry, entry - entry * ratio)).status == ValidationStatus.VALID

    def test_just_above_the_floor_passes(self):
        entry = 1000.0
        ratio = FLOOR + _SL_FLOOR_EPS
        assert validate(_long(entry, entry - entry * ratio)).status == ValidationStatus.VALID

    def test_short_below_floor_is_rejected(self):
        entry = 1000.0
        assert validate(_short(entry, entry + 2.99)).status == ValidationStatus.IGNORED


class TestToleranceIsOnlyNoise:
    def test_tolerance_is_one_billionth_of_the_price(self):
        assert _SL_FLOOR_EPS == 1e-9

    def test_largest_admitted_shortfall_is_a_fraction_of_a_paisa(self):
        # Highest NSE price in the universe is ~Rs 50,000; the tolerance there is 5e-5 rupees.
        assert 50_000 * _SL_FLOOR_EPS < 0.0001


class TestZeroFloorUnchanged:
    def test_zero_floor_never_checks_the_distance(self, monkeypatch):
        monkeypatch.setattr(
            "signal_engine.validator.settings",
            dataclasses.replace(settings, min_sl_pct=0.0, strategy_profiles={}),
        )
        entry = 1000.0
        assert validate(_long(entry, entry - 0.01)).status == ValidationStatus.VALID

    def test_strategy_override_of_zero_disables_the_check(self, monkeypatch):
        monkeypatch.setattr(
            "signal_engine.validator.settings",
            dataclasses.replace(
                settings, min_sl_pct=0.005, strategy_profiles={"BREAKOUT-PY": {"min_sl_pct": 0.0}}
            ),
        )
        sig = _make_signal(strategy="BREAKOUT-PY", entry=1000.0, sl=999.99, tp=1020.0)
        assert validate(sig).status == ValidationStatus.VALID
