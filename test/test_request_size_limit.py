"""F3: the two bug write routes refuse an oversized body with 413 before parsing it.

The limit is per route (blueprints.bug_tracker.MAX_BODY_BYTES), not app-wide.
"""

import io
import json

import pytest
from flask import Flask

import blueprints.bug_tracker as bt
import blueprints.ops_findings as of
from limiter import limiter
from signal_engine.bugs import store

pytestmark = pytest.mark.filterwarnings("ignore")

ROUTES = ["/api/bugs/BUG-001/note", "/api/ops/report-finding"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    import blueprints.auth as auth_bp_module
    import utils.session as session_utils

    path = str(tmp_path / "bugs.json")
    monkeypatch.setattr(bt, "TRACKER_PATH", path)
    monkeypatch.setattr(of, "TRACKER_PATH", path)
    monkeypatch.setattr(store, "TRACKER_PATH", path)
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["WTF_CSRF_ENABLED"] = False
    assert "MAX_CONTENT_LENGTH" not in app.config or app.config["MAX_CONTENT_LENGTH"] is None
    app.register_blueprint(bt.bug_tracker_bp)
    app.register_blueprint(of.ops_findings_bp)
    app.register_blueprint(auth_bp_module.auth_bp)
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)
    c = app.test_client()
    with c.session_transaction() as session:
        session["user"] = "tester"
        session["logged_in"] = True
        session["login_time"] = "2026-01-01T09:00:00+05:30"
    return c


def _big():
    return json.dumps({"text": "x" * (bt.MAX_BODY_BYTES + 10)}).encode()


@pytest.mark.parametrize("route", ROUTES)
def test_an_oversized_body_with_a_length_is_413_json(client, route):
    r = client.post(route, data=_big(), content_type="application/json")
    assert r.status_code == 413
    assert r.get_json()["status"] == "error" and "too large" in r.get_json()["message"]


@pytest.mark.parametrize("route", ROUTES)
def test_an_oversized_chunked_body_is_413(client, route):
    r = client.post(
        route,
        input_stream=io.BytesIO(_big()),
        content_type="application/json",
        environ_overrides={"wsgi.input_terminated": True},
    )
    assert r.status_code == 413
    assert r.get_json()["status"] == "error"


@pytest.mark.parametrize("route", ROUTES)
def test_a_normal_body_still_reaches_the_route_with_and_without_a_length(client, route):
    body = json.dumps({"text": "ok"}).encode()
    sized = client.post(route, data=body, content_type="application/json")
    chunked = client.post(
        route,
        input_stream=io.BytesIO(body),
        content_type="application/json",
        environ_overrides={"wsgi.input_terminated": True},
    )
    assert sized.status_code not in (413, 400) or sized.get_json()["message"] != bt.BODY_TOO_LARGE
    assert chunked.status_code != 413
    assert chunked.status_code == sized.status_code  # same route behaviour either way


@pytest.mark.parametrize("route", ROUTES)
def test_garbage_chunked_body_is_400_not_500(client, route):
    r = client.post(
        route,
        input_stream=io.BytesIO(b"nope"),
        content_type="application/json",
        environ_overrides={"wsgi.input_terminated": True},
    )
    assert r.status_code == 400
