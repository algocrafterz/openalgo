"""record_run wiring for the benchmarking upgrades: deployed cost model, embargo, baseline."""

import json
from pathlib import Path

import pytest
import yaml

from signal_engine.backtest import record_run as rr
from signal_engine.tests import test_record_run as base
from signal_engine.tests.test_record_run import SUPPORTED, _execution

root = base.root  # the idea-folder fixture, shared with test_record_run

CONFIG = {
    "sizing": {"risk_per_trade": 0.01, "sandbox_capital": 100000},
    "broker": {"exchange": "NSE", "product": "MIS", "mis_margin_pct": 0.20},
}


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    return path


class TestDeployedCostModel:
    def test_intraday_is_mis_at_the_configured_leverage(self, config_path):
        m = rr.deployed_cost_model("intraday", config_path)
        assert (m.product, m.capital, m.risk_per_trade) == ("MIS", 100000.0, 0.01)
        assert m.leverage == pytest.approx(5.0)
        assert m.slippage_bps_per_side == rr.SLIPPAGE_BPS_PER_SIDE

    def test_swing_is_unlevered_delivery(self, config_path):
        m = rr.deployed_cost_model("swing", config_path)
        assert (m.product, m.leverage) == ("CNC", 1.0)

    def test_missing_key_fails_fast_and_names_it(self, tmp_path):
        bad = {"sizing": {"risk_per_trade": 0.01}, "broker": CONFIG["broker"]}
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(bad), encoding="utf-8")
        with pytest.raises(KeyError, match="sandbox_capital"):
            rr.deployed_cost_model("intraday", path)

    def test_unknown_kind_is_rejected(self, config_path):
        with pytest.raises(ValueError):
            rr.deployed_cost_model("positional", config_path)


class TestRecordWiring:
    def test_run_record_carries_embargo_and_the_cost_model(self, root, config_path):
        model = rr.deployed_cost_model("intraday", config_path)
        execution = _execution()
        execution = rr.Execution(**{**execution.__dict__, "cost_model": model})
        path = rr.record_idea(
            root,
            "IDEA-009",
            execute=lambda key: execution,
            run_date="2026-10-01",
            supported=SUPPORTED,
        )
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["split"]["embargo_sessions"] == rr.EMBARGO_SESSIONS
        assert record["cost_model"]["model"] == "statutory-per-trade"

    def test_run_without_a_cost_model_still_records(self, root):
        path = rr.record_idea(
            root,
            "IDEA-009",
            execute=lambda key: _execution(),
            run_date="2026-10-01",
            supported=SUPPORTED,
        )
        assert "cost_model" not in json.loads(path.read_text(encoding="utf-8"))

    def test_embargo_is_one_session(self):
        assert rr.EMBARGO_SESSIONS == 1
