"""Intraday publication gates and immutability (INTRADAY-5M-DATASET-PLAN.md section 12)."""

import json
import os
import stat

import pandas as pd
import pytest

from signal_engine.backtest.data_readiness import intraday_publish as pub
from signal_engine.backtest.data_readiness import snapshot as sn

TS = 1704167100  # 2024-01-02 09:15 IST


def _daily(root, git):
    events = sn._put_object(root, sn._parquet_bytes(pd.DataFrame({"symbol": ["AAA"],
                                                                  "ex_date": ["2024-01-01"]})))
    body = {"dataset_version": "d1", "status": "published", "events_sha256": events,
            "corporate_action_version": "cav"}
    root_hash = sn._root_hash(body)
    (root / "versions").mkdir(parents=True)
    (root / "versions" / "d1.json").write_text(json.dumps({**body, "root_hash": root_hash,
                                                           "dataset_fingerprint": root_hash}))
    git.mkdir()
    (git / "d1.json").write_text(json.dumps({"status": "published", "root_hash": root_hash}))


def _build(tmp_path, state="READY_WITH_WARNINGS", ok=True):
    b = tmp_path / "build-x"
    (b / "five" / "AAA").mkdir(parents=True)
    pd.DataFrame({"symbol": ["AAA"], "session": pd.to_datetime(["2024-01-02"]),
                  "timestamp": [TS], "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
                  "volume": [5.0], "n_minutes": [5]}).to_parquet(b / "five/AAA/2024.parquet")
    pd.DataFrame({c: [None] for c in pub._LEDGER_COLS}).assign(
        symbol="AAA", cls="legacy", status="converted", origin="legacy",
        method="high+low").to_parquet(b / "session_ledger.parquet")
    pd.DataFrame({"symbol": ["AAA"], "result": ["exact"]}).to_parquet(b / "provenance.parquet")
    pd.DataFrame({"request_id": ["r0"]}).to_parquet(b / "requests.parquet")
    (b / "recovery_ledger.jsonl").write_text(json.dumps(
        {"request_id": "r0", "outcome": "ok", "sessions": ["2024-01-02"]}) + "\n")
    (b / "plan.json").write_text(json.dumps({"requests_total": 1}))
    (b / "policy.json").write_text(json.dumps({"N": 30, "method": "P99"}))
    report = {"readiness_state": state, "criteria": {"1_snapshot_hashed": ok},
              "snapshot_root_hash": "s" * 64, "template_sha256": "t" * 64,
              "provenance": {"sampled_sessions": 1, "exact_share": 1.0, "wilson_lower_95": 0.2,
                             "coverage": 0.1, "passed": True, "statement": "x"},
              "sessions": {"requested": 1, "published": 1, "out_of_scope_muhurat": 0},
              "exclusions": {"overall_rate": 0.0}}
    (b / "report.json").write_text(json.dumps(report))
    return b


@pytest.fixture
def store(tmp_path):
    root, git = tmp_path / "datasets", tmp_path / "git"
    _daily(root, git)
    return root, git


def _publish(build, store, **kw):
    root, git = store
    return pub.publish(build, version_id="nse-intraday-5m-test-001", daily_version="d1",
                       cause="test", root=root, git_dir=git, **kw)


def test_not_ready_or_a_failed_criterion_is_refused(tmp_path, store):
    with pytest.raises(sn.SnapshotError):
        _publish(_build(tmp_path, state="NOT_READY"), store)
    with pytest.raises(sn.SnapshotError):
        _publish(_build(tmp_path / "b2", ok=False), store)


def test_dry_run_writes_nothing_and_is_deterministic(tmp_path, store):
    b = _build(tmp_path)
    a, c = _publish(b, store, dry_run=True), _publish(b, store, dry_run=True)
    assert a == c and a["objects"] == 1
    assert not (store[0] / "versions" / "nse-intraday-5m-test-001.json").exists()


def test_published_version_verifies_and_is_never_rewritten(tmp_path, store):
    b = _build(tmp_path)
    out = _publish(b, store)
    root, git = store
    res = sn.verify("nse-intraday-5m-test-001", root=root, git_dir=git)
    assert res.ok, res.errors
    m = json.loads((root / "versions" / "nse-intraday-5m-test-001.json").read_text())
    assert m["frequency"] == "5m" and m["root_hash"] == out["root_hash"]
    assert m["contract"]["provenance_statement"] == "x"
    with pytest.raises(sn.SnapshotError):
        _publish(b, store)
    p = sn._object_path(root, m["extra_objects"]["provenance"])
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    p.write_bytes(b"tampered")
    res = sn.verify("nse-intraday-5m-test-001", root=root, git_dir=git)
    assert "extra object provenance missing or hash mismatch" in res.errors
