"""Registry checks for schema 3 flat run records and their artifact folders."""

import copy
import json
import os
from pathlib import Path

import pytest

from signal_engine.backtest import manifest as mf
from signal_engine.backtest.artifacts import RunFolder
from signal_engine.research import build_registry as br
from signal_engine.tests.test_research_registry import RUN, V2, VALID

HEX_A = "a" * 64
RUN_ID = "2026-09-29-orb-v1"
FILES = {
    "assumptions.json": {"sealed_ist": "2026-09-29T10:00:00+05:30", "entries": []},
    "strategy/spec.yaml": "name: orb\n",
    "strategy/strategy.json": {"strategy_id": "orb"},
    "execution/config.json": {"resolved": {}, "profile": {}},
    "evidence/trades.parquet": b"PAR1",
    "report/report.json": {"schema": 3},
}


def _manifest(run_id: str, artifacts: dict[str, str]) -> dict:
    fps = dict.fromkeys(mf.FINGERPRINT_KEYS, HEX_A)
    return {
        "schema": 3,
        "run_id": run_id,
        "created_ist": "2026-09-29T10:00:00+05:30",
        "idea_id": "IDEA-001",
        "strategy": {"version": "v1", "parent_version": None, "branch_label": "main"},
        "fingerprints": fps,
        "period": {"start": "2021-01-01", "end": "2026-09-25"},
        "dataset": {
            "id": "d",
            "source": "s",
            "interval": "5m",
            "data_as_of": "2026-09-28",
            "symbol_count": 1,
            "fingerprint": HEX_A,
            "corporate_action_policy": "none",
        },
        "universe": {"definition": "u", "symbols_sha256": HEX_A, "version": "1"},
        "calendar_version": "1",
        "deployment_profile": {"name": "p", "product": "MIS", "sha256": HEX_A},
        "session": {
            "exchange": "NSE",
            "strategy_window": "09:15-15:00",
            "broker_cutoff": "15:15",
            "force_flat": "15:15",
        },
        "engine": {
            "engine_version": "1",
            "code_commit": "abc",
            "code_dirty": False,
            "dirty_paths": [],
        },
        "environment": {"python": "3.12", "packages": {}},
        "random": {"seed": 1, "monte_carlo": {"n": 1}},
        "parent_run_id": "p0",
        "change_classes": [],
        "missing_capabilities": [],
        "reproducible": True,
        "reproducibility": dict.fromkeys(mf.REPRODUCIBILITY_KEYS, True),
        "artifacts": artifacts,
    }


def _make_folder(runs: Path, run_id: str = RUN_ID, skip: str | None = None) -> Path:
    rf = RunFolder(runs, run_id)
    for rel, content in FILES.items():
        if rel != skip:
            rf.add(rel, content)
    return rf.commit(lambda arts: _manifest(run_id, arts))


def _record(run_id: str, folder: Path) -> dict:
    return {
        **RUN,
        **V2,
        "run_id": run_id,
        "schema": 3,
        "artifact_dir": f"{run_id}/",
        "manifest_sha256": mf.manifest_sha256(folder),
        "strategy_version": "v1",
        "strategy_fingerprint": HEX_A,
        "change_classes": [],
        "reproducible": True,
        "code_dirty": False,
        "promotion_eligible": True,
        "missing_capabilities": [],
    }


@pytest.fixture
def idea(tmp_path: Path) -> Path:
    (tmp_path / "strategy_library" / "intraday" / "sample").mkdir(parents=True)
    d = tmp_path / "research" / "ideas" / "idea-001-sample"
    d.mkdir(parents=True)
    (d / "idea.md").write_text(VALID, encoding="utf-8")
    return d


def _setup(idea: Path, run_id: str = RUN_ID, **patch) -> tuple[Path, dict]:
    runs = idea / "runs"
    folder = _make_folder(runs, run_id)
    rec = {**_record(run_id, folder), **patch}
    (runs / f"{run_id}.json").write_text(json.dumps(rec), encoding="utf-8")
    return folder, rec


def _problems(idea: Path) -> list[str]:
    runs, problems = br._load_runs(idea)
    return problems


def _unlock(path: Path) -> None:
    os.chmod(path.parent, 0o755)
    os.chmod(path, 0o644)


