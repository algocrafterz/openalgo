"""Run calendar and session model.

A run on a published dataset takes its trading days from the official NSE calendar
(`official_calendar`): the calendar decides whether the market was scheduled to trade, and data
quality decides only whether a symbol-session is usable. `derive_calendar` below infers the days
from market data and is used only where no authoritative calendar is available.

Derived calendar:

No official NSE holiday list exists in the repo, so the calendar is derived from market data and
labelled so (`source = "derived_from_market_data"`). A date is a trading day when at least 80% of
the symbols that have any data in that calendar month traded it; counting per month means a
symbol that did not exist yet never counts against earlier months. Short or irregular sessions
are listed in `short_sessions`, never removed. The version is tied to the loaded symbol set:
a different set of symbols can change which dates reach the 80% share, so it identifies the
calendar of this run's data, not a universal exchange calendar. An official calendar can replace the source later
without changing the manifest shape. All times are IST wall-clock.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from signal_engine.research.intake.fingerprints import canonical_json, sha256_hex

SOURCE = "derived_from_market_data"
VERSION_PREFIX = "historify-derived-"
OFFICIAL_SOURCE = "official_nse_calendar"
OFFICIAL_VERSION_PREFIX = "nse-official-"
MIN_SYMBOL_SHARE = 0.80
SHORT_SESSION_BAR_SHARE = 0.75
SESSION_CLOSE_MINUTE = 15 * 60 + 15


@dataclass(frozen=True)
class Calendar:
    """Trading days and irregular sessions, with the identity of the derivation."""

    trading_days: tuple[date, ...]
    short_sessions: tuple[date, ...]
    source: str
    version: str


def _as_of_date(as_of: date | str | None) -> date | None:
    if as_of is None:
        return None
    if isinstance(as_of, str):
        return date.fromisoformat(as_of)
    return as_of


def _daily_stats(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per traded date: the date (datetime64[D]), the bar count and the last bar's IST minute."""
    idx = df.index
    local = idx.tz_localize(None) if idx.tz is not None else idx
    wall = local.values
    order = np.argsort(wall, kind="stable")
    wall = wall[order]
    days = wall.astype("datetime64[D]")
    uniq, first, counts = np.unique(days, return_index=True, return_counts=True)
    last = first + counts - 1
    minutes = (wall[last] - uniq.astype("datetime64[ns]")) // np.timedelta64(1, "m")
    return uniq, counts, minutes.astype(np.int64)


def _short_sessions(stats: list[tuple[np.ndarray, np.ndarray, np.ndarray]], keep: set) -> list:
    intraday = [s for s in stats if s[1].size and s[1].max() > 1]
    if not intraday:
        return []
    all_counts = np.concatenate([s[1] for s in intraday])
    modal = Counter(all_counts.tolist()).most_common(1)[0][0]
    per_day: dict[Any, tuple[list[int], list[int]]] = {}
    for days, counts, minutes in intraday:
        for d, c, m in zip(days.tolist(), counts.tolist(), minutes.tolist(), strict=True):
            if d in keep:
                bucket = per_day.setdefault(d, ([], []))
                bucket[0].append(c)
                bucket[1].append(m)
    flagged = []
    for d, (counts, minutes) in per_day.items():
        day_modal = Counter(counts).most_common(1)[0][0]
        if (
            day_modal < SHORT_SESSION_BAR_SHARE * modal
            or float(np.median(minutes)) < SESSION_CLOSE_MINUTE
        ):
            flagged.append(d)
    return flagged


def _frame_stats(frames: Mapping[str, pd.DataFrame], as_of: date | str | None) -> list:
    """Per symbol with bars: (dates, bar counts, last-bar minutes), ignoring dates after as_of."""
    cutoff = _as_of_date(as_of)
    cutoff64 = np.datetime64(cutoff, "D") if cutoff else None
    stats = []
    for name in sorted(frames):
        df = frames[name]
        if df is None or len(df) == 0:
            continue
        days, counts, minutes = _daily_stats(df)
        if cutoff64 is not None:
            keep = days <= cutoff64
            days, counts, minutes = days[keep], counts[keep], minutes[keep]
        if days.size:
            stats.append((days, counts, minutes))
    return stats


