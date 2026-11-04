"""Report a Daily Workflow finding as a bug: server-side resolution, dedupe, races and the route.

Everything runs against a temporary reports folder and a temporary tracker file; the real
signal_engine/bugs/bugs.json is never opened for writing.
"""

import json
import threading

import pytest
from flask import Flask
from flask_wtf.csrf import CSRFProtect, generate_csrf

import blueprints.ops_findings as mod
from limiter import limiter
from signal_engine.bugs import store

DAY = "2026-11-03"
NEXT = "2026-11-04"


def _write_day(folder, day, *, check_status="FAIL", step_exit=1, verdict="RED"):
    (folder / f"open-day-{day}.json").write_text(
        json.dumps(
            {
                "phase": "open-day",
                "day": day,
                "verdict": verdict,
                "summary": "s",
                "checks": [
                    {
                        "name": "close-day 2026-11-02",
                        "status": check_status,
                        "evidence": "close-day 2026-11-02: failed steps: ['jobs']",
                        "plain": "Yesterday's end-of-day job had a failed step or did not run.",
                    },
                    {"name": "disk", "status": "OK", "evidence": "886 GB", "plain": "fine"},
                ],
            }
        )
    )
    (folder / f"open-day-{day}-status.tsv").write_text(
        f"step\tstarted\tended\texit\ncheck\t09:12:03\t09:12:07\t{step_exit}\nnotify\t09:12:07\t09:12:09\t0\n"
    )
    (folder / f"open-day-{day}.md").write_text("# open day\n")
    (folder / f"logs-{day}.json").write_text(
        json.dumps(
            {
                "phase": "logs",
                "day": day,
                "verdict": "AMBER",
                "summary": "s",
                "checks": [
                    {
                        "name": "NEW x4",
                        "status": "WARN",
                        "evidence": "first 13:20:07; services.history_service: Error fetching",
                        "plain": "A new kind of error appeared today.",
                    }
                ],
            }
        )
    )
    (folder / f"close-day-{day}-status.tsv").write_text(
        "step\tstarted\tended\texit\nlogs\t15:36:00\t15:36:09\t0\n"
    )
    (folder / f"execution-health-{day}.md").write_text(
        "# Execution health\n\n| Metric | Today | Status | Prev day |\n| --- | --- | --- | --- |\n"
        "| Limiter refused calls (total) | 3575 | REGRESSION | 5193 |\n"
        "| Limiter timeouts (total) | 0 | OK | 0 |\n"
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    folder = tmp_path / "reports"
    folder.mkdir()
    tracker = tmp_path / "bugs.json"
    tracker.write_text(json.dumps({"version": 1, "bugs": []}))
    _write_day(folder, DAY)
    _write_day(folder, NEXT)
    monkeypatch.setattr(mod, "REPORTS_DIR", str(folder))
    monkeypatch.setattr(mod, "TRACKER_PATH", str(tracker))
    # A store call that forgets its path must land here, never in the real tracker.
    monkeypatch.setattr(store, "TRACKER_PATH", str(tracker))
    return folder, tracker


def _payload(**over):
    base = {
        "day": DAY,
        "phase": "open-day",
        "group": "open-day",
        "name": "close-day 2026-11-02",
        "severity": "MEDIUM",
        "kind": "EXECUTION",
        "area": "ops",
        "impact": "Not assessed",
        "severity_confirmed": True,
    }
    return {**base, **over}


def _bugs(tracker):
    return json.loads(tracker.read_text())["bugs"]


def test_finds_checks_failed_steps_and_metrics_but_not_a_red_check_step(env):
    found = mod.enumerate_findings(DAY)
    keys = {(f["phase"], f["group"], f["name"]) for f in found}
    assert ("open-day", "open-day", "close-day 2026-11-02") in keys
    assert ("close-day", "logs", "NEW x4") in keys
    assert ("close-day", "execution", "Limiter refused calls (total)") in keys
    assert not any(f["group"] == "step" for f in found)  # check exit 1 is the RED verdict itself
    assert not any(f["name"] in ("disk", "Limiter timeouts (total)") for f in found)


def test_a_check_step_that_failed_without_a_red_result_is_a_finding(env):
    folder, _ = env
    _write_day(folder, DAY, verdict="AMBER")
    steps = [f for f in mod.enumerate_findings(DAY) if f["group"] == "step"]
    assert [(f["phase"], f["name"]) for f in steps] == [("open-day", "check")]


def test_creates_a_bug_that_keeps_the_finding_context(env):
    _, tracker = env
    result = mod.report_finding(_payload())
    assert result["created"] and result["recorded"]
    bug = _bugs(tracker)[0]
    assert bug["id"] == "BUG-001" and bug["status"] == "open"
    assert (bug["severity"], bug["kind"], bug["area"]) == ("MEDIUM", "EXECUTION", "ops")
    assert bug["first_seen"] == DAY and bug["occurrences"] == [DAY]
    assert "Open checkpoint on 2026-11-03 at 09:12:03" in bug["evidence"][0]
    assert "recorded status FAIL" in bug["evidence"][0]
    assert "failed steps" in bug["evidence"][0]
    assert any(s.startswith(mod.MARKER) for s in bug["sources"])
    assert "signal_engine/reports/open-day-2026-11-03.md" in bug["sources"]
    assert bug["history"][0]["note"] == "recorded"


def test_the_evidence_comes_from_disk_not_from_the_request(env):
    _, tracker = env
    mod.report_finding(_payload(evidence="INJECTED", plain="x" * 10, status="OK"))
    assert "INJECTED" not in json.dumps(_bugs(tracker))


def test_reporting_twice_the_same_day_writes_nothing_more(env):
    _, tracker = env
    first = mod.report_finding(_payload())
    before = tracker.read_text()
    second = mod.report_finding(_payload())
    assert not second["created"] and not second["recorded"]
    assert second["bug"]["id"] == first["bug"]["id"]
    assert tracker.read_text() == before


def test_a_new_day_is_recorded_on_the_same_bug_with_the_stores_seen_rule(env):
    _, tracker = env
    mod.report_finding(_payload())
    again = mod.report_finding(_payload(day=NEXT))
    assert not again["created"] and again["recorded"]
    bugs = _bugs(tracker)
    assert len(bugs) == 1
    assert bugs[0]["occurrences"] == [DAY, NEXT]
    assert len(bugs[0]["evidence"]) == 2


def test_a_fixed_bug_seen_after_its_fix_is_reopened_but_not_on_or_before_it(env):
    folder, tracker = env
    later = "2099-01-01"
    _write_day(folder, later)
    mod.report_finding(_payload())
    store.update("BUG-001", status="fixed", fix="changed", note="fixed", path=str(tracker))
    # A day on or before the fix date is evidence of the old code: the store keeps it fixed.
    same = mod.report_finding(_payload(day=NEXT))
    assert same["bug"]["status"] == "fixed"
    again = mod.report_finding(_payload(day=later))
    assert again["bug"]["status"] == "open"


def _cli_bug(tracker, title, **over):
    fields = {
        "title": title,
        "kind": "BUG",
        "severity": "LOW",
        "area": "ops",
        "seen": "2026-11-01",
        "impact": "from the CLI",
        "path": str(tracker),
    }
    return store.add(**{**fields, **over})


def test_an_unambiguous_same_title_bug_of_the_same_kind_and_component_is_linked(env):
    _, tracker = env
    found = mod.enumerate_findings(DAY)[0]
    _cli_bug(tracker, mod.finding_title(found))
    result = mod.report_finding(_payload(name=found["name"], kind="BUG", area="ops"))
    assert not result["created"] and result["recorded"]
    bugs = _bugs(tracker)
    assert len(bugs) == 1 and bugs[0]["occurrences"] == ["2026-11-01", DAY]
    assert any(s.startswith(mod.MARKER) for s in bugs[0]["sources"])


@pytest.mark.parametrize("over", [{"kind": "DATA"}, {"area": "broker"}])
def test_a_same_title_bug_of_another_kind_or_component_is_not_silently_linked(env, over):
    _, tracker = env
    found = mod.enumerate_findings(DAY)[0]
    _cli_bug(tracker, mod.finding_title(found))
    before = tracker.read_text()
    with pytest.raises(store.AmbiguousMatch) as info:
        mod.report_finding(_payload(name=found["name"], **{"kind": "BUG", "area": "ops", **over}))
    assert info.value.candidates == ["BUG-001"]
    assert tracker.read_text() == before


def test_several_same_title_candidates_are_never_guessed_between(env):
    _, tracker = env
    found = mod.enumerate_findings(DAY)[0]
    title = mod.finding_title(found)
    _cli_bug(tracker, title)
    data = json.loads(tracker.read_text())
    twin = {**data["bugs"][0], "id": "BUG-002"}
    data["bugs"].append(twin)
    tracker.write_text(json.dumps(data))
    before = tracker.read_text()
    with pytest.raises(store.AmbiguousMatch) as info:
        mod.report_finding(_payload(name=found["name"], kind="BUG", area="ops"))
    assert info.value.candidates == ["BUG-001", "BUG-002"]
    assert tracker.read_text() == before


def test_the_finding_key_wins_over_a_title_that_has_since_changed(env):
    _, tracker = env
    mod.report_finding(_payload())
    store.update("BUG-001", title="A better title written by hand", path=str(tracker))
    again = mod.report_finding(_payload(day=NEXT))
    assert again["recorded"] and again["bug"]["id"] == "BUG-001"
    assert len(_bugs(tracker)) == 1


def test_a_closed_same_title_bug_is_not_a_candidate(env):
    _, tracker = env
    found = mod.enumerate_findings(DAY)[0]
    old = _cli_bug(tracker, mod.finding_title(found))
    store.update(old["id"], status="wontfix", note="not ours", path=str(tracker))
    result = mod.report_finding(_payload(name=found["name"], kind="BUG", area="ops"))
    assert result["created"] and result["bug"]["id"] == "BUG-002"


def test_the_risk_level_must_be_confirmed_and_is_never_taken_from_the_finding(env):
    _, tracker = env
    for payload in (
        {k: v for k, v in _payload().items() if k != "severity_confirmed"},
        _payload(severity_confirmed=False),
        _payload(severity_confirmed="true"),
    ):
        with pytest.raises(mod.FindingError):
            mod.report_finding(payload)
    assert _bugs(tracker) == []
    # a critical finding still gets the level the trader chose
    assert mod.report_finding(_payload(severity="LOW"))["bug"]["severity"] == "LOW"


def test_a_busy_tracker_is_refused_politely_and_recovers(env, monkeypatch):
    import fcntl

    _, tracker = env
    monkeypatch.setattr(store, "LOCK_TIMEOUT", 0.3)
    holder = open(str(tracker) + ".lock", "a")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        with pytest.raises(store.TrackerBusy):
            mod.report_finding(_payload())
        assert _bugs(tracker) == []
    finally:
        holder.close()
    assert mod.report_finding(_payload())["created"]


def test_simultaneous_submissions_make_exactly_one_bug(env):
    _, tracker = env
    barrier = threading.Barrier(8)
    results, errors = [], []

    def go():
        barrier.wait()
        try:
            results.append(mod.report_finding(_payload()))
        except Exception as exc:  # noqa: BLE001 - the test reports whatever escaped
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert len(_bugs(tracker)) == 1
    assert {r["bug"]["id"] for r in results} == {"BUG-001"}
    assert sum(r["created"] for r in results) == 1


def test_the_key_ignores_dates_and_names_a_log_group_by_its_error(env):
    a = mod.enumerate_findings(DAY)
    b = mod.enumerate_findings(NEXT)
    by = lambda rows, g: next(f for f in rows if f["group"] == g)  # noqa: E731
    assert mod.finding_key(by(a, "open-day")) == mod.finding_key(by(b, "open-day"))
    assert "<date>" in mod.finding_key(by(a, "open-day"))
    logs = by(a, "logs")
    assert "x4" not in mod.finding_key(logs)
    assert "services.history_service" in mod.finding_key(logs)


@pytest.mark.parametrize(
    "over",
    [
        {"severity": "CRITICAL"},
        {"severity": None},
        {"kind": "OTHER"},
        {"area": "somewhere"},
        {"impact": ""},
        {"impact": "x" * 601},
        {"plain": "x" * 401},
        {"day": "../../etc/passwd"},
        {"day": "2026-13-45x"},
        {"phase": "nope"},
        {"group": "logs"},  # logs belong to close-day, not open-day
        {"name": "disk"},  # an OK check is not a finding
        {"name": "not recorded anywhere"},
        {"impact": ["list"]},
        {"severity_confirmed": False},
        {"severity_confirmed": "yes"},
    ],
)
def test_bad_input_is_refused_and_nothing_is_written(env, over):
    _, tracker = env
    before = tracker.read_text()
    with pytest.raises(mod.FindingError):
        mod.report_finding(_payload(**over))
    assert tracker.read_text() == before


def test_links_show_which_findings_already_have_a_bug(env):
    mod.report_finding(_payload())
    links = mod.links_for_day(NEXT)
    assert [(link["group"], link["bug_id"]) for link in links] == [("open-day", "BUG-001")]


def _app(monkeypatch, csrf=True):
    import blueprints.auth as auth_bp_module
    import utils.session as session_utils

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.config["WTF_CSRF_ENABLED"] = csrf
    CSRFProtect(app)
    app.register_blueprint(mod.ops_findings_bp)
    app.register_blueprint(auth_bp_module.auth_bp)

    @app.route("/t/csrf")
    def token():
        return generate_csrf()

    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)
    return app


