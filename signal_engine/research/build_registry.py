"""Build the research idea registry from research/ideas/*/idea.md.

Every idea is a folder holding an `idea.md` with YAML frontmatter. This script is the
only thing that reads them; it validates each one and generates three outputs:

    research/REGISTRY.md      human index, counts by status / strategy category
    research/registry.json    machine-readable, for tooling
    research/site/index.html  self-contained static page, publishable as-is

The outputs are generated, never hand-edited. `--check` exits non-zero when they are
stale or any idea is invalid, so it can gate a commit or CI.

    uv run python -m signal_engine.research.build_registry           # regenerate
    uv run python -m signal_engine.research.build_registry --check   # verify only

Frontmatter fields are documented in research/README.md.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from signal_engine.backtest import manifest as run_manifest
from signal_engine.research.intake import lifecycle, spec, taxonomy, trials, versions

#: The legacy `status` and `verdict` values. Both are derived for every entry (see
#: `lifecycle.derive_status` / `derive_verdict`), never hand-written in frontmatter.
STATUSES = ("inbox", "evaluating", "backtested", "paper", "live", "rejected", "shelved")
VERDICTS = ("none", "edge", "marginal", "no-edge", "inconclusive")
HORIZONS = ("intraday", "swing", "positional")
#: Frontmatter keys retired in favour of `strategy_category` / `strategy_tags`.
LEGACY_CLASSIFICATION_KEYS = ("category", "tags")
LEGACY_CLASSIFICATION_HINT = (
    "category was replaced by strategy_category and tags by strategy_tags; "
    "run `uv run python -m signal_engine.research.intake migrate-classification`"
)
REQUIRED = (
    "id",
    "title",
    "stage",
    "horizon",
    "source",
    "summary",
    "last_reviewed",
)
PATH_FIELDS = ("strategy_path", "analysis_doc", "backtest_module")

#: Why a rejected idea was rejected. "Rejected" must mean there is evidence that more
#: tuning will not produce a durable edge, not that one parameter set did badly, so the
#: reason is stated, not implied. Ideas last reviewed before the cutoff predate the field
#: and are exempt; any later edit to a rejected idea has to name its basis.
REJECTION_BASES = (
    "no-gross-edge",
    "cost-dominated",
    "overfit",
    "not-robust",
    "concurrency-artifact",
    "not-a-strategy",
    "duplicate",
)
REJECTION_BASIS_REQUIRED_FROM = "2026-10-01"

#: A lineage step smaller than this many net bps is noise, not progress.
TREND_MIN_DELTA_BPS = 1.0
#: How many of the latest parent-to-child steps decide the trend.
TREND_WINDOW_STEPS = 3
#: Day-clustered t-statistic a run must reach before its level counts as evidence of an
#: edge (acceptance criteria G2.2); a run's own trial hurdle raises it, never lowers it.
EDGE_T_FLOOR = 2.5

#: A run record is one reproducible backtest execution, stored as
#: research/ideas/<idea>/runs/<run-id>.json. The required keys are what someone needs to
#: re-run it and to compare it fairly with another run: which code, which data snapshot,
#: which universe and period, which cost model, and the headline metrics.
RUN_REQUIRED = ("run_id", "date", "code_version", "data", "period", "costs", "metrics")
RUN_SECTIONS = {
    "data": ("source", "as_of", "universe", "bar_size"),
    "period": ("start", "end"),
    "costs": ("model", "round_trip_bps"),
    "metrics": ("trades", "gross_bps", "net_bps"),
}
RUN_NUMERIC = {
    "costs": ("round_trip_bps",),
    "metrics": (
        "trades",
        "gross_bps",
        "net_bps",
        "win_rate",
        "sharpe",
        "max_dd_pct",
        "t_stat",
        "breakeven_cost_bps",
        "profit_factor",
        "expectancy_r",
        "payoff",
        "hurdle_t",
        "max_dd_r",
        "days",
        "peak_concurrent",
        "median_concurrent",
    ),
}

#: Schema v2 keys, all optional so v1 records stay valid. They carry what the results page
#: needs: lineage between re-runs of the same idea, the parameters that produced a run, the
#: number of variants tried (overfitting exposure), and the tearsheet series.
RUN_HEAVY_KEYS = ("series", "evaluation", "regimes")

#: Row statuses of an evaluation profile (backtest/evaluation.py owns their meaning).
EVALUATION_STATUSES = ("pass", "fail", "report", "not_evaluated", "not_applicable")
_SERIES_POINTS = ("equity_r", "drawdown_r")
_MONTHLY_KEYS = ("month", "n", "total_r", "net_r")


def _is_num(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float))


def _valid_position_cap(row: Any) -> bool:
    slots = row.get("max_positions") if isinstance(row, dict) else None
    return (
        isinstance(row, dict)
        and isinstance(slots, int)
        and not isinstance(slots, bool)
        and slots >= 1
        and all(_is_num(row.get(k)) for k in ("trades", "gross_bps", "net_bps"))
    )


def _valid_evaluation_row(row: Any) -> bool:
    return (
        isinstance(row, dict)
        and isinstance(row.get("gate"), str)
        and row.get("status") in EVALUATION_STATUSES
        and isinstance(row.get("hard"), bool)
    )


def _valid_regime_row(row: Any) -> bool:
    return (
        isinstance(row, dict)
        and isinstance(row.get("regime"), str)
        and isinstance(row.get("trades"), int)
        and not isinstance(row.get("trades"), bool)
        and _is_num(row.get("net_r"))
        and isinstance(row.get("low_sample"), bool)
    )


def _validate_regimes(block: Any) -> list[str]:
    """The regimes block is optional; when present it holds per-regime result rows."""
    ok = (
        isinstance(block, dict)
        and isinstance(block.get("version"), int)
        and all(
            isinstance(block.get(dim), list) and all(_valid_regime_row(r) for r in block[dim])
            for dim in ("trend", "vol")
        )
    )
    if ok:
        return []
    return [
        "'regimes' must be {version, trend: [...], vol: [...]} where each row has a string "
        "regime, integer trades, numeric net_r and boolean low_sample"
    ]


_COST_MODEL_NUMBERS = (
    "capital",
    "risk_per_trade",
    "leverage",
    "slippage_bps_per_side",
    "flat_bps",
    "mean_statutory_bps",
    "mean_total_bps",
    "net_r",
    "net_r_slip_2x",
)


def _validate_cost_model(block: Any) -> list[str]:
    """The per-trade cost model block is optional; when present it must be complete."""
    ok = (
        isinstance(block, dict)
        and isinstance(block.get("model"), str)
        and isinstance(block.get("product"), str)
        and all(_is_num(block.get(k)) for k in _COST_MODEL_NUMBERS)
        and isinstance(block.get("total_bps_at"), dict)
        and all(_is_num(v) for v in block["total_bps_at"].values())
    )
    if ok:
        return []
    return [
        "'cost_model' must be an object with string model and product, numeric "
        f"{', '.join(_COST_MODEL_NUMBERS)}, and a total_bps_at object of numbers"
    ]


def _validate_evaluation(block: Any) -> list[str]:
    """The evaluation profile is optional; when present it must be a list of gate rows."""
    dimensions = block.get("dimensions") if isinstance(block, dict) else None
    ok = isinstance(dimensions, list) and all(
        isinstance(d, dict)
        and isinstance(d.get("name"), str)
        and isinstance(d.get("rows"), list)
        and all(_valid_evaluation_row(r) for r in d["rows"])
        for d in dimensions
    )
    if ok:
        return []
    return [
        "'evaluation' must be {dimensions: [{name, rows: [{gate, status, hard}]}]} with status "
        f"one of {', '.join(EVALUATION_STATUSES)}"
    ]


def _validate_run_v2(run: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    n_trials = run.get("n_trials")
    if "n_trials" in run and (
        isinstance(n_trials, bool) or not isinstance(n_trials, int) or n_trials < 1
    ):
        problems.append(f"'n_trials' must be an integer >= 1, got {n_trials!r}")
    if "promoted" in run and not isinstance(run["promoted"], bool):
        problems.append("'promoted' must be true or false")
    if "params" in run and not isinstance(run["params"], dict):
        problems.append("'params' must be an object")
    if "change_note" in run and not isinstance(run["change_note"], str):
        problems.append("'change_note' must be a string")
    parent = run.get("parent_run_id")
    if parent is not None and not isinstance(parent, str):
        problems.append("'parent_run_id' must be a string or null")
    if "cost_sensitivity" in run:
        rows = run["cost_sensitivity"]
        if not isinstance(rows, list) or not all(
            isinstance(r, dict) and _is_num(r.get("round_trip_bps")) and _is_num(r.get("net_bps"))
            for r in rows
        ):
            problems.append(
                "'cost_sensitivity' must be a list of {round_trip_bps, net_bps} numbers"
            )
    for key in ("in_sample", "out_of_sample"):
        if key in run and not isinstance(run[key], dict):
            problems.append(f"'{key}' must be an object")
    if "baseline" in run:
        base = run["baseline"]
        if not (
            isinstance(base, dict)
            and all(_is_num(base.get(k)) for k in ("gross_bps", "net_bps", "excess_net_bps"))
            and ("excess_t_stat" not in base or _is_num(base["excess_t_stat"]))
            and ("method" not in base or isinstance(base["method"], str))
        ):
            problems.append(
                "'baseline' must be an object with numeric gross_bps, net_bps and "
                "excess_net_bps (optional numeric excess_t_stat, string method)"
            )
    if "position_caps" in run:
        caps = run["position_caps"]
        if not isinstance(caps, list) or not all(_valid_position_cap(c) for c in caps):
            problems.append(
                "'position_caps' must be a list of {max_positions (int >= 1), trades, "
                "gross_bps, net_bps} rows"
            )
    if "cost_model" in run:
        problems.extend(_validate_cost_model(run["cost_model"]))
    split = run.get("split")
    if isinstance(split, dict) and "embargo_sessions" in split:
        for key in ("embargo_sessions", "purged_in_sample", "embargoed_oos"):
            value = split.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                problems.append(f"'split.{key}' (embargo) must be an integer >= 0, got {value!r}")
    if "evaluation" in run:
        problems.extend(_validate_evaluation(run["evaluation"]))
    if "regimes" in run:
        problems.extend(_validate_regimes(run["regimes"]))
    series = run.get("series")
    if "series" in run:
        if not isinstance(series, dict):
            problems.append("'series' must be an object")
        else:
            for key in _SERIES_POINTS:
                pts = series.get(key)
                if key in series and not (
                    isinstance(pts, list)
                    and all(
                        isinstance(p, list)
                        and len(p) == 2
                        and isinstance(p[0], str)
                        and _is_num(p[1])
                        for p in pts
                    )
                ):
                    problems.append(f"'series.{key}' must be a list of [date, number] pairs")
            monthly = series.get("monthly")
            if "monthly" in series and not (
                isinstance(monthly, list)
                and all(isinstance(m, dict) and all(k in m for k in _MONTHLY_KEYS) for m in monthly)
            ):
                problems.append(f"'series.monthly' rows need {', '.join(_MONTHLY_KEYS)}")
    return problems


_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_ID = re.compile(r"^IDEA-(\d{3})$")


class RegistryError(Exception):
    """One or more ideas are invalid. The message lists every problem found."""


def _normalise(value: Any) -> Any:
    """Turn YAML dates into ISO strings so the data is JSON-safe and stable."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalise(v) for v in value]
    return value


