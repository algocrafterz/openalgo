"""T1/T2: make the two things that silently move risk per trade visible in the log.

T1  adjust_qty_for_margin() scales qty down to fit live capital, so whenever slot 2 is taken
    the actual rupee risk is BELOW risk_per_trade. R-multiples stay comparable (they are
    per-share) but the rupee P&L and every loss-limit counter stop corresponding to "N full
    stops" — which is exactly the mental model those limits are written in.
T2  Under fixed_fractional the notional is risk_per_trade / (sl_pct x (1+slippage)) and the
    entry price cancels out. At the 0.20% min_sl_pct BREAKOUT/EMA9/BREAKINGTRADE use, that is
    4.55x — and at 20% MIS margin one such position needs ~91% of a Rs 35k account. The
    number is derivable but nobody derives it mid-session, so it goes in the line.
"""

import pytest
from loguru import logger

from signal_engine import main
from signal_engine.tests.conftest import make_signal as _make_signal


@pytest.fixture
def captured():
    """loguru bypasses stdlib logging, so caplog sees nothing - add a real sink."""
    lines = []
    sink_id = logger.add(lines.append, level="INFO", format="{message}")
    yield lines
    logger.remove(sink_id)


def _log(captured, **kwargs):
    signal = kwargs.pop("signal", None) or _make_signal(entry=800.0, sl=796.0, tp=810.0)
    main._log_entry_sizing(signal, **kwargs)
    return " ".join(captured)


class TestMarginScalingIsAttributed:
    def test_a_scaled_order_says_so_and_gives_the_real_risk(self, captured):
        text = _log(captured, quantity=60, sizing_capital=100_000, risk_based_quantity=100)
        assert "margin-scaled" in text
        assert "100 -> 60" in text

    def test_an_unscaled_order_adds_no_note(self, captured):
        text = _log(captured, quantity=100, sizing_capital=100_000, risk_based_quantity=100)
        assert "margin-scaled" not in text

    def test_the_reported_risk_percent_follows_the_final_quantity(self, captured):
        """1% intended, 60% of the shares -> 0.24% actually risked on a Rs 4 stop."""
        text = _log(captured, quantity=60, sizing_capital=100_000, risk_based_quantity=100)
        assert "total_risk=240" in text


class TestLeverageIsReported:
    def test_the_implied_leverage_is_in_the_line(self, captured):
        # 100 shares x Rs 800 = Rs 80,000 notional on Rs 100,000 -> 0.8x
        text = _log(captured, quantity=100, sizing_capital=100_000, risk_based_quantity=100)
        assert "leverage=0.8x" in text

    def test_leverage_beyond_the_mis_allowance_is_warned_about(self, captured):
        """At mis_margin_pct 0.20 the broker funds up to 5x. Past that the order cannot be
        funded and the margin API will cut it — worth saying before it happens."""
        signal = _make_signal(entry=800.0, sl=799.0, tp=810.0)  # very tight stop
        text = _log(captured, signal=signal, quantity=900, sizing_capital=100_000,
                    risk_based_quantity=900)
        assert "exceeds" in text.lower()

    def test_leverage_inside_the_allowance_is_not_warned_about(self, captured):
        text = _log(captured, quantity=100, sizing_capital=100_000, risk_based_quantity=100)
        assert "exceeds" not in text.lower()


class TestReturnValueUnchanged:
    def test_still_returns_the_reward_to_risk_ratio(self):
        signal = _make_signal(entry=800.0, sl=796.0, tp=810.0)  # 4 risk, 10 reward
        rr = main._log_entry_sizing(signal, quantity=100, sizing_capital=100_000,
                                    risk_based_quantity=100)
        assert rr == pytest.approx(2.5)

    def test_risk_based_quantity_defaults_to_the_final_quantity(self):
        """Callers that never scaled (smoke_test, tests) need not pass it."""
        signal = _make_signal(entry=800.0, sl=796.0, tp=810.0)
        assert main._log_entry_sizing(signal, quantity=100,
                                      sizing_capital=100_000) == pytest.approx(2.5)
