"""G4.1 parameter perturbation: does the result survive a 20% move in each tunable parameter?

Design (owner-approved 2026-10-08):

  DECLARATION.  A strategy lists every parameter in `param_meta`: tunable ones with a kind
                (int or float) and inclusive bounds, structural ones with a reason. Nothing is
                inferred from types. A missing, extra or out-of-bounds declaration is a
                declaration error and the gate is not evaluated.
  NEIGHBOURS.   One parameter at a time, at 0.8x and 1.2x of base, in declaration order. A float
                keeps its full precision (cleaned to 10 significant digits). An int rounds to the
                nearest integer, ties away from zero; if that equals base it steps by one in the
                direction of the move. The value is then clamped to the bounds; a value that
                equals base after that is dropped and listed, never silently ignored.
  METRIC.       Mean net R per trade on the full headline window at 1.0x cost, under the same
                statutory cost model as the base run. Net bps per trade is stored as a secondary
                figure, and the out-of-sample figures are stored for diagnostics only.
  BASE.         A base that is not net positive fails the gate and no variant is run.
  ISOLATION.    Each variant runs a deep copy of the strategy and its own params object over
                frames loaded exactly as the headline run saw them. The base is replayed first and
                must equal the headline run, otherwise the stage stops and the gate is not
                evaluated.
  TRIALS.       Variants are diagnostics. They are not registered as trials.
"""

from __future__ import annotations

import copy
import dataclasses
import math
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from signal_engine.backtest import tickgrid
from signal_engine.backtest.costs import recost
from signal_engine.backtest.engine import simulate
from signal_engine.backtest.types import Ctx
from signal_engine.research.intake.fingerprints import params_fingerprint
from utils.logging import get_logger

logger = get_logger(__name__)

EVIDENCE_VERSION = 1
MULTIPLIERS = (0.8, 1.2)
#: A neighbour with fewer trades than this is unusable evidence (the G2.1 floor).
MIN_TRADES = 30
#: At least this many usable neighbours are needed for the gate to be evaluated.
MIN_USABLE_NEIGHBOURS = 4
#: Share of neighbours that must be net positive, as the exact fraction 7/10.
POSITIVE_SHARE_NUM, POSITIVE_SHARE_DEN = 7, 10
#: A neighbour must keep at least this fraction of the base net R (it may lose at most 60%).
MIN_RETENTION = 0.4
#: The base is flagged as a peak when it is at least this many times the median usable neighbour.
PEAK_RATIO = 2.0
CHUNK_SYMBOLS = 10

COMPLETE = "complete"
BASE_NOT_POSITIVE = "base_not_positive"
NOT_RUN_STATES = (
    "no_declaration",
    "declaration_error",
    "unsupported",
    "base_replay_mismatch",
    "r_undefined",
)
STATES = (COMPLETE, BASE_NOT_POSITIVE, *NOT_RUN_STATES)

#: Float noise allowance when a rounded value is tested against base.
_EPS = 1e-9


@dataclass(frozen=True)
class ParamMeta:
    """What the strategy author states about one parameter."""

    kind: str = "float"
    min: float | None = None
    max: float | None = None
    tunable: bool = True
    structural: str = ""

    def problems(self, name: str, base: Any) -> list[str]:
        if not self.tunable:
            return (
                []
                if self.structural.strip()
                else [f"{name}: a structural parameter needs a reason"]
            )
        out = []
        if self.kind not in ("int", "float"):
            out.append(f"{name}: kind must be int or float, got {self.kind!r}")
        if self.min is None or self.max is None or not self.min < self.max:
            out.append(f"{name}: bounds must satisfy min < max, got {self.min!r}, {self.max!r}")
        elif isinstance(base, bool) or not isinstance(base, (int, float)):
            out.append(f"{name}: base value {base!r} is not a number")
        elif self.kind == "int" and not isinstance(base, int):
            out.append(f"{name}: declared int but base is {base!r}")
        elif not self.min <= base <= self.max:
            out.append(
                f"{name}: base {base!r} is outside the declared bounds [{self.min}, {self.max}]"
            )
        return out


def tunable(kind: str, low: float, high: float) -> ParamMeta:
    return ParamMeta(kind=kind, min=low, max=high)


def structural(reason: str) -> ParamMeta:
    return ParamMeta(tunable=False, structural=reason)


@dataclass(frozen=True)
class Neighbour:
    parameter: str
    multiplier: float
    value: float | int
    base: float | int


def _clean_float(value: float) -> float:
    return float(f"{value:.10g}")


def _round_half_away(value: float) -> int:
    return (
        int(math.floor(value + 0.5 + _EPS)) if value >= 0 else -int(math.floor(-value + 0.5 + _EPS))
    )


