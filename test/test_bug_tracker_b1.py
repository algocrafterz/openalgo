"""Endpoint: severity action and the optional verification reference (temp tracker path only)."""

import pytest
from flask import Flask
from flask_wtf.csrf import CSRFProtect, generate_csrf

import blueprints.bug_tracker as mod
from limiter import limiter
from signal_engine.bugs import store


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    path = str(tmp_path / "bugs.json")
    monkeypatch.setattr(mod, "TRACKER_PATH", path)
    monkeypatch.setattr(store, "TRACKER_PATH", path)
    store.add(
        title="Auto finding",
        kind="BUG",
        severity="MEDIUM",
        area="ops",
        seen="2026-11-01",
        impact="Not assessed",
        path=path,
    )
    return path


def _client(monkeypatch):
    import utils.session as session_utils

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["WTF_CSRF_ENABLED"] = True
    CSRFProtect(app)
    app.register_blueprint(mod.bug_tracker_bp)

    @app.route("/t/csrf")
    def token():
        return generate_csrf()

    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = "tester"
        session["logged_in"] = True
        session["login_time"] = "2026-01-01T09:00:00+05:30"
    return client, {"X-CSRFToken": client.get("/t/csrf").get_data(as_text=True)}


def _post(client, headers, action, body, bug_id="BUG-001"):
    return client.post(f"/api/bugs/{bug_id}/{action}", json=body, headers=headers)


def _stored(path):
    return store.load(path)["bugs"][0]


def test_severity_change_works_for_any_status_and_logs_the_reason(tracker, monkeypatch):
    client, h = _client(monkeypatch)
    r = _post(client, h, "severity", {"severity": "HIGH", "reason": "can leave a stop off"})
    assert r.status_code == 200
    data = r.get_json()["data"]
    assert data["severity"] == "HIGH" and data["status"] == "open" and data["overdue"] is True
    assert _stored(tracker)["history"][-1]["note"] == (
        "severity MEDIUM -> HIGH: can leave a stop off"
    )
    store.update("BUG-001", status="in_progress", path=tracker)
    r = _post(client, h, "severity", {"severity": "LOW", "reason": "cosmetic after all"})
    assert r.status_code == 200 and _stored(tracker)["status"] == "in_progress"


@pytest.mark.parametrize(
    ("body", "code", "word"),
    [
        ({"severity": "URGENT", "reason": "x"}, 400, "must be one of"),
        ({"severity": "HIGH"}, 400, "reason is required"),
        ({"severity": "HIGH", "reason": "  "}, 400, "reason is required"),
        ({"reason": "x"}, 400, "risk level is required"),
        ({"severity": 3, "reason": "x"}, 400, "must be text"),
        ({"severity": "HIGH", "reason": "x" * 4001}, 400, "too long"),
        ({"severity": "MEDIUM", "reason": "x"}, 409, "already MEDIUM"),
    ],
)
def test_severity_errors_use_the_existing_shape_and_write_nothing(
    tracker, monkeypatch, body, code, word
):
    client, h = _client(monkeypatch)
    before = open(tracker, "rb").read()
    r = _post(client, h, "severity", body)
    assert r.status_code == code
    assert r.get_json()["status"] == "error" and word in r.get_json()["message"]
    assert open(tracker, "rb").read() == before


def test_severity_on_a_missing_bug_is_404(tracker, monkeypatch):
    client, h = _client(monkeypatch)
    r = _post(client, h, "severity", {"severity": "LOW", "reason": "x"}, bug_id="BUG-099")
    assert r.status_code == 404


def test_severity_body_over_the_limit_is_413(tracker, monkeypatch):
    client, h = _client(monkeypatch)
    r = _post(client, h, "severity", {"severity": "LOW", "reason": "x" * (70 * 1024)})
    assert r.status_code == 413


def _to_fixed(tracker):
    store.update("BUG-001", status="in_progress", path=tracker)
    store.update("BUG-001", status="fixed", fix="f", path=tracker)


def test_verify_with_a_reference_stores_it(tracker, monkeypatch):
    _to_fixed(tracker)
    client, h = _client(monkeypatch)
    body = {
        "evidence": "clean session",
        "verification_ref": {"kind": "session", "ref": "eod-11-05"},
    }
    r = _post(client, h, "verify", body)
    assert r.status_code == 200
    assert r.get_json()["data"]["verification_ref"] == {"kind": "session", "ref": "eod-11-05"}
    assert _stored(tracker)["verification_ref"] == {"kind": "session", "ref": "eod-11-05"}


def test_verify_without_or_with_null_reference_leaves_no_key(tracker, monkeypatch):
    _to_fixed(tracker)
    client, h = _client(monkeypatch)
    r = _post(client, h, "verify", {"evidence": "clean session", "verification_ref": None})
    assert r.status_code == 200 and "verification_ref" not in _stored(tracker)


@pytest.mark.parametrize(
    ("ref", "word"),
    [
        ({"kind": "bogus", "ref": "x"}, "kind must be one of"),
        ({"ref": "x"}, "kind must be one of"),
        ({"kind": "test"}, "reference is required"),
        ({"kind": "test", "ref": " "}, "reference is required"),
        ({"kind": "test", "ref": "x" * 301}, "too long"),
        ("test:x", "kind and a reference"),
    ],
)
def test_verify_rejects_a_bad_reference_with_400(tracker, monkeypatch, ref, word):
    _to_fixed(tracker)
    client, h = _client(monkeypatch)
    before = open(tracker, "rb").read()
    r = _post(client, h, "verify", {"evidence": "e", "verification_ref": ref})
    assert r.status_code == 400 and word in r.get_json()["message"]
    assert open(tracker, "rb").read() == before


def test_verify_still_requires_evidence_even_with_a_reference(tracker, monkeypatch):
    _to_fixed(tracker)
    client, h = _client(monkeypatch)
    r = _post(client, h, "verify", {"verification_ref": {"kind": "test", "ref": "x"}})
    assert r.status_code == 400 and _stored(tracker)["status"] == "fixed"
