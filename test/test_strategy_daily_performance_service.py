"""
Unit tests for services/strategy_daily_performance_service.py - the
orchestration layer between the closed-trade ledger and the metrics math.

Run with: uv run pytest test/test_strategy_daily_performance_service.py -v
"""

import services.strategy_daily_performance_service as perf


def _trade(strategy, pnl, trade_date):
    return {"strategy": strategy, "realized_pnl": pnl, "trade_date": trade_date}


def test_invalid_period_is_rejected():
    success, response, status = perf.get_daily_performance(strategy="ORB", period="bogus")

    assert success is False
    assert status == 400
    assert response["status"] == "error"


def test_live_mode_is_resolved_from_analyze_mode_flag(monkeypatch):
    monkeypatch.setattr(perf, "get_analyze_mode", lambda: False)
    captured = {}

    def fake_get_closed_trades(strategy=None, mode=None, **_kwargs):
        captured["strategy"] = strategy
        captured["mode"] = mode
        return [_trade("ORB", 100, "2026-01-01")]

    monkeypatch.setattr(perf, "get_closed_trades", fake_get_closed_trades)

    success, response, status = perf.get_daily_performance(strategy="ORB", period="30d")

    assert success is True
    assert status == 200
    assert captured == {"strategy": "ORB", "mode": "live"}
    assert response["mode"] == "live"
    assert response["strategy"] == "ORB"


def test_analyze_mode_is_resolved_from_analyze_mode_flag(monkeypatch):
    monkeypatch.setattr(perf, "get_analyze_mode", lambda: True)
    monkeypatch.setattr(perf, "get_closed_trades", lambda **_kwargs: [])

    _success, response, _status = perf.get_daily_performance(strategy="ORB", period="30d")

    assert response["mode"] == "analyze"


def test_daily_performance_computes_metrics_from_fetched_trades(monkeypatch):
    monkeypatch.setattr(perf, "get_analyze_mode", lambda: False)
    monkeypatch.setattr(
        perf,
        "get_closed_trades",
        lambda **_kwargs: [
            _trade("ORB", 100, "2026-01-01"),
            _trade("ORB", -40, "2026-01-02"),
        ],
    )

    _success, response, _status = perf.get_daily_performance(strategy="ORB", period="all")

    assert response["trades_count"] == 2
    assert response["net_profit"] == 60.0


def test_compare_skips_strategies_with_no_closed_trades(monkeypatch):
    monkeypatch.setattr(perf, "get_analyze_mode", lambda: False)
    monkeypatch.setattr(perf, "list_strategies", lambda: ["ORB", "EMPTY-STRAT"])

    def fake_get_closed_trades(strategy=None, **_kwargs):
        if strategy == "ORB":
            return [_trade("ORB", 100, "2026-01-01")]
        return []

    monkeypatch.setattr(perf, "get_closed_trades", fake_get_closed_trades)

    _success, response, _status = perf.get_strategy_comparison(period="all")

    names = [row["strategy"] for row in response["strategies"]]
    assert names == ["ORB"]


def test_compare_sorts_by_net_profit_descending(monkeypatch):
    monkeypatch.setattr(perf, "get_analyze_mode", lambda: False)
    monkeypatch.setattr(perf, "list_strategies", lambda: ["WORSE", "BETTER"])

    def fake_get_closed_trades(strategy=None, **_kwargs):
        if strategy == "BETTER":
            return [_trade("BETTER", 500, "2026-01-01")]
        return [_trade("WORSE", 50, "2026-01-01")]

    monkeypatch.setattr(perf, "get_closed_trades", fake_get_closed_trades)

    _success, response, _status = perf.get_strategy_comparison(period="all")

    names = [row["strategy"] for row in response["strategies"]]
    assert names == ["BETTER", "WORSE"]


def test_compare_with_no_strategies_returns_empty_list(monkeypatch):
    monkeypatch.setattr(perf, "get_analyze_mode", lambda: False)
    monkeypatch.setattr(perf, "list_strategies", lambda: [])

    success, response, status = perf.get_strategy_comparison(period="30d")

    assert success is True
    assert status == 200
    assert response["strategies"] == []
