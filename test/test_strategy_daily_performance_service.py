"""get_daily_performance response shape: enrichment fields on every period,
and the per-trade list only for the 1D view.

Run with: uv run pytest test/test_strategy_daily_performance_service.py -v
"""

from datetime import date

import pytest

import services.strategy_daily_performance_service as svc


def _t(symbol, pnl, closed_at, trade_date=None):
    return {
        "strategy": "ORB", "symbol": symbol, "exchange": "NSE", "product": "MIS",
        "mode": "analyze", "direction": "LONG", "closed_quantity": 10.0,
        "entry_price": 100.0, "exit_price": 100.0 + pnl / 10, "realized_pnl": pnl,
        "trade_date": trade_date or date.today().isoformat(), "closed_at": closed_at,
    }


@pytest.fixture(autouse=True)
def _patch(monkeypatch, tmp_path):
    today = date.today().isoformat()
    trades = [
        _t("AAA", 100.0, f"{today}T10:00:00"),
        _t("BBB", -50.0, f"{today}T11:00:00"),
    ]
    monkeypatch.setattr(svc, "get_closed_trades", lambda **kw: list(trades))
    monkeypatch.setattr(svc, "_current_mode", lambda: "analyze")
    monkeypatch.setattr(
        "services.strategy_trade_enrichment.SIGNAL_TRADES_DB", tmp_path / "absent.db"
    )
    monkeypatch.setattr("services.strategy_metrics_service.LEDGER_RELIABLE_SINCE", date(2020, 1, 1))


def test_one_day_includes_trade_list_newest_first():
    ok, r, code = svc.get_daily_performance(None, "1d")
    assert ok and code == 200
    assert [t["symbol"] for t in r["trades"]] == ["BBB", "AAA"]
    assert r["trades"][0]["cost"] is not None
    assert r["trades"][0]["r_multiple"] is None


def test_other_periods_omit_trade_list_but_carry_enrichment():
    ok, r, _ = svc.get_daily_performance(None, "7d")
    assert ok and "trades" not in r
    assert r["est_costs"] > 0
    assert r["net_after_costs"] == pytest.approx(50.0 - r["est_costs"], abs=0.01)
    assert r["expectancy_r"] is None and r["r_covered"] == 0 and r["r_total"] == 2
    assert [s["symbol"] for s in r["by_symbol"]] == ["AAA", "BBB"]


def test_charge_drag_is_in_the_summary_and_the_comparison_row(monkeypatch):
    ok, r, _ = svc.get_daily_performance(None, "7d")
    assert ok and r["charges_pct_of_gross"] == pytest.approx(100 * r["est_costs"] / 50.0, abs=0.01)  # gross is +100 - 50
    assert r["turnover"] > 0 and r["charges_pct_of_turnover"] > 0
    ok, c, _ = svc.get_strategy_comparison("7d")
    row = c["strategies"][0]
    assert row["est_costs"] == pytest.approx(r["est_costs"])
    assert row["net_after_costs"] == pytest.approx(r["net_after_costs"])
    assert row["charges_pct_of_gross"] == pytest.approx(r["charges_pct_of_gross"])


def _session_trades():
    return [
        _t("OLD", 30.0, "2026-10-29T10:00:00", trade_date="2026-10-29"),
        _t("AAA", 100.0, "2026-10-30T10:00:00", trade_date="2026-10-30"),
        {**_t("XYZ", -40.0, "2026-10-30T11:00:00", trade_date="2026-10-30"), "strategy": "VWAP"},
    ]


def test_one_day_on_a_non_trading_day_shows_the_last_session(monkeypatch):
    monkeypatch.setattr(svc, "get_closed_trades", lambda **kw: _session_trades())
    monkeypatch.setattr(svc, "_today", lambda: date(2026, 11, 1))
    ok, r, _ = svc.get_daily_performance(None, "1d")
    assert ok
    assert r["start_date"] == r["end_date"] == "2026-10-30"
    assert r["trades_count"] == 2
    assert {t["symbol"] for t in r["trades"]} == {"AAA", "XYZ"}


def test_one_day_for_one_strategy_uses_the_portfolio_session(monkeypatch):
    """The session date stays the same whichever strategy is selected, so
    the strategy view and the comparison table describe the same day."""
    trades = _session_trades()
    trades[-1] = {**trades[-1], "trade_date": "2026-10-31"}
    monkeypatch.setattr(svc, "get_closed_trades", lambda **kw: list(trades))
    monkeypatch.setattr(svc, "_today", lambda: date(2026, 11, 1))
    ok, r, _ = svc.get_daily_performance("ORB", "1d")
    assert ok and r["start_date"] == "2026-10-31"
    assert r["trades_count"] == 0


