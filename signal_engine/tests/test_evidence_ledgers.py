"""Engine P1 step 4c: ledgers and cost components carried into the run artifact, and the
evaluators (G0.5, G6.1, G6.5, G1.6) that exist because of them."""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json

import pandas as pd
import pyarrow.parquet as pq
import pytest

from signal_engine.backtest import evaluation as ev
from signal_engine.backtest import ledgers as lg
from signal_engine.backtest import manifest as mf
from signal_engine.backtest import record_run as rr
from signal_engine.backtest import reproduce as rp
from signal_engine.backtest import trades_io
from signal_engine.backtest.costs import decompose_trades
from signal_engine.backtest.engine import simulate
from signal_engine.backtest.types import Ctx, Trade
from signal_engine.tests import test_record_run as base
from signal_engine.tests import test_run_artifact_write as raw
from signal_engine.tests.cost_fixtures import make_model, statutory
from signal_engine.tests.published_fixtures import frames_resolution
from signal_engine.tests.test_backtest_ledgers import FLAT, RUN, Script, make_frame
from signal_engine.tests.test_record_run import SUPPORTED

root = base.root
_stub_provenance = base._stub_provenance

IST = "Asia/Kolkata"
SESSION = {
    "mis_squareoff_min": 15 * 60,
    "squareoff_margin_min": 5,
    "bar_minutes": 5,
    "entry_window": "09:20-14:30",
}
MODEL = make_model(capital=100000.0)


def deployed_model(kind="intraday", *args, **kwargs):
    """Stands in for `record_run.deployed_cost_model`: MIS for intraday, unlevered CNC for swing."""
    return MODEL if kind == "intraday" else dataclasses.replace(MODEL, product="CNC", leverage=1.0)


PLAN_COLUMNS = {
    "signals": [
        "signal_id",
        "symbol",
        "bar_time",
        "direction",
        "tag",
        "signal_price",
        "stop",
        "target",
        "risk",
        "accepted",
        "exclusion_id",
    ],
    "orders": [
        "order_id",
        "signal_id",
        "symbol",
        "submit_time",
        "order_type",
        "side",
        "fill_bar_time",
        "status",
        "exclusion_id",
    ],
    "fills": [
        "fill_id",
        "order_id",
        "position_id",
        "symbol",
        "fill_time",
        "fill_price",
        "side",
        "kind",
        "exit_reason",
        "leg",
    ],
    "exclusions": [
        "exclusion_id",
        "scope",
        "symbol",
        "date",
        "bar_time",
        "reason_code",
        "detail",
        "count",
    ],
}
LEDGER_FILES = {f"evidence/{name}.parquet" for name in PLAN_COLUMNS}
SESSION_RUN = RUN.with_(min_sl_pct=0.003)


# ---- a small REAL engine run through a LedgerRecorder -----------------------------------


def synth(n_sessions=24, symbols=("AAA", "BBB")):
    """Two symbols, `n_sessions` sessions each. Session kinds: TP, SL, gap through the stop,
    and a stop too tight to trade (a rejected signal)."""
    rec = lg.LedgerRecorder()
    trades: list[Trade] = []
    days: list[str] = []
    for sym in symbols:
        sessions, plan = [], {}
        for k in range(n_sessions):
            day = str((pd.Timestamp("2026-06-01") + pd.Timedelta(days=k)).date())
            kind = k % 4
            if kind == 0:
                rows = [FLAT, FLAT, (100, 101.5, 99.5, 100)] + [FLAT] * 9
                plan[12 * k + 1] = (1, 99.0, 101.0)
            elif kind == 1:
                rows = [FLAT, FLAT, (100, 100.5, 98.5, 99)] + [FLAT] * 9
                plan[12 * k + 1] = (1, 99.0 - 0.1 * (k % 3), 101.0)
            elif kind == 2:
                rows = [FLAT, FLAT, FLAT, (98, 98.5, 97, 98)] + [FLAT] * 8
                plan[12 * k + 1] = (1, 99.0, 101.0)
            else:
                rows = [FLAT] * 12
                plan[12 * k + 1] = (1, 99.9, 101.0)  # 0.1% stop: below min_sl_pct
            sessions.append((day, rows))
            days.append(day)
        df = make_frame(sessions)
        trades += simulate(Ctx(df, sym), Script(plan), None, SESSION_RUN, rec=rec)
    return trades, rec, sorted(set(days))


@pytest.fixture(scope="module")
def run_data():
    trades, rec, days = synth()
    return trades, rec, rec.to_frames(), days


def copy_frames(frames):
    return {k: v.copy() for k, v in frames.items()}


def ledger_execution(run_data, **over):
    trades, rec, frames, days = run_data
    exe = base._execution()
    trades = over.pop("trades", trades)
    fields = dict(
        exe.__dict__,
        **statutory(list(trades), 16.0, capital=100000.0),
        days=days,
        data={**exe.data, "as_of": days[-1]},
        period={"start": days[0], "end": days[-1]},
        symbols=["AAA", "BBB"],
        ledgers=copy_frames(frames),
    )
    fields.update(over)
    return rr.Execution(**fields)


def record(root_, run_data, **over):
    exe = ledger_execution(run_data, **over)
    path = raw._record(root_, execute=lambda key: exe)
    return raw._folder(path)


def report_of(folder):
    return json.loads((folder / "report" / "report.json").read_text(encoding="utf-8"))


def read(folder, name):
    return trades_io.read_ledger_parquet(folder / "evidence" / f"{name}.parquet")


def row(profile, gate):
    rows = [r for d in profile["dimensions"] for r in d["rows"] if r["gate"] == gate]
    assert len(rows) == 1
    return rows[0]


def evaluate(trades, **kw):
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


# ---- Execution -----------------------------------------------------------------------------


class TestExecutionDefaults:
    def test_new_fields_default_to_none(self):
        exe = rr.Execution(trades=[], params={}, data={}, period={}, costs={}, days=[])
        assert exe.ledgers is None
        assert exe.cost_components is None

    def test_fixture_run_is_real(self, run_data):
        trades, rec, frames, _ = run_data
        assert len(trades) >= 30
        assert rec.reconcile() == []
        assert {t.reason for t in trades} >= {"TP", "SL", "SL_GAP"}
        assert (~frames["signals"]["accepted"]).sum() > 0


class _FakeStrategy:
    def prepare_key(self, p):
        return ("k",)


