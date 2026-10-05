"""The data contract of a frozen strategy version (plan Phase 4, owner refinements 2026-10-05).

What a run needs from the data, derived from the frozen spec only, with the provenance of every
value:

* frequency: from the spec timeframe;
* history: every historical-data requirement of the version (warm-up bars, the eligibility
  listing-age requirement, every lookback/period/window parameter, every indicator period); the
  contract requirement is the largest, and all of them are recorded;
* loader minimum: for daily data, the contract history (the largest requirement). A symbol with
  fewer sessions can never fill the strategy's window, so it is excluded as an eligibility
  limitation. This replaces the legacy loader's fixed 500, a default with no recorded rationale
  that also dropped symbols the strategy can use (owner decision 2026-10-05);
* session exclusion: a daily lookback strategy may not drop single sessions (owner decision
  for IDEA-027), intraday sessions are independent;
* universe: the spec's declared universe, else the platform default (today's NSE F&O list).
  The universe is never redefined as "whatever the dataset holds": preflight resolves universe
  -> dataset availability -> requested -> exclusions -> eligible and records each step.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

DEFAULT_UNIVERSE = "nse_fno_current"
DEFAULT_UNIVERSE_SOURCE = ("signal_engine.backtest.data.NSE_FNO: today's NSE F&O list "
                           "(survivorship bias; not point-in-time membership)")
_HISTORY_PARAM = re.compile(r"(lookback|period|window|sessions|bars)", re.IGNORECASE)
_RESOLVED_HISTORY = ("execution.warmup_bars", "execution.eligibility_min_prior_sessions")


class ContractError(ValueError):
    """The frozen spec does not give a usable data contract."""


@dataclass(frozen=True)
class DataContract:
    frequency: str
    history_sessions: int
    history_requirements: dict[str, int]
    loader_min_sessions: int
    session_exclusion_allowed: bool
    universe_name: str
    universe_source: str
    universe_symbols: tuple[str, ...]
    universe_version: str
    notes: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["universe_symbols"] = list(self.universe_symbols)
        d["notes"] = list(self.notes)
        return d


def _frequency(timeframe: Any) -> str:
    tf = str(timeframe or "").strip().lower()
    if tf in ("1d", "d", "day", "daily"):
        return "D"
    if re.fullmatch(r"\d+m", tf):
        return tf
    raise ContractError(f"unsupported timeframe {timeframe!r}")


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _history(frozen: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for name in _RESOLVED_HISTORY:
        item = (frozen.resolved or {}).get(name)
        v = _int(item.get("value")) if isinstance(item, dict) else None
        if v is not None:
            out[name] = v
    for key, value in (frozen.intent.get("parameters") or {}).items():
        v = _int(value)
        if v is not None and _HISTORY_PARAM.search(str(key)):
            out[f"parameters.{key}"] = v
    for ind in frozen.intent.get("indicators") or []:
        if isinstance(ind, dict):
            v = _int(ind.get("period"))
            if v is not None:
                out[f"indicators.{ind.get('name')}.period"] = v
    return out


def _universe(frozen: Any) -> tuple[str, str, tuple[str, ...]]:
    declared = frozen.intent.get("universe")
    if isinstance(declared, list) and declared:
        return "spec", "spec.yaml universe", tuple(str(s) for s in declared)
    if declared not in (None, "", []):
        raise ContractError(f"unsupported universe declaration {declared!r}")
    from signal_engine.backtest import data

    symbols = tuple(sorted(s.removesuffix(".NS") for s in data.NSE_FNO))
    return DEFAULT_UNIVERSE, DEFAULT_UNIVERSE_SOURCE, symbols


def resolve_contract(frozen: Any) -> DataContract:
    """The data contract of a frozen spec. Raises ContractError for an unusable spec."""
    frequency = _frequency(frozen.intent.get("timeframe"))
    history = _history(frozen)
    name, source, symbols = _universe(frozen)
    version = hashlib.sha256(json.dumps(sorted(symbols)).encode()).hexdigest()
    daily = frequency == "D"
    history_sessions = max(history.values(), default=0)
    notes = (f"loader minimum {history_sessions} sessions = contract history "
             "(largest history requirement)",) if daily else ()
    return DataContract(
        frequency=frequency,
        history_sessions=history_sessions,
        history_requirements=history,
        loader_min_sessions=history_sessions if daily else 0,
        session_exclusion_allowed=not daily,
        universe_name=name,
        universe_source=source,
        universe_symbols=symbols,
        universe_version=version,
        notes=notes,
    )
