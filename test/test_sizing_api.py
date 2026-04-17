"""Integration tests for POST /api/v1/sizing endpoint — TDD RED phase.

Uses Flask test client with mocked auth/DB/logging to stay hermetic.

Run with:
    PYTHONPATH=/home/anand/github/openalgo uv run pytest test/test_sizing_api.py -v --tb=short
"""
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# App fixture — creates minimal Flask app with only the sizing namespace
# ---------------------------------------------------------------------------

def _stub_sandbox_modules():
    """Pre-stub sandbox package and submodules to prevent import errors in test env.

    The restx_api package __init__.py eagerly imports all namespaces, some of
    which depend on sandbox.* modules that require a live database setup.
    Stubbing them as MagicMock allows the test app to be constructed without
    those real dependencies.
    """
    import sys

    sandbox_submodules = [
        "sandbox",
        "sandbox.fund_manager",
        "sandbox.holdings_manager",
        "sandbox.order_manager",
        "sandbox.position_manager",
        "sandbox.catch_up_processor",
        "sandbox.execution_engine",
        "sandbox.execution_thread",
        "sandbox.squareoff_manager",
        "sandbox.squareoff_thread",
        "sandbox.websocket_execution_engine",
    ]

    # Only stub if not already a real module
    real_sandbox = sys.modules.get("sandbox")
    if real_sandbox is not None and not isinstance(real_sandbox, MagicMock):
        # The real sandbox package is loaded; nothing to do
        return

    sandbox_mock = MagicMock()
    sandbox_mock.__path__ = ["/mock/sandbox"]
    sandbox_mock.__package__ = "sandbox"
    sandbox_mock.__name__ = "sandbox"
    sandbox_mock.__spec__ = None

    for mod_name in sandbox_submodules:
        if mod_name not in sys.modules:
            sub_mock = MagicMock()
            sub_mock.__name__ = mod_name
            sub_mock.__package__ = "sandbox"
            sys.modules[mod_name] = sub_mock

    # Replace the sandbox package module so attribute access works
    sys.modules["sandbox"] = sandbox_mock


def _make_app():
    """Build a minimal Flask + RESTX app that only mounts the sizing namespace."""
    import importlib

    from flask import Flask
    from flask_restx import Api

    # Stub sandbox deps before importing restx_api package (which eager-loads all namespaces)
    _stub_sandbox_modules()

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test-secret-key-for-flask-tests"
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["PROPAGATE_EXCEPTIONS"] = False
    app.config["ERROR_404_HELP"] = False

    api = Api(app, doc=False)

    sizing_mod = importlib.import_module("restx_api.sizing")
    sizing_ns = sizing_mod.api

    api.add_namespace(sizing_ns, path="/api/v1/sizing")

    return app


def _stub_auth_db():
    """Stub database.auth_db to avoid PBKDF2/cryptography issues under coverage.

    database.auth_db creates a PBKDF2HMAC instance at module import time
    which fails when pytest-cov's import instrumentation interferes with
    the OpenSSL backend. We stub the whole module with a MagicMock so
    the endpoint's get_auth_token_broker can be patched normally.
    """
    import sys

    if "database.auth_db" not in sys.modules:
        auth_mock = MagicMock()
        auth_mock.get_auth_token_broker = MagicMock(return_value=(None, None))
        auth_mock.get_username_by_apikey = MagicMock(return_value=None)
        auth_mock.verify_api_key = MagicMock(return_value=None)
        sys.modules["database.auth_db"] = auth_mock

    if "database.apilog_db" not in sys.modules:
        log_mock = MagicMock()
        log_mock.async_log_order = MagicMock()
        log_mock.executor = MagicMock()
        sys.modules["database.apilog_db"] = log_mock