class TestHeadlineOnly:
    def test_recorder_reaches_only_the_single_headline_call(self, monkeypatch):
        from signal_engine.backtest import __main__ as cli
        from signal_engine.backtest import data, harness

        calls: list[dict] = []
        built: list[object] = []
        df = make_frame([("2026-06-04", [FLAT, FLAT, (100, 101.5, 99.5, 100)] + [FLAT] * 9)])

        @dataclasses.dataclass
        class Params:
            x: int = 1

        class Strat:
            run_overrides: dict = {}

        class FakeBacktest:
            def __init__(self, strategy, frames, run, **kw):
                built.append(self)
                self.run, self.days = run, [pd.Timestamp("2026-06-04").date()]

            def trades(self, p, window="all", run=None, recorder=None):
                calls.append({"window": window, "recorder": recorder})
                return simulate(
                    Ctx(df, "AAA"), Script({1: (1, 99.0, 101.0)}), None, RUN, rec=recorder
                )

        monkeypatch.setattr(cli, "_register", lambda: None)
        monkeypatch.setattr(cli, "REGISTRY", {"k": (Strat, Params, "intraday")})
        res = frames_resolution(intraday={"AAA": df})  # plan Phase 4: bars via the resolution
        monkeypatch.setattr(harness, "Backtest", FakeBacktest)
        monkeypatch.setattr(rr, "random_entry_baseline_intraday", lambda *a, **k: None)
        monkeypatch.setattr(rr, "deployed_cost_model", deployed_model)

        exe = rr.execute_backtest("k", dataset=res)

        assert len(built) == 1 and len(calls) == 1
        assert isinstance(calls[0]["recorder"], lg.LedgerRecorder)
        assert set(exe.ledgers) == {"signals", "orders", "fills", "exclusions"}
        assert len(exe.ledgers["signals"]) >= 1
        assert list(exe.cost_components.columns) == list(trades_io.COST_COMPONENT_COLUMNS)
        assert len(exe.cost_components) == len(exe.trades) == 1

    def test_no_cost_model_refuses_the_run(self, monkeypatch):
        from signal_engine.backtest import __main__ as cli
        from signal_engine.backtest import data, harness

        df = make_frame([("2026-06-04", [FLAT] * 6)])

        @dataclasses.dataclass
        class Params:
            x: int = 1

        class Strat:
            run_overrides: dict = {}

        class FakeBacktest:
            def __init__(self, strategy, frames, run, **kw):
                self.days = [pd.Timestamp("2026-06-04").date()]

            def trades(self, p, window="all", run=None, recorder=None):
                return simulate(Ctx(df, "AAA"), Script({}), None, RUN, rec=recorder)

        monkeypatch.setattr(cli, "_register", lambda: None)
        monkeypatch.setattr(cli, "REGISTRY", {"k": (Strat, Params, "intraday")})
        res = frames_resolution(intraday={"AAA": df})  # plan Phase 4: bars via the resolution
        monkeypatch.setattr(harness, "Backtest", FakeBacktest)

        def unavailable(*a, **k):
            raise KeyError("config.yaml is missing sizing.sandbox_capital")

        monkeypatch.setattr(rr, "deployed_cost_model", unavailable)
        with pytest.raises(rr.CostModelUnavailable, match="sandbox_capital"):
            rr.execute_backtest("k", dataset=res)


# ---- the artifact ----------------------------------------------------------------------------


class TestEvidenceFiles:
    def test_four_ledgers_are_written_beside_the_seven_required_files(self, root, run_data):
        folder = record(root, run_data)
        found = {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}
        assert found == raw.FILES | LEDGER_FILES

    def test_schemas_are_exactly_the_plan_table(self, root, run_data):
        folder = record(root, run_data)
        for name, columns in PLAN_COLUMNS.items():
            assert list(read(folder, name).columns) == columns
        trades = trades_io.read_trades_parquet(folder / "evidence" / "trades.parquet")
        assert list(trades.columns) == trades_io.TRADE_COLUMNS_EXTENDED

    def test_ledger_rows_round_trip(self, root, run_data):
        _, _, frames, _ = run_data
        folder = record(root, run_data)
        for name in PLAN_COLUMNS:
            got = read(folder, name)
            assert len(got) == len(frames[name])
        sig = read(folder, "signals")
        assert sig["signal_id"].tolist() == frames["signals"]["signal_id"].tolist()
        assert str(sig["bar_time"].dtype) == f"datetime64[ns, {IST}]"
        assert sig["accepted"].dtype == bool

    def test_files_are_listed_in_the_manifest_and_verified(self, root, run_data):
        folder = record(root, run_data)
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        assert LEDGER_FILES <= set(manifest["artifacts"])
        assert mf.verify_artifacts(folder) == []
        for rel in LEDGER_FILES:
            assert (
                manifest["artifacts"][rel]
                == hashlib.sha256((folder / rel).read_bytes()).hexdigest()
            )

    def test_tampered_ledger_is_caught_by_verification(self, root, run_data):
        folder = record(root, run_data)
        target = folder / "evidence" / "fills.parquet"
        target.chmod(0o644)
        target.write_bytes(target.read_bytes() + b"x")
        assert any("fills.parquet" in p for p in mf.verify_artifacts(folder))

    def test_bytes_are_deterministic_across_runs(self, root, run_data):
        first = record(root, run_data)
        second = record(root, run_data)
        assert first != second
        for rel in LEDGER_FILES | {"evidence/trades.parquet"}:
            assert (first / rel).read_bytes() == (second / rel).read_bytes()

    def test_run_without_ledgers_is_unchanged(self, root):
        plain = rr.Execution(**{**base._execution().__dict__, "cost_components": None})
        path = raw._record(root, execute=lambda key: plain)
        folder = raw._folder(path)
        found = {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}
        assert found == raw.FILES
        trades = trades_io.read_trades_parquet(folder / "evidence" / "trades.parquet")
        assert list(trades.columns) == trades_io.TRADE_COLUMNS
        assert report_of(folder)["ledgers"] is None
        assert mf.verify_artifacts(folder) == []

    def test_empty_ledger_frames_keep_the_full_schema(self, root, run_data):
        empty = lg.LedgerRecorder().to_frames()
        folder = record(root, run_data, ledgers=empty)
        for name, columns in PLAN_COLUMNS.items():
            got = read(folder, name)
            assert len(got) == 0 and list(got.columns) == columns
        assert mf.verify_artifacts(folder) == []

    def test_trades_extended_columns_carry_lineage_and_components(self, root, run_data):
        trades, _, _, _ = run_data
        folder = record(root, run_data)
        table = trades_io.read_trades_parquet(folder / "evidence" / "trades.parquet")
        assert table["signal_id"].tolist() == [t.signal_id for t in trades]
        comps = decompose_trades(trades, MODEL)
        assert table["modeled_cost_bps"].tolist() == pytest.approx(
            comps["modeled_cost_bps"].tolist()
        )
        parts = [c for c in trades_io.COST_COMPONENT_COLUMNS if c != "modeled_cost_bps"]
        assert table[parts].sum(axis=1).tolist() == pytest.approx(
            table["modeled_cost_bps"].tolist()
        )

    def test_lineage_without_components_still_writes_extended_lineage(self, root, run_data):
        folder = record(root, run_data, cost_components=None)
        cols = list(trades_io.read_trades_parquet(folder / "evidence" / "trades.parquet").columns)
        assert cols == trades_io.TRADE_COLUMNS + trades_io.LINEAGE_COLUMNS


