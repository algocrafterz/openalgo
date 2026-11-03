"""
Tests for the P1 statistics in services/performance_stats.py and the assembly in
services/performance_insights_service.py.

Hand-computed expected values throughout; the golden test reads the real export (see
test_performance_stats.py for how to point at it) and is skipped when it is absent.

Run with: uv run pytest test/test_performance_insights.py -v
"""

import csv
import os
from datetime import date
from pathlib import Path

import numpy as np
import pytest

import services.strategy_daily_performance_service as svc
from services import performance_stats as ps
from services import strategy_trade_enrichment as enr
from services.performance_insights_service import (
    build_insights,
    comparison_extras,
    excursion_report,
    trade_rows,
)


def _row(net, day, strategy="A", closed="10:00:00", **kw):
    base = {
        "strategy": strategy,
        "symbol": "AAA",
        "trade_date": day,
        "closed_at": f"{day}T{closed}",
        "net": net,
    }
    base.update(kw)
    return base


# ---- concentration ---------------------------------------------------------------------


def _conc_rows():
    # Day totals: 10-01 +100, 10-02 -30, 10-03 +50, 10-04 +10, 10-05 +5, 10-06 +5, 10-07 -20
    nets = {
        "2026-10-01": [60.0, 40.0],
        "2026-10-02": [-30.0],
        "2026-10-03": [50.0],
        "2026-10-04": [10.0],
        "2026-10-05": [5.0],
        "2026-10-06": [5.0],
        "2026-10-07": [-20.0],
    }
    return [
        _row(n, d, "A" if i % 2 == 0 else "B") for i, (d, v) in enumerate(nets.items()) for n in v
    ]


def test_concentration_known_answers():
    c = ps.concentration(_conc_rows())
    assert c["net"] == 120.0 and c["traded_days"] == 7
    assert c["top1_days_share_pct"] == pytest.approx(100 / 120 * 100, abs=0.01)
    assert c["top3_days_share_pct"] == pytest.approx(160 / 120 * 100, abs=0.01)  # can exceed 100
    assert c["top5_days_share_pct"] == pytest.approx(170 / 120 * 100, abs=0.01)
    assert c["net_ex_best_day"] == 20.0
    assert c["net_ex_best_five_days"] == -50.0  # 120 - (100+50+10+5+5)
    assert c["median_daily"] == 5.0
    assert c["mean_daily"] == pytest.approx(120 / 7, abs=0.01)
    assert c["profitable_days"] == 5 and c["profitable_days_pct"] == pytest.approx(71.43, abs=0.01)
    assert c["avg_winning_day"] == 34.0 and c["avg_losing_day"] == -25.0
    assert c["best_day"] == 100.0 and c["worst_day"] == -30.0
    assert c["top10pct_trades"] == 1  # ceil(0.1 * 9 trades)
    assert c["top10pct_trades_share_of_net_pct"] == pytest.approx(60 / 120 * 100, abs=0.01)


def test_concentration_shares_are_withheld_without_a_net_profit():
    c = ps.concentration([_row(-10.0, "2026-10-01"), _row(5.0, "2026-10-02")])
    assert c["top1_days_share_pct"] is None
    assert c["net_ex_best_five_days"] is None  # five or fewer days


def test_concentration_by_strategy_shows_dependence_on_the_portfolio_best_day():
    rows = [
        _row(100.0, "2026-10-01", "A"),
        _row(10.0, "2026-10-02", "A"),
        _row(5.0, "2026-10-01", "B"),
        _row(30.0, "2026-10-03", "B"),
    ]
    c = ps.concentration(rows)
    assert c["best_day_date"] == "2026-10-01"
    a, b = c["by_strategy"]["A"], c["by_strategy"]["B"]
    assert a["net"] == 110.0 and a["net_ex_portfolio_best_day"] == 10.0
    assert b["net"] == 35.0 and b["net_ex_portfolio_best_day"] == 30.0
    assert a["net_ex_best_day"] == 10.0 and b["net_ex_best_day"] == 5.0
    assert a["share_of_net_pct"] == pytest.approx(110 / 145 * 100, abs=0.01)


def test_empty_rows():
    assert ps.concentration([]) == {"traded_days": 0, "trades": 0}
    assert ps.losing_streaks([])["max_consecutive_losses"] == 0


# ---- streaks ---------------------------------------------------------------------------


def test_losing_streaks_follow_closing_order_and_a_flat_trade_ends_a_streak():
    nets = [-1, -1, -1, 2, -1, 0, -1, -1, 3, -1]
    rows = [_row(n, "2026-10-01", closed=f"10:{i:02d}:00") for i, n in enumerate(nets)]
    rows.reverse()  # input order must not matter
    s = ps.losing_streaks(rows)
    assert s["max_consecutive_losses"] == 3
    assert s["losing_streaks"] == 4  # 3, 1, 2, 1
    assert s["avg_losing_streak"] == pytest.approx(7 / 4)


# ---- drawdown --------------------------------------------------------------------------


