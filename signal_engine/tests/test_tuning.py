"""Bounded parameter tuning: grid validation, in-sample split, stable-region selection, PBO,
trial accounting and the separation guard on the later recorded run.

Everything runs on deterministic fixtures; no strategy trial is spent on real data.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import pytest

from signal_engine.backtest import record_run as rr
from signal_engine.backtest import tuning
from signal_engine.research.intake import trials
from signal_engine.tests.test_run_artifact_write import CLEAN, NOW, SUPPORTED, _git, _record
from signal_engine.tests.test_run_artifact_write import root as _root

root = _root
SPEC = {"ema_fast": 9, "ema_slow": 20, "atr_len": 14}
DAYS = [date(2026, 1, 1) + timedelta(days=i) for i in range(60)]


def _t(day, r):
    return SimpleNamespace(day=day, r_net=r, net_bps=r * 10.0)


class TestGrid:
    def test_a_valid_grid_lists_every_combination_in_order(self):
        g = tuning.parse_grid({"parameters": {"ema_fast": [5, 9], "ema_slow": [20, 30]}}, SPEC)
        assert g.combinations() == [
            {"ema_fast": 5, "ema_slow": 20},
            {"ema_fast": 5, "ema_slow": 30},
            {"ema_fast": 9, "ema_slow": 20},
            {"ema_fast": 9, "ema_slow": 30},
        ]
        assert (g.min_trades, g.holdout_fraction) == (30, 0.40)

    @pytest.mark.parametrize(
        ("raw", "match"),
        [
            ({"parameters": {"nope": [1, 2]}}, "not a parameter"),
            ({"parameters": {"ema_fast": [5]}}, "at least two"),
            ({"parameters": {"ema_fast": [9, 5]}}, "ascending"),
            ({"parameters": {"ema_fast": [5, 5]}}, "repeats"),
            ({"parameters": {"ema_fast": [5, 7.5]}}, "type"),
            ({"parameters": {"ema_fast": [5, 9]}, "holdout_fraction": 0.2}, "holdout_fraction"),
            ({"parameters": {"ema_fast": [5, 9]}, "min_trades": 0}, "min_trades"),
            ({"parameters": {"ema_fast": [5, 9]}, "seed": 1}, "unknown key"),
            ({"parameters": {"ema_fast": list(range(1, 10)), "ema_slow": list(range(20, 28))}},
             "exceed the bound"),
            ([1, 2], "mapping"),
        ],
    )  # fmt: skip
    def test_an_unacceptable_grid_is_refused_before_anything_runs(self, raw, match):
        with pytest.raises(tuning.TuningError, match=match):
            tuning.parse_grid(raw, SPEC)


class TestSplit:
    def test_the_in_sample_end_is_the_session_before_the_hold_out(self):
        assert tuning.in_sample_end(DAYS[:10], 0.40) == DAYS[5]
        assert tuning.in_sample_end(list(reversed(DAYS[:10])), 0.40) == DAYS[5]

    def test_the_tuning_hold_out_contains_the_recorded_runs_hold_out(self):
        """The recorded run holds out its last 30%; tuning reads only the first 60%."""
        days = [d.isoformat() for d in DAYS]
        trades = [_t(d, 1.0) for d in DAYS]
        recorded_oos = rr.default_oos_start(days, trades)
        assert tuning.in_sample_end(DAYS, tuning.DEFAULT_HOLDOUT).isoformat() < recorded_oos

    def test_too_few_sessions_to_split_is_refused(self):
        with pytest.raises(tuning.TuningError):
            tuning.in_sample_end(DAYS[:1], 0.4)


def _grid():
    return tuning.parse_grid({"parameters": {"ema_fast": [3, 5, 7, 9, 11]}, "min_trades": 2}, SPEC)


def _runner(net_by_fast, n=4):
    def run(values):
        r = net_by_fast[values["ema_fast"]]
        return [_t(DAYS[i], r) for i in range(n)]

    return run


class TestSelection:
    def test_a_plateau_beats_an_isolated_peak(self):
        # 3 is a lone spike between poor neighbours; 7, 9 and 11 form a stable region
        net = {3: 2.0, 5: -0.5, 7: 0.6, 9: 0.7, 11: 0.6}
        out = tuning.evaluate_grid(_grid(), _runner(net), DAYS, DAYS[30])
        assert out["best_single"]["values"] == {"ema_fast": 3}
        assert out["selected"]["values"] == {"ema_fast": 9}
        assert out["selected_is_not_the_single_best"] is True
        assert out["selected"]["region_worst_net_r"] == pytest.approx(0.6)
        assert out["best_single"]["region_worst_net_r"] == pytest.approx(-0.5)

    def test_combinations_below_the_trade_floor_are_unusable_and_never_selected(self):
        grid = tuning.parse_grid({"parameters": {"ema_fast": [3, 5]}, "min_trades": 5}, SPEC)
        out = tuning.evaluate_grid(grid, _runner({3: 1.0, 5: 1.0}, n=4), DAYS, DAYS[30])
        assert all(not r["usable"] for r in out["results"])
        assert out["selected"] is None and out["best_single"] is None

    def test_a_trade_after_the_in_sample_end_stops_the_tuning(self):
        def leaky(values):
            return [_t(DAYS[40], 1.0)]

        with pytest.raises(tuning.TuningError, match="after the in-sample end"):
            tuning.evaluate_grid(_grid(), leaky, DAYS, DAYS[30])

    def test_pbo_is_computed_over_every_combination(self):
        rng = np.random.default_rng(7)

        def noisy(values):
            return [_t(d, float(rng.normal())) for d in DAYS[:31]]

        out = tuning.evaluate_grid(_grid(), noisy, DAYS, DAYS[30])
        assert 0.0 <= out["pbo"]["value"] <= 1.0 and out["pbo"]["slices"] == 16

    def test_pbo_is_labelled_as_a_diagnostic_of_single_best_selection(self):
        rng = np.random.default_rng(3)
        m = rng.normal(size=(40, 4))
        out = tuning.pbo(m)
        assert out["role"] == "diagnostic" and "single best" in out["method"]
        assert out["distinct_combinations"] == 4

    def test_identical_combinations_are_ranked_once(self):
        """A parameter with no effect in-sample yields duplicate columns. Counting them as rivals
        pushes the winner's out-of-sample rank down and reports PBO 1.0 for no selection at all."""
        rng = np.random.default_rng(5)
        col = rng.normal(size=(40, 1))
        same = tuning.pbo(np.hstack([col, col, col]))
        assert same["value"] is None and same["distinct_combinations"] == 1
        other = rng.normal(size=(40, 1))
        dup = tuning.pbo(np.hstack([col, col, other]))
        assert dup["value"] == tuning.pbo(np.hstack([col, other]))["value"]
        assert dup["distinct_combinations"] == 2

    def test_the_session_matrix_is_zero_filled(self):
        m = tuning.session_matrix([[_t(DAYS[1], 1.5)], []], DAYS[:3])
        assert m.tolist() == [[0.0, 0.0], [1.5, 0.0], [0.0, 0.0]]


