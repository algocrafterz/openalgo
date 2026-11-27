"""Route tests for GET /api/strategy-pnl/market-state and /market-state/today.

Read-only analytics: the tests pin auth, the server-side mode in the single-flight key,
live and analyze separation, the discretionary source, default parameters and bad parameters.
"""

from datetime import datetime

import pandas as pd
import pytest
from flask import Flask

import blueprints.auth as auth_bp_module
import blueprints.strategy_pnl as strategy_pnl_module
import services.market_state_report_service as report_module
import services.strategy_daily_performance_service as daily_module
from limiter import limiter
from signal_engine.timeutils import IST
from utils.single_flight import SingleFlight


def _bars():
    """300 rising business-day bars ending 2026-11-26."""
    days = pd.bdate_range(end="2026-11-26", periods=300)
    close = [100.0 + i for i in range(len(days))]
    return pd.DataFrame(
        {
            "date": [d.date() for d in days],
            "open": close,
            "high": [c + 1 for c in close],
            "low": [c - 1 for c in close],
            "close": close,
            "volume": [1000] * len(days),
        }
    )


def _trade(day, pnl, strategy="ORB", mode="live"):
    return {
        "strategy": strategy,
        "symbol": "NIFTY",
        "exchange": "NFO",
        "direction": "LONG",
        "mode": mode,
        "trade_date": day,
        "realized_pnl": pnl,
        "closed_quantity": 1,
        "entry_price": 100.0,
        "exit_price": 101.0,
    }


def _app():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(strategy_pnl_module.strategy_pnl_bp)
    app.register_blueprint(auth_bp_module.auth_bp)
    return app


def _log_in(client):
    with client.session_transaction() as session:
        session["user"] = "rajandran"
        session["logged_in"] = True
        session["login_time"] = datetime.now(IST).isoformat()
        session["broker"] = "flattrade"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    flights = SingleFlight(join_window=5, wait_timeout=10)
    monkeypatch.setattr(strategy_pnl_module, "_pnl_flights", flights)
    state = {"analyze": False, "ledger_reads": []}
    ledgers = {
        "live": [_trade("2026-11-24", 500.0), _trade("2026-11-25", -200.0)],
        "analyze": [_trade("2026-11-24", 999.0, mode="analyze")],
    }
    monkeypatch.setattr(strategy_pnl_module, "get_analyze_mode", lambda: state["analyze"])

    def closed(mode):
        state["ledger_reads"].append(mode)
        return list(ledgers[mode])

    monkeypatch.setattr(daily_module, "get_closed_trades", closed)
    monkeypatch.setattr(
        daily_module, "enrich_trades", lambda trades, s, e: [dict(t, cost=10.0) for t in trades]
    )
    monkeypatch.setattr(daily_module, "_today", lambda: datetime(2026, 11, 27).date())
    monkeypatch.setattr(report_module.ms, "read_benchmark_bars", _bars)
    client = _app().test_client()
    _log_in(client)
    return client, state, flights


def test_requires_a_logged_in_session():
    client = _app().test_client()
    assert client.get("/api/strategy-pnl/market-state").status_code in (302, 401)
    assert client.get("/api/strategy-pnl/market-state/today").status_code in (302, 401)


def test_defaults_to_strategy_source_and_server_mode(env):
    client, _state, _flights = env
    body = client.get("/api/strategy-pnl/market-state").get_json()
    assert body["status"] == "success"
    assert body["source"] == "strategy"
    assert body["mode"] == "live"
    assert body["period"] == "30d"
    assert body["benchmark"] == "NIFTY 50 (daily)"
    assert body["trades_count"] == 2
    assert [r["name"] for r in body["trend"]] == ["Uptrend", "Downtrend", "Range", "Unclassified"]
    assert [r["name"] for r in body["volatility"]] == ["Low", "Normal", "High", "Unclassified"]
    # a steadily rising series is an Uptrend on both trade dates
    assert body["trend"][0]["trades"] == 2
    assert body["trend"][0]["gross_pnl"] == 300.0
    assert body["trend"][0]["net_pnl"] == 280.0
    assert body["data_status"]["state"] == "ok"


def test_analyze_and_live_are_separated_and_mode_is_in_the_key(env):
    client, state, flights = env
    live = client.get("/api/strategy-pnl/market-state").get_json()
    state["analyze"] = True
    analyze = client.get("/api/strategy-pnl/market-state").get_json()
    assert live["mode"] == "live" and live["trades_count"] == 2
    assert analyze["mode"] == "analyze" and analyze["trades_count"] == 1
    assert analyze["trend"][0]["gross_pnl"] == 999.0
    assert state["ledger_reads"] == ["live", "analyze"]
    # the same request in the other mode was not served from the first mode's result
    state["analyze"] = False
    again = client.get("/api/strategy-pnl/market-state").get_json()
    assert again["mode"] == "live" and again["trades_count"] == 2