def _parse_frontmatter(path: Path) -> dict[str, Any]:
    match = _FRONTMATTER.match(path.read_text(encoding="utf-8"))
    if not match:
        raise ValueError("no frontmatter block (--- ... ---) at top of file")
    data = yaml.safe_load(match.group(1))
    if not isinstance(data, dict):
        raise ValueError("frontmatter is not a mapping")
    return _normalise(data)


def _validate_version_pointers(idea: dict[str, Any], folder: Path) -> list[str]:
    """versions.yaml must be valid and the idea's version pointers must name registered versions."""
    try:
        known = {v.version for v in versions.load_versions(folder)}
    except versions.VersionError as exc:
        return [f"versions.yaml: {exc}"]
    return [
        f"{key} '{idea[key]}' is not a version in versions.yaml"
        for key in versions.POINTER_KEYS
        if idea.get(key) is not None and idea[key] not in known
    ]


def _validate(
    idea: dict[str, Any], folder: Path, root: Path, categories: tuple[str, ...] | None = None
) -> list[str]:
    problems = [f"missing field '{f}'" for f in REQUIRED if f not in idea]
    problems.extend(lifecycle.vocabulary_problems(idea))
    if categories is not None:
        problems.extend(taxonomy.classification_problems(idea, categories))
    if any(key in idea for key in LEGACY_CLASSIFICATION_KEYS):
        problems.append(LEGACY_CLASSIFICATION_HINT)
    if "horizon" in idea and idea["horizon"] not in HORIZONS:
        problems.append(f"horizon '{idea['horizon']}' not one of {', '.join(HORIZONS)}")
    if "id" in idea:
        m = _ID.match(str(idea["id"]))
        if not m:
            problems.append(f"id '{idea['id']}' is not IDEA-NNN")
        elif not folder.name.startswith(f"idea-{m.group(1)}-"):
            problems.append(f"id {idea['id']} does not match folder name '{folder.name}'")
    for field in PATH_FIELDS:
        target = idea.get(field)
        if target and not (root / target).exists():
            problems.append(f"{field} '{target}' does not exist")
    problems.extend(_validate_rejection_basis(idea))
    problems.extend(_validate_version_pointers(idea, folder))
    return problems


