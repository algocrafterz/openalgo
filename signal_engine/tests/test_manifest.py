"""Tests for manifest validation, artifact verification and provenance helpers."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import duckdb
import pytest

from signal_engine.backtest import manifest as mf
from signal_engine.backtest.artifacts import RunFolder

HEX = "a" * 64
FP_KEYS = ["rules", "params", "strategy", "execution", "cost", "universe", "profile", "dataset"]
REPRO_KEYS = [
    "strategy_version",
    "strategy_fingerprint",
    "dataset_fingerprint",
    "execution_config",
    "deployment_profile",
    "engine_version",
    "code_commit",
    "resolved_assumptions",
]


def good_manifest() -> dict[str, Any]:
    return {
        "schema": 3,
        "run_id": "2026-10-02-x-v1",
        "created_ist": "2026-10-02T10:00:00+05:30",
        "idea_id": "idea-001",
        "strategy": {"version": "1", "parent_version": None, "branch_label": "main"},
        "fingerprints": dict.fromkeys(FP_KEYS, HEX),
        "period": {"start": "2025-01-01", "end": "2025-12-31"},
        "dataset": {
            "id": "historify",
            "source": "duckdb",
            "interval": "5m",
            "data_as_of": "2026-10-01",
            "symbol_count": 3,
            "fingerprint": HEX,
            "corporate_action_policy": "adjusted",
        },
        "universe": {"definition": "fno", "symbols_sha256": HEX, "version": "u1"},
        "calendar_version": "c1",
        "deployment_profile": {"name": "p", "product": "MIS", "sha256": HEX},
        "session": {
            "exchange": "NSE",
            "strategy_window": "09:30-11:00",
            "broker_cutoff": "15:00",
            "force_flat": "14:50",
        },
        "engine": {
            "engine_version": "1",
            "code_commit": "f" * 40,
            "code_dirty": False,
            "dirty_paths": [],
        },
        "environment": {"python": "3.12.3", "packages": {"pandas": "2.3.3"}},
        "random": {"seed": 1, "monte_carlo": {"n": 10}},
        "parent_run_id": "2026-10-01-x-v1",
        "change_classes": [],
        "missing_capabilities": [],
        "reproducible": True,
        "reproducibility": dict.fromkeys(REPRO_KEYS, True),
        "artifacts": {"a.json": HEX},
    }


# ---- validate_manifest -------------------------------------------------------------------


def test_good_manifest_is_clean() -> None:
    assert mf.validate_manifest(good_manifest()) == []


def test_manifest_keys_cover_doc_table() -> None:
    assert "artifacts" in mf.MANIFEST_KEYS and "schema" in mf.MANIFEST_KEYS
    assert len(mf.MANIFEST_KEYS) == 21
    assert set(good_manifest()) == set(mf.MANIFEST_KEYS)


def test_missing_key_reported() -> None:
    m = good_manifest()
    del m["idea_id"]
    assert any("idea_id" in p for p in mf.validate_manifest(m))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schema", 2),
        ("schema", "3"),
        ("change_classes", "none"),
        ("missing_capabilities", None),
        ("reproducible", "yes"),
        ("artifacts", []),
        ("run_id", 5),
    ],
)
def test_wrong_types_reported(key: str, value: Any) -> None:
    m = good_manifest()
    m[key] = value
    assert any(key in p for p in mf.validate_manifest(m))


def test_fingerprint_problems() -> None:
    m = good_manifest()
    m["fingerprints"]["rules"] = "xyz"
    del m["fingerprints"]["cost"]
    problems = " ".join(mf.validate_manifest(m))
    assert "rules" in problems and "cost" in problems


def test_reproducibility_must_be_bools() -> None:
    m = good_manifest()
    m["reproducibility"]["engine_version"] = "yes"
    del m["reproducibility"]["code_commit"]
    problems = " ".join(mf.validate_manifest(m))
    assert "engine_version" in problems and "code_commit" in problems


def test_null_without_missing_capability_flagged() -> None:
    m = good_manifest()
    m["calendar_version"] = None
    assert any("calendar_version" in p for p in mf.validate_manifest(m))
    m["missing_capabilities"] = ["calendar_version: trading calendar arrives in step 5"]
    assert mf.validate_manifest(m) == []


def test_nested_null_matches_last_segment() -> None:
    m = good_manifest()
    m["session"]["broker_cutoff"] = None
    assert any("session.broker_cutoff" in p for p in mf.validate_manifest(m))
    m["missing_capabilities"] = ["broker_cutoff"]
    assert mf.validate_manifest(m) == []


def test_null_at_non_nullable_path_flagged() -> None:
    m = good_manifest()
    m["dataset"]["fingerprint"] = None
    m["missing_capabilities"] = ["fingerprint"]
    assert mf.validate_manifest(m)


def test_nullable_set() -> None:
    assert set(mf.NULLABLE) == {
        "calendar_version",
        "session.exchange",
        "session.broker_cutoff",
        "session.force_flat",
        "strategy.branch_label",
        "dataset.corporate_action_policy",
        "universe.version",
        "parent_run_id",
        "random.monte_carlo",
    }


def test_non_dict_manifest() -> None:
    assert mf.validate_manifest([]) != []  # type: ignore[arg-type]


# ---- manifest_sha256 / verify_artifacts --------------------------------------------------


def _commit(tmp_path: Path) -> Path:
    folder = RunFolder(tmp_path, "r1")
    folder.add("a.json", {"x": 1})
    folder.add("sub/b.txt", "hello")
    return folder.commit(lambda arts: {"artifacts": arts})


def test_manifest_sha256(tmp_path: Path) -> None:
    out = _commit(tmp_path)
    assert (
        mf.manifest_sha256(out) == hashlib.sha256((out / "manifest.json").read_bytes()).hexdigest()
    )


def test_verify_clean(tmp_path: Path) -> None:
    assert mf.verify_artifacts(_commit(tmp_path)) == []


def test_verify_detects_tamper_missing_extra(tmp_path: Path) -> None:
    out = _commit(tmp_path)
    (out / "a.json").chmod(0o644)
    (out / "a.json").write_text("{}")
    (out / "sub" / "b.txt").unlink()
    (out / "extra.txt").write_text("x")
    problems = " ".join(mf.verify_artifacts(out))
    assert "a.json" in problems and "sub/b.txt" in problems and "extra.txt" in problems


def test_verify_unreadable_manifest(tmp_path: Path) -> None:
    out = _commit(tmp_path)
    (out / "manifest.json").chmod(0o644)
    (out / "manifest.json").write_text("{not json")
    assert any("manifest" in p for p in mf.verify_artifacts(out))
    (out / "manifest.json").unlink()
    assert any("manifest" in p for p in mf.verify_artifacts(out))


# ---- dataset_fingerprint -----------------------------------------------------------------

IST_DAY1 = 1767585000  # 2026-01-05 09:30 IST
DAY = 86400


def _make_db(path: Path, extra: list[tuple] | None = None) -> None:
    con = duckdb.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE market_data (symbol VARCHAR, interval VARCHAR, timestamp BIGINT, "
            "open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT)"
        )
        rows = [
            ("AAA", "1m", IST_DAY1, 1, 1, 1, 10.0, 5),
            ("AAA", "1m", IST_DAY1 + 300, 1, 1, 1, 11.0, 5),
            ("BBB", "1m", IST_DAY1, 1, 1, 1, 20.5, 5),
            ("AAA", "D", IST_DAY1, 1, 1, 1, 99.0, 5),
        ] + (extra or [])
        con.executemany("INSERT INTO market_data VALUES (?,?,?,?,?,?,?,?)", rows)
    finally:
        con.close()


def test_fingerprint_stable_and_hex(tmp_path: Path) -> None:
    db = tmp_path / "h.duckdb"
    _make_db(db)
    a = mf.dataset_fingerprint(["BBB", "AAA"], "5m", "2026-01-05", db)
    b = mf.dataset_fingerprint(["AAA", "BBB"], "5m", "2026-01-05", db)
    assert a == b and len(a) == 64 and int(a, 16) >= 0


def test_fingerprint_ignores_rows_after_as_of(tmp_path: Path) -> None:
    base = tmp_path / "a.duckdb"
    more = tmp_path / "b.duckdb"
    _make_db(base)
    _make_db(more, [("AAA", "1m", IST_DAY1 + DAY, 1, 1, 1, 50.0, 5)])
    assert mf.dataset_fingerprint(["AAA"], "5m", "2026-01-05", base) == mf.dataset_fingerprint(
        ["AAA"], "5m", "2026-01-05", more
    )
    assert mf.dataset_fingerprint(["AAA"], "5m", "2026-01-06", base) != mf.dataset_fingerprint(
        ["AAA"], "5m", "2026-01-06", more
    )


def test_fingerprint_changes_on_altered_close(tmp_path: Path) -> None:
    base = tmp_path / "a.duckdb"
    _make_db(base)
    before = mf.dataset_fingerprint(["AAA"], "5m", "2026-01-05", base)
    con = duckdb.connect(str(base))
    try:
        con.execute(
            "UPDATE market_data SET close = 10.5 WHERE symbol='AAA' AND timestamp = ?", [IST_DAY1]
        )
    finally:
        con.close()
    assert mf.dataset_fingerprint(["AAA"], "5m", "2026-01-05", base) != before


def test_fingerprint_missing_symbol_and_interval(tmp_path: Path) -> None:
    db = tmp_path / "h.duckdb"
    _make_db(db)
    with_missing = mf.dataset_fingerprint(["AAA", "ZZZ"], "5m", "2026-01-05", db)
    assert with_missing != mf.dataset_fingerprint(["AAA"], "5m", "2026-01-05", db)
    assert mf.dataset_fingerprint(["AAA"], "5m", "2026-01-05", db) != mf.dataset_fingerprint(
        ["AAA"], "1d", "2026-01-05", db
    )
    assert mf.dataset_fingerprint([], "5m", "2026-01-05", db)


def test_fingerprint_end_of_day_inclusive(tmp_path: Path) -> None:
    db = tmp_path / "h.duckdb"
    late = 1767585000 + 6 * 3600 + 3000  # 2026-01-05 16:20 IST, still the same IST day
    _make_db(db, [("AAA", "1m", late, 1, 1, 1, 1.0, 1)])
    ref = tmp_path / "r.duckdb"
    _make_db(ref)
    assert mf.dataset_fingerprint(["AAA"], "5m", "2026-01-05", db) != mf.dataset_fingerprint(
        ["AAA"], "5m", "2026-01-05", ref
    )


def test_fingerprint_closes_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "h.duckdb"
    _make_db(db)
    closed: list[bool] = []
    real = duckdb.connect

    class Wrapper:
        def __init__(self, con: Any) -> None:
            self._con = con

        def execute(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("boom")

        def close(self) -> None:
            closed.append(True)
            self._con.close()

    monkeypatch.setattr(duckdb, "connect", lambda *a, **k: Wrapper(real(*a, **k)))
    with pytest.raises(RuntimeError):
        mf.dataset_fingerprint(["AAA"], "5m", "2026-01-05", db)
    assert closed == [True]


# ---- environment_snapshot ----------------------------------------------------------------


def test_environment_snapshot(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        'version = 1\n\n[[package]]\nname = "pandas"\nversion = "2.3.3"\n\n'
        '[[package]]\nname = "Py_Arrow"\nversion = "1.0"\n'
    )
    snap = mf.environment_snapshot(["pandas", "py-arrow"], lock)
    assert snap["packages"] == {"pandas": "2.3.3", "py-arrow": "1.0"}
    assert snap["python"].count(".") == 2


def test_environment_snapshot_missing_package(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text('[[package]]\nname = "pandas"\nversion = "2.3.3"\n')
    with pytest.raises(KeyError, match="nopkg"):
        mf.environment_snapshot(["pandas", "nopkg"], lock)


def test_environment_snapshot_default_lock() -> None:
    assert "pandas" in mf.environment_snapshot(["pandas"])["packages"]


# ---- git_state ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_git_clean(repo: Path) -> None:
    state = mf.git_state(repo)
    assert len(state["code_commit"]) == 40
    assert state["code_dirty"] is False and state["dirty_paths"] == []


def test_git_dirty_paths(repo: Path) -> None:
    (repo / "a.txt").write_text("changed")
    (repo / "b.txt").unlink()
    (repo / "new dir").mkdir()
    (repo / "new dir" / "c.txt").write_text("c")
    (repo / "ignored.txt").write_text("i")
    state = mf.git_state(repo)
    assert state["code_dirty"] is True
    assert state["dirty_paths"] == ["a.txt", "b.txt", "new dir/c.txt"]


def test_git_staged_added(repo: Path) -> None:
    (repo / "z.txt").write_text("z")
    _git(repo, "add", "z.txt")
    assert mf.git_state(repo)["dirty_paths"] == ["z.txt"]


def test_git_absent(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", boom)
    assert mf.git_state(repo) == {"code_commit": "unknown", "code_dirty": True, "dirty_paths": []}


def test_git_not_a_repo(tmp_path: Path) -> None:
    state = mf.git_state(tmp_path)
    assert state["code_commit"] == "unknown" and state["code_dirty"] is True


def test_good_manifest_roundtrips_json() -> None:
    m = good_manifest()
    assert mf.validate_manifest(json.loads(json.dumps(copy.deepcopy(m)))) == []


def test_first_run_with_no_parent_and_no_branch_label_is_valid_without_capability_entries() -> None:
    m = good_manifest()
    m["parent_run_id"] = None
    m["strategy"]["branch_label"] = None
    assert mf.validate_manifest(m) == []


def test_capability_bound_nulls_still_need_an_entry() -> None:
    m = good_manifest()
    m["calendar_version"] = None
    m["missing_capabilities"] = []
    assert any("calendar_version" in p for p in mf.validate_manifest(m))


def test_capability_nullable_excludes_the_always_nullable_paths() -> None:
    assert "parent_run_id" not in mf.CAPABILITY_NULLABLE
    assert "strategy.branch_label" not in mf.CAPABILITY_NULLABLE
    assert set(mf.CAPABILITY_NULLABLE) < set(mf.NULLABLE)


# ---- dataset_fingerprint: stored interval and column coverage ----------------------------


def _empty_digest(tmp_path: Path) -> str:
    empty = tmp_path / "empty.duckdb"
    con = duckdb.connect(str(empty))
    try:
        con.execute(
            "CREATE TABLE market_data (symbol VARCHAR, interval VARCHAR, timestamp BIGINT, "
            "open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT)"
        )
    finally:
        con.close()
    return mf.dataset_fingerprint(["AAA"], "5m", "2026-01-05", empty)


def test_fingerprint_reads_stored_1m_rows_for_5m_bars(tmp_path: Path) -> None:
    db = tmp_path / "h.duckdb"
    _make_db(db)
    assert mf.dataset_fingerprint(["AAA"], "5m", "2026-01-05", db) != _empty_digest(tmp_path)


@pytest.mark.parametrize("bar", ["1m", "5m", "15m", "1h"])
def test_fingerprint_intraday_bars_share_the_1m_store(tmp_path: Path, bar: str) -> None:
    db = tmp_path / "h.duckdb"
    _make_db(db)
    assert mf.dataset_fingerprint(["AAA"], bar, "2026-01-05", db) == mf.dataset_fingerprint(
        ["AAA"], "1m", "2026-01-05", db
    )


def test_fingerprint_daily_reads_D_rows(tmp_path: Path) -> None:
    db = tmp_path / "h.duckdb"
    _make_db(db)
    assert mf.dataset_fingerprint(["AAA"], "1d", "2026-01-05", db) != _empty_digest(tmp_path)


@pytest.mark.parametrize("column", ["high", "volume", "open", "low"])
def test_fingerprint_changes_on_altered_column(tmp_path: Path, column: str) -> None:
    db = tmp_path / "h.duckdb"
    _make_db(db)
    before = mf.dataset_fingerprint(["AAA"], "5m", "2026-01-05", db)
    con = duckdb.connect(str(db))
    try:
        con.execute(
            f"UPDATE market_data SET {column} = 7 WHERE symbol='AAA' AND timestamp = ?", [IST_DAY1]
        )
    finally:
        con.close()
    assert mf.dataset_fingerprint(["AAA"], "5m", "2026-01-05", db) != before


@pytest.mark.parametrize("bar", ["2m", "1w", "", "5", "D"])
def test_fingerprint_unknown_bar_size_raises(tmp_path: Path, bar: str) -> None:
    db = tmp_path / "h.duckdb"
    _make_db(db)
    with pytest.raises(ValueError):
        mf.dataset_fingerprint(["AAA"], bar, "2026-01-05", db)