def neighbour_value(
    base: float | int, multiplier: float, meta: ParamMeta
) -> tuple[float | int, str]:
    """The neighbour value and a note ('' normally; 'stepped' when an int needed the +-1 step)."""
    raw = base * multiplier
    note = ""
    if meta.kind == "int":
        value: float | int = _round_half_away(raw)
        if value == base:
            value = base + (1 if multiplier > 1 else -1)
            note = "stepped"
    else:
        value = _clean_float(raw)
    value = min(max(value, meta.min), meta.max)
    if meta.kind == "int":
        value = int(value)
    else:
        value = _clean_float(float(value))
    return value, note


def build_neighbours(
    base: Mapping[str, Any], meta: Mapping[str, ParamMeta]
) -> tuple[list[Neighbour], list[dict[str, Any]], list[str]]:
    """Neighbours in declaration order, the skipped list, and the declaration problems."""
    problems: list[str] = []
    undeclared = [n for n in base if n not in meta]
    extra = [n for n in meta if n not in base]
    if undeclared:
        problems.append("parameters with no declaration: " + ", ".join(undeclared))
    if extra:
        problems.append("declared names that are not parameters: " + ", ".join(extra))
    for name in meta:
        if name in base:
            problems.extend(meta[name].problems(name, base[name]))
    if problems:
        return [], [], problems
    neighbours: list[Neighbour] = []
    skipped: list[dict[str, Any]] = []
    for name in meta:  # declaration order
        m = meta[name]
        if not m.tunable:
            skipped.append({"parameter": name, "reason": f"structural: {m.structural}"})
            continue
        found = 0
        for mult in MULTIPLIERS:
            value, _ = neighbour_value(base[name], mult, m)
            if value == base[name]:
                skipped.append(
                    {
                        "parameter": name,
                        "multiplier": mult,
                        "reason": "at bound: equals base after clamping",
                    }
                )
                continue
            neighbours.append(Neighbour(name, mult, value, base[name]))
            found += 1
        if not found:
            problems.append(f"{name}: tunable but no neighbour is possible inside its bounds")
    return neighbours, skipped, problems


def trade_metrics(trades: Sequence[Any], oos_start: str | None = None) -> dict[str, Any]:
    """Trade count and mean net R and net bps (full window), plus out-of-sample diagnostics."""
    n = len(trades)
    out: dict[str, Any] = {"trades": n}
    if not n:
        return {**out, "net_r": None, "net_bps": None}
    r = np.array([t.r_net for t in trades], dtype=float)
    out["net_r"] = float(np.nanmean(r))
    out["net_bps"] = float(np.mean([t.net_bps for t in trades]))
    if oos_start is not None:
        oos = [t for t in trades if str(t.day)[:10] >= oos_start]
        out["oos_trades"] = len(oos)
        out["oos_net_r"] = float(np.nanmean([t.r_net for t in oos])) if oos else None
    return out


def _fp(parameters: Mapping[str, Any]) -> str:
    return params_fingerprint(dict(parameters))


