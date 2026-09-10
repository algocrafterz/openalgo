"""H1: the loss limits must stay armed across a restart.

check_exposure() gated every loss check on `last_known_capital`, which is stamped only
inside calculate_quantity() and never persisted. On a fresh process it is 0.0, and
main._handle_entry runs the risk gates BEFORE it resolves capital — so the first signal
after a restart skipped all three loss limits entirely, even with the day's realised loss
correctly restored from risk.db. day_start_capital IS persisted, so it is the fallback.
"""

from signal_engine.tests.risk_fixtures import _engine

STRATEGY = "ORB"


class TestDailyLimitSurvivesRestart:
    def test_day_start_capital_arms_the_daily_limit(self):
        engine = _engine()
        state = engine._state(STRATEGY)
        state.last_known_capital = 0.0  # fresh process, nothing sized yet
        state.day_start_capital = 100_000  # restored from risk.db
        state.daily_realised_loss = 3100  # > 3% of 100k
        assert engine.check_exposure(STRATEGY) is False

    def test_block_reason_names_the_daily_limit(self):
        engine = _engine()
        state = engine._state(STRATEGY)
        state.last_known_capital = 0.0
        state.day_start_capital = 100_000
        state.daily_realised_loss = 3100
        assert "Daily loss limit" in engine.exposure_block_reason(STRATEGY)

    def test_live_capital_still_wins_when_present(self):
        """The fallback must not override a fresher in-session figure."""
        engine = _engine()
        state = engine._state(STRATEGY)
        state.last_known_capital = 200_000
        state.day_start_capital = 100_000
        state.daily_realised_loss = 3100  # 3.1% of 100k, only 1.55% of 200k
        assert engine.check_exposure(STRATEGY) is True

    def test_both_zero_still_skips_the_check(self):
        """With no capital figure at all there is nothing to compare against — the slot
        and trade-count gates below still apply, but the loss gates cannot."""
        engine = _engine()
        state = engine._state(STRATEGY)
        state.last_known_capital = 0.0
        state.day_start_capital = 0.0
        state.daily_realised_loss = 999_999
        assert engine.check_exposure(STRATEGY) is True


class TestWeeklyMonthlySurviveRestart:
    def test_weekly_limit_armed_by_day_start_capital(self):
        engine = _engine()
        state = engine._state(STRATEGY)
        state.day_start_capital = 100_000
        state.weekly_net_pnl = -6100  # > 6% of 100k
        assert engine.check_exposure(STRATEGY) is False

    def test_monthly_limit_armed_by_day_start_capital(self):
        engine = _engine()
        state = engine._state(STRATEGY)
        state.day_start_capital = 100_000
        state.monthly_net_pnl = -10100  # > 10% of 100k
        assert engine.check_exposure(STRATEGY) is False