def _validate_rejection_basis(idea: dict[str, Any]) -> list[str]:
    basis = idea.get("rejection_basis")
    rejected = idea.get("research_state") == "rejected"
    if basis is None:
        needed = rejected and str(idea.get("last_reviewed", "")) >= (REJECTION_BASIS_REQUIRED_FROM)
        return (
            [
                f"rejection_basis is required for a rejected idea: one of {', '.join(REJECTION_BASES)}"
            ]
            if needed
            else []
        )
    if basis not in REJECTION_BASES:
        return [f"rejection_basis '{basis}' not one of {', '.join(REJECTION_BASES)}"]
    if not rejected:
        return ["rejection_basis is only valid when research_state is rejected"]
    return []


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
V3_REQUIRED_FILES = (
    "manifest.json",
    "assumptions.json",
    "strategy/spec.yaml",
    "strategy/strategy.json",
    "execution/config.json",
    "evidence/trades.parquet",
    "report/report.json",
)


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def _v3_field_problems(run: dict[str, Any]) -> list[str]:
    """Presence and type checks for the schema 3 index fields."""
    checks = {
        "artifact_dir": lambda v: isinstance(v, str) and v.endswith("/"),
        "manifest_sha256": lambda v: isinstance(v, str) and bool(_HEX64.match(v)),
        "strategy_version": lambda v: isinstance(v, str),
        "strategy_fingerprint": lambda v: isinstance(v, str) and bool(_HEX64.match(v)),
        "change_classes": _is_str_list,
        "reproducible": lambda v: isinstance(v, bool),
        "code_dirty": lambda v: isinstance(v, bool),
        "promotion_eligible": lambda v: isinstance(v, bool),
        "missing_capabilities": _is_str_list,
    }
    problems: list[str] = []
    for field, ok in checks.items():
        if field not in run:
            problems.append(f"missing field '{field}'")
        elif not ok(run[field]):
            problems.append(f"'{field}' has the wrong type or format: {run[field]!r}")
    return problems


