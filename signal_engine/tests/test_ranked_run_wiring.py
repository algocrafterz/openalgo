"""record_run wiring for a ranked strategy: spec binding, fixed notional, the panel hook, truncation."""

from __future__ import annotations

import dataclasses

import pytest

from signal_engine.backtest import __main__ as cli
from signal_engine.backtest import data, harness
from signal_engine.backtest import record_run as rr
from signal_engine.backtest import truncation as tr
from signal_engine.backtest.session import derive_calendar
from signal_engine.backtest.swing_engine import SwingRunConfig, simulate_swing
from signal_engine.tests import test_evidence_ledgers as tel
from signal_engine.tests.published_fixtures import frames_resolution
from signal_engine.tests.ranked_fixtures import RankedTop, RankParams, ranked_frames

SPEC_PARAMS = {"n": 3, "closeness": 0.9, "window": 20}
SETTINGS = {
    "eligibility_min_prior_sessions": 2,
    "eligibility_min_volume": 1,
    "eligibility_min_price": 10.0,
    "cost_bps": 24.0,
    "warmup_bars": 20,
    "stop": "none",
    "capital_per_position": 100000,
    "parameters": SPEC_PARAMS,
    "holding_period": "none",
}


class TestParameterBinding:
    def test_equal_parameters_pass(self):
        rr.check_parameter_binding({"n": 3, "closeness": 0.9}, {"n": 3, "closeness": 0.9})

    def test_an_int_and_the_same_float_are_equal(self):
        rr.check_parameter_binding({"pct": 5}, {"pct": 5.0})

    def test_a_different_value_fails_and_names_both(self):
        with pytest.raises(rr.ParameterBindingError, match=r"closeness.*0\.9.*0\.8"):
            rr.check_parameter_binding({"closeness": 0.9}, {"closeness": 0.8})

    def test_a_class_parameter_missing_from_the_spec_fails(self):
        with pytest.raises(rr.ParameterBindingError, match="seed"):
            rr.check_parameter_binding({"n": 3}, {"n": 3, "seed": 1})

    def test_a_spec_parameter_missing_from_the_class_fails(self):
        with pytest.raises(rr.ParameterBindingError, match="window"):
            rr.check_parameter_binding({"n": 3, "window": 20}, {"n": 3})

    def test_the_word_none_means_no_parameters(self):
        rr.check_parameter_binding("none", {})
        with pytest.raises(rr.ParameterBindingError):
            rr.check_parameter_binding("none", {"n": 3})

    def test_every_mismatch_is_listed(self):
        with pytest.raises(rr.ParameterBindingError) as err:
            rr.check_parameter_binding({"a": 1, "b": 2}, {"a": 9, "c": 3})
        text = str(err.value)
        assert "a" in text and "b" in text and "c" in text


@pytest.fixture
def ranked(monkeypatch):
    seen: dict = {"backtests": 0}

    class Spy(harness.Backtest):
        def __init__(self, strategy, frames, run, **kw):
            seen["frames"] = dict(frames)
            seen["run"] = run
            seen["backtests"] += 1
            super().__init__(strategy, frames, run, **kw)

    class Strat(RankedTop):
        def prepare_panel(self, frames, p, calendar):
            seen["panel_calls"] = seen.get("panel_calls", 0) + 1
            seen["calendar_days"] = len(calendar.trading_days)
            return super().prepare_panel(frames, p, calendar)

    monkeypatch.setattr(cli, "_register", lambda: None)
    monkeypatch.setattr(cli, "REGISTRY", {"k": (Strat, RankParams, "swing")})
    # Plan Phase 4: the run reads bars only through its dataset resolution.
    seen["res"] = frames_resolution(daily=ranked_frames())
    monkeypatch.setattr(harness, "Backtest", Spy)
    monkeypatch.setattr(rr, "_cost_model_or_none", lambda kind, slip=None: tel.MODEL)
    return seen