def test_drawdown_starts_from_zero_and_measures_recovery():
    # equity: -2, 1, 4, 1, -1, 2, 5, 6 -> peak 4 (index 2), trough -1 (index 4), recovered at 6
    values = np.array([-2, 3, 3, -3, -2, 3, 3, 1], dtype=float)
    sessions = [f"2026-10-{i + 1:02d}" for i in range(8)]
    d = ps.net_drawdown(values, sessions, capital_base=100.0, trade_nets=list(values))
    assert d["max_drawdown"] == -5.0
    assert d["drawdown_peak_date"] == "2026-10-03" and d["drawdown_trough_date"] == "2026-10-05"
    assert d["drawdown_recovered"] is True
    assert d["drawdown_duration_sessions"] == 4  # peak index 2 to recovery index 6
    assert d["max_drawdown_pct"] == pytest.approx(100 * -5 / (100 + 4), abs=0.01)
    assert d["recovery_factor"] == pytest.approx(6 / 5)  # net 6 over a fall of 5
    assert d["return_on_capital_pct"] == 6.0
    assert d["calmar_ratio"] is None  # fewer than 60 sessions


def test_drawdown_reports_an_unrecovered_fall_and_a_start_that_is_already_below_zero():
    d = ps.net_drawdown(np.array([-5.0, 2.0]), ["2026-10-01", "2026-10-02"], 100.0, [-5.0, 2.0])
    assert d["max_drawdown"] == -5.0 and d["drawdown_peak_date"] is None
    assert d["drawdown_recovered"] is False
    assert d["recovery_factor"] is None or d["recovery_factor"] < 0


def test_no_drawdown_when_equity_only_rises():
    d = ps.net_drawdown(np.array([1.0, 2.0]), ["2026-10-01", "2026-10-02"], 100.0, [1.0, 2.0])
    assert d["max_drawdown"] == 0.0 and d["recovery_factor"] is None
    assert d["max_drawdown_pct"] == 0.0


def test_calmar_is_annualised_and_gated_on_sixty_sessions():
    values = np.array([5.0, -3.0] * 30)  # 60 sessions, net +60, drawdown -3
    sessions = [f"s{i}" for i in range(60)]
    d = ps.net_drawdown(values, sessions, 1000.0, list(values))
    assert d["max_drawdown"] == -3.0
    assert d["calmar_ratio"] == pytest.approx(60 * 252 / (60 * 3))


def test_trade_sequence_drawdown_sees_intraday_swings_the_daily_close_hides():
    d = ps.net_drawdown(np.array([1.0]), ["2026-10-01"], 100.0, [10.0, -8.0, -4.0, 3.0])
    assert d["max_drawdown"] == 0.0  # the day closed up
    assert d["max_drawdown_trade_sequence"] == -12.0


# ---- deployment ------------------------------------------------------------------------


def _pos(opened, closed, value, day="2026-10-01", flag=None):
    return {
        "trade_date": day,
        "opened_at": f"{day}T{opened}",
        "closed_at": f"{day}T{closed}",
        "entry_price": value,
        "closed_quantity": 1,
        "data_flag": flag,
    }


def test_deployment_is_the_peak_of_positions_open_together():
    rows = [
        _pos("09:20:00", "10:00:00", 100.0),
        _pos("09:30:00", "09:50:00", 50.0),  # overlaps the first: peak 150
        _pos("10:00:00", "10:30:00", 70.0),  # starts the minute the first exits: no overlap
        _pos("09:20:00", "09:40:00", 30.0, day="2026-10-02"),
    ]
    d = ps.deployment(rows)
    assert d["max_deployed"] == 150.0
    assert d["avg_daily_peak_deployed"] == pytest.approx((150.0 + 30.0) / 2)
    assert d["deployment_days"] == 2 and d["deployment_excluded"] == 0


def test_deployment_leaves_out_unreliable_rows_and_counts_them():
    rows = [
        _pos("09:20:00", "10:00:00", 100.0),
        _pos("09:20:00", "10:00:00", 999.0, flag="overnight_mis"),
    ]
    rows.append(
        {
            "trade_date": "2026-10-01",
            "closed_at": "2026-10-01T10:00:00",
            "entry_price": 1,
            "closed_quantity": 1,
        }
    )
    d = ps.deployment(rows)
    assert d["max_deployed"] == 100.0 and d["deployment_excluded"] == 2


# ---- rolling and monthly ---------------------------------------------------------------