class TestFrameParquetBytes:
    def test_same_frame_same_bytes_and_no_pandas_metadata(self):
        frame = pd.DataFrame({"a": [1, 2], "b": ["x", None]})
        one, two = trades_io.frame_parquet_bytes(frame), trades_io.frame_parquet_bytes(frame.copy())
        assert one == two
        assert pq.read_schema(io.BytesIO(one)).metadata is None

    def test_uses_the_trades_writer_options(self):
        frame = pd.DataFrame({"a": [1.0, 2.0]})
        meta = pq.ParquetFile(io.BytesIO(trades_io.frame_parquet_bytes(frame))).metadata
        assert meta.row_group(0).column(0).compression == "ZSTD"

    def test_ledger_schema_applies_to_an_empty_frame(self):
        empty = lg.LedgerRecorder().to_frames()["fills"]
        data = trades_io.frame_parquet_bytes(empty, trades_io.LEDGER_SCHEMAS["fills"])
        table = pq.read_table(io.BytesIO(data))
        assert table.num_rows == 0
        assert table.column_names == PLAN_COLUMNS["fills"]
        assert str(table.schema.field("fill_price").type) == "double"
        assert str(table.schema.field("leg").type) == "int64"


# ---- report.json ledger summary --------------------------------------------------------------


class TestLedgerSummary:
    def test_summary_equals_independent_counts(self, root, run_data):
        trades, _, frames, _ = run_data
        summary = report_of(record(root, run_data))["ledgers"]
        sig, orders, fills, exc = (frames[k] for k in ("signals", "orders", "fills", "exclusions"))
        by_reason = {k: int(v) for k, v in exc.groupby("reason_code")["count"].sum().items()}
        assert summary["signals"] == len(sig)
        assert summary["accepted"] == int(sig["accepted"].sum())
        assert summary["excluded_by_reason"] == by_reason
        assert summary["orders"] == len(orders)
        assert summary["filled"] == int((orders["status"] == "FILLED").sum())
        assert summary["dropped"] == int((orders["status"] == "DROPPED").sum())
        assert summary["fills"] == len(fills)
        assert summary["trades"] == len(trades)
        assert summary["reconcile_problems"] == 0
        assert summary["problems"] == []

    def test_reconcile_problems_are_recorded(self, root, run_data):
        frames = copy_frames(run_data[2])
        exc = frames["exclusions"]
        frames["exclusions"] = exc[exc["reason_code"] != "STOP_TOO_TIGHT"]
        summary = report_of(record(root, run_data, ledgers=frames))["ledgers"]
        assert summary["reconcile_problems"] >= 1
        assert len(summary["problems"]) == summary["reconcile_problems"]


# ---- lineage walk ----------------------------------------------------------------------------


class TestLineageWalk:
    def test_every_trade_row_reaches_its_signal_order_and_fills(self, root, run_data):
        folder = record(root, run_data)
        trades = trades_io.read_trades_parquet(folder / "evidence" / "trades.parquet")
        sig, orders, fills = (read(folder, k) for k in ("signals", "orders", "fills"))
        sig_ix = sig.set_index("signal_id")
        ord_ix = orders.set_index("order_id")
        fill_ix = fills.set_index("fill_id")
        assert len(trades) >= 30
        for t in trades.itertuples():
            s = sig_ix.loc[t.signal_id]
            o = ord_ix.loc[t.order_id]
            assert bool(s["accepted"]) and s["symbol"] == t.symbol
            assert o["signal_id"] == t.signal_id and o["status"] == "FILLED"
            entry, exit_ = fill_ix.loc[t.entry_fill_id], fill_ix.loc[t.exit_fill_id]
            assert entry["kind"] == "entry" and exit_["kind"] == "exit"
            assert entry["order_id"] == exit_["order_id"] == t.order_id
            assert entry["position_id"] == exit_["position_id"] == t.position_id
            assert entry["fill_time"] == t.entry_time and exit_["fill_time"] == t.exit_time
            assert entry["fill_price"] == pytest.approx(t.entry_price)
            assert exit_["exit_reason"] == t.exit_reason

    def test_every_rejected_signal_has_an_exclusion_row(self, root, run_data):
        folder = record(root, run_data)
        sig, exc = read(folder, "signals"), read(folder, "exclusions")
        rejected = sig[~sig["accepted"]]
        assert len(rejected) > 0
        assert set(rejected["exclusion_id"]) <= set(exc["exclusion_id"])
        assert set(rejected["exclusion_id"].map(exc.set_index("exclusion_id")["reason_code"])) == {
            "STOP_TOO_TIGHT"
        }


# ---- evaluation plumbing ---------------------------------------------------------------------


