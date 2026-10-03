"""Run an idea's backtest and store the result as an immutable run record.

    uv run --group analysis python -m signal_engine.backtest.record_run --idea IDEA-012
    uv run --group analysis python -m signal_engine.backtest.record_run --idea IDEA-012 \\
        --parent 2026-09-30-ema9-vwap-cross-v1 --note "wider stop" \\
        --external-trials 4 --reason "grid search run in a notebook"
    uv run --group analysis python -m signal_engine.backtest.record_run --all

Each run is written to `research/ideas/<idea-folder>/runs/<run_id>.json` using the schema
described in research/README.md. A run file is never overwritten: re-running an idea after
a change creates the next version, and `--parent` names the run it came from so the results
page can show what changed.

The run uses the strategy's shipped default parameters and the engine defaults the
`python -m signal_engine.backtest` CLI uses (5-minute bars and 16 bps for intraday, daily
bars and 24 bps for swing), on every symbol the Historify store holds - never a subset.

`n_trials` comes from the idea's `trials.yaml` ledger (research/intake/trials.py): one entry per
distinct material configuration (rules, params, execution, cost fingerprints), plus
the variants searched outside the system, added with `--external-trials N --reason TEXT`. The
ledger is written right after the backtest executes and before any artifact is built: a run that
crashed counts as no trial, a run that executed but failed to write its artifacts does count (it
was evaluated), and a re-run of the same configuration adds nothing.

Every run also gets the validity checks (data quality, config, lineage) and, unless
`--no-truncation` is given, a look-ahead check that re-runs three symbols on truncated data.
"""

from __future__ import annotations

import argparse
import dataclasses
import functools
import inspect
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from signal_engine.backtest import ledgers as ledgers_mod
from signal_engine.backtest import manifest as manifest_mod
from signal_engine.backtest import regime, regime_data, run_artifact
from signal_engine.backtest.baseline import (
    INTRADAY_MAX_TRADES,
    random_entry_baseline,
    random_entry_baseline_intraday,
    sample_trades,
)
from signal_engine.backtest.costs import CostModel, decompose_trades
from signal_engine.backtest.data_collect import CLEAN_KEYS, DataCollector
from signal_engine.backtest.dq_run import build_data_quality
from signal_engine.backtest.eligibility import (
    MEMBERSHIP_SOURCE,
    SURVIVORSHIP_BIAS,
    EligibilityPolicy,
    EligibilityResult,
    apply_eligibility,
    compute_eligibility,
)
from signal_engine.backtest.engine import simulate
from signal_engine.backtest.ledgers import LedgerRecorder
from signal_engine.backtest.session import Calendar, derive_calendar
from signal_engine.backtest.tearsheet import build_run_record
from signal_engine.backtest.truncation import truncation_check
from signal_engine.backtest.types import HOLDOUT_OOS_FRACTION
from signal_engine.research import readiness
from signal_engine.research.build_registry import _parse_frontmatter
from signal_engine.research.intake import freeze as intake_freeze
from signal_engine.research.intake import spec, versions
from signal_engine.research.intake import trials as trials_mod
from signal_engine.timeutils import IST
from utils.logging import get_logger

logger = get_logger(__name__)

#: Engine defaults, identical to the `python -m signal_engine.backtest` CLI.
INTRADAY_COST_BPS = 16.0
SWING_COST_BPS = 24.0
OOS_FRACTION = HOLDOUT_OOS_FRACTION  # G3.1: hold out the last 30% to 40% of sessions
EMBARGO_SESSIONS = 1  # G3.5: purge straddling trades and skip one session after the cut
#: Slippage per side for the per-trade cost model, the same working figure as
#: `types.india_intraday_bps` (4 bps for a market order on an F&O-liquid name).
SLIPPAGE_BPS_PER_SIDE = 4.0
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"

_FOLDER = re.compile(r"^idea-(\d{3})-(.+)$")


#: The shape of every run id this module generates; anything else is not a run.
RUN_ID_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}-[a-z0-9-]+-v\d+")


class Unsupported(Exception):
    """The idea's backtest module cannot produce a trade list."""


@dataclass(frozen=True)
class Execution:
    """What one backtest run produced, before it becomes a record."""

    trades: list
    params: dict[str, Any]
    data: dict[str, Any]
    period: dict[str, str]
    costs: dict[str, Any]
    days: list[str]
    baseline: dict[str, Any] | None = None
    cost_model: CostModel | None = None
    symbols: list[str] = field(default_factory=list)
    #: Lineage ledgers of the headline run (signals, orders, fills, exclusions), or None.
    ledgers: dict[str, pd.DataFrame] | None = None
    #: Modelled per-trade cost components, one row per trade, or None without a cost model.
    cost_components: pd.DataFrame | None = None
    #: Problems `LedgerRecorder.reconcile` found against the recorder's own buffers (empty = clean).
    ledger_problems: list[str] = field(default_factory=list)
    #: Trading calendar derived from the loaded frames, or None when the run did not derive one.
    calendar: Calendar | None = None
    #: Verdict-relevant part of the data-quality report: status, affects_validity, items.
    data_quality: dict[str, Any] | None = None
    #: One row per loaded symbol with its eligibility counts (`execution/universe.parquet`).
    universe_frame: pd.DataFrame | None = None
    #: The full data-quality report (`data_quality.json`).
    data_report: dict[str, Any] | None = None
    #: membership_source, survivorship_bias and the eligibility policy applied.
    membership: dict[str, Any] | None = None
    #: Look-ahead check result (`TruncationResult.to_dict()`), or None when it was not run.
    truncation: dict[str, Any] | None = None


#: Symbols the look-ahead check re-runs on truncated data.
TRUNCATION_SYMBOLS = 3


def _evenly_spaced(keys: Sequence[str], n: int) -> list[str]:
    """Up to `n` of the sorted `keys`, evenly spaced (all of them when there are fewer)."""
    keys = sorted(keys)
    if len(keys) <= n:
        return keys
    step = (len(keys) - 1) / (n - 1)
    return [keys[round(i * step)] for i in range(n)]