def _resolve_artifact_dir(run: dict[str, Any], runs_dir: Path) -> tuple[Path | None, str | None]:
    """Resolve artifact_dir inside runs_dir. Returns (path, None) or (None, problem)."""
    rel = run["artifact_dir"]
    if rel.startswith(("/", "\\")) or ".." in rel.replace("\\", "/").split("/"):
        return None, f"artifact_dir '{rel}' must be relative and stay inside the runs directory"
    root = runs_dir.resolve()
    target = (runs_dir / rel).resolve()
    if target == root or root not in target.parents:
        return None, f"artifact_dir '{rel}' must stay inside the runs directory"
    if not target.is_dir():
        return None, f"artifact_dir '{rel}' does not exist"
    return target, None


def _v3_manifest_agreement(run: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    """Problems where the flat record contradicts its manifest."""
    engine = manifest.get("engine")
    dirty = engine.get("code_dirty") if isinstance(engine, dict) else None
    repro = manifest.get("reproducible")
    pairs = (
        ("reproducible", repro),
        ("code_dirty", dirty),
        ("change_classes", manifest.get("change_classes")),
        ("missing_capabilities", manifest.get("missing_capabilities")),
    )
    problems = [
        f"{field} differs from the manifest ({run.get(field)!r} vs {expected!r})"
        for field, expected in pairs
        if run.get(field) != expected
    ]
    if isinstance(repro, bool) and isinstance(dirty, bool):
        eligible = repro and not dirty
        if run.get("promotion_eligible") != eligible:
            problems.append(
                f"promotion_eligible must be {eligible} (reproducible and not code_dirty), "
                f"got {run.get('promotion_eligible')!r}"
            )
    return problems


def _v3_report_agreement(run: dict[str, Any], report: Any) -> list[str]:
    """Problems where the flat verdict fields contradict the hashed report/report.json."""
    if not isinstance(report, dict):
        return ["report/report.json is not a JSON object"]
    verdict = report.get("verdict")
    promotion = report.get("promotion")
    stages = promotion.get("stages") if isinstance(promotion, dict) else None
    backtest = stages.get("backtest") if isinstance(stages, dict) else None
    pairs = (
        ("run_verdict", verdict.get("run_verdict") if isinstance(verdict, dict) else None),
        (
            "promotion_decision",
            backtest.get("decision") if isinstance(backtest, dict) else None,
        ),
        ("next_action", report.get("next_action")),
    )
    return [
        f"{field} differs from report/report.json ({run.get(field)!r} vs {expected!r})"
        for field, expected in pairs
        if run.get(field) != expected
    ]


def _v3_folder_problems(run: dict[str, Any], runs_dir: Path) -> list[str]:
    """Check a schema 3 record against its artifact folder."""
    folder, problem = _resolve_artifact_dir(run, runs_dir)
    if folder is None:
        return [problem or "artifact_dir unusable"]
    problems = [
        f"required artifact missing: {rel}"
        for rel in V3_REQUIRED_FILES
        if not (folder / rel).is_file()
    ]
    if "manifest.json" in {r.split(": ", 1)[-1] for r in problems}:
        return problems
    try:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [*problems, f"manifest.json unreadable ({exc})"]
    if run_manifest.manifest_sha256(folder) != run["manifest_sha256"]:
        problems.append("manifest_sha256 does not match manifest.json")
    problems.extend(f"artifact check: {p}" for p in run_manifest.verify_artifacts(folder))
    problems.extend(f"manifest invalid: {p}" for p in run_manifest.validate_manifest(manifest))
    if not isinstance(manifest, dict):
        return problems
    if manifest.get("run_id") != run["run_id"]:
        problems.append(
            f"manifest run_id '{manifest.get('run_id')}' differs from record run_id '{run['run_id']}'"
        )
    fps = manifest.get("fingerprints")
    strat = manifest.get("strategy")
    if isinstance(fps, dict) and fps.get("strategy") != run["strategy_fingerprint"]:
        problems.append("strategy_fingerprint differs from manifest fingerprints.strategy")
    if isinstance(strat, dict) and strat.get("version") != run["strategy_version"]:
        problems.append("strategy_version differs from manifest strategy.version")
    problems.extend(_v3_manifest_agreement(run, manifest))
    report_path = folder / "report" / "report.json"
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"report/report.json unreadable ({exc})")
        else:
            problems.extend(_v3_report_agreement(run, report))
    return problems


