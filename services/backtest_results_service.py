"""Backtest results service.

Read-only view over `signal_engine/research`: every idea in the registry, and the dated,
immutable backtest run records stored beside it (`ideas/<idea>/runs/<run_id>.json`).
Serves the /backtest-results page: the leaderboard, one idea's run history, and a
side-by-side comparison of two runs of the same idea.

All functions return `(success, response, status_code)` like the other services. Nothing
here writes: run records are produced by `signal_engine.backtest.record_run`.
"""

import csv
import io
import itertools
import json
import math
import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from signal_engine.research.build_registry import RegistryError, load_ideas
from signal_engine.research.intake.spec import SpecError, load_spec
from signal_engine.research.intake.versions import VERSIONS_FILE, VersionError, parse_versions
from signal_engine.timeutils import IST
from utils.logging import get_logger

logger = get_logger(__name__)

#: Directory holding `research/` (the registry root that `load_ideas` expects).
DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "signal_engine"

SPARKLINE_POINTS = 60

_IDEA_ID = re.compile(r"^IDEA-\d{3}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
#: Run ids of the per-run detail endpoint: the dated, versioned folder name.
_DETAIL_RUN_ID = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9-]+-v\d+\Z")

#: Differences that make two runs a different experiment, not a tuning step. Comparing
#: them is allowed but must be flagged: the deltas then mix a code change with a data or
#: cost change.
_LIKE_FOR_LIKE = (
    ("universe", ("data", "universe")),
    ("bar size", ("data", "bar_size")),
    ("data source", ("data", "source")),
    ("test period", ("period",)),
    ("cost model", ("costs", "model")),
    ("round-trip cost", ("costs", "round_trip_bps")),
)

_MAX_CACHED_ROOTS = 8
_cache_lock = threading.Lock()
_cache: dict[str, tuple[tuple, list[dict[str, Any]]]] = {}


def _signature(root: Path) -> tuple:
    """Changes whenever an idea or a run file is added, edited or removed."""
    ideas = root / "research" / "ideas"
    files = [
        *ideas.glob("*/idea.md"),
        *ideas.glob("*/spec.yaml"),
        *ideas.glob("*/runs/*.json"),
    ]
    return tuple(sorted((str(p), p.stat().st_mtime_ns) for p in files))


def _load(root: Path) -> list[dict[str, Any]]:
    """Validated ideas with their runs, cached until a file under research/ideas changes.

    Raises FileNotFoundError when there is no research directory and RegistryError when
    an idea or run is invalid. Callers must treat the returned data as read-only.
    """
    if not (root / "research" / "ideas").is_dir():
        raise FileNotFoundError(str(root / "research" / "ideas"))
    key, signature = str(root), _signature(root)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] == signature:
            return hit[1]
    ideas = load_ideas(root)
    with _cache_lock:
        if len(_cache) >= _MAX_CACHED_ROOTS and key not in _cache:
            _cache.clear()
        _cache[key] = (signature, ideas)
    return ideas


def _error(message: str, status: int) -> tuple[bool, dict[str, Any], int]:
    return False, {"status": "error", "message": message}, status


def _sparkline(run: dict[str, Any]) -> list[float]:
    values = [p[1] for p in (run.get("series") or {}).get("equity_r", [])]
    if len(values) <= SPARKLINE_POINTS:
        return values
    step = (len(values) - 1) / (SPARKLINE_POINTS - 1)
    return [values[round(i * step)] for i in range(SPARKLINE_POINTS)]


def _parse_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _coverage(run: dict[str, Any], today: date) -> dict[str, Any]:
    """How far back the run tested, and how stale the data behind it is."""
    start = _parse_day((run.get("period") or {}).get("start"))
    end = _parse_day((run.get("period") or {}).get("end"))
    as_of = _parse_day((run.get("data") or {}).get("as_of"))
    return {
        "years": round((end - start).days / 365.25, 1) if start and end else None,
        "data_age_days": max((today - as_of).days, 0) if as_of else None,
    }


def _net_at_cap(run: dict[str, Any], slots: int) -> float | None:
    for row in run.get("position_caps") or []:
        if row.get("max_positions") == slots:
            return row.get("net_bps")
    return None


def _run_summary(run: dict[str, Any], today: date) -> dict[str, Any]:
    oos = run.get("out_of_sample") or {}
    return {
        "run_id": run["run_id"],
        "date": run["date"],
        "code_version": run["code_version"],
        "parent_run_id": run.get("parent_run_id"),
        "change_note": run.get("change_note", ""),
        "metrics": dict(run["metrics"]),
        "n_trials": run.get("n_trials", 1),
        "promoted": run.get("promoted", False),
        "oos_net_bps": oos.get("net_bps"),
        "net_bps_cap10": _net_at_cap(run, 10),
        "baseline_excess_bps": (run.get("baseline") or {}).get("excess_net_bps"),
        "baseline_excess_t": (run.get("baseline") or {}).get("excess_t_stat"),
        "period": dict(run["period"]),
        "data": dict(run["data"]),
        **_coverage(run, today),
        "sparkline": _sparkline(run),
    }


_IDEA_FIELDS = (
    "id",
    "title",
    "status",
    "verdict",
    "horizon",
    "summary",
    "result",
    "revisit_when",
    "last_reviewed",
    "source",
    "rejection_basis",
    "block_reason",
    "stage",
    "research_state",
    "strategy_category",
    "strategy_tags",
    "classification",
)


def _idea_card(idea: dict[str, Any], today: date) -> dict[str, Any]:
    runs = idea.get("runs") or []
    return {
        **{k: idea.get(k) for k in _IDEA_FIELDS},
        "run_count": len(runs),
        "latest_run": _run_summary(runs[-1], today) if runs else None,
    }


def _today() -> date:
    return datetime.now(IST).date()


