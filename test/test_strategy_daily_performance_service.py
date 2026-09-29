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
