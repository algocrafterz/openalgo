"""
Route tests: the Strategy P&L and Performance endpoints attach a `data_health`
verdict and kick off a background reconcile, so the pages can warn when their
figures may be stale (missed fills after a crash/restart or a feed outage).

Every service call is stubbed, so no database or broker is touched.

Run with: uv run pytest test/test_strategy_pnl_data_health_route.py -v
"""

import os
import sys

import pytest
from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import blueprints.auth as auth_bp_module  # noqa: E402
import blueprints.strategy_pnl as module  # noqa: E402
from limiter import limiter  # noqa: E402

STALE = {
    "status": "stale",
    "warnings": ["1 open position(s) do not match the broker"],
    "mismatches": [],
    "recovered_fills": 0,
    "last_reconciled_at": "2026-09-29T12:00:00+05:30",
    "feed_connected": True,
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(module.strategy_pnl_bp)
    app.register_blueprint(auth_bp_module.auth_bp)

    scheduled = []
    monkeypatch.setattr(module, "get_auth_token", lambda _u: "tok123")
    monkeypatch.setattr(module, "get_api_key_for_tradingview", lambda _u: "key123")
    monkeypatch.setattr(module, "get_analyze_mode", lambda: False)
    monkeypatch.setattr(
        module, "schedule_reconcile", lambda mode, creds: scheduled.append((mode, creds))
    )
    monkeypatch.setattr(module, "get_data_health", lambda mode: {**STALE, "mode": mode})
    app.scheduled = scheduled

    import utils.session as session_utils

    monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)

    with app.test_client() as c:
        with c.session_transaction() as s:
            s["user"] = "rajandran"
            s["logged_in"] = True
            s["login_time"] = "2026-01-01T09:00:00+05:30"
            s["broker"] = "flattrade"
        yield c


def test_daily_performance_includes_data_health_and_schedules_reconcile(client, monkeypatch):
    monkeypatch.setattr(
        module, "get_daily_performance", lambda s, p: (True, {"status": "success"}, 200)
    )

    body = client.get("/api/strategy-pnl/daily?period=7d").get_json()

    assert body["data_health"]["status"] == "stale"
    assert body["data_health"]["mode"] == "live"
    assert client.application.scheduled == [
        ("live", {"auth_token": "tok123", "broker": "flattrade", "api_key": "key123"})
    ]


def test_compare_includes_data_health(client, monkeypatch):
    monkeypatch.setattr(
        module, "get_strategy_comparison", lambda p: (True, {"status": "success"}, 200)
    )

    body = client.get("/api/strategy-pnl/compare?period=7d").get_json()

    assert body["data_health"]["status"] == "stale"


def test_strategy_pnl_includes_data_health(client, monkeypatch):
    monkeypatch.setattr(
        module, "get_strategy_performance", lambda u, t, b: (True, {"status": "success"}, 200)
    )

    body = client.get("/api/strategy-pnl").get_json()

    assert body["data_health"]["status"] == "stale"


def test_error_responses_carry_no_data_health(client, monkeypatch):
    monkeypatch.setattr(
        module, "get_daily_performance", lambda s, p: (False, {"status": "error"}, 400)
    )

    body = client.get("/api/strategy-pnl/daily?period=bad").get_json()

    assert "data_health" not in body


def test_health_failure_never_breaks_the_page(client, monkeypatch):
    def boom(_mode):
        raise RuntimeError("health unavailable")

    monkeypatch.setattr(module, "get_data_health", boom)
    monkeypatch.setattr(
        module, "get_daily_performance", lambda s, p: (True, {"status": "success"}, 200)
    )

    response = client.get("/api/strategy-pnl/daily?period=7d")

    assert response.status_code == 200
    assert response.get_json()["status"] == "success"