def loaded_sessions(frames: Mapping[str, pd.DataFrame], as_of: date | str | None = None) -> set:
    """Every date on which at least one frame has a bar, ignoring dates after `as_of`."""
    return {d for days, _, _ in _frame_stats(frames, as_of) for d in days.tolist()}


def official_calendar(
    frames: Mapping[str, pd.DataFrame],
    official: Any,
    as_of: date | str | None = None,
    out_of_scope: Any = (),
) -> Calendar:
    """The run calendar from the official NSE calendar, over the loaded range.

    Trading days are the official sessions from the first loaded date to `as_of` (the last
    loaded date when None), minus the dataset's declared out-of-scope sessions. No data share
    decides whether a date was a trading day. Short sessions are still read from the bars,
    because they describe the shape of the data, not the schedule.
    """
    stats = _frame_stats(frames, as_of)
    loaded = {d for days, _, _ in stats for d in days.tolist()}
    if not loaded:
        return Calendar((), (), OFFICIAL_SOURCE, OFFICIAL_VERSION_PREFIX + "empty")
    last = _as_of_date(as_of) or max(loaded)
    skip = set(out_of_scope)
    days_out = tuple(d for d in official.expected_sessions(min(loaded), last) if d not in skip)
    short_out = tuple(sorted(_short_sessions(stats, set(days_out))))
    digest = sha256_hex(canonical_json([official.version, [d.isoformat() for d in days_out]]))
    return Calendar(days_out, short_out, OFFICIAL_SOURCE, OFFICIAL_VERSION_PREFIX + digest[:8])


def derive_calendar(
    frames: Mapping[str, pd.DataFrame], as_of: date | str | None = None
) -> Calendar:
    """Derive the trading calendar from the symbols' bars, ignoring dates after `as_of`."""
    stats = _frame_stats(frames, as_of)
    month_n: Counter = Counter()
    day_n: Counter = Counter()
    for days, _, _ in stats:
        month_n.update({(d.year, d.month) for d in days.tolist()})
        day_n.update(days.tolist())
    trading = sorted(
        d for d, n in day_n.items() if n >= MIN_SYMBOL_SHARE * month_n[(d.year, d.month)] - 1e-9
    )
    keep_set = set(trading)
    short = sorted(_short_sessions(stats, keep_set))
    days_out = tuple(date.fromisoformat(str(d)) for d in trading)
    short_out = tuple(date.fromisoformat(str(d)) for d in short)
    digest = sha256_hex(canonical_json([d.isoformat() for d in days_out]))
    return Calendar(days_out, short_out, SOURCE, VERSION_PREFIX + digest[:8])


def _hhmm(minutes: int | None) -> str | None:
    return None if minutes is None else f"{minutes // 60:02d}:{minutes % 60:02d}"


def _calendar_label(calendar: Calendar) -> str:
    """How the session block names the calendar the run actually used."""
    if calendar.source == OFFICIAL_SOURCE:
        return "official NSE calendar"
    if calendar.source == SOURCE:
        return "derived calendar"
    return f"{calendar.source} calendar"


def session_model(
    calendar: Calendar,
    *,
    profile_rules: Any,
    product: str,
    strategy_window: str | None,
    exchange: str = "NSE",
) -> dict[str, Any]:
    """The manifest `session` block. MIS carries the profile cutoffs, CNC has none."""
    cutoff = force_flat = None
    if product == "MIS" and profile_rules is not None:
        cutoff = _hhmm(profile_rules.latest_exit_bar_close_min)
        force_flat = _hhmm(profile_rules.force_flat_min)
    return {
        "exchange": f"{exchange} cash 09:15-15:30 ({_calendar_label(calendar)})",
        "strategy_window": strategy_window,
        "broker_cutoff": cutoff,
        "force_flat": force_flat,
    }
