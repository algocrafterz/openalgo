"""Evaluation profile: a run scored gate by gate against STRATEGY-ACCEPTANCE-CRITERIA.md.

The profile is a list of rows grouped by dimension, never a single score, so a failure can
always be traced to the gate and the measurement behind it. Gates the engine cannot compute
are listed as not_evaluated, not left out.
"""

import pandas as pd
import pytest

from signal_engine.backtest import evaluation as ev
from signal_engine.backtest.tearsheet import build_run_record
from signal_engine.backtest.types import Trade
from signal_engine.research import build_registry as br

DIMENSIONS = (
    "edge",
    "robustness",
    "risk",
    "stability",
    "cost_sensitivity",
    "regime_robustness",
    "parameter_sensitivity",
    "statistical_confidence",
    "complexity",
)


def _trade(day, r_net=0.3, r_gross=0.6, symbol="A.NS", direction=1, exit_hm="14:30", risk=1.0):
    return Trade(
        symbol=symbol,
        day=day,
        direction=direction,
        tag="t",
        entry_time=pd.Timestamp(f"{day} 09:20"),
        entry=100.0,
        signal_price=100.0,
        sl=99.0,
        tp=0.0,
        risk=risk,
        exit_time=pd.Timestamp(f"{day} {exit_hm}"),
        reason="tp",
        r_gross=r_gross,
        r_net=r_net,
    )


def _days(n, start="2024-01-01"):
    return [d.date().isoformat() for d in pd.bdate_range(start, periods=n)]


def _book(n=300, r_net=0.3, r_gross=0.6, symbols=20, **kw):
    days = _days(n)
    return [
        _trade(d, r_net=r_net, r_gross=r_gross, symbol=f"S{i % symbols}.NS", **kw)
        for i, d in enumerate(days)
    ]


def _evaluate(trades, **kw):
    args = {
        "metrics": {"trades": len(trades), "t_stat": 3.0, "gross_bps": 60.0, "net_bps": 30.0},
        "n_trials": 1,
        "horizon": "intraday",
        "cost_bps": 16.0,
        "oos_start": None,
        "position_caps": None,
        "params": None,
    }
    args.update(kw)
    return ev.evaluate(trades, **args)


def _row(profile, gate):
    rows = [r for d in profile["dimensions"] for r in d["rows"] if r["gate"] == gate]
    assert len(rows) == 1, f"{gate} appears {len(rows)} times"
    return rows[0]


class TestStructure:
    def test_all_nine_dimensions_present_in_order(self):
        names = [d["name"] for d in _evaluate(_book())["dimensions"]]
        assert names[:9] == list(DIMENSIONS)

    def test_every_acceptance_gate_is_listed_exactly_once(self):
        profile = _evaluate(_book())
        ids = [r["gate"] for d in profile["dimensions"] for r in d["rows"]]
        assert len(ids) == len(set(ids))
        for gate in ("G0.1", "G1.2", "G2.2", "G3.2", "G4.5", "G5.1", "G6.9", "G7.1", "G9.4"):
            assert gate in ids

    def test_rows_carry_the_documented_keys(self):
        for d in _evaluate(_book())["dimensions"]:
            for r in d["rows"]:
                assert set(r) == {
                    "gate",
                    "title",
                    "status",
                    "hard",
                    "value",
                    "threshold",
                    "evidence",
                }
                assert r["status"] in ev.STATUSES

    def test_no_composite_score(self):
        profile = _evaluate(_book())
        assert "score" not in profile
        assert "score" not in profile["summary"]

    def test_uncomputable_gates_are_not_evaluated_with_a_reason(self):
        row = _row(_evaluate(_book()), "G2.3")
        assert row["status"] == "not_evaluated"
        assert row["evidence"]

    def test_summary_counts_add_up(self):
        profile = _evaluate(_book())
        rows = [r for d in profile["dimensions"] for r in d["rows"]]
        s = profile["summary"]
        assert sum(s["counts"].values()) == len(rows)
        assert s["hard_failed"] == [r["gate"] for r in rows if r["hard"] and r["status"] == "fail"]

    def test_profile_is_json_safe(self):
        import json

        json.dumps(_evaluate(_book()), allow_nan=False)

    def test_empty_trade_list_still_returns_a_profile(self):
        profile = _evaluate([], metrics={"trades": 0})
        assert _row(profile, "G1.2")["status"] == "not_evaluated"
        assert _row(profile, "G2.1")["status"] == "fail"


