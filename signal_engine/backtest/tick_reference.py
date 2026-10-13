"""Dated NSE tick-size reference.

`research/reference/nse_tick_size.yaml` records the cash-segment tick tiers by effective date. The
engine snaps stop triggers, and gate G6.4 validates them, against the regime in force on each
trade's date. Using today's table for a 2020 trade would model a Rs 0.01 trigger in a year when the
exchange tick was Rs 0.05.

The tier of a session comes from the previous month's last close (the exchange reviews monthly);
`safe_tick` is the coarsest tick a price could carry within the month, valid under every finer tier
because each tier's tick is a multiple of the finer ones.

A date outside the file's coverage has no tick: `NotCovered` is raised and nothing is assumed.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from signal_engine.features.instruments import TIER_DRIFT

DEFAULT_PATH = (
    Path(__file__).resolve().parent.parent / "research" / "reference" / "nse_tick_size.yaml"
)
#: Float noise allowance when dividing a price by its tick.
_TICK_EPS = 1e-9
#: A price is on the grid when its distance to the nearest tick multiple is below this many ticks.
GRID_TOLERANCE_TICKS = 1e-6


class TickReferenceError(Exception):
    """The reference file is malformed."""


class NotCovered(TickReferenceError, ValueError):  # noqa: N818 - reads as a condition, not a fault
    """The date has no tick regime in the reference."""


@dataclass(frozen=True)
class Tier:
    upper: float | None
    inclusive: bool
    tick: float


@dataclass(frozen=True)
class Regime:
    id: str
    start: date
    end: date | None
    tiers: tuple[Tier, ...]


@dataclass(frozen=True)
class Reference:
    version: int
    verification: str
    verified_on: str
    coverage_start: date
    regimes: tuple[Regime, ...]
    sha256: str

    def info(self) -> dict[str, Any]:
        """What a run record stores to pin the reference it was validated against."""
        return {
            "version": self.version,
            "sha256": self.sha256,
            "verification": self.verification,
            "verified_on": self.verified_on,
            "coverage_start": self.coverage_start.isoformat(),
            "regimes": [r.id for r in self.regimes],
        }


def _day(value: Any, where: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise TickReferenceError(f"{where}: {value!r} is not an ISO date") from None


def _tiers(raw: Any, where: str) -> tuple[Tier, ...]:
    if not isinstance(raw, list) or not raw:
        raise TickReferenceError(f"{where}: tiers must be a non-empty list")
    tiers: list[Tier] = []
    for i, row in enumerate(raw):
        tick, upper = row.get("tick"), row.get("upper")
        if isinstance(tick, bool) or not isinstance(tick, (int, float)) or not tick > 0:
            raise TickReferenceError(f"{where} tier {i}: tick must be a positive number")
        if upper is not None and (isinstance(upper, bool) or not isinstance(upper, (int, float))):
            raise TickReferenceError(f"{where} tier {i}: upper must be a number or null")
        if (upper is None) != (i == len(raw) - 1):
            raise TickReferenceError(f"{where}: only the last tier may be open (upper: null)")
        tiers.append(Tier(upper, bool(row.get("inclusive", False)), float(tick)))
    uppers = [t.upper for t in tiers[:-1]]
    ticks = [t.tick for t in tiers]
    if uppers != sorted(uppers) or ticks != sorted(ticks):
        raise TickReferenceError(f"{where}: tiers must rise in price and in tick")
    for fine, coarse in zip(ticks, ticks[1:], strict=False):
        if round(coarse / fine, 6) != round(coarse / fine):
            raise TickReferenceError(f"{where}: tick {coarse} is not a multiple of {fine}")
    return tuple(tiers)


def parse(text: str) -> Reference:
    """The reference from its YAML text. Raises TickReferenceError when it is malformed."""
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise TickReferenceError("the tick reference is not a mapping")
    for key in ("version", "verification", "verified_on", "coverage_start", "regimes", "sources"):
        if key not in raw:
            raise TickReferenceError(f"missing field '{key}'")
    regimes: list[Regime] = []
    for i, row in enumerate(raw["regimes"] or []):
        where = f"regime {row.get('id', i)}"
        start = _day(row.get("effective_from"), where)
        end = None if row.get("effective_to") is None else _day(row["effective_to"], where)
        regimes.append(Regime(str(row.get("id", i)), start, end, _tiers(row.get("tiers"), where)))
    if not regimes:
        raise TickReferenceError("no regimes")
    coverage = _day(raw["coverage_start"], "coverage_start")
    if regimes[0].start != coverage:
        raise TickReferenceError("the first regime must start at coverage_start")
    for prev, nxt in zip(regimes, regimes[1:], strict=False):
        if prev.end is None or (nxt.start - prev.end).days != 1:
            raise TickReferenceError(f"regimes {prev.id} and {nxt.id} are not contiguous")
    if regimes[-1].end is not None:
        raise TickReferenceError("the last regime must be open-ended")
    return Reference(
        int(raw["version"]),
        str(raw["verification"]),
        str(raw["verified_on"]),
        coverage,
        tuple(regimes),
        hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def load(path: Path | str | None = None) -> Reference:
    """Read and validate the reference file (the shipped one by default)."""
    return parse(Path(path or DEFAULT_PATH).read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def default_reference() -> Reference:
    return load()


def _regime(day: date, ref: Reference) -> Regime:
    for regime in ref.regimes:
        if regime.start <= day and (regime.end is None or day <= regime.end):
            return regime
    raise NotCovered(
        f"no NSE tick regime covers {day.isoformat()} (reference starts "
        f"{ref.coverage_start.isoformat()}); no tick is assumed"
    )


def tick_for_price(price: float, day: date, ref: Reference | None = None) -> float:
    """The cash-segment tick of a reference price in the regime in force on `day`."""
    if not (isinstance(price, (int, float)) and math.isfinite(price) and price > 0):
        raise ValueError(f"price must be positive and finite, got {price!r}")
    for tier in _regime(day, ref or default_reference()).tiers:
        if tier.upper is None:
            return tier.tick
        if price < tier.upper or (tier.inclusive and price == tier.upper):
            return tier.tick
    raise TickReferenceError("unreachable: the last tier is open")  # pragma: no cover


def safe_tick(price: float, day: date, ref: Reference | None = None) -> float:
    """The coarsest tick `price` could carry this month, valid under every finer tier."""
    return tick_for_price(price * TIER_DRIFT, day, ref)


def round_to_safe_tick(
    price: float, direction: str, day: date, ref: Reference | None = None
) -> float:
    """Round a price onto the dated safe-tick grid ('nearest', 'down' or 'up')."""
    tick = safe_tick(price, day, ref)
    steps = price / tick
    if direction == "down":
        n = math.floor(steps + _TICK_EPS)
    elif direction == "up":
        n = math.ceil(steps - _TICK_EPS)
    else:
        n = math.floor(steps + 0.5 + _TICK_EPS)
    return round(n * tick, 2)


def on_grid(price: float, tick: float) -> bool:
    """True when `price` is a multiple of `tick` (float noise allowed)."""
    steps = price / tick
    return abs(steps - round(steps)) < GRID_TOLERANCE_TICKS
