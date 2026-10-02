"""Run an idea's backtest and store the result as an immutable run record.

    uv run --group analysis python -m signal_engine.backtest.record_run --idea IDEA-012
    uv run --group analysis python -m signal_engine.backtest.record_run --idea IDEA-012 \\
        --parent 2026-09-30-ema9-vwap-cross-v1 --note "wider stop" --trials 4
    uv run --group analysis python -m signal_engine.backtest.record_run --all

Each run is written to `research/ideas/<idea-folder>/runs/<run_id>.json` using the schema
described in research/README.md. A run file is never overwritten: re-running an idea after
a change creates the next version, and `--parent` names the run it came from so the results
page can show what changed.

The run uses the strategy's shipped default parameters and the engine defaults the
`python -m signal_engine.backtest` CLI uses (5-minute bars and 16 bps for intraday, daily
bars and 24 bps for swing), on every symbol the Historify store holds - never a subset.

`--trials` is how many configurations were searched to arrive at the strategy being
recorded. Nothing in the repository records that number, so when it is omitted the record
carries 1 and says so: the hurdle it shows is a lower bound, not the true bar.
"""

from __future__ import annotations

import argparse
import dataclasses
import functools
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from signal_engine.backtest import regime, regime_data
from signal_engine.backtest.baseline import (
    INTRADAY_MAX_TRADES,
    random_entry_baseline,
    random_entry_baseline_intraday,
    sample_trades,
)
from signal_engine.backtest.costs import CostModel
from signal_engine.backtest.tearsheet import build_run_record
from signal_engine.backtest.types import HOLDOUT_OOS_FRACTION
from signal_engine.research.build_registry import _parse_frontmatter
from signal_engine.research.intake import spec
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


def deployed_cost_model(kind: str, config_path: Path | None = None) -> CostModel:
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
        slippage_bps_per_side=SLIPPAGE_BPS_PER_SIDE,
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
    if runs_dir.is_dir():
        for path in runs_dir.glob("*.json"):
            match = pattern.match(path.stem)
            if match:
                highest = max(highest, int(match.group(1)))
    return f"{run_date}-{slug}-v{highest + 1}"


def _existing_run_ids(runs_dir: Path) -> set[str]:
    return {p.stem for p in runs_dir.glob("*.json")} if runs_dir.is_dir() else set()