class TestNewGatesNeedLedgers:
    def test_without_ledgers_the_four_gates_stay_not_evaluated(self, run_data):
        profile = evaluate(run_data[0])
        for gate in ("G0.5", "G6.1", "G6.5", "G1.6", "G6.8"):
            assert row(profile, gate)["status"] == "not_evaluated"

    def test_evaluate_accepts_ledgers_and_session(self, run_data):
        trades, _, frames, _ = run_data
        profile = evaluate(trades, ledgers=frames, session=SESSION)
        assert row(profile, "G0.5")["status"] == "pass"
        assert row(profile, "G6.1")["status"] == "pass"
        assert row(profile, "G6.5")["status"] == "report"
        assert row(profile, "G1.6")["status"] == "report"
        assert row(profile, "G6.8")["status"] == "pass"

    def test_ledgers_given_does_not_change_other_rows(self, run_data):
        trades, _, frames, _ = run_data
        plain = evaluate(trades)
        full = evaluate(trades, ledgers=frames, session=SESSION)
        for d_plain, d_full in zip(plain["dimensions"], full["dimensions"], strict=True):
            for a, b in zip(d_plain["rows"], d_full["rows"], strict=True):
                if a["gate"] not in {"G0.5", "G6.1", "G6.5", "G1.6", "G6.8"}:
                    assert a == b

    def test_build_run_record_threads_ledgers_and_session(self, run_data):
        from signal_engine.backtest.tearsheet import build_run_record

        trades, _, frames, days = run_data
        rec = build_run_record(
            trades,
            run_id="2026-10-02-x-v1",
            run_date="2026-10-02",
            code_version="c",
            data={"source": "s", "as_of": days[-1], "universe": "u", "bar_size": "5m"},
            period={"start": days[0], "end": days[-1]},
            costs={"model": "m", "round_trip_bps": 16.0},
            ledgers=frames,
            session=SESSION,
        )
        assert row(rec["evaluation"], "G0.5")["status"] == "pass"

    def test_swing_keeps_g61_not_applicable(self, run_data):
        trades, _, frames, _ = run_data
        profile = evaluate(trades, horizon="swing", ledgers=frames, session=SESSION)
        assert row(profile, "G6.1")["status"] == "not_applicable"
        assert row(profile, "G0.5")["status"] == "pass"


class TestG05:
    def test_pass_evidence_counts_by_reason_code(self, run_data):
        trades, _, frames, _ = run_data
        r = row(evaluate(trades, ledgers=frames, session=SESSION), "G0.5")
        assert r["status"] == "pass"
        assert "STOP_TOO_TIGHT" in r["evidence"]
        assert r["value"] == 0

    def test_dropping_an_exclusion_row_fails_and_names_the_problem(self, run_data):
        trades, _, frames, _ = run_data
        bad = copy_frames(frames)
        bad["exclusions"] = bad["exclusions"][bad["exclusions"]["reason_code"] != "STOP_TOO_TIGHT"]
        r = row(evaluate(trades, ledgers=bad, session=SESSION), "G0.5")
        assert r["status"] == "fail"
        assert "rejected without an exclusion row" in r["evidence"]
        assert r["value"] >= 1

    def test_unknown_reason_code_fails(self, run_data):
        trades, _, frames, _ = run_data
        bad = copy_frames(frames)
        bad["exclusions"].loc[bad["exclusions"].index[0], "reason_code"] = "MADE_UP"
        r = row(evaluate(trades, ledgers=bad, session=SESSION), "G0.5")
        assert r["status"] == "fail" and "MADE_UP" in r["evidence"]

    def test_a_trade_without_an_entry_position_fails(self, run_data):
        trades, _, frames, _ = run_data
        fewer = list(trades)[:-1]
        r = row(evaluate(fewer, ledgers=frames, session=SESSION), "G0.5")
        assert r["status"] == "fail" and "trades" in r["evidence"]

    def test_order_without_status_or_with_orphan_signal_fails(self, run_data):
        trades, _, frames, _ = run_data
        bad = copy_frames(frames)
        bad["orders"] = bad["orders"].iloc[1:]
        r = row(evaluate(trades, ledgers=bad, session=SESSION), "G0.5")
        assert r["status"] == "fail"

    def test_reconcile_frames_is_empty_for_a_real_run(self, run_data):
        trades, _, frames, _ = run_data
        assert ev.reconcile_frames(frames, trades) == []

    def test_empty_ledgers_and_no_trades_reconcile(self):
        assert ev.reconcile_frames(lg.LedgerRecorder().to_frames(), []) == []


