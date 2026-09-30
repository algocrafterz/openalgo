"""
Integration tests for the session-authed /api/backtest-results routes.

Run with: uv run pytest test/test_backtest_results_route.py -v
"""

import os
import sys

import pytest
from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import blueprints.auth as auth_bp_module  # noqa: E402
import blueprints.backtest_results as route_module  # noqa: E402
from limiter import limiter  # noqa: E402


def _app():
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(route_module.backtest_results_bp)
    app.register_blueprint(auth_bp_module.auth_bp)
    return app


def _client_logged_in(app, monkeypatch):
    import utils.session as session_utils

    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = "rajandran"
        session["logged_in"] = True
        session["login_time"] = "2026-01-01T09:00:00+05:30"
    return client


@pytest.mark.parametrize(
    "path",
    [
        "/api/backtest-results",
        "/api/backtest-results/IDEA-001",
        "/api/backtest-results/IDEA-001/compare?a=x&b=y",
    ],
)
def test_unauthenticated_requests_are_rejected(path):
    with _app().test_client() as client:
        assert client.get(path).status_code in (302, 401)


def test_list_route_returns_service_payload(monkeypatch):
    monkeypatch.setattr(
        route_module,
        "list_results",
        lambda: (True, {"status": "success", "ideas": [{"id": "IDEA-001"}]}, 200),
    )
    client = _client_logged_in(_app(), monkeypatch)
    response = client.get("/api/backtest-results")
    assert response.status_code == 200
    assert response.get_json()["ideas"][0]["id"] == "IDEA-001"


def test_detail_route_passes_idea_id_and_propagates_status(monkeypatch):
    seen = {}

    def fake(idea_id):
        seen["id"] = idea_id
        return False, {"status": "error", "message": "not found"}, 404

    monkeypatch.setattr(route_module, "get_idea_results", fake)
    client = _client_logged_in(_app(), monkeypatch)
    response = client.get("/api/backtest-results/IDEA-042")
    assert seen["id"] == "IDEA-042"
    assert response.status_code == 404


def test_compare_route_passes_both_run_ids(monkeypatch):
    seen = {}

    def fake(idea_id, a, b):
        seen.update(idea=idea_id, a=a, b=b)
        return True, {"status": "success"}, 200

    monkeypatch.setattr(route_module, "compare_runs", fake)
    client = _client_logged_in(_app(), monkeypatch)
    response = client.get("/api/backtest-results/IDEA-001/compare?a=r1&b=r2")
    assert response.status_code == 200
    assert seen == {"idea": "IDEA-001", "a": "r1", "b": "r2"}


def test_compare_without_run_ids_is_400(monkeypatch):
    client = _client_logged_in(_app(), monkeypatch)
    response = client.get("/api/backtest-results/IDEA-001/compare?a=r1")
    assert response.status_code == 400
    assert response.get_json()["status"] == "error"