def write_run(runs_dir: Path, record: dict[str, Any]) -> Path:
    """Write `record` as `<run_id>.json`, atomically, refusing to replace an existing run.

    The text is fully built before anything touches disk, so a NaN fails cleanly. It is
    written to a temp file then hard-linked into place: link() fails if the target exists,
    which makes "create only if absent" a single atomic step.
    """
    text = json.dumps(record, indent=2, allow_nan=False) + "\n"
    runs_dir.mkdir(parents=True, exist_ok=True)
    target = runs_dir / f"{record['run_id']}.json"
    fd, tmp_name = tempfile.mkstemp(dir=runs_dir, prefix=".run-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.link(tmp_name, target)
    finally:
        os.unlink(tmp_name)
    return target


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


def _cost_model_or_none(kind: str) -> CostModel | None:
    """The deployed cost model, or None (logged) when config.yaml cannot supply it."""
    try:
        return deployed_cost_model(kind)
    except (KeyError, OSError, ValueError, yaml.YAMLError):
        logger.exception("Per-trade cost model unavailable; record has no cost_model block")
        return None


def execute_backtest(key: str) -> Execution:
    """Run strategy `key` with shipped defaults on the full Historify universe."""
    from signal_engine.backtest import __main__ as cli
    from signal_engine.backtest import data
    from signal_engine.backtest.harness import Backtest
    from signal_engine.backtest.swing_engine import SwingRunConfig, simulate_swing
    from signal_engine.backtest.types import RunConfig

    cli._register()
    cls, params_cls, kind = cli.REGISTRY[key]
    strategy, params = cls(), params_cls()
    if kind == "swing":
        frames = data.from_historify_daily(min_sessions=500)
        run = SwingRunConfig(cost_bps=SWING_COST_BPS)
        universe, bar_size = len(frames), "1d"
        bt = Backtest(strategy, frames, run, engine=simulate_swing, release_raw=True)
    else:
        frames = data.from_historify(interval="5m")
        run = RunConfig(cost_bps=INTRADAY_COST_BPS).with_(**getattr(strategy, "run_overrides", {}))
        universe, bar_size = len(frames), "5m"
        bt = Backtest(strategy, frames, run, release_raw=True)
    days = [_iso(d) for d in bt.days]
    trades = bt.trades(params, "all")
    baseline = None
    if kind != "swing":
        # Intraday: score a seeded sample of trades, loading only the symbols that sample
        # touches. Reloading every symbol's 5-minute history is what made this unaffordable.
        sample = sample_trades(trades, INTRADAY_MAX_TRADES)
        if sample:
            baseline = random_entry_baseline_intraday(
                sample,
                data.from_historify(symbols=sorted({t.symbol for t in sample}), interval="5m"),
                float(run.cost_bps),
                population_trades=len(trades),
            )
    if kind == "swing":
        # The engine released its raw frames, so reload the (small, daily) panel.
        baseline = random_entry_baseline(
            trades, data.from_historify_daily(min_sessions=500), float(run.cost_bps)
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
        cost_model=_cost_model_or_none(kind),
    )


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


def record_idea(
    root: Path,
    idea_id: str,
    *,
    execute: Callable[[str], Execution],
    run_date: str,
    supported: Iterable[str] | None = None,
    parent: str | None = None,
    note: str = "",
    trials: int | None = None,
    oos_start: str | None = None,
    position_caps: Sequence[int] = DEFAULT_POSITION_CAPS,
    validation: Sequence[str] = (),
    regimes: Callable[[], regime_data.RegimeInputs] | None = None,
) -> Path:
    """Run one idea and write its record. Returns the path written.

    `regimes` loads the NIFTY and VIX inputs for the per-regime block. Left empty the record
    has no `regimes` block; a loader that raises is logged, the record is still written, and
    its notes say the regime analysis is missing and why.

    Raises KeyError for an unknown idea, Unsupported when its module has no trade-list
    engine, spec.SpecError when a new idea has no complete spec.yaml, ValueError for a
    parent that is not a run of this idea, and FileExistsError if the run id was taken
    in the meantime. Nothing is executed or written in the first four cases.
    """
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
    spec.check_idea(idea_id, folder)
    runs_dir = folder / "runs"
    if parent is not None and parent not in _existing_run_ids(runs_dir):
        raise ValueError(f"parent run '{parent}' is not a run of {idea_id}")

    execution = execute(key)
    n_trials = trials if trials is not None else 1
    trials_note = (
        f"n_trials={n_trials} as stated by the caller."
        if trials is not None
        else "n_trials defaulted to 1: the number of variants searched is not recorded, "
        "so hurdle_t is a lower bound."
    )
    regime_days, regime_source, regime_note = _regime_inputs(regimes)
    record = build_run_record(
        execution.trades,
        run_id=next_run_id(runs_dir, run_date, run_slug(folder)),
        run_date=run_date,
        code_version=git_version(root),
        data=execution.data,
        period=execution.period,
        costs=execution.costs,
        params=jsonable(execution.params),
        parent_run_id=parent,
        change_note=note or ("baseline" if parent is None else ""),
        n_trials=n_trials,
        oos_start=oos_start or default_oos_start(execution.days, execution.trades),
        position_caps=position_caps,
        baseline=execution.baseline,
        validation=validation,
        horizon=execution.params.get("engine", "intraday"),
        regime_days=regime_days,
        regime_source=regime_source,
        cost_model=execution.cost_model,
        embargo_sessions=EMBARGO_SESSIONS,
        notes=(
            f"Shipped default configuration on {execution.data['universe']}. {trials_note}"
            f"{regime_note}"
        ),
    )
    return write_run(runs_dir, record)


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
) -> int:
    parser = argparse.ArgumentParser(prog="signal_engine.backtest.record_run")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--idea", help="idea id, e.g. IDEA-012")
    target.add_argument("--all", action="store_true", help="every idea with a backtest module")
    parser.add_argument("--parent", help="run id this run descends from (with --idea)")
    parser.add_argument("--note", default="", help="what changed since the parent")
    parser.add_argument("--trials", type=int, help="configurations searched in total")
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
    parser.add_argument("--run-date", default=datetime.now(IST).date().isoformat())
    args = parser.parse_args(argv)
    if args.all and (args.parent or args.oos_start):
        parser.error("--parent and --oos-start apply to a single idea, not --all")
    try:
        position_caps = parse_caps(args.caps)
    except ValueError as exc:
        parser.error(str(exc))

    keys = set(supported) if supported is not None else supported_keys()
    run = execute or execute_backtest
    load_regimes = None
    if not args.no_regimes:
        # An injected `execute` is a test double: do not reach for the real data stores.
        load_regimes = regimes or (regime_data.load_regime_inputs if execute is None else None)
        if load_regimes is not None:
            load_regimes = functools.cache(load_regimes)  # one load serves every idea in --all
    ids = _ideas_with_module(args.root) if args.all else [args.idea]
    failures = 0
    for idea_id in ids:
        try:
            path = record_idea(
                args.root,
                idea_id,
                execute=run,
                run_date=args.run_date,
                supported=keys,
                parent=args.parent,
                note=args.note,
                trials=args.trials,
                oos_start=args.oos_start,
                position_caps=position_caps,
                validation=args.validation,
                regimes=load_regimes,
            )
        except (Unsupported, spec.SpecError) as exc:
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
