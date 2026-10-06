"""Publish the intraday 5m build as an immutable dataset version (plan section 12).

Same store and fingerprint algorithm as the daily versions (snapshot.py): content-addressed
read-only objects, a manifest whose root hash is the dataset fingerprint, and a small git record.
Publication is refused unless the validation report of the same build says READY or
READY_WITH_WARNINGS with every hard criterion passed. `--dry-run` evaluates every gate and
reports the content hash without writing anything.

    uv run python -m signal_engine.backtest.data_readiness.intraday_publish \
        --build db/staging/intraday/build-<root12> --daily-version nse-daily-20261005-002 \
        --version-id nse-intraday-5m-YYYYMMDD-001 [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

import pandas as pd

from signal_engine.backtest.data_readiness import intraday_convert as ic
from signal_engine.backtest.data_readiness import intraday_plan as ip
from signal_engine.backtest.data_readiness import intraday_recovery as ir
from signal_engine.backtest.data_readiness import snapshot as sn
from signal_engine.backtest.data_readiness import state as st
from signal_engine.backtest.data_readiness.calendar import load_calendar
from signal_engine.research.intake.fingerprints import canonical_json
from signal_engine.timeutils import IST
from utils.logging import get_logger

logger = get_logger(__name__)

OBJECT_COLS = ["symbol", "exchange", "interval", "timestamp", "session", "open", "high", "low",
               "close", "volume", "n_minutes"]
SOURCE = "AngelOne 1m (legacy Historify store, frozen, plus targeted AngelOne recovery)"
PRICE_BASIS = ("canonical NSE: each session converted from the vendor basis to NSE raw prices by "
               "a per-session factor, then back-adjusted with the factors of the daily version")
LIMITATIONS = ("legacy row-level provenance unavailable (sample-verified); dividends not adjusted; "
               "renames not joined; universe is today's F&O list (survivorship); minute bars "
               "exclude pre-open auction volume; NSE daily extremes may lie outside the minute "
               "range (2023-06 to 2024-03)")
QUALITY_COLS = ["symbol", "session", "cls", "origin", "price_status", "final_defect",
                "treatment", "price_valid", "volume_valid", "final_bar_valid",
                "final_bar_convention", "method", "k", "vol_c", "vol_ratio", "missing_minutes",
                "open_ok", "contained", "timestamp_aligned", "timestamp_basis"]
_TEXT_COLS = ("cls", "origin", "price_status", "final_defect", "treatment",
              "final_bar_convention", "method", "timestamp_basis")


def _sha_bytes(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def content(build: Path) -> tuple[list[tuple[dict, bytes]], dict[str, bytes]]:
    """(5m object rows with their parquet bytes, extra objects by name). Pure: writes nothing."""
    objects = []
    for path in sorted((build / "five").glob("*/*.parquet")):
        df = pd.read_parquet(path).assign(exchange="NSE", interval="5m")[OBJECT_COLS]
        blob = sn._parquet_bytes(df)
        objects.append(({"interval": "5m", "symbol": path.parent.name, "period": int(path.stem),
                         "rows": len(df), "first_timestamp": int(df.timestamp.min()),
                         "last_timestamp": int(df.timestamp.max())}, blob))
    ledger = pd.read_parquet(build / "session_ledger.parquet")
    quality = ledger[QUALITY_COLS].astype(dict.fromkeys(_TEXT_COLS, str))
    extras = {
        "quality_table": sn._parquet_bytes(quality),
        "data_quality_policy": ic.POLICY_PATH.read_bytes(),
        "irrecoverability": (build / "irrecoverability.json").read_bytes(),
        "provenance": sn._parquet_bytes(pd.read_parquet(build / "provenance.parquet")),
        "recovery_ledger": sn._parquet_bytes(ir.read_ledger(build).astype({"sessions": str})),
        "requests": sn._parquet_bytes(pd.read_parquet(build / "requests.parquet")),
        "report": (build / "report.json").read_bytes(),
    }
    if (build / "assessment.yaml").exists():
        extras["assessment"] = (build / "assessment.yaml").read_bytes()
    return objects, extras


def contract(build: Path, report: dict, daily: dict) -> dict:
    plan = json.loads((build / "plan.json").read_text())
    policy = json.loads((build / "policy.json").read_text())
    return {
        "frequency": "5m",
        "source": {"primary": "AngelOne historical API (ONE_MINUTE)", "legacy_store": True,
                   "legacy_snapshot_root_hash": report["snapshot_root_hash"],
                   "legacy_row_level_provenance": "unavailable",
                   "sample_verification": {k: report["provenance"][k] for k in (
                       "sampled_sessions", "exact_share", "wilson_lower_95", "coverage",
                       "passed")}},
        "provenance_statement": report["provenance"]["statement"],
        "recovery": {"source": "AngelOne", "requests": int(plan["requests_total"]),
                     "ledger_lines": len(ir.read_ledger(build))},
        "history_start": "2017-01-01", "history_end": "2026-10-01",
        "session_template_sha256": report["template_sha256"],
        "bar_alignment": "fixed 5-minute bins anchored at 09:15 IST, labelled with the bin start; "
                         "absent bins are not filled",
        "closing_auction": "from 2026-08-03 the auction print forms its own 15:25 bar",
        "price_basis": PRICE_BASIS, "daily_version": daily["dataset_version"],
        "daily_root_hash": daily["root_hash"],
        "conversion_spec_version": ic.CONVERSION_SPEC_VERSION,
        "conversion": {"agree": ic.AGREE, "k_max": ic.K_MAX,
                       "fallback_pairs": [f"{a}+{b}" for a, b in ic.FALLBACK_PAIRS],
                       "contain_rel": ic.CONTAIN_REL, "contain_abs": ic.CONTAIN_ABS,
                       "fallback_reach": ic.FALLBACK_REACH, "volume_band": [ic.VOL_LO, ic.VOL_HI],
                       "open_tol_reported": ic.OPEN_TOL},
        "missing_minute_threshold": {"N": policy["N"], "method": policy["method"]},
        "missing_session_policy": "an expected session with no bars after recovery is MISSING",
        "exclusion_policy": "per session and field family (quality table); Muhurat sessions out "
                            "of scope",
        "field_families": list(ic.FIELD_FAMILIES),
        "final_bar_convention": "per session: official_close | last_trade | both | none "
                                "(Part B2); before 2023-06 the final bar usually carries the "
                                "official close, which is not a traded price",
        "data_policy_version": ic.policy_version(),
        "usable_range_removals": report["usable_range_removals"],
        "readiness": report["readiness_state"],
    }


def _write_json(path: Path, doc: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def publish(build: Path, *, version_id: str, daily_version: str, cause: str,
            root: Path = sn.DEFAULT_ROOT, git_dir: Path = sn.DEFAULT_GIT_DIR,
            dry_run: bool = False) -> dict:
    report = json.loads((build / "report.json").read_text())
    failed = [k for k, v in {**report["criteria"], **report.get("metadata", {})}.items() if not v]
    if report["readiness_state"] == st.NOT_READY or failed:
        raise sn.SnapshotError(f"build is {report['readiness_state']}; failed criteria {failed}")
    if (root / "versions" / f"{version_id}.json").exists() or (
            git_dir / f"{version_id}.json").exists():
        raise sn.SnapshotError(f"version {version_id} exists; a published version is never "
                               "rewritten")
    daily = sn.resolve_version(daily_version, root=root, git_dir=git_dir)
    objects, extras = content(build)
    index = pd.DataFrame([{**row, "object_sha256": _sha_bytes(blob)} for row, blob in objects])
    index = index.sort_values(["interval", "symbol", "period"]).reset_index(drop=True)
    index_blob = sn._parquet_bytes(index)
    extra_shas = {name: _sha_bytes(blob) for name, blob in extras.items()}
    content_hash = hashlib.sha256(canonical_json(
        {"index": _sha_bytes(index_blob), "extra": extra_shas}).encode()).hexdigest()
    if dry_run:
        return {"dry_run": True, "content_hash": content_hash, "objects": len(index),
                "rows": int(index.rows.sum()), "readiness_state": report["readiness_state"]}
    sess = report["sessions"]
    body = {
        "dataset_version": version_id, "parent": None, "status": "published", "cause": cause,
        "created_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
        "source": SOURCE, "as_of": "2026-10-01", "frequency": "5m",
        "price_basis": PRICE_BASIS, "known_limitations": LIMITATIONS,
        "calendar_version": load_calendar().version,
        "corporate_action_version": daily["corporate_action_version"],
        "contract": contract(build, report, daily),
        "coverage": {"start": "2017-01-01", "end": "2026-10-01",
                     "symbols": int(index.symbol.nunique()), "rows": int(index.rows.sum()),
                     "sessions_price_valid": sess["valid"]["price"],
                     "sessions_requested": sess["requested"]},
        "readiness_state": report["readiness_state"],
        "warnings": [*(f"{f}: {sess['requested'] - n} requested sessions invalid"
                       for f, n in sess["valid"].items()),
                     f"{sess['out_of_scope_muhurat']} Muhurat sessions out of scope",
                     *(f"alert {a['id']} rate {a['rate']}" for a in report["alerts"])],
        "excluded_symbols": [],
        "audit_sha256": extra_shas["report"], "index_sha256": _sha_bytes(index_blob),
        "events_sha256": daily["events_sha256"], "extra_objects": extra_shas,
        "content_hash": content_hash, "objects": len(index),
        "data_policy_version": ic.policy_version(),
        "field_families": list(ic.FIELD_FAMILIES),
        "usable_range_removals": report["usable_range_removals"],
        "out_of_scope_sessions": sorted(str(d.date()) for d in ip.muhurat_dates()),
        "fingerprint_algo": sn.FINGERPRINT_ALGO, "staging_build": build.name,
    }
    for _, blob in objects:
        sn._put_object(root, blob)
    sn._put_object(root, index_blob)
    for blob in extras.values():
        sn._put_object(root, blob)
    root_hash = sn._root_hash(body)
    path = root / "versions" / f"{version_id}.json"
    _write_json(path, {**body, "root_hash": root_hash, "dataset_fingerprint": root_hash})
    os.chmod(path, sn._READ_ONLY)
    git_dir.mkdir(parents=True, exist_ok=True)
    _write_json(git_dir / f"{version_id}.json", {
        "dataset_version": version_id, "root_hash": root_hash, "parent": None,
        "status": "published", "cause": cause, "created_ist": body["created_ist"],
        "frequency": "5m", "coverage": body["coverage"],
        "readiness_state": body["readiness_state"], "content_hash": content_hash,
        "data_policy_version": body["data_policy_version"],
        "excluded_symbols": []})
    logger.info(f"published {version_id} root {root_hash[:12]} objects {len(index)}")
    return {"dry_run": False, "root_hash": root_hash, "content_hash": content_hash,
            "objects": len(index)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="signal_engine.backtest.data_readiness.intraday_publish")
    ap.add_argument("--build", required=True, type=Path)
    ap.add_argument("--daily-version", required=True)
    ap.add_argument("--version-id", required=True)
    ap.add_argument("--cause", default="first intraday 5m dataset (IDEA-029 data dependency)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    print(publish(args.build, version_id=args.version_id, daily_version=args.daily_version,
                  cause=args.cause, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
