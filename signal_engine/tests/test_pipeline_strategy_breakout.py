"""BreakoutStrategy is compute_signals wrapped in the Strategy protocol, nothing more."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from signal_engine.pipeline.strategies.breakout import BreakoutStrategy
from signal_engine.pipeline.strategy import Fire
from signal_engine.strategy_core.breakout import BreakoutParams, compute_signals

from .test_backtest_breakout_adapter import raw_bars, scenario_inputs


@pytest.fixture
def strat(monkeypatch):
    monkeypatch.setattr(
        "signal_engine.pipeline.strategies.breakout.build_breakout_inputs", scenario_inputs
    )
    return BreakoutStrategy(tick_source=lambda sym: 0.05)


class TestProtocolSurface:
    def test_declares_what_the_runner_needs(self, strat):
        assert strat.name == "BREAKOUT"
        assert strat.interval_min == 5
        assert strat.needs_1m is True
        assert strat.warmup_sessions >= 20
        assert strat.entry_window_min == (9 * 60 + 45, 11 * 60 + 45)


class TestEvaluate:
    def test_matches_compute_signals_row_for_row(self, strat):
        raw = raw_bars()
        fires = strat.evaluate("SYM", raw, raw)
        ref = compute_signals(scenario_inputs(), replace(BreakoutParams(), tick=0.05))
        assert len(fires) == len(ref) == 1
        f, r = fires[0], ref.iloc[0]
        assert isinstance(f, Fire)
        assert f.bar_time == ref.index[0]
        assert f.direction == int(r["direction"])
        assert f.entry_ref == pytest.approx(r["entry_ref"])
        assert f.sl == pytest.approx(r["sl"])
        assert f.targets == pytest.approx((r["tp1"], r["tp2"], r["tp3"]))
        assert f.tag == r["code"] == "PDH-RT"

    def test_context_has_score_fields_and_levels(self, strat):
        raw = raw_bars()
        ctx = strat.evaluate("SYM", raw, raw)[0].context
        for k in ("score", "conf", "chase", "headroom", "atr", "tp2", "tp3", "pdh", "ibh", "ibl"):
            assert k in ctx, k
        assert ctx["pdh"] == 100.0
        assert "pvah" not in ctx  # NaN levels are omitted

    def test_no_fires_gives_empty_list(self, monkeypatch):
        monkeypatch.setattr(
            "signal_engine.pipeline.strategies.breakout.build_breakout_inputs", scenario_inputs
        )
        s = BreakoutStrategy(BreakoutParams(score_threshold=99), tick_source=lambda sym: 0.05)
        raw = raw_bars()
        assert s.evaluate("SYM", raw, raw) == []

    def test_tick_comes_from_the_tick_source_per_symbol(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            "signal_engine.pipeline.strategies.breakout.build_breakout_inputs",
            lambda d5, d1, tick, **k: seen.setdefault("tick", tick) and scenario_inputs(),
        )
        BreakoutStrategy(tick_source=lambda sym: 0.1).evaluate("SYM", raw_bars(), raw_bars())
        assert seen["tick"] == 0.1

    def test_inputs_are_float64(self, monkeypatch):
        seen = {}

        def spy(d5, d1, tick, **k):
            seen["dtype"] = d5["Close"].dtype
            return scenario_inputs()

        monkeypatch.setattr("signal_engine.pipeline.strategies.breakout.build_breakout_inputs", spy)
        r = raw_bars().astype("float32")
        BreakoutStrategy(tick_source=lambda s: 0.05).evaluate("SYM", r, r)
        assert seen["dtype"] == np.float64
