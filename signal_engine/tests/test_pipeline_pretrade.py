"""Shared pre-trade gates reuse the validator's stateless checks."""

from unittest.mock import patch

import pandas as pd
import pytest

from signal_engine.models import Direction, Signal, ValidationStatus
from signal_engine.pipeline import pretrade

IST = "Asia/Kolkata"


def sig(direction=Direction.LONG, entry=100.0, sl=99.0, tp=101.5, strategy="BREAKOUT"):
    return Signal(strategy=strategy, direction=direction, symbol="TCS", entry=entry, sl=sl, tp=tp)


@pytest.fixture
def cfg():
    with patch("signal_engine.validator.settings") as s:
        s.blacklist = {}
        s.min_rr = 1.0
        s.min_sl_pct = 0.002
        s.strategy_profiles = {}
        yield s


class TestCheck:
    def test_valid_signal_passes(self, cfg):
        assert pretrade.check(sig()) is None

    def test_blacklisted_symbol_is_ignored(self, cfg):
        cfg.blacklist = {"_GLOBAL": frozenset({"TCS"})}
        r = pretrade.check(sig())
        assert r.status == ValidationStatus.IGNORED and "blacklisted" in r.reason

    def test_low_reward_risk_is_ignored(self, cfg):
        r = pretrade.check(sig(tp=100.5))
        assert r.status == ValidationStatus.IGNORED and "R:R" in r.reason

    def test_tight_stop_is_ignored(self, cfg):
        r = pretrade.check(sig(sl=99.95, tp=100.5))
        assert r.status == ValidationStatus.IGNORED and "SL distance" in r.reason

    def test_wrong_side_stop_is_invalid(self, cfg):
        r = pretrade.check(sig(sl=101.0))
        assert r.status == ValidationStatus.INVALID

    def test_non_positive_price_is_invalid(self, cfg):
        r = pretrade.check(sig(sl=0.0))
        assert r.status == ValidationStatus.INVALID

    def test_duplicate_check_is_not_applied(self, cfg):
        assert pretrade.check(sig()) is None
        assert pretrade.check(sig()) is None


class TestWindow:
    def test_inside_half_open(self):
        t = pd.Timestamp("2026-09-30 09:45", tz=IST)
        assert pretrade.in_entry_window(t, (9 * 60 + 45, 11 * 60 + 45))

    def test_cutoff_is_exclusive(self):
        t = pd.Timestamp("2026-09-30 11:45", tz=IST)
        assert not pretrade.in_entry_window(t, (9 * 60 + 45, 11 * 60 + 45))

    def test_before_start(self):
        t = pd.Timestamp("2026-09-30 09:40", tz=IST)
        assert not pretrade.in_entry_window(t, (9 * 60 + 45, 11 * 60 + 45))


class TestStale:
    def test_fresh(self):
        c = pd.Timestamp("2026-09-30 10:05", tz=IST)
        assert not pretrade.is_stale(c, c + pd.Timedelta(seconds=30), 60)

    def test_stale(self):
        c = pd.Timestamp("2026-09-30 10:05", tz=IST)
        assert pretrade.is_stale(c, c + pd.Timedelta(seconds=61), 60)


class TestEligible:
    def test_in_band(self):
        assert pretrade.ineligible_reason(500.0, 50.0, 1500.0) is None

    def test_below_band(self):
        assert "below" in pretrade.ineligible_reason(40.0, 50.0, 1500.0)

    def test_above_band(self):
        assert "above" in pretrade.ineligible_reason(2000.0, 50.0, 1500.0)

    def test_zero_bound_means_no_limit(self):
        assert pretrade.ineligible_reason(99999.0, 0.0, 0.0) is None

    def test_non_positive_price(self):
        assert pretrade.ineligible_reason(0.0, 0.0, 0.0) is not None


class TestPriceBand:
    def test_profile_override_wins_else_global(self):
        with patch("signal_engine.pipeline.pretrade.settings") as s:
            s.min_entry_price, s.max_entry_price = 50.0, 1500.0
            s.strategy_profiles = {"BREAKOUT": {"max_entry_price": 900.0}}
            assert pretrade.price_band("BREAKOUT") == (50.0, 900.0)
            assert pretrade.price_band("ORB") == (50.0, 1500.0)