def _capped(trades, net_r):
    return tuning.Outcome(
        trades=trades, capped={"trades": len(trades), "net_r": net_r, "net_bps": net_r * 10}
    )


class TestPortfolioConstraints:
    """Ranking uses the live-caps primary pass (G5.2), the policy the recorded run is held to."""

    def test_capped_results_decide_the_ranking_when_every_combination_has_them(self):
        # Uncapped, 3 looks best; with portfolio caps applied 9 is the better strategy.
        uncapped = {3: 2.0, 5: 0.2, 7: 0.3, 9: 0.4, 11: 0.3}
        capped = {3: -0.5, 5: 0.1, 7: 0.5, 9: 0.6, 11: 0.5}

        def run(values):
            f = values["ema_fast"]
            return _capped([_t(DAYS[i], uncapped[f]) for i in range(4)], capped[f])

        out = tuning.evaluate_grid(_grid(), run, DAYS, DAYS[30])
        assert out["constraints"]["fully_constrained"] is True
        assert out["selected"]["values"] == {"ema_fast": 9}
        assert out["best_single"]["values"] == {"ema_fast": 9}
        row3 = out["results"][0]
        assert row3["net_r"] == pytest.approx(-0.5) and row3["uncapped_net_r"] == pytest.approx(2.0)

    def test_the_capped_trade_floor_decides_usability(self):
        grid = tuning.parse_grid({"parameters": {"ema_fast": [3, 5]}, "min_trades": 3}, SPEC)

        def run(values):
            trades = [_t(DAYS[i], 1.0) for i in range(5)]
            out = _capped(trades, 0.5)
            return tuning.Outcome(trades=trades, capped={**out.capped, "trades": 2})

        out = tuning.evaluate_grid(grid, run, DAYS, DAYS[30])
        assert all(not r["usable"] for r in out["results"])

    def test_one_unconstrained_combination_makes_the_whole_selection_unconstrained(self):
        def run(values):
            trades = [_t(DAYS[i], 0.3) for i in range(4)]
            if values["ema_fast"] == 7:
                return tuning.Outcome(trades=trades, unconstrained_reason="base replay mismatch")
            return _capped(trades, -1.0)

        out = tuning.evaluate_grid(_grid(), run, DAYS, DAYS[30])
        c = out["constraints"]
        assert c["fully_constrained"] is False
        assert c["unconstrained"] == [{"values": {"ema_fast": 7}, "reason": "base replay mismatch"}]
        # One basis for every row: the uncapped trade list, never a mix.
        assert [r["net_r"] for r in out["results"]] == pytest.approx([0.3] * 5)

    def test_a_plain_trade_list_is_an_unconstrained_outcome(self):
        out = tuning.evaluate_grid(
            _grid(), _runner({3: 1, 5: 1, 7: 1, 9: 1, 11: 1}), DAYS, DAYS[30]
        )
        assert out["constraints"]["fully_constrained"] is False

    def test_capped_outcome_reads_the_primary_pass_of_the_recorded_stage(self):
        evidence = {
            "state": "complete",
            "caps": {"max_open_positions": 1},
            "passes": [
                {
                    "max_open_positions": 1,
                    "primary": True,
                    "trades": 12,
                    "net_r": 0.21,
                    "net_bps": 3.0,
                },
                {
                    "max_open_positions": 2,
                    "primary": False,
                    "trades": 20,
                    "net_r": 0.1,
                    "net_bps": 1.0,
                },
            ],
        }
        o = tuning.capped_outcome([], evidence)
        assert o.capped == {"trades": 12, "net_r": 0.21, "net_bps": 3.0}
        assert o.unconstrained_reason is None

    @pytest.mark.parametrize(
        ("evidence", "reason"),
        [
            (None, "not run"),
            ({"state": "unsupported", "reason": "swing"}, "unsupported: swing"),
            (
                {"state": "base_replay_mismatch", "reason": "differs"},
                "base_replay_mismatch: differs",
            ),
            ({"state": "complete", "passes": []}, "no primary pass"),
        ],
    )
    def test_a_stage_that_did_not_complete_is_unconstrained_with_its_reason(self, evidence, reason):
        o = tuning.capped_outcome([], evidence)
        assert o.capped is None and reason in o.unconstrained_reason