def run_variants(
    *,
    strategy: Any,
    params: Any,
    meta: Mapping[str, ParamMeta] | None,
    load_frames: Callable[[list[str]], Mapping[str, Any]],
    symbols: Sequence[str],
    run: Any,
    cost_model: Any,
    headline_trades: Sequence[Any],
    oos_start: str | None = None,
    shared: Mapping[str, Any] | None = None,
    chunk_size: int = CHUNK_SYMBOLS,
) -> dict[str, Any]:
    """The `perturbation.json` evidence for a run.

    `load_frames(chunk)` returns the frames the headline run's engine saw for those symbols;
    `headline_trades` are the run's trades on the statutory basis.
    """
    base_params = dataclasses.asdict(params)
    evidence: dict[str, Any] = {
        "version": EVIDENCE_VERSION,
        "state": "",
        "reason": "",
        "metric": "mean net R per trade at 1.0x cost (statutory model); net bps is secondary",
        "rules": {
            "multipliers": list(MULTIPLIERS),
            "min_trades": MIN_TRADES,
            "min_usable_neighbours": MIN_USABLE_NEIGHBOURS,
            "positive_share": f"{POSITIVE_SHARE_NUM}/{POSITIVE_SHARE_DEN}",
            "min_retention": MIN_RETENTION,
        },
        "diagnostic_not_trials": True,
        "shared": dict(shared or {}),
        "base": {
            "params_fingerprint": _fp(base_params),
            **trade_metrics(headline_trades, oos_start),
        },
        "neighbours": [],
        "skipped": [],
    }
    if meta is None:
        return {
            **evidence,
            "state": "no_declaration",
            "reason": "the strategy declares no param_meta",
        }
    neighbours, skipped, problems = build_neighbours(base_params, meta)
    evidence["skipped"] = skipped
    if problems:
        return {**evidence, "state": "declaration_error", "reason": "; ".join(problems)}
    base = evidence["base"]
    if base["trades"] == 0 or base["net_r"] is None or not math.isfinite(base["net_r"]):
        return {**evidence, "state": "r_undefined", "reason": "the base run has no defined net R"}
    if base["net_r"] <= 0:
        return {
            **evidence,
            "state": BASE_NOT_POSITIVE,
            "reason": f"the base net R per trade is {base['net_r']:.4f}, not positive; variants not run",
        }

    variants: dict[str, tuple[Any, Any]] = {"__base__": (params, None)}
    for nb in neighbours:
        variants[_key(nb)] = (dataclasses.replace(params, **{nb.parameter: nb.value}), nb)
    collected: dict[str, list] = defaultdict(list)
    errors: dict[str, str] = {}
    ordered = sorted(symbols)
    for start in range(0, len(ordered), chunk_size):
        frames = dict(load_frames(ordered[start : start + chunk_size]))
        for symbol in sorted(frames):
            for key, (p, _nb) in variants.items():
                if key in errors:
                    continue
                try:
                    collected[key].extend(_run_symbol(strategy, p, symbol, frames[symbol], run))
                except Exception as exc:  # recorded per variant, never swallowed
                    logger.exception("Perturbation variant %s failed on %s", key, symbol)
                    errors[key] = f"{type(exc).__name__}: {exc}"
            frames[symbol] = None
        del frames
        logger.info(f"Perturbation: {min(start + chunk_size, len(ordered))}/{len(ordered)} symbols")

    restated = {k: recost(v, cost_model) for k, v in collected.items()}
    replay = trade_metrics(restated.get("__base__", []), oos_start)
    if "__base__" in errors or not _same_result(replay, base):
        return {
            **evidence,
            "state": "base_replay_mismatch",
            "reason": (
                "the base replay does not equal the headline run "
                f"(replay trades {replay['trades']} net R {replay['net_r']}, headline trades "
                f"{base['trades']} net R {base['net_r']}; error {errors.get('__base__', 'none')})"
            ),
            "base_replay": replay,
        }
    rows = []
    for nb in neighbours:
        key = _key(nb)
        applied = {**base_params, nb.parameter: nb.value}
        row = {
            "parameter": nb.parameter,
            "multiplier": nb.multiplier,
            "value": nb.value,
            "base_value": nb.base,
            "params_fingerprint": _fp(applied),
        }
        if key in errors:
            row.update(trades=0, net_r=None, net_bps=None, error=errors[key])
        else:
            row.update(trade_metrics(restated.get(key, []), oos_start))
        rows.append(row)
    return {**evidence, "state": COMPLETE, "neighbours": rows, "base_replay_matches": True}


def _key(nb: Neighbour) -> str:
    return f"{nb.parameter}@{nb.multiplier}"


def _run_symbol(
    strategy: Any, params: Any, symbol: str, frame: Any, run: Any, entry_delay_bars: int = 0
) -> list:
    own = copy.deepcopy(strategy)
    # `prepare` keeps only OHLCV, so the price factor the tick snap needs is carried across it,
    # exactly as the harness does for the headline run.
    factor = frame["price_factor"] if "price_factor" in frame.columns else None
    prepared = tickgrid.carry_price_factor(own.prepare_for(symbol, frame.copy(), params), factor)
    return simulate(Ctx(prepared, symbol=symbol), own, params, run, entry_delay_bars=entry_delay_bars)


def _same_result(replay: Mapping[str, Any], base: Mapping[str, Any]) -> bool:
    if replay["trades"] != base["trades"]:
        return False
    a, b = replay.get("net_r"), base.get("net_r")
    return a is not None and b is not None and math.isclose(a, b, rel_tol=0, abs_tol=1e-9)


def validate_evidence(evidence: Any) -> list[str]:
    """Problems that make a perturbation evidence object unusable (empty when sound)."""
    if not isinstance(evidence, Mapping):
        return ["perturbation evidence is not an object"]
    problems = []
    if evidence.get("state") not in STATES:
        problems.append(f"state must be one of {', '.join(STATES)}")
    base = evidence.get("base")
    if not isinstance(base, Mapping) or not isinstance(base.get("trades"), int):
        problems.append("base must carry an integer trade count")
    if evidence.get("state") == COMPLETE:
        rows = evidence.get("neighbours")
        if not isinstance(rows, list) or not rows:
            problems.append("a complete evidence object needs neighbour rows")
        else:
            for r in rows:
                if not isinstance(r, Mapping) or not isinstance(r.get("trades"), int):
                    problems.append("every neighbour needs an integer trade count")
                    break
    return problems