@pytest.fixture(scope="session")
def app():
    """Flask test app with only the sizing namespace registered."""
    mock_pepper = "a" * 64  # 64-char hex-like pepper (meets 32-byte requirement)
    env_patches = {
        "DATABASE_URL": "sqlite:////:memory:",
        "LOGS_DATABASE_URL": "sqlite:////:memory:",
        "APP_KEY": "test-secret-key-for-flask",
        "API_KEY_PEPPER": mock_pepper,
    }
    with patch.dict(os.environ, env_patches):
        _stub_auth_db()
        yield _make_app()


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_PAYLOAD = {
    "apikey": "testkey123",
    "symbol": "SBIN",
    "exchange": "NSE",
    "side": "BUY",
    "product": "MIS",
    "entry_price": 820.0,
    "stop_loss": 815.0,
    "target": 830.0,
    "capital": 35_000.0,
    "sizing_mode": "fixed_fractional",
    "risk_per_trade": 0.01,
    "pct_of_capital": None,
    "slippage_factor": 0.0,
    "max_sl_pct_for_sizing": 0.0,
    "min_entry_price": 0.0,
    "max_entry_price": 0.0,
}

URL = "/api/v1/sizing/"


def post_sizing(client, payload):
    return client.post(URL, data=json.dumps(payload), content_type="application/json")


# ---------------------------------------------------------------------------
# Validation tests (400 errors — no auth needed)
# ---------------------------------------------------------------------------

class TestValidationErrors:
    def test_missing_apikey_returns_400(self, client):
        payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "apikey"}
        resp = post_sizing(client, payload)
        assert resp.status_code == 400

    def test_missing_entry_price_returns_400(self, client):
        payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "entry_price"}
        resp = post_sizing(client, payload)
        assert resp.status_code == 400

    def test_missing_stop_loss_returns_400(self, client):
        payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "stop_loss"}
        resp = post_sizing(client, payload)
        assert resp.status_code == 400

    def test_missing_sizing_mode_returns_400(self, client):
        payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "sizing_mode"}
        resp = post_sizing(client, payload)
        assert resp.status_code == 400

    def test_invalid_sizing_mode_returns_400(self, client):
        payload = {**VALID_PAYLOAD, "sizing_mode": "bad_mode"}
        resp = post_sizing(client, payload)
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["status"] == "error"

    def test_negative_entry_price_returns_400(self, client):
        payload = {**VALID_PAYLOAD, "entry_price": -10.0}
        resp = post_sizing(client, payload)
        assert resp.status_code == 400

    def test_negative_capital_returns_400(self, client):
        payload = {**VALID_PAYLOAD, "capital": -100.0}
        resp = post_sizing(client, payload)
        assert resp.status_code == 400

    def test_invalid_risk_per_trade_out_of_range_returns_400(self, client):
        payload = {**VALID_PAYLOAD, "risk_per_trade": 1.5}
        resp = post_sizing(client, payload)
        assert resp.status_code == 400

    def test_empty_body_returns_400(self, client):
        resp = client.post(URL, data="{}", content_type="application/json")
        assert resp.status_code == 400

    def test_non_json_body_returns_400(self, client):
        resp = client.post(URL, data="not-json", content_type="application/json")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Auth tests (403 errors)
# ---------------------------------------------------------------------------

class TestAuthErrors:
    def test_invalid_apikey_returns_403(self, client):
        with patch("restx_api.sizing.get_auth_token_broker", return_value=(None, None)):
            resp = post_sizing(client, VALID_PAYLOAD)
        assert resp.status_code == 403
        data = resp.get_json()
        assert data["status"] == "error"
        assert "apikey" in data["message"].lower() or "invalid" in data["message"].lower()


# ---------------------------------------------------------------------------
# Success path tests
# ---------------------------------------------------------------------------

