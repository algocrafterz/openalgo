"""Market benchmark for a recorded run (read-only diagnostic on trades and 5-minute bars)."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import market_benchmark as mb

T0 = pd.Timestamp("2024-01-01 04:00", tz="UTC")


def _bars(opens, closes, start=T0):
    idx = pd.date_range(start, periods=len(opens), freq="5min")
    o = np.asarray(opens, float)
    c = np.asarray(closes, float)
    return pd.DataFrame(
        {"Open": o, "High": np.maximum(o, c), "Low": np.minimum(o, c), "Close": c}, index=idx
    )


def _market(rets, n=40, start=T0):
    """A market frame with a given per-bar return and n symbols behind each bar."""
    idx = pd.date_range(start, periods=len(rets), freq="5min")
    return pd.DataFrame({"n": float(n), "ret": rets}, index=idx.astype("int64").to_numpy())


def _trades(rows):
    recs = []
    for side, a, b, gross, cost, start in rows:
        recs.append(
            {
                "symbol": "AAA",
                "side": side,
                "entry_time": start + pd.Timedelta(minutes=5 * a),
                "exit_time": start + pd.Timedelta(minutes=5 * b),
                "gross_bps": gross,
                "cost_bps": cost,
                "net_bps": gross - cost,
            }
        )
    return pd.DataFrame(recs)


def _day_rows(n_days=40, side="BUY", gross=5.0, cost=10.0):
    """One same-bar trade per session."""
    return [(side, 0, 0, gross, cost, T0 + pd.Timedelta(days=k)) for k in range(n_days)]


def _day_market(trades, ret):
    """A market frame holding one bar, the trade's entry bar, per session."""
    idx = [pd.Timestamp(t).value for t in trades["entry_time"]]
    return pd.DataFrame({"n": 40.0, "ret": ret}, index=sorted(idx))


class TestMarketBarReturns:
    def test_equal_weight_mean_across_symbols(self):
        bars = {f"S{i}": _bars([100, 100], [100 * (1 + r), 100]) for i, r in enumerate([0.01, 0.03])}
        m = mb.market_bar_returns(bars, min_symbols=2)
        assert m["ret"].iloc[0] == pytest.approx(0.02) and m["n"].iloc[0] == 2

    def test_thin_bars_are_blanked(self):
        bars = {"S0": _bars([100], [101]), "S1": _bars([100], [102])}
        m = mb.market_bar_returns(bars, min_symbols=3)
        assert m["ret"].isna().all() and m["n"].iloc[0] == 2

    def test_symbol_missing_a_bar_is_averaged_over_the_rest(self):
        bars = {"S0": _bars([100, 100], [101, 100]), "S1": _bars([100], [103])}
        m = mb.market_bar_returns(bars, min_symbols=1)
        assert m["n"].tolist() == [2, 1]
        assert m["ret"].iloc[0] == pytest.approx(0.02)

    def test_non_positive_open_is_ignored(self):
        m = mb.market_bar_returns({"S0": _bars([0.0, 100], [1.0, 101])}, min_symbols=1)
        assert len(m) == 1 and m["ret"].iloc[0] == pytest.approx(0.01)