class TestG61:
    def _shift(self, frames, name, col, mask_col, mask_val, minutes_to):
        bad = copy_frames(frames)
        df = bad[name]
        idx = df.index[df[mask_col] == mask_val][0]
        stamp = df.loc[idx, col]
        df.loc[idx, col] = stamp.normalize() + pd.Timedelta(minutes=minutes_to)
        return bad, df.loc[idx, "fill_id"]

    def test_pass_reports_latest_exit_and_window(self, run_data):
        trades, _, frames, _ = run_data
        r = row(evaluate(trades, ledgers=frames, session=SESSION), "G6.1")
        assert r["status"] == "pass"
        assert "09:20-14:30" in r["threshold"] or "09:20-14:30" in r["evidence"]

    def test_exit_after_latest_exit_bar_fails_and_names_the_fill(self, run_data):
        trades, _, frames, _ = run_data
        # stamp 14:55 closes at 15:00, which is later than 15:00 - 5 minutes
        bad, fid = self._shift(frames, "fills", "fill_time", "kind", "exit", 14 * 60 + 55)
        r = row(evaluate(trades, ledgers=bad, session=SESSION), "G6.1")
        assert r["status"] == "fail" and fid in r["evidence"]

    def test_exit_on_the_last_allowed_bar_passes(self, run_data):
        trades, _, frames, _ = run_data
        ok, _ = self._shift(frames, "fills", "fill_time", "kind", "exit", 14 * 60 + 50)
        assert row(evaluate(trades, ledgers=ok, session=SESSION), "G6.1")["status"] == "pass"

    def _signal_at(self, frames, minutes, symbol="AAA"):
        """Move the signal bar of the first order of `symbol`; returns frames and entry fill id."""
        bad = copy_frames(frames)
        orders = bad["orders"]
        idx = orders.index[(orders["symbol"] == symbol) & (orders["status"] == "FILLED")][0]
        stamp = orders.loc[idx, "submit_time"]
        orders.loc[idx, "submit_time"] = stamp.normalize() + pd.Timedelta(minutes=minutes)
        fills = bad["fills"]
        fid = fills.loc[
            (fills["order_id"] == orders.loc[idx, "order_id"]) & (fills["kind"] == "entry"),
            "fill_id",
        ].iloc[0]
        return bad, fid

    def test_entry_signal_before_window_fails(self, run_data):
        trades, _, frames, _ = run_data
        bad, fid = self._signal_at(frames, 9 * 60 + 15)
        r = row(evaluate(trades, ledgers=bad, session=SESSION), "G6.1")
        assert r["status"] == "fail" and fid in r["evidence"]

    def test_signal_bar_boundaries_follow_the_engine_cutoff(self, run_data):
        trades, _, frames, _ = run_data
        # engine: a signal is blocked when its bar start >= cutoff, so 14:25 passes, 14:30 fails
        last, _ = self._signal_at(frames, 14 * 60 + 25)
        assert row(evaluate(trades, ledgers=last, session=SESSION), "G6.1")["status"] == "pass"
        at_cutoff, fid = self._signal_at(frames, 14 * 60 + 30)
        r = row(evaluate(trades, ledgers=at_cutoff, session=SESSION), "G6.1")
        assert r["status"] == "fail" and fid in r["evidence"]
        first, _ = self._signal_at(frames, 9 * 60 + 20)
        assert row(evaluate(trades, ledgers=first, session=SESSION), "G6.1")["status"] == "pass"

    def test_a_fill_on_the_cutoff_stamp_is_allowed_when_its_signal_bar_is_before_it(self, run_data):
        trades, _, frames, _ = run_data
        bad = copy_frames(frames)
        fills = bad["fills"]
        idx = fills.index[fills["kind"] == "entry"][0]
        fills.loc[idx, "fill_time"] = fills.loc[idx, "fill_time"].normalize() + pd.Timedelta(
            hours=14, minutes=30
        )
        # the signal bar is still the ledger's early one: the fill stamp alone is not the rule
        assert row(evaluate(trades, ledgers=bad, session=SESSION), "G6.1")["status"] == "pass"

    def test_exit_span_uses_each_symbols_own_bar_size(self, run_data):
        trades, _, frames, _ = run_data
        bad = copy_frames(frames)
        fills = bad["fills"]
        for sym, minute in (("AAA", 14 * 60 + 50), ("BBB", 14 * 60 + 40)):
            idx = fills.index[(fills["kind"] == "exit") & (fills["symbol"] == sym)][0]
            fills.loc[idx, "fill_time"] = fills.loc[idx, "fill_time"].normalize() + pd.Timedelta(
                minutes=minute
            )
        mixed = {**SESSION, "bar_minutes_by_symbol": {"BBB": 15}}
        # AAA 5m exit bar 14:50 closes 14:55 (ok); BBB 15m exit bar 14:40 closes 14:55 (ok)
        assert row(evaluate(trades, ledgers=bad, session=mixed), "G6.1")["status"] == "pass"
        idx = fills.index[(fills["kind"] == "exit") & (fills["symbol"] == "BBB")][0]
        fills.loc[idx, "fill_time"] = fills.loc[idx, "fill_time"].normalize() + pd.Timedelta(
            hours=14, minutes=45
        )
        r = row(evaluate(trades, ledgers=bad, session=mixed), "G6.1")
        assert r["status"] == "fail" and fills.loc[idx, "fill_id"] in r["evidence"]
        # the same 14:45 stamp is fine for a 5-minute BBB
        assert row(evaluate(trades, ledgers=bad, session=SESSION), "G6.1")["status"] == "pass"

    def test_fifteen_minute_run(self, run_data):
        trades, _, frames, _ = run_data
        s15 = {**SESSION, "bar_minutes": 15}
        bad = copy_frames(frames)
        fills = bad["fills"]
        idx = fills.index[fills["kind"] == "exit"][0]
        base_day = fills.loc[idx, "fill_time"].normalize()
        fills.loc[idx, "fill_time"] = base_day + pd.Timedelta(hours=14, minutes=40)
        assert row(evaluate(trades, ledgers=bad, session=s15), "G6.1")["status"] == "pass"
        fills.loc[idx, "fill_time"] = base_day + pd.Timedelta(hours=14, minutes=45)
        r = row(evaluate(trades, ledgers=bad, session=s15), "G6.1")
        assert r["status"] == "fail" and fills.loc[idx, "fill_id"] in r["evidence"]

    def test_first_offender_is_named(self, run_data):
        trades, _, frames, _ = run_data
        bad = copy_frames(frames)
        exits = bad["fills"].index[bad["fills"]["kind"] == "exit"]
        for ix in exits[:2]:
            bad["fills"].loc[ix, "fill_time"] = bad["fills"].loc[
                ix, "fill_time"
            ].normalize() + pd.Timedelta(hours=15, minutes=10)
        first = bad["fills"].loc[exits[0], "fill_id"]
        r = row(evaluate(trades, ledgers=bad, session=SESSION), "G6.1")
        assert first in r["evidence"]

    def test_no_entry_window_only_checks_the_exit_bar(self, run_data):
        trades, _, frames, _ = run_data
        s = {**SESSION, "entry_window": None}
        assert row(evaluate(trades, ledgers=frames, session=s), "G6.1")["status"] == "pass"

    def test_missing_session_is_not_evaluated(self, run_data):
        trades, _, frames, _ = run_data
        assert row(evaluate(trades, ledgers=frames), "G6.1")["status"] == "not_evaluated"
        s = {**SESSION, "mis_squareoff_min": None}
        assert row(evaluate(trades, ledgers=frames, session=s), "G6.1")["status"] == "not_evaluated"

    def test_no_fills_is_not_evaluated(self):
        empty = lg.LedgerRecorder().to_frames()
        assert (
            row(evaluate([], ledgers=empty, session=SESSION), "G6.1")["status"] == "not_evaluated"
        )


def _trade(reason, entry, sl, exit_, direction=1):
    risk = abs(entry - sl)
    return Trade(
        symbol="AAA",
        day=pd.Timestamp("2026-06-01").date(),
        direction=direction,
        tag="t",
        entry_time=pd.Timestamp("2026-06-01 09:25", tz=IST),
        entry=entry,
        signal_price=entry,
        sl=sl,
        tp=0.0,
        risk=risk,
        exit_time=pd.Timestamp("2026-06-01 10:00", tz=IST),
        exit=exit_,
        reason=reason,
        r_gross=0.0,
        r_net=0.0,
    )