def _validate_run(run: Any, runs_dir: Path | None = None) -> list[str]:
    if not isinstance(run, dict):
        return ["run is not a JSON object"]
    problems = [f"missing field '{f}'" for f in RUN_REQUIRED if f not in run]
    for section, keys in RUN_SECTIONS.items():
        block = run.get(section)
        if section in run and not isinstance(block, dict):
            problems.append(f"'{section}' must be an object")
            continue
        problems.extend(
            f"missing '{section}.{k}'" for k in keys if isinstance(block, dict) and k not in block
        )
    for section, keys in RUN_NUMERIC.items():
        block = run.get(section)
        if not isinstance(block, dict):
            continue
        for key in keys:
            value = block.get(key)
            if key in block and (isinstance(value, bool) or not isinstance(value, (int, float))):
                problems.append(f"'{section}.{key}' must be a number, got {value!r}")
    problems.extend(_validate_run_v2(run))
    if "schema" in run:
        if isinstance(run["schema"], bool) or run["schema"] != 3:
            problems.append(f"unsupported schema {run['schema']!r}")
        else:
            field_problems = _v3_field_problems(run)
            problems.extend(field_problems)
            if not field_problems and runs_dir is not None and isinstance(run.get("run_id"), str):
                problems.extend(_v3_folder_problems(run, runs_dir))
    return problems


def _load_runs(folder: Path) -> tuple[list[dict[str, Any]], list[str]]:
    runs: list[dict[str, Any]] = []
    problems: list[str] = []
    seen: set[str] = set()
    for path in sorted((folder / "runs").glob("*.json")):
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"{folder.name}/runs/{path.name}: unreadable ({exc})")
            continue
        found = _validate_run(run, folder / "runs")
        problems.extend(f"{folder.name}/runs/{path.name}: {p}" for p in found)
        if found:
            continue
        if run["run_id"] in seen:
            problems.append(f"{folder.name}/runs/{path.name}: duplicate run_id {run['run_id']}")
            continue
        seen.add(run["run_id"])
        runs.append(run)
    runs_dir = folder / "runs"
    if runs_dir.is_dir():
        for sub in sorted(runs_dir.iterdir()):
            if (
                sub.is_dir()
                and not sub.name.startswith(".")
                and not sub.name.endswith(".reproductions")
                and not (runs_dir / f"{sub.name}.json").exists()
            ):
                problems.append(
                    f"{folder.name}/runs/{sub.name}: orphan run folder with no {sub.name}.json record"
                )
    ids = {r["run_id"] for r in runs}
    for run in runs:
        parent = run.get("parent_run_id")
        if parent is not None and (parent == run["run_id"] or parent not in ids):
            problems.append(
                f"{folder.name}: run {run['run_id']} has parent_run_id '{parent}' "
                "that is not another run of this idea"
            )
    return sorted(runs, key=lambda r: (r["date"], r["run_id"])), problems


