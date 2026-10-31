"""Holding-time and entry-hour analytics (pure functions, known answers).

Run with: uv run pytest test/test_strategy_hold_analytics.py -v
"""

from datetime import date

import pandas as pd
import pytest

import services.strategy_daily_performance_service as svc
from services import strategy_trade_enrichment as enr


def _t(pnl, opened, closed, symbol="AAA", trade_date="2026-10-30"):
    return {
        "strategy": "ORB",
        "symbol": symbol,
        "exchange": "NSE",
        "product": "MIS",
        "mode": "analyze",
        "direction": "LONG",
        "closed_quantity": 10.0,
        "entry_price": 100.0,
        "exit_price": 100.0 + pnl / 10,
        "realized_pnl": pnl,
        "trade_date": trade_date,
        "opened_at": opened,
        "closed_at": closed,
    }


D = "2026-10-30"
TRADES = [
    _t(100.0, f"{D}T09:20:00", f"{D}T09:30:00"),  # 10 min win
    _t(-40.0, f"{D}T09:45:00", f"{D}T10:45:00"),  # 60 min loss
    _t(60.0, f"{D}T10:05:00", f"{D}T12:05:00"),  # 120 min win
    _t(-20.0, f"{D}T10:15:00", f"{D}T14:15:00"),  # 240 min loss
    _t(30.0, "2026-10-29T14:00:00", f"{D}T09:30:00"),  # overnight 1170 min win
    _t(5.0, None, f"{D}T09:30:00"),  # no entry time
]


def test_hold_minutes():
    assert enr.hold_minutes(TRADES[0]) == 10.0
    assert enr.hold_minutes(TRADES[4]) == 1170.0
    assert enr.hold_minutes(TRADES[5]) is None
    assert enr.hold_minutes(_t(1, f"{D}T10:00:00", f"{D}T09:00:00")) is None
    assert enr.hold_minutes({"closed_at": f"{D}T09:00:00"}) is None
    assert enr.hold_minutes(_t(1, "garbage", f"{D}T09:00:00")) is None


def test_hold_stats_known_answer():
    s = enr.hold_stats(TRADES)
    assert s["covered"] == 5 and s["total"] == 6
    assert s["avg_min"] == 320.0  # (10+60+120+240+1170)/5
    assert s["median_min"] == 120.0
    assert s["p90_min"] == 798.0  # 240 + 0.6 * (1170 - 240)
    assert s["winners_avg_min"] == 433.3  # (10+120+1170)/3
    assert s["losers_avg_min"] == 150.0


def test_hold_stats_without_coverage():
    s = enr.hold_stats([TRADES[5]])
    assert s == {
        "avg_min": None,
        "median_min": None,
        "p90_min": None,
        "winners_avg_min": None,
        "losers_avg_min": None,
        "covered": 0,
        "total": 1,
    }
    assert enr.hold_stats([])["total"] == 0


def test_hold_stats_no_losers():
    s = enr.hold_stats([TRADES[0]])
    assert s["losers_avg_min"] is None and s["winners_avg_min"] == 10.0


def test_entry_hour_breakdown():
    rows = enr.entry_hour_breakdown(TRADES)
    assert [r["hour"] for r in rows] == [9, 10, 14]
    nine = rows[0]
    assert nine == {"hour": 9, "trades": 2, "net_pnl": 60.0, "win_rate": 50.0, "avg_hold_min": 35.0}
    ten = rows[1]
    assert ten["trades"] == 2 and ten["net_pnl"] == 40.0 and ten["win_rate"] == 50.0
    assert ten["avg_hold_min"] == 180.0
    assert rows[2]["avg_hold_min"] == 1170.0


def test_hold_bucket_breakdown_order_and_content():
    rows = enr.hold_bucket_breakdown(TRADES)
    assert [r["bucket"] for r in rows] == [
        "<15 min",
        "1-3 h",
        "3 h+ same day",
        "Overnight",
    ]
    by = {r["bucket"]: r for r in rows}
    assert by["<15 min"] == {
        "bucket": "<15 min",
        "trades": 1,
        "net_pnl": 100.0,
        "win_rate": 100.0,
        "avg_pnl": 100.0,
    }
    # 60 min belongs to 1-3 h (15-60 is [15, 60)), 120 min to 1-3 h
    assert by["1-3 h"]["trades"] == 2 and by["1-3 h"]["net_pnl"] == 20.0
    assert by["3 h+ same day"]["trades"] == 1
    assert by["Overnight"]["net_pnl"] == 30.0