class TestAgreementWithTheRecordedStage:
    """The tuning score of a combination is exactly what the recorded run's G5.2 stage reports
    for the same trades: one capped engine pass at the live slot count."""

    def test_the_score_equals_the_live_caps_primary_pass(self):
        from signal_engine.backtest import live_caps as lc
        from signal_engine.backtest.engine import Ctx, simulate
        from signal_engine.backtest.perturbation import trade_metrics
        from signal_engine.tests.test_live_caps import RUN, Fire, caps, session

        frames = {"A": session("2026-06-01", {4: "SL"}), "B": session("2026-06-01", {5: "TP"})}
        head = simulate(Ctx(frames["A"], "A"), Fire({2, 3}), None, RUN) + simulate(
            Ctx(frames["B"], "B"), Fire({2, 3}), None, RUN
        )
        limits = caps(max_open_positions=1)
        ev = lc.run_stage(
            strategy=Fire({2, 3}),
            params=None,
            load_frames=lambda syms: {s: frames[s] for s in syms},
            symbols=["A", "B"],
            run=RUN,
            cost_model=None,
            headline_trades=head,
            caps=limits,
        )
        o = tuning.capped_outcome(head, ev)
        contexts = {s: (Fire({2, 3}), Ctx(frames[s], s)) for s in frames}
        direct = trade_metrics(lc.run_capped(contexts, None, RUN, limits).trades)
        assert o.capped["trades"] == direct["trades"]
        assert o.capped["net_r"] == pytest.approx(direct["net_r"])
        # The caps matter here: the capped pass is not the uncapped trade list.
        assert o.capped["trades"] < len(head)
        assert o.caps["max_open_positions"] == 1