class TestEconomics:
    def test_g12_passes_at_005r(self):
        assert _row(_evaluate(_book(r_net=0.05)), "G1.2")["status"] == "pass"

    def test_g12_fails_below_005r(self):
        row = _row(_evaluate(_book(r_net=0.04)), "G1.2")
        assert row["status"] == "fail" and row["hard"] is True

    def test_g14_reports_gross_over_cost(self):
        row = _row(_evaluate(_book(), metrics={"trades": 300, "gross_bps": 24.0}), "G1.4")
        assert row["status"] == "report"
        assert row["value"] == pytest.approx(1.5)

    def test_g15_median_cost_in_r(self):
        ok = _row(_evaluate(_book(r_gross=0.6, r_net=0.3)), "G1.5")
        bad = _row(_evaluate(_book(r_gross=0.6, r_net=0.1)), "G1.5")
        assert ok["status"] == "pass" and ok["value"] == pytest.approx(0.3)
        assert bad["status"] == "fail"


class TestSignificance:
    def test_g21_needs_200_trades(self):
        assert _row(_evaluate(_book(200)), "G2.1")["status"] == "pass"
        assert _row(_evaluate(_book(199)), "G2.1")["status"] == "fail"

    def test_g22_threshold_is_2_5_up_to_ten_trials(self):
        assert ev.t_threshold(1) == 2.5
        assert ev.t_threshold(10) == 2.5

    def test_g22_threshold_rises_with_trials(self):
        assert 2.8 < ev.t_threshold(20) < 3.0
        assert 2.9 < ev.t_threshold(30) < 3.1
        assert ev.t_threshold(50) > ev.t_threshold(30)

    def test_g22_pass_and_fail(self):
        metrics = {"trades": 300, "t_stat": 2.5}
        assert _row(_evaluate(_book(), metrics=metrics), "G2.2")["status"] == "pass"
        metrics = {"trades": 300, "t_stat": 2.49}
        assert _row(_evaluate(_book(), metrics=metrics), "G2.2")["status"] == "fail"

    def test_g22_uses_trial_count(self):
        metrics = {"trades": 300, "t_stat": 2.6}
        row = _row(_evaluate(_book(), metrics=metrics, n_trials=30), "G2.2")
        assert row["status"] == "fail"

    def test_g22_bar_is_the_larger_of_criteria_level_and_hurdle(self):
        from signal_engine.backtest.metrics import hurdle_t

        n = 50
        assert hurdle_t(n) > ev.t_threshold(n)  # the Sidak hurdle is the binding one here
        between = (ev.t_threshold(n) + hurdle_t(n)) / 2
        row = _row(
            _evaluate(_book(), metrics={"trades": 300, "t_stat": between}, n_trials=n), "G2.2"
        )
        assert row["status"] == "fail"
        assert f"{hurdle_t(n):.2f}" in row["evidence"]

    def test_g22_without_t_stat_is_not_evaluated(self):
        row = _row(_evaluate(_book(), metrics={"trades": 300}), "G2.2")
        assert row["status"] == "not_evaluated"


class TestOutOfSample:
    def _split(self, is_r, oos_r, n=300):
        days = _days(n)
        cut = days[int(n * 0.7)]
        trades = [_trade(d, r_net=is_r if d < cut else oos_r, r_gross=1.0) for d in days]
        return trades, cut

    def test_g32_passes_when_oos_holds_up(self):
        trades, cut = self._split(0.3, 0.2)
        assert _row(_evaluate(trades, oos_start=cut), "G3.2")["status"] == "pass"

    def test_g32_fails_when_oos_below_40_percent_of_in_sample(self):
        trades, cut = self._split(0.5, 0.1)
        assert _row(_evaluate(trades, oos_start=cut), "G3.2")["status"] == "fail"

    def test_g32_fails_when_oos_negative(self):
        trades, cut = self._split(0.3, -0.1)
        assert _row(_evaluate(trades, oos_start=cut), "G3.2")["status"] == "fail"

    def test_g32_without_split_is_not_evaluated(self):
        assert _row(_evaluate(_book()), "G3.2")["status"] == "not_evaluated"

    def test_g31_reports_holdout_fraction(self):
        trades, cut = self._split(0.3, 0.3)
        row = _row(_evaluate(trades, oos_start=cut), "G3.1")
        assert row["status"] == "report"
        assert row["value"] == pytest.approx(0.3, abs=0.02)