def load_runs(folder: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """The valid run records of an idea folder (oldest first) and the problems found."""
    return _load_runs(folder)


def latest_run(idea: dict[str, Any]) -> dict[str, Any] | None:
    """The most recent run record of an idea, or None if it has never been backtested."""
    runs = idea.get("runs") or []
    return runs[-1] if runs else None


def _step(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    """What changed from `parent` to `child`: net bps, trades and, if both have it, t."""
    pm, cm = parent["metrics"], child["metrics"]
    out: dict[str, Any] = {
        "parent_run_id": parent["run_id"],
        "net_bps": round(cm["net_bps"] - pm["net_bps"], 4),
    }
    if "t_stat" in pm and "t_stat" in cm:
        out["t_stat"] = round(cm["t_stat"] - pm["t_stat"], 4)
    out["trades"] = cm["trades"] - pm["trades"]
    return out


def edge_bar(metrics: dict[str, Any]) -> float:
    """The t-statistic a run must reach: the larger of the floor and its trial hurdle."""
    return max(EDGE_T_FLOOR, float(metrics.get("hurdle_t") or 0.0))


def lineage(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Summarise an idea's run history, oldest first. None when it has never been run.

    The trend follows the latest run's own parent chain (side branches do not count) and
    sums the last `TREND_WINDOW_STEPS` steps of net bps: improving, regressing, or
    stagnant when the sum is inside `TREND_MIN_DELTA_BPS`. The trend is about direction
    only; `edge_status` says whether the latest level clears `edge_bar`, so a rising line
    that is still below the bar is not mistaken for promise. `trials_sum` adds `n_trials`
    over every run. Runs recorded without a trial count contribute 1 each, so it is a
    floor for the number of variants looked at, not the true ledger.
    """
    if not runs:
        return None
    by_id = {r["run_id"]: r for r in runs}
    latest = runs[-1]
    chain = [latest]
    while chain[-1].get("parent_run_id") in by_id:
        chain.append(by_id[chain[-1]["parent_run_id"]])
    chain.reverse()
    window = chain[-(TREND_WINDOW_STEPS + 1) :]
    steps = [_step(p, c) for p, c in zip(window, window[1:], strict=False)]
    total = sum(s["net_bps"] for s in steps)
    if not steps:
        trend = "insufficient"
    elif total >= TREND_MIN_DELTA_BPS:
        trend = "improving"
    elif total <= -TREND_MIN_DELTA_BPS:
        trend = "regressing"
    else:
        trend = "stagnant"
    bar = edge_bar(latest["metrics"])
    t_stat = latest["metrics"].get("t_stat")
    if t_stat is None:
        edge_status = "unknown"
    else:
        edge_status = "clears_bar" if t_stat >= bar else "below_bar"
    return {
        "runs": len(runs),
        "latest_run_id": latest["run_id"],
        "trials_sum": sum(int(r.get("n_trials", 1)) for r in runs),
        "delta_vs_parent": _step(chain[-2], latest) if len(chain) > 1 else None,
        "trend": trend,
        "edge_status": edge_status,
        "edge_bar": bar,
    }


def _entry(
    idea: dict[str, Any], folder: Path, runs: list[dict[str, Any]], categories: tuple[str, ...]
) -> dict[str, Any]:
    """A validated idea as published: frontmatter plus runs, lineage, the derived legacy
    `status` / `verdict` and the classification."""
    state = idea.get("research_state", lifecycle.DEFAULT_RESEARCH_STATE)
    entry: dict[str, Any] = {
        **idea,
        "folder": folder.name,
        "runs": runs,
        "research_state": state,
        "status": lifecycle.derive_status(idea["stage"], state, bool(runs)),
        "verdict": lifecycle.derive_verdict(runs[-1] if runs else None),
        "strategy_category": taxonomy.category_of(idea),
        "strategy_tags": taxonomy.tags_of(idea),
    }
    entry["classification"] = taxonomy.classification_block(idea, categories, _valid_spec(folder))
    if runs:
        entry["lineage"] = lineage(runs)
    return entry


def _valid_spec(folder: Path) -> dict[str, Any] | None:
    """The idea's current spec if it exists and is valid, else None."""
    try:
        data = spec.load_spec(folder)
    except spec.SpecError:
        return None
    return data if isinstance(data, dict) and not spec.validate_spec(data) else None


def _trials_problems(folder: Path, runs: list[dict[str, Any]]) -> list[str]:
    """Problems with the idea's trials.yaml: unreadable or invalid, or shrunk.

    The ledger is append-only, so its current total can never be lower than the highest total
    any run recorded in its `trials` block at the time it ran.
    """
    try:
        total = trials.count(folder).total
    except trials.TrialError as exc:
        return [f"trials.yaml: {exc}"]
    recorded = [
        r["trials"]["total"]
        for r in runs
        if isinstance(r.get("trials"), dict) and isinstance(r["trials"].get("total"), int)
    ]
    if recorded and total < max(recorded):
        return [
            f"trials.yaml shrank: rows were removed or counts lowered (now {total} trials, a run "
            f"recorded {max(recorded)})"
        ]
    return []


def load_ideas(root: Path) -> list[dict[str, Any]]:
    """Load and validate every idea under `root`/research/ideas. Raises RegistryError."""
    ideas_dir = root / "research" / "ideas"
    ideas: list[dict[str, Any]] = []
    problems: list[str] = []
    seen: dict[str, str] = {}
    try:
        categories: tuple[str, ...] | None = taxonomy.load_categories(taxonomy.taxonomy_path(root))
    except taxonomy.TaxonomyError as exc:
        categories = None
        problems.append(f"taxonomy: {exc}")
    for folder in sorted(
        p for p in ideas_dir.iterdir() if p.is_dir() and not p.name.startswith("_")
    ):
        idea_file = folder / "idea.md"
        try:
            idea = _parse_frontmatter(idea_file)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            problems.append(
                f"{folder.name}: {exc}" if idea_file.exists() else f"{folder.name}: idea.md missing"
            )
            continue
        found = _validate(idea, folder, root, categories)
        if not found:
            found = spec.registry_problems(str(idea["id"]), str(idea["stage"]), folder)
        runs, run_problems = _load_runs(folder)
        if not found and not run_problems:
            found = lifecycle.evidence_problems(idea, runs)
        found = [*found, *_trials_problems(folder, runs)]
        problems.extend(f"{folder.name}: {p}" for p in found)
        ident = str(idea.get("id"))
        if ident in seen:
            problems.append(f"{folder.name}: duplicate id {ident} (also {seen[ident]})")
        seen[ident] = folder.name
        problems.extend(run_problems)
        if not found and not run_problems and categories is not None:
            ideas.append(_entry(idea, folder, runs, categories))
    if problems:
        raise RegistryError("\n".join(problems))
    return sorted(ideas, key=lambda i: i["id"])


def summarize(ideas: list[dict[str, Any]]) -> dict[str, Any]:
    def count(field: str) -> dict[str, int]:
        return dict(sorted(Counter(i[field] for i in ideas).items()))

    return {
        "total": len(ideas),
        "by_status": count("status"),
        "by_verdict": count("verdict"),
        "by_category": count("strategy_category"),
        "by_horizon": count("horizon"),
        "total_runs": sum(len(i.get("runs", [])) for i in ideas),
        "ideas_with_runs": sum(1 for i in ideas if i.get("runs")),
        "last_reviewed": max((i["last_reviewed"] for i in ideas), default=""),
    }


def _run_line(run: dict[str, Any] | None) -> str:
    if run is None:
        return "none"
    m = run["metrics"]
    return f"{run['run_id']}: {m['trades']} trades, gross {m['gross_bps']} / net {m['net_bps']} bps"


def _cell(text: Any) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ")


def render_markdown(ideas: list[dict[str, Any]]) -> str:
    s = summarize(ideas)
    lines = [
        "# Research Idea Registry",
        "",
        "Generated by `signal_engine/research/build_registry.py` from `research/ideas/*/idea.md`.",
        "Do not edit by hand: edit the idea's `idea.md` and re-run the script.",
        "",
        f"Total ideas: **{s['total']}**. Backtest runs recorded: **{s['total_runs']}** "
        f"across {s['ideas_with_runs']} ideas. Last reviewed: {s['last_reviewed'] or 'n/a'}.",
        "",
        "## By status",
        "",
        "| Status | Count |",
        "| --- | --- |",
        *[f"| {k} | {v} |" for k, v in s["by_status"].items()],
        "",
        "## By category",
        "",
        "| Category | Count |",
        "| --- | --- |",
        *[f"| {k} | {v} |" for k, v in s["by_category"].items()],
        "",
        "## All ideas",
        "",
        "| ID | Title | Status | Verdict | Category | Latest run | Result | Revisit when | Detail |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i in ideas:
        detail = (
            i.get("analysis_doc")
            or i.get("strategy_path")
            or f"research/ideas/{i['folder']}/idea.md"
        )
        lines.append(
            f"| {i['id']} | {_cell(i['title'])} | {i['status']} | {i['verdict']} | {i['strategy_category']} "
            f"| {_run_line(latest_run(i))} | {_cell(i.get('result'))} | {_cell(i.get('revisit_when'))} | `{detail}` |"
        )
    return "\n".join(lines) + "\n"


def _slim(idea: dict[str, Any]) -> dict[str, Any]:
    """The idea as published in registry.json: run series stay in the run files."""
    runs = []
    for r in idea.get("runs", []):
        slim = {k: v for k, v in r.items() if k not in RUN_HEAVY_KEYS}
        summary = (r.get("evaluation") or {}).get("summary")
        if summary is not None:
            slim["evaluation_summary"] = summary
        runs.append(slim)
    return {**idea, "runs": runs}


def render_json(ideas: list[dict[str, Any]]) -> str:
    return (
        json.dumps(
            {"summary": summarize(ideas), "ideas": [_slim(i) for i in ideas]},
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


_CSS = """
:root{--bg:#fafaf9;--fg:#1c1917;--muted:#57534e;--line:#d6d3d1;--card:#fff;--accent:#1d4ed8}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#1c1917;--fg:#f5f5f4;--muted:#a8a29e;--line:#44403c;--card:#292524;--accent:#93c5fd}}
:root[data-theme=dark]{--bg:#1c1917;--fg:#f5f5f4;--muted:#a8a29e;--line:#44403c;--card:#292524;--accent:#93c5fd}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px}h1{margin:0 0 4px;font-size:1.5rem}
.sub{color:var(--muted);margin:0 0 20px}
.tiles{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 14px;cursor:pointer;color:inherit;font:inherit}
.tile[aria-pressed=true]{border-color:var(--accent);outline:1px solid var(--accent)}
.tile b{display:block;font-size:1.3rem}.tile span{color:var(--muted);font-size:.8rem}
.list{display:grid;gap:10px}
.item{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.item h2{margin:0;font-size:1rem}.meta{color:var(--muted);font-size:.8rem;margin:2px 0 6px}
.tag{display:inline-block;border:1px solid var(--line);border-radius:4px;padding:0 6px;font-size:.75rem;margin-right:4px}
.item p{margin:4px 0}.k{color:var(--muted)}
"""

_JS = """
const items=[...document.querySelectorAll('.item')],tiles=[...document.querySelectorAll('.tile')];
tiles.forEach(t=>t.addEventListener('click',()=>{const on=t.getAttribute('aria-pressed')==='true';
tiles.forEach(x=>x.setAttribute('aria-pressed','false'));
if(!on)t.setAttribute('aria-pressed','true');
const f=on?'all':t.dataset.status;items.forEach(i=>{i.hidden=f!=='all'&&i.dataset.status!==f})}));
"""


def render_html(ideas: list[dict[str, Any]]) -> str:
    s = summarize(ideas)
    e = html.escape
    tiles = [
        f'<button class="tile" data-status="all" aria-pressed="false"><b>{s["total"]}</b><span>all ideas</span></button>'
    ]
    tiles += [
        f'<button class="tile" data-status="{e(k)}" aria-pressed="false"><b>{v}</b><span>{e(k)}</span></button>'
        for k, v in s["by_status"].items()
    ]
    cards = []
    for i in ideas:
        src = i["source"]
        src_txt = " / ".join(
            str(x) for x in (src.get("type"), src.get("author"), src.get("captured")) if x
        )
        rows = [
            f"<p>{e(i['summary'])}</p>",
            f'<p><span class="k">Result:</span> {e(str(i.get("result") or "not yet tested"))}</p>',
        ]
        rows.append(f'<p><span class="k">Latest run:</span> {e(_run_line(latest_run(i)))}</p>')
        if i.get("revisit_when"):
            rows.append(f'<p><span class="k">Revisit when:</span> {e(str(i["revisit_when"]))}</p>')
        cards.append(
            f'<article class="item" data-status="{e(i["status"])}">'
            f"<h2>{e(i['id'])} - {e(i['title'])}</h2>"
            f'<div class="meta"><span class="tag">{e(i["status"])}</span>'
            f'<span class="tag">verdict: {e(i["verdict"])}</span>'
            f'<span class="tag">{e(i["strategy_category"])}</span>'
            f'<span class="tag">{e(i["horizon"])}</span> source: {e(src_txt)}'
            f" - reviewed {e(i['last_reviewed'])}</div>{''.join(rows)}</article>"
        )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Strategy Idea Registry</title><style>{_CSS}</style></head><body><main>"
        f'<h1>Strategy Idea Registry</h1><p class="sub">{s["total"]} ideas. '
        f"Last reviewed {e(s['last_reviewed'])}. Click a count to filter.</p>"
        f'<div class="tiles">{"".join(tiles)}</div><div class="list">{"".join(cards)}</div>'
        f"</main><script>{_JS}</script></body></html>\n"
    )


def _outputs(root: Path, ideas: list[dict[str, Any]]) -> dict[Path, str]:
    base = root / "research"
    return {
        base / "REGISTRY.md": render_markdown(ideas),
        base / "registry.json": render_json(ideas),
        base / "site" / "index.html": render_html(ideas),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--check", action="store_true", help="verify only; exit 1 if stale or invalid"
    )
    args = parser.parse_args(argv)

    try:
        ideas = load_ideas(args.root)
    except RegistryError as exc:
        sys.stderr.write(f"Invalid ideas:\n{exc}\n")
        return 1

    outputs = _outputs(args.root, ideas)
    if args.check:
        stale = [
            p
            for p, text in outputs.items()
            if not p.exists() or p.read_text(encoding="utf-8") != text
        ]
        if stale:
            sys.stderr.write(
                "Stale generated files (re-run build_registry): "
                + ", ".join(str(p) for p in stale)
                + "\n"
            )
            return 1
        return 0

    for path, text in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    sys.stdout.write(f"Registry built: {len(ideas)} ideas\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