def test_rolling_expectancy_and_profit_factor_match_a_reference():
    nets = [((i * 13) % 11) - 4.0 for i in range(120)]
    rows = [
        _row(n, f"2026-10-{i // 10 + 1:02d}", closed=f"{9 + i % 10:02d}:{i % 60:02d}:00")
        for i, n in enumerate(nets)
    ]
    # closing order is by (closed_at string); build the expected order the same way
    order = sorted(range(120), key=lambda i: (rows[i]["closed_at"], rows[i]["trade_date"]))
    seq = [nets[i] for i in order]
    pts = {p["n"]: p for p in ps.rolling_series(rows)}
    assert 19 not in pts and pts[20]["exp20"] == pytest.approx(sum(seq[:20]) / 20, abs=0.01)
    assert pts[20]["pf50"] is None
    window = seq[70:120]
    pf = sum(v for v in window if v > 0) / -sum(v for v in window if v < 0)
    assert pts[120]["pf50"] == pytest.approx(pf, abs=0.01)
    assert pts[120]["exp20"] == pytest.approx(sum(seq[100:120]) / 20, abs=0.01)


def test_rolling_pf_is_none_without_a_loss_in_the_window():
    rows = [_row(1.0, "2026-10-01", closed=f"10:{i % 60:02d}:00") for i in range(55)]
    assert ps.rolling_series(rows)[-1]["pf50"] is None


def test_monthly_table():
    rows = [
        _row(10.0, "2026-09-29"),
        _row(-4.0, "2026-09-30"),
        _row(6.0, "2026-10-01"),
    ]
    m = ps.monthly_table(rows)
    assert [x["month"] for x in m] == ["2026-09", "2026-10"]
    assert m[0] == {
        "month": "2026-09",
        "trades": 2,
        "traded_days": 2,
        "net": 6.0,
        "win_rate": 50.0,
        "profit_factor": 2.5,
        "profitable_days_pct": 50.0,
    }
    assert m[1]["profit_factor"] is None  # no losing trade


# ---- entry quality ---------------------------------------------------------------------


def test_entry_quality_buckets_and_first_hour_boundary():
    rows = [
        _row(10.0, "2026-10-05", opened_at="2026-10-05T09:20:00", direction="LONG", net_r=0.5),
        _row(-5.0, "2026-10-05", opened_at="2026-10-05T10:14:00", direction="SHORT", net_r=-0.25),
        _row(4.0, "2026-10-06", opened_at="2026-10-06T10:15:00", direction="LONG", net_r=None),
        _row(7.0, "2026-10-06", opened_at=None, direction="LONG"),
        _row(
            9.0,
            "2026-10-06",
            opened_at="2026-10-05T14:00:00",
            direction="LONG",
            data_flag="overnight_mis",
        ),
    ]
    q = ps.entry_quality(rows)
    parts = {r["label"]: r for r in q["by_session_part"]}
    assert parts["First hour"]["trades"] == 2 and parts["First hour"]["net"] == 5.0
    assert parts["Later in the session"]["trades"] == 1  # 10:15 is already later
    assert q["entry_excluded"] == 2  # no entry time, and the flagged overnight row
    hours = {r["label"]: r for r in q["by_hour"]}
    assert set(hours) == {9, 10}
    assert hours[9]["expectancy_r"] == 0.5 and hours[10]["trades"] == 2
    long_ = next(r for r in q["by_direction"] if r["label"] == "LONG")
    assert long_["trades"] == 4 and long_["expectancy_r"] == 0.5 and long_["r_trades"] == 1
    assert [r["label"] for r in q["by_weekday"]] == ["Mon", "Tue"]


def test_bucket_profit_factor_and_expectancy():
    rows = [_row(10.0, "2026-10-05", direction="L"), _row(-4.0, "2026-10-05", direction="L")]
    [b] = ps.bucket_stats(rows, lambda r: r["direction"])
    assert b["profit_factor"] == 2.5 and b["expectancy"] == 3.0 and b["win_rate"] == 50.0


# ---- excursions ------------------------------------------------------------------------


def _ex(mfe, mae, pnl, risk=100.0, strategy="A", net=None, flag=None):
    return {
        "strategy": strategy,
        "mfe": mfe,
        "mae": mae,
        "risk_amount": risk,
        "realized_pnl": pnl,
        "net": pnl if net is None else net,
        "data_flag": flag,
    }


def test_excursion_stats_known_answers():
    rows = [
        _ex(200.0, 20.0, 100.0),  # winner kept half
        _ex(100.0, 40.0, 50.0),  # winner kept half
        _ex(80.0, 90.0, -60.0),  # reached 0.8R then lost
        _ex(10.0, 120.0, -100.0),  # reached the stop (MAE 1.2R)
        _ex(500.0, 5.0, 400.0, flag="overnight_mis"),  # left out
        _ex(None, None, 30.0),  # no excursion data
    ]
    e = ps.excursion_stats(rows)
    assert (e["total"], e["covered"], e["excluded_flagged"], e["trades"]) == (6, 5, 1, 4)
    assert e["avg_mfe_r"] == pytest.approx((2.0 + 1.0 + 0.8 + 0.1) / 4)
    assert e["avg_mae_r"] == pytest.approx((0.2 + 0.4 + 0.9 + 1.2) / 4)
    assert e["mfe_capture_pct"] == pytest.approx(150 / 300 * 100)
    assert e["winners_avg_mae_r"] == pytest.approx(0.3) and e["losers_avg_mae_r"] == pytest.approx(
        1.05
    )
    assert e["mae_reached_stop_pct"] == 25.0
    assert e["reached_meaningful_mfe"] == 3  # 2.0, 1.0 and 0.8 R
    assert e["gave_back_pct"] == pytest.approx(100 / 3, abs=0.01)
    assert e["by_strategy"]["A"]["trades"] == 4


