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
