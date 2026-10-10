"""G4.2 latency: does the result survive every entry filling one 5-minute bar later?

Design:

  WHAT IT IS.   A simulation, as the criteria specify ("entry delayed by one 5-minute bar"). It is
                not a measurement of latency: no measured alert-to-fill delay is held here, and
                none is substituted. Real alert lag is seconds, so one bar is a severe case.
  VARIANT.      The headline strategy and parameters, on the frames the headline run saw, with
                the engine's `entry_delay_bars` set to 1: a signal on bar i fills at the open of
                bar i + 2. Stop, target and risk stay those of the signal; the symbol waits flat
                with the order in flight; a signal whose delayed fill would fall outside its
                session is not taken. Everything else, including the statutory cost model, is as
                in the headline run.
  BASE.         The same pass replays the undelayed run and it must equal the headline run (trade
                count, net R and the trade fingerprint). Otherwise the stage stops and the gate
                is not evaluated.
  METRIC.       Mean net R per trade at 1.0x cost, with net bps secondary. The sign flips when
                "net R above zero" differs between the headline and the delayed run.
  TRIALS.       The delayed run is a diagnostic. It is not registered as a trial.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from signal_engine.backtest.costs import recost
from signal_engine.backtest.live_caps import trade_hash
from signal_engine.backtest.perturbation import (
    CHUNK_SYMBOLS,
    _run_symbol,
    _same_result,
    trade_metrics,
)
from utils.logging import get_logger

logger = get_logger(__name__)

VERSION = 1
DELAY_BARS = 1

COMPLETE = "complete"
UNSUPPORTED = "unsupported"
BASE_MISMATCH = "base_replay_mismatch"
R_UNDEFINED = "r_undefined"
ERROR = "error"
STATES = (COMPLETE, UNSUPPORTED, BASE_MISMATCH, R_UNDEFINED, ERROR)

LIMITATIONS = (
    "a simulation of a one-bar delay as the criteria specify, not a measured latency; the real "
    "alert lag is seconds, so one 5-minute bar is a severe case",
    "stops, targets and risk stay those of the signal bar, as the live engine sizes on the alert",
    "per-symbol intraday strategies only; swing and panel strategies are unsupported",
)


def run_stage(
    *,
    strategy: Any,
    params: Any,
    load_frames: Callable[[list[str]], Mapping[str, Any]],
    symbols: Sequence[str],
    run: Any,
    cost_model: Any,
    headline_trades: Sequence[Any],
    oos_start: str | None = None,
    shared: Mapping[str, Any] | None = None,
    chunk_size: int = CHUNK_SYMBOLS,
    delay: int = DELAY_BARS,
) -> dict[str, Any]:
    """The `latency.json` evidence for a run: the headline book against the delayed book."""
    base = {
        **trade_metrics(headline_trades, oos_start),
        "trade_sha256": trade_hash(headline_trades),
    }
    evidence: dict[str, Any] = {
        "version": VERSION,
        "state": "",
        "reason": "",
        "delay_bars": delay,
        "metric": "mean net R per trade at 1.0x cost (statutory model); net bps is secondary",
        "diagnostic_not_trials": True,
        "limitations": list(LIMITATIONS),
        "shared": dict(shared or {}),
        "base": base,
    }
    if base["trades"] == 0 or base["net_r"] is None or not math.isfinite(base["net_r"]):
        return {**evidence, "state": R_UNDEFINED, "reason": "the headline run has no defined net R"}
    plain: list = []
    late: list = []
    ordered = sorted(symbols)
    try:
        for start in range(0, len(ordered), chunk_size):
            frames = dict(load_frames(ordered[start : start + chunk_size]))
            for symbol in sorted(frames):
                plain.extend(_run_symbol(copy.deepcopy(strategy), params, symbol, frames[symbol], run))
                late.extend(
                    _run_symbol(copy.deepcopy(strategy), params, symbol, frames[symbol], run, delay)
                )
                frames[symbol] = None
            del frames
            logger.info(f"Latency: {min(start + chunk_size, len(ordered))}/{len(ordered)} symbols")
    except Exception as exc:  # recorded as a state, never swallowed
        logger.exception("Latency stage failed")
        return {**evidence, "state": ERROR, "reason": f"{type(exc).__name__}: {exc}"}
    plain, late = recost(plain, cost_model), recost(late, cost_model)
    replay = {**trade_metrics(plain, oos_start), "trade_sha256": trade_hash(plain)}
    if not _same_result(replay, base) or replay["trade_sha256"] != base["trade_sha256"]:
        return {
            **evidence,
            "state": BASE_MISMATCH,
            "reason": (
                "the undelayed replay does not equal the headline run "
                f"(replay trades {replay['trades']} net R {replay['net_r']}, headline trades "
                f"{base['trades']} net R {base['net_r']})"
            ),
            "base_replay": replay,
        }
    delayed = trade_metrics(late, oos_start)
    flips = None
    if delayed["net_r"] is not None and math.isfinite(delayed["net_r"]):
        flips = (base["net_r"] > 0) != (delayed["net_r"] > 0)
    return {
        **evidence,
        "state": COMPLETE,
        "base_replay_matches": True,
        "delayed": delayed,
        "sign_flips": flips,
    }


def validate_evidence(evidence: Any) -> list[str]:
    """Problems that make a latency evidence object unusable (empty when sound)."""
    if not isinstance(evidence, Mapping):
        return ["latency evidence is not an object"]
    problems = []
    if evidence.get("state") not in STATES:
        problems.append(f"state must be one of {', '.join(STATES)}")
    base = evidence.get("base")
    if not isinstance(base, Mapping) or not isinstance(base.get("trades"), int):
        problems.append("base must carry an integer trade count")
    if evidence.get("state") == COMPLETE:
        delayed = evidence.get("delayed")
        if not isinstance(delayed, Mapping) or not isinstance(delayed.get("trades"), int):
            problems.append("a complete evidence object needs a delayed book with a trade count")
        delay = evidence.get("delay_bars")
        if not isinstance(delay, int) or isinstance(delay, bool) or delay < 1:
            problems.append("delay_bars must be an integer of at least 1")
        if evidence.get("diagnostic_not_trials") is not True:
            problems.append("diagnostic_not_trials must be true")
    return problems
