"""Immutable published dataset versions (plan section 3, Phase 3; revision 4 NSE source)."""
from __future__ import annotations

import json
import os
import stat
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from signal_engine.backtest.data_readiness import nse_dataset, nse_eod, snapshot
from signal_engine.backtest.data_readiness.calendar import NseCalendar
from signal_engine.tests.dq_fixtures import weekdays

DAYS = [date.fromisoformat(d) for d in weekdays("2025-12-01", "2026-01-30")]  # crosses a year
SYMBOLS = ("AAA", "BBB", "CCC")


def _calendar(days=DAYS):
    hol = frozenset(d for d in (date.fromisoformat(x) for x in weekdays("2025-01-01", "2026-12-31"))
                    if d not in set(days) and DAYS[0] <= d <= DAYS[-1])
    return NseCalendar(holidays=hol, specials={}, mocks=frozenset(), unconfirmed=frozenset(),
                       coverage=(2025, 2026), regular_session=("09:15", "15:30"),
                       version="cal-test", source="synthetic")


def _staging(tmp_path, drop=None, anns=()):
    con = nse_eod.open_staging(tmp_path / "nse.duckdb")
    rows = []
    for s in SYMBOLS:
        for i, d in enumerate(DAYS):
            if drop and (s, d) in drop:
                continue
            px = 100.0 + i
            rows.append((s, "INE0", "EQ", d, px, px + 1, px - 1, px, px, px, 1000 + i, 1.0, 5,
                         "f.zip", "sha"))
    df = pd.DataFrame(rows, columns=[*nse_eod.COLUMNS, "source_file", "source_sha256"])
    con.register("_r", df)
    con.execute("INSERT INTO nse_daily_raw SELECT * FROM _r")
    con.close()
    out = tmp_path / "adjusted.duckdb"
    nse_dataset.build(tmp_path / "nse.duckdb", out, symbols=SYMBOLS, announcements=list(anns),
                      overrides=(), calendar_version="cal-test")
    return out


def _lenient():
    """The repository policy, with room for one excluded symbol out of three."""
    import dataclasses

    from signal_engine.backtest.data_readiness import policy as pol
    return dataclasses.replace(pol.load_policy(), max_defect_excluded_symbols=5,
                               max_defect_excluded_symbol_share=1.0)


def _publish(tmp_path, staging, version="nse-daily-20260130-001", accepted=(), **kw):
    return snapshot.publish(staging, version_id=version, root=tmp_path / "datasets",
                            git_dir=tmp_path / "git", calendar=_calendar(),
                            accepted_exclusions=accepted, cause="test", **kw)


@pytest.fixture
def published(tmp_path):
    staging = _staging(tmp_path)
    return tmp_path, staging, _publish(tmp_path, staging)


def test_publish_writes_content_addressed_objects(published):
    tmp, _, v = published
    objs = list((tmp / "datasets" / "objects").rglob("*.parquet"))
    assert len(objs) == len(SYMBOLS) * 2 + 2           # symbol x year, plus index and events
    for p in objs:
        assert p.stem == snapshot.sha256_file(p) and p.parent.name == p.stem[:2]


def test_manifest_records_the_required_metadata(published):
    tmp, _, v = published
    m = json.loads((tmp / "datasets" / "versions" / f"{v.version_id}.json").read_text())
    for key in ("dataset_version", "dataset_fingerprint", "calendar_version",
                "corporate_action_version", "overrides_source", "series_rule_version",
                "source", "as_of", "coverage", "excluded_symbols", "known_limitations",
                "warnings", "readiness_state", "audit_sha256", "policy_version", "index_sha256",
                "events_sha256", "fingerprint_algo", "status", "created_ist"):
        assert key in m, key
    assert m["dataset_fingerprint"] == v.root_hash and m["status"] == "published"
    assert m["coverage"]["symbols"] == 3 and m["coverage"]["start"] == "2025-12-01"
    assert m["calendar_version"] == "cal-test"


def test_git_summary_carries_the_root_hash(published):
    tmp, _, v = published
    g = json.loads((tmp / "git" / f"{v.version_id}.json").read_text())
    assert g["root_hash"] == v.root_hash and g["dataset_version"] == v.version_id


def test_verify_passes_on_a_fresh_version(published):
    tmp, _, v = published
    assert snapshot.verify(v.version_id, root=tmp / "datasets", git_dir=tmp / "git").ok


def _writable(p: Path):
    os.chmod(p, stat.S_IWUSR | stat.S_IRUSR | (stat.S_IXUSR if p.is_dir() else 0))


