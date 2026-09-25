"""
Integration tests for the session-authed /api/strategy-pnl route.

Run with: uv run pytest test/test_strategy_pnl_route.py -v
"""

import os
import sys

from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import blueprints.auth as auth_bp_module  # noqa: E402
import blueprints.strategy_pnl as strategy_pnl_module  # noqa: E402
from limiter import limiter  # noqa: E402


def _app():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(strategy_pnl_module.strategy_pnl_bp)
    # redirect(url_for("auth.logout")) needs this route to exist for url_for to resolve.
    app.register_blueprint(auth_bp_module.auth_bp)
    return app


def _log_in(client):
    import utils.session as session_utils

    with client.session_transaction() as session:
        session["user"] = "rajandran"
        session["logged_in"] = True
        session["login_time"] = "2026-01-01T09:00:00+05:30"
        session["broker"] = "flattrade"
    return session_utils


def test_unauthenticated_request_is_rejected():
    app = _app()
    with app.test_client() as client:
        response = client.get("/api/strategy-pnl")

    assert response.status_code in (302, 401)


def test_authenticated_request_returns_strategy_shape(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(strategy_pnl_module, "get_auth_token", lambda _user: "tok123")
    monkeypatch.setattr(
        strategy_pnl_module,
        "get_strategy_performance",
        lambda *_a, **_k: (
            True,
            {
                "status": "success",
                "mode": "live",
                "strategies": [{"strategy": "ORB", "realized": 100.0}],
                "count": 1,
                "book_mixed_mode": True,
            },
            200,
        ),
    )

    app = _app()
    with app.test_client() as client:
        session_utils = _log_in(client)
        monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)

        response = client.get("/api/strategy-pnl")

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "success"
    assert body["strategies"][0]["strategy"] == "ORB"
    assert body["book_mixed_mode"] is True


def test_missing_broker_in_session_returns_400(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(strategy_pnl_module, "get_auth_token", lambda _user: "tok123")

    app = _app()
    with app.test_client() as client:
        import utils.session as session_utils

        with client.session_transaction() as session:
            session["user"] = "rajandran"
            session["logged_in"] = True
            session["login_time"] = "2026-01-01T09:00:00+05:30"
            # no broker set
        monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)

        response = client.get("/api/strategy-pnl")

    assert response.status_code == 400
    assert response.get_json()["status"] == "error"


def test_no_auth_token_redirects_to_logout(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(strategy_pnl_module, "get_auth_token", lambda _user: None)

    app = _app()
    with app.test_client() as client:
        session_utils = _log_in(client)
        monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)

        response = client.get("/api/strategy-pnl")

    assert response.status_code == 302


def test_daily_route_passes_query_params_and_returns_metrics(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    captured = {}

    def fake_get_daily_performance(strategy, period):
        captured["strategy"] = strategy
        captured["period"] = period
        return True, {"status": "success", "mode": "live", "trades_count": 0}, 200

    monkeypatch.setattr(strategy_pnl_module, "get_daily_performance", fake_get_daily_performance)

    app = _app()
    with app.test_client() as client:
        session_utils = _log_in(client)
        monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)

        response = client.get("/api/strategy-pnl/daily?strategy=ORB&period=90d")

    assert response.status_code == 200
    assert captured == {"strategy": "ORB", "period": "90d"}
    assert response.get_json()["status"] == "success"


def test_daily_route_defaults_period_to_30d_and_strategy_to_none(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    captured = {}

    def fake_get_daily_performance(strategy, period):
        captured["strategy"] = strategy
        captured["period"] = period
        return True, {"status": "success"}, 200

    monkeypatch.setattr(strategy_pnl_module, "get_daily_performance", fake_get_daily_performance)

    app = _app()
    with app.test_client() as client:
        session_utils = _log_in(client)
        monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)

        client.get("/api/strategy-pnl/daily")

    assert captured == {"strategy": None, "period": "30d"}


def test_compare_route_returns_strategy_list(monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(
        strategy_pnl_module,
        "get_strategy_comparison",
        lambda period: (
            True,
            {"status": "success", "mode": "live", "period": period, "strategies": []},
            200,
        ),
    )

    app = _app()
    with app.test_client() as client:
        session_utils = _log_in(client)
        monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)

        response = client.get("/api/strategy-pnl/compare?period=ytd")

    assert response.status_code == 200
    body = response.get_json()
    assert body["period"] == "ytd"
    assert body["strategies"] == []


def test_daily_route_unauthenticated_is_rejected():
    app = _app()
    with app.test_client() as client:
        response = client.get("/api/strategy-pnl/daily")

    assert response.status_code in (302, 401)
