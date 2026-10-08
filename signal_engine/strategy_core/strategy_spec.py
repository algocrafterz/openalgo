"""The frozen BREAKOUT v1.0 specification as data (spec item 0.3, step S10).

`research/ideas/idea-031-breakout-key-level/strategy.yaml` is the one machine-readable form of the
strategy. The core, the backtest adapter, the runner and the start-up check read it here, so no mode
has a private copy of a rule. A config value that must also live in `config.yaml` (the time exit, the
per-direction cap, the exit plan) is compared with the spec at start-up and a difference refuses to start.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SPEC_PATH = (
    Path(__file__).resolve().parents[1]
    / "research"
    / "ideas"
    / "idea-031-breakout-key-level"
    / "strategy.yaml"
)


class SpecError(Exception):
    """The specification file is missing, unreadable or malformed."""


@dataclass(frozen=True)
class StrategySpec:
    id: str
    version: str
    fingerprint: str  # sha256 of the file's bytes: any edit to the file is a new fingerprint
    body: dict[str, Any]  # the `strategy:` mapping

    @property
    def core(self) -> dict[str, Any]:
        return self.body["core"]

    @property
    def policy(self) -> dict[str, Any]:
        return self.body["policy"]

    @property
    def exits(self) -> dict[str, Any]:
        return self.body["exits"]

    def stamp(self) -> dict[str, str]:
        """The three identifiers every signal, ledger entry row and run artifact carries."""
        return {
            "strategy_id": self.id,
            "strategy_version": self.version,
            "strategy_fingerprint": self.fingerprint,
        }


def load_spec(path: Path | str = SPEC_PATH) -> StrategySpec:
    """Read and validate the specification file."""
    path = Path(path)
    try:
        raw = path.read_bytes()
        data = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError) as exc:
        raise SpecError(f"cannot read the strategy specification {path}: {exc}") from exc
    body = data.get("strategy") if isinstance(data, dict) else None
    if not isinstance(body, dict):
        raise SpecError(f"{path}: no `strategy:` mapping")
    for block in ("core", "policy", "exits"):
        if not isinstance(body.get(block), dict):
            raise SpecError(f"{path}: strategy.{block} must be a mapping")
    for key in ("id", "version"):
        if not isinstance(body.get(key), str) or not body[key]:
            raise SpecError(f"{path}: strategy.{key} must be a non-empty string")
    return StrategySpec(
        id=body["id"], version=body["version"], fingerprint=hashlib.sha256(raw).hexdigest(), body=body
    )


def _hhmm_to_min(value: str) -> int:
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def check_runner(spec: StrategySpec, tag: str, entry, time_exit: tuple[int, int]) -> list[str]:
    """Differences between the spec and the config of runner strategy `tag` (`entry` is its RunnerStrategy).

    `time_exit` is (hour, minute) from config.yaml. An empty list means the config matches. Start-up
    refuses to run on any difference, because a value held in two places is a rule that can drift.
    """
    problems: list[str] = []
    want_exit = _hhmm_to_min(spec.policy["time_exit"])
    if want_exit != time_exit[0] * 60 + time_exit[1]:
        problems.append(
            f"time_exit: spec {spec.policy['time_exit']}, config {time_exit[0]:02d}:{time_exit[1]:02d}"
        )
    cap = spec.policy["max_entries_per_direction_per_symbol_per_day"]
    if entry.max_entries_per_direction_per_day != cap:
        problems.append(
            f"{tag} max_entries_per_direction_per_day: spec {cap}, "
            f"config {entry.max_entries_per_direction_per_day}"
        )
    if entry.exit_plan != spec.exits["plan"]:
        problems.append(f"{tag} exit_plan: spec {spec.exits['plan']}, config {entry.exit_plan}")
    if entry.exit_owner != spec.exits["exit_owner"]:
        problems.append(f"{tag} exit_owner: spec {spec.exits['exit_owner']}, config {entry.exit_owner}")
    if entry.supersedes_pine_tag != spec.id:
        problems.append(
            f"{tag} supersedes_pine_tag: spec needs Pine {spec.id} entries stopped on the Python universe, "
            f"config {entry.supersedes_pine_tag}"
        )
    if spec.policy.get("limits_scope") == "per_strategy_book" and entry.paper_limits != "live":
        problems.append(
            f"{tag} paper_limits: spec needs the live limits per strategy book, config {entry.paper_limits}"
        )
    return problems
