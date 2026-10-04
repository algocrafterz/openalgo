"""Runs whose trades have no stop: R is not applicable, the bps-based metrics stay available."""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import compare, trades_io
from signal_engine.backtest.tearsheet import build_run_record, headline_metrics
from signal_engine.backtest.types import Trade
from signal_engine.research import build_registry as br

META = {
    "run_id": "2026-01-01-x-v1",
    "run_date": "2026-01-01",
    "code_version": "x",
    "data": {"source": "s", "as_of": "2026-01-01", "universe": "u", "bar_size": "1d"},
    "period": {"start": "2026-01-01", "end": "2026-03-31"},
    "costs": {"model": "flat-round-trip-bps", "round_trip_bps": 24.0},
    "horizon": "swing",
}
# net bps per trade, one trade a day: chosen so every figure below is easy to verify by hand
NET = [100.0, -50.0, 200.0, -300.0, 150.0, 80.0, -20.0, 60.0]


def no_stop_trade(day, net_bps, cost_bps=24.0, symbol="A.NS"):
    gross = (net_bps + cost_bps) / 1e4
    return Trade(
        symbol=symbol,
        day=day,
        direction=1,
        tag="t",
        entry_time=pd.Timestamp(f"{day} 09:15"),
        entry=100.0,
        signal_price=100.0,
        sl=float("nan"),
        tp=math.inf,
        risk=float("nan"),
        exit_time=f"{day} 15:30",
        exit=100.0 * (1 + gross),
        reason="REBALANCE",
        r_gross=float("nan"),
        r_net=float("nan"),
        gross_ret=gross,
        net_ret=net_bps / 1e4,
    )


def trades(net=NET, start=date(2026, 1, 5)):
    return [no_stop_trade(start + timedelta(days=i), v) for i, v in enumerate(net)]


@pytest.fixture(scope="module")
def record():
    return build_run_record(trades(), **META)


class TestHeadlineMetrics:
    def test_bps_metrics_are_available(self, record):
        m = record["metrics"]
        assert m["trades"] == len(NET)
        assert m["net_bps"] == pytest.approx(np.mean(NET), abs=1e-3)
        assert m["gross_bps"] == pytest.approx(np.mean(NET) + 24.0, abs=1e-3)
        assert m["win_rate"] == pytest.approx(5 / 8, abs=1e-4)
        assert "t_stat" in m and "sharpe" in m and np.isfinite(m["t_stat"])

    def test_r_based_metrics_are_absent_not_zero_or_nan(self, record):
        for key in ("expectancy_r", "payoff", "max_dd_r", "total_cost_r"):
            assert key not in record["metrics"]

    def test_the_record_says_r_is_not_defined(self, record):
        assert record["metrics"]["r_defined"] is False

    def test_profit_factor_is_the_bps_ratio(self, record):
        wins = sum(v for v in NET if v > 0)
        losses = -sum(v for v in NET if v < 0)
        assert record["metrics"]["profit_factor"] == pytest.approx(wins / losses, abs=1e-3)

    def test_drawdown_is_in_bps_from_the_cumulative_curve(self, record):
        equity = np.cumsum(NET)
        peak = np.maximum.accumulate(np.maximum(equity, 0.0))
        assert record["metrics"]["max_dd_bps"] == pytest.approx(
            float((peak - equity).max()), abs=1e-3
        )

    def test_total_cost_is_the_sum_of_per_trade_cost_bps(self, record):
        assert record["metrics"]["total_cost_bps"] == pytest.approx(24.0 * len(NET), abs=1e-3)

    def test_the_series_are_bps_and_the_r_series_are_not_invented(self, record):
        series = record["series"]
        assert "equity_r" not in series and "drawdown_r" not in series
        assert [p[1] for p in series["equity_bps"]] == pytest.approx(list(np.cumsum(NET)), abs=1e-3)
        assert len(series["drawdown_bps"]) == len(NET)

    def test_monthly_rows_are_in_bps(self, record):
        (row,) = record["series"]["monthly"]
        assert row["n"] == len(NET)
        assert row["total_bps"] == pytest.approx(sum(NET), abs=1e-3)
        assert row["net_bps"] == pytest.approx(np.mean(NET), abs=1e-3)
        assert "total_r" not in row

    def test_the_split_halves_are_bps_metrics_too(self):
        rec = build_run_record(trades(), oos_start="2026-01-10", **META)
        assert rec["in_sample"]["net_bps"] == pytest.approx(np.mean(NET[:5]), abs=1e-3)
        assert rec["out_of_sample"]["net_bps"] == pytest.approx(np.mean(NET[5:]), abs=1e-3)