class TestIntervalReturns:
    def test_compounds_the_bars_of_the_window_inclusive(self):
        mk = _market([0.01, 0.02, -0.01, 0.0])
        out = mb.interval_returns(_trades([("BUY", 1, 2, 0, 0, T0)]), mk)
        assert out["status"].iloc[0] == "ok"
        assert out["market_ret"].iloc[0] == pytest.approx(1.02 * 0.99 - 1)

    def test_excluding_the_exit_bar(self):
        mk = _market([0.01, 0.02, -0.01, 0.0])
        out = mb.interval_returns(_trades([("BUY", 1, 2, 0, 0, T0)]), mk, include_exit_bar=False)
        assert out["market_ret"].iloc[0] == pytest.approx(0.02)

    def test_same_bar_trade_excluding_exit_bar_is_zero_not_missing(self):
        mk = _market([0.01, 0.02])
        out = mb.interval_returns(_trades([("BUY", 1, 1, 0, 0, T0)]), mk, include_exit_bar=False)
        assert out["status"].iloc[0] == "ok" and out["market_ret"].iloc[0] == 0.0

    def test_missing_bar_and_thin_bar_are_counted_not_averaged(self):
        mk = _market([0.01, np.nan, 0.01, 0.01])
        t = _trades([("BUY", 0, 2, 0, 0, T0), ("BUY", 2, 3, 0, 0, T0), ("BUY", 0, 9, 0, 0, T0)])
        out = mb.interval_returns(t, mk)
        assert out["status"].tolist() == ["thin_market_bar", "ok", "bar_missing"]

    def test_gap_in_the_market_series(self):
        mk = _market([0.01] * 4)
        gap = mk.drop(mk.index[2])
        out = mb.interval_returns(_trades([("BUY", 0, 3, 0, 0, T0)]), gap)
        assert out["status"].iloc[0] == "path_gap"


class TestCompare:
    def test_direction_matched_flips_the_sign_for_shorts(self):
        for side, sign in (("BUY", 1), ("SELL", -1)):
            trades = _trades(_day_rows(side=side))
            res = mb.compare(trades, mb.interval_returns(trades, _day_market(trades, 0.01)))["results"]
            assert res["market_long_gross_bps"]["mean"] == pytest.approx(100.0)
            assert res["direction_matched_gross_bps"]["mean"] == pytest.approx(sign * 100.0)

    def test_costs_are_equal_for_strategy_and_benchmark_and_cancel_in_relative(self):
        trades = _trades(_day_rows(gross=30.0, cost=10.0))
        res = mb.compare(trades, mb.interval_returns(trades, _day_market(trades, 0.002)))["results"]
        assert res["strategy_net_bps"]["mean"] == pytest.approx(20.0)
        assert res["direction_matched_net_bps"]["mean"] == pytest.approx(20.0 - 10.0)
        assert res["relative_gross_bps"]["mean"] == pytest.approx(30.0 - 20.0)
        assert res["strategy_net_minus_market_long_net_bps"]["mean"] == pytest.approx(10.0)

    def test_relative_does_not_depend_on_the_cost(self):
        out = []
        for cost in (1.0, 50.0):
            trades = _trades(_day_rows(cost=cost))
            res = mb.compare(trades, mb.interval_returns(trades, _day_market(trades, 0.002)))
            out.append(res["results"]["relative_gross_bps"]["mean"])
        assert out[0] == pytest.approx(out[1])

    def test_unusable_windows_are_excluded_and_counted(self):
        trades = _trades(_day_rows())
        market = _day_market(trades, 0.001)
        market.iloc[0, market.columns.get_loc("ret")] = np.nan
        out = mb.compare(trades, mb.interval_returns(trades, market))
        assert out["trades_total"] == 40 and out["trades_compared"] == 39
        assert out["status_counts"]["thin_market_bar"] == 1

    def test_small_groups_are_withheld(self):
        trades = _trades(_day_rows(n_days=10))
        out = mb.compare(trades, mb.interval_returns(trades, _day_market(trades, 0.001)))
        assert "withheld" in out["by_side"]["BUY"]

    def test_no_comparable_trades_gives_counts_only(self):
        trades = _trades([("BUY", 0, 0, 1.0, 1.0, T0)])
        out = mb.compare(trades, mb.interval_returns(trades, _market([np.nan])))
        assert out["trades_compared"] == 0 and "results" not in out

    def test_missing_columns_and_empty_raise(self):
        with pytest.raises(mb.BenchmarkError):
            mb.compare(pd.DataFrame([{"symbol": "A"}]), pd.DataFrame())
        with pytest.raises(mb.BenchmarkError):
            mb.compare(_trades([("BUY", 0, 0, 1, 1, T0)]).iloc[0:0], pd.DataFrame())