class TestSuccessPath:
    def _valid_auth_patch(self):
        return patch("restx_api.sizing.get_auth_token_broker", return_value=("tok123", "zerodha"))

    def test_fixed_fractional_returns_200(self, client):
        with self._valid_auth_patch():
            resp = post_sizing(client, VALID_PAYLOAD)
        assert resp.status_code == 200

    def test_response_envelope_structure(self, client):
        with self._valid_auth_patch():
            resp = post_sizing(client, VALID_PAYLOAD)
        data = resp.get_json()
        assert data["status"] == "success"
        assert "data" in data
        d = data["data"]
        for key in ("quantity", "raw_quantity", "risk_amount", "risk_pct_of_capital",
                    "reward_amount", "position_value", "rr_ratio", "sl_distance_pct",
                    "skip_reason", "warnings"):
            assert key in d, f"Missing key: {key}"

    def test_quantity_calculation_correct(self, client):
        """35 000 * 0.01 / 5.0 = 70."""
        with self._valid_auth_patch():
            resp = post_sizing(client, VALID_PAYLOAD)
        data = resp.get_json()
        assert data["data"]["quantity"] == 70

    def test_rr_ratio_correct(self, client):
        """(830-820)/(820-815) = 2.0."""
        with self._valid_auth_patch():
            resp = post_sizing(client, VALID_PAYLOAD)
        data = resp.get_json()
        assert abs(data["data"]["rr_ratio"] - 2.0) < 1e-4

    def test_skip_reason_none_when_qty_positive(self, client):
        with self._valid_auth_patch():
            resp = post_sizing(client, VALID_PAYLOAD)
        data = resp.get_json()
        assert data["data"]["skip_reason"] is None

    def test_warnings_is_list(self, client):
        with self._valid_auth_patch():
            resp = post_sizing(client, VALID_PAYLOAD)
        data = resp.get_json()
        assert isinstance(data["data"]["warnings"], list)

    def test_pct_of_capital_mode(self, client):
        payload = {
            **VALID_PAYLOAD,
            "sizing_mode": "pct_of_capital",
            "pct_of_capital": 0.10,
            "capital": 50_000.0,
            "entry_price": 500.0,
            "stop_loss": 490.0,
            "target": 520.0,
        }
        with self._valid_auth_patch():
            resp = post_sizing(client, payload)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
        assert data["data"]["quantity"] == 10  # floor(50000*0.10/500)

    def test_zero_qty_returns_success_with_skip_reason(self, client):
        """When qty==0, endpoint returns 200 with skip_reason populated."""
        payload = {
            **VALID_PAYLOAD,
            "capital": 100.0,
            "entry_price": 10_000.0,
            "stop_loss": 9_000.0,
        }
        with self._valid_auth_patch():
            resp = post_sizing(client, payload)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"
        assert data["data"]["quantity"] == 0
        assert data["data"]["skip_reason"] is not None

    def test_target_optional_accepted(self, client):
        payload = {k: v for k, v in VALID_PAYLOAD.items() if k != "target"}
        with self._valid_auth_patch():
            resp = post_sizing(client, payload)
        assert resp.status_code == 200

    def test_capital_none_uses_live_funds(self, client):
        """When capital is absent, endpoint fetches live funds via funds_service."""
        payload = {**VALID_PAYLOAD, "capital": None}

        mock_funds_response = {
            "status": "success",
            "data": {"availablecash": "50000.0"},
        }
        with self._valid_auth_patch(), \
             patch("restx_api.sizing.get_funds", return_value=(True, mock_funds_response, 200)):
            resp = post_sizing(client, payload)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "success"

    def test_capital_none_funds_fetch_fails_returns_error(self, client):
        """When live funds fetch fails and no capital provided, return 4xx/5xx."""
        payload = {**VALID_PAYLOAD, "capital": None}

        with self._valid_auth_patch(), \
             patch("restx_api.sizing.get_funds",
                   return_value=(False, {"status": "error", "message": "Broker down"}, 500)):
            resp = post_sizing(client, payload)
        assert resp.status_code in (400, 500)
        data = resp.get_json()
        assert data["status"] == "error"

    def test_slippage_applied_correctly(self, client):
        """With slippage_factor=0.10, risk/share = 5*1.1 = 5.5; qty=floor(350/5.5)=63."""
        payload = {**VALID_PAYLOAD, "slippage_factor": 0.10}
        with self._valid_auth_patch():
            resp = post_sizing(client, payload)
        data = resp.get_json()
        assert data["data"]["quantity"] == 63


# ---------------------------------------------------------------------------
# Logging: verify async_log_order is called on success
# ---------------------------------------------------------------------------

class TestLogging:
    def test_async_log_called_on_success(self, client):
        with patch("restx_api.sizing.get_auth_token_broker", return_value=("tok123", "zerodha")), \
             patch("restx_api.sizing.log_executor") as mock_executor:
            post_sizing(client, VALID_PAYLOAD)
            assert mock_executor.submit.called