def deployed_cost_model(
    kind: str, config_path: Path | None = None, slippage_bps_per_side: float | None = None
) -> CostModel:
    """The per-trade cost model for the deployment in `config.yaml`.

    Capital and risk per trade come from `sizing`, exchange and MIS margin from `broker`.
    Intraday is MIS at the broker's leverage (1 / mis_margin_pct); swing is unlevered CNC
    delivery. A missing key raises KeyError naming it: there are no defaults for deployed
    sizing, because a silent default would price trades against capital nobody runs.
    """
    if kind not in ("intraday", "swing"):
        raise ValueError(f"kind must be intraday or swing, got {kind!r}")
    cfg = yaml.safe_load((config_path or CONFIG_PATH).read_text(encoding="utf-8"))

    def need(section: str, key: str) -> Any:
        try:
            return cfg[section][key]
        except (KeyError, TypeError):
            raise KeyError(f"config.yaml is missing {section}.{key}") from None

    if kind == "intraday":
        product, leverage = "MIS", 1.0 / float(need("broker", "mis_margin_pct"))
    else:
        product, leverage = "CNC", 1.0
    return CostModel(
        capital=float(need("sizing", "sandbox_capital")),
        risk_per_trade=float(need("sizing", "risk_per_trade")),
        leverage=leverage,
        product=product,
        exchange=str(need("broker", "exchange")),
        slippage_bps_per_side=(
            SLIPPAGE_BPS_PER_SIDE if slippage_bps_per_side is None else float(slippage_bps_per_side)
        ),
    )


def jsonable(value: Any) -> Any:
    """Convert dataclasses, tuples, sets, dates and numpy scalars into strict JSON."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return [jsonable(v) for v in sorted(value, key=str)]
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "item") and callable(value.item):
        return jsonable(value.item())
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (bool, int, str)):
        return value
    return str(value)


def git_version(root: Path) -> str:
    """Short commit sha of the code that produced a run, `-dirty` if backtest/ has changes.

    "unknown" outside a git checkout. Untracked files count as dirty: a run made from code
    that is not committed cannot be reproduced from a sha alone.
    """
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if head.returncode != 0 or not head.stdout.strip():
            return "unknown"
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", "backtest"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    dirty = status.returncode == 0 and bool(status.stdout.strip())
    return head.stdout.strip() + ("-dirty" if dirty else "")


def idea_folders(root: Path) -> dict[str, Path]:
    """Map IDEA-NNN to its folder under `root`/research/ideas."""
    found: dict[str, Path] = {}
    ideas_dir = root / "research" / "ideas"
    if not ideas_dir.is_dir():
        return found
    for folder in sorted(p for p in ideas_dir.iterdir() if p.is_dir()):
        match = _FOLDER.match(folder.name)
        if match:
            found[f"IDEA-{match.group(1)}"] = folder
    return found


def run_slug(folder: Path) -> str:
    match = _FOLDER.match(folder.name)
    return match.group(2) if match else folder.name


def strategy_key(idea: dict[str, Any], supported: Iterable[str]) -> str | None:
    """The backtest registry key for an idea, or None if it has no trade-list strategy."""
    module = idea.get("backtest_module") or ""
    stem = Path(module).stem
    return stem if module and stem in set(supported) else None


def supported_keys() -> set[str]:
    from signal_engine.backtest import __main__ as cli

    cli._register()
    return set(cli.REGISTRY)


def next_run_id(runs_dir: Path, run_date: str, slug: str) -> str:
    """`<date>-<slug>-vN` where N follows the highest version this idea already has."""
    pattern = re.compile(rf"^\d{{4}}-\d{{2}}-\d{{2}}-{re.escape(slug)}-v(\d+)$")
    highest = 0
    for run_id in _existing_run_ids(runs_dir):
        match = pattern.match(run_id)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{run_date}-{slug}-v{highest + 1}"


def _existing_run_ids(runs_dir: Path) -> set[str]:
    """Ids of flat records plus run folders (a committed folder consumes its id)."""
    if not runs_dir.is_dir():
        return set()
    ids = {p.stem for p in runs_dir.glob("*.json") if not p.name.startswith(".")}
    ids |= {
        p.name
        for p in runs_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".") and not p.name.endswith(".reproductions")
    }
    return ids


def serialise_run(record: Mapping[str, Any]) -> str:
    """The flat record as JSON text. Raises ValueError for NaN or infinity, before any write."""
    return json.dumps(record, indent=2, allow_nan=False) + "\n"


def write_run_text(runs_dir: Path, run_id: str, text: str) -> Path:
    """Place `text` as `<run_id>.json`, atomically, refusing to replace an existing run.

    It is written to a temp file then hard-linked into place: link() fails if the target
    exists, which makes "create only if absent" a single atomic step.
    """
    runs_dir.mkdir(parents=True, exist_ok=True)
    target = runs_dir / f"{run_id}.json"
    fd, tmp_name = tempfile.mkstemp(dir=runs_dir, prefix=".run-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.link(tmp_name, target)
    finally:
        os.unlink(tmp_name)
    return target


def write_run(runs_dir: Path, record: dict[str, Any]) -> Path:
    """Write `record` as `<run_id>.json`, atomically, refusing to replace an existing run.

    The text is fully built before anything touches disk, so a NaN fails cleanly.
    """
    return write_run_text(runs_dir, record["run_id"], serialise_run(record))


def _iso(day: Any) -> str:
    return pd.Timestamp(day).date().isoformat()


def default_oos_start(
    days: Sequence[str], trades: Sequence, fraction: float = OOS_FRACTION
) -> str | None:
    """First day of the last `fraction` of sessions, or None if no honest split exists.

    The split must leave trades on both sides; when the session-based cut does not, the
    cut is taken from the days that actually had trades.
    """
    trade_days = sorted({_iso(t.day) for t in trades})
    if len(trade_days) < 2:
        return None
    ordered = sorted(days)
    if ordered:
        cut = ordered[int(len(ordered) * (1 - fraction))]
        if trade_days[0] < cut <= trade_days[-1]:
            return cut
    return trade_days[int(len(trade_days) * (1 - fraction))]


def _cost_model_or_none(kind: str, slippage_bps_per_side: float | None = None) -> CostModel | None:
    """The deployed cost model, or None (logged) when config.yaml cannot supply it."""
    try:
        return deployed_cost_model(kind, slippage_bps_per_side=slippage_bps_per_side)
    except (KeyError, OSError, ValueError, yaml.YAMLError):
        logger.exception("Per-trade cost model unavailable; record has no cost_model block")
        return None


def _apply_profile_settings(run: Any, cfg: Mapping[str, Any]) -> Any:
    """Overlay the deployment profile's square-off settings on an intraday RunConfig."""
    changes: dict[str, Any] = {}
    for name in ("mis_squareoff_min", "squareoff_margin_min"):
        if name in cfg:
            changes[name] = int(cfg[name])
    if "time_exit_min" in cfg:
        changes["time_exit_min"] = min(int(run.time_exit_min), int(cfg["time_exit_min"]))
    return run.with_(**changes) if changes else run