def test_excursion_without_a_stop_has_no_r_figures():
    e = ps.excursion_stats([_ex(50.0, 10.0, 20.0, risk=None)])
    assert e["avg_mfe_r"] is None and e["mfe_capture_pct"] == 40.0
    assert e["r_trades"] == 0


def test_excursion_report_uses_net_for_the_gave_back_test():
    trades = [
        {
            "strategy": "A",
            "mfe": 80.0,
            "mae": 5.0,
            "risk_amount": 100.0,
            "realized_pnl": 10.0,
            "cost": 25.0,
        }
    ]
    assert excursion_report(trades)["gave_back_pct"] == 100.0  # gross win, net loss


# ---- slippage --------------------------------------------------------------------------


def test_slippage_shows_adverse_and_favourable_separately():
    rows = [
        {
            "strategy": "A",
            "symbol": "X",
            "entry_slippage": 30.0,
            "entry_price": 100.0,
            "closed_quantity": 10.0,
        },
        {
            "strategy": "A",
            "symbol": "Y",
            "entry_slippage": -10.0,
            "entry_price": 200.0,
            "closed_quantity": 10.0,
        },
        {
            "strategy": "B",
            "symbol": "X",
            "entry_slippage": 0.0,
            "entry_price": 100.0,
            "closed_quantity": 10.0,
        },
        {
            "strategy": "B",
            "symbol": "Z",
            "entry_slippage": None,
            "entry_price": 1.0,
            "closed_quantity": 1.0,
        },
    ]
    s = ps.slippage_summary(rows)
    assert (s["covered"], s["total"]) == (3, 4)
    assert s["net"] == 20.0 and s["adverse_total"] == 30.0 and s["favourable_total"] == -10.0
    assert s["adverse_trades"] == 1 and s["favourable_trades"] == 1
    assert s["net_bps"] == pytest.approx(10000 * 20 / 4000)
    assert s["by_strategy"]["A"]["net"] == 20.0 and s["by_strategy"]["B"]["net"] == 0.0
    assert s["by_symbol"][0]["symbol"] == "X"
    assert "not measured" in s["basis"]


# ---- comparison ------------------------------------------------------------------------


def test_correlation_and_contribution_known_answers():
    a = np.array([5.0, -3.0, 4.0, -2.0] * 6)
    b = -a
    corr = ps.daily_correlation({"A": a, "B": b})
    assert corr["matrix"] == [[1.0, -1.0], [-1.0, 1.0]]
    contrib = {c["strategy"]: c for c in ps.portfolio_contribution({"A": a, "B": b})}
    assert contrib["A"]["portfolio_dd_with"] == 0.0  # perfect hedge: the sum is flat
    assert contrib["A"]["portfolio_dd_without"] == ps.max_drawdown_value(b)
    assert contrib["A"]["drawdown_change"] == -ps.max_drawdown_value(b)  # positive: A helps


def test_correlation_needs_enough_sessions_and_variation():
    short = {"A": np.array([1.0, 2.0, 3.0]), "B": np.array([3.0, 1.0, 2.0])}
    assert ps.daily_correlation(short)["matrix"][0][1] is None
    flat = {"A": np.zeros(30), "B": np.arange(30.0)}
    assert ps.daily_correlation(flat)["matrix"][0][1] is None


# ---- grading ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("metric", "value", "grade"),
    [
        ("net_profit_factor", 0.99, "poor"),
        ("net_profit_factor", 1.0, "acceptable"),
        ("net_profit_factor", 1.3, "good"),
        ("net_profit_factor", 2.0, "good"),
        ("net_profit_factor", 2.01, "excellent"),
        ("expectancy_r", -0.01, "poor"),
        ("expectancy_r", 0.1, "good"),
        ("expectancy_r", 0.31, "excellent"),
        ("win_rate_pct", 39.9, "poor"),
        ("win_rate_pct", 60.1, "excellent"),
        ("sortino", 3.5, "excellent"),
        ("recovery_factor", 1.5, "acceptable"),
        ("cost_pct_of_gross", 9.9, "excellent"),
        ("cost_pct_of_gross", 10.0, "good"),
        ("cost_pct_of_gross", 20.0, "acceptable"),
        ("cost_pct_of_gross", 30.0, "acceptable"),
        ("cost_pct_of_gross", 30.1, "poor"),
    ],
)
def test_grade_bands(metric, value, grade):
    assert ps.grade_value(metric, value) == grade


def _good_values(**kw):
    values = {
        "net_profit_factor": 2.5,
        "win_rate_pct": 70.0,
        "expectancy_r": 0.5,
        "sharpe": 2.5,
        "sortino": 3.5,
        "recovery_factor": 4.0,
        "cost_pct_of_gross": 5.0,
    }
    values.update(kw)
    return values