def test_hold_bucket_skips_empty_buckets():
    rows = enr.hold_bucket_breakdown([TRADES[0]])
    assert [r["bucket"] for r in rows] == ["<15 min"]


def test_hold_bucket_boundaries():
    def b(mins):
        return enr._hold_bucket(
            _t(1, f"{D}T09:00:00", f"{D}T{9 + mins // 60:02d}:{mins % 60:02d}:00")
        )

    assert b(14) == "<15 min" and b(15) == "15-60 min" and b(59) == "15-60 min"
    assert b(60) == "1-3 h" and b(179) == "1-3 h" and b(180) == "3 h+ same day"


def test_with_hold_minutes_copies():
    out = enr.with_hold_minutes(TRADES)
    assert out[0]["hold_min"] == 10.0 and out[5]["hold_min"] is None
    assert "hold_min" not in TRADES[0]


def test_excursions_use_trade_opened_at_over_signal_match(monkeypatch):
    seen = {}

    def reader(symbol, exchange, start_ts, end_ts):
        seen["start"] = start_ts
        return pd.DataFrame({"high": [110.0], "low": [95.0]})

    trade = _t(50.0, f"{D}T09:20:30", f"{D}T09:40:00")
    monkeypatch.setattr(enr, "_load_signal_entries", lambda *a, **k: {})
    out, covered, total = enr.add_excursions([trade], D, D, bar_reader=reader)
    assert covered == 1 and total == 1
    assert out[0]["mfe"] == 100.0 and out[0]["mae"] == 50.0
    assert seen["start"] == enr._to_epoch(f"{D}T09:20:30") // 60 * 60


def test_excursions_fall_back_to_signal_match_when_opened_at_missing(monkeypatch):
    called = {}
    monkeypatch.setattr(enr, "_load_signal_entries", lambda *a, **k: {"x": 1})
    monkeypatch.setattr(
        enr, "_match_entry_time", lambda trade, index: called.setdefault("t", f"{D}T09:21:00")
    )
    reader = lambda *a: pd.DataFrame({"high": [101.0], "low": [99.0]})  # noqa: E731
    out, covered, _ = enr.add_excursions([_t(5.0, None, f"{D}T09:40:00")], D, D, bar_reader=reader)
    assert called and covered == 1


@pytest.fixture
def _patch(monkeypatch, tmp_path):
    trades = [dict(t) for t in TRADES]
    monkeypatch.setattr(svc, "get_closed_trades", lambda **kw: list(trades))
    monkeypatch.setattr(svc, "_current_mode", lambda: "analyze")
    monkeypatch.setattr(svc, "_today", lambda: date(2026, 10, 30))
    monkeypatch.setattr(svc, "list_strategies", lambda: ["ORB"])
    monkeypatch.setattr(enr, "SIGNAL_TRADES_DB", tmp_path / "absent.db")
    monkeypatch.setattr("services.strategy_metrics_service.LEDGER_RELIABLE_SINCE", date(2020, 1, 1))


def test_daily_response_carries_hold_sections_on_every_period(_patch):
    ok, r, _ = svc.get_daily_performance(None, "7d")
    assert ok and "trades" not in r
    assert r["hold_stats"]["covered"] == 5
    assert [x["hour"] for x in r["by_entry_hour"]] == [9, 10, 14]
    assert r["by_hold_bucket"][-1]["bucket"] == "Overnight"


def test_single_day_trades_have_opened_at_and_hold_min(_patch):
    ok, r, _ = svc.get_daily_performance(None, "1d")
    assert ok
    by_pnl = {t["realized_pnl"]: t for t in r["trades"]}
    assert by_pnl[100.0]["opened_at"] == f"{D}T09:20:00" and by_pnl[100.0]["hold_min"] == 10.0
    assert by_pnl[5.0]["hold_min"] is None


def test_comparison_rows_have_avg_and_median_hold(_patch):
    ok, c, _ = svc.get_strategy_comparison("7d")
    row = c["strategies"][0]
    assert row["avg_hold_min"] == 320.0 and row["median_hold_min"] == 120.0


