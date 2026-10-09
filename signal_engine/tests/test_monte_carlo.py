"""G5.1 Monte Carlo risk diagnostic (read-only, report only)."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import monte_carlo as mc

T0 = pd.Timestamp("2024-01-01 04:00", tz="UTC")  # 09:30 IST
COST_R = 0.1


def _trades(days, start=T0, cost=COST_R):
    """days: list of lists of net R, one inner list per session; trades share the first bars."""
    rows = []
    for d, rs in enumerate(days):
        for k, r in enumerate(rs):
            entry = start + pd.Timedelta(days=d, minutes=5 * k)
            rows.append(
                {
                    "symbol": f"S{k}",
                    "side": "BUY",
                    "entry_time": entry,
                    "exit_time": entry + pd.Timedelta(minutes=5),
                    "gross_r": r + cost,
                    "net_r": r,
                }
            )
    return pd.DataFrame(rows)


class TestDeterministicStats:
    def test_additive_known_path(self):
        a = mc.additive_stats(np.array([[1.0, -2.0, 3.0]]))
        assert a["final"][0] == 2.0 and a["max_dd"][0] == 2.0

    def test_all_winning_has_no_drawdown_and_no_ruin(self):
        p = np.full((1, 20), 1.0)
        assert mc.additive_stats(p)["max_dd"][0] == 0.0
        e = mc.equity_stats(p, 0.01)
        assert e["max_dd"][0] == 0.0 and not e["ruin"][0]
        assert e["final"][0] == pytest.approx(1.01**20)

    def test_all_losing_reaches_ruin_at_the_expected_step(self):
        e = mc.equity_stats(np.full((1, 5), -1.0), 0.1)
        assert e["ruin"][0] and e["min_equity"][0] == pytest.approx(0.9**5)
        # 0.9 and 0.81 are above 0.75; the third loss (0.729) crosses it
        assert not mc.equity_stats(np.full((1, 2), -1.0), 0.1)["ruin"][0]
        assert mc.equity_stats(np.full((1, 3), -1.0), 0.1)["ruin"][0]

    def test_zero_returns_are_flat_without_nan(self):
        p = np.zeros((2, 10))
        a, e = mc.additive_stats(p), mc.equity_stats(p, 0.01)
        assert (a["final"] == 0).all() and (a["max_dd"] == 0).all()
        assert (e["final"] == 1).all() and (e["max_dd"] == 0).all() and not e["ruin"].any()

    def test_ruin_boundary_counts_as_ruin(self):
        assert mc.equity_stats(np.array([[-1.0]]), 0.25)["ruin"][0]
        assert not mc.equity_stats(np.array([[-1.0]]), 0.2499)["ruin"][0]

    def test_drawdown_from_peak_is_not_ruin_from_start(self):
        e = mc.equity_stats(np.array([[2.0, -1.0]]), 0.5)  # equity 2.0 then 1.0
        assert e["max_dd"][0] == pytest.approx(0.5) and not e["ruin"][0]

    def test_equity_is_floored_at_zero_and_stays_there(self):
        e = mc.equity_stats(np.array([[-3.0, 5.0, 5.0]]), 0.5)
        assert e["final"][0] == 0.0 and e["ruin"][0]

    def test_compounding_matches_a_hand_product(self):
        r = np.array([[1.0, -2.0, 0.5, 3.0]])
        assert mc.equity_stats(r, 0.05)["final"][0] == pytest.approx(np.prod(1 + 0.05 * r[0]))

    def test_theta_is_configurable(self):
        r = np.array([[-1.0]])
        assert not mc.equity_stats(r, 0.1, theta=0.25)["ruin"][0]
        assert mc.equity_stats(r, 0.1, theta=0.10)["ruin"][0]


class TestSessions:
    def test_sessions_group_trades_by_ist_day(self):
        s = mc.Sessions(_trades([[1.0, -0.5], [2.0], [0.0, 0.0, 3.0]]))
        assert s.n_days == 3 and s.n_trades == 6
        assert s.day_r.tolist() == [0.5, 2.0, 3.0] and s.counts.tolist() == [2, 1, 3]

    def test_a_utc_evening_trade_belongs_to_the_next_ist_day(self):
        late = pd.Timestamp("2024-01-01 20:00", tz="UTC")  # 01:30 IST on 2 Jan
        t = pd.concat([_trades([[1.0]]), _trades([[2.0]], start=late)])
        assert mc.Sessions(t).n_days == 2

    def test_undefined_r_is_excluded_and_counted(self):
        t = _trades([[1.0, 2.0], [3.0]])
        t.loc[0, "net_r"] = np.nan
        s = mc.Sessions(t)
        assert s.excluded_undefined_r == 1 and s.n_trades == 2

    def test_input_row_order_does_not_matter(self):
        t = _trades([[1.0, 2.0], [3.0], [4.0, 5.0, 6.0]])
        a = mc.Sessions(t)
        b = mc.Sessions(t.sample(frac=1.0, random_state=1))
        assert a.day_r.tolist() == b.day_r.tolist()
        assert a.counts.tolist() == b.counts.tolist()

    def test_cost_multiplier_scales_the_cost_in_r(self):
        t = _trades([[1.0], [1.0]])
        assert mc.Sessions(t, 1.0).day_r.tolist() == [1.0, 1.0]
        assert mc.Sessions(t, 2.0).day_r == pytest.approx([1.0 - COST_R, 1.0 - COST_R])

    def test_missing_columns_and_no_defined_r_raise(self):
        with pytest.raises(mc.MonteCarloError):
            mc.Sessions(pd.DataFrame([{"symbol": "A"}]))
        t = _trades([[1.0]])
        t["net_r"] = np.nan
        with pytest.raises(mc.MonteCarloError):
            mc.Sessions(t)


class TestResampling:
    @staticmethod
    def _rng(seed=1):
        return np.random.default_rng(seed)

    def test_day_bootstrap_keeps_whole_sessions_together(self):
        # day A: trades +5 and -5 (session R 0); day B: +1 and +1 (session R 2)
        s = mc.Sessions(_trades([[5.0, -5.0], [1.0, 1.0]]))
        values = np.unique(np.concatenate(list(mc.day_paths(self._rng(), s, 300))))
        assert set(values.tolist()) <= {0.0, 2.0}

    def test_trade_bootstrap_splits_sessions_but_keeps_their_trade_counts(self):
        s = mc.Sessions(_trades([[5.0, -5.0], [1.0, 1.0]]))
        values = np.unique(np.concatenate(list(mc.trade_paths(self._rng(), s, 300))))
        assert not set(values.tolist()) <= {0.0, 2.0}
        ones = mc.Sessions(_trades([[1.0, 1.0], [1.0], [1.0, 1.0, 1.0]]))
        for block in mc.trade_paths(self._rng(), ones, 50):
            assert (block == ones.counts).all()

    def test_block_bootstrap_keeps_consecutive_runs(self):
        s = mc.Sessions(_trades([[float(i)] for i in range(23)]))
        length = 5
        for block in mc.block_paths(self._rng(), s, 20, length):
            assert block.shape == (20, 23)
            for row in block:
                for i in range(1, 23):
                    if i % length:
                        assert (row[i] - row[i - 1]) % 23 == 1

    def test_block_longer_than_the_sample_still_works(self):
        s = mc.Sessions(_trades([[1.0], [2.0], [3.0]]))
        assert next(mc.block_paths(self._rng(), s, 10, 20)).shape == (10, 3)

    def test_paths_have_the_observed_length(self):
        s = mc.Sessions(_trades([[1.0]] * 7))
        assert next(mc.day_paths(self._rng(), s, 5)).shape == (5, 7)
        assert next(mc.trade_paths(self._rng(), s, 5)).shape == (5, 7)

    def test_same_seed_same_paths_different_seed_different_paths(self):
        s = mc.Sessions(_trades([[float(i)] for i in range(30)]))
        a = next(mc.day_paths(self._rng(1), s, 5))
        b = next(mc.day_paths(self._rng(1), s, 5))
        c = next(mc.day_paths(self._rng(2), s, 5))
        assert (a == b).all() and not (a == c).all()


class TestReport:
    DAYS = [[float(x)] for x in np.random.default_rng(0).normal(-0.3, 1.0, 120)]

    def _report(self, days=None, **kw):
        kw.setdefault("n_paths", 200)
        return mc.build_report(_trades(days or self.DAYS), **kw)

    def test_reproducible_for_a_fixed_seed_and_different_for_another(self):
        a, b = self._report(seed=5), self._report(seed=5)
        assert a == b
        assert self._report(seed=6)["methods"]["day"] != a["methods"]["day"]

    def test_all_methods_and_fractions_are_present(self):
        r = self._report(days=[[1.0]] * 3 + [[-1.0]] * 3, n_paths=20)
        assert {"day", "trade", "block_5", "block_20", "day_cost_1.5x", "day_cost_2x"} <= set(
            r["methods"]
        )
        assert set(r["methods"]["day"]["c2_account"]) == {"0.0025", "0.005", "0.01"}
        assert r["methods"]["day"]["c2_account"]["0.01"]["deployed_reference"] is True
        assert r["methods"]["day"]["c2_account"]["0.005"]["deployed_reference"] is False

    def test_ruin_definition_and_caveats_are_recorded(self):
        r = self._report()
        assert "<= (1 - theta)" in r["definitions"]["ruin"]
        assert "boundary counts" in r["definitions"]["ruin"]
        assert any("exchangeable" in x for x in r["limitations"])
        assert any("caps are not applied" in x for x in r["limitations"])
        assert r["implied_exposure"]["caps_applied"] is False

    def test_losing_sample_gives_negative_finals_and_ruin(self):
        d = self._report(days=[[-1.0]] * 60, n_paths=100)["methods"]["day"]
        assert d["c1_normalised_r"]["p_final_negative"]["p"] == 1.0
        assert d["c2_account"]["0.01"]["p_ruin"]["p"] == 1.0

    def test_winning_sample_has_no_ruin(self):
        r = self._report(days=[[1.0]] * 60, n_paths=100)
        for method in ("day", "trade", "block_5"):
            m = r["methods"][method]
            assert m["c1_normalised_r"]["p_final_negative"]["p"] == 0.0
            assert m["c2_account"]["0.01"]["p_ruin"]["p"] == 0.0

    def test_higher_cost_lowers_the_median_result(self):
        r = self._report(days=[[0.2, 0.1] for _ in range(80)], n_paths=300)
        med = {k: r["methods"][k]["c1_normalised_r"]["final_r"]["p50"] for k in r["methods"]}
        assert med["day"] > med["day_cost_1.5x"] > med["day_cost_2x"]

    def test_observed_path_matches_a_hand_calculation(self):
        o = mc.build_report(_trades([[1.0, -3.0], [2.0], [-1.0]]), n_paths=20)["observed"]
        assert o["sessions"] == 3 and o["trades"] == 4
        assert o["sum_net_r"] == pytest.approx(-1.0)
        assert o["worst_session_r"] == pytest.approx(-2.0)
        assert o["best_session_r"] == pytest.approx(2.0)
        assert o["c2_account"]["0.01"]["final_equity_multiple"] == pytest.approx(
            (1 - 0.02) * (1 + 0.02) * (1 - 0.01), abs=1e-6
        )

    def test_observed_first_ruin_session_is_reported(self):
        r = mc.build_report(_trades([[-1.0]] * 6), n_paths=20, fractions=(0.1,))
        o = r["observed"]["c2_account"]["0.1"]
        assert o["ruined"] and o["first_session_at_or_below_ruin_level"] == 3

    def test_implied_exposure_counts_simultaneous_positions(self):
        t = _trades([[1.0, 1.0, 1.0], [1.0]])
        same_bar = t["entry_time"].dt.normalize() + pd.Timedelta(hours=4)  # three entries, one bar
        t["entry_time"], t["exit_time"] = same_bar, same_bar + pd.Timedelta(minutes=5)
        r = mc.build_report(t, n_paths=20, fractions=(0.01,))
        e = r["implied_exposure"]
        assert e["peak_concurrent_positions"] == 3
        assert e["implied_peak_simultaneous_risk_fraction"]["0.01"] == pytest.approx(
            e["peak_concurrent_positions"] * 0.01
        )

    def test_recent_method_uses_only_recent_sessions_and_is_skipped_without_them(self):
        old = _trades([[1.0]] * 10, start=pd.Timestamp("2020-01-01 04:00", tz="UTC"))
        assert "recent_day" not in mc.build_report(old, n_paths=20)["methods"]
        new = _trades([[-1.0]] * 10, start=pd.Timestamp("2024-06-03 04:00", tz="UTC"))
        m = mc.build_report(pd.concat([old, new]), n_paths=50)["methods"]["recent_day"]
        assert m["sessions"] == 10 and m["c1_normalised_r"]["p_final_negative"]["p"] == 1.0

    def test_monte_carlo_standard_error_formula(self):
        p = mc._prob(np.array([True, False] * 5000))
        assert p["p"] == 0.5 and p["mcse"] == pytest.approx(0.005)

    def test_degenerate_probabilities_carry_a_rule_of_three_bound(self):
        all_ruined = mc._prob(np.ones(10000, bool))
        assert all_ruined["mcse"] == 0.0 and all_ruined["bound95_other_outcome_at_most"] == pytest.approx(0.0003)
        assert mc._prob(np.zeros(10000, bool))["bound95_other_outcome_at_most"] == pytest.approx(0.0003)
        assert "bound95_other_outcome_at_most" not in mc._prob(np.array([True, False] * 50))

    def test_reference_values_are_informational_and_no_gate_key_exists(self):
        ref = self._report(n_paths=50)["methods"]["day"]["c2_account"]["0.01"]["reference_only"]
        assert "informational" in ref["note"] and "no gate is changed" in ref["note"]


class TestEvidence:
    @staticmethod
    def _run(tmp_path):
        run = tmp_path / "run"
        (run / "evidence").mkdir(parents=True)
        _trades([[1.0, -2.0], [0.5], [-1.0, 1.0]] * 5).to_parquet(run / "evidence" / "trades.parquet")
        return run

    def test_sealed_payload_is_valid_deterministic_strict_json_and_read_only(self, tmp_path):
        run = self._run(tmp_path)
        before = sorted(p.name for p in run.rglob("*"))
        ev1 = mc.evidence_for_run(run, n_paths=50)
        ev2 = mc.evidence_for_run(run, n_paths=50)
        assert ev1 == ev2 and mc.validate_evidence(ev1) == []
        assert ev1["report_only"] and ev1["diagnostic_not_trials"]
        json.dumps(ev1, allow_nan=False)
        assert sorted(p.name for p in run.rglob("*")) == before

    def test_tampering_or_a_missing_ruin_definition_is_caught(self, tmp_path):
        ev = mc.evidence_for_run(self._run(tmp_path), n_paths=20)
        report = json.loads(json.dumps(ev["report"]))
        ev["report"]["parameters"]["paths"] = 1
        assert any("content_sha256" in p for p in mc.validate_evidence(ev))
        del report["definitions"]["ruin"]
        assert any("ruin definition" in p for p in mc.validate_evidence(mc.seal_report(report, {})))
