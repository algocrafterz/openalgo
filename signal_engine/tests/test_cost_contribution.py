"""Cost stress and symbol/year contribution (report-only diagnostics on a trade list)."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from signal_engine.backtest import cost_contribution as cc

STAT = {
    "cost_brokerage_bps": 0.0,
    "cost_stt_bps": 2.5,
    "cost_exchange_bps": 0.5,
    "cost_gst_bps": 0.1,
    "cost_sebi_bps": 0.0,
    "cost_stamp_bps": 0.4,
}
SLIP = 8.0
COST_BPS = sum(STAT.values()) + SLIP  # 11.5
RISK_BPS = 50.0


def _trade(symbol, when, gross_r):
    gross_bps = gross_r * RISK_BPS
    return {
        "symbol": symbol,
        "entry_time": pd.Timestamp(when, tz="UTC"),
        "gross_r": gross_r,
        "net_r": gross_r - COST_BPS / RISK_BPS,
        "gross_bps": gross_bps,
        "net_bps": gross_bps - COST_BPS,
        "cost_bps": COST_BPS,
        "cost_slippage_bps": SLIP,
        **STAT,
    }


def _frame(rows):
    return pd.DataFrame([_trade(*r) for r in rows])


ROWS = [
    ("AAA", "2023-03-01 05:00", 1.0),
    ("AAA", "2023-03-02 05:00", -1.0),
    ("BBB", "2024-03-01 05:00", -1.0),
    ("BBB", "2024-03-02 05:00", -1.0),
    ("CCC", "2024-06-01 05:00", 0.5),
]


class TestCostStress:
    def test_one_x_reproduces_the_recorded_result(self):
        out = cc.cost_stress(_frame(ROWS))
        assert out["reconciliation"]["matches"]
        assert out["all_costs"][0]["net_r_per_trade"] == pytest.approx(
            out["reconciliation"]["recorded_net_r_per_trade"], abs=1e-5
        )

    def test_scaling_all_costs_moves_net_r_by_the_cost_in_r(self):
        out = cc.cost_stress(_frame(ROWS))
        cost_r = COST_BPS / RISK_BPS
        one, half_more, two = out["all_costs"]
        assert one["net_r_per_trade"] - half_more["net_r_per_trade"] == pytest.approx(
            0.5 * cost_r, abs=1e-5
        )
        assert one["net_r_per_trade"] - two["net_r_per_trade"] == pytest.approx(cost_r, abs=1e-5)

    def test_statutory_only_leaves_slippage_alone(self):
        out = cc.cost_stress(_frame(ROWS))
        stat_r = sum(STAT.values()) / RISK_BPS
        one, _, two = out["statutory_only"]
        assert one["net_r_per_trade"] - two["net_r_per_trade"] == pytest.approx(stat_r, abs=1e-5)
        assert two["mean_cost_bps"] == pytest.approx(SLIP + 2 * sum(STAT.values()), abs=1e-4)

    def test_trade_count_is_unchanged_across_multipliers(self):
        out = cc.cost_stress(_frame(ROWS))
        assert {r["trades"] for r in out["all_costs"]} == {5}

    def test_gap_between_component_sum_and_recorded_total_is_reported(self):
        df = _frame(ROWS)
        assert cc.cost_stress(df)["component_sum_gap_bps"]["max"] == 0
        df["cost_bps"] = df["cost_bps"] + 1.0
        df["net_r"] = df["gross_r"] - df["cost_bps"] / RISK_BPS
        df["net_bps"] = df["gross_bps"] - df["cost_bps"]
        out = cc.cost_stress(df)
        assert out["component_sum_gap_bps"]["max"] == pytest.approx(1.0)
        assert out["reconciliation"]["matches"]
        # slippage stays at its recorded value, so statutory absorbs the whole gap
        assert out["statutory_only"][2]["mean_cost_bps"] == pytest.approx(
            SLIP + 2 * (df["cost_bps"].iloc[0] - SLIP), abs=1e-4
        )

    def test_undefined_r_trades_are_excluded_and_counted(self):
        df = _frame(ROWS)
        df.loc[0, ["net_r", "gross_r"]] = float("nan")
        out = cc.cost_stress(df)
        assert out["trades_total"] == 5
        assert out["trades_with_defined_r"] == 4

    def test_zero_cost_trades_do_not_divide_by_zero(self):
        df = _frame(ROWS)
        for c in cc._COMPONENTS:
            df[c] = 0.0
        df["cost_bps"] = 0.0
        df["net_r"] = df["gross_r"]
        df["net_bps"] = df["gross_bps"]
        out = cc.cost_stress(df)
        assert out["statutory_only"][2]["net_r_per_trade"] == out["all_costs"][0]["net_r_per_trade"]

    def test_missing_columns_and_empty_list_raise(self):
        with pytest.raises(cc.CostContributionError):
            cc.cost_stress(_frame(ROWS).drop(columns=["cost_stt_bps"]))
        with pytest.raises(cc.CostContributionError):
            cc.cost_stress(_frame(ROWS).iloc[0:0])


class TestContribution:
    def test_negative_aggregate_is_flagged_and_uses_the_stated_denominator(self):
        out = cc.contribution(_frame(ROWS))
        total = out["aggregate_total_net_r"]
        assert total < 0 and out["aggregate_negative"] is True
        by = {r["symbol"]: r for r in out["by_symbol"]}
        assert by["BBB"]["share_of_aggregate"] == pytest.approx(by["BBB"]["total_net_r"] / total)
        assert "negative" in out["share_note"]
        abs_total = sum(abs(x) for x in _frame(ROWS)["net_r"])
        assert sum(r["share_of_abs_total"] for r in out["by_symbol"]) == pytest.approx(
            total / abs_total, abs=1e-5
        )

    def test_a_winning_group_has_a_negative_share_of_a_negative_aggregate(self):
        rows = [("WIN", "2023-03-01 05:00", 3.0)] + [
            ("LOSE", f"2023-04-{d:02d} 05:00", -1.0) for d in range(1, 9)
        ]
        out = cc.contribution(_frame(rows))
        assert out["aggregate_total_net_r"] < 0
        win = next(r for r in out["by_symbol"] if r["symbol"] == "WIN")
        assert win["total_net_r"] > 0 and win["share_of_aggregate"] < 0

    def test_zero_aggregate_gives_no_share(self):
        df = _frame([("AAA", "2023-03-01 05:00", 1.0), ("BBB", "2023-03-02 05:00", -1.0)])
        df["net_r"] = [1.0, -1.0]
        out = cc.contribution(df)
        assert out["aggregate_total_net_r"] == 0
        assert all(r["share_of_aggregate"] is None for r in out["by_symbol"])
        assert out["top_five_symbols"]["share_of_aggregate"] is None
        assert "undefined" in out["share_note"]

    def test_all_zero_r_has_no_abs_share(self):
        df = _frame(ROWS)
        df["net_r"] = 0.0
        out = cc.contribution(df)
        assert all(r["share_of_abs_total"] is None for r in out["by_year"])

    def test_year_is_the_ist_year_not_the_utc_year(self):
        # 2023-12-31 20:00 UTC is 2024-01-01 01:30 IST
        df = _frame([("AAA", "2023-12-31 20:00", 1.0), ("AAA", "2024-01-02 05:00", 1.0)])
        years = {r["year"] for r in cc.contribution(df)["by_year"]}
        assert years == {2024}

    def test_removal_recomputes_on_the_remaining_trades(self):
        df = _frame(ROWS)
        out = cc.contribution(df)
        best = out["by_symbol"][0]
        rem = out["remove_best_symbol"]
        assert rem["removed"] == best["symbol"]
        rest = df[df["symbol"] != best["symbol"]]
        assert rem["remaining_trades"] == len(rest)
        assert rem["remaining_total_net_r"] == pytest.approx(rest["net_r"].sum(), abs=1e-5)
        assert out["remove_best_year"]["remaining_trades"] < len(df)

    def test_removing_the_only_group_leaves_nothing(self):
        out = cc.contribution(_frame([("AAA", "2023-03-01 05:00", 1.0)]))
        assert out["remove_best_symbol"]["remaining_trades"] == 0
        assert out["remove_best_symbol"]["remaining_total_net_r"] is None

    def test_top_five_is_ranked_by_total_net_r(self):
        rows = [(f"S{i}", f"2023-03-{i + 1:02d} 05:00", float(i)) for i in range(8)]
        out = cc.contribution(_frame(rows))
        assert out["top_five_symbols"]["symbols"] == ["S7", "S6", "S5", "S4", "S3"]

    def test_counts_of_positive_groups(self):
        out = cc.contribution(_frame(ROWS))
        assert out["symbols_positive"] == sum(1 for r in out["by_symbol"] if r["total_net_r"] > 0)


class TestEvidence:
    def _evidence(self):
        return cc.build_evidence(_frame(ROWS), {"run_dir": "synthetic"})

    def test_sealed_payload_validates(self):
        ev = self._evidence()
        assert ev["report_only"] and ev["diagnostic_not_trials"]
        assert cc.validate_evidence(ev) == []

    def test_tampering_breaks_the_seal(self):
        ev = self._evidence()
        ev["contribution"]["aggregate_total_net_r"] = 1.0
        assert any("content_sha256" in p for p in cc.validate_evidence(ev))

    def test_payload_is_deterministic(self):
        assert self._evidence() == self._evidence()

    def test_payload_is_strict_json(self):
        json.dumps(self._evidence(), allow_nan=False)

    def test_run_folder_is_read_only(self, tmp_path):
        run = tmp_path / "run"
        (run / "evidence").mkdir(parents=True)
        _frame(ROWS).to_parquet(run / "evidence" / "trades.parquet")
        before = sorted(p.name for p in run.rglob("*"))
        ev = cc.evidence_for_run(run)
        assert cc.validate_evidence(ev) == []
        assert sorted(p.name for p in run.rglob("*")) == before