def test_one_changed_byte_in_an_object_fails_verify(published):
    tmp, _, v = published
    obj = next((tmp / "datasets" / "objects").rglob("*.parquet"))
    _writable(obj)
    data = bytearray(obj.read_bytes())
    data[len(data) // 2] ^= 0xFF
    obj.write_bytes(bytes(data))
    res = snapshot.verify(v.version_id, root=tmp / "datasets", git_dir=tmp / "git")
    assert not res.ok and any("hash" in e for e in res.errors)


def test_edited_manifest_fails_verify(published):
    tmp, _, v = published
    path = tmp / "datasets" / "versions" / f"{v.version_id}.json"
    _writable(path)
    m = json.loads(path.read_text())
    m["coverage"]["symbols"] = 99
    path.write_text(json.dumps(m))
    assert not snapshot.verify(v.version_id, root=tmp / "datasets", git_dir=tmp / "git").ok


def test_missing_object_fails_verify(published):
    tmp, _, v = published
    obj = next((tmp / "datasets" / "objects").rglob("*.parquet"))
    _writable(obj.parent)
    obj.unlink()
    assert not snapshot.verify(v.version_id, root=tmp / "datasets", git_dir=tmp / "git").ok


def test_published_files_are_read_only(published):
    tmp, _, v = published
    for p in list((tmp / "datasets" / "objects").rglob("*.parquet")) + [
            tmp / "datasets" / "versions" / f"{v.version_id}.json"]:
        assert not (p.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def test_open_version_gives_the_staged_bars_exactly(published):
    tmp, staging, v = published
    import duckdb
    want = duckdb.connect(str(staging), read_only=True).execute(
        "SELECT symbol, timestamp, open, high, low, close, volume FROM market_data "
        "ORDER BY symbol, timestamp").df()
    with snapshot.open_version(v.version_id, root=tmp / "datasets", git_dir=tmp / "git") as con:
        got = con.execute("SELECT symbol, timestamp, open, high, low, close, volume FROM market_data "
                          "ORDER BY symbol, timestamp").df()
    pd.testing.assert_frame_equal(want, got)


def test_open_version_refuses_a_tampered_version(published):
    tmp, _, v = published
    obj = next((tmp / "datasets" / "objects").rglob("*.parquet"))
    _writable(obj)
    obj.write_bytes(b"x")
    with pytest.raises(snapshot.SnapshotError):
        with snapshot.open_version(v.version_id, root=tmp / "datasets", git_dir=tmp / "git"):
            pass


def test_existing_version_is_never_overwritten(published):
    tmp, staging, v = published
    with pytest.raises(snapshot.SnapshotError, match="exists"):
        _publish(tmp, staging, version=v.version_id)


def test_republishing_identical_data_reuses_the_objects(published):
    tmp, staging, v = published
    before = sorted(p.name for p in (tmp / "datasets" / "objects").rglob("*.parquet"))
    v2 = _publish(tmp, staging, version="nse-daily-20260130-002")
    after = sorted(p.name for p in (tmp / "datasets" / "objects").rglob("*.parquet"))
    assert before == after and v2.index_sha256 == v.index_sha256


def test_not_ready_data_is_not_published(tmp_path):
    # every symbol misses 3 sessions: the data-defect threshold blocks (3 of 3 symbols)
    drop = {(s, d) for s in SYMBOLS for d in DAYS[5:8]}
    staging = _staging(tmp_path, drop=drop)
    with pytest.raises(snapshot.SnapshotError, match="NOT_READY"):
        _publish(tmp_path, staging, accepted=SYMBOLS)
    versions = tmp_path / "datasets" / "versions"
    assert not versions.exists() or not list(versions.iterdir())


def test_unaccepted_exclusion_blocks_and_accepted_one_is_recorded(tmp_path):
    staging = _staging(tmp_path, drop={("CCC", DAYS[10]), ("CCC", DAYS[11])})
    with pytest.raises(snapshot.SnapshotError, match="CCC"):
        _publish(tmp_path, staging, accepted=(), policy=_lenient())
    v = _publish(tmp_path, staging, accepted=("CCC",), policy=_lenient())
    m = json.loads((tmp_path / "datasets" / "versions" / f"{v.version_id}.json").read_text())
    (ex,) = m["excluded_symbols"]
    assert ex["symbol"] == "CCC" and ex["missing_sessions"] == 2
    assert ex["ranges"] == [[DAYS[10].isoformat(), DAYS[11].isoformat()]]
    assert "no NSE trading" in ex["reason"]


def test_blocked_build_is_not_published(tmp_path):
    anns = [{"symbol": "AAA", "exDate": DAYS[3].strftime("%d-%b-%Y"), "subject": "Consolidation",
             "faceVal": "10"}]
    staging = _staging(tmp_path, anns=anns)
    with pytest.raises(snapshot.SnapshotError, match="BLOCKED"):
        _publish(tmp_path, staging)


def test_refuses_the_landing_store(tmp_path):
    from signal_engine.backtest.data_readiness import staging as st
    with pytest.raises(snapshot.SnapshotError):
        _publish(tmp_path, st.LANDING_STORE)


def test_a_child_version_records_its_published_parent(published):
    tmp, staging, v = published
    child = _publish(tmp, staging, version="nse-daily-20260130-002", parent=v.version_id)
    m = json.loads((tmp / "datasets" / "versions" / f"{child.version_id}.json").read_text())
    assert m["parent"] == v.version_id


def test_an_unknown_parent_is_refused(published):
    tmp, staging, _ = published
    with pytest.raises(snapshot.SnapshotError, match="parent"):
        _publish(tmp, staging, version="nse-daily-20260130-002", parent="nse-daily-19990101-001")