class TestStatistics:
    def test_clustered_mean_matches_the_hand_formula(self):
        x = np.array([1.0, 3.0, 2.0, 6.0, 5.0, 4.0])
        day = np.array(["a", "a", "b", "b", "c", "c"])
        out = mb._clustered_mean(x, day)
        dev = x - x.mean()
        want = np.sqrt(sum(dev[day == d].sum() ** 2 for d in "abc")) / len(x)
        assert out["mean"] == pytest.approx(x.mean())
        assert out["se"] == pytest.approx(want, abs=1e-5)
        assert out["ci95"][0] < out["mean"] < out["ci95"][1]

    def test_fewer_than_three_days_gives_no_t(self):
        out = mb._clustered_mean(np.array([1.0, 2.0, 3.0]), np.array(["a", "a", "b"]))
        assert out["t"] is None and out["ci95"] is None

    def test_regression_recovers_alpha_and_beta(self):
        rng = np.random.default_rng(3)
        day = np.repeat([f"d{i}" for i in range(60)], 5)
        x = rng.normal(0, 30, len(day))
        y = 4.0 + 0.7 * x + rng.normal(0, 1e-6, len(day))
        out = mb._cluster_ols(y, x, day)
        assert out["alpha_bps"] == pytest.approx(4.0, abs=1e-3)
        assert out["beta"] == pytest.approx(0.7, abs=1e-3)

    def test_regression_without_variation_is_not_estimable(self):
        out = mb._cluster_ols(np.ones(20), np.zeros(20), np.array(["a", "b"] * 10))
        assert out["beta"] is None


class TestEvidence:
    N = 35

    def _setup(self, tmp_path):
        run = tmp_path / "run"
        (run / "evidence").mkdir(parents=True)
        _trades(_day_rows(n_days=self.N)).to_parquet(run / "evidence" / "trades.parquet")
        idx = [T0 + pd.Timedelta(days=k) for k in range(self.N)]
        frame = pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 100.0, "Close": 101.0}, index=idx)
        return run, {f"S{i}": frame for i in range(mb.MIN_SYMBOLS)}

    def test_sealed_payload_is_valid_deterministic_strict_json_and_read_only(self, tmp_path):
        run, bars = self._setup(tmp_path)
        before = sorted(p.name for p in run.rglob("*"))
        ev1 = mb.evidence_for_run(run, bars)
        ev2 = mb.evidence_for_run(run, bars)
        assert ev1 == ev2 and mb.validate_evidence(ev1) == []
        assert ev1["report_only"] and ev1["diagnostic_not_trials"]
        assert ev1["comparison"]["trades_compared"] == self.N
        res = ev1["comparison"]["results"]
        assert res["market_long_gross_bps"]["mean"] == pytest.approx(100.0)
        json.dumps(ev1, allow_nan=False)
        assert sorted(p.name for p in run.rglob("*")) == before

    def test_tampering_breaks_the_seal(self, tmp_path):
        run, bars = self._setup(tmp_path)
        ev = mb.evidence_for_run(run, bars)
        ev["comparison"]["trades_total"] = 1
        assert any("content_sha256" in p for p in mb.validate_evidence(ev))


