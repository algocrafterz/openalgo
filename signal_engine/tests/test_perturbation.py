"""G4.1 parameter perturbation: neighbours, the stage, and the gate."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import evaluation, indicators, perturbation
from signal_engine.backtest.costs import recost
from signal_engine.backtest.engine import simulate
from signal_engine.backtest.perturbation import (
    ParamMeta,
    build_neighbours,
    neighbour_value,
    structural,
    tunable,
)
from signal_engine.backtest.strategies.base import Strategy
from signal_engine.backtest.strategies.vwap_reversion import VwapReversion
from signal_engine.backtest.types import Ctx, EntrySignal, RunConfig
from signal_engine.strategy_core.vwap_reversion import VwapReversionParams
from signal_engine.tests.cost_fixtures import make_model

SIGNAL_MINS = 9 * 60 + 20


class TestIdea029Matrix:
    """The exact 24 neighbours the owner approved on 2026-10-08."""

    EXPECTED = [
        ("vwap_sd", 0.8, 1.6), ("vwap_sd", 1.2, 2.4),
        ("atr_period_5m", 0.8, 11), ("atr_period_5m", 1.2, 17),
        ("min_vwap_distance_atr", 0.8, 0.6), ("min_vwap_distance_atr", 1.2, 0.9),
        ("stop_buffer_atr", 0.8, 0.16), ("stop_buffer_atr", 1.2, 0.24),
        ("adx_period_15m", 0.8, 11), ("adx_period_15m", 1.2, 17),
        ("adx_max", 0.8, 17.6), ("adx_max", 1.2, 26.4),
        ("ema_regime_period_15m", 0.8, 16), ("ema_regime_period_15m", 1.2, 24),
        ("ema_regime_lookback_bars", 0.8, 3), ("ema_regime_lookback_bars", 1.2, 5),
        ("ema_regime_max_change_atr", 0.8, 0.2), ("ema_regime_max_change_atr", 1.2, 0.3),
        ("atr_period_15m", 0.8, 11), ("atr_period_15m", 1.2, 17),
        ("ema_entry_period_5m", 0.8, 7), ("ema_entry_period_5m", 1.2, 11),
        ("arm_bars", 0.8, 5), ("arm_bars", 1.2, 7),
    ]  # fmt: skip

    def test_the_generated_matrix_is_exactly_the_approved_one(self):
        base = dataclasses.asdict(VwapReversionParams())
        neighbours, skipped, problems = build_neighbours(base, VwapReversion.param_meta)
        assert problems == []
        assert [(n.parameter, n.multiplier, n.value) for n in neighbours] == self.EXPECTED
        assert len(neighbours) == 24
        assert [s["parameter"] for s in skipped] == ["warmup_15m_bars"]
        assert skipped[0]["reason"].startswith("structural:")

    def test_value_types_follow_the_declared_kind(self):
        base = dataclasses.asdict(VwapReversionParams())
        neighbours, _, _ = build_neighbours(base, VwapReversion.param_meta)
        for n in neighbours:
            kind = VwapReversion.param_meta[n.parameter].kind
            assert isinstance(n.value, int if kind == "int" else float), n

    def test_the_declaration_covers_every_parameter_and_no_neighbour_reaches_a_bound(self):
        names = {f.name for f in dataclasses.fields(VwapReversionParams)}
        assert set(VwapReversion.param_meta) == names
        base = dataclasses.asdict(VwapReversionParams())
        for name, meta in VwapReversion.param_meta.items():
            if meta.tunable:
                assert meta.min < base[name] < meta.max

    def test_approved_bounds(self):
        got = {n: (m.min, m.max) for n, m in VwapReversion.param_meta.items() if m.tunable}
        assert got == {
            "vwap_sd": (1.0, 3.5), "atr_period_5m": (5, 50),
            "min_vwap_distance_atr": (0.25, 3.0), "stop_buffer_atr": (0.05, 1.0),
            "adx_period_15m": (5, 30), "adx_max": (10.0, 40.0),
            "ema_regime_period_15m": (8, 60), "ema_regime_lookback_bars": (1, 12),
            "ema_regime_max_change_atr": (0.05, 1.0), "atr_period_15m": (5, 30),
            "ema_entry_period_5m": (3, 30), "arm_bars": (2, 18),
        }  # fmt: skip

    def test_the_warm_up_still_covers_the_slowest_indicator_at_the_upper_bounds(self):
        m = VwapReversion.param_meta
        slowest = max(2 * m["adx_period_15m"].max, m["ema_regime_period_15m"].max
                      + m["ema_regime_lookback_bars"].max, m["atr_period_15m"].max)  # fmt: skip
        assert slowest <= VwapReversionParams().warmup_15m_bars


class TestNeighbourRules:
    def test_floats_keep_precision_and_are_cleaned_of_float_noise(self):
        meta = tunable("float", 0.1, 5.0)
        assert neighbour_value(0.75, 0.8, meta)[0] == 0.6  # 0.75 * 0.8 is 0.6000000000000001
        assert neighbour_value(2.0, 1.2, meta)[0] == 2.4

    def test_ints_round_to_nearest(self):
        meta = tunable("int", 1, 100)
        assert neighbour_value(14, 0.8, meta)[0] == 11  # 11.2
        assert neighbour_value(14, 1.2, meta)[0] == 17  # 16.8
        assert neighbour_value(9, 1.2, meta)[0] == 11  # 10.8

    def test_ties_round_away_from_zero(self):
        assert perturbation._round_half_away(2.5) == 3
        assert perturbation._round_half_away(3.5) == 4
        assert perturbation._round_half_away(-2.5) == -3

    def test_an_int_that_rounds_back_to_base_steps_by_one(self):
        meta = tunable("int", 1, 100)
        assert neighbour_value(2, 0.8, meta) == (1, "stepped")  # 1.6 rounds to 2 = base
        assert neighbour_value(2, 1.2, meta) == (3, "stepped")  # 2.4 rounds to 2 = base
        assert (
            neighbour_value(1, 0.8, tunable("int", 1, 100))[0] == 1
        )  # 0.8 rounds to 1 = base, steps to 0, clamps to 1

    def test_values_are_clamped_to_the_bounds(self):
        assert neighbour_value(3.0, 1.2, tunable("float", 1.0, 3.2))[0] == 3.2
        assert neighbour_value(6, 0.8, tunable("int", 5, 50))[0] == 5

    def test_a_neighbour_equal_to_base_after_clamping_is_dropped_and_listed(self):
        meta = {"a": tunable("float", 1.0, 3.0), "b": tunable("float", 1.0, 10.0)}
        neighbours, skipped, problems = build_neighbours({"a": 3.0, "b": 2.0}, meta)
        assert problems == []
        assert [(n.parameter, n.multiplier) for n in neighbours] == [
            ("a", 0.8), ("b", 0.8), ("b", 1.2)]  # fmt: skip
        assert skipped == [
            {"parameter": "a", "multiplier": 1.2, "reason": "at bound: equals base after clamping"}
        ]

    def test_a_tunable_parameter_with_no_possible_neighbour_is_a_declaration_error(self):
        meta = {"a": tunable("int", 5, 5 + 0)}
        _, _, problems = build_neighbours({"a": 5}, meta)
        assert any("bounds" in p for p in problems)

    def test_order_is_declaration_order_then_multiplier(self):
        meta = {"z": tunable("float", 0.1, 9.0), "a": tunable("float", 0.1, 9.0)}
        neighbours, _, _ = build_neighbours({"a": 1.0, "z": 1.0}, meta)
        assert [(n.parameter, n.multiplier) for n in neighbours] == [
            ("z", 0.8), ("z", 1.2), ("a", 0.8), ("a", 1.2)]  # fmt: skip


class TestDeclarationContract:
    def test_an_undeclared_parameter_is_refused(self):
        _, _, problems = build_neighbours({"a": 1.0, "b": 2.0}, {"a": tunable("float", 0.1, 5)})
        assert any("no declaration: b" in p for p in problems)

    def test_a_declared_name_that_is_not_a_parameter_is_refused(self):
        meta = {"a": tunable("float", 0.1, 5), "ghost": tunable("float", 0.1, 5)}
        _, _, problems = build_neighbours({"a": 1.0}, meta)
        assert any("ghost" in p for p in problems)

    def test_a_base_outside_its_bounds_is_refused(self):
        _, _, problems = build_neighbours({"a": 9.0}, {"a": tunable("float", 0.1, 5)})
        assert any("outside the declared bounds" in p for p in problems)

    def test_a_structural_parameter_needs_a_reason(self):
        _, _, problems = build_neighbours({"a": 1}, {"a": ParamMeta(tunable=False)})
        assert any("needs a reason" in p for p in problems)

    def test_the_kind_must_match_the_base(self):
        _, _, p1 = build_neighbours({"a": 1.5}, {"a": tunable("int", 1, 9)})
        _, _, p2 = build_neighbours({"a": 1.5}, {"a": tunable("str", 1, 9)})
        assert p1 and p2

    def test_bool_is_not_a_number(self):
        _, _, problems = build_neighbours({"a": True}, {"a": tunable("int", 0, 3)})
        assert problems


@dataclass(frozen=True)
class _P:
    stop_pct: float = 0.01
    tp_mult: float = 2.0
    label: int = 1


class _Long(Strategy):
    """Long at the 09:20 close with a percent stop; the target is far, so only the stop matters."""

    name = "synthetic-long"
    param_meta = {
        "stop_pct": tunable("float", 0.002, 0.05),
        "tp_mult": tunable("float", 1.0, 10.0),
        "label": structural("a tag, not a rule"),
    }

    def prepare(self, df, p):
        return indicators.add_session_columns(df)

    def entry(self, c, i, p, direction):
        if direction != 1 or c["mins"][i] != SIGNAL_MINS:
            return None
        close = float(c["Close"][i])
        return EntrySignal(1, close * (1 - p.stop_pct), close * (1 + 0.5 * p.tp_mult))


def _frames(n_days=40, symbols=3, spike=None, drift_bps=60.0):
    out = {}
    days = [d.date() for d in pd.bdate_range("2026-01-05", periods=n_days)]
    for s in range(symbols):
        parts = []
        for day in days:
            idx = pd.date_range(f"{day} 09:15", periods=75, freq="5min", tz="Asia/Kolkata")
            k = np.arange(75)
            close = 100.0 * (1 + drift_bps / 1e4 * np.maximum(0, k - 1) / 65)
            open_ = np.r_[close[0], close[:-1]]
            low = np.minimum(open_, close) * (1 - 2e-5)
            if spike is not None:
                low[10] = 100.0 * (1 - spike)
            parts.append(
                pd.DataFrame(
                    {
                        "Open": open_,
                        "High": np.maximum(open_, close) * (1 + 2e-5),
                        "Low": low,
                        "Close": close,
                        "Volume": 1000.0,
                    },
                    index=idx,
                )  # fmt: skip
            )
        out[f"S{s}"] = pd.concat(parts)
    return out


def _run():
    return RunConfig(skip_open_minutes=0, entry_cutoff_min=11 * 60, min_sl_pct=0.002)


def _headline(strategy, params, frames, run, model):
    trades = []
    for sym, df in frames.items():
        own = _copy(strategy)
        trades += simulate(
            Ctx(own.prepare_for(sym, df.copy(), params), symbol=sym), own, params, run
        )
    return recost(trades, model)


def _copy(strategy):
    import copy

    return copy.deepcopy(strategy)


def _stage(frames, **kw):
    model = make_model()
    run = _run()
    strategy = kw.pop("strategy", _Long())
    params = kw.pop("params", _P())
    head = kw.pop("headline", None)
    if head is None:
        head = _headline(strategy, params, frames, run, model)
    calls = []

    def load(chunk):
        calls.append(list(chunk))
        return {s: frames[s].copy() for s in chunk}

    ev = perturbation.run_variants(
        strategy=strategy, params=params, meta=kw.pop("meta", strategy.param_meta),
        load_frames=kw.pop("load", load), symbols=list(frames), run=run, cost_model=model,
        headline_trades=head, oos_start="2026-02-20", shared={"dataset_version": "t"}, chunk_size=2,
    )  # fmt: skip
    return ev, calls


class TestStage:
    def test_a_plateau_is_complete_with_every_neighbour_matching_the_base(self):
        ev, _ = _stage(_frames())
        assert ev["state"] == "complete"
        assert len(ev["neighbours"]) == 4  # stop_pct and tp_mult, both ways; label is structural
        assert ev["skipped"] == [{"parameter": "label", "reason": "structural: a tag, not a rule"}]
        # the result in bps does not move; net R rescales with the stop distance, which is the
        # risk unit, so the stop neighbours differ in R by 1/0.8 and 1/1.2
        assert all(r["net_bps"] == pytest.approx(ev["base"]["net_bps"]) for r in ev["neighbours"])
        for r in ev["neighbours"]:
            scale = 1 / r["multiplier"] if r["parameter"] == "stop_pct" else 1.0
            assert r["net_r"] == pytest.approx(ev["base"]["net_r"] * scale)
        assert ev["base_replay_matches"] is True
        row = evaluation._g41(_inputs(ev))
        assert row[0] == "pass"

    def test_a_sharp_peak_fails_and_names_the_parameter(self):
        # a dip of 0.9% never hits the base 1.0% stop but hits the 0.8x stop on every day
        ev, _ = _stage(_frames(spike=0.009))
        assert ev["state"] == "complete"
        by = {(r["parameter"], r["multiplier"]): r for r in ev["neighbours"]}
        assert by[("stop_pct", 0.8)]["net_r"] < 0 < ev["base"]["net_r"]
        status, _, _, note = evaluation._g41(_inputs(ev))
        assert status == "fail" and "stop_pct x0.8" in note

    def test_the_base_replay_equals_the_headline_run(self):
        frames = _frames()
        head = _headline(_Long(), _P(), frames, _run(), make_model())
        ev, _ = _stage(frames, headline=head)
        assert ev["state"] == "complete" and ev["base"]["trades"] == len(head)
        assert ev["base_replay_matches"] is True

    def test_a_replay_that_differs_from_the_headline_stops_the_stage(self):
        frames = _frames()
        head = _headline(_Long(), _P(), frames, _run(), make_model())[:-5]
        ev, _ = _stage(frames, headline=head)
        assert ev["state"] == "base_replay_mismatch"
        assert evaluation._g41(_inputs(ev))[0] == "not_evaluated"

    def test_a_base_that_is_not_net_positive_runs_no_variant(self):
        frames = _frames(drift_bps=-60.0)
        ev, calls = _stage(frames)
        assert ev["state"] == "base_not_positive"
        assert calls == []
        status, value, _, note = evaluation._g41(_inputs(ev))
        assert status == "fail" and "not positive" in note

    def test_no_declaration_is_not_evaluated(self):
        ev, calls = _stage(_frames(), meta=None)
        assert ev["state"] == "no_declaration" and calls == []
        assert evaluation._g41(_inputs(ev))[0] == "not_evaluated"

    def test_a_declaration_error_is_not_evaluated_and_names_the_problem(self):
        ev, calls = _stage(_frames(), meta={"stop_pct": tunable("float", 0.002, 0.05)})
        assert ev["state"] == "declaration_error" and "tp_mult" in ev["reason"] and calls == []

    def test_the_variants_do_not_touch_the_strategy_or_the_params(self):
        class Stateful(_Long):
            def __init__(self):
                self.seen = 0

            def entry(self, c, i, p, direction):
                self.seen += 1
                return super().entry(c, i, p, direction)

        strategy, params = Stateful(), _P()
        _stage(_frames(), strategy=strategy, params=params, headline=[])
        assert strategy.seen == 0 and params == _P()

    def test_the_stage_is_deterministic(self):
        a, _ = _stage(_frames(spike=0.009))
        b, _ = _stage(_frames(spike=0.009))
        assert a == b

    def test_each_neighbour_differs_from_the_base_only_in_its_params_fingerprint(self):
        ev, _ = _stage(_frames())
        fps = {r["params_fingerprint"] for r in ev["neighbours"]}
        assert len(fps) == 4 and ev["base"]["params_fingerprint"] not in fps
        assert ev["shared"] == {"dataset_version": "t"}

    def test_the_variants_are_not_trials(self):
        ev, _ = _stage(_frames())
        assert ev["diagnostic_not_trials"] is True

    def test_a_variant_that_raises_is_recorded_and_counted_against(self):
        class Boom(_Long):
            def entry(self, c, i, p, direction):
                if p.tp_mult > 2.0:
                    raise RuntimeError("bad parameter")
                return super().entry(c, i, p, direction)

        ev, _ = _stage(_frames(), strategy=Boom())
        bad = [r for r in ev["neighbours"] if r.get("error")]
        assert len(bad) == 1 and "bad parameter" in bad[0]["error"]
        assert bad[0]["trades"] == 0

    def test_the_stage_loads_each_symbol_once(self):
        _, calls = _stage(_frames())
        assert sorted(s for c in calls for s in c) == ["S0", "S1", "S2"]


def _inputs(ev):
    return evaluation._Inputs(
        trades=[], metrics={}, n_trials=1, horizon="intraday", cost_bps=0.0, cost_model=None,
        oos_start=None, position_caps=(), params={}, perturbation=ev,
    )  # fmt: skip


def _evidence(base=1.0, nets=(), trades=100, state="complete"):
    rows = [
        {"parameter": f"p{i}", "multiplier": 0.8, "value": i, "base_value": 9, "trades": trades,
         "net_r": r, "net_bps": 0.0, "params_fingerprint": "x"}
        for i, r in enumerate(nets)
    ]  # fmt: skip
    return {"version": 1, "state": state, "reason": "", "base": {"trades": 500, "net_r": base},
            "neighbours": rows}  # fmt: skip


class TestGate:
    def test_no_stage_is_not_evaluated(self):
        status, _, _, note = evaluation._g41(_inputs(None))
        assert status == "not_evaluated" and "not run" in note

    def test_malformed_evidence_fails(self):
        assert evaluation._g41(_inputs({"state": "bogus", "base": {"trades": 1}}))[0] == "fail"
        assert evaluation._g41(_inputs({"state": "complete", "base": {"trades": 1}}))[0] == "fail"

    def test_seven_in_ten_passes_and_six_in_ten_fails(self):
        assert evaluation._g41(_inputs(_evidence(1.0, [0.9] * 7 + [-0.1] * 3)))[0] == "fail"  # weak
        assert evaluation._g41(_inputs(_evidence(1.0, [0.9] * 7 + [0.5] * 3)))[0] == "pass"
        assert evaluation._g41(_inputs(_evidence(1.0, [0.9] * 6 + [-0.1] * 4)))[0] == "fail"

    def test_the_share_boundary_is_exact(self):
        # 17 of 23 is 73.9 percent, 16 of 23 is 69.6 percent; the failing ones keep 50% so only the share decides
        ok = _evidence(1.0, [0.9] * 17 + [-0.0001] * 6)
        assert evaluation._g41(_inputs(ok))[0] == "fail"  # the negative ones also break retention
        # isolate the share rule: neighbours that are not positive still keep 40% only if positive
        assert evaluation._g41(_inputs(_evidence(1.0, [0.5] * 23)))[0] == "pass"

    def test_exactly_sixty_percent_lost_passes_and_a_hair_more_fails(self):
        assert evaluation._g41(_inputs(_evidence(1.0, [0.4] * 10)))[0] == "pass"
        assert evaluation._g41(_inputs(_evidence(1.0, [0.4] * 9 + [0.3999])))[0] == "fail"

    def test_one_neighbour_below_retention_fails_even_with_all_positive(self):
        status, _, _, note = evaluation._g41(_inputs(_evidence(1.0, [0.9] * 9 + [0.1])))
        assert status == "fail" and "1 keep under 40%" in note

    def test_a_negative_neighbour_loses_more_than_sixty_percent(self):
        assert evaluation._g41(_inputs(_evidence(1.0, [0.9] * 9 + [-0.5])))[0] == "fail"

    def test_an_unusable_neighbour_counts_in_the_denominator_and_against(self):
        ev = _evidence(1.0, [0.9] * 10)
        ev["neighbours"][0]["trades"] = 29
        status, _, _, note = evaluation._g41(_inputs(ev))
        assert status == "fail" and "1 keep under 40%" in note

    def test_fewer_than_four_usable_neighbours_is_not_evaluated(self):
        ev = _evidence(1.0, [0.9] * 5)
        for r in ev["neighbours"][:2]:
            r["trades"] = 10
        status, value, _, note = evaluation._g41(_inputs(ev))
        assert status == "not_evaluated" and value == 3 and "4 usable" in note

    def test_a_base_that_is_not_positive_fails(self):
        ev = {"version": 1, "state": "base_not_positive", "reason": "the base net R is -0.2",
              "base": {"trades": 900, "net_r": -0.2}, "neighbours": []}  # fmt: skip
        status, _, _, note = evaluation._g41(_inputs(ev))
        assert status == "fail" and "-0.2" in note

    @pytest.mark.parametrize("state", perturbation.NOT_RUN_STATES)
    def test_every_not_run_state_is_not_evaluated(self, state):
        ev = {
            "version": 1,
            "state": state,
            "reason": "why",
            "base": {"trades": 1},
            "neighbours": [],
        }
        status, _, _, note = evaluation._g41(_inputs(ev))
        assert status == "not_evaluated" and state in note

    def test_the_peak_flag_appears_when_the_median_neighbour_is_half_the_base(self):
        status, _, _, note = evaluation._g41(_inputs(_evidence(1.0, [0.45] * 10)))
        assert "peak" in note

    def test_the_gate_is_registered_and_hard(self):
        assert "G4.1" in evaluation._EVALUATORS
        assert next(g for g in evaluation._GATES if g[0] == "G4.1")[3] is True


class TestReplayMatchesTheHeadlineOnAdjustedFrames:
    """Found on real data: the replay dropped the price factor, so its stops snapped on the wrong
    prices and 1,258 of 26,144 trades differed from the headline run."""

    FACTOR = 0.9871  # an adjusted price level, off every tick multiple

    def _setup(self):
        out = {}
        for sym, df in _frames().items():
            f = df.copy()
            # every third session dips through the 1% stop, so some trades exit at the stop
            for n, day in enumerate(sorted(set(f.index.date))):
                if n % 3 == 0:
                    idx = f.index[f.index.date == day][10]
                    f.loc[idx, "Low"] = 100.0 * 0.98
            for col in ("Open", "High", "Low", "Close"):
                f[col] = f[col] * self.FACTOR
            f["price_factor"] = self.FACTOR
            out[sym] = f
        return out

    def test_the_replay_trades_equal_the_harness_headline_trade_for_trade(self):
        from signal_engine.backtest.harness import Backtest

        frames = self._setup()
        run = _run().with_(tick_grid="safe_tick")
        bt = Backtest(_Long(), {k: v.copy() for k, v in frames.items()}, run,
                      release_raw=True, single_pass=True)  # fmt: skip
        headline = {(t.symbol, str(t.entry_time)): t for t in bt.trades(_P())}
        replay = {}
        for sym, df in frames.items():
            for t in perturbation._run_symbol(_Long(), _P(), sym, df, run):
                replay[(t.symbol, str(t.entry_time))] = t
        assert set(headline) == set(replay) and headline
        stopped = [k for k in headline if headline[k].reason == "SL"]
        assert stopped, "the setup must include stop exits"
        for k, h in headline.items():
            assert (replay[k].sl, replay[k].exit, replay[k].reason) == (h.sl, h.exit, h.reason)

    def test_without_the_carry_the_snap_would_use_the_wrong_prices(self):
        frames = self._setup()
        run = _run().with_(tick_grid="safe_tick")
        with_factor = perturbation._run_symbol(_Long(), _P(), "S0", frames["S0"], run)
        plain = frames["S0"].drop(columns="price_factor")
        no_factor = perturbation._run_symbol(_Long(), _P(), "S0", plain, run)
        assert {round(t.sl, 6) for t in with_factor} != {round(t.sl, 6) for t in no_factor}