def _count(ideas: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for idea in ideas:
        counts[idea[field]] = counts.get(idea[field], 0) + 1
    return dict(sorted(counts.items()))


def list_results(
    root: Path | None = None, today: date | None = None
) -> tuple[bool, dict[str, Any], int]:
    """Every idea with its run count and latest run, for the leaderboard."""
    root = root or DEFAULT_ROOT
    today = today or _today()
    try:
        ideas = _load(root)
    except FileNotFoundError:
        return True, {"status": "success", "available": False, "ideas": [], "summary": {}}, 200
    except RegistryError:
        logger.exception("Backtest registry is invalid")
        return _error("Research registry is invalid; run build_registry for details", 500)
    return (
        True,
        {
            "status": "success",
            "available": True,
            "ideas": [_idea_card(i, today) for i in ideas],
            "summary": {
                "total_ideas": len(ideas),
                "total_runs": sum(len(i.get("runs") or []) for i in ideas),
                "ideas_with_runs": sum(1 for i in ideas if i.get("runs")),
                "by_status": _count(ideas, "status"),
                "by_verdict": _count(ideas, "verdict"),
            },
        },
        200,
    )


def _find_idea(idea_id: str, root: Path) -> tuple[dict[str, Any] | None, tuple | None]:
    """The idea, or None plus a ready-made error result."""
    if not _IDEA_ID.match(idea_id or ""):
        return None, _error("Invalid idea id", 400)
    try:
        ideas = _load(root)
    except FileNotFoundError:
        return None, _error("Research registry not available", 404)
    except RegistryError:
        logger.exception("Backtest registry is invalid")
        return None, _error("Research registry is invalid; run build_registry for details", 500)
    idea = next((i for i in ideas if i["id"] == idea_id), None)
    if idea is None:
        return None, _error(f"Idea {idea_id} not found", 404)
    return idea, None


_FRONTMATTER_BLOCK = re.compile(r"\A---\s*\n.*?\n---\s*\n?", re.DOTALL)

#: Run-record sidecars, in the order they are looked for, served only when present.
_TRADES_SIDECARS = (
    ("trades.csv.gz", "application/gzip"),
    ("trades.csv", "text/csv"),
)


def _idea_folder(idea: dict[str, Any], root: Path) -> Path:
    return root / "research" / "ideas" / idea["folder"]


def _trades_sidecar(folder: Path, run_id: str) -> tuple[Path, str] | None:
    """The per-trade file stored beside a run record, when one exists.

    `run_id` has already been matched against the idea's own runs, so the name cannot
    contain a path separator.
    """
    for suffix, mimetype in _TRADES_SIDECARS:
        path = folder / "runs" / f"{run_id}.{suffix}"
        if path.is_file():
            return path, mimetype
    return None


def _research_notes(folder: Path) -> str:
    """The body of idea.md: research notes and the decision log, without the front matter."""
    try:
        text = (folder / "idea.md").read_text(encoding="utf-8")
    except OSError:
        logger.exception("Could not read idea.md in %s", folder)
        return ""
    return _FRONTMATTER_BLOCK.sub("", text, count=1).strip()


def _load_spec(folder: Path) -> dict[str, Any] | None:
    """The canonical strategy definition, or None when the idea has no spec.yaml."""
    try:
        data = load_spec(folder)
    except SpecError:
        logger.exception("Unreadable strategy spec in %s", folder)
        return None
    return data if isinstance(data, dict) else None


_VERSION_KEYS = ("version", "strategy_fingerprint", "parent", "created_ist", "note")
_POINTER_KEYS = ("current_version", "promoted_version", "promoted_run_id")


def _load_versions(folder: Path, base: Path) -> tuple[list[dict[str, Any]] | None, str | None]:
    """The idea's strategy versions and a path-free error sentence.

    Returns `(versions, None)` when healthy, `(None, None)` when versions.yaml is absent (the
    idea was never frozen) and `(None, message)` when it exists but cannot be used. The file
    is opened once, with the same hardening as other run files (inside `base`, no symlink,
    size cap), and the text that was size-checked is the text that is parsed.
    """
    path = folder / VERSIONS_FILE
    try:
        if not path.exists() and not path.is_symlink():
            return None, None
        text = _read_text(path, base)
        return [{k: getattr(v, k) for k in _VERSION_KEYS} for v in parse_versions(text)], None
    except VersionError:
        logger.exception("Invalid versions file %s", path)
        return None, "versions.yaml is invalid"
    except ValueError as exc:
        logger.exception("Could not read %s", path)
        if "exceeds" in str(exc):
            return None, "versions.yaml is larger than the allowed size"
        if isinstance(exc, UnicodeDecodeError):
            return None, "versions.yaml is invalid"
        return None, "versions.yaml is not readable"
    except _READ_ERRORS:
        logger.exception("Could not read %s", path)
        return None, "versions.yaml is not readable"


#: Mirrors signal_engine.backtest.tuning (TUNING_DIR, RECORD), not imported because it pulls
#: the backtest harness into the web process.
_TUNING_DIR, _TUNING_RECORD = "tuning", "tuning.json"
_TUNING_KEYS = (
    "tuning_id",
    "created_ist",
    "objective",
    "grid",
    "min_trades",
    "in_sample_end",
    "held_out_from",
    "combinations",
    "selected_is_not_the_single_best",
)
_TUNED_ROW_KEYS = (
    "values",
    "trades",
    "net_r",
    "region_worst_net_r",
    "region_median_net_r",
    "region_size",
)


def _region_possible(grid: Any, values: Any) -> int | None:
    """How many grid points lie within one step of `values` in every parameter (itself included)."""
    if not isinstance(grid, dict) or not isinstance(values, dict) or not grid:
        return None
    total = 1
    for name, options in grid.items():
        if not isinstance(options, list) or values.get(name) not in options:
            return None
        i = options.index(values[name])
        total *= len(options[max(0, i - 1) : i + 2])
    return total


def _tuned_row(row: Any, grid: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    out = {k: _jsonable(row.get(k)) for k in _TUNED_ROW_KEYS}
    out["region_possible"] = _region_possible(grid, row.get("values"))
    return out


def _tuning_summary(path: Path, base: Path) -> dict[str, Any]:
    """The headline of one tuning record: split, selection, its region and the PBO diagnostic."""
    record = _read_json(path, base)
    if not isinstance(record, dict):
        return {"tuning_id": path.parent.name, "error": "tuning record is not readable"}
    out = {k: _jsonable(record.get(k)) for k in _TUNING_KEYS}
    out["tuning_id"] = path.parent.name
    results = record.get("results") if isinstance(record.get("results"), list) else []
    out["usable"] = sum(1 for r in results if isinstance(r, dict) and r.get("usable") is True)
    out["selected"] = _tuned_row(record.get("selected"), record.get("grid"))
    out["best_single"] = _tuned_row(record.get("best_single"), record.get("grid"))
    pbo = record.get("pbo") if isinstance(record.get("pbo"), dict) else {}
    out["pbo"] = {"value": _jsonable(pbo.get("value")), "role": "diagnostic"}
    # A record without the block predates portfolio caps in tuning: never shown as constrained.
    cons = record.get("constraints") if isinstance(record.get("constraints"), dict) else {}
    out["constraints"] = {
        "fully_constrained": cons.get("fully_constrained") is True,
        "basis": _str_or_none(cons.get("basis")),
        "unconstrained": len(cons.get("unconstrained") or []),
    }
    return out


def _load_tuning(folder: Path, base: Path) -> list[dict[str, Any]]:
    """Every tuning record of the idea, oldest first. An empty list means no tuning ran."""
    paths = sorted((folder / _TUNING_DIR).glob(f"*/{_TUNING_RECORD}"))
    return [_tuning_summary(p, base) for p in paths]


def _version_pointers(idea: dict[str, Any]) -> dict[str, str | None]:
    return {k: idea[k] if isinstance(idea.get(k), str) else None for k in _POINTER_KEYS}


def get_idea_results(
    idea_id: str, root: Path | None = None, today: date | None = None
) -> tuple[bool, dict[str, Any], int]:
    """One idea and its full run history, oldest first, with chart series.

    Also carries the canonical strategy definition (`spec`, from spec.yaml), kept apart
    from the research notes (`research_notes`, the idea.md body), and the computed
    `lineage` of the runs. `spec` is null for an idea with no specification on file.
    """
    root = root or DEFAULT_ROOT
    idea, failure = _find_idea(idea_id, root)
    if failure:
        return failure
    today = today or _today()
    folder = _idea_folder(idea, root)
    card = {k: idea.get(k) for k in _IDEA_FIELDS}
    runs = [
        {
            **run,
            **_coverage(run, today),
            "trades_export": _trades_sidecar(folder, run["run_id"]) is not None,
        }
        for run in idea.get("runs") or []
    ]
    versions, versions_error = _load_versions(folder, root / "research" / "ideas")
    return (
        True,
        {
            "status": "success",
            "idea": card,
            "runs": runs,
            "lineage": idea.get("lineage"),
            "spec": _load_spec(folder),
            "research_notes": _research_notes(folder),
            "versions": versions,
            "versions_error": versions_error,
            "version_pointers": _version_pointers(idea),
            "tuning": _load_tuning(folder, root / "research" / "ideas"),
        },
        200,
    )


EXPORT_FORMATS = ("json", "metrics", "series", "trades")

#: Record sections holding a flat mapping of metric name to value. Mirrors
#: signal_engine.backtest.export, which is not imported here because it pulls the whole
#: backtest harness (and scipy) into the web process; a test keeps the two identical.
_METRIC_SECTIONS = ("metrics", "in_sample", "out_of_sample")


def metrics_rows(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a record's metric blocks into `section, metric, value` rows."""
    rows: list[dict[str, Any]] = []
    for section in _METRIC_SECTIONS:
        block = record.get(section)
        if isinstance(block, dict):
            rows.extend({"section": section, "metric": k, "value": v} for k, v in block.items())
    for cap in record.get("position_caps") or []:
        section = f"position_caps.{cap['max_positions']}"
        rows.extend(
            {"section": section, "metric": k, "value": v}
            for k, v in cap.items()
            if k != "max_positions"
        )
    return rows


def series_rows(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Equity and drawdown curves joined on date, in calendar order."""
    series = record.get("series") or {}
    equity = dict(series.get("equity_r") or [])
    drawdown = dict(series.get("drawdown_r") or [])
    return [
        {"date": d, "equity_r": equity.get(d), "drawdown_r": drawdown.get(d)}
        for d in sorted(equity.keys() | drawdown.keys())
    ]


def _csv_cell(value: Any) -> Any:
    """Stop a text cell being read as a spreadsheet formula."""
    if isinstance(value, str) and value[:1] in ("=", "+", "@", "\t", "\r"):
        return "'" + value
    return value


def _to_csv(rows: list[dict[str, Any]], columns: tuple[str, ...]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_cell(row.get(c)) for c in columns])
    return buffer.getvalue().encode("utf-8")


@dataclass(frozen=True)
class ExportFile:
    """A download: `path` for a file served as stored, `body` for one built on request."""

    filename: str
    mimetype: str
    path: Path | None = None
    body: bytes | None = None


def export_run(
    idea_id: str, run_id: str, fmt: str, root: Path | None = None
) -> tuple[bool, ExportFile | dict[str, Any], int]:
    """One run as a download: the canonical JSON record, metric or series CSV, or trades.

    The JSON record is the canonical result and is served exactly as stored. Trades are
    served only when a sidecar was stored beside the record; records never hold them.
    """
    if not _RUN_ID.match(run_id or ""):
        return _error("Invalid run id", 400)
    if fmt not in EXPORT_FORMATS:
        return _error(f"Unknown export format; use one of: {', '.join(EXPORT_FORMATS)}", 400)
    root = root or DEFAULT_ROOT
    idea, failure = _find_idea(idea_id, root)
    if failure:
        return failure
    run = next((r for r in idea.get("runs") or [] if r["run_id"] == run_id), None)
    if run is None:
        return _error(f"Run not found: {run_id}", 404)
    folder = _idea_folder(idea, root)
    if fmt == "json":
        path = folder / "runs" / f"{run_id}.json"
        if not path.is_file():
            return _error(f"Run not found: {run_id}", 404)
        return True, ExportFile(f"{run_id}.json", "application/json", path=path), 200
    if fmt == "metrics":
        body = _to_csv(metrics_rows(run), ("section", "metric", "value"))
        return True, ExportFile(f"{run_id}.metrics.csv", "text/csv", body=body), 200
    if fmt == "series":
        body = _to_csv(series_rows(run), ("date", "equity_r", "drawdown_r"))
        return True, ExportFile(f"{run_id}.series.csv", "text/csv", body=body), 200
    sidecar = _trades_sidecar(folder, run_id)
    if sidecar is None:
        return _error("No per-trade export is stored for this run", 404)
    path, mimetype = sidecar
    return True, ExportFile(path.name, mimetype, path=path), 200


#: Largest run-folder file read into memory; anything bigger is treated as unreadable.
_MAX_FILE_BYTES = 2 * 1024 * 1024
#: Most directory entries inspected when looking for the newest reproduction attempt.
_MAX_SCAN_ENTRIES = 1000
_READ_ERRORS = (OSError, ValueError, RecursionError, MemoryError, yaml.YAMLError)


def _read_text(path: Path, base: Path) -> str:
    """A file's text, refusing symlinks, paths outside `base` and files over the size cap."""
    if path.is_symlink():
        raise OSError(f"refusing symlink {path}")
    path.resolve().relative_to(base.resolve())
    with path.open("rb") as handle:
        raw = handle.read(_MAX_FILE_BYTES + 1)
    if len(raw) > _MAX_FILE_BYTES:
        raise ValueError(f"file exceeds {_MAX_FILE_BYTES} bytes: {path}")
    return raw.decode("utf-8")


def _read_json(path: Path, base: Path) -> Any:
    """A JSON file's content, or None when it is missing or unreadable (logged)."""
    try:
        return json.loads(_read_text(path, base))
    except _READ_ERRORS:
        logger.exception("Could not read %s", path)
        return None


def _jsonable(value: Any) -> Any:
    """`value` with string keys and ISO date strings, so it always serialises to JSON."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _read_yaml(path: Path, base: Path) -> dict[str, Any] | None:
    try:
        data = yaml.safe_load(_read_text(path, base))
        return _jsonable(data) if isinstance(data, dict) else None
    except _READ_ERRORS:
        logger.exception("Could not read %s", path)
        return None


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _str_list(value: Any) -> list[str]:
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    return []


def _checklist(value: Any) -> dict[str, bool]:
    return {k: v for k, v in _dict(value).items() if isinstance(k, str) and isinstance(v, bool)}


def _period(value: Any) -> dict[str, str] | None:
    data = _dict(value)
    start, end = data.get("start"), data.get("end")
    if isinstance(start, str) and isinstance(end, str):
        return {"start": start, "end": end}
    return None


def _profile(value: Any) -> dict[str, str] | None:
    data = _dict(value)
    name, sha = data.get("name"), data.get("sha256")
    if isinstance(name, str) and isinstance(sha, str):
        return {"name": name, "sha256": sha}
    return None


def _strategy_window(value: Any) -> str | dict[str, Any] | None:
    if isinstance(value, str):
        return value
    return _jsonable(value) if isinstance(value, dict) else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _resolve_run_folder(
    record: dict[str, Any], runs_dir: Path, run_id: str, ideas_root: Path
) -> Path | None:
    """The run folder named by artifact_dir, or None when it is not exactly this run's own.

    The runs directory must be a real (non-symlink) directory under `ideas_root`, and the
    folder must be a direct, non-symlink child of it named `run_id`.
    """
    rel = record["artifact_dir"]
    if not isinstance(rel, str) or not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
        return None
    try:
        if runs_dir.is_symlink():
            return None
        base = runs_dir.resolve()
        base.relative_to(ideas_root.resolve())
        candidate = runs_dir / rel
        if candidate.is_symlink():
            return None
        folder = candidate.resolve()
    except (OSError, ValueError):
        return None
    if folder.name != run_id or folder.parent != base or not folder.is_dir():
        return None
    return folder


#: Run records with an artifact folder: schema 3, and schema 4 (published dataset version,
#: preflight and run status; signal_engine plan Phase 4). Schema 2 records have no folder.
_ARTIFACT_SCHEMAS = (3, 4)


def _empty_run_detail(run_id: str, schema: Any, captured: bool) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "schema": schema,
        "captured": captured,
        "strategy_version": None,
        "parent_version": None,
        "branch_label": None,
        "change_classes": [],
        "verdict": None,
        "promotion": None,
        "next_action": None,
        "spec": None,
        "fingerprints": None,
        "scope": None,
        "reproducibility": None,
        "assumptions": None,
        "missing_capabilities": [],
        "manifest_sha256": None,
        "created_ist": None,
        "validity": None,
        "data_quality": None,
        "trials": None,
        "ledgers": None,
        "cost_components": None,
        "has_trades": False,
        "has_ledgers": False,
    }


def _resolved_value(execution: dict[str, Any], field: str) -> Any:
    """A resolved execution value; config.json keys them with an `execution.` prefix."""
    return _dict(_dict(execution.get("resolved")).get(f"execution.{field}")).get("value")


def _build_scope(
    manifest: dict[str, Any] | None,
    execution: dict[str, Any] | None,
    strategy: dict[str, Any] | None,
    spec: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if manifest is None:
        return None
    m, ex, st, sp = manifest, _dict(execution), _dict(strategy), _dict(spec)
    session, dataset = _dict(m.get("session")), _dict(m.get("dataset"))
    profile, sizing, portfolio = (
        _dict(m.get("deployment_profile")),
        _dict(sp.get("sizing")),
        _dict(sp.get("portfolio")),
    )
    return {
        "market": _dict(ex.get("profile")).get("market"),
        "universe": _dict(m.get("universe")).get("definition"),
        "symbol_count": dataset.get("symbol_count"),
        "style": st.get("style"),
        "direction": st.get("direction"),
        "timeframe": st.get("timeframe"),
        "product": st.get("product"),
        "period": _period(m.get("period")),
        "strategy_window": _strategy_window(session.get("strategy_window")),
        "exchange_session": session.get("exchange"),
        "broker_cutoff": session.get("broker_cutoff"),
        "force_flat": session.get("force_flat"),
        "capital_per_trade": sizing.get("capital_per_trade"),
        "risk_per_trade_pct": sizing.get("risk_per_trade_pct"),
        "portfolio_mode": portfolio.get("mode"),
        "portfolio_scope": portfolio.get("scope"),
        "slippage_bps_per_side": _resolved_value(ex, "slippage_bps_per_side"),
        "cost_bps_round_trip": _resolved_value(ex, "cost_bps_round_trip"),
        "profile": _profile(profile),
        "dataset": {
            "id": _str_or_none(dataset.get("id")),
            "source": _str_or_none(dataset.get("source")),
            "interval": _str_or_none(dataset.get("interval")),
            "data_as_of": _str_or_none(dataset.get("data_as_of")),
            "fingerprint": _str_or_none(dataset.get("fingerprint")),
        },
    }


def _last_attempt(runs_dir: Path, run_id: str) -> dict[str, Any] | None:
    """The reproduction attempt with the greatest file name among the first 1000 entries.

    Scans at most `_MAX_SCAN_ENTRIES` directory entries, then reads that one file.
    """
    folder = runs_dir / f"{run_id}.reproductions"
    try:
        if folder.is_symlink():
            return None
        with os.scandir(folder) as entries:
            names = [
                e.name
                for e in itertools.islice(entries, _MAX_SCAN_ENTRIES)
                if e.name.endswith(".json")
            ]
    except FileNotFoundError:
        return None
    except OSError:
        logger.exception("Could not list %s", folder)
        return None
    if not names:
        return None
    data = _read_json(folder / max(names), folder)
    if not isinstance(data, dict):
        return None
    return {k: _jsonable(data.get(k)) for k in ("attempted_ist", "reproducible", "reasons")}


def _build_reproducibility(
    manifest: dict[str, Any] | None, runs_dir: Path, run_id: str
) -> dict[str, Any] | None:
    if manifest is None:
        return None
    engine = _dict(manifest.get("engine"))
    return {
        "reproducible": manifest.get("reproducible"),
        "code_dirty": engine.get("code_dirty"),
        "dirty_paths": _str_list(engine.get("dirty_paths")),
        "code_commit": engine.get("code_commit"),
        "engine_version": engine.get("engine_version"),
        "checklist": _checklist(manifest.get("reproducibility")),
        "last_attempt": _last_attempt(runs_dir, run_id),
    }


def _read_run_folder(
    folder: Path, runs_dir: Path, run_id: str, detail: dict[str, Any]
) -> dict[str, Any]:
    """Fill `detail` from the run folder; an unreadable file nulls only its own blocks."""
    manifest = _read_json(folder / "manifest.json", folder)
    manifest = manifest if isinstance(manifest, dict) else None
    report = _read_json(folder / "report" / "report.json", folder)
    report = report if isinstance(report, dict) else {}
    execution = _read_json(folder / "execution" / "config.json", folder)
    strategy = _read_json(folder / "strategy" / "strategy.json", folder)
    assumptions = _read_json(folder / "assumptions.json", folder)
    spec = _read_yaml(folder / "strategy" / "spec.yaml", folder)
    detail["verdict"] = _jsonable(report.get("verdict"))
    detail["promotion"] = _jsonable(report.get("promotion"))
    detail["next_action"] = _jsonable(report.get("next_action"))
    _fill_evidence(folder, report, detail)
    detail["spec"] = spec
    if isinstance(assumptions, dict):
        detail["assumptions"] = {
            "sealed_ist": assumptions.get("sealed_ist"),
            "entries": _jsonable(assumptions.get("entries") or []),
        }
    detail["scope"] = _build_scope(
        manifest,
        execution if isinstance(execution, dict) else None,
        strategy if isinstance(strategy, dict) else None,
        spec,
    )
    detail["reproducibility"] = _build_reproducibility(manifest, runs_dir, run_id)
    if manifest is not None:
        meta = _dict(manifest.get("strategy"))
        detail["strategy_version"] = meta.get("version")
        detail["parent_version"] = meta.get("parent_version")
        detail["branch_label"] = meta.get("branch_label")
        detail["change_classes"] = _str_list(manifest.get("change_classes"))
        detail["missing_capabilities"] = _str_list(manifest.get("missing_capabilities"))
        detail["fingerprints"] = _jsonable(manifest.get("fingerprints"))
        detail["created_ist"] = manifest.get("created_ist")
    return detail


def get_run_detail(
    idea_id: str, run_id: str, root: Path | None = None
) -> tuple[bool, dict[str, Any], int]:
    """Everything a schema 3 run folder says about one run, reshaped for the page.

    Read-model only: nothing is derived or verified here (the registry check verifies
    hashes; verdicts and promotion are read from report.json as the engine wrote them).
    """
    if not isinstance(run_id, str) or not _DETAIL_RUN_ID.match(run_id):
        return _error("invalid run id", 400)
    root = root or DEFAULT_ROOT
    idea, failure = _find_idea(idea_id, root)
    if failure:
        return failure
    record = next((r for r in idea.get("runs") or [] if r.get("run_id") == run_id), None)
    if record is None:
        return _error(f"Run not found: {run_id}", 404)
    if record.get("schema") not in _ARTIFACT_SCHEMAS or not record.get("artifact_dir"):
        detail = _empty_run_detail(run_id, record.get("schema", 2), False)
        return True, {"status": "success", "run": detail}, 200
    runs_dir = _idea_folder(idea, root) / "runs"
    folder = _resolve_run_folder(record, runs_dir, run_id, root / "research" / "ideas")
    if folder is None:
        return _error("invalid run folder", 400)
    detail = _empty_run_detail(run_id, 3, True)
    detail["manifest_sha256"] = record.get("manifest_sha256")
    _read_run_folder(folder, runs_dir, run_id, detail)
    return True, {"status": "success", "run": detail}, 200


# ---- Evidence read-model: validity, data quality, cost components, trades, lineage ------------

#: Largest Parquet evidence file (bytes) and most rows decoded for interactive browsing. The
#: single production worker is cooperative: a whole-file decode of a huge run would freeze it.
_MAX_PARQUET_BYTES = 64 * 1024 * 1024
_MAX_PARQUET_ROWS = 300_000
_MAX_SAFE_INT = 2**53 - 1
_TRADE_ID = re.compile(r"^T-\d{6,}\Z")
_TRADE_SUBSETS = ("all", "in_sample", "oos", "losers", "gap_exits")
_GAP_EXIT_REASONS = ("SL_GAP", "SL_GAP_ENTRY")
_MIN_LIMIT, _MAX_LIMIT = 1, 500
_COST_LABELS = (
    ("brokerage", "Brokerage"),
    ("stt", "STT"),
    ("exchange", "Exchange charges"),
    ("gst", "GST"),
    ("sebi", "SEBI fee"),
    ("stamp", "Stamp duty"),
    ("slippage", "Slippage"),
)
_COST_COLUMNS = tuple(f"cost_{key}_bps" for key, _ in _COST_LABELS)
_TRADE_COLUMNS = (
    "trade_id",
    "symbol",
    "side",
    "entry_time",
    "exit_time",
    "entry_price",
    "exit_price",
    "quantity",
    "exit_reason",
    "gross_r",
    "net_r",
    "gross_bps",
    "net_bps",
    "cost_bps",
    "signal_id",
    "order_id",
    "entry_fill_id",
    "exit_fill_id",
    "position_id",
    *_COST_COLUMNS,
    "modeled_cost_bps",
)
_LINEAGE_COLUMNS = ("signal_id", "order_id", "position_id")
_LEDGER_NAMES = ("signals", "orders", "fills")
_IST_NAME = "Asia/Kolkata"
_PARQUET_ERRORS = (OSError, ValueError, KeyError, TypeError, pa.ArrowException)
_SUBSET_NOTES = {
    "all": "every trade",
    "in_sample": "entry before oos_start; purge and embargo exclusions are not applied here",
    "oos": "entry on or after oos_start; purge and embargo exclusions are not applied here",
    "losers": "net_r below zero",
    "gap_exits": "exit reason SL_GAP or SL_GAP_ENTRY",
}


class _TooLarge(Exception):
    """A parquet file is over the interactive size or row cap."""


class _LedgerSchema(Exception):
    """A ledger lacks a column the lookup needs."""


class _Inconsistent(Exception):
    """An id that must be unique matched several rows."""


_CACHE_LOCK = threading.Lock()
_FRAME_CACHE: "OrderedDict[tuple, pd.DataFrame]" = OrderedDict()
_ORDER_CACHE: "OrderedDict[tuple, Any]" = OrderedDict()
_COST_CACHE: "OrderedDict[tuple, Any]" = OrderedDict()
_LOOKUP_CACHE: "OrderedDict[tuple, list]" = OrderedDict()
_FRAME_CACHE_MAX, _ORDER_CACHE_MAX, _COST_CACHE_MAX, _LOOKUP_CACHE_MAX = 2, 4, 16, 256


def _clear_evidence_caches() -> None:
    with _CACHE_LOCK:
        for cache in (_FRAME_CACHE, _ORDER_CACHE, _COST_CACHE, _LOOKUP_CACHE):
            cache.clear()


def _cache_get(cache: OrderedDict, key: tuple) -> Any:
    with _CACHE_LOCK:
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
    return None


def _cache_put(cache: OrderedDict, key: tuple, value: Any, limit: int) -> None:
    with _CACHE_LOCK:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)


def _file_key(path: Path) -> tuple:
    stat = path.stat()
    return (str(path), stat.st_mtime_ns, stat.st_size)


def _open_guarded(path: Path) -> pq.ParquetFile:
    """The file's ParquetFile, refusing one over the byte or row cap (no data decoded)."""
    if path.stat().st_size > _MAX_PARQUET_BYTES:
        raise _TooLarge(path.name)
    handle = pq.ParquetFile(path)
    if handle.metadata.num_rows > _MAX_PARQUET_ROWS:
        raise _TooLarge(path.name)
    return handle


def _evidence_file(folder: Path, name: str) -> Path | None:
    """`evidence/<name>.parquet` when it is a real file (not a symlink) inside `folder`."""
    path = folder / "evidence" / f"{name}.parquet"
    try:
        if path.is_symlink() or not path.is_file():
            return None
        path.resolve().relative_to(folder.resolve())
    except (OSError, ValueError):
        return None
    return path


def _optional_block(value: Any) -> dict[str, Any] | None:
    return _jsonable(value) if isinstance(value, dict) else None


def _mean(series: pd.Series) -> float | None:
    value = pd.to_numeric(series, errors="coerce").mean()
    return None if pd.isna(value) else float(value)


def _compute_cost_components(path: Path) -> dict[str, Any] | None:
    handle = _open_guarded(path)
    present = set(handle.schema_arrow.names)
    wanted = [c for c in _COST_COLUMNS if c in present]
    if not wanted or "modeled_cost_bps" not in present:
        return None
    columns = [*wanted, "modeled_cost_bps"] + (["cost_bps"] if "cost_bps" in present else [])
    frame = handle.read(columns=columns).to_pandas()
    components = [
        {"key": key, "label": label, "mean_bps": _mean(frame[f"cost_{key}_bps"])}
        for key, label in _COST_LABELS
        if f"cost_{key}_bps" in frame.columns
    ]
    return {
        "trades": int(len(frame)),
        "components": components,
        "modeled_cost_bps_mean": _mean(frame["modeled_cost_bps"]),
        "engine_cost_bps_mean": _mean(frame["cost_bps"]) if "cost_bps" in frame.columns else None,
    }


def _cost_components(folder: Path) -> dict[str, Any] | None:
    """Per-trade cost columns of evidence/trades.parquet, averaged. Nothing else is derived."""
    path = _evidence_file(folder, "trades")
    if path is None:
        return None
    try:
        key = ("cost", *_file_key(path))
        cached = _cache_get(_COST_CACHE, key)
        if cached is not None:
            return cached[0]
        result = _compute_cost_components(path)
    except _TooLarge:
        logger.error("Trades file too large for cost components: %s", path.name)
        return None
    except _PARQUET_ERRORS:
        logger.exception("Could not read cost components for a run")
        return None
    _cache_put(_COST_CACHE, key, (result,), _COST_CACHE_MAX)
    return result


def _fill_evidence(folder: Path, report: dict[str, Any], detail: dict[str, Any]) -> None:
    detail["validity"] = _optional_block(report.get("validity"))
    detail["trials"] = _optional_block(report.get("trials"))
    detail["ledgers"] = _optional_block(report.get("ledgers"))
    quality_path = folder / "data_quality.json"
    quality = _read_json(quality_path, folder) if quality_path.exists() else None
    if not isinstance(quality, dict):
        quality = report.get("data_quality")
    detail["data_quality"] = _optional_block(quality)
    detail["has_trades"] = _evidence_file(folder, "trades") is not None
    detail["has_ledgers"] = all(_evidence_file(folder, n) is not None for n in _LEDGER_NAMES)
    detail["cost_components"] = _cost_components(folder) if detail["has_trades"] else None


def _evidence_value(value: Any) -> Any:
    """One cell as plain JSON: NaN/inf/NaT become None, timestamps ISO in IST, big ints strings."""
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, pd.Timestamp):
        stamp = (
            value.tz_localize(_IST_NAME) if value.tzinfo is None else value.tz_convert(_IST_NAME)
        )
        return stamp.isoformat()
    if hasattr(value, "item") and not isinstance(value, str):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            return str(value)
        if isinstance(value, (bytes, bytearray)):
            return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if abs(value) <= _MAX_SAFE_INT else str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value if isinstance(value, str) else str(value)


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    cols = [str(c) for c in frame.columns]
    out = []
    for row in frame.astype(object).itertuples(index=False, name=None):
        out.append({c: _evidence_value(v) for c, v in zip(cols, row, strict=True)})
    return out


def _resolve_evidence_folder(
    idea_id: str, run_id: str, root: Path
) -> tuple[Path | None, dict[str, Any] | None, tuple | None]:
    """(run folder, record, None), or (None, record, error) with the ready-made error result."""
    if not isinstance(run_id, str) or not _DETAIL_RUN_ID.match(run_id):
        return None, None, _error("invalid run id", 400)
    idea, failure = _find_idea(idea_id, root)
    if failure:
        return None, None, failure
    record = next((r for r in idea.get("runs") or [] if r.get("run_id") == run_id), None)
    if record is None:
        return None, None, _error(f"Run not found: {run_id}", 404)
    if record.get("schema") not in _ARTIFACT_SCHEMAS or not record.get("artifact_dir"):
        return None, record, _error("no trades for this run", 404)
    runs_dir = _idea_folder(idea, root) / "runs"
    folder = _resolve_run_folder(record, runs_dir, run_id, root / "research" / "ideas")
    if folder is None:
        return None, record, _error("invalid run folder", 400)
    return folder, record, None


def _oos_start(record: dict[str, Any] | None) -> str | None:
    value = _dict(_dict(record).get("split")).get("oos_start")
    if isinstance(value, (date, datetime)):
        value = value.isoformat()[:10]
    try:
        return date.fromisoformat(value).isoformat() if isinstance(value, str) else None
    except ValueError:
        return None


def _entry_stamps(frame: pd.DataFrame) -> pd.Series:
    """Entry times as IST-aware timestamps (naive values are taken as IST); bad ones are NaT."""
    stamps = pd.to_datetime(frame["entry_time"], errors="coerce")
    if stamps.dt.tz is None:
        return stamps.dt.tz_localize(_IST_NAME)
    return stamps.dt.tz_convert(_IST_NAME)


def _filter_mask(
    frame: pd.DataFrame, subset: str, symbol: str | None, reason: str | None, oos_start: str | None
) -> pd.Series:
    mask = pd.Series(True, index=frame.index)
    if subset == "losers":
        mask &= pd.to_numeric(frame["net_r"], errors="coerce") < 0
    elif subset == "gap_exits":
        mask &= frame["exit_reason"].isin(_GAP_EXIT_REASONS)
    elif subset in ("in_sample", "oos"):
        if oos_start is None:
            return pd.Series(False, index=frame.index)
        stamps = _entry_stamps(frame)
        cut = pd.Timestamp(oos_start).tz_localize(_IST_NAME)
        mask &= stamps.notna() & ((stamps >= cut) if subset == "oos" else (stamps < cut))
    if symbol is not None:
        mask &= frame["symbol"] == symbol
    if reason is not None:
        mask &= frame["exit_reason"] == reason
    return mask


def _page_error(subset: Any, sort: Any, symbol: Any, reason: Any, limit: Any, offset: Any):
    if subset not in _TRADE_SUBSETS:
        return _error("invalid subset", 400)
    if not isinstance(sort, str):
        return _error("invalid sort column", 400)
    for value in (symbol, reason):
        if value is not None and not isinstance(value, str):
            return _error("invalid filter", 400)
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not (_MIN_LIMIT <= limit <= _MAX_LIMIT)
    ):
        return _error(f"limit must be between {_MIN_LIMIT} and {_MAX_LIMIT}", 400)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return _error("invalid offset", 400)
    return None


def _schema_error(
    names: set[str], subset: str, symbol: Any, reason: Any, sort: str, oos_start: str | None
):
    """A 400 when the file lacks a column the query needs, decided before any data is read."""
    if sort not in names or sort not in _TRADE_COLUMNS:
        return _error("invalid sort column", 400)
    needs = {"losers": "net_r", "gap_exits": "exit_reason", "in_sample": "entry_time"}
    needs["oos"] = "entry_time"
    required = []
    if subset in needs and not (subset in ("in_sample", "oos") and oos_start is None):
        required.append(needs[subset])
    if symbol is not None:
        required.append("symbol")
    if reason is not None:
        required.append("exit_reason")
    missing = [c for c in required if c not in names]
    if missing:
        return _error(f"this run's trades have no {missing[0]} column", 400)
    return None


def _load_frame(path: Path, handle: pq.ParquetFile) -> pd.DataFrame:
    key = ("frame", *_file_key(path))
    frame = _cache_get(_FRAME_CACHE, key)
    if frame is None:
        columns = [c for c in handle.schema_arrow.names if c in _TRADE_COLUMNS]
        frame = handle.read(columns=columns).to_pandas()
        _cache_put(_FRAME_CACHE, key, frame, _FRAME_CACHE_MAX)
    return frame


def _sorted_order(frame, path, subset, symbol, reason, sort, desc, oos_start):
    key = ("order", *_file_key(path), subset, symbol, reason, sort, desc, oos_start)
    order = _cache_get(_ORDER_CACHE, key)
    if order is None:
        mask = _filter_mask(frame, subset, symbol, reason, oos_start)
        picked = frame.loc[mask, [sort]]
        picked = picked.sort_values(sort, ascending=not desc, kind="mergesort", na_position="last")
        order = picked.index.to_numpy()
        _cache_put(_ORDER_CACHE, key, order, _ORDER_CACHE_MAX)
    return order


_TOO_LARGE_TRADES = "trades file is too large to browse interactively"
_TOO_LARGE_LEDGER = "ledger too large for interactive lineage"


def get_run_trades(
    idea_id: str,
    run_id: str,
    *,
    subset: str = "all",
    symbol: str | None = None,
    reason: str | None = None,
    sort: str = "entry_time",
    desc: bool = False,
    limit: int = 100,
    offset: int = 0,
    root: Path | None = None,
) -> tuple[bool, dict[str, Any], int]:
    """One page of a run's trades from evidence/trades.parquet, filtered and sorted.

    Read-only reshaping: the table's columns (lineage ids and cost components included) are
    returned as written, with NaN/inf as null and timestamps as ISO strings in IST.
    """
    folder, record, failure = _resolve_evidence_folder(idea_id, run_id, root or DEFAULT_ROOT)
    if failure:
        return failure
    bad = _page_error(subset, sort, symbol, reason, limit, offset)
    if bad:
        return bad
    path = _evidence_file(folder, "trades")
    if path is None:
        return _error("no trades for this run", 404)
    oos_start = _oos_start(record)
    try:
        handle = _open_guarded(path)
        bad = _schema_error(set(handle.schema_arrow.names), subset, symbol, reason, sort, oos_start)
        if bad:
            return bad
        frame = _load_frame(path, handle)
        order = _sorted_order(frame, path, subset, symbol, reason, sort, desc, oos_start)
        rows = _records(frame.loc[order[offset : offset + limit]])
    except _TooLarge:
        return _error(_TOO_LARGE_TRADES, 413)
    except Exception:
        logger.exception("Could not read trades for run %s", run_id)
        return _error("Could not read the trades for this run", 500)
    page = {
        "total": int(len(order)),
        "limit": limit,
        "offset": offset,
        "subset": subset,
        "subset_note": _SUBSET_NOTES[subset],
        "rows": rows,
    }
    response: dict[str, Any] = {"status": "success", "page": page}
    if subset in ("in_sample", "oos") and oos_start is None:
        response["message"] = "This run has no in-sample / out-of-sample split date."
    return True, response, 200


def _lookup(path: Path, column: str, value: str, label: str, schema_message: str) -> list:
    """Records where `column == value`: size guarded, projected, pushed down, memoised."""
    key = ("lookup", *_file_key(path), column, value)
    cached = _cache_get(_LOOKUP_CACHE, key)
    if cached is not None:
        return cached
    handle = _open_guarded(path)
    names = handle.schema_arrow.names
    if column not in names:
        raise _LedgerSchema(schema_message)
    table = pq.read_table(path, columns=names, filters=[(column, "==", value)])
    records = _records(table.to_pandas())
    _cache_put(_LOOKUP_CACHE, key, records, _LOOKUP_CACHE_MAX)
    return records


def _unique(records: list, what: str) -> dict[str, Any] | None:
    if len(records) > 1:
        raise _Inconsistent(what)
    return dict(records[0]) if records else None


def _sorted_fills(records: list) -> list[dict[str, Any]]:
    return sorted(
        (dict(r) for r in records),
        key=lambda r: (r.get("fill_time") is None, r.get("fill_time") or "", r.get("leg") or 0),
    )


def _ledger_part(folder: Path, name: str, column: str, value: Any) -> list | None:
    path = _evidence_file(folder, name)
    if path is None or not isinstance(value, str):
        return None
    return _lookup(path, column, value, name, "ledger schema unexpected")


def get_trade_lineage(
    idea_id: str, run_id: str, trade_id: str, root: Path | None = None
) -> tuple[bool, dict[str, Any], int]:
    """The signal, order and fills behind one trade, found by id in the run's ledgers."""
    if not isinstance(trade_id, str) or not _TRADE_ID.match(trade_id):
        return _error("invalid trade id", 400)
    folder, _record, failure = _resolve_evidence_folder(idea_id, run_id, root or DEFAULT_ROOT)
    if failure:
        return failure
    path = _evidence_file(folder, "trades")
    if path is None:
        return _error("no trades for this run", 404)
    try:
        trade_rows = _lookup(path, "trade_id", trade_id, "trades", "trades schema unexpected")
        if not trade_rows:
            return _error(f"Trade not found: {trade_id}", 404)
        trade = _unique(trade_rows, "trade")
        missing = [c for c in _LINEAGE_COLUMNS if c not in trade]
        signal = _unique(
            _ledger_part(folder, "signals", "signal_id", trade.get("signal_id")) or [], "signal"
        )
        orders = _ledger_part(folder, "orders", "order_id", trade.get("order_id"))
        order = _unique(orders or [], "order")
        fills = _sorted_fills(
            _ledger_part(folder, "fills", "order_id", trade.get("order_id")) or []
        )
    except _TooLarge:
        return _error(_TOO_LARGE_LEDGER, 413)
    except _LedgerSchema as exc:
        return _error(str(exc), 422)
    except _Inconsistent:
        return _error("ledger inconsistent", 409)
    except Exception:
        logger.exception("Could not read lineage for run %s", run_id)
        return _error("Could not read the lineage for this trade", 500)
    lineage = {
        "trade_id": trade_id,
        "signal": signal,
        "order": order,
        "fills": fills,
        "position_id": trade.get("position_id"),
        "missing": missing,
    }
    return True, {"status": "success", "lineage": lineage}, 200


def _get(run: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = run
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _flat_metrics(run: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(run["metrics"])
    oos = run.get("out_of_sample") or {}
    if "net_bps" in oos:
        metrics["oos_net_bps"] = oos["net_bps"]
    return metrics


def _metric_deltas(a: dict[str, Any], b: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key in sorted({*a, *b}):
        va, vb = a.get(key), b.get(key)
        delta = round(vb - va, 6) if va is not None and vb is not None else None
        out[key] = {"a": va, "b": vb, "delta": delta}
    return out


def _param_diff(a: dict[str, Any], b: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        "changed": {k: {"a": a[k], "b": b[k]} for k in a.keys() & b.keys() if a[k] != b[k]},
        "added": {k: b[k] for k in b.keys() - a.keys()},
        "removed": {k: a[k] for k in a.keys() - b.keys()},
    }


def _comparability(a: dict[str, Any], b: dict[str, Any]) -> tuple[list[str], list[str]]:
    warnings = [
        f"{label} differs: {_get(a, path)} vs {_get(b, path)}"
        for label, path in _LIKE_FOR_LIKE
        if _get(a, path) != _get(b, path)
    ]
    info = []
    if _get(a, ("data", "as_of")) != _get(b, ("data", "as_of")):
        info.append("data snapshot date differs")
    if a["code_version"] != b["code_version"]:
        info.append("code version differs")
    return warnings, info


_NO_FOLDER_NOTE = "Detailed comparison needs both runs to have a run folder."
_UNAVAILABLE_NOTE = "Detailed comparison is unavailable for these runs."
_COMPARE_ERROR_NOTE = "Detailed comparison is unavailable: a run folder is missing required files."


def _detailed_comparison(
    idea: dict[str, Any], a: dict[str, Any], b: dict[str, Any], root: Path
) -> tuple[dict[str, Any] | None, str | None, bool]:
    """The (comparison, note, folder_rejected) triple for two runs of one idea.

    `folder_rejected` is True when a schema 3 run's artifact_dir fails the same
    containment checks get_run_detail applies; the caller answers 400 then.
    """
    if any(r.get("schema") not in _ARTIFACT_SCHEMAS or not r.get("artifact_dir") for r in (a, b)):
        return None, _NO_FOLDER_NOTE, False
    runs_dir = _idea_folder(idea, root) / "runs"
    ideas_root = root / "research" / "ideas"
    folders = [_resolve_run_folder(r, runs_dir, r["run_id"], ideas_root) for r in (a, b)]
    if any(f is None for f in folders):
        return None, None, True
    # Lazy on purpose: the engine loads numpy/pandas/pyarrow/scipy on first use only,
    # and never imports the backtest harness. Keep it out of module import time.
    from signal_engine.backtest.compare import CompareError, compare_run_folders

    try:
        return _jsonable(compare_run_folders(folders[0], folders[1])), None, False
    except CompareError:
        logger.exception("Run comparison failed for %s and %s", a["run_id"], b["run_id"])
        return None, _COMPARE_ERROR_NOTE, False
    except Exception:
        logger.exception("Unexpected error comparing %s and %s", a["run_id"], b["run_id"])
        return None, _UNAVAILABLE_NOTE, False


def compare_runs(
    idea_id: str, run_a: str, run_b: str, root: Path | None = None
) -> tuple[bool, dict[str, Any], int]:
    """Two runs of one idea side by side: metric deltas, parameter diff, overlay series."""
    for run_id in (run_a, run_b):
        if not _RUN_ID.match(run_id or ""):
            return _error("Invalid run id", 400)
    if run_a == run_b:
        return _error("Pick two different runs to compare", 400)
    idea, failure = _find_idea(idea_id, root or DEFAULT_ROOT)
    if failure:
        return failure
    by_id = {r["run_id"]: r for r in idea.get("runs") or []}
    missing = [r for r in (run_a, run_b) if r not in by_id]
    if missing:
        return _error(f"Run not found: {', '.join(missing)}", 404)
    a, b = by_id[run_a], by_id[run_b]
    warnings, info = _comparability(a, b)
    comparison, note, rejected = _detailed_comparison(idea, a, b, root or DEFAULT_ROOT)
    if rejected:
        return _error("invalid run folder", 400)
    return (
        True,
        {
            "status": "success",
            "idea_id": idea_id,
            "run_a": {k: a.get(k) for k in ("run_id", "date", "code_version", "change_note")},
            "run_b": {k: b.get(k) for k in ("run_id", "date", "code_version", "change_note")},
            "metric_deltas": _metric_deltas(_flat_metrics(a), _flat_metrics(b)),
            "params": _param_diff(a.get("params") or {}, b.get("params") or {}),
            "n_trials": {"a": a.get("n_trials", 1), "b": b.get("n_trials", 1)},
            "comparable": not warnings,
            "warnings": warnings,
            "info": info,
            "equity_r": {
                "a": (a.get("series") or {}).get("equity_r", []),
                "b": (b.get("series") or {}).get("equity_r", []),
            },
            "comparison": comparison,
            "comparison_note": note,
        },
        200,
    )