def test_comparison_row_hold_none_when_uncovered(_patch, monkeypatch):
    monkeypatch.setattr(svc, "get_closed_trades", lambda **kw: [dict(TRADES[5])])
    ok, c, _ = svc.get_strategy_comparison("7d")
    row = c["strategies"][0]
    assert row["avg_hold_min"] is None and row["median_hold_min"] is None


def test_old_fixture_without_opened_at_key_is_tolerated(_patch, monkeypatch):
    old = {k: v for k, v in TRADES[0].items() if k != "opened_at"}
    monkeypatch.setattr(svc, "get_closed_trades", lambda **kw: [old])
    ok, r, _ = svc.get_daily_performance(None, "7d")
    assert ok and r["hold_stats"]["covered"] == 0 and r["by_entry_hour"] == []


def test_export_trades_carry_hold_min(_patch):
    ok, body, _ = svc.get_trades_for_export(None, "7d")
    assert ok and all("hold_min" in t for t in body["trades"])


def test_export_columns_place_hold_after_closed_at():
    from blueprints.strategy_pnl import _NUMERIC_COLUMNS, EXPORT_COLUMNS

    assert EXPORT_COLUMNS == tuple(
        (
            "trade_date opened_at closed_at hold_min entry_hour weekday mode strategy broker "
            "symbol exchange product direction closed_quantity entry_price exit_price sl_price "
            "realized_pnl est_cost net_pnl return_pct r_multiple entry_slippage mfe mae"
        ).split()
    )
    assert {"hold_min", "net_pnl", "return_pct", "mfe", "mae", "sl_price", "entry_hour"} <= (
        _NUMERIC_COLUMNS
    )


def test_export_rows_carry_quant_fields(_patch):
    ok, body, _ = svc.get_trades_for_export(None, "7d")
    assert ok
    t = body["trades"][0]
    assert t["closed_at"].endswith("+05:30")
    assert t["weekday"] in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    for key in ("net_pnl", "return_pct", "sl_price", "entry_hour", "mfe", "mae"):
        assert key in t
    if t["cost"] is None:
        assert t["net_pnl"] is None
    else:
        assert t["net_pnl"] == round(t["realized_pnl"] - t["cost"], 2)
    assert t["return_pct"] == round(
        t["realized_pnl"] / (t["entry_price"] * t["closed_quantity"]) * 100, 4
    )


def test_export_skips_excursions_above_cap(_patch, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("excursions must be skipped")

    monkeypatch.setattr(svc, "add_excursions", boom)
    monkeypatch.setattr(svc, "EXPORT_EXCURSION_MAX_TRADES", 0)
    ok, body, _ = svc.get_trades_for_export(None, "7d")
    assert ok and all(t.get("mfe") is None and t.get("mae") is None for t in body["trades"])


def test_with_export_fields_known_answer():
    row = {
        "trade_date": "2026-10-30",
        "opened_at": "2026-10-30T09:20:00",
        "closed_at": "2026-10-30T10:40:00",
        "realized_pnl": 50.0,
        "cost": 8.0,
        "entry_price": 100.0,
        "closed_quantity": 10.0,
        "signal_sl": 98.5,
    }
    out = enr.with_export_fields([row, {**row, "cost": None, "opened_at": None}])
    assert out[0]["opened_at"] == "2026-10-30T09:20:00+05:30"
    assert out[0]["net_pnl"] == 42.0 and out[0]["return_pct"] == 5.0
    assert out[0]["sl_price"] == 98.5 and out[0]["entry_hour"] == 9
    assert out[0]["weekday"] == "Fri"
    assert out[1]["net_pnl"] is None and out[1]["opened_at"] is None
    assert out[1]["entry_hour"] is None


def test_daily_and_compare_carry_edge_stats(_patch):
    ok, r, _ = svc.get_daily_performance(None, "7d")
    assert ok
    for key in ("net_expectancy", "t_stat", "expectancy_ci95", "edge_verdict", "edge_trades"):
        assert key in r
    ok, c, _ = svc.get_strategy_comparison("7d")
    for row in c["strategies"]:
        assert {"net_expectancy", "t_stat", "edge_verdict"} <= set(row)