class TestReconciliation:
    """Numerators, denominators and cost basis: the figures must tie to the recorded trades."""

    def _setup(self, n=40, gross=7.0, cost=11.5):
        trades = _trades(_day_rows(n_days=n, gross=gross, cost=cost))
        return trades, _day_market(trades, 0.002)

    def test_strategy_net_is_gross_minus_the_recorded_cost_in_bps(self):
        trades, market = self._setup()
        res = mb.compare(trades, mb.interval_returns(trades, market))["results"]
        assert res["strategy_gross_bps"]["mean"] == pytest.approx(7.0)
        assert res["strategy_net_bps"]["mean"] == pytest.approx(7.0 - 11.5)
        assert res["strategy_net_bps"]["mean"] == pytest.approx(
            trades["net_bps"].mean()
        )

    def test_t_statistic_is_not_a_bps_figure(self):
        trades, market = self._setup()
        trades["gross_bps"] += np.linspace(-1, 1, len(trades))
        trades["net_bps"] = trades["gross_bps"] - trades["cost_bps"]
        res = mb.compare(trades, mb.interval_returns(trades, market))
        net = res["results"]["strategy_net_bps"]
        assert net["t"] == pytest.approx(net["mean"] / net["se"], rel=1e-3)
        assert "not bps" in res["metrics"]["t"]

    def test_benchmark_pays_exactly_the_strategy_cost(self):
        trades, market = self._setup(cost=11.5)
        res = mb.compare(trades, mb.interval_returns(trades, market))["results"]
        assert res["market_long_gross_bps"]["mean"] - res["market_long_net_bps"]["mean"] == pytest.approx(11.5)
        assert res["direction_matched_gross_bps"]["mean"] - res["direction_matched_net_bps"][
            "mean"
        ] == pytest.approx(11.5)

    def test_relative_equals_strategy_net_minus_direction_matched_net(self):
        trades, market = self._setup()
        res = mb.compare(trades, mb.interval_returns(trades, market))["results"]
        assert res["relative_gross_bps"]["mean"] == pytest.approx(
            res["strategy_net_bps"]["mean"] - res["direction_matched_net_bps"]["mean"]
        )

    def test_reconciliation_block_ties_all_compared_and_excluded_samples(self):
        trades, market = self._setup()
        market.iloc[0, market.columns.get_loc("ret")] = np.nan
        trades.loc[0, ["gross_bps", "net_bps"]] = [100.0, 100.0 - 11.5]
        rec = mb.compare(trades, mb.interval_returns(trades, market))["reconciliation"]
        a, c, e = rec["all_recorded_trades"], rec["compared_sample"], rec["excluded_sample"]
        assert (a["trades"], c["trades"], e["trades"]) == (40, 39, 1)
        assert a["mean_net_bps"] * 40 == pytest.approx(c["mean_net_bps"] * 39 + e["mean_net_bps"] * 1)
        assert rec["net_equals_gross_minus_cost_max_abs_error_bps"] == pytest.approx(0.0, abs=1e-9)

    def test_a_trade_list_whose_net_is_not_gross_minus_cost_is_flagged(self):
        trades, market = self._setup()
        trades["net_bps"] += 1.0
        rec = mb.compare(trades, mb.interval_returns(trades, market))["reconciliation"]
        assert rec["net_equals_gross_minus_cost_max_abs_error_bps"] == pytest.approx(1.0)

    def test_regression_uses_the_same_trades_as_the_means(self):
        trades, market = self._setup()
        market["ret"] = np.linspace(-0.004, 0.004, len(market))
        market.iloc[0, market.columns.get_loc("ret")] = np.nan
        res = mb.compare(trades, mb.interval_returns(trades, market))
        assert res["trades_compared"] == 39
        assert res["regression_gross_on_direction_matched"]["days"] == 39

    def test_sensitivity_uses_exactly_the_same_trades(self, tmp_path):
        n = 35
        idx = [T0 + pd.Timedelta(days=k) for k in range(n)]
        frame = pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 100.0, "Close": 101.0}, index=idx)
        bars = {f"S{i}": frame for i in range(mb.MIN_SYMBOLS)}
        trades = _trades(_day_rows(n_days=n))
        ev = mb.build_evidence(trades, bars, {"run_dir": "x"})
        assert ev["exit_bar_excluded"]["same_trades_as_comparison"] is True
        assert ev["exit_bar_excluded"]["trades_compared"] == ev["comparison"]["trades_compared"]

    def test_evidence_states_the_survivorship_limitation(self):
        n = 35
        idx = [T0 + pd.Timedelta(days=k) for k in range(n)]
        frame = pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 100.0, "Close": 101.0}, index=idx)
        bars = {f"S{i}": frame for i in range(mb.MIN_SYMBOLS)}
        ev = mb.build_evidence(_trades(_day_rows(n_days=n)), bars, {"run_dir": "x"})
        assert any("survivorship" in line for line in ev["limitations"])
        assert "survivorship" in ev["source"]["universe"]
