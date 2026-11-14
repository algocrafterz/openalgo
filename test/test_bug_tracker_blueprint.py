"""The /logs/bugs read model and its seven store-backed write actions (temp tracker path only)."""

import fcntl
import json
import os

import pytest
from flask import Flask
from flask_wtf.csrf import CSRFProtect, generate_csrf

import blueprints.bug_tracker as mod
from blueprints.bug_tracker import TRACKER_PATH, read_tracker
from limiter import limiter
from signal_engine.bugs import store


def test_points_at_the_signal_engine_tracker():
    assert TRACKER_PATH.replace("\\", "/").endswith("signal_engine/bugs/bugs.json")


def test_a_missing_file_is_an_empty_list(tmp_path):
    assert read_tracker(str(tmp_path / "none.json")) == []


def test_returns_the_bugs(tmp_path):
    path = tmp_path / "bugs.json"
    path.write_text(json.dumps({"version": 1, "bugs": [{"id": "BUG-001"}]}))
    assert read_tracker(str(path)) == [{"id": "BUG-001"}]


def test_a_file_that_is_not_a_tracker_is_refused(tmp_path):
    path = tmp_path / "bugs.json"
    path.write_text(json.dumps([1, 2]))
    with pytest.raises(ValueError):
        read_tracker(str(path))


# ---- computed fields and write actions -------------------------------------------------------


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    path = str(tmp_path / "bugs.json")
    monkeypatch.setattr(mod, "TRACKER_PATH", path)
    monkeypatch.setattr(store, "TRACKER_PATH", path)
    return path


def _add(path, **over):
    args = {
        "title": "Stops not placed",
        "kind": "BUG",
        "severity": "HIGH",
        "area": "signal_engine",
        "seen": "2026-11-01",
        "impact": "Unprotected position",
        "path": path,
        **over,
    }
    return store.add(**args)


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
    headers = {"X-CSRFToken": client.get("/t/csrf").get_data(as_text=True)}
    return client, headers


def _post(client, headers, bug_id, action, body):
    return client.post(f"/api/bugs/{bug_id}/{action}", json=body, headers=headers)


def _stored(path, bug_id="BUG-001"):
    return next(b for b in store.load(path)["bugs"] if b["id"] == bug_id)


def test_enrich_adds_computed_fields_and_keeps_stored_ones():
    old = {"id": "BUG-1", "status": "open", "severity": "HIGH", "first_seen": "2026-11-01"}
    out = mod.enrich(old, "2026-11-10")
    assert out["age_days"] == 9 and out["overdue"] is True
    assert out["owner"] == "" and out["awaiting_verification"] is False
    assert out["next_action"] == "Assign and start"
    assert old == {"id": "BUG-1", "status": "open", "severity": "HIGH", "first_seen": "2026-11-01"}
    fixed = mod.enrich({**old, "status": "fixed", "owner": "ana"}, "2026-11-02")
    assert fixed["awaiting_verification"] is True and fixed["overdue"] is False
    assert fixed["next_action"] == "Verify with evidence" and fixed["owner"] == "ana"
    for status, text in (("in_progress", "Finish fix"), ("verified", "None"), ("wontfix", "None")):
        assert mod.enrich({**old, "status": status}, "2026-11-02")["next_action"] == text


def test_list_is_backward_compatible_and_enriched(tracker, monkeypatch):
    _add(tracker)
    client, _ = _client(monkeypatch)
    data = client.get("/api/bugs").get_json()["data"]
    assert data[0]["id"] == "BUG-001" and data[0]["title"] == "Stops not placed"
    assert {"owner", "age_days", "overdue", "awaiting_verification", "next_action"} <= set(data[0])
    assert "age_days" not in _stored(tracker)


def test_a_post_needs_the_csrf_token_and_a_session(tracker, monkeypatch):
    _add(tracker)
    client, headers = _client(monkeypatch)
    assert client.post("/api/bugs/BUG-001/note", json={"text": "x"}).status_code == 400
    anon = Flask(__name__)
    anon.secret_key = "k"
    anon.register_blueprint(mod.bug_tracker_bp)
    monkeypatch.setattr(limiter, "enabled", False)
    assert anon.test_client().post("/api/bugs/BUG-001/note", json={"text": "x"}).status_code in (
        302,
        401,
    )
    assert len(_stored(tracker)["history"]) == 1


def test_the_full_lifecycle_through_the_seven_actions(tracker, monkeypatch):
    _add(tracker)
    client, h = _client(monkeypatch)
    r = _post(client, h, "BUG-001", "assign", {"owner": "Priya"})
    assert r.status_code == 200 and r.get_json()["data"]["owner"] == "Priya"
    assert _post(client, h, "BUG-001", "note", {"text": "seen again"}).status_code == 200
    r = _post(client, h, "BUG-001", "start", {})
    assert r.get_json()["data"]["status"] == "in_progress"
    assert r.get_json()["data"]["next_action"] == "Finish fix"
    r = _post(
        client,
        h,
        "BUG-001",
        "fix",
        {"fix": "Place the stop first", "commit": "abc1234", "fix_explanation": "order of calls"},
    )
    data = r.get_json()["data"]
    assert r.status_code == 200 and data["status"] == "fixed" and data["awaiting_verification"]
    stored = _stored(tracker)
    assert stored["fix_commit"] == "abc1234" and stored["fix_explanation"] == "order of calls"
    r = _post(client, h, "BUG-001", "verify", {"evidence": "2026-11-05 session: stops placed"})
    assert r.status_code == 200 and r.get_json()["data"]["status"] == "verified"
    assert _stored(tracker)["verification"].startswith("2026-11-05")
    r = _post(client, h, "BUG-001", "reopen", {"reason": "happened again"})
    assert r.get_json()["data"]["status"] == "open"
    r = _post(client, h, "BUG-001", "wontfix", {"reason": "obsolete"})
    assert r.get_json()["data"]["status"] == "wontfix"
    notes = [e["note"] for e in _stored(tracker)["history"]]
    assert any("happened again" in n for n in notes) and any("obsolete" in n for n in notes)