def test_overall_grade_is_the_weakest_metric_not_the_best():
    r = ps.grade_report(_good_values(), trades=100, sessions=80)
    assert r["overall"] == "excellent"
    weak = ps.grade_report(_good_values(recovery_factor=1.2), trades=100, sessions=80)
    assert weak["overall"] == "acceptable"


def test_a_high_win_rate_alone_never_lifts_the_grade():
    r = ps.grade_report(
        _good_values(win_rate_pct=95.0, net_profit_factor=1.1, expectancy_r=0.05),
        trades=100,
        sessions=80,
    )
    assert r["metrics"]["win_rate_pct"]["grade"] == "excellent"
    assert r["overall"] == "acceptable"


def test_risk_metrics_are_not_graded_on_a_short_history():
    r = ps.grade_report(_good_values(), trades=100, sessions=59)
    for name in ("recovery_factor", "sharpe", "sortino"):
        assert r["metrics"][name]["grade"] is None
        assert "60 trading sessions" in r["metrics"][name]["reason"]
    assert r["overall"] is None and "recovery factor" in r["overall_note"]


def test_return_metrics_are_not_graded_on_too_few_trades():
    r = ps.grade_report(_good_values(), trades=29, sessions=80)
    assert r["metrics"]["net_profit_factor"]["grade"] is None
    assert r["overall"] is None


def test_missing_sharpe_does_not_block_the_overall_grade_but_cannot_raise_it():
    r = ps.grade_report(_good_values(sharpe=None, sortino=None), trades=100, sessions=80)
    assert r["overall"] == "excellent"
    assert r["metrics"]["sharpe"]["reason"] == "not available"


# ---- assembly --------------------------------------------------------------------------


def _enriched(n=40, sessions=20):
    out = []
    for i in range(n):
        day = f"2026-10-{i % sessions + 1:02d}"
        pnl = 120.0 if i % 3 else -90.0
        out.append(
            {
                "strategy": "A" if i % 2 == 0 else "B",
                "symbol": "AAA",
                "trade_date": day,
                "opened_at": f"{day}T09:{20 + i % 30:02d}:00",
                "closed_at": f"{day}T11:{i % 60:02d}:00",
                "direction": "LONG",
                "entry_price": 100.0,
                "closed_quantity": 10.0,
                "realized_pnl": pnl,
                "cost": 10.0,
                "net_r_multiple": pnl / 100.0,
                "risk_amount": 100.0,
                "entry_slippage": 1.0,
                "mfe": 150.0,
                "mae": 30.0,
                "data_flag": None,
            }
        )
    return out


def test_trade_rows_use_net_of_charges_and_keep_gross_separate():
    rows = trade_rows(_enriched(2))
    assert rows[0]["net"] == -100.0 and rows[1]["net"] == 110.0


def test_build_insights_has_every_section_and_withholds_grades_on_a_short_range():
    sessions = [f"2026-10-{d:02d}" for d in range(1, 21)]
    ins = build_insights(_enriched(), sessions, None, None)
    for key in (
        "concentration", "drawdown", "streaks", "deployment", "rolling", "monthly",
        "entry_quality", "slippage", "grading", "gross_profit_factor", "net_profit_factor",
    ):  # fmt: skip
        assert key in ins
    assert ins["gross_profit_factor"] > ins["net_profit_factor"]
    assert ins["grading"]["overall"] is None
    assert set(ins["rolling"]["by_strategy"]) == {"A", "B"}


def test_comparison_extras_per_strategy_portfolio_and_pairwise_blocks():
    sessions = [f"2026-10-{d:02d}" for d in range(1, 21)]
    by = {"A": _enriched()[0::2], "B": _enriched()[1::2]}
    x = comparison_extras(
        by, sessions, {"A": (None, None), "B": (None, None), "__portfolio__": (None, None)}
    )
    assert set(x["per_strategy"]) == {"A", "B"}
    assert x["per_strategy"]["A"]["net_profit_factor"] is not None
    assert x["portfolio"]["net"] == pytest.approx(
        x["per_strategy"]["A"]["return_on_capital_pct"] * 1000
        + x["per_strategy"]["B"]["return_on_capital_pct"] * 1000,
        abs=1.0,
    )
    assert x["correlation"]["strategies"] == ["A", "B"]
    assert len(x["contribution"]) == 2
    solo = comparison_extras({"A": by["A"]}, sessions, {})
    assert solo["correlation"] is None and solo["contribution"] == []


# ---- service integration ---------------------------------------------------------------