class TestG65:
    def test_share_and_mean_loss_beyond_the_stop(self, run_data):
        _, _, frames, _ = run_data
        trades = [
            _trade("SL_GAP", 100.0, 99.0, 98.0),  # 1.0R beyond the stop
            _trade("SL_GAP_ENTRY", 100.0, 99.0, 98.5, direction=1),  # 0.5R
            _trade("SL", 100.0, 99.0, 99.0),
            _trade("TP", 100.0, 99.0, 101.0),
        ]
        r = row(evaluate(trades, ledgers=frames, session=SESSION), "G6.5")
        assert r["status"] == "report"
        assert r["value"] == pytest.approx(0.5)
        assert "2 of 4" in r["evidence"] and "0.750R" in r["evidence"]

    def test_short_trades_are_measured_on_their_own_side(self, run_data):
        _, _, frames, _ = run_data
        trades = [_trade("SL_GAP", 100.0, 101.0, 103.0, direction=-1)]  # 2R beyond
        r = row(evaluate(trades, ledgers=frames, session=SESSION), "G6.5")
        assert r["value"] == pytest.approx(1.0)
        assert "2.000R" in r["evidence"]

    def test_no_gap_exits(self, run_data):
        _, _, frames, _ = run_data
        r = row(
            evaluate([_trade("TP", 100.0, 99.0, 101.0)], ledgers=frames, session=SESSION), "G6.5"
        )
        assert r["status"] == "report" and r["value"] == 0.0
        assert "0 of 1" in r["evidence"]

    def test_on_the_real_run(self, run_data):
        trades, _, frames, _ = run_data
        gap = [t for t in trades if t.reason in ("SL_GAP", "SL_GAP_ENTRY")]
        r = row(evaluate(trades, ledgers=frames, session=SESSION), "G6.5")
        assert r["value"] == pytest.approx(len(gap) / len(trades), abs=1e-4)
        assert f"{len(gap)} of {len(trades)}" in r["evidence"]


def _signals(widths, price=100.0, accepted=True):
    n = len(widths)
    return pd.DataFrame(
        {
            "signal_id": [f"S{i}" for i in range(n)],
            "symbol": ["AAA"] * n,
            "direction": [1] * n,
            "signal_price": [price] * n,
            "risk": [w / 100.0 * price for w in widths],
            "accepted": [accepted] * n,
            "exclusion_id": [None] * n,
        }
    )


class TestG16:
    def test_buckets_and_counts(self):
        table = ev.stop_width_table(_signals([0.1, 0.25, 0.3, 0.4, 0.6, 0.9, 1.5]))
        assert [r["count"] for r in table] == [1, 1, 2, 1, 1, 1]
        assert len(table) == 6
        assert table[0]["bucket"].startswith("<")
        assert table[-1]["bucket"].startswith(">=")

    def test_table_ignores_rejected_and_invalid_rows(self):
        sig = pd.concat([_signals([0.4, 0.4]), _signals([0.1], accepted=False)])
        sig.loc[sig.index[0], "risk"] = 0.0
        assert sum(r["count"] for r in ev.stop_width_table(sig)) == 1

    def test_fixed_floor_is_flagged_with_the_table_and_stays_a_report(self, run_data):
        trades, _, frames, _ = run_data
        frames = copy_frames(frames)
        frames["signals"] = _signals([0.3] * 8 + [0.5, 0.8])
        r = row(evaluate(trades, ledgers=frames, session=SESSION), "G1.6")
        assert r["status"] == "report"
        assert r["value"] == pytest.approx(0.8)
        assert "fixed floor" in r["evidence"] and "0.30%" in r["evidence"]
        assert "0.30-0.50%" in r["evidence"]

    def test_spread_widths_are_not_flagged(self, run_data):
        trades, _, frames, _ = run_data
        frames = copy_frames(frames)
        frames["signals"] = _signals([0.21, 0.33, 0.45, 0.6, 0.7, 0.9, 1.2, 1.4])
        r = row(evaluate(trades, ledgers=frames, session=SESSION), "G1.6")
        assert r["status"] == "report" and "fixed floor" not in r["evidence"]

    def test_no_accepted_signals_is_not_evaluated(self, run_data):
        trades, _, frames, _ = run_data
        frames = copy_frames(frames)
        frames["signals"] = _signals([], accepted=True)
        assert (
            row(evaluate(trades, ledgers=frames, session=SESSION), "G1.6")["status"]
            == "not_evaluated"
        )


# ---- reproduction with ledgers on --------------------------------------------------------------


def _reproduce(folder, root_, execute):
    return rp.reproduce(
        folder,
        root=root_,
        execute=execute,
        supported=SUPPORTED,
        git_state_fn=raw._git(raw.CLEAN),
        dataset_fingerprint_fn=lambda s, i, a: "d" * 64,
        now=raw.NOW,
    )


class TestReproduceWithLedgers:
    def test_identical_rerun_passes_with_extended_columns_compared(self, root, run_data):
        folder = record(root, run_data)
        exe = ledger_execution(run_data)
        report = _reproduce(folder, root, lambda key, settings=None, end=None: exe)
        assert report.reproducible is True, report.reasons
        assert report.compared_trades == len(run_data[0])

    def test_changed_cost_component_is_a_mismatch(self, root, run_data):
        folder = record(root, run_data)
        comps = decompose_trades(run_data[0], MODEL)
        comps.loc[3, "cost_stt_bps"] += 0.5
        exe = ledger_execution(run_data, cost_components=comps)
        report = _reproduce(folder, root, lambda key, settings=None, end=None: exe)
        assert report.reproducible is False
        assert any("cost_stt_bps" in r for r in report.reasons)

    def test_changed_lineage_id_is_a_mismatch(self, root, run_data):
        folder = record(root, run_data)
        trades = list(run_data[0])
        trades[2] = dataclasses.replace(trades[2], signal_id=trades[2].signal_id + "x")
        exe = ledger_execution(run_data, trades=trades)
        report = _reproduce(folder, root, lambda key, settings=None, end=None: exe)
        assert report.reproducible is False
        assert any("signal_id" in r for r in report.reasons)

    def test_identical_ledgers_are_reported_as_matching(self, root, run_data):
        folder = record(root, run_data)
        exe = ledger_execution(run_data)
        report = _reproduce(folder, root, lambda key, settings=None, end=None: exe)
        assert report.checks["ledgers_match"] is True, report.reasons

    def test_a_changed_ledger_row_is_a_mismatch_even_when_the_trades_match(self, root, run_data):
        folder = record(root, run_data)
        frames = copy_frames(run_data[2])
        fills = frames["fills"].copy()
        fills.loc[fills.index[0], "fill_price"] += 0.05
        frames["fills"] = fills
        exe = ledger_execution(run_data, ledgers=frames)
        report = _reproduce(folder, root, lambda key, settings=None, end=None: exe)
        assert report.checks["trades_match"] is True
        assert report.checks["ledgers_match"] is False
        assert report.reproducible is False
        assert any("fills" in r for r in report.reasons)

    def test_a_recorded_ledger_the_rerun_lacks_is_a_mismatch(self, root, run_data):
        folder = record(root, run_data)
        exe = ledger_execution(run_data, ledgers=None)
        report = _reproduce(folder, root, lambda key, settings=None, end=None: exe)
        assert report.checks["ledgers_match"] is False

    def test_run_recorded_without_ledgers_still_reproduces_from_a_ledger_rerun(
        self, root, run_data
    ):
        plain = rr.Execution(
            **{**ledger_execution(run_data).__dict__, "ledgers": None, "cost_components": None}
        )
        stripped = [
            dataclasses.replace(
                t,
                signal_id=None,
                order_id=None,
                entry_fill_id=None,
                exit_fill_id=None,
                position_id=None,
            )
            for t in plain.trades
        ]
        plain = rr.Execution(**{**plain.__dict__, "trades": stripped})
        path = raw._record(root, execute=lambda key: plain)
        folder = raw._folder(path)
        exe = ledger_execution(run_data)
        report = _reproduce(folder, root, lambda key, settings=None, end=None: exe)
        assert report.checks["trades_match"] is True, report.reasons
        assert report.checks["ledgers_match"] is True