def _end_of_session(end: str | None) -> str | None:
    """`end` (an ISO date) as the last second of that session, or None.

    The loaders compare `timestamp <= end`, and a bare date is midnight IST, which would drop
    that session's own bars; the closing second keeps the whole session.
    """
    return None if end is None else f"{date.fromisoformat(end).isoformat()} 23:59:59"


#: Symbols whose bars the per-run data-quality audit reads (an even sample of the universe).
DQ_MAX_SYMBOLS = 60


def _last_session(frames: Mapping[str, pd.DataFrame]) -> date | None:
    last = [df.index[-1].date() for df in frames.values() if df is not None and len(df)]
    return max(last) if last else None


def _eligibility_policy(cfg: Mapping[str, Any]) -> EligibilityPolicy | None:
    """The policy from the frozen `execution.eligibility_*` settings, None when none are given.

    A missing key falls back to the platform default, so a partial mapping still applies a
    full policy; no keys at all means the run applies no eligibility drop.
    """
    names = ("eligibility_min_prior_sessions", "eligibility_min_volume", "eligibility_min_price")
    if not any(n in cfg for n in names):
        return None
    from signal_engine.research.intake import execution as intake_execution

    return EligibilityPolicy(
        min_prior_sessions=int(
            cfg.get(
                "eligibility_min_prior_sessions", intake_execution.ELIGIBILITY_MIN_PRIOR_SESSIONS
            )
        ),
        min_volume=float(
            cfg.get("eligibility_min_volume", intake_execution.ELIGIBILITY_MIN_VOLUME)
        ),
        min_price=float(cfg.get("eligibility_min_price", intake_execution.ELIGIBILITY_MIN_PRICE)),
    )


@dataclass(frozen=True)
class _DataLayer:
    """What the data and session layer learned about the loaded frames."""

    calendar: Calendar
    data_report: dict[str, Any] | None
    universe_frame: pd.DataFrame | None
    membership: dict[str, Any] | None
    #: NOT_ELIGIBLE sessions that were dropped: symbol -> {date: reason}.
    dropped: dict[str, dict[date, str]]
    #: What the engine sees: the loaded frames minus the ineligible symbol-sessions.
    frames: dict[str, pd.DataFrame]
    #: The eligibility result, kept so the baselines drop the same sessions; None without a policy.
    eligibility: EligibilityResult | None = None


def _build_data_layer(
    frames: dict[str, pd.DataFrame],
    collector: DataCollector,
    cfg: Mapping[str, Any],
    as_of: date | None = None,
    adjusted_prices: bool = False,
) -> _DataLayer:
    """Calendar, data quality, eligibility and the frames the engine will run on.

    Everything that reads the loaded frames happens here, before the backtest, because the
    harness releases its raw frames as it goes. Data quality is audited on the frames as
    loaded (before any eligibility drop). The calendar ignores sessions after `as_of` (the
    run's `end`; the last loaded session when None). `adjusted_prices` marks the split-adjusted
    daily panel, for which the price floor is not applied.
    """
    calendar = derive_calendar(frames, as_of=as_of if as_of is not None else _last_session(frames))
    report = None
    if collector.interval:
        report = build_data_quality(
            frames,
            collector,
            calendar=calendar.trading_days,
            short_sessions=calendar.short_sessions,
            max_symbols=DQ_MAX_SYMBOLS,
        )
        report = {
            **report,
            "calendar": {
                "source": calendar.source,
                "version": calendar.version,
                "sessions": len(calendar.trading_days),
                "short_sessions": [d.isoformat() for d in calendar.short_sessions],
            },
        }
    policy = _eligibility_policy(cfg)
    if policy is None:
        return _DataLayer(calendar, report, None, None, {}, frames)
    result = compute_eligibility(frames, policy, calendar, adjusted_prices=adjusted_prices)
    kept, dropped = apply_eligibility(frames, result)
    membership = {
        "membership_source": MEMBERSHIP_SOURCE,
        "survivorship_bias": SURVIVORSHIP_BIAS,
        "eligibility_policy": {
            "min_prior_sessions": policy.min_prior_sessions,
            "min_volume": policy.min_volume,
            "min_price": policy.min_price,
        },
    }
    return _DataLayer(calendar, report, result.universe_frame, membership, dropped, kept, result)


def _record_data_exclusions(
    recorder: LedgerRecorder, collector: DataCollector, dropped: Mapping[str, Mapping[date, str]]
) -> None:
    """Write what the data layer removed before the engine ran into the recorder."""
    for symbol, days in sorted(collector.split_days_dropped.items()):
        for day in sorted(days):
            recorder.add_exclusion(
                symbol,
                "symbol_session",
                ledgers_mod.SPLIT_DAY,
                date=day,
                detail="open versus previous close outside 0.75 to 1.33",
            )
    for symbol, sessions in sorted(collector.symbols_dropped_min_sessions.items()):
        recorder.add_exclusion(
            symbol,
            "symbol",
            ledgers_mod.SYMBOL_MIN_SESSIONS,
            detail=f"sessions={int(sessions)}",
        )
    for symbol, counts in sorted(collector.clean_dropped.items()):
        total = sum(int(counts.get(k, 0)) for k in CLEAN_KEYS)
        if total:
            recorder.add_exclusion(
                symbol,
                "symbol",
                ledgers_mod.BAD_BAR,
                detail=" ".join(f"{k}={int(counts.get(k, 0))}" for k in CLEAN_KEYS),
                count=total,
            )
    for symbol, days in sorted(dropped.items()):
        for day, reason in sorted(days.items()):
            recorder.add_exclusion(
                symbol, "symbol_session", ledgers_mod.NOT_ELIGIBLE, date=day, detail=reason
            )