def _ledger_trade(i, strategy, pnl, day):
    return {
        "strategy": strategy,
        "symbol": f"S{i % 4}",
        "exchange": "NSE",
        "product": "MIS",
        "mode": "analyze",
        "direction": "LONG",
        "closed_quantity": 10.0,
        "entry_price": 100.0,
        "exit_price": 100.0 + pnl / 10,
        "realized_pnl": pnl,
        "trade_date": day,
        "opened_at": f"{day}T09:30:00",
        "closed_at": f"{day}T11:{i % 60:02d}:00",
    }


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    trades = [
        _ledger_trade(
            i, "ORB" if i % 2 else "VWAP", 80.0 if i % 3 else -60.0, f"2026-10-{i % 12 + 1:02d}"
        )
        for i in range(48)
    ]
    monkeypatch.setattr(svc, "get_closed_trades", lambda **kw: [dict(t) for t in trades])
    monkeypatch.setattr(svc, "_current_mode", lambda: "analyze")
    monkeypatch.setattr(svc, "_today", lambda: date(2026, 10, 30))
    monkeypatch.setattr(enr, "SIGNAL_TRADES_DB", tmp_path / "absent.db")
    monkeypatch.setattr("services.strategy_metrics_service.LEDGER_RELIABLE_SINCE", date(2020, 1, 1))
    return trades


def test_daily_response_carries_insights_and_percent_figures_ignore_capital(ledger):
    ok, plain, _ = svc.get_daily_performance(None, "all")
    ok2, with_cap, _ = svc.get_daily_performance(None, "all", capital=1000.0)
    assert ok and ok2
    assert plain["insights"]["concentration"]["traded_days"] == 12
    assert plain["insights"] == with_cap["insights"]  # recorded P&L over the reference base


def test_insights_follow_the_strategy_filter(ledger):
    _, orb, _ = svc.get_daily_performance("ORB", "all")
    _, both, _ = svc.get_daily_performance(None, "all")
    assert orb["insights"]["concentration"]["trades"] == 24
    assert set(orb["insights"]["concentration"]["by_strategy"]) == {"ORB"}
    assert both["insights"]["concentration"]["trades"] == 48
    _, excl, _ = svc.get_daily_performance(None, "all", exclude=["ORB"])
    assert set(excl["insights"]["concentration"]["by_strategy"]) == {"VWAP"}


def test_comparison_rows_have_the_new_columns_and_portfolio_blocks(ledger):
    ok, r, _ = svc.get_strategy_comparison("all")
    assert ok
    row = r["strategies"][0]
    for key in ("gross_profit_factor", "net_profit_factor", "return_on_capital_pct", "max_drawdown_pct",
                "recovery_factor", "top1_day_share_pct", "overall_grade"):  # fmt: skip
        assert key in row
    assert r["portfolio"]["net"] is not None
    assert r["correlation"]["strategies"] == ["ORB", "VWAP"]
    assert {c["strategy"] for c in r["contribution"]} == {"ORB", "VWAP"}


def test_excursion_endpoint_service_reads_bars_and_reports(ledger, monkeypatch):
    import pandas as pd

    monkeypatch.setattr(
        enr, "_default_bar_reader", lambda *a: pd.DataFrame({"high": [101.0], "low": [99.0]})
    )
    ok, r, _ = svc.get_excursion_quality(None, "all")
    assert ok and r["excursions"]["covered"] == 48
    assert r["excursions"]["avg_mfe_r"] is None  # no signal stop, so no R
    monkeypatch.setattr(svc, "EXPORT_EXCURSION_MAX_TRADES", 10)
    ok, r, _ = svc.get_excursion_quality(None, "all")
    assert r["excursions"] is None and "Too many trades" in r["message"]


# ---- golden ----------------------------------------------------------------------------

GOLDEN_CSV = Path(
    os.environ.get(
        "OPENALGO_GOLDEN_TRADES_CSV",
        "/mnt/c/Users/Anand/Downloads/strategy-trades-2026-09-25_2026-11-03.csv",
    )
)


@pytest.fixture(scope="module")
def golden():
    if not GOLDEN_CSV.exists():
        pytest.skip("golden trades export not present")
    with GOLDEN_CSV.open(newline="") as f:
        return list(csv.DictReader(f))


def _golden_rows(golden):
    return [
        _row(
            float(r["net_pnl"]),
            r["trade_date"],
            r["strategy"],
            closed=r["closed_at"][11:19],
            opened_at=r["opened_at"] or None,
            direction=r["direction"],
        )
        for r in golden
    ]


def test_golden_concentration_matches_the_audit(golden):
    c = ps.concentration(_golden_rows(golden))
    assert c["net"] == pytest.approx(13919.15, abs=0.01)
    assert c["top1_days_share_pct"] == pytest.approx(45.6, abs=0.05)
    assert c["top3_days_share_pct"] == pytest.approx(79.8, abs=0.05)
    assert c["top5_days_share_pct"] == pytest.approx(96.9, abs=0.1)
    assert c["net_ex_best_day"] == pytest.approx(7572.78, abs=0.01)
    assert c["net_ex_best_five_days"] == pytest.approx(424.81, abs=0.01)
    assert c["median_daily"] == pytest.approx(116.29, abs=0.01)
    assert c["profitable_days"] == 16 and c["traded_days"] == 25
    assert c["best_day_date"] == "2026-10-28"
    bt = c["by_strategy"]["BREAKINGTRADE"]
    assert bt["net_ex_portfolio_best_day"] == pytest.approx(2885.62, abs=0.01)
    assert c["by_strategy"]["ORB"]["net"] == pytest.approx(4687.16, abs=0.01)