def _login(client):
    with client.session_transaction() as session:
        session["user"] = "tester"
        session["logged_in"] = True
        session["login_time"] = "2026-01-01T09:00:00+05:30"


def test_the_routes_need_a_session(env, monkeypatch):
    with _app(monkeypatch).test_client() as client:
        assert client.post("/api/ops/report-finding", json=_payload()).status_code in (
            302,
            400,
            401,
        )
        assert client.get(f"/api/ops/finding-links?day={DAY}").status_code in (302, 401)
        assert len(_bugs(env[1])) == 0


def test_a_post_without_the_csrf_token_is_refused(env, monkeypatch):
    client = _app(monkeypatch).test_client()
    _login(client)
    assert client.post("/api/ops/report-finding", json=_payload()).status_code == 400
    assert _bugs(env[1]) == []


def test_post_with_a_token_creates_then_reports_the_existing_bug(env, monkeypatch):
    client = _app(monkeypatch).test_client()
    _login(client)
    token = client.get("/t/csrf").get_data(as_text=True)
    headers = {"X-CSRFToken": token}
    first = client.post("/api/ops/report-finding", json=_payload(), headers=headers)
    assert first.status_code == 201
    body = first.get_json()
    assert body["created"] is True and body["bug"]["id"] == "BUG-001"
    second = client.post("/api/ops/report-finding", json=_payload(), headers=headers)
    assert second.status_code == 200 and second.get_json()["created"] is False
    assert len(_bugs(env[1])) == 1
    links = client.get(f"/api/ops/finding-links?day={DAY}").get_json()["data"]
    assert links[0]["bug_id"] == "BUG-001"


