"""Sidecars rebuilt from a sealed run's frozen inputs, never touching the run (reconstruct)."""

import hashlib
import json
from datetime import datetime

import pandas as pd
import pytest

from signal_engine.backtest import book_series
from signal_engine.backtest import reconstruct_portfolio as rp
from signal_engine.timeutils import IST

RUN_ID = "2024-02-01-toy-v1"
DAYS = [d.date() for d in pd.bdate_range("2024-01-01", "2024-03-29")]


class _Calendar:
    version = "cal-1"

    def expected_sessions(self, start, end):
        return tuple(d for d in DAYS if start <= d <= end)


def _row(gate, status="report", evidence="recorded"):
    return {
        "gate": gate,
        "title": gate,
        "status": status,
        "hard": False,
        "value": None,
        "threshold": "",
        "evidence": evidence,
    }


def _ts(day):
    return pd.Timestamp(day, tz=IST)


def _sealed_run(tmp_path, gross_r=float("nan")):
    runs = tmp_path / "idea-999-toy" / "runs"
    folder = runs / RUN_ID
    (folder / "evidence").mkdir(parents=True)
    trades = pd.DataFrame(
        {
            "trade_id": ["T-1", "T-2", "T-3", "T-4"],
            "symbol": ["A", "B", "A", "B"],
            "side": ["BUY"] * 4,
            "entry_time": [
                _ts("2024-01-02"),
                _ts("2024-01-03"),
                _ts("2024-02-05"),
                _ts("2024-02-06"),
            ],
            "exit_time": [
                _ts("2024-01-31"),
                _ts("2024-02-09"),
                _ts("2024-03-01"),
                _ts("2024-03-28"),
            ],
            "entry_price": [100.0, 50.0, 104.0, 51.0],
            "exit_price": [104.0, 52.0, 101.0, 55.0],
            "exit_reason": ["REBALANCE_EXIT"] * 3 + ["EOD_DATA"],
            "gross_r": [gross_r] * 4,
        }
    )
    trades["gross_bps"] = (trades["exit_price"] / trades["entry_price"] - 1) * 1e4
    trades["cost_bps"] = 24.0
    trades["modeled_cost_bps"] = 30.0
    trades["net_bps"] = trades["gross_bps"] - trades["cost_bps"]
    trades.to_parquet(folder / "evidence" / "trades.parquet")
    (folder / "manifest.json").write_text(
        json.dumps(
            {
                "dataset": {
                    "version_id": "toy-001",
                    "root_hash": "r" * 64,
                    "calendar_version": "cal-1",
                }
            }
        )
    )
    gates = [_row(g) for g in ("G2.3", "G2.6", "G2.7")] + [_row("G3.2", "fail")]
    record = {
        "run_id": RUN_ID,
        "date": "2024-04-01",
        "n_trials": 1,
        "period": {"start": "2024-01-01", "end": "2024-03-29"},
        "costs": {"model": "flat-round-trip-bps", "round_trip_bps": 24.0},
        "metrics": {"trades": 4, "t_stat": 1.0, "gross_bps": 100.0, "net_bps": 76.0, "days": 4},
        "params": {"strategy": {"max_positions": 2}},
        "cost_model": {"fixed_notional": 100000.0},
        "split": {
            "type": "oos",
            "oos_start": "2024-02-05",
            "embargo_sessions": 1,
            "purged_in_sample": 1,
            "embargoed_oos": 0,
        },
        "evaluation": {"horizon": "swing", "dimensions": [{"name": "x", "rows": gates}]},
    }
    run_json = runs / f"{RUN_ID}.json"
    run_json.write_text(json.dumps(record))
    return run_json


def _closes(manifest, symbols, end):
    assert manifest["dataset"]["root_hash"] == "r" * 64
    rows = {
        "A": [100.0 + i * 0.1 for i in range(len(DAYS))],
        "B": [50.0 + i * 0.05 for i in range(len(DAYS))],
    }
    return pd.DataFrame({s: rows[s] for s in symbols}, index=DAYS)


NOW = datetime(2026, 10, 14, 12, 0, tzinfo=IST)


def test_reconstructs_the_book_and_rescored_gates_from_frozen_inputs(tmp_path):
    run_json = _sealed_run(tmp_path)
    portfolio, gates = rp.reconstruct(
        run_json, closes_loader=_closes, calendar=_Calendar(), now=NOW
    )
    block = portfolio["portfolio"]
    assert block["capital_rs"] == 200000.0
    assert abs(block["reconciliation"]["difference_rs"]) < 1e-6
    assert block["alternate_cost"]["cost_column"] == "modeled_cost_bps"
    counts = portfolio["session_counts"]
    assert counts["recorded_days"] == 4
    assert counts["entry_sessions"] == 4
    assert counts["test_sessions"] == len(_Calendar().expected_sessions(DAYS[1], DAYS[-1]))
    assert set(gates["gates"]) == set(rp.RESCORED)
    assert gates["gates"]["G3.2"]["recorded"]["evidence"] == "recorded"
    assert "purged" in gates["gates"]["G3.2"]["corrected"]["evidence"]
    assert "marked to market" in gates["gates"]["G2.6"]["corrected"]["evidence"]
    assert portfolio["new_trial"] is False


def test_write_leaves_the_sealed_run_unchanged_and_refuses_to_overwrite(tmp_path):
    run_json = _sealed_run(tmp_path)
    before = {
        p: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in run_json.parent.rglob("*")
        if p.is_file()
    }
    portfolio, gates = rp.reconstruct(
        run_json, closes_loader=_closes, calendar=_Calendar(), now=NOW
    )
    paths = rp.write(run_json, portfolio, gates)
    assert [p.name for p in paths] == [
        "20261014T120000-ist-portfolio.json",
        "20261014T120000-ist-session-gates.json",
    ]
    assert all(p.parent.name == RUN_ID and p.parent.parent.name == "evaluations" for p in paths)
    after = {
        p: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in run_json.parent.rglob("*")
        if p.is_file()
    }
    assert before == after
    assert portfolio["sealed_run_unchanged"]["record_sha256"] == before[run_json]
    with pytest.raises(FileExistsError):
        rp.write(run_json, portfolio, gates)


def test_stop_based_ledgers_are_refused(tmp_path):
    run_json = _sealed_run(tmp_path, gross_r=1.0)
    with pytest.raises(book_series.BookError, match="stop-based"):
        rp.reconstruct(run_json, closes_loader=_closes, calendar=_Calendar(), now=NOW)


def test_a_different_calendar_version_is_refused(tmp_path, monkeypatch):
    run_json = _sealed_run(tmp_path)

    class _Other(_Calendar):
        version = "cal-2"

    from signal_engine.backtest.data_readiness import calendar as cal_mod

    monkeypatch.setattr(cal_mod, "load_calendar", lambda: _Other())
    with pytest.raises(book_series.BookError, match="calendar"):
        rp.reconstruct(run_json, closes_loader=_closes, now=NOW)