class TestExecuteRankedBacktest:
    def test_a_mismatch_between_spec_and_class_fails_before_anything_runs(self, ranked):
        bad = {**SETTINGS, "parameters": {**SPEC_PARAMS, "closeness": 0.5}}
        with pytest.raises(rr.ParameterBindingError, match="closeness"):
            rr.execute_backtest("k", dataset=ranked["res"], settings=bad)
        assert ranked["backtests"] == 0

    def test_the_panel_hook_runs_once_on_the_data_layers_frames_and_calendar(self, ranked):
        rr.execute_backtest("k", dataset=ranked["res"], settings=SETTINGS, run_truncation=False)
        assert ranked["panel_calls"] == 1
        assert ranked["calendar_days"] == 130

    def test_the_engine_receives_the_schedule_columns(self, ranked):
        rr.execute_backtest("k", dataset=ranked["res"], settings=SETTINGS, run_truncation=False)
        frame = next(iter(ranked["frames"].values()))
        for column in ("rebalance_prev", "target_prev", "target_latest"):
            assert column in frame.columns

    def test_no_stop_with_a_capital_per_position_sizes_every_trade_to_it(self, ranked):
        exe = rr.execute_backtest("k", dataset=ranked["res"], settings=SETTINGS, run_truncation=False)
        assert exe.trades and all(not t.r_defined for t in exe.trades)
        assert exe.cost_model.fixed_notional == 100000.0
        assert set(exe.cost_components["modeled_cost_bps"].round(6)) <= {
            round(exe.cost_model.components(t)["modeled_cost_bps"], 6) for t in exe.trades
        }

    def test_the_specs_no_maximum_holding_period_reaches_the_run_even_without_an_override(
        self, ranked, monkeypatch
    ):
        class NoOverride(RankedTop):
            run_overrides = {}

        monkeypatch.setattr(cli, "REGISTRY", {"k": (NoOverride, RankParams, "swing")})
        rr.execute_backtest("k", dataset=ranked["res"], settings=SETTINGS, run_truncation=False)
        assert ranked["run"].max_hold_bars is None

    def test_a_finite_holding_period_is_not_turned_into_unlimited(self, ranked, monkeypatch):
        class NoOverride(RankedTop):
            run_overrides = {}

        monkeypatch.setattr(cli, "REGISTRY", {"k": (NoOverride, RankParams, "swing")})
        settings = {**SETTINGS, "holding_period": "12 bars"}
        rr.execute_backtest("k", dataset=ranked["res"], settings=settings, run_truncation=False)
        assert ranked["run"].max_hold_bars == 120  # the engine default, untouched

    def test_a_strategy_run_override_reaches_the_swing_run_config(self, ranked):
        exe = rr.execute_backtest("k", dataset=ranked["res"], settings=SETTINGS, run_truncation=False)
        assert ranked["run"].max_hold_bars is None
        assert exe.params["run"]["max_hold_bars"] is None

    def test_the_frozen_parameters_are_what_the_run_records(self, ranked):
        exe = rr.execute_backtest("k", dataset=ranked["res"], settings=SETTINGS, run_truncation=False)
        assert exe.params["strategy"] == SPEC_PARAMS

    def test_a_stop_based_swing_run_keeps_its_risk_based_cost_model(self, ranked):
        settings = {k: v for k, v in SETTINGS.items() if k not in ("stop", "capital_per_position")}
        exe = rr.execute_backtest("k", dataset=ranked["res"], settings=settings, run_truncation=False)
        assert exe.cost_model.fixed_notional is None

    def test_truncation_runs_on_the_whole_truncated_panel(self, ranked):
        exe = rr.execute_backtest("k", dataset=ranked["res"], settings=SETTINGS)
        assert exe.truncation is not None and exe.truncation["ok"] is True
        assert exe.truncation["checked"]


class CausalPanel(RankedTop):
    pass


