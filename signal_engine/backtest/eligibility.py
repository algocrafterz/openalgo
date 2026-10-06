"""Per-date eligibility and the universe frame.

`MEMBERSHIP_SOURCE` and `SURVIVORSHIP_BIAS` are a standing disclosure. The repo has no historical
F&O membership source, so the universe is today's symbol master applied to the past: it is NOT
point-in-time, and symbols that were delisted or dropped from F&O are absent. Every run must
say so, and the survivorship gate reports the bias rather than passing it.

A symbol-session is eligible when the symbol has at least `min_prior_sessions` earlier traded
sessions in the calendar, traded that day, session volume is above `min_volume` and the session
close is at or above `min_price` (the NSE raw close when `price_factors` is given).

Two limits of the data, stated rather than hidden. The listing-age rule only applies to a symbol
whose first session lies more than `LISTING_TOLERANCE_SESSIONS` calendar sessions after the
panel's first session, i.e. one that appears to list inside the panel; a symbol present from the
panel start is exempt, because its true listing date is unknown (a data-start effect: it may
have listed years earlier). The price floor is skipped for split-adjusted prices
(`adjusted_prices=True`, the daily swing panel), where a stock adjusted below the floor in the
past would otherwise lose real history. `adjusted_prices=True` with `price_factors` is refused.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

MEMBERSHIP_SOURCE = "current_symbol_master"
SURVIVORSHIP_BIAS = True

#: A symbol first seen within this many calendar sessions of the panel start is treated as
#: present from the start (exempt from LISTING_AGE).
LISTING_TOLERANCE_SESSIONS = 5

LISTING_AGE = "LISTING_AGE"
NO_TRADE = "NO_TRADE"
ZERO_VOLUME = "ZERO_VOLUME"
PRICE_FLOOR = "PRICE_FLOOR"
PRICE_FLOOR_NOT_APPLIED = (
    "not applied to the split-adjusted daily panel: its prices are back-adjusted, not the "
    "traded price"
)
#: Version of the eligibility policy. 2: the price floor tests the NSE raw close, not the frame.
ELIGIBILITY_POLICY_VERSION = 2
BASIS_NSE_RAW = "nse_raw_last_bar_close"
BASIS_NOT_APPLIED = "not_applied_back_adjusted"
BASIS_AS_LOADED = "frame_close_as_loaded"
REASONS = (LISTING_AGE, NO_TRADE, ZERO_VOLUME, PRICE_FLOOR)

UNIVERSE_COLUMNS = (
    "symbol",
    "first_date",
    "last_date",
    "sessions",
    "eligible_sessions",
    "ineligible_sessions",
    "median_daily_volume",
)


@dataclass(frozen=True)
class EligibilityPolicy:
    """Thresholds, taken from the resolved `execution.eligibility_*` fields."""

    min_prior_sessions: int
    min_volume: float
    min_price: float


@dataclass(frozen=True)
class EligibilityResult:
    """The universe frame and, per symbol, each ineligible date with its reason code."""

    universe_frame: pd.DataFrame
    ineligible: dict[str, dict[date, str]]


def _col(df: pd.DataFrame, name: str) -> np.ndarray:
    return (df[name] if name in df.columns else df[name.lower()]).to_numpy(dtype=float)


def _daily(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per date: date (datetime64[D]), session volume, session close (last bar)."""
    idx = df.index
    local = idx.tz_localize(None) if idx.tz is not None else idx
    wall = local.values
    order = np.argsort(wall, kind="stable")
    days = wall[order].astype("datetime64[D]")
    uniq, first, counts = np.unique(days, return_index=True, return_counts=True)
    volume = np.add.reduceat(np.nan_to_num(_col(df, "Volume")[order]), first)
    close = _col(df, "Close")[order][first + counts - 1]
    return uniq, volume, close


def _raw_close(symbol: str, days: np.ndarray, close: np.ndarray, factors) -> np.ndarray:
    """Session close divided by its price factor; a missing or invalid factor raises."""
    out = np.empty(close.size)
    for i, day in enumerate(days):
        d = date.fromisoformat(str(day))
        f = (factors or {}).get(d)
        if f is None or not np.isfinite(f) or f <= 0:
            raise ValueError(f"no valid price factor for {symbol} on {d}: {f!r}")
        # Rounded so a raw price stored as raw * factor round-trips exactly at the floor.
        out[i] = round(close[i] / f, 6)
    return out


