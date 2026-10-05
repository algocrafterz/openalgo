"""Phase 4 run path: published dataset only, preflight before any trial, INVALID_DATA semantics."""
from __future__ import annotations

import dataclasses
import json
from datetime import date
from pathlib import Path

import duckdb
import pytest
import yaml

from signal_engine.backtest import data
from signal_engine.backtest import record_run as rr
from signal_engine.backtest.data_readiness import preflight as pf
from signal_engine.backtest.data_readiness import snapshot
from signal_engine.backtest.data_readiness import state as st
from signal_engine.backtest.data_readiness.contract import DataContract
from signal_engine.tests.dq_fixtures import weekdays
from signal_engine.tests.intake_fixtures import GOOD_SPEC
from signal_engine.tests.published_fixtures import calendar_for, fake_resolution, publish_fixture
from signal_engine.tests.test_record_run import IDEA_MD, SUPPORTED, _execution

DAYS = [date.fromisoformat(d) for d in weekdays("2023-01-02", "2025-06-30")]   # ~650 sessions
SYMS = ("AAA", "BBB", "CCC", "DDD")


# ---------------------------------------------------------------- execute_backtest

def _contract(symbols=SYMS):
    return DataContract("D", 252, {"parameters.lookback_sessions": 252}, 500, False, "spec",
                        "test", tuple(symbols), "u" * 64)


@pytest.fixture
def published(tmp_path):
    def rows(s, i, d):                       # trending, so a 52-week-high strategy trades
        return 100.0 + i * (0.2 + 0.05 * SYMS.index(s)) + (i % 7)
    return publish_fixture(tmp_path, SYMS, DAYS, rows=rows)


def _resolution(published, eligible=SYMS, calendar=None):
    root, git_dir, vid, cal = published
    m = snapshot.resolve_version(vid, root=root, git_dir=git_dir)
    report = pf.PreflightReport(state=st.READY, reasons=[], warnings=[], dataset={"version_id": vid},
                                contract=_contract().to_dict(),
                                universe={"eligible": list(eligible)})
    return pf.Resolution(manifest=m, report=report, contract=_contract(), calendar=calendar or cal,
                         root=root, git_dir=git_dir)


@pytest.fixture
def no_landing_store(monkeypatch):
    """Any attempt to read the live store fails the test."""
    real_connect = duckdb.connect
    landing = snapshot._staging.LANDING_STORE

    def guarded(database=":memory:", *a, **k):
        if str(database) not in (":memory:", "") and Path(str(database)).resolve() == landing:
            raise AssertionError("the run path opened the live landing store")
        return real_connect(database, *a, **k)

    def refuse(*a, **k):
        raise AssertionError("legacy store loader called")

    monkeypatch.setattr(duckdb, "connect", guarded)
    for name in ("from_historify", "from_historify_daily", "historify_symbols"):
        monkeypatch.setattr(data, name, refuse)


def test_execute_reads_only_the_published_version(published, no_landing_store):
    ex = rr.execute_backtest("high_proximity", settings={}, run_truncation=False,
                             dataset=_resolution(published))
    assert ex.data["source"].startswith("published:nse-daily-test-001")
    assert set(ex.symbols) <= set(SYMS) and ex.days


def test_excluded_symbols_are_never_loaded(published, no_landing_store):
    ex = rr.execute_backtest("high_proximity", settings={}, run_truncation=False,
                             dataset=_resolution(published, eligible=("AAA", "BBB", "CCC")))
    assert "DDD" not in ex.symbols


def test_a_run_without_a_published_dataset_is_refused():
    with pytest.raises(rr.PublishedDataRequired):
        rr.execute_backtest("high_proximity", settings={}, run_truncation=False)


def test_calendar_mismatch_at_load_is_rejected_before_the_engine(published):
    wrong = calendar_for([d for d in DAYS if d != DAYS[400]], version="cal-other")
    with pytest.raises(rr.LoadRejected, match="calendar"):
        rr.execute_backtest("high_proximity", settings={}, run_truncation=False,
                            dataset=_resolution(published, calendar=wrong))


# ---------------------------------------------------------------- record_idea lifecycle

@pytest.fixture
def root(tmp_path, monkeypatch):
    folder = tmp_path / "research" / "ideas" / "idea-009-orb"
    folder.mkdir(parents=True)
    (folder / "idea.md").write_text(IDEA_MD.format(id="IDEA-009", slug="orb",
                                                   module="backtest/strategies/orb.py"))
    (folder / "spec.yaml").write_text(yaml.safe_dump(GOOD_SPEC))
    from signal_engine.backtest import manifest
    monkeypatch.setattr(manifest, "git_state",
                        lambda r: {"code_commit": "a" * 40, "code_dirty": False, "dirty_paths": []})
    return tmp_path


IDEA = "research/ideas/idea-009-orb"


def _record(root, execute, **kw):
    return rr.record_idea(root, "IDEA-009", execute=execute, run_date="2026-10-05",
                          supported=SUPPORTED, **kw)


def _trials(root):
    p = root / IDEA / "trials.yaml"
    return p.read_bytes() if p.exists() else None


def test_not_ready_creates_no_run_and_no_trial(root, monkeypatch):
    calls = []

    def refuse(folder, frozen, **kw):
        report = pf.PreflightReport(state=st.NOT_READY, reasons=["dataset_integrity"], warnings=[],
                                    dataset={}, contract={})
        raise pf.DataNotReadyError(report, pf.write_record(report, folder))

    monkeypatch.setattr(rr, "prepare_data", refuse)
    before = _trials(root)
    with pytest.raises(pf.DataNotReadyError):
        _record(root, lambda key, **k: calls.append(key) or _execution())
    assert calls == [] and _trials(root) == before
    assert not (root / IDEA / "runs").exists()
    assert len(list((root / IDEA / "preflight").glob("*.json"))) == 1


