"""Immutable published dataset versions (plan section 3, Phase 3; revision 4 NSE daily source).

    db/datasets/objects/<sha[:2]>/<sha>.parquet   content-addressed, read-only, shared by versions
    db/datasets/versions/<version_id>.json         the manifest; read-only; never rewritten
    signal_engine/research/data_versions/<id>.json small git copy: version id and root hash

One object per (symbol, year) holds the adjusted bars plus the raw NSE values, the series, the
factors and the source file. A partition index and the corporate-action events are objects too.
The manifest's root hash (sha256 of its canonical body) is the dataset fingerprint; git records
it, so any later change to an object, the index or the manifest is detectable.

Publication is gated: it re-runs the readiness audit on the exact staging file, and refuses a
NOT_READY state, a BLOCKED build, an existing version id, or a data-defect exclusion the owner has
not explicitly accepted. Verification recomputes every hash; it never trusts file metadata.

    uv run python -m signal_engine.backtest.data_readiness.snapshot publish \
        --staging db/staging/nse-D-adjusted-20261005.duckdb --version-id nse-daily-20261005-001 \
        --accept FORCEMOT,CGPOWER
    uv run python -m signal_engine.backtest.data_readiness.snapshot verify --version-id ...
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import stat
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from signal_engine.backtest.data_readiness import audit as _audit
from signal_engine.backtest.data_readiness import policy as pol
from signal_engine.backtest.data_readiness import staging as _staging
from signal_engine.backtest.data_readiness import state as st
from signal_engine.backtest.data_readiness.calendar import NseCalendar, load_calendar
from signal_engine.research.intake.fingerprints import canonical_json
from signal_engine.timeutils import IST
from utils.logging import get_logger

logger = get_logger(__name__)

_REPO = Path(__file__).resolve().parents[3]
DEFAULT_ROOT = _REPO / "db" / "datasets"
DEFAULT_GIT_DIR = _REPO / "signal_engine" / "research" / "data_versions"
FINGERPRINT_ALGO = "snapshot-v1: sha256(canonical_json(manifest body)); objects sha256 of bytes"
_READ_ONLY = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
_OBJECT_COLS = ["symbol", "exchange", "interval", "timestamp", "session", "series", "open", "high",
                "low", "close", "volume", "raw_open", "raw_high", "raw_low", "raw_close",
                "raw_volume", "price_factor", "volume_factor", "source_file", "source_sha256"]
_ROOT_FIELDS = ("root_hash", "dataset_fingerprint")


class SnapshotError(RuntimeError):
    """A version cannot be published, verified or opened safely."""


@dataclass(frozen=True)
class Version:
    version_id: str
    root_hash: str
    index_sha256: str
    manifest_path: Path


@dataclass
class VerifyResult:
    ok: bool
    objects_checked: int = 0
    errors: list[str] = field(default_factory=list)


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _object_path(root: Path, sha: str) -> Path:
    return root / "objects" / sha[:2] / f"{sha}.parquet"


def _parquet_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), buf, compression="zstd")
    return buf.getvalue()


def _put_object(root: Path, blob: bytes) -> str:
    """Store `blob` under its sha256 (write-once; an existing object is shared, not rewritten)."""
    sha = hashlib.sha256(blob).hexdigest()
    path = _object_path(root, sha)
    if path.exists():
        if sha256_file(path) != sha:
            raise SnapshotError(f"object {sha} on disk does not match its name")
        return sha
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(blob)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    os.chmod(path, _READ_ONLY)
    return sha


def _root_hash(body: dict) -> str:
    return hashlib.sha256(canonical_json({k: v for k, v in body.items()
                                          if k not in _ROOT_FIELDS}).encode()).hexdigest()


# ---------------------------------------------------------------- publish

def _staged(con) -> tuple[pd.DataFrame, pd.DataFrame, dict, pd.DataFrame]:
    bars = con.execute(
        "SELECT m.symbol, m.exchange, m.interval, m.timestamp, f.session, f.series, m.open, "
        "m.high, m.low, m.close, m.volume, f.raw_open, f.raw_high, f.raw_low, f.raw_close, "
        "f.raw_volume, f.price_factor, f.volume_factor, f.source_file, f.source_sha256 "
        "FROM market_data m JOIN daily_raw_factors f ON f.symbol = m.symbol AND "
        "CAST(epoch(CAST(f.session AS TIMESTAMP)) AS BIGINT) = m.timestamp "
        "ORDER BY m.symbol, m.timestamp").df()
    events = con.execute("SELECT * FROM ca_events ORDER BY symbol, ex_date, kind").df()
    meta = dict(con.execute("SELECT key, value FROM dataset_meta").fetchall())
    gaps = con.execute("SELECT * FROM series_gaps").df()
    return bars, events, meta, gaps


def _ranges(missing: list[date], sessions: Sequence[date]) -> list[list[str]]:
    """Runs of consecutive calendar sessions, as [first, last] pairs."""
    pos = {d: i for i, d in enumerate(sessions)}
    out: list[list[str]] = []
    prev = None
    for d in missing:
        if prev is not None and pos[d] == pos[prev] + 1:
            out[-1][1] = d.isoformat()
        else:
            out.append([d.isoformat(), d.isoformat()])
        prev = d
    return out


def _exclusions(bars: pd.DataFrame, gaps: pd.DataFrame, projection: dict,
                calendar: NseCalendar) -> list[dict]:
    by_rule = projection["defect_excluded_by_rule"]
    out = []
    for sym in sorted({s for syms in by_rule.values() for s in syms}):
        days = sorted(pd.to_datetime(bars.loc[bars.symbol == sym, "session"]).dt.date)
        expected = calendar.expected_sessions(days[0], days[-1]) if days else ()
        have = set(days)
        missing = [d for d in expected if d not in have]
        g = gaps[gaps.symbol == sym] if len(gaps) else gaps
        gap_days = set(pd.to_datetime(g["session"]).dt.date) if len(g) else set()
        other = sorted(set(g["series"])) if len(g) else []
        if missing and set(missing) <= gap_days:
            reason = (f"traded only in series {', '.join(other)} on the missing sessions; "
                      "series rule v1 uses EQ, else BE")
        elif missing:
            reason = "no NSE trading recorded in any series on the missing sessions"
        else:
            reason = "see rules"
        out.append({"symbol": sym, "rules": sorted(r for r, s in by_rule.items() if sym in s),
                    "missing_sessions": len(missing), "ranges": _ranges(missing, expected),
                    "reason": reason, "accepted": True})
    return out


def publish(staging_db: str | Path, *, version_id: str, accepted_exclusions: Sequence[str],
            cause: str, root: str | Path = DEFAULT_ROOT, git_dir: str | Path = DEFAULT_GIT_DIR,
            calendar: NseCalendar | None = None, policy: pol.Policy | None = None,
            parent: str | None = None) -> Version:
    """Publish the adjusted staging dataset as an immutable version. Raises SnapshotError when any
    gate fails; nothing is published then."""
    root, git_dir, staging_db = Path(root), Path(git_dir), Path(staging_db).resolve()
    if _staging._is_landing_store(staging_db):
        raise SnapshotError("the landing store is never published")
    manifest_path = root / "versions" / f"{version_id}.json"
    if manifest_path.exists() or (git_dir / f"{version_id}.json").exists():
        raise SnapshotError(f"version {version_id} exists; a published version is never rewritten")
    if parent is not None and not (root / "versions" / f"{parent}.json").is_file():
        raise SnapshotError(f"parent version {parent} is not published under {root}")
    calendar = calendar or load_calendar()
    policy = policy or pol.load_policy()
    with duckdb.connect(str(staging_db), read_only=True) as con:
        bars, events, meta, gaps = _staged(con)
    if meta.get("status") != "OK":
        raise SnapshotError(f"staging build status is {meta.get('status')} (BLOCKED or unknown)")

    with tempfile.TemporaryDirectory() as tmp:
        report = _audit.run_audit(staging_db, Path(tmp), policy, ("D",), calendar=calendar)
        audit_sha = sha256_file(Path(tmp) / "audit.json")
    proj = report["intervals"]["D"]["projection"]
    if proj["state"] == st.NOT_READY:
        raise SnapshotError(f"readiness audit is NOT_READY: {proj['reasons']}")
    excluded = _exclusions(bars, gaps, proj, calendar)
    unaccepted = [e["symbol"] for e in excluded if e["symbol"] not in set(accepted_exclusions)]
    if unaccepted:
        raise SnapshotError(f"exclusions not accepted by the owner: {unaccepted}")

    rows = []
    for (sym, year), g in bars.groupby([bars.symbol, pd.to_datetime(bars.session).dt.year]):
        part = g[_OBJECT_COLS].reset_index(drop=True)
        sha = _put_object(root, _parquet_bytes(part))
        rows.append({"interval": "D", "symbol": sym, "period": int(year), "object_sha256": sha,
                     "rows": len(part), "first_timestamp": int(part.timestamp.min()),
                     "last_timestamp": int(part.timestamp.max())})
    index = pd.DataFrame(rows).sort_values(["interval", "symbol", "period"]).reset_index(drop=True)
    index_sha = _put_object(root, _parquet_bytes(index))
    events_sha = _put_object(root, _parquet_bytes(events.astype({"ex_date": "string"})))

    sessions = pd.to_datetime(bars.session).dt.date
    body = {
        "dataset_version": version_id, "parent": parent, "status": "published", "cause": cause,
        "created_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
        "source": meta["source"], "as_of": str(sessions.max()), "frequency": "D",
        "price_basis": meta["price_basis"], "known_limitations": meta["known_limitations"],
        "calendar_version": calendar.version,
        "corporate_action_version": meta["corporate_action_version"],
        "announcements_source": meta.get("announcements_source"),
        "overrides_source": meta.get("overrides_source"), "series_rule": meta["series_rule"],
        "series_rule_version": meta["series_rule_version"], "policy_version": policy.version,
        "coverage": {"start": str(sessions.min()), "end": str(sessions.max()),
                     "symbols": int(bars.symbol.nunique()), "rows": len(bars),
                     "sessions": int(sessions.nunique()),
                     "rows_by_series": {k: int(v) for k, v in bars.series.value_counts().items()}},
        "readiness_state": proj["state"], "warnings": proj["warnings"],
        "audit_sha256": audit_sha, "excluded_symbols": excluded,
        "index_sha256": index_sha, "events_sha256": events_sha, "objects": len(index),
        "fingerprint_algo": FINGERPRINT_ALGO, "staging_build": meta.get("built_at_ist"),
    }
    root_hash = _root_hash(body)
    manifest = {**body, "root_hash": root_hash, "dataset_fingerprint": root_hash}
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_manifest = manifest_path.with_name(manifest_path.name + ".tmp")
    tmp_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    os.replace(tmp_manifest, manifest_path)
    os.chmod(manifest_path, _READ_ONLY)
    git_dir.mkdir(parents=True, exist_ok=True)
    summary = {"dataset_version": version_id, "root_hash": root_hash, "parent": parent,
               "status": "published", "cause": cause, "created_ist": body["created_ist"],
               "coverage": body["coverage"], "readiness_state": body["readiness_state"],
               "excluded_symbols": [e["symbol"] for e in excluded]}
    # Atomic, like the manifest. A crash between the two writes leaves a manifest without a git
    # record: resolve_version refuses it and the id counts as taken; check and delete it by hand.
    git_record = git_dir / f"{version_id}.json"
    tmp_record = git_record.with_name(git_record.name + ".tmp")
    tmp_record.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    os.replace(tmp_record, git_record)
    logger.info(f"published {version_id} root {root_hash[:12]} objects {len(index)}")
    return Version(version_id, root_hash, index_sha, manifest_path)


# ---------------------------------------------------------------- verify / open

def _load_manifest(version_id: str, root: Path) -> dict:
    path = root / "versions" / f"{version_id}.json"
    if not path.exists():
        raise SnapshotError(f"version {version_id} not found under {root}")
    return json.loads(path.read_text())


def verify(version_id: str, *, root: str | Path = DEFAULT_ROOT,
           git_dir: str | Path = DEFAULT_GIT_DIR) -> VerifyResult:
    """Recompute every hash of a version: manifest root, git record, index, events, objects."""
    root, git_dir = Path(root), Path(git_dir)
    res = VerifyResult(ok=False)
    try:
        m = _load_manifest(version_id, root)
    except (SnapshotError, json.JSONDecodeError) as exc:
        res.errors.append(str(exc))
        return res
    if _root_hash(m) != m.get("root_hash") or m.get("root_hash") != m.get("dataset_fingerprint"):
        res.errors.append("manifest root hash does not match its body")
    g = git_dir / f"{version_id}.json"
    if not g.exists() or json.loads(g.read_text()).get("root_hash") != m.get("root_hash"):
        res.errors.append("git record root hash missing or different")
    for name in ("index_sha256", "events_sha256"):
        p = _object_path(root, m.get(name, ""))
        if not p.exists() or sha256_file(p) != m.get(name):
            res.errors.append(f"{name} object missing or hash mismatch")
    ip = _object_path(root, m.get("index_sha256", ""))
    if ip.exists() and sha256_file(ip) == m.get("index_sha256"):
        index = pd.read_parquet(ip)
        if len(index) != m.get("objects"):
            res.errors.append("index object count differs from the manifest")
        for sha in index.object_sha256:
            p = _object_path(root, sha)
            res.objects_checked += 1
            if not p.exists():
                res.errors.append(f"object {sha[:12]} missing")
            elif sha256_file(p) != sha:
                res.errors.append(f"object {sha[:12]} hash mismatch")
    res.ok = not res.errors
    return res


def run_fingerprint(manifest: dict, symbols, end: str | None, *,
                    root: str | Path = DEFAULT_ROOT) -> str:
    """sha256 of the sorted object hashes a run read: the (symbol, year) partitions of its symbols
    up to the year of `end` (all years when None), plus the version root hash."""
    index = pd.read_parquet(_object_path(Path(root), manifest["index_sha256"]))
    wanted = set(symbols)
    rows = index[index.symbol.isin(wanted)]
    if end:
        rows = rows[rows.period <= date.fromisoformat(end[:10]).year]
    body = {"root_hash": manifest["root_hash"], "objects": sorted(rows.object_sha256)}
    return hashlib.sha256(canonical_json(body).encode()).hexdigest()


def resolve_version(version_id: str | None, *, root: str | Path = DEFAULT_ROOT,
                    git_dir: str | Path = DEFAULT_GIT_DIR) -> dict:
    """The manifest of the version a run will use: `version_id`, or the newest published one.

    Only `published` versions resolve; `legacy`, `rejected` or unknown status is refused. The git
    record and the manifest must agree on the status and the root hash (full hash verification
    happens in `open_version`)."""
    root, git_dir = Path(root), Path(git_dir)
    if version_id is None:
        records = []
        for g in sorted(git_dir.glob("*.json")) if git_dir.is_dir() else []:
            doc = json.loads(g.read_text())
            if doc.get("status") == "published" and doc.get("dataset_version"):
                records.append((doc.get("created_ist") or "", doc["dataset_version"]))
        if not records:
            raise SnapshotError(f"no published dataset version under {git_dir}")
        version_id = max(records)[1]
    g = git_dir / f"{version_id}.json"
    if not g.exists():
        raise SnapshotError(f"dataset version {version_id} not found in {git_dir}")
    record = json.loads(g.read_text())
    m = _load_manifest(version_id, root)
    for doc, where in ((record, "git record"), (m, "manifest")):
        if doc.get("status") != "published":
            raise SnapshotError(f"dataset version {version_id} {where} status is "
                                f"{doc.get('status')!r}; only published versions can be used")
    if record.get("root_hash") != m.get("root_hash"):
        raise SnapshotError(f"dataset version {version_id}: git record and manifest disagree")
    return m


@contextmanager
def open_version(version_id: str, *, root: str | Path = DEFAULT_ROOT,
                 git_dir: str | Path = DEFAULT_GIT_DIR, symbols: Sequence[str] | None = None,
                 end_ts: int | None = None,
                 expected_root: str | None = None) -> Iterator[duckdb.DuckDBPyConnection]:
    """A read-only DuckDB view `market_data` over a verified version (the landing-store layout).

    `symbols` and `end_ts` (epoch seconds, inclusive) limit the view to a run's scope.
    `expected_root` is the root hash a run recorded; a different version on disk is refused."""
    root = Path(root)
    res = verify(version_id, root=root, git_dir=git_dir)
    if not res.ok:
        raise SnapshotError(f"version {version_id} failed verification: {res.errors[:3]}")
    m = _load_manifest(version_id, root)
    if expected_root is not None and m["root_hash"] != expected_root:
        raise SnapshotError(f"version {version_id} root hash {m['root_hash'][:12]} is not the "
                            f"recorded {expected_root[:12]}")
    index = pd.read_parquet(_object_path(root, m["index_sha256"]))
    files = [str(_object_path(root, s)) for s in index.object_sha256]
    where = []
    if symbols is not None:
        names = ", ".join("'" + s.replace("'", "''") + "'" for s in symbols) or "NULL"
        where.append(f"symbol IN ({names})")
    if end_ts is not None:
        where.append(f"timestamp <= {int(end_ts)}")
    con = duckdb.connect(":memory:")
    try:
        con.execute(f"CREATE VIEW bars AS SELECT * FROM read_parquet({files!r})")
        con.execute(
            "CREATE VIEW market_data AS SELECT symbol, exchange, interval, timestamp, open, high, "
            f"low, close, volume, 0::BIGINT AS oi, TIMESTAMP '{m['created_ist']}' AS created_at "
            "FROM bars" + (f" WHERE {' AND '.join(where)}" if where else ""))
        yield con
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="signal_engine.backtest.data_readiness.snapshot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("publish")
    p.add_argument("--staging", required=True)
    p.add_argument("--version-id", required=True)
    p.add_argument("--accept", default="", help="comma-separated owner-accepted exclusions")
    p.add_argument("--cause", default="publish")
    p.add_argument("--parent", default=None, help="the published version this one supersedes")
    v = sub.add_parser("verify")
    v.add_argument("--version-id", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "publish":
        ver = publish(args.staging, version_id=args.version_id, cause=args.cause, parent=args.parent,
                      accepted_exclusions=[s for s in args.accept.split(",") if s])
        print(f"published {ver.version_id} fingerprint {ver.root_hash}")
        return 0
    res = verify(args.version_id)
    print(f"{args.version_id}: {'OK' if res.ok else 'FAILED'}, objects checked "
          f"{res.objects_checked}, errors {res.errors[:10]}")
    return 0 if res.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