def _symbol(
    cal: np.ndarray,
    df: pd.DataFrame,
    policy: EligibilityPolicy,
    adjusted_prices: bool = False,
    factors=None,
    use_factors: bool = False,
    symbol: str = "",
):
    days, volume, close = _daily(df)
    pos = np.searchsorted(cal, days)
    inside = (pos < cal.size) & (cal[np.minimum(pos, cal.size - 1)] == days)
    days, volume, close, pos = days[inside], volume[inside], close[inside], pos[inside]
    if days.size == 0:
        return None
    if use_factors:
        close = _raw_close(symbol, days, close, factors)
    traded = np.zeros(cal.size, dtype=bool)
    traded[pos] = True
    vol = np.zeros(cal.size)
    vol[pos] = volume
    px = np.zeros(cal.size)
    px[pos] = close
    prior = np.cumsum(traded) - traded
    span = (np.arange(cal.size) >= pos[0]) & (np.arange(cal.size) <= pos[-1])
    reason = np.full(cal.size, "", dtype=object)
    todo = span.copy()
    from_start = int(pos[0]) <= LISTING_TOLERANCE_SESSIONS
    no_mask = np.zeros(cal.size, dtype=bool)
    for mask, code in (
        (~traded, NO_TRADE),
        (no_mask if from_start else prior < policy.min_prior_sessions, LISTING_AGE),
        (~(vol > policy.min_volume), ZERO_VOLUME),
        (no_mask if adjusted_prices else ~(px >= policy.min_price), PRICE_FLOOR),
    ):
        hit = todo & mask
        reason[hit] = code
        todo &= ~hit
    bad = np.flatnonzero(span & (reason != ""))
    return days, volume, pos, int(span.sum()), bad, reason


def compute_eligibility(
    frames: Mapping[str, pd.DataFrame],
    policy: EligibilityPolicy,
    calendar,
    *,
    adjusted_prices: bool = False,
    price_factors: Mapping[str, Mapping[date, float]] | None = None,
) -> EligibilityResult:
    """Per-symbol, per-date eligibility over the calendar. Symbols are sorted by name.

    `adjusted_prices=True` skips the price floor (split-adjusted daily panel).
    `price_factors` maps symbol to {session date: adjusted/raw price factor}; the floor then
    tests the NSE raw close (last-bar close / factor). A traded session without a factor raises.
    """
    if adjusted_prices and price_factors is not None:
        raise ValueError("adjusted_prices=True cannot be combined with price_factors")
    cal = np.array(sorted(calendar.trading_days), dtype="datetime64[D]")
    rows = []
    ineligible: dict[str, dict[date, str]] = {}
    for name in sorted(frames):
        out = (
            _symbol(
                cal,
                frames[name],
                policy,
                adjusted_prices,
                (price_factors or {}).get(name),
                price_factors is not None,
                name,
            )
            if cal.size and len(frames[name])
            else None
        )
        if out is None:
            rows.append((name, "", "", 0, 0, 0, 0.0))
            continue
        days, volume, pos, span_n, bad, reason = out
        cal_dates = [date.fromisoformat(str(cal[i])) for i in bad]
        if cal_dates:
            ineligible[name] = {d: str(reason[i]) for d, i in zip(cal_dates, bad, strict=True)}
        rows.append(
            (
                name,
                str(days[0]),
                str(days[-1]),
                int(days.size),
                span_n - int(bad.size),
                int(bad.size),
                float(np.median(volume)),
            )
        )
    frame = pd.DataFrame(rows, columns=list(UNIVERSE_COLUMNS))
    frame = frame.astype(
        {
            "sessions": "int64",
            "eligible_sessions": "int64",
            "ineligible_sessions": "int64",
            "median_daily_volume": "float64",
        }
    )
    return EligibilityResult(frame, ineligible)


def apply_eligibility(
    frames: Mapping[str, pd.DataFrame], result: EligibilityResult
) -> tuple[dict[str, pd.DataFrame], dict[str, dict[date, str]]]:
    """Frames without their ineligible symbol-sessions, and what was actually dropped.

    Returns `(kept, dropped)`. `dropped` maps symbol to `{date: reason}` for the ineligible
    dates that had rows in the frame (a date with no bars, such as NO_TRADE, has nothing to
    drop and is not listed). A symbol left with no rows is omitted from `kept` but stays in
    `dropped`. Inputs are not modified; a symbol with nothing to drop is returned as is.

    Dropping a whole session removes its bars from the series the engine sees, so a rolling
    indicator spans the gap exactly as it already does after the loader's split-day drop.
    """
    kept: dict[str, pd.DataFrame] = {}
    dropped: dict[str, dict[date, str]] = {}
    for name, df in frames.items():
        bad = result.ineligible.get(name)
        if not bad or len(df) == 0:
            kept[name] = df
            continue
        dates = pd.Series(df.index.date, index=df.index)
        present = set(dates.unique())
        hit = {d: r for d, r in bad.items() if d in present}
        if not hit:
            kept[name] = df
            continue
        dropped[name] = dict(sorted(hit.items()))
        left = df[~dates.isin(set(hit)).to_numpy()]
        if len(left):
            kept[name] = left
    return kept, dropped