def test_valid_v3_loads(idea: Path):
    _, rec = _setup(idea)
    runs, problems = br._load_runs(idea)
    assert problems == []
    assert runs == [rec]
    assert br.load_ideas(idea.parents[2])[0]["runs"][0]["manifest_sha256"] == rec["manifest_sha256"]


def test_schema2_without_schema_key_loads(idea: Path):
    (idea / "runs").mkdir()
    (idea / "runs" / "a.json").write_text(json.dumps({**RUN, **V2}), encoding="utf-8")
    runs, problems = br._load_runs(idea)
    assert problems == [] and len(runs) == 1


def test_v3_and_v2_in_same_idea_both_load(idea: Path):
    _setup(idea)
    v2 = {**RUN, **V2, "run_id": "2026-10-01-orb-v2", "date": "2026-10-01"}
    (idea / "runs" / "2026-10-01-orb-v2.json").write_text(json.dumps(v2), encoding="utf-8")
    runs, problems = br._load_runs(idea)
    assert problems == [] and [r["run_id"] for r in runs] == [RUN_ID, "2026-10-01-orb-v2"]


@pytest.mark.parametrize("value", [1, 4, "3", None, True])
def test_unsupported_schema(idea: Path, value):
    _setup(idea, schema=value)
    assert any("unsupported schema" in p for p in _problems(idea))


@pytest.mark.parametrize(
    "field,bad",
    [
        ("artifact_dir", 5),
        ("artifact_dir", "no-slash"),
        ("manifest_sha256", "abc"),
        ("manifest_sha256", 7),
        ("strategy_version", 1),
        ("strategy_fingerprint", "xyz"),
        ("change_classes", "ENGINE_CHANGE"),
        ("change_classes", [1]),
        ("reproducible", "yes"),
        ("code_dirty", 0),
        ("promotion_eligible", None),
        ("missing_capabilities", "x"),
        ("missing_capabilities", [2]),
    ],
)
def test_wrong_typed_v3_field(idea: Path, field: str, bad):
    _setup(idea, **{field: bad})
    problems = _problems(idea)
    assert problems and any(field in p for p in problems)
    assert all(p.startswith("idea-001-sample/runs/") for p in problems)


@pytest.mark.parametrize(
    "field",
    [
        "artifact_dir",
        "manifest_sha256",
        "strategy_version",
        "strategy_fingerprint",
        "change_classes",
        "reproducible",
        "code_dirty",
        "promotion_eligible",
        "missing_capabilities",
    ],
)
def test_missing_v3_field(idea: Path, field: str):
    _, rec = _setup(idea)
    rec.pop(field)
    (idea / "runs" / f"{RUN_ID}.json").write_text(json.dumps(rec), encoding="utf-8")
    assert any(field in p for p in _problems(idea))


def test_missing_artifact_dir_directory(idea: Path):
    _setup(idea, artifact_dir="nope/")
    assert any("artifact_dir" in p and "does not exist" in p for p in _problems(idea))


@pytest.mark.parametrize("bad", ["../x/", "/etc/", "../../idea-001-sample/runs/" + RUN_ID + "/"])
def test_artifact_dir_traversal(idea: Path, bad: str):
    _setup(idea, artifact_dir=bad)
    assert any("artifact_dir" in p for p in _problems(idea))


@pytest.mark.parametrize("rel", list(FILES) + ["manifest.json"])
def test_missing_required_file(idea: Path, rel: str):
    folder, _ = _setup(idea)
    _unlock(folder / rel)
    (folder / rel).unlink()
    assert any(rel in p for p in _problems(idea))


def test_tampered_artifact_detected(idea: Path):
    folder, _ = _setup(idea)
    target = folder / "report" / "report.json"
    _unlock(target)
    target.write_text('{"schema": 3, "x": 1}', encoding="utf-8")
    assert any("hash mismatch" in p for p in _problems(idea))


def test_extra_file_detected(idea: Path):
    folder, _ = _setup(idea)
    os.chmod(folder, 0o755)
    (folder / "stray.txt").write_text("x", encoding="utf-8")
    assert any("unlisted file" in p for p in _problems(idea))


def test_manifest_hash_mismatch(idea: Path):
    _setup(idea, manifest_sha256="b" * 64)
    assert any("manifest_sha256" in p for p in _problems(idea))