def execute_backtest(
    key: str,
    settings: Mapping[str, Any] | None = None,
    end: str | None = None,
    run_truncation: bool = True,
) -> Execution:
    """Run strategy `key` with shipped defaults on the full Historify universe.

    `settings` is the frozen execution configuration (keys cost_bps, mis_squareoff_min,
    squareoff_margin_min, time_exit_min, slippage_bps_per_side, warmup_bars and the three
    eligibility_* thresholds). Without it the engine defaults apply, no warm-up is set and no
    symbol-session is dropped as ineligible. The profile's square-off values win over the
    strategy's overrides, and the time exit is only ever tightened.

    The loaders report what they dropped through a `DataCollector`. The calendar, the data
    quality report and the eligibility drop are computed from the loaded frames before the
    backtest; the data-layer exclusions are added to the recorder before its frames are built.

    `end` (ISO date) limits every data load, the baseline reloads included, to sessions up to
    and including that day; None loads everything available.

    `run_truncation` keeps small copies of up to three evenly spaced symbols' frames (the
    harness releases the raw ones) and, after the headline run, re-runs them on truncated data
    (`truncation.truncation_check`); the result is `Execution.truncation`. False skips it and
    leaves `Execution.truncation` None, which the validity block records as a check not run.
    """
    cfg = dict(settings or {})
    until = _end_of_session(end)
    from signal_engine.backtest import __main__ as cli
    from signal_engine.backtest import data
    from signal_engine.backtest.harness import Backtest
    from signal_engine.backtest.swing_engine import SwingRunConfig, simulate_swing
    from signal_engine.backtest.types import RunConfig

    cli._register()
    cls, params_cls, kind = cli.REGISTRY[key]
    strategy, params = cls(), params_cls()
    collector = DataCollector()
    if kind == "swing":
        frames = data.from_historify_daily(min_sessions=500, end=until, collector=collector)
        run = SwingRunConfig(cost_bps=cfg.get("cost_bps", SWING_COST_BPS))
        bar_size = "1d"
    else:
        frames = data.from_historify(interval="5m", end=until, collector=collector)
        run = RunConfig(cost_bps=cfg.get("cost_bps", INTRADAY_COST_BPS)).with_(
            **getattr(strategy, "run_overrides", {})
        )
        run = _apply_profile_settings(run, cfg)
        bar_size = "5m"
    if "warmup_bars" in cfg:
        run = run.with_(warmup_bars=int(cfg["warmup_bars"]))
    loaded_symbols, universe = sorted(frames), len(frames)
    layer = _build_data_layer(
        frames,
        collector,
        cfg,
        as_of=date.fromisoformat(end) if end else None,
        adjusted_prices=kind == "swing",
    )
    del frames  # the harness releases its raw frames as it goes; do not keep a second copy
    kept = (
        {s: layer.frames[s].copy() for s in _evenly_spaced(list(layer.frames), TRUNCATION_SYMBOLS)}
        if run_truncation
        else {}
    )
    engine_used = simulate_swing if kind == "swing" else simulate
    bt = Backtest(strategy, layer.frames, run, engine=engine_used, release_raw=True)
    days = [_iso(d) for d in bt.days]
    # The recorder serves this one headline run; no other call below receives one.
    recorder = LedgerRecorder()
    trades = bt.trades(params, "all", recorder=recorder)
    _record_data_exclusions(recorder, collector, layer.dropped)
    ledgers = recorder.to_frames()
    ledger_problems = recorder.reconcile(ledgers, trades)
    truncation = None
    if run_truncation:
        truncation = truncation_check(strategy, params, kept, run, engine=engine_used).to_dict()
        del kept
    cost_model = _cost_model_or_none(kind, cfg.get("slippage_bps_per_side"))
    cost_components = decompose_trades(trades, cost_model) if cost_model is not None else None
    baseline = None
    if kind != "swing":
        # Intraday: score a seeded sample of trades, loading only the symbols that sample
        # touches. Reloading every symbol's 5-minute history is what made this unaffordable.
        sample = sample_trades(trades, INTRADAY_MAX_TRADES)
        if sample:
            baseline = random_entry_baseline_intraday(
                sample,
                _baseline_frames(
                    data.from_historify(
                        symbols=sorted({t.symbol for t in sample}), interval="5m", end=until
                    ),
                    layer,
                    int(run.warmup_bars),
                ),
                float(run.cost_bps),
                population_trades=len(trades),
            )
    if kind == "swing":
        # The engine released its raw frames, so reload the (small, daily) panel.
        baseline = random_entry_baseline(
            trades,
            _baseline_frames(
                data.from_historify_daily(min_sessions=500, end=until), layer, int(run.warmup_bars)
            ),
            float(run.cost_bps),
        )
    return Execution(
        trades=trades,
        params={
            "strategy_class": cls.__name__,
            "engine": kind,
            "strategy": jsonable(params),
            "run": jsonable(run),
        },
        data={
            "source": "historify",
            "as_of": days[-1],
            "universe": f"historify-{universe}-symbols",
            "bar_size": bar_size,
        },
        period={"start": days[0], "end": days[-1]},
        costs={"model": "flat-round-trip-bps", "round_trip_bps": float(run.cost_bps)},
        days=days,
        baseline=baseline,
        cost_model=cost_model,
        symbols=loaded_symbols,
        ledgers=ledgers,
        cost_components=cost_components,
        ledger_problems=ledger_problems,
        calendar=layer.calendar,
        data_quality=_verdict_data_quality(layer.data_report),
        universe_frame=layer.universe_frame,
        data_report=layer.data_report,
        membership=layer.membership,
        truncation=truncation,
    )