class TestRecordIsValid:
    def test_the_registry_accepts_a_no_stop_record(self, record):
        assert br._validate_run_v2(record) == []

    def test_a_stop_based_record_is_unchanged(self):
        t = Trade(
            symbol="A.NS",
            day=date(2026, 1, 5),
            direction=1,
            tag="t",
            entry_time=pd.Timestamp("2026-01-05 09:15"),
            entry=100.0,
            signal_price=100.0,
            sl=99.0,
            tp=0.0,
            risk=1.0,
            exit_time="2026-01-05 15:30",
            exit=101.0,
            r_gross=1.0,
            r_net=0.9,
        )
        rec = build_run_record([t], **META)
        assert "r_defined" not in rec["metrics"]
        assert "equity_r" in rec["series"] and "equity_bps" not in rec["series"]
        assert br._validate_run_v2(rec) == []


class TestCompareRecompute:
    def test_headline_metrics_from_bps_arrays_match_the_record(self, record):
        days = [str(date(2026, 1, 5) + timedelta(days=i)) for i in range(len(NET))]
        nan = [float("nan")] * len(NET)
        net = list(NET)
        gross = [v + 24.0 for v in NET]
        got = headline_metrics(days, nan, nan, nan, gross_bps=gross, net_bps=net)
        for key in (
            "trades",
            "net_bps",
            "gross_bps",
            "win_rate",
            "t_stat",
            "sharpe",
            "profit_factor",
        ):
            assert got[key] == pytest.approx(record["metrics"][key], abs=1e-3), key


def retained(trade_list):
    """The frame compare reads from a run's trades file."""
    frame = trades_io.trades_to_frame(trade_list)[
        ["entry_time", "gross_r", "net_r", "gross_bps", "net_bps"]
    ].copy()
    frame["day"] = frame["entry_time"].dt.date.map(lambda d: d.isoformat())
    return frame


def stop_based_trades():
    return [
        Trade(
            symbol="B.NS",
            day=date(2026, 1, 5) + timedelta(days=i),
            direction=1,
            tag="t",
            entry_time=pd.Timestamp(f"2026-01-{5 + i:02d} 09:15"),
            entry=100.0,
            signal_price=100.0,
            sl=99.0,
            tp=0.0,
            risk=1.0,
            exit_time="2026-02-05 15:30",
            exit=101.0,
            r_gross=1.0,
            r_net=0.9,
        )
        for i in range(6)
    ]


class TestCompareUnits:
    def test_a_no_stop_frame_is_scored_from_its_bps_columns(self, record):
        got = compare.metrics_from_frame(retained(trades()))
        assert got["r_defined"] is False
        for key in ("trades", "net_bps", "gross_bps", "win_rate", "profit_factor"):
            assert got[key] == pytest.approx(record["metrics"][key], abs=1e-3), key

    def test_a_stop_based_frame_is_scored_in_r_as_before(self):
        got = compare.metrics_from_frame(retained(stop_based_trades()))
        assert "r_defined" not in got and "expectancy_r" in got

    def test_the_bps_view_puts_bps_in_the_r_columns(self):
        run = type("Run", (), {})()
        run.frame = retained(trades())
        view = compare._bps_view(run)
        assert view.frame["net_r"].tolist() == pytest.approx(NET, abs=1e-6)
        assert run.frame["net_r"].isna().all()

    def test_a_mixed_pair_is_compared_in_bps(self):
        a, b = retained(stop_based_trades()), retained(trades())
        assert not compare._has_no_r(a) and compare._has_no_r(b)


class TestTradesFile:
    def test_bps_columns_are_exact_and_r_columns_are_nan(self):
        frame = trades_io.trades_to_frame(trades())
        assert frame["net_bps"].tolist() == pytest.approx(NET, abs=1e-6)
        assert frame["gross_bps"].tolist() == pytest.approx([v + 24.0 for v in NET], abs=1e-6)
        assert frame["cost_bps"].tolist() == pytest.approx([24.0] * len(NET), abs=1e-6)
        assert frame["gross_r"].isna().all() and frame["net_r"].isna().all()


class TestCompareStatesTheUnit:
    def test_a_run_with_r_adds_nothing(self):
        assert compare._with_unit_note(None, False) is None
        assert compare._with_unit_note("periods differ", False) == "periods differ"

    def test_a_run_without_r_says_the_curves_are_in_bps(self):
        note = compare._with_unit_note(None, True)
        assert "basis points" in note and "not R" in note

    def test_the_note_is_added_to_an_existing_warning(self):
        text = compare._with_unit_note("The runs cover different periods.", True)
        assert text.startswith("The runs cover different periods.")
        assert "basis points" in text
