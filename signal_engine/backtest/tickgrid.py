"""Tick grid: the stop trigger the executor would send, and the evidence that it is on the grid.

Production rule (`executor.round_to_tick`, PRD 2026-09-30): the exact NSE tier comes from the
PREVIOUS month's last close, which is unknown at order time, so every stop trigger is rounded
onto the `safe_tick` grid, toward the market (a long stop up, a short stop down), and so fires
earlier than the signal's stop. A price on that grid is also on the grid of every finer tier.

Intraday bars are back-adjusted (`adjusted = raw * price_factor`), but the exchange only sees raw
prices. So the snap works on raw: `raw = stop / price_factor`, rounded, then scaled back. A frame
without a `price_factor` column is already raw (factor 1).

Scope. Only the stop is a resting order in production (entries and the target exit are market
orders), so the stop is the only price snapped here. A trailed stop is snapped each time it moves.
A ladder stop (`RunConfig.exit_plan`) is not snapped.

`tick_grid_evidence` is Part A of gate G6.4: it proves the stops the run modelled are on the
exchange grid for their month. Whether the broker accepts them is Part B, a recorded attestation
that no backtest can compute.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from signal_engine.backtest import tick_reference
from signal_engine.features.instruments import round_to_safe_tick

#: `RunConfig.tick_grid` values. NONE keeps the legacy engine, which every run recorded before
#: engine 1.2 used.
NONE = "none"
SAFE_TICK = "safe_tick"
MODES = (NONE, SAFE_TICK)

#: 2: ticks come from the dated reference (`tick_reference`); the evidence pins it and counts stops
#: on dates it does not cover. Version 1 used today's table for every date.
EVIDENCE_VERSION = 2

#: A stop is on the grid when its distance to the nearest tick multiple is below this many ticks.
GRID_TOLERANCE_TICKS = 1e-6

WORST_LIMIT = 10

RULE = (
    "every stop trigger is a multiple of the exact tick tier of its month, taken from the previous "
    "month's last close restated for corporate actions into the trade session's price units "
    "(safe_tick of the stop where that month is not in the data)"
)

NOT_COVERED = (
    "stops moved by a ladder exit plan are not snapped or checked",
    "trailed stops are snapped by the engine but only the initial stop of each trade is checked",
    "targets are market exits in production and are not orders",
    "broker acceptance of a trigger is a pre-live operational check and is not part of this evidence",
    "the dated tick reference is verified against secondary sources (see its header)",
)


def snap_stop(
    stop: float, direction: int, price_factor: float = 1.0, day: date | None = None
) -> float:
    """The trigger the executor sends for `stop`, in the frame's (possibly adjusted) price units.

    With `day`, the grid is the one in force on that date (`tick_reference`); without it, today's
    table (`features.instruments`), which is what the live executor uses.

    Raises:
        ValueError: The price factor or the raw stop is not a positive finite number.
    """
    if not (math.isfinite(price_factor) and price_factor > 0):
        raise ValueError(f"price_factor must be positive and finite, got {price_factor!r}")
    raw = stop / price_factor
    if not (math.isfinite(raw) and raw > 0):
        raise ValueError(f"raw stop must be positive and finite, got {raw!r}")
    way = "up" if direction == 1 else "down"
    snapped = round_to_safe_tick(raw, way) if day is None else (
        tick_reference.round_to_safe_tick(raw, way, day)
    )
    return snapped * price_factor


def on_grid(price: float, tick: float) -> bool:
    """True when `price` is a multiple of `tick` (float noise allowed)."""
    steps = price / tick
    return abs(steps - round(steps)) < GRID_TOLERANCE_TICKS


def month_key(day: Any) -> str:
    """'YYYY-MM' of a date or an ISO date string."""
    return str(day)[:7]


def previous_month_key(key: str) -> str:
    year, month = int(key[:4]), int(key[5:7])
    return f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"


def month_end_closes(
    closes: Mapping[str, Mapping[date, float]],
) -> dict[str, dict[str, float]]:
    """Per symbol, 'YYYY-MM' -> the close of that month's last session.

    `closes` maps symbol -> {session date: close} (`session_closes`: adjusted prices).
    """
    out: dict[str, dict[str, float]] = {}
    for symbol, sessions in closes.items():
        last: dict[str, tuple[date, float]] = {}
        for day, close in sessions.items():
            key = month_key(day)
            if key not in last or day > last[key][0]:
                last[key] = (day, float(close))
        out[symbol] = {k: v[1] for k, v in last.items()}
    return out


def tick_grid_evidence(
    trades: Iterable[Any],
    month_closes: Mapping[str, Mapping[str, float]],
    price_factors: Mapping[str, Mapping[date, float]] | None = None,
    *,
    engine_mode: str = NONE,
) -> dict[str, Any]:
    """G6.4: is the initial stop of every trade on the exchange tick grid of its own date?

    Each stop is checked against the tick regime in force on the trade's date
    (`research/reference/nse_tick_size.yaml`). A stop on a date the reference does not cover is
    counted under `uncovered` and not checked: no tick is assumed.

    Args:
        trades: Engine trades; a trade with a NaN stop has no resting order and is not counted.
        month_closes: `month_end_closes` output, in adjusted prices.
        price_factors: symbol -> session date -> price factor; missing means raw prices (1.0).
        engine_mode: The `RunConfig.tick_grid` the trades were produced under.
    """
    factors = price_factors or {}
    by_tier: dict[str, Counter] = {}
    basis: Counter = Counter()
    worst: list[dict[str, Any]] = []
    checked = off = uncovered = 0
    reference = tick_reference.default_reference()
    for t in trades:
        sl = float(t.sl)
        if not math.isfinite(sl):
            continue
        factor = float(factors.get(t.symbol, {}).get(_as_date(t.day), 1.0))
        raw = sl / factor
        prev = month_closes.get(t.symbol, {}).get(previous_month_key(month_key(t.day)))
        day = _as_date(t.day)
        try:
            if prev is not None and prev > 0:
                # the previous month's close restated into this session's price units
                tick, source = tick_reference.tick_for_price(prev / factor, day, reference), "exact"
            else:
                tick, source = tick_reference.safe_tick(raw, day, reference), "fallback_safe_tick"
        except tick_reference.NotCovered:
            uncovered += 1
            continue
        ok = on_grid(raw, tick)
        checked += 1
        basis[source] += 1
        row = by_tier.setdefault(f"{tick:g}", Counter())
        row["checked"] += 1
        if not ok:
            off += 1
            row["off_grid"] += 1
            worst.append(
                {
                    "symbol": t.symbol,
                    "day": str(t.day),
                    "direction": int(t.direction),
                    "raw_stop": round(raw, 6),
                    "tick": tick,
                    "tick_source": source,
                    "ticks_off": round(abs(raw / tick - round(raw / tick)), 6),
                }
            )
    worst.sort(key=lambda r: -r["ticks_off"])
    return {
        "version": EVIDENCE_VERSION,
        "rule": RULE,
        "engine_tick_grid": engine_mode,
        "reference": reference.info(),
        "uncovered": uncovered,
        "stops_checked": checked,
        "on_grid": checked - off,
        "off_grid": off,
        "tick_basis": dict(sorted(basis.items())),
        "by_tier": {k: dict(v) for k, v in sorted(by_tier.items(), key=lambda kv: float(kv[0]))},
        "worst": worst[:WORST_LIMIT],
        "not_covered": list(NOT_COVERED),
    }


def _as_date(day: Any) -> date:
    return day if isinstance(day, date) else date.fromisoformat(str(day)[:10])


def validate_evidence(evidence: Any) -> list[str]:
    """Problems that make a tick-grid evidence object unusable (empty when sound)."""
    if not isinstance(evidence, Mapping):
        return ["tick-grid evidence is not an object"]
    problems = []
    for key in ("stops_checked", "on_grid", "off_grid"):
        v = evidence.get(key)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            problems.append(f"{key} must be a non-negative integer")
    if not problems and evidence["on_grid"] + evidence["off_grid"] != evidence["stops_checked"]:
        problems.append("on_grid plus off_grid must equal stops_checked")
    return problems


def _session_factors(df: pd.DataFrame, factors: Mapping[date, float]) -> tuple[Any, np.ndarray]:
    sessions = df.index.date
    mapped = pd.Series(sessions).map(factors).to_numpy(dtype=float)
    return sessions, mapped


def attach_price_factors(
    frames: Mapping[str, pd.DataFrame],
    price_factors: Mapping[str, Mapping[date, float]],
    *,
    in_place: bool = False,
) -> dict[str, pd.DataFrame]:
    """Each frame with a `price_factor` column (adjusted / NSE raw price) for the engine's snap.

    `in_place=True` adds the column to the given frames instead of copying them, which the
    full-universe run needs to avoid holding two copies of every bar.

    Raises:
        ValueError: A bar's session has no positive finite factor. The snap cannot be made on
            raw prices without it, and guessing 1.0 would put adjusted prices on the grid.
    """
    out: dict[str, pd.DataFrame] = {}
    for symbol, df in frames.items():
        _, factor = _session_factors(df, price_factors.get(symbol, {}))
        bad = ~(np.isfinite(factor) & (factor > 0))
        if bad.any():
            raise ValueError(
                f"{symbol}: {int(bad.sum())} bars have no usable price_factor, so stops cannot "
                "be snapped on raw prices"
            )
        if in_place:
            with pd.option_context("mode.chained_assignment", None):
                df["price_factor"] = factor
            out[symbol] = df
        else:
            out[symbol] = df.assign(price_factor=factor)
    return out


def carry_price_factor(prepared: pd.DataFrame, factor: pd.Series | None) -> pd.DataFrame:
    """`prepared` with the input frame's `price_factor` column, which a strategy's `prepare` drops.

    `indicators.add_session_columns` keeps only OHLCV, so every strategy built on it loses the
    column. The factor is put back by bar time; None (a raw frame) changes nothing.

    Raises:
        ValueError: A prepared bar has no factor in the input.
    """
    if factor is None or "price_factor" in prepared.columns:
        return prepared
    carried = factor.reindex(prepared.index).to_numpy(dtype=float)
    if not np.isfinite(carried).all():
        raise ValueError("a prepared bar has no price_factor in the input frame")
    return prepared.assign(price_factor=carried)


def session_closes(frames: Mapping[str, pd.DataFrame]) -> dict[str, dict[date, float]]:
    """Per symbol and session, the last bar's close in the frame's own (adjusted) prices.

    Kept adjusted on purpose: the exchange sets a month's tier from the previous month's close
    restated for corporate actions, and `tick_grid_evidence` restates it into the trade
    session's units by dividing by that session's price factor. The raw close of a pre-split
    month would put a post-split stock in the wrong tier.
    """
    out: dict[str, dict[date, float]] = {}
    for symbol, df in frames.items():
        if not len(df):
            out[symbol] = {}
            continue
        last = pd.Series(df["Close"].to_numpy(), index=df.index.date).groupby(level=0).last()
        out[symbol] = {d: float(v) for d, v in last.items() if np.isfinite(v) and v > 0}
    return out
