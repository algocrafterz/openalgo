"""Dynamic max_open_positions: the -1 sentinel that computes the position-count ceiling
fresh from live capital instead of a fixed number in config.yaml (risk.py's
_dynamic_max_open_positions / _effective_max_open_positions).
"""

from signal_engine.tests.risk_fixtures import _engine

STRATEGY = "ORB"


def _with_capital(engine, capital: float, strategy: str = STRATEGY):
    engine._state(strategy).last_known_capital = capital
    return engine


class TestStaticValuesUnchanged:
    """max_open_positions values other than -1 must behave exactly as before."""

    def test_positive_value_is_used_as_is(self):
        engine = _engine(max_open_positions=3)
        _with_capital(engine, 1_000_000)
        assert engine.effective_max_open_positions_for(STRATEGY) == 3

    def test_zero_still_means_unlimited(self):
        engine = _engine(
            max_open_positions=0,
            mis_margin_pct=0.20,
            margin_reserve_buffer=5000,
            worst_case_sl_pct=0.002,
        )
        _with_capital(engine, 25_000)
        assert engine.effective_max_open_positions_for(STRATEGY) == 0
        engine._state(STRATEGY).open_positions = 50
        assert engine.check_exposure(STRATEGY) is True


class TestDynamicSentinel:
    """max_open_positions=-1 computes the ceiling from live capital."""

    def _dynamic_engine(self, **overrides):
        defaults = {
            "max_open_positions": -1,
            "mis_margin_pct": 0.20,
            "margin_reserve_buffer": 5000,
            "worst_case_sl_pct": 0.005,  # 0.50% — ORB's global floor
            "slippage_factor": 0.10,
        }
        defaults.update(overrides)
        return _engine(**defaults)

    def test_unknown_capital_falls_back_to_one_not_unlimited(self):
        engine = self._dynamic_engine()
        # No capital ever recorded for this strategy yet (fresh process, first signal).
        assert engine.effective_max_open_positions_for(STRATEGY) == 1

    def test_scales_with_live_capital(self):
        engine = self._dynamic_engine()
        # margin_ratio = 0.01*0.20/(0.005*1.10) = 0.3636 -> per-position margin = 36.36% of capital.
        _with_capital(engine, 25_000)
        assert engine.effective_max_open_positions_for(STRATEGY) == 2  # (25000-5000)/9091 = 2.2
        _with_capital(engine, 10_000)
        assert engine.effective_max_open_positions_for(STRATEGY) == 1  # (10000-5000)/3636 = 1.375
        _with_capital(engine, 100_000)
        assert engine.effective_max_open_positions_for(STRATEGY) == 2  # (95000)/36364 = 2.61

    def test_tight_stop_strategy_stays_at_one_regardless_of_capital(self):
        """A margin ratio >= 50% per position (e.g. BREAKOUT's 0.20% floor) can never fit
        two concurrent positions, at ANY capital — this is the correct, safe answer, not a
        gap in the formula: risk-based sizing means a bigger account takes a proportionally
        bigger position too, so the % of capital tied up per trade never shrinks.
        """
        engine = self._dynamic_engine(worst_case_sl_pct=0.002)  # margin_ratio = 0.909
        for capital in (25_000, 100_000, 1_000_000, 10_000_000):
            _with_capital(engine, capital)
            assert engine.effective_max_open_positions_for(STRATEGY) == 1

    def test_never_below_one_even_with_reserve_buffer_exceeding_capital(self):
        engine = self._dynamic_engine(margin_reserve_buffer=50_000)
        _with_capital(engine, 25_000)
        assert engine.effective_max_open_positions_for(STRATEGY) == 1

    def test_check_exposure_blocks_at_the_dynamic_ceiling(self):
        engine = self._dynamic_engine()
        _with_capital(engine, 25_000)
        assert engine.effective_max_open_positions_for(STRATEGY) == 2
        engine._state(STRATEGY).open_positions = 2
        assert engine.check_exposure(STRATEGY) is False
        assert engine.exposure_block_reason(STRATEGY) == "Max positions (2/2)"

    def test_check_exposure_allows_below_the_dynamic_ceiling(self):
        engine = self._dynamic_engine()
        _with_capital(engine, 25_000)
        engine._state(STRATEGY).open_positions = 1
        assert engine.check_exposure(STRATEGY) is True

    def test_capacity_status_shows_resolved_number_not_sentinel(self):
        engine = self._dynamic_engine()
        _with_capital(engine, 25_000)
        assert engine.capacity_status(STRATEGY) == "0/2 positions open"