@pytest.mark.parametrize(
    "action, body",
    [
        ("assign", {}),
        ("assign", {"owner": "  "}),
        ("note", {"text": ""}),
        ("fix", {"fix": ""}),
        ("verify", {"evidence": ""}),
        ("verify", {}),
        ("reopen", {"reason": ""}),
        ("wontfix", {"reason": " "}),
        ("note", {"text": 5}),
        ("fix", {"fix": "x", "commit": "has spaces here"}),
    ],
)
def test_missing_text_is_refused_with_400_and_nothing_is_written(
    tracker, monkeypatch, action, body
):
    _add(tracker, status="in_progress")
    if action in ("verify", "reopen"):
        store.update("BUG-001", status="fixed", fix="f", path=tracker)
    before = open(tracker, "rb").read()
    client, h = _client(monkeypatch)
    r = _post(client, h, "BUG-001", action, body)
    assert r.status_code == 400 and r.get_json()["status"] == "error"
    assert open(tracker, "rb").read() == before


def test_verified_cannot_be_reached_except_through_verify_with_evidence(tracker, monkeypatch):
    _add(tracker)
    client, h = _client(monkeypatch)
    for action in ("note", "assign", "start", "reopen", "wontfix"):
        r = _post(
            client,
            h,
            "BUG-001",
            action,
            {"status": "verified", "verification": "x", "text": "t", "owner": "o", "reason": "r"},
        )
        assert _stored(tracker)["status"] != "verified", action
        assert r.status_code in (200, 409)
    # open -> verify is refused by the transition table
    assert _post(client, h, "BUG-001", "verify", {"evidence": "e"}).status_code == 409
    assert _stored(tracker)["status"] != "verified"
    # a status route does not exist
    assert _post(client, h, "BUG-001", "verified", {"evidence": "e"}).status_code == 404
    assert _post(client, h, "BUG-001", "status", {"status": "verified"}).status_code == 404


def test_client_computed_and_unlisted_fields_are_ignored(tracker, monkeypatch):
    _add(tracker)
    client, h = _client(monkeypatch)
    _post(client, h, "BUG-001", "assign", {"owner": "Priya", "age_days": 99, "severity": "LOW"})
    stored = _stored(tracker)
    assert stored["severity"] == "HIGH" and "age_days" not in stored and "overdue" not in stored


def test_invalid_transitions_give_409_and_unknown_things_404(tracker, monkeypatch):
    _add(tracker)
    client, h = _client(monkeypatch)
    assert _post(client, h, "BUG-001", "fix", {"fix": "f"}).status_code == 409
    assert _post(client, h, "BUG-001", "reopen", {"reason": "r"}).status_code == 409
    assert _post(client, h, "BUG-009", "note", {"text": "t"}).status_code == 404
    assert _post(client, h, "nope", "note", {"text": "t"}).status_code == 404
    assert _post(client, h, "BUG-001", "delete", {}).status_code == 404
    r = _post(client, h, "BUG-001", "fix", {"fix": "f"})
    assert "Traceback" not in r.get_json()["message"] and "TrackerError" not in r.get_data(
        as_text=True
    )


def test_a_non_json_body_is_400(tracker, monkeypatch):
    _add(tracker)
    client, h = _client(monkeypatch)
    r = client.post("/api/bugs/BUG-001/note", data="nope", headers=h)
    assert r.status_code == 400


def test_a_busy_tracker_gives_423_and_the_next_request_succeeds(tracker, monkeypatch):
    _add(tracker)
    monkeypatch.setattr(store, "LOCK_TIMEOUT", 0.2)
    client, h = _client(monkeypatch)
    handle = open(tracker + ".lock", "a")
    fcntl.flock(handle, fcntl.LOCK_EX)
    try:
        r = _post(client, h, "BUG-001", "note", {"text": "t"})
        assert r.status_code == 423 and "busy" in r.get_json()["message"]
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
    assert _post(client, h, "BUG-001", "note", {"text": "t"}).status_code == 200


def test_the_write_limit_is_env_driven():
    assert mod.BUG_TRACKER_WRITE_LIMIT == os.getenv("BUG_TRACKER_WRITE_LIMIT", "30 per minute")


def test_assign_with_unchanged_owner_adds_no_history_line(tracker, monkeypatch):
    _add(tracker)
    client, h = _client(monkeypatch)
    assert _post(client, h, "BUG-001", "assign", {"owner": "Priya"}).status_code == 200
    count = len(_stored(tracker)["history"])
    r = _post(client, h, "BUG-001", "assign", {"owner": "Priya"})
    assert r.status_code == 200 and r.get_json()["data"]["owner"] == "Priya"
    assert len(_stored(tracker)["history"]) == count


def test_a_record_with_no_status_does_not_500(tracker, monkeypatch):
    _add(tracker)
    data = store.load(tracker)
    del data["bugs"][0]["status"]
    store._save(data, tracker)
    assert mod.enrich(data["bugs"][0])["next_action"] == "Assign and start"
    client, h = _client(monkeypatch)
    assert client.get("/api/bugs").status_code == 200
    for action, body in (("assign", {"owner": "X"}), ("note", {"text": "t"}), ("start", {})):
        assert _post(client, h, "BUG-001", action, body).status_code == 409