class TestRunTuning:
    """The real workflow on the shared idea fixture: frozen spec, published-data stub, ledger."""

    GRID = {"parameters": {"ema_fast": [5, 9], "ema_slow": [20, 30]}, "min_trades": 2}

    def _execute(self, seen):
        def execute(key, *, settings, end, dataset, run_truncation, run_live_caps):
            seen.append(
                {
                    "key": key,
                    "settings": settings,
                    "end": end,
                    "trunc": run_truncation,
                    "caps": run_live_caps,
                }
            )
            r = 0.3 if settings["parameter_overrides"]["ema_fast"] == 9 else -0.1
            stop = date.fromisoformat(end)
            days = [d for d in DAYS if d <= stop][:6]
            caps = {
                "state": "complete",
                "caps": {"max_open_positions": 1},
                "passes": [{"primary": True, "trades": 5, "net_r": r / 2, "net_bps": r * 5}],
            }
            return SimpleNamespace(trades=[_t(d, r) for d in days], live_caps=caps)

        return execute

    def _tune(self, root_, seen):
        return tuning.run_tuning(
            root_,
            "IDEA-009",
            self.GRID,
            now=NOW,
            execute=self._execute(seen),
            supported=SUPPORTED,
            git_state_fn=_git(CLEAN),
        )

    def test_every_combination_runs_in_sample_with_its_own_values(self, root):
        seen = []
        path = self._tune(root, seen)
        record = json.loads(path.read_text(encoding="utf-8"))
        assert len(seen) == 4 == record["combinations"]
        assert {s["end"] for s in seen} == {record["in_sample_end"]}
        assert all(s["trunc"] is False for s in seen)
        # The same G5.2 live-caps stage the recorded run uses, for every combination.
        assert all(s["caps"] is True for s in seen)
        assert record["constraints"]["fully_constrained"] is True
        assert record["constraints"]["caps"] == {"max_open_positions": 1}
        assert record["selected"]["net_r"] == pytest.approx(0.15)
        for s in seen:
            assert s["settings"]["parameters"] == {**SPEC, **s["settings"]["parameter_overrides"]}
        assert record["held_out_from"] > record["in_sample_end"]
        assert record["selected"]["values"]["ema_fast"] == 9
        assert record["dataset_version"] and record["code_commit"] == CLEAN["code_commit"]
        assert all(len(r["params_fingerprint"]) == 64 for r in record["results"])

    def test_every_combination_is_counted_in_the_trial_ledger(self, root):
        path = self._tune(root, [])
        folder = path.parents[2]
        entries = trials.load_trials(folder)
        assert [(e.kind, e.count) for e in entries] == [("external", 4)]
        assert path.parent.name in entries[0].reason

    def test_a_tuning_record_is_written_once_and_never_overwritten(self, root):
        first = self._tune(root, [])
        second = self._tune(root, [])
        assert first != second and first.read_text() != ""


class TestSeparationGuard:
    """A recorded run whose out-of-sample start overlaps a tuning period is not valid."""

    @staticmethod
    def _tuning_record(folder, is_end):
        out = folder / tuning.TUNING_DIR / "2026-10-13-001"
        out.mkdir(parents=True)
        (out / tuning.RECORD).write_text(json.dumps({"in_sample_end": is_end}))

    def test_an_overlap_is_a_validity_failure(self, tmp_path):
        self._tuning_record(tmp_path, "2026-03-10")
        failures = tuning.separation_failures(tmp_path, "2026-03-01")
        assert [f["check"] for f in failures] == ["tuning_separation"]

    def test_a_tuning_that_ended_before_the_hold_out_is_fine(self, tmp_path):
        self._tuning_record(tmp_path, "2026-02-27")
        assert tuning.separation_failures(tmp_path, "2026-03-01") == []

    def test_no_tuning_means_no_check(self, tmp_path):
        assert tuning.separation_failures(tmp_path, "2026-03-01") == []
        assert tuning.has_tuning(tmp_path) is False

    def test_a_recorded_run_after_an_overlapping_tuning_is_inconclusive(self, root):
        folder = next((root / "research" / "ideas").glob("idea-009-*"))
        self._tuning_record(folder, "2099-01-01")
        flat = json.loads(_record(root).read_text(encoding="utf-8"))
        assert flat["run_verdict"] == "INCONCLUSIVE"
        assert flat["promotion_decision"] != "READY_FOR_NEXT_STAGE"

    def test_a_recorded_run_after_a_clean_tuning_is_unaffected(self, root):
        folder = next((root / "research" / "ideas").glob("idea-009-*"))
        baseline = json.loads(_record(root).read_text(encoding="utf-8"))["run_verdict"]
        self._tuning_record(folder, "2000-01-01")
        flat = json.loads(_record(root).read_text(encoding="utf-8"))
        assert flat["run_verdict"] == baseline
