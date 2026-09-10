"""H2: weekly and monthly limits measure NET drawdown, not gross losses.

record_close() only ever accumulated abs(pnl) on the losing branch, and the weekly/monthly
limits summed that same gross column. At Rs 350 risk/trade on Rs 35k, weekly 8% = 8 losing
trades and monthly 15% = 15 — reachable in two and four sessions at 10 trades/day, on a
NET-PROFITABLE account. Weekly and monthly now net wins against losses.

The DAILY limit deliberately stays gross: "4% daily = four full stops" is how config.yaml
derives it, and a four-stop-out day is a loss-streak signal worth halting on regardless of
what the winners did.
"""

from signal_engine.tests.risk_fixtures import _engine

STRATEGY = "ORB"


def _armed(engine, capital=100_000):
    state = engine._state(STRATEGY)
    state.last_known_capital = capital
    return state


def _weekly_engine(**kw):
    """Daily gate off so the weekly/monthly behaviour is what is actually under test —
    the daily limit is gross and would fire first on any multi-stop-out sequence."""
    return _engine(daily_loss_limit=1.0, **kw)


class TestWeeklyLimitNetsWins:
    def test_wins_offset_losses_within_the_week(self):
        engine = _weekly_engine()  # weekly_loss_limit 0.06 -> Rs 6,000 of 100k
        _armed(engine)
        for _ in range(8):
            engine.record_close(-1000, strategy=STRATEGY)  # -8,000 gross
        engine.record_close(5000, strategy=STRATEGY)  # net -3,000
        assert engine.check_exposure(STRATEGY) is True

    def test_net_loss_past_the_limit_still_blocks(self):
        engine = _weekly_engine()
        _armed(engine)
        for _ in range(8):
            engine.record_close(-1000, strategy=STRATEGY)
        engine.record_close(1000, strategy=STRATEGY)  # net -7,000 > 6,000
        assert engine.check_exposure(STRATEGY) is False
        assert "Weekly net drawdown" in engine.exposure_block_reason(STRATEGY)

    def test_a_profitable_week_never_blocks(self):
        engine = _weekly_engine()
        _armed(engine)
        for _ in range(20):
            engine.record_close(-1000, strategy=STRATEGY)
        engine.record_close(25_000, strategy=STRATEGY)
        assert engine.check_exposure(STRATEGY) is True


class TestMonthlyLimitNetsWins:
    def test_wins_offset_losses_within_the_month(self):
        engine = _weekly_engine(weekly_loss_limit=1.0)  # monthly 0.10 -> Rs 10,000 of 100k
        _armed(engine)
        for _ in range(15):
            engine.record_close(-1000, strategy=STRATEGY)  # -15,000 gross
        engine.record_close(9000, strategy=STRATEGY)  # net -6,000
        assert engine.check_exposure(STRATEGY) is True

    def test_net_loss_past_the_limit_still_blocks(self):
        engine = _weekly_engine(weekly_loss_limit=1.0)
        _armed(engine)
        for _ in range(15):
            engine.record_close(-1000, strategy=STRATEGY)
        assert engine.check_exposure(STRATEGY) is False
        assert "Monthly net drawdown" in engine.exposure_block_reason(STRATEGY)


class TestDailyLimitStaysGross:
    def test_a_winning_day_still_halts_after_the_stop_out_budget(self):
        """Deliberate: four full stops halts the day even if the winners more than cover
        them. See config.yaml's daily_loss_limit note."""
        engine = _engine()  # daily_loss_limit 0.03 -> Rs 3,000 of 100k
        _armed(engine)
        engine.record_close(50_000, strategy=STRATEGY)
        for _ in range(4):
            engine.record_close(-1000, strategy=STRATEGY)  # 4,000 gross
        assert engine.check_exposure(STRATEGY) is False
        assert "Daily loss limit" in engine.exposure_block_reason(STRATEGY)


class TestNetCountersTrackBothSigns:
    def test_net_pnl_accumulates_wins_and_losses(self):
        engine = _engine()
        engine.record_close(-500, strategy=STRATEGY)
        engine.record_close(1200, strategy=STRATEGY)
        state = engine._state(STRATEGY)
        assert state.daily_net_pnl == 700
        assert state.weekly_net_pnl == 700
        assert state.monthly_net_pnl == 700
        # gross loss counter is unchanged by the win
        assert state.daily_realised_loss == 500
