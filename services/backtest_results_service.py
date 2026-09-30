"""Backtest results service.

Read-only view over `signal_engine/research`: every idea in the registry, and the dated,
immutable backtest run records stored beside it (`ideas/<idea>/runs/<run_id>.json`).
Serves the /backtest-results page: the leaderboard, one idea's run history, and a
side-by-side comparison of two runs of the same idea.

All functions return `(success, response, status_code)` like the other services. Nothing
here writes: run records are produced by `signal_engine.backtest.record_run`.
"""

import re
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

from signal_engine.research.build_registry import RegistryError, load_ideas
from signal_engine.timeutils import IST
from utils.logging import get_logger

logger = get_logger(__name__)

#: Directory holding `research/` (the registry root that `load_ideas` expects).
DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "signal_engine"

SPARKLINE_POINTS = 60

_IDEA_ID = re.compile(r"^IDEA-\d{3}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")

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
    files = [*ideas.glob("*/idea.md"), *ideas.glob("*/runs/*.json")]
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
    "category",
    "horizon",
    "summary",
    "result",
    "revisit_when",
    "last_reviewed",
    "tags",
    "source",
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


def get_idea_results(
    idea_id: str, root: Path | None = None, today: date | None = None
) -> tuple[bool, dict[str, Any], int]:
    """One idea and its full run history, oldest first, with chart series."""
    idea, failure = _find_idea(idea_id, root or DEFAULT_ROOT)
    if failure:
        return failure
    today = today or _today()
    card = {k: idea.get(k) for k in _IDEA_FIELDS}
    runs = [{**run, **_coverage(run, today)} for run in idea.get("runs") or []]
    return True, {"status": "success", "idea": card, "runs": runs}, 200


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
        },
        200,
    )
