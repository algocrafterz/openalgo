"""Run record wiring: purge and embargo at the split, and the per-trade cost model block."""

import pandas as pd
import pytest

from signal_engine.backtest import costs
from signal_engine.backtest.tearsheet import build_run_record
from signal_engine.backtest.types import Trade
from signal_engine.research import build_registry as br

META = {
    "run_id": "2026-10-01-x-v1",
    "run_date": "2026-10-01",
    "code_version": "abc1234",
    "data": {"source": "historify", "as_of": "2026-09-29", "universe": "u", "bar_size": "1d"},
    "period": {"start": "2024-01-01", "end": "2024-06-30"},
    "costs": {"model": "flat-round-trip-bps", "round_trip_bps": 24.0},
}


def _book(n=80, hold=3):
    days = [d.date().isoformat() for d in pd.bdate_range("2024-01-01", periods=n)]
    out = []
    for i, d in enumerate(days):
        out.append(
            Trade(
                symbol=f"S{i % 5}.NS",
                day=d,
                direction=1,
                tag="t",
                entry_time=pd.Timestamp(f"{d} 09:15"),
                entry=100.0,
                signal_price=100.0,
                sl=99.0,
                tp=0.0,
                risk=1.0,
                exit_time=pd.Timestamp(f"{days[min(i + hold, n - 1)]} 15:30"),
                reason="tp",
                r_gross=0.4 if i % 2 else -0.2,
                r_net=0.3 if i % 2 else -0.3,
            )
        )
    return out


class TestEmbargo:
    def test_default_record_is_unchanged(self):
        rec = build_run_record(_book(), oos_start="2024-03-01", **META)
        assert rec["split"] == {"type": "oos", "oos_start": "2024-03-01"}

    def test_embargo_records_what_was_removed(self):
        rec = build_run_record(_book(), oos_start="2024-03-01", embargo_sessions=1, **META)
        assert rec["split"]["embargo_sessions"] == 1
        assert rec["split"]["purged_in_sample"] >= 1
        assert rec["split"]["embargoed_oos"] >= 1

    def test_embargo_shrinks_the_two_windows(self):
        plain = build_run_record(_book(), oos_start="2024-03-01", **META)
        cut = build_run_record(_book(), oos_start="2024-03-01", embargo_sessions=1, **META)
        assert cut["in_sample"]["trades"] < plain["in_sample"]["trades"]
        assert cut["out_of_sample"]["trades"] < plain["out_of_sample"]["trades"]

    def test_g35_passes_once_the_embargo_is_applied(self):
        rec = build_run_record(_book(), oos_start="2024-03-01", embargo_sessions=1, **META)
        row = next(
            r for d in rec["evaluation"]["dimensions"] for r in d["rows"] if r["gate"] == "G3.5"
        )
        assert row["status"] == "pass"

    def test_record_with_embargo_validates(self):
        rec = build_run_record(_book(), oos_start="2024-03-01", embargo_sessions=1, **META)
        assert br._validate_run_v2(rec) == []


class TestCostModelBlock:
    MODEL = costs.CostModel(
        capital=100_000.0,
        risk_per_trade=0.01,
        leverage=1.0,
        product="CNC",
        exchange="NSE",
        slippage_bps_per_side=4.0,
    )

    def test_absent_by_default(self):
        assert "cost_model" not in build_run_record(_book(), **META)

    def test_block_is_recorded_and_feeds_the_cost_gates(self):
        rec = build_run_record(_book(), cost_model=self.MODEL, **META)
        assert rec["cost_model"]["model"] == "statutory-per-trade"
        rows = {r["gate"]: r for d in rec["evaluation"]["dimensions"] for r in d["rows"]}
        assert rows["G1.1"]["status"] == "pass"
        assert rows["G1.3"]["status"] in ("pass", "fail")

    def test_headline_metrics_stay_on_the_engine_flat_cost(self):
        with_model = build_run_record(_book(), cost_model=self.MODEL, **META)
        without = build_run_record(_book(), **META)
        assert with_model["metrics"] == without["metrics"]

    def test_record_with_cost_model_validates(self):
        rec = build_run_record(_book(), cost_model=self.MODEL, **META)
        assert br._validate_run_v2(rec) == []

    def test_malformed_cost_model_block_is_rejected_by_the_registry(self):
        rec = build_run_record(_book(), cost_model=self.MODEL, **META)
        rec["cost_model"] = {"model": 3}
        assert any("cost_model" in p for p in br._validate_run_v2(rec))

    def test_malformed_embargo_info_is_rejected_by_the_registry(self):
        rec = build_run_record(_book(), oos_start="2024-03-01", embargo_sessions=1, **META)
        rec["split"]["embargo_sessions"] = -2
        assert any("embargo" in p for p in br._validate_run_v2(rec))