def test_golden_streaks_and_pf(golden):
    rows = _golden_rows(golden)
    assert ps.losing_streaks(rows)["max_consecutive_losses"] >= 1
    nets = [r["net"] for r in rows]
    assert ps.profit_factor(nets) == pytest.approx(1.7785, abs=0.001)
    by = {n: [r["net"] for r in rows if r["strategy"] == n] for n in ("ORB", "BREAKINGTRADE")}
    assert ps.profit_factor(by["BREAKINGTRADE"]) == pytest.approx(1.92, abs=0.01)
    assert ps.profit_factor(by["ORB"]) == pytest.approx(1.59, abs=0.01)


def test_return_metrics_need_ten_sessions_as_well_as_thirty_trades():
    """Forty-four trades over five sessions are five samples of the market, not forty-four."""
    r = ps.grade_report(_good_values(), trades=44, sessions=5)
    assert r["metrics"]["net_profit_factor"]["grade"] is None
    assert "10 trading sessions" in r["metrics"]["net_profit_factor"]["reason"]
    assert r["overall"] is None
    ok = ps.grade_report(_good_values(), trades=44, sessions=10)
    assert ok["metrics"]["net_profit_factor"]["grade"] == "excellent"


def test_overall_note_uses_plain_metric_names_and_ends_with_a_full_stop():
    r = ps.grade_report(_good_values(cost_pct_of_gross=None), trades=100, sessions=80)
    assert r["overall"] is None
    assert r["overall_note"].startswith("Not graded: charges as a share of gross profit")
    assert r["overall_note"].endswith(".") and "_" not in r["overall_note"]


# ---- golden: scenarios (strategy filter, date range, capital) ---------------------------


def _golden_enriched(golden):
    """Trade dicts shaped like enriched ledger rows, from the export's own columns."""
    out = []
    for r in golden:
        pnl, net = float(r["realized_pnl"]), float(r["net_pnl"])
        rm = float(r["r_multiple"]) if r["r_multiple"] else None
        out.append(
            {
                "strategy": r["strategy"],
                "symbol": r["symbol"],
                "trade_date": r["trade_date"],
                "opened_at": r["opened_at"] or None,
                "closed_at": r["closed_at"],
                "direction": r["direction"],
                "product": r["product"],
                "entry_price": float(r["entry_price"]),
                "closed_quantity": float(r["closed_quantity"]),
                "realized_pnl": pnl,
                "cost": float(r["est_cost"]),
                "net_r_multiple": rm * net / pnl if rm is not None and pnl else None,
            }
        )
    for t in out:
        t["data_flag"] = enr.trade_data_flag(t)
    return out


def _scenarios(rows):
    return {
        "combined": rows,
        "BREAKINGTRADE": [t for t in rows if t["strategy"] == "BREAKINGTRADE"],
        "ORB": [t for t in rows if t["strategy"] == "ORB"],
        "october_only": [t for t in rows if t["trade_date"].startswith("2026-10")],
        "first_ten_days": [t for t in rows if t["trade_date"] <= "2026-10-08"],
        "empty": [],
    }


def test_golden_scenarios_match_an_independent_pandas_calculation(golden):
    pd = pytest.importorskip("pandas")
    rows = _golden_enriched(golden)
    for name, subset in _scenarios(rows).items():
        sessions = ps.trading_sessions(
            "2026-09-25", "2026-11-03", traded_dates=[t["trade_date"] for t in subset]
        )
        ins = build_insights(subset, sessions, None, None)
        c = ins["concentration"]
        if not subset:
            assert c == {"traded_days": 0, "trades": 0}, name
            assert (
                ins["net_profit_factor"] is None and ins["streaks"]["max_consecutive_losses"] == 0
            )
            continue
        df = pd.DataFrame(subset)
        df["net"] = df["realized_pnl"] - df["cost"]
        daily = df.groupby("trade_date")["net"].sum().sort_values(ascending=False)
        total = float(daily.sum())
        assert c["net"] == pytest.approx(total, abs=0.01), name
        assert c["traded_days"] == len(daily) and c["trades"] == len(df), name
        assert c["median_daily"] == pytest.approx(float(daily.median()), abs=0.01), name
        assert c["net_ex_best_day"] == pytest.approx(total - daily.iloc[0], abs=0.01), name
        if total > 0:
            assert c["top1_days_share_pct"] == pytest.approx(100 * daily.iloc[0] / total, abs=0.01)
            assert c["top3_days_share_pct"] == pytest.approx(
                100 * daily.iloc[:3].sum() / total, abs=0.01
            )
        gains, losses = df.net[df.net > 0].sum(), -df.net[df.net < 0].sum()
        assert ins["net_profit_factor"] == pytest.approx(gains / losses, abs=0.01), name
        g_gain, g_loss = (
            df.realized_pnl[df.realized_pnl > 0].sum(),
            -df.realized_pnl[df.realized_pnl < 0].sum(),
        )
        assert ins["gross_profit_factor"] == pytest.approx(g_gain / g_loss, abs=0.01), name
        eq = daily.sort_index().reindex(sessions, fill_value=0.0).cumsum()
        dd = float(min(0.0, (eq - eq.cummax().clip(lower=0.0)).min()))
        assert ins["drawdown"]["max_drawdown"] == pytest.approx(dd, abs=0.01), name
        assert ins["drawdown"]["net"] == pytest.approx(total, abs=0.01), name
        assert sum(m["net"] for m in ins["monthly"]) == pytest.approx(total, abs=0.01), name
        if len(sessions) < ps.MIN_SESSIONS:  # every golden range is short
            assert ins["grading"]["overall"] is None, name
            assert ins["grading"]["metrics"]["sharpe"]["grade"] is None, name


