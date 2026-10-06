"""record_idea and reproduce through the REAL prepare_data / _resolution_for (review M1).

Every other record_run test stubs the dataset resolution; these run the real composition (frozen
spec -> contract -> version resolution -> preflight -> Resolution) against a published fixture.
"""
from __future__ import annotations

import copy
import dataclasses
import functools
import json
from datetime import date

import pytest
import yaml

from signal_engine.backtest import record_run as rr
from signal_engine.backtest import reproduce
from signal_engine.backtest.data_readiness import preflight as pf
from signal_engine.backtest.data_readiness import snapshot
from signal_engine.tests.dq_fixtures import weekdays
from signal_engine.tests.intake_fixtures import GOOD_SPEC
from signal_engine.tests.published_fixtures import lenient_policy, publish_fixture
from signal_engine.tests.test_record_run import IDEA_MD, SUPPORTED, _execution

pytestmark = pytest.mark.real_preflight

DAYS = [date.fromisoformat(d) for d in weekdays("2024-01-01", "2025-06-30")]
SYMS = ("INFY", "SBIN", "TCS")   # in the platform F&O universe (a spec cannot declare one)
IDEA = "research/ideas/idea-009-orb"


def _daily_spec():
    spec = copy.deepcopy(GOOD_SPEC)
    spec.update(timeframe="1d", style="swing", product="CNC", holding_period="none",
                session_window="none", direction="long_only")
    spec["exit"] = ["Close below EMA20"]
    return spec


def _root(tmp_path, monkeypatch, spec):
    folder = tmp_path / "research" / "ideas" / "idea-009-orb"
    folder.mkdir(parents=True)
    (folder / "idea.md").write_text(IDEA_MD.format(id="IDEA-009", slug="orb",
                                                   module="backtest/strategies/orb.py"))
    (folder / "spec.yaml").write_text(yaml.safe_dump(spec))
    from signal_engine.backtest import manifest
    monkeypatch.setattr(manifest, "git_state",
                        lambda r: {"code_commit": "a" * 40, "code_dirty": False, "dirty_paths": []})
    return tmp_path


@pytest.fixture
def version(tmp_path):
    return publish_fixture(tmp_path / "pub", SYMS, DAYS)


def _real_prepare(monkeypatch, version):
    root, git_dir, _, cal = version
    monkeypatch.setattr(rr, "prepare_data", functools.partial(
        pf.prepare_data, root=root, git_dir=git_dir, calendar=cal, policy=lenient_policy()))


def _record(root, execute):
    return rr.record_idea(root, "IDEA-009", execute=execute, run_date="2026-10-05",
                          supported=SUPPORTED)


def test_intraday_spec_is_not_ready_through_the_real_preflight(tmp_path, monkeypatch, version):
    root = _root(tmp_path, monkeypatch, GOOD_SPEC)
    _real_prepare(monkeypatch, version)
    calls = []
    with pytest.raises(pf.DataNotReadyError):
        _record(root, lambda key, **k: calls.append(1) or _execution())
    assert calls == [] and not (root / IDEA / "runs").exists()
    (rec,) = (root / IDEA / "preflight").glob("*.json")
    assert "no_published_interval" in json.loads(rec.read_text())["reasons"]


def test_daily_spec_records_the_real_resolution(tmp_path, monkeypatch, version):
    root = _root(tmp_path, monkeypatch, _daily_spec())
    _real_prepare(monkeypatch, version)
    seen = {}

    def execute(key, dataset=None, **k):
        seen["eligible"] = dataset.eligible
        ex = _execution()
        return dataclasses.replace(ex, costs={**ex.costs, "flat_round_trip_bps": 24.0})   # swing

    flat = json.loads(_record(root, execute).read_text())
    m = json.loads((root / IDEA / "runs" / flat["artifact_dir"] / "manifest.json").read_text())
    assert seen["eligible"] == list(SYMS)
    assert m["dataset"]["version_id"] == version[2]
    assert set(SYMS) < set(m["preflight"]["contract"]["universe_symbols"])
    assert m["preflight"]["contract"]["loader_min_sessions"] == \
        m["preflight"]["contract"]["history_sessions"]

    res = reproduce._resolution_for(m, root=version[0], git_dir=version[1])
    assert res.eligible == list(SYMS) and res.version_id == version[2]
    assert res.contract.to_dict() == m["preflight"]["contract"]
    assert res.report.end == m["preflight"]["end"]

    bad = {**m, "dataset": {**m["dataset"], "root_hash": "0" * 64}}
    with pytest.raises(snapshot.SnapshotError, match="root hash"):
        reproduce._resolution_for(bad, root=version[0], git_dir=version[1])