def test_custom_range_filters_trades(monkeypatch):
    monkeypatch.setattr(svc, "get_closed_trades", lambda **kw: _session_trades())
    monkeypatch.setattr(svc, "_today", lambda: date(2026, 11, 1))
    ok, r, _ = svc.get_daily_performance(None, "custom", start="2026-10-29", end="2026-10-29")
    assert ok and r["trades_count"] == 1 and r["net_profit"] == 30.0
    assert r["period"] == "custom"


def test_custom_range_is_rejected_when_invalid():
    ok, r, code = svc.get_daily_performance(None, "custom", start="2026-10-30", end="2026-10-01")
    assert not ok and code == 400 and r["status"] == "error"


def test_fifteen_day_period_is_accepted():
    ok, r, _ = svc.get_daily_performance(None, "15d")
    assert ok and r["calendar_days"] == 15


def test_comparison_groups_one_query_by_strategy(monkeypatch):
    calls = []

    def fake(**kw):
        calls.append(kw)
        return _session_trades()

    monkeypatch.setattr(svc, "get_closed_trades", fake)
    monkeypatch.setattr(svc, "_today", lambda: date(2026, 11, 1))
    ok, r, _ = svc.get_strategy_comparison("1d")
    assert ok and len(calls) == 1
    assert {row["strategy"]: row["net_profit"] for row in r["strategies"]} == {
        "ORB": 100.0,
        "VWAP": -40.0,
    }
    assert r["start_date"] == r["end_date"] == "2026-10-30"


def test_comparison_custom_range(monkeypatch):
    monkeypatch.setattr(svc, "get_closed_trades", lambda **kw: _session_trades())
    monkeypatch.setattr(svc, "_today", lambda: date(2026, 11, 1))
    ok, r, _ = svc.get_strategy_comparison("custom", start="2026-10-29", end="2026-10-29")
    assert ok and [row["strategy"] for row in r["strategies"]] == ["ORB"]
    ok, _, code = svc.get_strategy_comparison("custom", start="bad", end="2026-10-29")
    assert not ok and code == 400


def test_comparison_lists_strategies_from_the_trades_not_the_position_book(monkeypatch):
    """A strategy with closed trades but no position row (reset, pruned, or
    reconstructed history) must still appear in the comparison table."""
    monkeypatch.setattr(svc, "get_closed_trades", lambda **kw: _session_trades())
    monkeypatch.setattr(svc, "_today", lambda: date(2026, 11, 1))
    ok, r, _ = svc.get_strategy_comparison("15d")
    assert ok and sorted(row["strategy"] for row in r["strategies"]) == ["ORB", "VWAP"]


def _filter_trades():
    def trade(strategy, symbol, pnl, day):
        return {
            **_t(symbol, pnl, f"{day}T10:00:00", trade_date=day),
            "strategy": strategy,
        }

    return [
        trade("ORB", "AAA", 100.0, "2026-10-28"),
        trade("ORB", "AAA", -30.0, "2026-10-29"),
        trade("VWAP", "BBB", 50.0, "2026-10-29"),
        trade("VWAP", "BBB", -80.0, "2026-10-30"),
        trade("EMA", "CCC", 40.0, "2026-10-30"),
        trade("LATE", "DDD", 10.0, "2026-10-31"),
    ]


@pytest.fixture
def filter_ledger(monkeypatch):
    monkeypatch.setattr(svc, "get_closed_trades", lambda **kw: _filter_trades())
    monkeypatch.setattr(svc, "_today", lambda: date(2026, 11, 1))


def test_include_only_pools_just_those_strategies(filter_ledger):
    ok, r, _ = svc.get_daily_performance("ORB", "all", include=["VWAP"])
    assert ok and r["trades_count"] == 4
    assert r["net_profit"] == 40.0
    assert r["included"] == ["ORB", "VWAP"] and r["excluded"] == []
    assert r["strategy"] == "ORB"


def test_exclude_only_means_all_except(filter_ledger):
    ok, r, _ = svc.get_daily_performance(None, "all", exclude=["VWAP"])
    assert ok and r["trades_count"] == 4
    assert r["included"] == ["EMA", "LATE", "ORB"]
    assert r["excluded"] == ["VWAP"]


def test_include_and_exclude_together(filter_ledger):
    ok, r, _ = svc.get_daily_performance(
        None, "all", include=["ORB", "VWAP", "EMA"], exclude=["EMA"]
    )
    assert ok and r["included"] == ["ORB", "VWAP"] and r["trades_count"] == 4


def test_exclusion_wins_over_inclusion(filter_ledger):
    ok, r, _ = svc.get_daily_performance("ORB", "all", include=["VWAP"], exclude=["ORB"])
    assert ok and r["included"] == ["VWAP"] and r["trades_count"] == 2
    ok, r, _ = svc.get_daily_performance("ORB", "all", exclude=["ORB"])
    assert ok and r["trades_count"] == 0 and r["included"] == []


