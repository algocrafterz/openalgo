"""Export of backtest results: the run record is the canonical JSON; CSV is derived from it.

Per-trade rows are not stored in run records, so they are produced by re-executing an idea
and written outside the git-tracked research tree by default.
"""

import csv
import gzip
import json
from pathlib import Path

import pandas as pd
import pytest

from signal_engine.backtest import export
from signal_engine.backtest import record_run as rr
from signal_engine.backtest.types import Trade

RECORD = {
    "run_id": "2026-09-29-x-v1",
    "date": "2026-09-29",
    "code_version": "abc",
    "data": {"source": "historify", "as_of": "2026-09-29", "universe": "u", "bar_size": "1d"},
    "period": {"start": "2026-01-01", "end": "2026-09-29"},
    "costs": {"model": "flat-round-trip-bps", "round_trip_bps": 24.0},
    "metrics": {"trades": 10, "gross_bps": 30.0, "net_bps": 6.0, "t_stat": 1.5},
    "in_sample": {"trades": 7, "gross_bps": 28.0, "net_bps": 4.0},
    "out_of_sample": {"trades": 3, "gross_bps": 34.0, "net_bps": 10.0},
    "position_caps": [{"max_positions": 3, "trades": 5, "gross_bps": 1.0, "net_bps": 0.5}],
    "series": {
        "equity_r": [["2026-01-02", 0.5], ["2026-01-03", 0.9]],
        "drawdown_r": [["2026-01-02", 0.0], ["2026-01-03", -0.1]],
        "monthly": [{"month": "2026-01", "n": 2, "total_r": 0.9, "net_r": 0.45}],
    },
}


def _trade(day, direction=1, r_net=0.5):
    return Trade(
        symbol="A.NS",
        day=day,
        direction=direction,
        tag="t",
        entry_time=pd.Timestamp(f"{day} 09:20"),
        entry=100.0,
        signal_price=100.0,
        sl=99.0,
        tp=0.0,
        risk=1.0,
        exit_time=pd.Timestamp(f"{day} 15:00"),
        reason="tp",
        r_gross=r_net + 0.1,
        r_net=r_net,
    )


def _read_csv(path: Path, opener=open):
    with opener(path, "rt", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


class TestMetricsRows:
    def test_flattens_headline_and_split_sections(self):
        rows = export.metrics_rows(RECORD)
        keyed = {(r["section"], r["metric"]): r["value"] for r in rows}
        assert keyed[("metrics", "net_bps")] == 6.0
        assert keyed[("in_sample", "trades")] == 7
        assert keyed[("out_of_sample", "net_bps")] == 10.0
        assert keyed[("position_caps.3", "trades")] == 5

    def test_missing_optional_sections_are_skipped(self):
        slim = {k: v for k, v in RECORD.items() if k not in ("in_sample", "position_caps")}
        sections = {r["section"] for r in export.metrics_rows(slim)}
        assert sections == {"metrics", "out_of_sample"}


class TestSeriesRows:
    def test_equity_and_drawdown_are_joined_by_date(self):
        rows = export.series_rows(RECORD)
        assert rows == [
            {"date": "2026-01-02", "equity_r": 0.5, "drawdown_r": 0.0},
            {"date": "2026-01-03", "equity_r": 0.9, "drawdown_r": -0.1},
        ]

    def test_record_without_series_gives_no_rows(self):
        assert export.series_rows({"metrics": {}}) == []


class TestTradeRows:
    def test_rows_are_in_close_order_with_direction_label(self):
        trades = [_trade("2026-01-03", -1), _trade("2026-01-02", 1)]
        rows = export.trade_rows(trades)
        assert [r["day"] for r in rows] == ["2026-01-02", "2026-01-03"]
        assert [r["dir"] for r in rows] == ["LONG", "SHORT"]

    def test_empty_trades_give_empty_rows(self):
        assert export.trade_rows([]) == []


class TestWriters:
    def test_write_trades_plain_and_gzip(self, tmp_path: Path):
        trades = [_trade("2026-01-02"), _trade("2026-01-03", -1)]
        plain = export.write_trades(trades, tmp_path / "t.csv")
        gz = export.write_trades(trades, tmp_path / "t.csv.gz")
        assert len(_read_csv(plain)) == 2
        assert len(_read_csv(gz, gzip.open)) == 2

    def test_write_trades_refuses_empty(self, tmp_path: Path):
        with pytest.raises(ValueError, match="no trades"):
            export.write_trades([], tmp_path / "t.csv")

    def test_export_record_writes_json_and_csvs(self, tmp_path: Path):
        src = tmp_path / "run.json"
        src.write_text(json.dumps(RECORD), encoding="utf-8")
        out = export.export_record(src, tmp_path / "out")
        assert json.loads(out["json"].read_text(encoding="utf-8")) == RECORD
        assert {r["metric"] for r in _read_csv(out["metrics"])} >= {"net_bps", "trades"}
        assert len(_read_csv(out["series"])) == 2

    def test_export_record_rejects_unreadable_json(self, tmp_path: Path):
        src = tmp_path / "bad.json"
        src.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match="not valid JSON"):
            export.export_record(src, tmp_path / "out")


class TestCli:
    def test_run_export(self, tmp_path: Path):
        src = tmp_path / "run.json"
        src.write_text(json.dumps(RECORD), encoding="utf-8")
        out = tmp_path / "out"
        assert export.main(["--run", str(src), "--out", str(out)]) == 0
        assert (out / "2026-09-29-x-v1.metrics.csv").exists()

    def test_missing_run_file_fails(self, tmp_path: Path):
        assert export.main(["--run", str(tmp_path / "nope.json"), "--out", str(tmp_path)]) == 1

    def test_trades_export_uses_injected_executor(self, tmp_path: Path):
        idea_root = tmp_path / "root"
        folder = idea_root / "research" / "ideas" / "idea-012-x"
        folder.mkdir(parents=True)
        (folder / "idea.md").write_text(
            "---\nid: IDEA-012\ntitle: t\nstage: inbox\nresearch_state: rejected\n"
            "category: intraday-breakout\nhorizon: intraday\nsource: {type: self}\n"
            "summary: s\nbacktest_module: backtest/strategies/orb.py\nlast_reviewed: 2026-01-01\n---\n",
            encoding="utf-8",
        )
        exe = rr.Execution(
            trades=[_trade("2026-01-02"), _trade("2026-01-03")],
            params={},
            data={},
            period={"start": "2026-01-02", "end": "2026-01-03"},
            costs={},
            days=["2026-01-02", "2026-01-03"],
        )
        out = tmp_path / "out"
        code = export.main(
            ["--idea", "IDEA-012", "--root", str(idea_root), "--out", str(out), "--gzip"],
            execute=lambda key: exe,
            supported={"orb"},
        )
        assert code == 0
        files = list(out.glob("IDEA-012.trades.csv.gz"))
        assert len(files) == 1
        assert len(_read_csv(files[0], gzip.open)) == 2

    def test_default_out_dir_is_outside_research_tree(self):
        assert "research" not in export.DEFAULT_OUT.parts
        assert export.DEFAULT_OUT.parts[-2:] == ("data", "exports")