def test_golden_strategy_blocks_add_up_to_the_portfolio(golden):
    rows = _golden_enriched(golden)
    sessions = ps.trading_sessions(
        "2026-09-25", "2026-11-03", traded_dates=[t["trade_date"] for t in rows]
    )
    by = {n: [t for t in rows if t["strategy"] == n] for n in ("BREAKINGTRADE", "ORB")}
    x = comparison_extras(by, sessions, {})
    assert x["per_strategy"]["BREAKINGTRADE"]["net_profit_factor"] == pytest.approx(1.92, abs=0.01)
    assert x["per_strategy"]["ORB"]["net_profit_factor"] == pytest.approx(1.59, abs=0.01)
    assert x["portfolio"]["net"] == pytest.approx(9231.99 + 4687.16, abs=0.01)
    assert x["correlation"]["matrix"][0][1] == pytest.approx(-0.032, abs=0.005)
    contrib = {c["strategy"]: c for c in x["contribution"]}
    assert contrib["BREAKINGTRADE"]["drawdown_change"] == pytest.approx(586.60, abs=0.01)
    assert contrib["ORB"]["drawdown_change"] == pytest.approx(-190.48, abs=0.01)


def test_golden_r_is_invariant_and_ratios_follow_the_capital_rules(golden):
    rows = _golden_enriched(golden)
    sessions = ps.trading_sessions(
        "2026-09-25", "2026-11-03", traded_dates=[t["trade_date"] for t in rows]
    )
    daily = ps.daily_totals(trade_rows(rows))
    pnl = ps.daily_pnl_values(daily, sessions)
    ref = ps.risk_adjusted_summary(pnl, 200000)
    # R multiples carry no capital: building insights never touches them
    r_net = [t["net_r_multiple"] for t in rows if t["net_r_multiple"] is not None]
    assert sum(r_net) / len(r_net) == pytest.approx(0.1824, abs=0.001)
    for capital in (100000, 400000, 2_000_000):
        out = ps.risk_adjusted_summary(pnl, capital)
        # 28 sessions: withheld at every capital, with the same counts
        assert out["sharpe_ratio"] is None and out["sortino_ratio"] is None
        assert out["ratio_sessions"] == ref["ratio_sessions"] == len(sessions)
        # the ungated ratios do not move with capital at zero rf and target
        a = ps.sharpe_ratio(ps.daily_returns(pnl, 200000))
        b = ps.sharpe_ratio(ps.daily_returns(pnl, capital))
        assert a == pytest.approx(b, rel=1e-12)
        s1 = ps.sortino_ratio(ps.daily_returns(pnl, 200000))
        s2 = ps.sortino_ratio(ps.daily_returns(pnl, capital))
        assert s1 == pytest.approx(s2, rel=1e-12)
    # a non-zero risk-free rate or target does depend on capital
    r1, r2 = ps.daily_returns(pnl, 200000), ps.daily_returns(pnl, 400000)
    assert ps.sharpe_ratio(r1, 0.07) != pytest.approx(ps.sharpe_ratio(r2, 0.07))
    assert ps.sortino_ratio(r1, 0.0005) != pytest.approx(ps.sortino_ratio(r2, 0.0005))


def test_preliminary_grade_is_return_metrics_only_and_never_validated():
    r = ps.grade_report(_good_values(), trades=44, sessions=12)
    assert r["overall"] is None and r["assessment"] == "preliminary"
    assert r["preliminary"] == "excellent"
    weak = ps.grade_report(_good_values(net_profit_factor=1.1), trades=44, sessions=12)
    assert weak["preliminary"] == "acceptable"  # weakest of the three return metrics
    assert ps.grade_report(_good_values(), trades=100, sessions=80)["assessment"] == "validated"
    assert ps.grade_report(_good_values(), trades=100, sessions=80)["preliminary"] is None
    thin = ps.grade_report(_good_values(), trades=44, sessions=5)
    assert thin["preliminary"] is None and thin["assessment"] == "none"
