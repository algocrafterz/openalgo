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


def test_export_requires_a_session():
    with _app().test_client() as client:
        path = "/api/backtest-results/IDEA-001/runs/r1/export"
        assert client.get(path).status_code in (302, 401)


def test_export_serves_a_built_csv_as_an_attachment(monkeypatch):
    seen = {}

    def fake(idea_id, run_id, fmt):
        seen.update(idea=idea_id, run=run_id, fmt=fmt)
        return True, route_module_export("r1.metrics.csv", b"section,metric,value\n"), 200

    monkeypatch.setattr(route_module, "export_run", fake)
    client = _client_logged_in(_app(), monkeypatch)
    response = client.get("/api/backtest-results/IDEA-001/runs/r1/export?format=metrics")
    assert response.status_code == 200
    assert seen == {"idea": "IDEA-001", "run": "r1", "fmt": "metrics"}
    assert response.headers["Content-Disposition"] == 'attachment; filename="r1.metrics.csv"'
    assert response.data == b"section,metric,value\n"


def route_module_export(filename, body):
    from services.backtest_results_service import ExportFile

    return ExportFile(filename, "text/csv", body=body)


def test_export_serves_a_stored_file(monkeypatch, tmp_path):
    from services.backtest_results_service import ExportFile

    record = tmp_path / "r1.json"
    record.write_text('{"run_id": "r1"}', encoding="utf-8")
    monkeypatch.setattr(
        route_module,
        "export_run",
        lambda *_a: (True, ExportFile("r1.json", "application/json", path=record), 200),
    )
    client = _client_logged_in(_app(), monkeypatch)
    response = client.get("/api/backtest-results/IDEA-001/runs/r1/export")
    assert response.status_code == 200
    assert response.get_json() == {"run_id": "r1"}
    assert "attachment" in response.headers["Content-Disposition"]


def test_export_defaults_to_json_and_propagates_errors(monkeypatch):
    seen = {}

    def fake(idea_id, run_id, fmt):
        seen["fmt"] = fmt
        return False, {"status": "error", "message": "Run not found: r1"}, 404

    monkeypatch.setattr(route_module, "export_run", fake)
    client = _client_logged_in(_app(), monkeypatch)
    response = client.get("/api/backtest-results/IDEA-001/runs/r1/export")
    assert seen["fmt"] == "json"
    assert response.status_code == 404


def test_run_detail_requires_a_session():
    with _app().test_client() as client:
        path = "/api/backtest-results/IDEA-001/runs/2026-09-29-orb-v1"
        assert client.get(path).status_code in (302, 401)


@pytest.mark.parametrize("status", [200, 400, 404])
def test_run_detail_route_passes_ids_and_propagates_status(monkeypatch, status):
    seen = {}

    def fake(idea_id, run_id):
        seen.update(idea=idea_id, run=run_id)
        return status == 200, {"status": "success" if status == 200 else "error"}, status

    monkeypatch.setattr(route_module, "get_run_detail", fake)
    client = _client_logged_in(_app(), monkeypatch)
    response = client.get("/api/backtest-results/IDEA-001/runs/2026-09-29-orb-v1")
    assert response.status_code == status
    assert seen == {"idea": "IDEA-001", "run": "2026-09-29-orb-v1"}


def test_run_detail_does_not_shadow_export(monkeypatch):
    monkeypatch.setattr(
        route_module, "get_run_detail", lambda *_a: (True, {"status": "success"}, 200)
    )
    monkeypatch.setattr(route_module, "export_run", lambda *_a: (False, {"status": "error"}, 404))
    client = _client_logged_in(_app(), monkeypatch)
    assert client.get("/api/backtest-results/IDEA-001/runs/r1/export").status_code == 404


def test_run_detail_serialises_yaml_spec_with_mixed_keys(monkeypatch, tmp_path):
    import json

    from services import backtest_results_service as svc

    rid = "2026-09-29-orb-v1"
    runs = tmp_path / "research" / "ideas" / "idea-001-sample" / "runs"
    (runs / rid / "strategy").mkdir(parents=True)
    (runs / rid / "strategy" / "spec.yaml").write_text(
        "name: orb\nsince: 2026-01-02\nlevels:\n  1: a\n  x: b\n", encoding="utf-8"
    )
    (runs / rid / "manifest.json").write_text(json.dumps({"schema": 3}), encoding="utf-8")
    record = {"run_id": rid, "schema": 3, "artifact_dir": f"{rid}/", "manifest_sha256": "a" * 64}
    monkeypatch.setattr(
        svc,
        "_load",
        lambda _r: [{"id": "IDEA-001", "folder": "idea-001-sample", "runs": [record]}],
    )
    monkeypatch.setattr(svc, "DEFAULT_ROOT", tmp_path)
    client = _client_logged_in(_app(), monkeypatch)
    response = client.get(f"/api/backtest-results/IDEA-001/runs/{rid}")
    assert response.status_code == 200
    spec = response.get_json()["run"]["spec"]
    assert spec["since"] == "2026-01-02" and spec["levels"] == {"1": "a", "x": "b"}