def test_a_bad_request_gets_a_plain_message_and_no_internals(env, monkeypatch):
    client = _app(monkeypatch).test_client()
    _login(client)
    token = client.get("/t/csrf").get_data(as_text=True)
    resp = client.post(
        "/api/ops/report-finding", json=_payload(severity="HUGE"), headers={"X-CSRFToken": token}
    )
    body = resp.get_json()
    assert resp.status_code == 400 and body["status"] == "error"
    assert "Traceback" not in body["message"] and "TrackerError" not in body["message"]
    assert (
        client.post(
            "/api/ops/report-finding", data="nope", headers={"X-CSRFToken": token}
        ).status_code
        == 400
    )


def test_the_route_answers_409_for_an_ambiguous_title_and_503_when_busy(env, monkeypatch):
    import fcntl

    _, tracker = env
    found = mod.enumerate_findings(DAY)[0]
    _cli_bug(tracker, mod.finding_title(found))
    client = _app(monkeypatch).test_client()
    _login(client)
    headers = {"X-CSRFToken": client.get("/t/csrf").get_data(as_text=True)}
    body = _payload(name=found["name"], kind="DATA")
    resp = client.post("/api/ops/report-finding", json=body, headers=headers)
    data = resp.get_json()
    assert resp.status_code == 409 and data["candidates"] == ["BUG-001"]
    assert "Traceback" not in data["message"] and "AmbiguousMatch" not in data["message"]

    monkeypatch.setattr(store, "LOCK_TIMEOUT", 0.2)
    holder = open(str(tracker) + ".lock", "a")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        busy = client.post("/api/ops/report-finding", json=_payload(), headers=headers)
    finally:
        holder.close()
    assert busy.status_code == 503
    assert busy.get_json()["message"] == "The bug tracker is busy. Try again in a moment."
