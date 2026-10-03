"""Run validity checks: each check passes on clean inputs and fails on a crafted defect."""

from __future__ import annotations

import copy

import numpy as np
import pandas as pd

from signal_engine.backtest.harness import Backtest
from signal_engine.backtest.ledgers import LedgerRecorder
from signal_engine.backtest.trades_io import trades_to_frame
from signal_engine.backtest.validity import run_validity_checks
from signal_engine.tests.intake_fixtures import GOOD_SPEC, make_spec
from signal_engine.tests.test_backtest_ledgers import RUN, Momo, random_sessions


def clean_run():
    frames = {"A": random_sessions(1, 6), "B": random_sessions(2, 6)}
    rec = LedgerRecorder()
    trades = Backtest(Momo(), frames, RUN).trades(None, "all", recorder=rec)
    assert len(trades) > 3
    return rec.to_frames(), trades_to_frame(trades)


def fresh():
    ledgers, trades = clean_run()
    return copy.deepcopy(ledgers), trades.copy()


def checks(result):
    return [f["check"] for f in result["failures"]]


class TestShape:
    def test_nothing_given_runs_nothing(self):
        r = run_validity_checks()
        assert r == {"ok": True, "failures": [], "checks_run": []}

    def test_clean_everything_passes(self):
        ledgers, trades = fresh()
        r = run_validity_checks(
            data_quality={"status": "PASS", "affects_validity": False, "items": []},
            intent=GOOD_SPEC,
            ledgers=ledgers,
            trades=trades,
            truncation={"ok": True, "checked": [], "mismatches": []},
        )
        assert r["ok"] is True and r["failures"] == []
        assert r["checks_run"] == ["data_quality", "config", "lineage", "truncation"]

    def test_trade_objects_accepted(self):
        frames = {"A": random_sessions(1, 6)}
        rec = LedgerRecorder()
        trades = Backtest(Momo(), frames, RUN).trades(None, "all", recorder=rec)
        r = run_validity_checks(ledgers=rec.to_frames(), trades=trades)
        assert r["ok"] and r["checks_run"] == ["lineage"]

    def test_skipped_check_is_not_listed(self):
        r = run_validity_checks(intent=GOOD_SPEC)
        assert r["checks_run"] == ["config"]


class TestDataQuality:
    def test_affects_validity_fails(self):
        dq = {
            "status": "WARNING",
            "affects_validity": True,
            "items": [{"check": "incomplete_sessions", "detail": "x"}, "second", "third", "4th"],
        }
        r = run_validity_checks(data_quality=dq)
        assert r["ok"] is False and checks(r) == ["data_quality"]
        assert "incomplete_sessions" in r["failures"][0]["detail"]
        assert "4th" not in r["failures"][0]["detail"]

    def test_warning_not_affecting_validity_passes(self):
        dq = {"status": "WARNING", "affects_validity": False, "items": ["x"]}
        assert run_validity_checks(data_quality=dq)["ok"] is True

    def test_malformed_data_quality_is_failure_not_exception(self):
        r = run_validity_checks(data_quality="garbage")
        assert r["ok"] is False and checks(r) == ["data_quality"]


class TestConfig:
    def test_swing_shorting_fails(self):
        spec = make_spec(style="swing", direction="both", product="CNC")
        r = run_validity_checks(intent=spec)
        assert r["ok"] is False
        assert set(checks(r)) == {"config"}
        assert any("long_only" in f["detail"] for f in r["failures"])

    def test_non_mapping_intent_fails(self):
        r = run_validity_checks(intent=["x"])
        assert checks(r) == ["config"]


