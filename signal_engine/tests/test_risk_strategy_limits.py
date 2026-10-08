"""Per-strategy limit overrides (BREAKOUT spec v1.0 item 16: paper uses the live numbers per strategy).

The ANALYZE profile is loose on purpose, so every strategy takes every signal. BREAKOUT-PY asks for the
LIVE numbers on its OWN book. Other strategies keep the profile, and a pooled live bucket ignores
overrides.
"""

from types import SimpleNamespace

import pytest

from signal_engine import runtime
from signal_engine.config import RunnerStrategy
from signal_engine.risk import RiskEngine
from signal_engine.tests.risk_fixtures import _engine

PY = "BREAKOUT-PY"
OTHER = "ORB"
LIVE_NUMBERS = {
    "daily_loss_limit": 0.04,
    "weekly_loss_limit": 0.08,
    "monthly_loss_limit": 0.15,
    "max_open_positions": 2,
    "max_trades_per_day": 10,
}


def loose(**kw):
    """An ANALYZE-like engine: nothing capped, loss limits off (1.0), with PY on the live numbers."""
    base = {"max_open_positions": 0, "max_trades_per_day": 0, "daily_loss_limit": 1.0,
            "weekly_loss_limit": 1.0, "monthly_loss_limit": 1.0, "strategy_limits": {PY: LIVE_NUMBERS},
            "max_positions_per_symbol": 0, "max_positions_per_sector": 0}
    base.update(kw)
    return _engine(**base)


class TestTradesAndPositions:
    def test_the_override_caps_trades_per_day_for_its_own_book_only(self):
        engine = loose(strategy_limits={PY: {"max_trades_per_day": 10}})
        for i in range(10):
            engine.record_trade(strategy=PY, symbol=f"S{i}")
            engine.record_close(pnl=1.0, strategy=PY, symbol=f"S{i}")
        assert engine.check_exposure(PY) is False
        assert "Max trades/day (10/10)" in engine.exposure_block_reason(PY)
        for i in range(10):
            engine.record_trade(strategy=OTHER, symbol=f"S{i}")
            engine.record_close(pnl=1.0, strategy=OTHER, symbol=f"S{i}")
        assert engine.check_exposure(OTHER) is True  # the profile is unlimited

    def test_the_override_caps_open_positions(self):
        engine = loose()
        engine.record_trade(strategy=PY, symbol="A")
        engine.record_trade(strategy=PY, symbol="B")
        assert engine.check_exposure(PY) is False
        assert "Max positions (2/2)" in engine.exposure_block_reason(PY)
        assert engine.effective_max_open_positions_for(PY) == 2
        assert engine.effective_max_open_positions_for(OTHER) == 0
        assert engine.capacity_status(PY) == "2/2 positions open"


class TestLossLimits:
    @pytest.mark.parametrize(
        "field,value,text",
        [("daily_realised_loss", 4_000.0, "Daily loss limit hit"),
         ("weekly_net_pnl", -8_000.0, "Weekly net drawdown limit hit"),
         ("monthly_net_pnl", -15_000.0, "Monthly net drawdown limit hit")],
    )
    def test_each_live_loss_limit_applies_to_its_own_book(self, field, value, text):
        engine = loose()
        for tag in (PY, OTHER):
            state = engine._state(tag)
            state.last_known_capital = 100_000
            setattr(state, field, value)
        assert engine.check_exposure(PY) is False
        assert text in engine.exposure_block_reason(PY)
        assert engine.check_exposure(OTHER) is True  # off in the profile

    def test_just_under_the_limit_still_trades(self):
        engine = loose()
        state = engine._state(PY)
        state.last_known_capital = 100_000
        state.daily_realised_loss = 3_999.0
        assert engine.check_exposure(PY) is True


class TestWhoIsAffected:
    def test_no_override_means_the_profile_numbers(self):
        engine = _engine(max_trades_per_day=3, max_open_positions=2)
        assert engine._limit(PY, "max_trades_per_day") == 3
        assert engine._limit(None, "max_open_positions") == 2

    def test_a_live_pooled_bucket_ignores_overrides(self):
        engine = _engine(trade_mode="live", max_trades_per_day=3, strategy_limits={PY: LIVE_NUMBERS})
        assert engine._limit(PY, "max_trades_per_day") == 3

    def test_an_override_may_name_only_some_limits(self):
        engine = loose(strategy_limits={PY: {"max_trades_per_day": 1}})
        assert engine._limit(PY, "max_trades_per_day") == 1
        assert engine._limit(PY, "daily_loss_limit") == 1.0

    def test_the_tag_match_ignores_case(self):
        engine = loose(strategy_limits={"breakout-py": LIVE_NUMBERS})
        assert engine._limit("BREAKOUT-PY", "max_trades_per_day") == 10

    def test_the_dynamic_sentinel_is_computed_per_book(self):
        engine = loose(
            strategy_limits={PY: {"max_open_positions": -1}},
            mis_margin_pct=0.2, worst_case_sl_pct=0.002, margin_reserve_buffer=0.0,
        )
        assert engine._effective_max_open_positions(1_000_000, PY) >= 1
        assert engine._effective_max_open_positions(1_000_000, OTHER) == 0


class TestWiring:
    LIVE = {"max_open_positions": -1, "max_trades_per_day": 10, "daily_loss_limit": 0.04,
            "weekly_loss_limit": 0.08, "monthly_loss_limit": 0.15, "min_entry_price": 300}

    def _settings(self, monkeypatch, limits="live"):
        monkeypatch.setattr(runtime, "settings", SimpleNamespace(
            mode_profiles={"live": self.LIVE, "analyze": {}},
            runner=SimpleNamespace(strategies={
                PY: RunnerStrategy("python", "tp1_only", "ladder", True, 2, limits),
                "NOLIMITS": RunnerStrategy("python", "tp1_only", "ladder", True, None, "mode"),
            }),
        ))

    def test_analyze_gets_the_live_numbers_for_a_strategy_that_asks(self, monkeypatch):
        self._settings(monkeypatch)
        got = runtime._paper_strategy_limits("analyze")
        assert set(got) == {PY}
        assert got[PY] == {k: self.LIVE[k] for k in RiskEngine.LIMIT_NAMES}

    def test_a_strategy_on_mode_limits_gets_nothing(self, monkeypatch):
        self._settings(monkeypatch, limits="mode")
        assert runtime._paper_strategy_limits("analyze") == {}

    def test_live_mode_gets_no_overrides(self, monkeypatch):
        self._settings(monkeypatch)
        assert runtime._paper_strategy_limits("live") == {}