def test_pooled_ratios_come_from_the_filtered_trades(filter_ledger):
    from services.strategy_metrics_service import compute_metrics

    ok, r, _ = svc.get_daily_performance(None, "all", exclude=["VWAP", "LATE"])
    kept = [t for t in _filter_trades() if t["strategy"] in ("ORB", "EMA")]
    direct = compute_metrics(kept, r["start_date"], r["end_date"])
    assert ok
    assert r["sharpe_ratio"] == direct["sharpe_ratio"]
    assert r["max_drawdown"] == direct["max_drawdown"]
    assert r["net_profit"] == direct["net_profit"]


def test_comparison_rows_are_filtered_but_all_strategies_is_not(filter_ledger):
    ok, r, _ = svc.get_strategy_comparison("all", include=["ORB", "VWAP"], exclude=["VWAP"])
    assert ok and [row["strategy"] for row in r["strategies"]] == ["ORB"]
    assert r["all_strategies"] == ["EMA", "LATE", "ORB", "VWAP"]
    assert r["included"] == ["ORB"] and r["excluded"] == ["VWAP"]


def test_export_is_filtered(filter_ledger):
    ok, r, _ = svc.get_trades_for_export(None, "all", exclude=["VWAP", "ORB"])
    assert ok and {t["strategy"] for t in r["trades"]} == {"EMA", "LATE"}
    ok, r, _ = svc.get_trades_for_export("ORB", "all", include=["EMA"])
    assert ok and {t["strategy"] for t in r["trades"]} == {"ORB", "EMA"}


def test_one_day_window_ignores_the_filter(filter_ledger):
    """LATE traded on the latest session; filtering it out must not move 1D."""
    ok, r, _ = svc.get_daily_performance(None, "1d", exclude=["LATE"])
    assert ok and r["start_date"] == r["end_date"] == "2026-10-31"
    assert r["trades_count"] == 0
    ok, c, _ = svc.get_strategy_comparison("1d", exclude=["LATE"])
    assert ok and c["start_date"] == "2026-10-31" and c["strategies"] == []


def test_unknown_name_contributes_nothing(filter_ledger):
    ok, r, _ = svc.get_daily_performance(None, "all", include=["NOPE"])
    assert ok and r["trades_count"] == 0 and r["included"] == []


# ---- capital view: additive only ----------------------------------------------


def _without_capital_view(response):
    return {k: v for k, v in response.items() if k != "capital_view"}


def test_no_capital_means_no_capital_view_key():
    ok, r, _ = svc.get_daily_performance(None, "7d")
    assert ok and "capital_view" not in r


@pytest.mark.parametrize("period", ["1d", "7d"])
def test_capital_leaves_every_other_key_unchanged(period):
    _, plain, _ = svc.get_daily_performance(None, period)
    ok, with_cap, code = svc.get_daily_performance(None, period, capital=100000.0)
    assert ok and code == 200
    assert "capital_view" in with_cap
    assert _without_capital_view(with_cap) == plain
    for key in ("equity_curve", "equity_curve_net", "daily_pnl", "by_symbol"):
        assert with_cap[key] == plain[key]
    assert ("trades" in with_cap) == ("trades" in plain)


def test_capital_view_matches_the_response_metrics():
    ok, r, _ = svc.get_daily_performance(None, "7d", capital=50000.0)
    v = r["capital_view"]
    assert v["capital"] == 50000.0
    assert v["return_pct"] == round(r["net_profit"] / 50000.0 * 100, 2)
    assert v["return_net_pct"] == round(r["net_after_costs"] / 50000.0 * 100, 2)
    assert v["max_drawdown_pct_of_capital"] == round(r["max_drawdown"] / 50000.0 * 100, 2)
    assert v["best_day_pct"] == round(r["best_day"] / 50000.0 * 100, 2)
    assert v["worst_day_pct"] == round(r["worst_day"] / 50000.0 * 100, 2)


def test_capital_with_a_filter_is_invariant_and_uses_the_filtered_set(filter_ledger):
    _, plain, _ = svc.get_daily_performance("ORB", "all", include=["VWAP"], exclude=["EMA"])
    ok, with_cap, _ = svc.get_daily_performance(
        "ORB", "all", include=["VWAP"], exclude=["EMA"], capital=1000.0
    )
    assert ok
    assert _without_capital_view(with_cap) == plain
    assert with_cap["capital_view"]["return_pct"] == 4.0  # 40 rupees on 1000


def test_capital_on_an_empty_selection_is_safe(filter_ledger):
    ok, r, _ = svc.get_daily_performance(None, "all", include=["NOPE"], capital=1000.0)
    assert ok and r["capital_view"]["return_pct"] == 0.0