def test_compare_route_returns_comparison_keys_end_to_end(monkeypatch, tmp_path):
    import json

    import services.backtest_results_service as svc
    from signal_engine.backtest import manifest
    from signal_engine.tests import test_backtest_compare as tc
    from signal_engine.tests import test_record_run as rrbase

    monkeypatch.setattr(
        manifest,
        "git_state",
        lambda r: {"code_commit": "a" * 40, "code_dirty": False, "dirty_paths": []},
    )
    monkeypatch.setattr(manifest, "dataset_fingerprint", lambda s, i, a: "d" * 64)
    rrbase._make_idea(tmp_path, "009", "orb", "backtest/strategies/orb.py")
    flats = [
        tc._record(tmp_path, tc._execution_of(tc.A_ROWS, "2026-03-01", "2026-03-10")),
        tc._record(tmp_path, tc._execution_of(tc.B_ROWS, "2026-03-04", "2026-03-20")),
    ]
    records = [json.loads(f.read_text(encoding="utf-8")) for f in flats]
    monkeypatch.setattr(svc, "DEFAULT_ROOT", tmp_path)
    monkeypatch.setattr(
        svc,
        "_load",
        lambda _r: [{"id": "IDEA-009", "folder": "idea-009-orb", "runs": records}],
    )
    client = _client_logged_in(_app(), monkeypatch)
    a, b = (r["run_id"] for r in records)
    response = client.get(f"/api/backtest-results/IDEA-009/compare?a={a}&b={b}")
    body = response.get_json()
    assert response.status_code == 200
    assert body["comparison_note"] is None
    assert body["comparison"]["schema"] == 1
    assert "metric_deltas" in body


def test_idea_route_returns_versions_and_pointers_end_to_end(monkeypatch, tmp_path):
    import services.backtest_results_service as svc

    folder = tmp_path / "research" / "ideas" / "idea-001-sample"
    folder.mkdir(parents=True)
    (folder / "versions.yaml").write_text(
        "- version: v1\n  strategy_fingerprint: " + "a" * 64 + "\n  parent: null\n"
        "  created_ist: '2026-10-01T10:00:00+05:30'\n  note: first\n",
        encoding="utf-8",
    )
    idea = {
        "id": "IDEA-001",
        "folder": "idea-001-sample",
        "runs": [],
        "current_version": "v1",
    }
    monkeypatch.setattr(svc, "DEFAULT_ROOT", tmp_path)
    monkeypatch.setattr(svc, "_load", lambda _r: [idea])
    client = _client_logged_in(_app(), monkeypatch)
    response = client.get("/api/backtest-results/IDEA-001")
    body = response.get_json()
    assert response.status_code == 200
    assert [v["version"] for v in body["versions"]] == ["v1"]
    assert body["version_pointers"] == {
        "current_version": "v1",
        "promoted_version": None,
        "promoted_run_id": None,
    }


def test_idea_route_reports_versions_error_without_a_path(monkeypatch, tmp_path):
    import services.backtest_results_service as svc

    folder = tmp_path / "research" / "ideas" / "idea-001-sample"
    folder.mkdir(parents=True)
    (folder / "versions.yaml").write_text("- version: nope\n", encoding="utf-8")
    idea = {"id": "IDEA-001", "folder": "idea-001-sample", "runs": []}
    monkeypatch.setattr(svc, "DEFAULT_ROOT", tmp_path)
    monkeypatch.setattr(svc, "_load", lambda _r: [idea])
    client = _client_logged_in(_app(), monkeypatch)
    body = client.get("/api/backtest-results/IDEA-001").get_json()
    assert body["versions"] is None
    assert body["versions_error"] == "versions.yaml is invalid"
    assert str(tmp_path) not in body["versions_error"]


# ---- trades and lineage routes ---------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/backtest-results/IDEA-001/runs/2026-09-29-orb-v1/trades",
        "/api/backtest-results/IDEA-001/runs/2026-09-29-orb-v1/trades/T-000001/lineage",
    ],
)
def test_trade_routes_require_a_session(path):
    with _app().test_client() as client:
        assert client.get(path).status_code in (302, 401)


def test_trades_route_parses_query_and_passes_it(monkeypatch):
    seen = {}

    def fake(idea_id, run_id, **kw):
        seen.update(idea=idea_id, run=run_id, **kw)
        return True, {"status": "success", "page": {}}, 200

    monkeypatch.setattr(route_module, "get_run_trades", fake)
    client = _client_logged_in(_app(), monkeypatch)
    url = (
        "/api/backtest-results/IDEA-001/runs/r1/trades"
        "?subset=oos&symbol=AAA&reason=SL&sort=net_r&desc=true&limit=50&offset=10"
    )
    assert client.get(url).status_code == 200
    assert seen == {
        "idea": "IDEA-001",
        "run": "r1",
        "subset": "oos",
        "symbol": "AAA",
        "reason": "SL",
        "sort": "net_r",
        "desc": True,
        "limit": 50,
        "offset": 10,
    }