def _baseline_frames(
    frames: dict[str, pd.DataFrame], layer: _DataLayer, warmup: int
) -> dict[str, pd.DataFrame]:
    """Reloaded baseline frames on the same footing as the strategy's: ineligible symbol-sessions
    dropped with the run's eligibility result, and the first `warmup` bars of each symbol
    skipped, so a random entry can never land where the strategy could not trade."""
    if layer.eligibility is not None:
        frames, _ = apply_eligibility(frames, layer.eligibility)
    if warmup > 0:
        frames = {s: df.iloc[warmup:] for s, df in frames.items()}
        frames = {s: df for s, df in frames.items() if len(df)}
    return frames


def _verdict_data_quality(report: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The three keys the verdict reads from a full data-quality report."""
    if report is None:
        return None
    return {
        "status": report["status"],
        "affects_validity": report["affects_validity"],
        "items": report["items"],
    }


#: Slot counts every run is also scored at, so a per-trade edge that only exists for an
#: account taking every signal cannot pass for a tradeable one.
DEFAULT_POSITION_CAPS = (3, 5, 10)


def parse_caps(text: str) -> tuple[int, ...]:
    """`"3,5,10"` -> (3, 5, 10). Empty, `none` or `off` switches the cap view off."""
    text = text.strip().lower()
    if text in ("", "none", "off"):
        return ()
    try:
        caps = tuple(int(part) for part in text.split(","))
    except ValueError:
        raise ValueError(f"caps must be a comma list of whole numbers, got {text!r}") from None
    if any(c < 1 for c in caps):
        raise ValueError(f"caps must each be >= 1, got {text!r}")
    return caps


def _regime_inputs(
    loader: Callable[[], regime_data.RegimeInputs] | None,
) -> tuple[pd.DataFrame | None, dict[str, Any] | None, str]:
    """Daily regime labels, their source record, and a note when they could not be built."""
    if loader is None:
        return None, None, ""
    try:
        inputs = loader()
        days = regime.build_regime_days(inputs.close, inputs.vix)
    except Exception as exc:
        logger.exception("Regime analysis unavailable")
        return None, None, f" Regime analysis not recorded: {exc}."
    source = {**inputs.source, "vol_agreement": regime.vol_agreement(days)}
    return days, jsonable(source), ""


#: Frozen resolved field -> `settings` key handed to the execute callable.
_SETTINGS_FIELDS = {
    "cost_bps": "execution.cost_bps_round_trip",
    "mis_squareoff_min": "profile.mis_squareoff_min",
    "squareoff_margin_min": "profile.squareoff_margin_min",
    "time_exit_min": "profile.time_exit_min",
    "slippage_bps_per_side": "execution.slippage_bps_per_side",
    "warmup_bars": "execution.warmup_bars",
    "eligibility_min_prior_sessions": "execution.eligibility_min_prior_sessions",
    "eligibility_min_volume": "execution.eligibility_min_volume",
    "eligibility_min_price": "execution.eligibility_min_price",
}


def session_inputs(frozen: Any, execution: Execution) -> dict[str, Any] | None:
    """The session facts the ledger gates need, read from the frozen configuration.

    None when the run has no ledgers or the data is not intraday bars. `mis_squareoff_min` and
    `squareoff_margin_min` come from the resolved profile (None when the profile lacks them),
    `bar_minutes` from the data bar size, `entry_window` from the spec's session window.
    """
    if execution.ledgers is None:
        return None
    size = str(execution.data.get("bar_size", ""))
    if not (size.endswith("m") and size[:-1].isdigit()):
        return None

    def resolved(name: str) -> int | None:
        item = frozen.resolved.get(name)
        return None if item is None else int(item["value"])

    window = frozen.intent.get("session_window")
    entry = window.get("entry") if isinstance(window, Mapping) else None
    return {
        "mis_squareoff_min": resolved("profile.mis_squareoff_min"),
        "squareoff_margin_min": resolved("profile.squareoff_margin_min"),
        "bar_minutes": int(size[:-1]),
        "entry_window": entry if isinstance(entry, str) else None,
    }


def _execute_frozen(
    execute: Callable[..., Execution],
    key: str,
    frozen: Any,
    end: str | None = None,
    run_truncation: bool = True,
) -> Execution:
    """Run `execute`, passing the frozen settings, `end` and `run_truncation` when it accepts
    them, then verify the cost."""
    settings = {
        name: frozen.resolved[field_name]["value"]
        for name, field_name in _SETTINGS_FIELDS.items()
        if field_name in frozen.resolved
    }
    try:
        accepted = inspect.signature(execute).parameters
    except (TypeError, ValueError):
        accepted = {}
    kwargs: dict[str, Any] = {}
    if "settings" in accepted:
        kwargs["settings"] = settings
    if "end" in accepted:
        kwargs["end"] = end
    if "run_truncation" in accepted:
        kwargs["run_truncation"] = run_truncation
    execution = execute(key, **kwargs)
    expected = frozen.resolved["execution.cost_bps_round_trip"]["value"]
    actual = execution.costs.get("round_trip_bps")
    if actual is None or float(actual) != float(expected):
        raise ValueError(
            "execution did not use the frozen configuration: round_trip_bps "
            f"{actual!r} != frozen {expected!r}"
        )
    return execution


def _check_external(count: int, reason: str | None) -> None:
    """Raise ValueError for a bad external trial count or a count given without a reason."""
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError(f"external_trials must be an integer >= 0, got {count!r}")
    if count > 0 and not (isinstance(reason, str) and reason.strip()):
        raise ValueError("external_trials needs an external_reason saying where they came from")
    if count == 0 and reason is not None:
        raise ValueError("external_reason was given without external_trials")


def record_idea(
    root: Path,
    idea_id: str,
    *,
    execute: Callable[[str], Execution],
    run_date: str,
    supported: Iterable[str] | None = None,
    parent: str | None = None,
    note: str = "",
    external_trials: int = 0,
    external_reason: str | None = None,
    oos_start: str | None = None,
    position_caps: Sequence[int] = DEFAULT_POSITION_CAPS,
    validation: Sequence[str] = (),
    regimes: Callable[[], regime_data.RegimeInputs] | None = None,
    parent_version: str | None = None,
    version_label: str | None = None,
    allow_dirty: bool = False,
    git_state_fn: Callable[[Path], dict[str, Any]] | None = None,
    dataset_fingerprint_fn: Callable[[Sequence[str], str, str], str] | None = None,
    readiness_missing_fn: Callable[[], list[str]] | None = None,
    now: datetime | None = None,
    frozen: intake_freeze.FrozenSpec | None = None,
    end: str | None = None,
    run_truncation: bool = True,
) -> Path:
    """Run one idea, write its immutable run folder and then the flat record. Returns the
    flat record's path.

    The order is: key check, parent check, dirty check, freeze, version dry run, execute,
    run id, record build, second git read, then everything that can raise is built and validated
    in memory (fingerprint, readiness, environment, report, manifest, flat record text), then
    version registration, folder commit and the flat record. Anything raised before the version
    registration leaves no folder, no flat record, no version and no consumed run id. A tree
    that changed during the run raises DirtyTreeError unless `allow_dirty`. `allow_dirty` records a run made from
    uncommitted result-affecting code as non-reproducible. The `*_fn` hooks default to the real
    `manifest.git_state`, `manifest.dataset_fingerprint` and `readiness.missing_p1`, looked up
    at call time.

    `frozen` is a FrozenSpec to run instead of freezing the idea's current spec (a rerun of a
    recorded run): the freeze step, the version dry run and the version registration are
    skipped because its version is already registered; every other check still applies. `end`
    (ISO date) is passed to an `execute` that accepts it, limiting the data to that session.

    `external_trials` (with a non-empty `external_reason`) are variants searched outside the
    system; they are added to the idea's `trials.yaml` ledger together with this run's
    configuration, right after the execution succeeds and before artifacts are built (see the
    module docstring). `n_trials` is the ledger's material entries plus its external count. `run_truncation=False` skips the look-ahead check; the validity block then
    lists no 'truncation' in `checks_run`.

    `regimes` loads the NIFTY and VIX inputs for the per-regime block. Left empty the record
    has no `regimes` block; a loader that raises is logged, the record is still written, and
    its notes say the regime analysis is missing and why.

    Raises KeyError for an unknown idea, Unsupported when its module has no trade-list
    engine, spec.SpecError when a new idea has no complete spec.yaml, ValueError for a
    parent that is not a run of this idea, and FileExistsError if the run id was taken
    in the meantime, and ValueError for external trials without a reason. Nothing is executed or
    written in the first four cases.
    """
    _check_external(external_trials, external_reason)
    folders = idea_folders(root)
    if idea_id not in folders:
        raise KeyError(idea_id)
    folder = folders[idea_id]
    idea = _parse_frontmatter(folder / "idea.md")
    key = strategy_key(idea, supported if supported is not None else supported_keys())
    if key is None:
        raise Unsupported(
            f"{idea_id}: backtest module {idea.get('backtest_module')!r} has no trade-list "
            "engine, so it cannot produce a run record"
        )
    runs_dir = folder / "runs"
    if parent is not None and parent not in _existing_run_ids(runs_dir):
        raise ValueError(f"parent run '{parent}' is not a run of {idea_id}")
    run_artifact.require_aware(now)
    git_fn = git_state_fn or manifest_mod.git_state
    git = git_fn(root)
    run_artifact.check_clean(git, allow_dirty)
    # Freeze is the last gate before anything executes: it refuses an idea that is not Backtest
    # Ready. The version is only checked here (a bad parent or label fails before the backtest
    # runs) and is registered once the record is built, so a failed run leaves no orphan version.
    version_args = {"parent": parent_version, "label": version_label, "note": note}
    if frozen is None:
        frozen = intake_freeze.freeze(folder, idea_id=idea_id, register_version=False)
        intake_freeze.register(folder, frozen, dry_run=True, **version_args)
        register_version = True
    else:
        register_version = False

    execution = _execute_frozen(execute, key, frozen, end=end, run_truncation=run_truncation)
    regime_days, regime_source, regime_note = _regime_inputs(regimes)
    # The id is chosen only now, after the long backtest, to keep the window in which another
    # process could take it as short as possible.
    run_id = next_run_id(runs_dir, run_date, run_slug(folder))
    stamp = now or datetime.now(IST)
    cost_model_json = jsonable(execution.cost_model)
    material = run_artifact.material_fingerprints(frozen, execution, cost_model_json)
    # The trial is written now, right after the execution succeeded and before any artifact is
    # built: a trial is an EVALUATED change. A run whose execution crashed counts nothing; a run
    # that executed but whose artifact write then failed DOES count (it was evaluated). The write
    # is idempotent, so a retry never double counts.
    try:
        registered = trials_mod.register_material(folder, material, run_id, now=stamp)
        if external_trials:
            registered = trials_mod.add_external(
                folder, external_trials, str(external_reason), now=stamp
            )
    except Exception:
        logger.exception("Could not write the trial ledger of %s for run %s", idea_id, run_id)
        raise
    trials_block = {
        "material": registered.count.material,
        "external": registered.count.external,
        "total": registered.count.total,
        "source": "trials.yaml",
    }
    n_trials = trials_block["total"]
    trials_note = (
        f"n_trials={n_trials} from trials.yaml: {trials_block['material']} material "
        f"configuration(s) and {trials_block['external']} external."
    )
    record = build_run_record(
        execution.trades,
        run_id=run_id,
        run_date=run_date,
        code_version=git_version(root),
        data=execution.data,
        period=execution.period,
        costs=execution.costs,
        params=jsonable(execution.params),
        parent_run_id=parent,
        change_note=note or ("baseline" if parent is None else ""),
        n_trials=n_trials,
        trials=trials_block,
        truncation=execution.truncation,
        oos_start=oos_start or default_oos_start(execution.days, execution.trades),
        position_caps=position_caps,
        baseline=execution.baseline,
        validation=validation,
        horizon=execution.params.get("engine", "intraday"),
        regime_days=regime_days,
        regime_source=regime_source,
        cost_model=execution.cost_model,
        embargo_sessions=EMBARGO_SESSIONS,
        ledgers=execution.ledgers,
        ledger_problems=execution.ledger_problems,
        session=session_inputs(frozen, execution),
        data_quality=getattr(execution, "data_report", None),
        calendar=getattr(execution, "calendar", None),
        membership=getattr(execution, "membership", None),
        notes=(
            f"Shipped default configuration on {execution.data['universe']}. {trials_note}"
            f"{regime_note}"
        ),
    )
    # The tree may have changed while the backtest ran: read it again, then build and validate
    # everything that can raise before anything is written.
    engine = run_artifact.settle_git(git, git_fn(root), allow_dirty)
    readiness_missing = (readiness_missing_fn or readiness.missing_p1)()
    # The manifest records the dry-run version; the real registration must agree with it.
    dry = (
        intake_freeze.register(folder, frozen, dry_run=True, **version_args)
        if register_version
        else frozen
    )
    prepared = run_artifact.prepare_run_artifact(
        runs_dir=runs_dir,
        idea_folder=folder,
        idea_id=idea_id,
        record=record,
        frozen=dry,
        execution=execution,
        cost_model_json=cost_model_json,
        engine=engine,
        dataset_fingerprint_fn=dataset_fingerprint_fn or manifest_mod.dataset_fingerprint,
        readiness_missing=readiness_missing,
        parent_run_id=parent,
        now=stamp,
    )
    flat_text = serialise_run(prepared.flat)
    if register_version:
        registered = intake_freeze.register(folder, frozen, **version_args)
        if (registered.version, registered.parent_version) != (dry.version, dry.parent_version):
            raise RuntimeError(
                f"version registered as {registered.version} but the manifest recorded {dry.version}"
            )
    prepared.commit(runs_dir)
    return write_run_text(runs_dir, run_id, flat_text)


class RerunError(Exception):
    """The run to rerun or reproduce does not exist or has no manifest to rebuild from."""


class InvalidRunId(RerunError, ValueError):
    """A run id from the command line that is not shaped like a generated run id."""


def validate_run_id(run_id: str) -> str:
    """Return `run_id` if it is `<date>-<slug>-v<n>`, else raise InvalidRunId.

    Checked before any path is built from it, so '../x', an absolute path or a name with a
    slash or newline never reaches the filesystem.
    """
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise InvalidRunId(f"'{run_id}' is not a valid run id (expected <date>-<slug>-v<n>)")
    return run_id


def find_run_folder(root: Path, run_id: str) -> tuple[str, Path]:
    """The (idea id, run folder) of `run_id`. Raises RerunError if no idea has that run folder
    or the folder has no manifest (a legacy run, which cannot be rebuilt)."""
    validate_run_id(run_id)
    for idea_id, folder in idea_folders(root).items():
        run_folder = folder / "runs" / run_id
        if run_folder.is_dir():
            if not (run_folder / "manifest.json").is_file():
                raise RerunError(f"run {run_id} has no manifest.json, so it cannot be rebuilt")
            return idea_id, run_folder
    raise RerunError(f"run '{run_id}' is not a run folder of any idea under {root}")


def record_rerun(
    root: Path, run_id: str, *, end: str | None = None, note: str = "", **kwargs: Any
) -> Path:
    """Record a new run of the frozen strategy `run_id` was made from, against current data.

    The strategy version is the run's own, the parent is `run_id`, and the result is classified
    by the usual change classes. `end` limits the data to that session; None uses everything
    available. The other keyword arguments are those of `record_idea`. Raises RerunError for an
    unknown run or one without a manifest, and FreezeError if its folder does not verify.
    """
    if kwargs.get("external_trials") or kwargs.get("external_reason") is not None:
        raise ValueError(
            "a rerun cannot add external trials; use `trials add IDEA-n N --reason TEXT`"
        )
    kwargs.pop("external_trials", None)
    kwargs.pop("external_reason", None)
    idea_id, run_folder = find_run_folder(root, run_id)
    frozen = intake_freeze.from_run_folder(run_folder)
    return record_idea(
        root,
        idea_id,
        frozen=frozen,
        parent=run_id,
        end=end,
        note=note or f"rerun of {run_id}",
        **kwargs,
    )


def _reproduce_main(
    args: argparse.Namespace,
    run: Callable[..., Execution],
    keys: Iterable[str],
    hooks: Mapping[str, Any],
) -> int:
    from signal_engine.backtest import reproduce as reproduce_mod

    try:
        _, run_folder = find_run_folder(args.root, args.reproduce)
    except RerunError:
        logger.exception("Cannot reproduce %s", args.reproduce)
        return 1
    report = reproduce_mod.reproduce(
        run_folder,
        root=args.root,
        execute=run,
        supported=keys,
        git_state_fn=hooks["git_state_fn"],
        dataset_fingerprint_fn=hooks["dataset_fingerprint_fn"],
    )
    if report.reproducible:
        sys.stdout.write(
            f"reproducible: {report.run_id} ({report.compared_trades} trades identical)\n"
        )
        return 0
    sys.stdout.write(f"not reproducible: {report.run_id}: {'; '.join(report.reasons)}\n")
    return 1


def _summary_line(path: Path) -> str:
    record = json.loads(path.read_text(encoding="utf-8"))
    m = record["metrics"]
    return (
        f"wrote {path.name}: {m['trades']} trades, gross {m['gross_bps']} / "
        f"net {m['net_bps']} bps, t {m.get('t_stat', 'n/a')} vs hurdle {m.get('hurdle_t', 'n/a')}\n"
    )


def _ideas_with_module(root: Path) -> list[str]:
    ids = []
    for idea_id, folder in idea_folders(root).items():
        try:
            if _parse_frontmatter(folder / "idea.md").get("backtest_module"):
                ids.append(idea_id)
        except (OSError, ValueError):
            logger.exception("Could not read %s", folder / "idea.md")
    return ids


def main(
    argv: Sequence[str] | None = None,
    *,
    execute: Callable[[str], Execution] | None = None,
    supported: Iterable[str] | None = None,
    regimes: Callable[[], regime_data.RegimeInputs] | None = None,
    git_state_fn: Callable[[Path], dict[str, Any]] | None = None,
    dataset_fingerprint_fn: Callable[[Sequence[str], str, str], str] | None = None,
    readiness_missing_fn: Callable[[], list[str]] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="signal_engine.backtest.record_run")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--idea", help="idea id, e.g. IDEA-012")
    target.add_argument("--all", action="store_true", help="every idea with a backtest module")
    target.add_argument(
        "--rerun", metavar="RUN_ID", help="rerun this run's frozen strategy on current data"
    )
    target.add_argument(
        "--reproduce", metavar="RUN_ID", help="rerun this run on its own data and compare trades"
    )
    parser.add_argument("--end", help="YYYY-MM-DD last session to load (with --rerun)")
    parser.add_argument("--parent", help="run id this run descends from (with --idea)")
    parser.add_argument("--note", default="", help="what changed since the parent")
    parser.add_argument("--parent-version", help="strategy version a new version descends from")
    parser.add_argument("--version", help="branch label for a new strategy version, e.g. v3b")
    parser.add_argument("--trials", help=argparse.SUPPRESS)  # removed: rejected with a pointer
    parser.add_argument(
        "--external-trials",
        type=int,
        default=0,
        metavar="N",
        help="variants searched outside the system, added to trials.yaml (needs --reason)",
    )
    parser.add_argument("--reason", help="where the --external-trials came from")
    parser.add_argument(
        "--no-truncation",
        action="store_true",
        help="skip the look-ahead (truncation) check; validity then lists it as not run",
    )
    parser.add_argument("--oos-start", help="YYYY-MM-DD; default splits the last 30 percent")
    parser.add_argument(
        "--caps",
        default=",".join(str(c) for c in DEFAULT_POSITION_CAPS),
        help="slot counts to also score the run at, e.g. 3,5,10; 'none' to skip",
    )
    parser.add_argument("--validation", action="append", default=[], help="repeatable")
    parser.add_argument(
        "--no-regimes", action="store_true", help="skip the per-regime block (no network needed)"
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="record a run from uncommitted code (marked non-reproducible, not promotable)",
    )
    parser.add_argument("--run-date", default=datetime.now(IST).date().isoformat())
    args = parser.parse_args(argv)
    if args.trials is not None:
        parser.error(
            "--trials was removed: n_trials now comes from the idea's trials.yaml ledger. "
            "Use --external-trials N --reason TEXT for variants searched outside the system."
        )
    if args.external_trials < 0 or (args.external_trials == 0 and args.reason is not None):
        parser.error("--external-trials must be a positive integer, and --reason needs it")
    if args.external_trials and not (args.reason and args.reason.strip()):
        parser.error("--external-trials needs --reason saying where those variants came from")
    if (args.all or args.rerun) and args.external_trials:
        parser.error(
            "--external-trials applies to a new run of a single idea, not --all or --rerun; "
            "add external trials with: trials add IDEA-n N --reason TEXT"
        )
    if args.end and not args.rerun:
        parser.error("--end applies to --rerun only")
    if args.end:
        try:
            date.fromisoformat(args.end)
        except ValueError:
            parser.error(f"--end must be YYYY-MM-DD, got {args.end!r}")
    if (args.rerun or args.reproduce) and (args.parent or args.parent_version or args.version):
        parser.error("--parent, --parent-version and --version do not apply to --rerun/--reproduce")
    if args.all and (args.parent or args.oos_start):
        parser.error("--parent and --oos-start apply to a single idea, not --all")
    try:
        position_caps = parse_caps(args.caps)
    except ValueError as exc:
        parser.error(str(exc))

    keys = set(supported) if supported is not None else supported_keys()
    run = execute or execute_backtest
    if args.reproduce:
        hooks = {"git_state_fn": git_state_fn, "dataset_fingerprint_fn": dataset_fingerprint_fn}
        return _reproduce_main(args, run, keys, hooks)
    load_regimes = None
    if not args.no_regimes:
        # An injected `execute` is a test double: do not reach for the real data stores.
        load_regimes = regimes or (regime_data.load_regime_inputs if execute is None else None)
        if load_regimes is not None:
            load_regimes = functools.cache(load_regimes)  # one load serves every idea in --all
    ids = _ideas_with_module(args.root) if args.all else [args.idea or args.rerun]
    failures = 0
    for idea_id in ids:
        try:
            common = {
                "execute": run,
                "run_date": args.run_date,
                "supported": keys,
                "note": args.note,
                "external_trials": args.external_trials,
                "external_reason": args.reason,
                "run_truncation": not args.no_truncation,
                "oos_start": args.oos_start,
                "position_caps": position_caps,
                "validation": args.validation,
                "regimes": load_regimes,
                "allow_dirty": args.allow_dirty,
                "git_state_fn": git_state_fn,
                "dataset_fingerprint_fn": dataset_fingerprint_fn,
                "readiness_missing_fn": readiness_missing_fn,
            }
            if args.rerun:
                common.pop("external_trials")
                common.pop("external_reason")
                path = record_rerun(args.root, args.rerun, end=args.end, **common)
            else:
                path = record_idea(
                    args.root,
                    idea_id,
                    parent=args.parent,
                    parent_version=args.parent_version,
                    version_label=args.version,
                    **common,
                )
        except (
            RerunError,
            Unsupported,
            run_artifact.DirtyTreeError,
            spec.SpecError,
            intake_freeze.FreezeError,
            versions.VersionError,
        ) as exc:
            if args.all:
                sys.stdout.write(f"skipped {exc}\n")
                continue
            logger.exception("Cannot record %s", idea_id)
            failures += 1
        except Exception:
            logger.exception("Backtest run failed for %s", idea_id)
            failures += 1
        else:
            sys.stdout.write(_summary_line(path))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