def test_load_time_rejection_creates_no_run_and_no_trial(root):
    def execute(key, dataset=None, **k):
        raise rr.LoadRejected("calendar mismatch at load", {"calendar": "x"})

    before = _trials(root)
    with pytest.raises(pf.DataNotReadyError, match="load"):
        _record(root, execute)
    assert _trials(root) == before and not (root / IDEA / "runs").exists()
    (rec,) = (root / IDEA / "preflight").glob("*load*.json")
    assert json.loads(rec.read_text())["stage"] == "load"


def _invalid_execution():
    return dataclasses.replace(_execution(), data_quality={
        "status": "WARNING", "affects_validity": True, "items": ["impossible bars"]})


def _manifest(root, flat):
    return json.loads((root / IDEA / "runs" / flat["artifact_dir"] / "manifest.json").read_text())


def test_post_engine_invalid_data_is_sealed_but_never_a_trial(root):
    from signal_engine.research.intake import trials
    flat = json.loads(_record(root, lambda key, **k: _invalid_execution()).read_text())
    m = _manifest(root, flat)
    assert m["run_status"] == "INVALID_DATA" and m["trial_counted"] is False and m["invalid_reason"]
    assert flat["run_verdict"] is None and flat["run_status"] == "INVALID_DATA"
    report = json.loads((root / IDEA / "runs" / flat["artifact_dir"] / "report" / "report.json")
                        .read_text())
    assert report["verdict"] is None
    assert trials.count(root / IDEA).material == 0


def test_a_valid_run_after_an_invalid_one_counts_exactly_one_trial(root):
    from signal_engine.research.intake import trials
    _record(root, lambda key, **k: _invalid_execution())
    flat = json.loads(_record(root, lambda key, **k: _execution()).read_text())
    assert trials.count(root / IDEA).material == 1
    m = _manifest(root, flat)
    assert m["run_status"] == "VALID" and m["trial_counted"] is True


def test_schema_4_manifest_records_the_dataset_and_preflight(root):
    flat = json.loads(_record(root, lambda key, **k: _execution()).read_text())
    m = _manifest(root, flat)
    assert m["schema"] == 4 and flat["schema"] == 4
    d = m["dataset"]
    for k in ("version_id", "root_hash", "status", "fingerprint_algo", "run_fingerprint",
              "price_basis", "calendar_version", "corporate_action_version",
              "series_rule_version", "coverage", "latest_session"):
        assert k in d, k
    assert d["fingerprint"] == d["run_fingerprint"]
    assert m["preflight"]["state"] in (st.READY, st.READY_WITH_WARNINGS) and m["preflight"]["sha256"]
    assert set(m["symbol_counts"]) >= {"strategy_universe", "requested", "eligible",
                                       "defect_excluded", "eligibility_excluded"}
    assert flat["dataset_version"] == d["version_id"]


def test_regime_block_is_unavailable_on_published_runs(root):
    called = []
    flat = json.loads(_record(root, lambda key, **k: _execution(),
                              regimes=lambda: called.append(1)).read_text())
    assert called == []
    assert "regime analysis not available" in flat["notes"].lower()


def test_verdict_set_is_unchanged():
    from signal_engine.backtest import verdict
    names = {v for k, v in vars(verdict).items() if k.isupper() and isinstance(v, str)
             and v in {"EDGE", "MARGINAL", "NO_EDGE", "INCONCLUSIVE", "INVALID_DATA"}}
    assert names == {"EDGE", "MARGINAL", "NO_EDGE", "INCONCLUSIVE"}


def test_fake_resolution_helper_is_ready():
    assert fake_resolution().report.state == st.READY


def test_schema_3_runs_are_historical_only(tmp_path):
    """Owner decision 2: a schema 3 run is never re-executed (and so never touches the live store);
    it reports dataset_version not_recorded. The source run folder is copied, never modified."""
    import shutil

    from signal_engine.backtest import reproduce

    src = Path(__file__).resolve().parents[1] / "research" / "ideas" / \
        "idea-027-52-week-high-proximity" / "runs"
    run_id = "2026-10-04-52-week-high-proximity-v2"
    if not (src / run_id / "manifest.json").exists():
        pytest.skip("IDEA-027 v2 run folder not present")
    idea = tmp_path / "research" / "ideas" / "idea-027-52-week-high-proximity"
    shutil.copytree(src.parent, idea, ignore=shutil.ignore_patterns("*.reproductions"))
    calls = []
    report = reproduce.reproduce(idea / "runs" / run_id, root=tmp_path,
                                 execute=lambda *a, **k: calls.append(1),
                                 git_state_fn=lambda r: {"code_commit": "x", "code_dirty": False,
                                                         "dirty_paths": []},
                                 write_report=False)
    assert calls == [] and report.reproducible is False
    assert "dataset_version: not_recorded" in report.notes
    assert any("never re-executed" in r for r in report.reasons)


def test_the_source_never_serves_sessions_after_the_preflight_end(published):
    """Review H1: the run's source is capped at the end the preflight checked."""
    res = _resolution(published)
    res.report.end = DAYS[399].isoformat()
    with res.open() as source:
        frames = source.daily(list(SYMS), None, 10, None)
    assert max(f.index[-1].date() for f in frames.values()) == DAYS[399]


def test_a_version_that_fails_to_open_is_a_load_rejection(published):
    """Review L2: a verification failure after the preflight is recorded as a load rejection."""
    res = _resolution(published)
    res = dataclasses.replace(res, manifest={**res.manifest, "root_hash": "0" * 64})
    with pytest.raises(rr.LoadRejected, match="root hash"):
        rr.execute_backtest("high_proximity", settings={}, run_truncation=False, dataset=res)