class TestRobustness:
    def test_g44_positive_years_and_worst_year(self):
        good = []
        for year, r in ((2022, 0.2), (2023, 0.1), (2024, -0.1)):
            good += [_trade(d, r_net=r) for d in _days(60, f"{year}-01-01")]
        assert _row(_evaluate(good), "G4.4")["status"] == "pass"

    def test_g44_fails_on_deep_worst_year(self):
        trades = []
        for year, r in ((2022, 0.3), (2023, 0.3), (2024, -0.4)):
            trades += [_trade(d, r_net=r) for d in _days(60, f"{year}-01-01")]
        row = _row(_evaluate(trades), "G4.4")
        assert row["status"] == "fail" and row["hard"] is True

    def test_g44_fails_when_too_few_years_positive(self):
        trades = []
        for year, r in ((2022, 0.3), (2023, -0.1), (2024, -0.1)):
            trades += [_trade(d, r_net=r) for d in _days(60, f"{year}-01-01")]
        assert _row(_evaluate(trades), "G4.4")["status"] == "fail"

    def test_g45_passes_when_profit_is_spread(self):
        assert _row(_evaluate(_book(300, symbols=30)), "G4.5")["status"] == "pass"

    def test_g45_fails_when_top_ten_dominate(self):
        days = _days(300)
        trades = [
            _trade(d, r_net=1.0 if i % 30 < 10 else -0.01, symbol=f"S{i % 30}.NS")
            for i, d in enumerate(days)
        ]
        row = _row(_evaluate(trades), "G4.5")
        assert row["status"] == "fail"

    def test_g45_fails_when_the_book_loses_money(self):
        assert _row(_evaluate(_book(r_net=-0.2)), "G4.5")["status"] == "fail"

    def test_g46_reports_direction_split(self):
        days = _days(100)
        trades = [_trade(d, direction=1 if i % 4 else -1) for i, d in enumerate(days)]
        row = _row(_evaluate(trades), "G4.6")
        assert row["status"] == "report"
        assert "75 long" in row["evidence"] and "25 short" in row["evidence"]

    def test_g46_declares_one_sided(self):
        assert "one-sided" in _row(_evaluate(_book()), "G4.6")["evidence"]


class TestHorizon:
    def test_g69_passes_before_1500(self):
        assert _row(_evaluate(_book(exit_hm="14:50")), "G6.9")["status"] == "pass"

    def test_g69_fails_at_or_after_1500(self):
        assert _row(_evaluate(_book(exit_hm="15:00")), "G6.9")["status"] == "fail"

    def test_swing_marks_intraday_only_gates_not_applicable(self):
        profile = _evaluate(_book(exit_hm="00:00"), horizon="swing")
        for gate in ("G6.1", "G6.9", "G6.8"):
            assert _row(profile, gate)["status"] == "not_applicable"

    def test_swing_still_checks_what_is_not_about_the_intraday_session(self):
        """Charge-schedule age and the SEBI framework apply to every horizon; only the MIS
        session rules (G6.1, G6.8, G6.9) and the stop-order and ledger checks are intraday-only."""
        profile = _evaluate(_book(exit_hm="00:00"), horizon="swing")
        assert _row(profile, "G6.7")["status"] != "not_applicable"
        assert _row(profile, "G6.6")["status"] == "not_evaluated"

    def test_unknown_horizon_is_rejected(self):
        with pytest.raises(ValueError, match="horizon"):
            _evaluate(_book(), horizon="scalp")


class TestCapsAndComplexity:
    def test_g52_shows_each_cap_but_stays_not_evaluated(self):
        # A generic slot sweep applies none of the live limits, so it must not read as a
        # non-blocking report (owner decision 2026-10-09).
        caps = [
            {"max_positions": 3, "trades": 100, "gross_bps": 10.0, "net_bps": 2.0},
            {"max_positions": 5, "trades": 150, "gross_bps": 9.0, "net_bps": -1.0},
        ]
        row = _row(_evaluate(_book(), position_caps=caps), "G5.2")
        assert row["status"] == "not_evaluated"
        assert "3 slots" in row["evidence"] and "5 slots" in row["evidence"]

    def test_g52_without_caps_is_not_evaluated(self):
        assert _row(_evaluate(_book()), "G5.2")["status"] == "not_evaluated"

    def test_parameter_count_is_reported_under_complexity(self):
        profile = _evaluate(_book(), params={"strategy": {"a": 1, "b": 2.5, "c": "x"}})
        row = _row(profile, "X.1")
        assert row["value"] == 3 and row["status"] == "report"