class TestRecordRunPassesSession:
    def test_session_comes_from_the_frozen_config(self, root, run_data):
        folder = record(root, run_data)
        profile = report_of(folder)["evaluation"]
        r = row(profile, "G6.1")
        assert r["status"] == "pass"
        assert r["threshold"]

    def test_run_without_ledgers_has_the_gates_not_evaluated(self, root):
        path = raw._record(root, execute=lambda key: base._execution())
        profile = report_of(raw._folder(path))["evaluation"]
        for gate in ("G0.5", "G6.1", "G6.5", "G1.6", "G6.8"):
            assert row(profile, gate)["status"] == "not_evaluated"


# ---- review fixes: reconcile problems, cost residual, reproduce columns -----------------


class TestLedgerProblemsCarried:
    def test_execution_default_is_empty(self):
        assert base._execution().ledger_problems == []

    def test_clean_run_passes_g05_and_summary_is_clean(self, root, run_data):
        folder = record(root, run_data)
        assert report_of(folder)["ledgers"]["reconcile_problems"] == 0
        assert row(report_of(folder)["evaluation"], "G0.5")["status"] == "pass"

    def test_recorder_disagreement_fails_g05_and_is_in_the_summary(self, root, run_data):
        problems = ["count: signals 59 != recorded 60"]
        folder = record(root, run_data, ledger_problems=problems)
        rep = report_of(folder)
        assert rep["ledgers"]["reconcile_problems"] == 1
        assert problems[0] in rep["ledgers"]["problems"]
        g = row(rep["evaluation"], "G0.5")
        assert g["status"] == "fail" and problems[0] in g["evidence"]

    def test_evaluator_fails_on_ledger_problems_even_when_frames_reconcile(self, run_data):
        trades, _, frames, _ = run_data
        r = row(
            evaluate(trades, ledgers=frames, ledger_problems=["count: orders 1 != recorded 2"]),
            "G0.5",
        )
        assert r["status"] == "fail" and "orders 1 != recorded 2" in r["evidence"]

    def test_execute_backtest_reconciles_against_the_buffers(self, monkeypatch):
        from signal_engine.backtest import __main__ as cli
        from signal_engine.backtest import data, harness

        df = make_frame([("2026-06-04", [FLAT, FLAT, (100, 101.5, 99.5, 100)] + [FLAT] * 9)])

        @dataclasses.dataclass
        class Params:
            x: int = 1

        class Strat:
            run_overrides: dict = {}

        class FakeBacktest:
            def __init__(self, strategy, frames, run, **kw):
                self.days = [pd.Timestamp("2026-06-04").date()]

            def trades(self, p, window="all", run=None, recorder=None):
                return simulate(
                    Ctx(df, "AAA"), Script({1: (1, 99.0, 101.0)}), None, RUN, rec=recorder
                )

        monkeypatch.setattr(cli, "_register", lambda: None)
        monkeypatch.setattr(cli, "REGISTRY", {"k": (Strat, Params, "intraday")})
        res = frames_resolution(intraday={"AAA": df})  # plan Phase 4: bars via the resolution
        monkeypatch.setattr(harness, "Backtest", FakeBacktest)
        monkeypatch.setattr(rr, "random_entry_baseline_intraday", lambda *a, **k: None)
        monkeypatch.setattr(rr, "deployed_cost_model", deployed_model)
        assert rr.execute_backtest("k", dataset=res).ledger_problems == []

        original = lg.LedgerRecorder.to_frames

        def tampered(self):
            frames = original(self)
            frames["orders"] = frames["orders"].iloc[1:]
            return frames

        monkeypatch.setattr(lg.LedgerRecorder, "to_frames", tampered)
        problems = rr.execute_backtest("k", dataset=res).ledger_problems
        assert problems and any("orders" in p for p in problems)


class _ExtraKeySchedule:
    """A schedule with a charge key the model does not know and its own slippage line."""

    def breakdown(self, buy_value, sell_value, orders=0):
        parts = {
            "brokerage": 20.0,
            "stt": 25.0,
            "exchange_txn": 3.0,
            "tax": 4.0,
            "sebi": 0.1,
            "stamp_duty": 1.5,
            "slippage": 7.0,
            "new_levy": 2.5,
        }
        parts["total"] = sum(parts.values())
        parts["orders"] = float(orders)
        return parts

    def charge(self, buy_value, sell_value, orders=0):
        return self.breakdown(buy_value, sell_value, orders)["total"]