class TestLineage:
    def test_clean_passes(self):
        ledgers, trades = fresh()
        r = run_validity_checks(ledgers=ledgers, trades=trades)
        assert r["ok"] and r["checks_run"] == ["lineage"]

    def test_duplicate_signal_id(self):
        ledgers, trades = fresh()
        sig = ledgers["signals"]
        sig.loc[1, "signal_id"] = sig.loc[0, "signal_id"]
        r = run_validity_checks(ledgers=ledgers, trades=trades)
        assert r["ok"] is False
        assert any(sig.loc[0, "signal_id"] in f["detail"] for f in r["failures"])
        assert set(checks(r)) == {"lineage"}

    def test_trade_points_at_missing_fill(self):
        ledgers, trades = fresh()
        trades.loc[trades.index[0], "exit_fill_id"] = "F|nope|exit1"
        r = run_validity_checks(ledgers=ledgers, trades=trades)
        assert r["ok"] is False
        assert any("F|nope|exit1" in f["detail"] for f in r["failures"])

    def test_trade_points_at_missing_signal(self):
        ledgers, trades = fresh()
        trades.loc[trades.index[0], "signal_id"] = "S|ghost"
        r = run_validity_checks(ledgers=ledgers, trades=trades)
        assert any("S|ghost" in f["detail"] for f in r["failures"])

    def test_entry_before_signal(self):
        ledgers, trades = fresh()
        sid = trades.loc[trades.index[0], "signal_id"]
        sig = ledgers["signals"]
        m = sig["signal_id"] == sid
        sig.loc[m, "bar_time"] = sig.loc[m, "bar_time"] + pd.Timedelta(days=30)
        r = run_validity_checks(ledgers=ledgers, trades=trades)
        assert r["ok"] is False
        assert any(sid in f["detail"] and "signal" in f["detail"] for f in r["failures"])

    def test_exit_before_entry(self):
        ledgers, trades = fresh()
        xid = trades.loc[trades.index[0], "exit_fill_id"]
        fills = ledgers["fills"]
        m = fills["fill_id"] == xid
        fills.loc[m, "fill_time"] = fills.loc[m, "fill_time"] - pd.Timedelta(days=30)
        r = run_validity_checks(ledgers=ledgers, trades=trades)
        assert r["ok"] is False
        assert any(xid in f["detail"] for f in r["failures"])

    def test_overlapping_positions_same_symbol(self):
        ledgers, trades = fresh()
        sym = trades["symbol"].iloc[0]
        rows = trades.index[trades["symbol"] == sym]
        assert len(rows) >= 2
        trades.loc[rows[1], "entry_time"] = trades.loc[rows[0], "entry_time"]
        r = run_validity_checks(ledgers=ledgers, trades=trades)
        assert r["ok"] is False
        assert any("overlap" in f["detail"] for f in r["failures"])

    def test_nan_fill_price(self):
        ledgers, trades = fresh()
        ledgers["fills"].loc[0, "fill_price"] = np.nan
        fid = ledgers["fills"].loc[0, "fill_id"]
        r = run_validity_checks(ledgers=ledgers, trades=trades)
        assert r["ok"] is False
        assert any(fid in f["detail"] for f in r["failures"])

    def test_non_positive_fill_price(self):
        ledgers, trades = fresh()
        ledgers["fills"].loc[0, "fill_price"] = 0.0
        assert run_validity_checks(ledgers=ledgers, trades=trades)["ok"] is False

    def test_exit_without_entry(self):
        ledgers, trades = fresh()
        fills = ledgers["fills"]
        pid = trades["position_id"].iloc[0]
        ledgers["fills"] = fills[~((fills["position_id"] == pid) & (fills["kind"] == "entry"))]
        r = run_validity_checks(ledgers=ledgers, trades=trades)
        assert r["ok"] is False

    def test_failures_deterministic_and_ordered(self):
        a, ta = fresh()
        b, tb = fresh()
        for ledgers, trades in ((a, ta), (b, tb)):
            ledgers["fills"].loc[0, "fill_price"] = np.nan
            trades.loc[trades.index[1], "signal_id"] = "S|ghost"
            ledgers["signals"].loc[3, "signal_id"] = ledgers["signals"].loc[2, "signal_id"]
        r1 = run_validity_checks(ledgers=a, trades=ta)
        r2 = run_validity_checks(ledgers=b, trades=tb)
        assert r1 == r2 and len(r1["failures"]) >= 3

    def test_malformed_ledgers_are_failures(self):
        _, trades = fresh()
        for bad in ({}, {"signals": 3}, "x", {"signals": pd.DataFrame({"a": [1]})}):
            r = run_validity_checks(ledgers=bad, trades=trades)
            assert r["ok"] is False and checks(r) == ["lineage"]

    def test_trades_missing_when_ledgers_given_is_failure(self):
        ledgers, _ = fresh()
        r = run_validity_checks(ledgers=ledgers, trades=None)
        assert r["ok"] is False and checks(r) == ["lineage"]

    def test_empty_trades_pass(self):
        ledgers, trades = fresh()
        r = run_validity_checks(ledgers=ledgers, trades=trades.iloc[0:0])
        assert r["ok"] is True


class TestTruncationInput:
    def test_mismatches_become_failures(self):
        t = {
            "ok": False,
            "checked": [],
            "mismatches": [
                {"symbol": "A", "cut": "2026-06-04", "kind": "trade_missing", "detail": "d1"},
                {"symbol": "B", "cut": "2026-06-05", "kind": "trade_extra", "detail": "d2"},
            ],
        }
        r = run_validity_checks(truncation=t)
        assert r["ok"] is False and checks(r) == ["truncation", "truncation"]
        assert "trade_missing" in r["failures"][0]["detail"] and "d1" in r["failures"][0]["detail"]

    def test_accepts_result_object(self):
        from signal_engine.backtest.truncation import TruncationResult

        res = TruncationResult(ok=True, checked=[], mismatches=[], skipped=[])
        assert run_validity_checks(truncation=res)["ok"] is True

    def test_malformed_truncation_is_failure(self):
        r = run_validity_checks(truncation=42)
        assert r["ok"] is False and checks(r) == ["truncation"]


class TestOrdering:
    def test_failures_ordered_by_check(self):
        ledgers, trades = fresh()
        ledgers["fills"].loc[0, "fill_price"] = np.nan
        r = run_validity_checks(
            data_quality={"status": "WARNING", "affects_validity": True, "items": ["x"]},
            intent=make_spec(style="swing", direction="both", product="CNC"),
            ledgers=ledgers,
            trades=trades,
            truncation={
                "ok": False,
                "mismatches": [{"symbol": "A", "cut": "c", "kind": "trade_extra", "detail": "d"}],
            },
        )
        order = checks(r)
        assert order == sorted(order, key=["data_quality", "config", "lineage", "truncation"].index)


class TestLadder:
    def test_ladder_exits_resolve_and_pass(self):
        from signal_engine.tests.test_backtest_ledgers import RUN_VARIANTS

        frames = {"A": random_sessions(1, 8), "B": random_sessions(2, 8)}
        rec = LedgerRecorder()
        trades = Backtest(Momo(), frames, RUN_VARIANTS[5]).trades(None, "all", recorder=rec)
        assert any(t.legs > 1 for t in trades)
        fills = rec.to_frames()["fills"].set_index("fill_id")
        assert all(t.exit_fill_id in fills.index for t in trades)
        r = run_validity_checks(ledgers=rec.to_frames(), trades=trades)
        assert r["ok"], r["failures"]