def test_trades_route_defaults(monkeypatch):
    seen = {}

    def fake(idea_id, run_id, **kw):
        seen.update(kw)
        return True, {"status": "success"}, 200

    monkeypatch.setattr(route_module, "get_run_trades", fake)
    client = _client_logged_in(_app(), monkeypatch)
    client.get("/api/backtest-results/IDEA-001/runs/r1/trades")
    assert seen == {
        "subset": "all",
        "symbol": None,
        "reason": None,
        "sort": "entry_time",
        "desc": False,
        "limit": 100,
        "offset": 0,
    }


@pytest.mark.parametrize(
    "query",
    [
        "limit=abc",
        "offset=x",
        "limit=1.5",
        "desc=maybe",
        "offset=",
        "limit=99999999999999999999",
        "limit=%2B5",
        "limit=1234567890",
        "offset=-x",
    ],
)
def test_trades_route_bad_params_400(monkeypatch, query):
    called = []
    monkeypatch.setattr(
        route_module, "get_run_trades", lambda *a, **k: called.append(1) or (True, {}, 200)
    )
    client = _client_logged_in(_app(), monkeypatch)
    response = client.get(f"/api/backtest-results/IDEA-001/runs/r1/trades?{query}")
    assert response.status_code == 400 and not called
    assert response.get_json()["status"] == "error"


@pytest.mark.parametrize(
    "raw,expected", [("1", True), ("0", False), ("false", False), ("TRUE", True)]
)
def test_trades_route_boolean_forms(monkeypatch, raw, expected):
    seen = {}
    monkeypatch.setattr(
        route_module,
        "get_run_trades",
        lambda i, r, **kw: seen.update(kw) or (True, {"status": "success"}, 200),
    )
    client = _client_logged_in(_app(), monkeypatch)
    client.get(f"/api/backtest-results/IDEA-001/runs/r1/trades?desc={raw}")
    assert seen["desc"] is expected


def test_lineage_route_passes_ids_and_status(monkeypatch):
    seen = {}

    def fake(idea_id, run_id, trade_id):
        seen.update(idea=idea_id, run=run_id, trade=trade_id)
        return False, {"status": "error", "message": "x"}, 404

    monkeypatch.setattr(route_module, "get_trade_lineage", fake)
    client = _client_logged_in(_app(), monkeypatch)
    response = client.get("/api/backtest-results/IDEA-001/runs/r1/trades/T-000001/lineage")
    assert response.status_code == 404
    assert seen == {"idea": "IDEA-001", "run": "r1", "trade": "T-000001"}


def test_new_routes_do_not_shadow_detail_or_export(monkeypatch):
    monkeypatch.setattr(
        route_module, "get_run_detail", lambda *_a: (True, {"status": "detail"}, 200)
    )
    monkeypatch.setattr(route_module, "export_run", lambda *_a: (False, {"status": "export"}, 404))
    client = _client_logged_in(_app(), monkeypatch)
    assert client.get("/api/backtest-results/IDEA-001/runs/r1").get_json()["status"] == "detail"
    assert client.get("/api/backtest-results/IDEA-001/runs/r1/export").status_code == 404


def test_trades_and_lineage_end_to_end(monkeypatch, tmp_path):
    import json

    import pandas as pd

    from services import backtest_results_service as svc

    rid = "2026-09-29-orb-v1"
    runs = tmp_path / "research" / "ideas" / "idea-001-sample" / "runs"
    (runs / rid / "evidence").mkdir(parents=True)
    (runs / rid / "manifest.json").write_text(json.dumps({"schema": 3}), encoding="utf-8")
    ts = pd.to_datetime(["2026-01-05 09:30", "2026-02-05 09:30"]).tz_localize("Asia/Kolkata")
    pd.DataFrame(
        {
            "trade_id": ["T-000001", "T-000002"],
            "symbol": ["AAA", "BBB"],
            "entry_time": ts,
            "net_r": [1.0, -1.0],
            "exit_reason": ["TP", "SL"],
        }
    ).to_parquet(runs / rid / "evidence" / "trades.parquet", index=False)
    record = {"run_id": rid, "schema": 3, "artifact_dir": f"{rid}/", "manifest_sha256": "a" * 64}
    monkeypatch.setattr(
        svc,
        "_load",
        lambda _r: [{"id": "IDEA-001", "folder": "idea-001-sample", "runs": [record]}],
    )
    monkeypatch.setattr(svc, "DEFAULT_ROOT", tmp_path)
    client = _client_logged_in(_app(), monkeypatch)
    base = f"/api/backtest-results/IDEA-001/runs/{rid}/trades"
    page = client.get(base + "?subset=losers&sort=net_r&desc=1").get_json()["page"]
    assert page["total"] == 1 and page["rows"][0]["trade_id"] == "T-000002"
    assert page["rows"][0]["entry_time"] == "2026-02-05T09:30:00+05:30"
    assert client.get(base + "?sort=nope").status_code == 400
    lin = client.get(base + "/T-000001/lineage")
    assert lin.status_code == 200
    body = lin.get_json()["lineage"]
    assert body["trade_id"] == "T-000001" and body["signal"] is None and body["fills"] == []
    assert client.get(base + "/T-9/lineage").status_code == 400
