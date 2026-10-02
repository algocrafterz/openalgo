"""Tests for the atomic, write-once run folder."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from signal_engine.backtest.artifacts import RunFolder


def _builder(artifacts: dict[str, str]) -> dict:
    return {"run_id": "r1", "artifacts": artifacts}


def _filled(runs: Path, run_id: str = "r1") -> RunFolder:
    folder = RunFolder(runs, run_id)
    folder.add("assumptions.json", {"b": 1, "a": [1, 2]})
    folder.add("strategy/spec.yaml", "name: x\n")
    folder.add("evidence/trades.parquet", b"\x00\x01binary")
    return folder


def test_layout_and_hashes(tmp_path: Path) -> None:
    out = _filled(tmp_path / "runs").commit(_builder)
    assert out == tmp_path / "runs" / "r1"
    manifest = json.loads((out / "manifest.json").read_text())
    assert set(manifest["artifacts"]) == {
        "assumptions.json",
        "strategy/spec.yaml",
        "evidence/trades.parquet",
    }
    for rel, digest in manifest["artifacts"].items():
        assert hashlib.sha256((out / rel).read_bytes()).hexdigest() == digest
    assert (out / "strategy" / "spec.yaml").read_text() == "name: x\n"


def test_json_is_pretty_sorted_with_newline(tmp_path: Path) -> None:
    out = _filled(tmp_path).commit(_builder)
    text = (out / "assumptions.json").read_text()
    assert text.endswith("\n")
    assert text.index('"a"') < text.index('"b"')
    assert "\n  " in text


def test_existing_id_raises_and_leaves_folder_untouched(tmp_path: Path) -> None:
    existing = tmp_path / "r1"
    existing.mkdir()
    (existing / "keep.txt").write_text("keep")
    with pytest.raises(FileExistsError):
        _filled(tmp_path).commit(_builder)
    assert [p.name for p in existing.iterdir()] == ["keep.txt"]
    assert (existing / "keep.txt").read_text() == "keep"


def test_builder_failure_leaves_nothing(tmp_path: Path) -> None:
    def boom(_: dict[str, str]) -> dict:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        _filled(tmp_path).commit(boom)
    assert list(tmp_path.iterdir()) == []


def test_files_read_only(tmp_path: Path) -> None:
    out = _filled(tmp_path).commit(_builder)
    for path in out.rglob("*"):
        if path.is_file():
            assert stat.S_IMODE(path.stat().st_mode) == 0o444
        else:
            assert os.access(path, os.W_OK)


@pytest.mark.parametrize("bad", ["/abs.json", "../x.json", "a/../../x", "", "a//", "manifest.json"])
def test_bad_relpaths_rejected(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        RunFolder(tmp_path, "r1").add(bad, "x")


def test_duplicate_relpath_rejected(tmp_path: Path) -> None:
    folder = RunFolder(tmp_path, "r1")
    folder.add("a.json", {})
    with pytest.raises(ValueError):
        folder.add("a.json", {})


def test_manifest_written_last(tmp_path: Path) -> None:
    seen: list[str] = []

    def builder(artifacts: dict[str, str]) -> dict:
        seen.extend(sorted(p.name for p in tmp_path.iterdir()))
        return {"artifacts": artifacts}

    out = _filled(tmp_path).commit(builder)
    assert all(not name.endswith("manifest.json") for name in seen)
    manifest_mtime = (out / "manifest.json").stat().st_mtime_ns
    for rel in json.loads((out / "manifest.json").read_text())["artifacts"]:
        assert (out / rel).stat().st_mtime_ns <= manifest_mtime


def test_builder_must_echo_artifacts(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _filled(tmp_path).commit(lambda a: {"artifacts": {}})
    assert list(tmp_path.iterdir()) == []


def test_missing_parent_created(tmp_path: Path) -> None:
    out = _filled(tmp_path / "deep" / "runs").commit(_builder)
    assert (out / "manifest.json").is_file()


def test_race_target_appears(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def builder(artifacts: dict[str, str]) -> dict:
        (tmp_path / "r1").mkdir()
        return {"artifacts": artifacts}

    with pytest.raises(FileExistsError):
        _filled(tmp_path).commit(builder)
    assert [p.name for p in tmp_path.iterdir()] == ["r1"]