class TestWiring:
    def _record(self, trades, **kw):
        return build_run_record(
            trades,
            run_id="2026-09-30-x-v1",
            run_date="2026-09-30",
            code_version="abc",
            data={"source": "historify", "as_of": "2026-09-30", "universe": "u", "bar_size": "5m"},
            period={"start": "2024-01-01", "end": "2025-06-01"},
            costs={"model": "flat-round-trip-bps", "round_trip_bps": 16.0},
            **kw,
        )

    def test_build_run_record_attaches_a_valid_evaluation(self):
        record = self._record(_book())
        assert record["evaluation"]["dimensions"]
        assert br._validate_run(record) == []

    def test_horizon_flows_through(self):
        record = self._record(_book(exit_hm="00:00"), horizon="swing")
        assert _row(record["evaluation"], "G6.9")["status"] == "not_applicable"

    def test_evaluation_is_optional_for_old_records(self):
        record = self._record(_book())
        del record["evaluation"]
        assert br._validate_run(record) == []

    @pytest.mark.parametrize(
        "bad",
        [
            "x",
            {"dimensions": "x"},
            {
                "dimensions": [
                    {"name": "edge", "rows": [{"gate": "G1.2", "status": "maybe", "hard": True}]}
                ]
            },
            {"dimensions": [{"name": "edge", "rows": [{"status": "pass", "hard": True}]}]},
            {"dimensions": [{"rows": []}]},
        ],
    )
    def test_malformed_evaluation_is_rejected(self, bad):
        record = self._record(_book())
        record["evaluation"] = bad
        assert any("evaluation" in p for p in br._validate_run(record))

    def test_registry_json_keeps_only_the_evaluation_summary(self):
        record = self._record(_book())
        slim = br._slim({"runs": [record]})["runs"][0]
        assert "evaluation" not in slim
        assert slim["evaluation_summary"] == record["evaluation"]["summary"]


class TestRegimeDispersion:
    """X.2 reports how many market regimes the edge is positive in, per dimension."""

    def _block(self, trend, vol):
        def rows(spec):
            return [
                {"regime": n, "trades": t, "net_r": r, "low_sample": t < 30} for n, t, r in spec
            ]

        return {"version": 1, "trend": rows(trend), "vol": rows(vol), "unclassified_trades": 0}

    def _x2(self, block):
        return _row(_evaluate(_book(), regimes=block), "X.2")

    def test_lives_under_regime_robustness(self):
        profile = _evaluate(_book(), regimes=self._block([("up", 100, 0.1)], []))
        dim = next(d for d in profile["dimensions"] if d["name"] == "regime_robustness")
        assert "X.2" in [r["gate"] for r in dim["rows"]]

    def test_not_evaluated_without_a_block(self):
        row = _row(_evaluate(_book()), "X.2")
        assert row["status"] == "not_evaluated"

    def test_positive_everywhere_passes(self):
        row = self._x2(
            self._block(
                [("up", 100, 0.2), ("down", 100, 0.1), ("range", 100, 0.1)],
                [("high", 100, 0.2), ("low", 100, 0.1)],
            )
        )
        assert row["status"] == "pass"

    def test_edge_in_one_regime_only_is_flagged(self):
        row = self._x2(
            self._block(
                [("up", 100, 0.4), ("down", 100, -0.2), ("range", 100, -0.1)],
                [("high", 100, 0.2), ("low", 100, 0.1)],
            )
        )
        assert row["status"] == "fail"
        assert "trend" in row["evidence"]
        assert row["hard"] is False

    def test_two_of_three_positive_fails_under_the_calibrated_rule(self):
        # docs/REGIME-GATE-CALIBRATION.md: ">= 2 positive" false-passed 35% of zero-edge
        # books, so the heuristic requires every sampled regime to be net positive.
        row = self._x2(
            self._block(
                [("up", 100, 0.4), ("down", 100, 0.2), ("range", 100, -0.1)],
                [("high", 100, 0.2), ("low", 100, 0.1)],
            )
        )
        assert row["status"] == "fail"
        assert "range" in row["evidence"]

    def test_is_labelled_a_calibrated_heuristic(self):
        row = self._x2(self._block([("up", 100, 0.2), ("down", 100, 0.1)], []))
        assert "heuristic" in row["threshold"].lower()
        assert ev.REGIME_RULE == "all"

    def test_low_sample_regimes_are_not_counted(self):
        row = self._x2(
            self._block(
                [("up", 100, 0.4), ("down", 5, -0.9)],
                [("high", 100, 0.2)],
            )
        )
        assert row["status"] != "fail"

    def test_too_few_sampled_regimes_is_reported_not_judged(self):
        row = self._x2(self._block([("up", 100, 0.4)], [("high", 100, 0.2)]))
        assert row["status"] == "report"

    def test_g44_evidence_no_longer_claims_terciles_are_missing(self):
        row = _row(_evaluate(_book()), "G4.4")
        assert "terciles" not in row["evidence"]