def test_the_client_cannot_choose_the_mode(env):
    client, _state, _flights = env
    body = client.get("/api/strategy-pnl/market-state?mode=analyze").get_json()
    assert body["mode"] == "live"


def test_strategy_filter_is_applied(env):
    client, _state, _flights = env
    body = client.get("/api/strategy-pnl/market-state?strategy=OTHER").get_json()
    assert body["trades_count"] == 0
    body = client.get("/api/strategy-pnl/market-state?exclude=ORB").get_json()
    assert body["trades_count"] == 0


def test_discretionary_source_uses_its_own_rows_and_is_live(env, monkeypatch):
    client, _state, _flights = env
    import blueprints.discretionary_pnl as disc_bp
    import services.discretionary_pnl_service as disc

    monkeypatch.setattr(disc_bp, "discretionary_enabled", lambda: True)
    seen = {}

    def rows(period, start, end, broker, symbol, account, category):
        seen["args"] = (period, start, end, broker, symbol, account, category)
        return {
            "start_date": "2026-11-01",
            "end_date": "2026-11-27",
            "rows": [
                {"trade_date": "2026-11-24", "realized_pnl": 100.0, "cost": 5.0},
                {"trade_date": "2026-11-25", "realized_pnl": -40.0, "cost": None},
            ],
        }

    monkeypatch.setattr(disc, "get_completed_trade_rows", rows)
    body = client.get(
        "/api/strategy-pnl/market-state?source=discretionary&broker=zerodha&category=intraday"
    ).get_json()
    assert body["source"] == "discretionary" and body["mode"] == "live"
    assert seen["args"] == ("30d", None, None, "zerodha", None, None, "intraday")
    row = body["trend"][0]
    assert row["trades"] == 2 and row["gross_pnl"] == 60.0
    assert row["net_pnl"] is None and row["net_coverage"] == {"costed": 1, "total": 2}


def test_discretionary_source_off_is_a_404_with_a_plain_message(env, monkeypatch):
    client, _state, _flights = env
    import blueprints.discretionary_pnl as disc_bp

    monkeypatch.setattr(disc_bp, "discretionary_enabled", lambda: False)
    response = client.get("/api/strategy-pnl/market-state?source=discretionary")
    assert response.status_code == 404
    assert "turned off" in response.get_json()["message"]


@pytest.mark.parametrize(
    "query",
    [
        "source=crypto",
        "period=forever",
        "period=custom&start=2026-11-10&end=nonsense",
        "capital=abc",
        "capital=-5",
    ],
)
def test_bad_parameters_are_a_400(env, query):
    client, _state, _flights = env
    response = client.get(f"/api/strategy-pnl/market-state?{query}")
    assert response.status_code == 400
    assert response.get_json()["status"] == "error"


def test_a_service_failure_is_a_plain_500_without_internals(env, monkeypatch):
    client, _state, _flights = env

    def boom(*_a, **_k):
        raise RuntimeError("duckdb exploded at /secret/path")

    monkeypatch.setattr(report_module, "get_strategy_market_state", boom)
    response = client.get("/api/strategy-pnl/market-state")
    assert response.status_code == 500
    assert "duckdb" not in response.get_data(as_text=True)


def test_empty_benchmark_gives_unclassified_not_a_class(env, monkeypatch):
    client, _state, _flights = env
    monkeypatch.setattr(report_module.ms, "read_benchmark_bars", lambda: pd.DataFrame())
    body = client.get("/api/strategy-pnl/market-state").get_json()
    assert body["data_status"]["state"] == "no_data"
    assert body["trend"][3]["name"] == "Unclassified" and body["trend"][3]["trades"] == 2
    assert all(r["trades"] == 0 for r in body["trend"][:3])
    assert body["unclassified"]["trend"]["reasons"] == {"no_bars": 2}


def test_today_returns_the_label_of_the_previous_bar(env, monkeypatch):
    client, _state, _flights = env
    monkeypatch.setattr("utils.session.get_trading_session_date", lambda: "2026-11-27")
    body = client.get("/api/strategy-pnl/market-state/today").get_json()
    assert body["status"] == "success"
    assert body["session_date"] == "2026-11-27"
    assert body["as_of"] == "2026-11-26"
    assert body["trend"] == "Uptrend"
    assert body["volatility"] in ("Low", "Normal", "High")


def test_today_without_data_is_unclassified(env, monkeypatch):
    client, _state, _flights = env
    monkeypatch.setattr(report_module.ms, "read_benchmark_bars", lambda: pd.DataFrame())
    monkeypatch.setattr("utils.session.get_trading_session_date", lambda: "2026-11-27")
    body = client.get("/api/strategy-pnl/market-state/today").get_json()
    assert body["trend"] is None and body["volatility"] is None
    assert body["label_status"] == "unclassified" and body["reason"] == "no_bars"