def _rewrite_manifest(folder: Path, rec: dict, idea: Path, mutate) -> None:
    path = folder / "manifest.json"
    _unlock(path)
    m = json.loads(path.read_text(encoding="utf-8"))
    mutate(m)
    path.write_text(json.dumps(m), encoding="utf-8")
    rec["manifest_sha256"] = mf.manifest_sha256(folder)
    (idea / "runs" / f"{rec['run_id']}.json").write_text(json.dumps(rec), encoding="utf-8")


def test_manifest_run_id_mismatch(idea: Path):
    folder, rec = _setup(idea)
    _rewrite_manifest(folder, rec, idea, lambda m: m.update(run_id="other"))
    assert any("run_id" in p for p in _problems(idea))


def test_invalid_manifest_reported(idea: Path):
    folder, rec = _setup(idea)
    _rewrite_manifest(folder, rec, idea, lambda m: m.pop("random"))
    assert any("manifest" in p and "random" in p for p in _problems(idea))


def test_fingerprint_mismatch(idea: Path):
    folder, rec = _setup(idea)
    _rewrite_manifest(folder, rec, idea, lambda m: m["fingerprints"].update(strategy="c" * 64))
    assert any("strategy_fingerprint" in p for p in _problems(idea))


def test_version_mismatch(idea: Path):
    folder, rec = _setup(idea)
    _rewrite_manifest(folder, rec, idea, lambda m: m["strategy"].update(version="v9"))
    assert any("strategy_version" in p for p in _problems(idea))


def test_orphan_run_folder(idea: Path):
    _setup(idea)
    _make_folder(idea / "runs", "2026-10-02-orb-v3")
    (idea / "runs" / ".hidden").mkdir()
    problems = _problems(idea)
    assert len(problems) == 1 and "orphan" in problems[0] and "2026-10-02-orb-v3" in problems[0]


def test_reproduction_reports_folder_is_not_an_orphan(idea: Path):
    _, rec = _setup(idea)
    repro = idea / "runs" / f"{rec['run_id']}.reproductions"
    repro.mkdir()
    (repro / "20261002T140000-ist.json").write_text("{}", encoding="utf-8")
    assert _problems(idea) == []


def test_reproductions_folder_of_an_unknown_run_is_still_not_a_run_folder(idea: Path):
    _setup(idea)
    (idea / "runs" / "2026-10-02-ghost-v9.reproductions").mkdir()
    assert _problems(idea) == []


def test_load_ideas_raises_on_v3_problem(idea: Path):
    _setup(idea, manifest_sha256="b" * 64)
    with pytest.raises(br.RegistryError, match="manifest_sha256"):
        br.load_ideas(idea.parents[2])


def test_validate_run_signature_still_works():
    assert br._validate_run(copy.deepcopy({**RUN, **V2})) == []


# ---- the flat record must agree with its manifest ----------------------------------------


@pytest.mark.parametrize(
    "field,bad",
    [
        ("reproducible", False),
        ("code_dirty", True),
        ("change_classes", ["PARAMETER_CHANGE"]),
        ("missing_capabilities", ["something"]),
        ("promotion_eligible", False),
    ],
)
def test_flat_record_disagreeing_with_manifest(idea: Path, field: str, bad):
    _setup(idea, **{field: bad})
    assert any(field in p for p in _problems(idea))


def test_hand_edited_promotion_eligible_fails(idea: Path):
    runs = idea / "runs"
    rf = RunFolder(runs, RUN_ID)
    for rel, content in FILES.items():
        rf.add(rel, content)

    def build(arts):
        m = _manifest(RUN_ID, arts)
        m["reproducible"] = False
        m["engine"]["code_dirty"] = True
        return m

    folder = rf.commit(build)
    rec = {**_record(RUN_ID, folder), "reproducible": False, "code_dirty": True}
    rec["promotion_eligible"] = True  # hand edited: should be False
    (runs / f"{RUN_ID}.json").write_text(json.dumps(rec), encoding="utf-8")
    problems = _problems(idea)
    assert any("promotion_eligible" in p for p in problems)
    assert not any("reproducible differs" in p or "code_dirty differs" in p for p in problems)
    rec["promotion_eligible"] = False
    (runs / f"{RUN_ID}.json").write_text(json.dumps(rec), encoding="utf-8")
    assert _problems(idea) == []