class LookaheadPanel(RankedTop):
    """Ranks every week against the whole-history high, which a truncated run cannot see."""

    def prepare_panel(self, frames, p, calendar):
        from signal_engine.backtest import panel

        sessions = list(calendar.trading_days)
        close = panel.close_panel(frames, sessions)
        whole_high = close.max()  # the look-ahead: uses sessions after each rebalance
        score = (close / whole_high).where(close.notna())
        rebalances = panel.weekly_rebalance_sessions(sessions)
        schedule = panel.top_n_schedule(score, rebalances, p.n, p.closeness)
        return panel.attach_schedule(frames, rebalances, schedule, sessions)


def _truncation(strategy_cls):
    frames = ranked_frames()
    strategy, params = strategy_cls(), RankParams()
    run = SwingRunConfig(cost_bps=24.0, **strategy.run_overrides)

    def panel_fn(fr):
        return strategy.prepare_panel(fr, params, derive_calendar(fr))

    return tr.truncation_check(
        strategy, params, frames, run, engine=simulate_swing, symbols=4, panel=panel_fn
    )


class TestPanelTruncation:
    def test_a_causal_ranked_strategy_reproduces_its_earlier_trades(self):
        result = _truncation(CausalPanel)
        assert result.ok, result.mismatches
        assert result.checked

    def test_a_ranking_that_looks_ahead_is_caught(self):
        result = _truncation(LookaheadPanel)
        assert not result.ok
        assert result.mismatches

    def test_trades_without_a_stop_do_not_fail_on_nan_equality(self):
        result = _truncation(CausalPanel)
        assert not [m for m in result.mismatches if m["kind"] == "trade_differs"]

    def test_the_coverage_note_says_the_ranking_was_recomputed(self):
        result = _truncation(CausalPanel)
        assert "panel" in result.coverage.lower()


class TestFixedNotionalSizingBasis:
    """sizing.basis fixed_notional prices every trade's costs at sizing.capital_per_trade."""

    def run(self, ranked, **extra):
        base = {k: v for k, v in SETTINGS.items() if k not in ("stop", "capital_per_position")}
        return rr.execute_backtest("k", dataset=ranked["res"], settings={**base, **extra},
                                   run_truncation=False)

    def test_the_basis_binds_the_cost_model_even_when_the_spec_has_a_stop(self, ranked):
        exe = self.run(ranked, stop="1 ATR below entry", sizing_basis="fixed_notional",
                       capital_per_trade=100000)
        assert exe.cost_model.fixed_notional == 100000.0

    def test_the_basis_wins_over_a_portfolio_capital_per_position(self, ranked):
        exe = self.run(ranked, stop="none", capital_per_position=50000,
                       sizing_basis="fixed_notional", capital_per_trade=100000)
        assert exe.cost_model.fixed_notional == 100000.0

    def test_the_risk_basis_and_no_basis_keep_risk_sizing(self, ranked):
        assert self.run(ranked, stop="1 ATR below entry").cost_model.fixed_notional is None
        risk = self.run(ranked, stop="1 ATR below entry", sizing_basis="risk",
                        capital_per_trade=100000)
        assert risk.cost_model.fixed_notional is None

    def test_no_basis_and_no_stop_still_uses_the_capital_per_position(self, ranked):
        exe = self.run(ranked, stop="none", capital_per_position=70000)
        assert exe.cost_model.fixed_notional == 70000.0

    def test_a_fixed_notional_basis_without_an_amount_is_refused(self, ranked):
        with pytest.raises(ValueError, match="capital_per_trade"):
            self.run(ranked, stop="1 ATR below entry", sizing_basis="fixed_notional")

    def test_the_intent_settings_carry_the_sizing_block(self):
        got = rr._intent_settings({"sizing": {"basis": "fixed_notional",
                                              "capital_per_trade": 100000,
                                              "risk_per_trade_pct": 1.0}})
        assert got == {"sizing_basis": "fixed_notional", "capital_per_trade": 100000}
        assert rr._intent_settings({"sizing": {"capital_per_trade": 1}}) == {}
