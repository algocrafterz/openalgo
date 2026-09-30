"""Exactly one signal source (pine | python) per strategy.

Two active sources means duplicate orders, and a python source with nothing generating
signals means silence. This module is the single place both hazards are decided: the engine
drops the other source's alerts, the runner refuses to dispatch for a pine strategy, and
start-up refuses an inconsistent config.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from loguru import logger

from signal_engine.config import RunnerSettings


class SingleSourceError(Exception):
    """The signal-source configuration would double-trade or never trade."""


_dropped: Counter = Counter()


def _source(cfg: RunnerSettings, strategy: str) -> str | None:
    entry = cfg.strategies.get(strategy.upper())
    return None if entry is None else entry.signal_source


def should_drop_alert(cfg: RunnerSettings, strategy: str) -> bool:
    """True when this strategy's signals come from the Python runner, so a Pine alert is a duplicate."""
    return _source(cfg, strategy) == "python"


def record_drop(strategy: str) -> int:
    key = strategy.upper()
    _dropped[key] += 1
    return _dropped[key]


def dropped_count(strategy: str) -> int:
    return _dropped[strategy.upper()]


def reset_counts() -> None:
    _dropped.clear()


def assert_can_dispatch(cfg: RunnerSettings, strategy: str, sink_dispatches: bool) -> None:
    """The runner may only place orders for a strategy configured with signal_source python."""
    if not sink_dispatches:
        return
    if _source(cfg, strategy) != "python":
        raise SingleSourceError(
            f"{strategy}: runner refuses to dispatch, signal_source is "
            f"{_source(cfg, strategy) or 'not configured'} (must be python)"
        )


def check_consistency(cfg: RunnerSettings, known_strategies: Iterable[str]) -> None:
    """Raise SingleSourceError listing every problem, or return when the config is coherent."""
    known = {k.upper() for k in known_strategies}
    problems: list[str] = []
    for tag, entry in cfg.strategies.items():
        if tag not in known:
            problems.append(f"{tag} is not a registered strategy")
        if entry.signal_source == "python" and not cfg.enabled:
            problems.append(
                f"{tag} is signal_source python but the runner is disabled "
                f"(runner.enabled is false; Pine alerts would be dropped and nothing would generate signals)"
            )
        if entry.signal_source == "python" and not entry.paper_only:
            problems.append(
                f"{tag} is signal_source python with paper_only false: a Python-sourced "
                f"strategy may not trade live until it passes docs/STRATEGY-ACCEPTANCE-CRITERIA.md "
                f"and that promotion is written into the code, so paper_only must be true"
            )
        if entry.exit_owner == "ladder" and entry.signal_source != "python":
            problems.append(
                f"{tag} has exit_owner ladder but signal_source {entry.signal_source} "
                f"(the Ladder only manages positions the runner opened)"
            )
    if problems:
        raise SingleSourceError("; ".join(problems))


def describe(cfg: RunnerSettings) -> list[str]:
    """One line per strategy, for the start-up log."""
    state = "enabled" if cfg.enabled else "disabled"
    return [
        f"signal source {tag}: {e.signal_source} (runner {state}, exit plan {e.exit_plan}, "
        f"exit owner {e.exit_owner}, {'paper only' if e.paper_only else 'live allowed'})"
        for tag, e in cfg.strategies.items()
    ]


def log_active_sources(cfg: RunnerSettings) -> None:
    for line in describe(cfg):
        logger.info(line)
