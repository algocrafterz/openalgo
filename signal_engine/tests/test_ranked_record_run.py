"""A run whose trades have no stop, recorded end to end: artifact, registry, evaluation, decision."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from signal_engine.backtest import costs, trades_io
from signal_engine.backtest import record_run as rr
from signal_engine.backtest.ledgers import LedgerRecorder
from signal_engine.research import build_registry as br
from signal_engine.research.intake import decisions
from signal_engine.tests.intake_fixtures import MULTI_POSITION, make_spec
from signal_engine.tests.ranked_fixtures import RankedTop, RankParams
from signal_engine.tests.test_ranked_portfolio_constraints import run_book
from signal_engine.tests.test_spec_gate import _idea, _write_spec

SPEC = make_spec(
    style="swing",
    direction="long_only",
    timeframe="1d",
    product="CNC",
    entry=["Weekly, hold the 3 stocks closest to their 20-session high close within 10%"],
    exit=["Sell at the next open any stock that drops out of the 3 closest within 10%"],
    stop="none",
    take_profit="none",
    holding_period="none",
    indicators=[{"name": "HIGHEST_CLOSE", "period": 20, "source": "close"}],
    parameters={"n": 3, "closeness": 0.9, "window": 20},
    timing={
        "signal_evaluation": "bar_close",
        "order_submission": "signal_close",
        "fill_model": "next_bar_open",
    },
    session_window="none",
    filters="none",
    re_entry={"allowed": True, "max_per_symbol_day": 1, "trigger": "same_signal"},
    portfolio={**MULTI_POSITION, "max_concurrent": 3, "selection": "rank_by_score"},
)


@pytest.fixture(autouse=True)
def _stub_provenance(monkeypatch):
    from signal_engine.backtest import manifest

    monkeypatch.setattr(
        manifest,
        "git_state",
        lambda root: {"code_commit": "a" * 40, "code_dirty": False, "dirty_paths": []},
    )
    monkeypatch.setattr(manifest, "dataset_fingerprint", lambda symbols, interval, as_of: "d" * 64)


def execute(key):
    recorder = LedgerRecorder()
    frames, calendar, trades = run_book(recorder=recorder)
    ledgers = recorder.to_frames()
    model = costs.CostModel(
        capital=2_000_000.0,
        risk_per_trade=0.01,
        leverage=1.0,
        product="CNC",
        exchange="NSE",
        slippage_bps_per_side=4.0,
        fixed_notional=100_000.0,
        broker="flattrade",
    )
    trades, cost_block = rr.statutory_execution(trades, model, 24.0)
    days = [d.isoformat() for d in calendar.trading_days]
    return rr.Execution(
        trades=trades,
        params={"strategy": {"n": 3, "closeness": 0.9, "window": 20}, "engine": "swing"},
        data={"source": "historify", "as_of": days[-1], "universe": "u", "bar_size": "1d"},
        period={"start": days[0], "end": days[-1]},
        costs=cost_block,
        days=days,
        cost_model=model,
        cost_components=costs.decompose_trades(trades, model),
        symbols=sorted(frames),
        ledgers=ledgers,
        ledger_problems=recorder.reconcile(ledgers, trades),
    )


@pytest.fixture
def recorded(tmp_path):
    folder = _idea(tmp_path, "012")
    _write_spec(folder, SPEC)
    path = rr.record_idea(
        tmp_path, "IDEA-012", execute=execute, run_date="2026-10-04", supported={"orb"}
    )
    return tmp_path, folder, path


class TestRecordedArtifact:
    def test_the_run_is_recorded(self, recorded):
        _, _, path = recorded
        assert path.exists()

    def test_the_registry_loads_the_idea_with_its_run(self, recorded):
        root, _, _ = recorded
        (idea,) = br.load_ideas(root)
        assert len(idea["runs"]) == 1

    def test_the_headline_is_in_bps_with_no_r(self, recorded):
        _, _, path = recorded
        metrics = json.loads(path.read_text(encoding="utf-8"))["metrics"]
        assert metrics["r_defined"] is False
        assert "expectancy_r" not in metrics and "max_dd_r" not in metrics
        for key in ("net_bps", "gross_bps", "t_stat", "max_dd_bps", "total_cost_bps"):
            assert key in metrics

    def test_the_rupee_pnl_is_modelled_and_states_its_basis(self, recorded):
        _, _, path = recorded
        block = json.loads(path.read_text(encoding="utf-8"))["cost_model"]
        assert block["fixed_notional"] == 100_000.0
        assert block["pnl_basis"].startswith("modelled")
        assert "modelled_net_pnl_rs" in block and "net_bps" in block

    def test_the_trades_file_has_bps_and_no_r(self, recorded):
        _, folder, path = recorded
        run_folder = path.with_suffix("")
        frame = pd.read_parquet(run_folder / "evidence" / "trades.parquet")
        assert frame["gross_r"].isna().all() and frame["net_r"].isna().all()
        assert frame["net_bps"].notna().all()

    def test_the_lineage_ledger_has_signals_without_a_stop_value(self, recorded):
        _, _, path = recorded
        run_folder = path.with_suffix("")
        signals = pd.read_parquet(run_folder / "evidence" / "signals.parquet")
        assert signals["stop"].isna().all()
        assert signals["accepted"].all()

    def test_r_denominated_gates_are_not_evaluated_not_passed(self, recorded):
        _, _, path = recorded
        rows = {
            r["gate"]: r
            for d in json.loads(path.read_text(encoding="utf-8"))["evaluation"]["dimensions"]
            for r in d["rows"]
        }
        for gate in ("G1.2", "G1.5"):
            assert rows[gate]["status"] == "not_evaluated", gate
            assert "no stop" in rows[gate]["evidence"]
        assert rows["G1.3"]["status"] in ("pass", "fail")

    def test_a_verdict_is_calculated_and_it_is_never_edge(self, recorded):
        root, _, _ = recorded
        (idea,) = br.load_ideas(root)
        assert idea["verdict"] in ("marginal", "no-edge", "inconclusive")

    def test_a_keep_decision_does_not_change_the_calculated_result(self, recorded):
        root, folder, path = recorded
        before = br.load_ideas(root)[0]["verdict"]
        decisions.record(folder, path.stem, "keep", note="reference run")
        assert br.load_ideas(root)[0]["verdict"] == before


class TestFrozenSpecReachesTheRun:
    """The frozen spec's intent, not only its execution fields, must reach `execute_backtest`.

    Binding the class to the spec, the fixed notional and the unlimited holding cap all depend on
    these settings. Unit tests that pass them by hand would not notice if the real path dropped them.
    """

    def captured(self, tmp_path):
        seen = {}

        def spying(key, settings=None):
            seen.update(settings or {})
            return execute(key)

        folder = _idea(tmp_path, "012")
        _write_spec(folder, SPEC)
        rr.record_idea(
            tmp_path, "IDEA-012", execute=spying, run_date="2026-10-04", supported={"orb"}
        )
        return seen

    def test_the_spec_parameters_are_handed_to_the_run(self, tmp_path):
        assert self.captured(tmp_path)["parameters"] == SPEC["parameters"]

    def test_the_stop_and_the_holding_period_are_handed_to_the_run(self, tmp_path):
        seen = self.captured(tmp_path)
        assert seen["stop"] == "none" and seen["holding_period"] == "none"

    def test_the_capital_per_position_is_handed_to_the_run(self, tmp_path):
        assert self.captured(tmp_path)["capital_per_position"] == 100000

    def test_the_execution_settings_are_still_handed_over(self, tmp_path):
        seen = self.captured(tmp_path)
        assert seen["cost_bps"] == 24.0 and seen["warmup_bars"] == 20