class TestCostComponentResidual:
    def test_components_still_sum_to_the_total_with_an_unknown_charge_key(self, monkeypatch):
        from signal_engine.backtest import costs

        monkeypatch.setattr(
            costs, "india_intraday", lambda exchange="NSE", broker=None: _ExtraKeySchedule()
        )
        costs._component_bps.cache_clear()
        costs._statutory_bps.cache_clear()
        try:
            trade = _trade("TP", 100.0, 99.0, 101.0)
            comps = MODEL.components(trade)
            assert comps["modeled_cost_bps"] == pytest.approx(MODEL.trade_cost_bps(trade), abs=1e-9)
            parts = list(costs.COMPONENT_COLUMNS)
            assert sum(comps[c] for c in parts) == pytest.approx(
                comps["modeled_cost_bps"], abs=1e-9
            )
            frame = decompose_trades([trade], MODEL)
            assert frame["modeled_cost_bps"].iloc[0] == pytest.approx(
                MODEL.trade_cost_bps(trade), abs=1e-9
            )
            assert frame[parts].sum(axis=1).iloc[0] == pytest.approx(
                frame["modeled_cost_bps"].iloc[0], abs=1e-9
            )
            # the unlisted lines (7.0 slippage and 2.5 new levy) are folded into slippage
            assert comps["cost_slippage_bps"] > 2.0 * MODEL.slippage_bps_per_side
        finally:
            costs._component_bps.cache_clear()
            costs._statutory_bps.cache_clear()


class TestReproduceColumns:
    def test_old_run_with_lineage_but_no_cost_columns_reproduces(self, root, run_data):
        folder = record(root, run_data, cost_components=None)
        exe = ledger_execution(run_data)  # rerun now has cost columns the old file lacks
        report = _reproduce(folder, root, lambda key, settings=None, end=None: exe)
        assert report.reproducible is True, report.reasons
        assert any("cost_stt_bps" in n for n in report.notes)

    def test_changed_value_in_an_old_column_still_fails(self, root, run_data):
        folder = record(root, run_data, cost_components=None)
        trades = list(run_data[0])
        # The run's net is restated from the gross result, so an old column changes via r_gross.
        trades[1] = dataclasses.replace(trades[1], r_gross=trades[1].r_gross + 1.0)
        exe = ledger_execution(run_data, trades=trades)
        report = _reproduce(folder, root, lambda key, settings=None, end=None: exe)
        assert report.reproducible is False

    def test_altered_cost_component_fails_when_the_old_run_has_them(self, root, run_data):
        folder = record(root, run_data)
        comps = decompose_trades(run_data[0], MODEL)
        comps.loc[0, "cost_gst_bps"] += 0.2
        exe = ledger_execution(run_data, cost_components=comps)
        report = _reproduce(folder, root, lambda key, settings=None, end=None: exe)
        assert report.reproducible is False
        assert any("cost_gst_bps" in r for r in report.reasons)

    def test_a_column_missing_from_the_rerun_is_a_mismatch(self, root, run_data):
        folder = record(root, run_data)
        exe = ledger_execution(run_data, cost_components=None)
        report = _reproduce(folder, root, lambda key, settings=None, end=None: exe)
        assert report.reproducible is False
        assert any("columns differ" in r for r in report.reasons)


class TestPrunableLedgers:
    def _fills(self, n=120_000):
        import numpy as np

        sym = np.array([f"S{k:03d}" for k in range(300)])[np.arange(n) % 300]
        when = pd.Timestamp("2026-06-01 09:20", tz=IST) + pd.to_timedelta(np.arange(n) // 300, "m")
        ids = [f"F|{s}|{i}" for i, s in enumerate(sym)]
        return pd.DataFrame(
            {
                "fill_id": [f"{s}|{i:07d}" for i, s in enumerate(sym)],
                "order_id": ids,
                "position_id": ids,
                "symbol": sym,
                "fill_time": when,
                "fill_price": 100.0,
                "side": "BUY",
                "kind": "entry",
                "exit_reason": None,
                "leg": 1,
            }
        )

    def test_bytes_deterministic_and_input_order_independent(self):
        frame = self._fills(2000)
        schema = trades_io.LEDGER_SCHEMAS["fills"]
        one = trades_io.frame_parquet_bytes(frame, schema)
        assert one == trades_io.frame_parquet_bytes(frame.copy(), schema)
        assert one == trades_io.frame_parquet_bytes(frame.iloc[::-1], schema)

    def test_statistics_present_for_id_columns(self):
        frame = self._fills(2000)
        for name, col in (("fills", "fill_id"), ("orders", "order_id")):
            if name == "orders":
                continue
            data = trades_io.frame_parquet_bytes(frame, trades_io.LEDGER_SCHEMAS[name])
            meta = pq.ParquetFile(io.BytesIO(data)).metadata
            idx = meta.schema.names.index(col)
            stats = meta.row_group(0).column(idx).statistics
            assert stats is not None and stats.has_min_max

    def test_id_lookup_reads_only_the_matching_row_groups(self):
        frame = self._fills()
        data = trades_io.frame_parquet_bytes(frame, trades_io.LEDGER_SCHEMAS["fills"])
        pf = pq.ParquetFile(io.BytesIO(data))
        assert pf.metadata.num_row_groups >= 3
        target = frame["fill_id"].iloc[77_777]
        idx = pf.schema_arrow.names.index("fill_id")
        hit = [
            g
            for g in range(pf.metadata.num_row_groups)
            if pf.metadata.row_group(g).column(idx).statistics.min
            <= target
            <= pf.metadata.row_group(g).column(idx).statistics.max
        ]
        assert 1 <= len(hit) < pf.metadata.num_row_groups
        got = pq.read_table(io.BytesIO(data), filters=[("fill_id", "=", target)])
        assert got.num_rows == 1 and got.column("fill_id")[0].as_py() == target
        assert (
            len(
                pf.read_row_groups(hit).filter(
                    __import__("pyarrow.compute").compute.equal(
                        pf.read_row_groups(hit)["fill_id"], target
                    )
                )
            )
            == 1
        )

    def test_round_trip_equals_the_sorted_frame(self):
        frame = self._fills(5000)
        data = trades_io.frame_parquet_bytes(frame, trades_io.LEDGER_SCHEMAS["fills"])
        got = pq.read_table(io.BytesIO(data)).to_pandas()
        want = frame.sort_values(["symbol", "fill_time", "fill_id"]).reset_index(drop=True)
        assert got["fill_id"].tolist() == want["fill_id"].tolist()
        assert got["fill_price"].tolist() == want["fill_price"].tolist()

    def test_trades_table_layout_is_unchanged(self):
        data = trades_io.trades_parquet_bytes([])
        meta = pq.ParquetFile(io.BytesIO(data)).metadata
        assert meta.num_row_groups <= 1