class TestSymbolHoldout:
    def test_uniform_edge_passes(self):
        row = _row(_evaluate(_book(n=400, symbols=20)), "G3.4")
        assert row["status"] == "pass"

    def test_edge_only_in_one_half_fails(self):
        trades = _book(n=400, symbols=20)
        for t in trades:
            half = ev.symbol_half(t.symbol)
            t.r_net = 0.4 if half == 0 else -0.3
        row = _row(_evaluate(trades), "G3.4")
        assert row["status"] == "fail"

    def test_too_few_symbols_not_evaluated(self):
        row = _row(_evaluate(_book(n=100, symbols=3)), "G3.4")
        assert row["status"] == "not_evaluated"

    def test_split_is_deterministic_and_balanced(self):
        halves = [ev.symbol_half(f"S{i}.NS") for i in range(200)]
        assert halves == [ev.symbol_half(f"S{i}.NS") for i in range(200)]
        assert 70 < sum(halves) < 130


class TestRegimeActivation:
    def _block(self, trend_rows):
        return {"trend": trend_rows, "vol": [], "unclassified_trades": 0}

    def _r(self, name, net, low=False):
        return {"regime": name, "trades": 200, "net_r": net, "low_sample": low}

    def test_reports_enable_and_disable_sets_as_hypothesis(self):
        block = self._block([self._r("up", 0.2), self._r("down", -0.2), self._r("flat", 0.1)])
        row = _row(_evaluate(_book(), regimes=block), "X.3")
        assert row["status"] == "report"
        assert "trend: enable up, flat; disable down" in row["evidence"]
        assert "out-of-sample" in row["evidence"]

    def test_all_positive_means_no_conditioning_needed(self):
        block = self._block([self._r("up", 0.2), self._r("down", 0.1)])
        row = _row(_evaluate(_book(), regimes=block), "X.3")
        assert "no regime condition needed" in row["evidence"]

    def test_no_regime_block_not_evaluated(self):
        assert _row(_evaluate(_book()), "X.3")["status"] == "not_evaluated"


class TestEdgeOverRandomEntry:
    """X.4: the matched random-entry baseline is evidence, so a row reads it. It is a diagnostic
    and never decides: market drift alone can make a long-only book profitable."""

    BASE = {"method": "m", "trades": 500, "gross_bps": 5.0, "net_bps": -4.0,
            "trade_net_bps": 6.0, "excess_net_bps": 10.0, "excess_t_stat": 3.1}

    def test_without_a_baseline_it_is_not_evaluated(self):
        assert _row(_evaluate(_book()), "X.4")["status"] == "not_evaluated"

    def test_with_a_baseline_it_reports_the_excess_and_its_t(self):
        row = _row(_evaluate(_book(), baseline=self.BASE), "X.4")
        assert row["status"] == "report"
        assert "10.0" in row["evidence"] and "3.1" in row["evidence"]

    def test_an_edge_that_is_no_better_than_random_entry_is_said_plainly(self):
        weak = {**self.BASE, "excess_net_bps": 0.4, "excess_t_stat": 0.3}
        row = _row(_evaluate(_book(), baseline=weak), "X.4")
        assert row["status"] == "report"
        assert "does not clear" in row["evidence"]

    def test_it_is_a_diagnostic_and_never_blocks(self):
        from signal_engine.backtest import promotion

        assert "X.4" in promotion.DIAGNOSTIC_GATES
