"""breakingtrade.com's own documented "ready-made scans" - not a fitted score.

Source: the live guide at https://breakingtrade.com/scanner-guide, which lists 13 ready-made
scans (the two .docx compilations in this folder carry only 8 of them - the live page is the
authority). Each SCAN below reproduces the vendor's exact stated column conditions and time
window rather than an invented weighting - this is "the filter suggested in the documentation,"
applied literally.

Only the scans that produce a DIRECTIONAL (trend / breakout / resolution) entry are kept. The
vendor's fade and utility scans are deliberately excluded:
  - "Sell Tail Fade"        (Day Type Normal Var + TPO Pos In VA -> fade)
  - "Poor High Magnet"      (Day Type Neutral Center -> fade toward the poor high)
  - "Single Print Refill"   (TPO Pos In VA -> rotation back into value)
  - "Range-Day Edge Fade"   (Day Type Normal -> "Sell IB High / buy IB Low")
  - "Sector Sweep"          (a text search over sector names, not a structural filter)

Two caveats, stated rather than hidden:
  1. Some bearish sides are mirrored, not quoted. The guide states the down mirror explicitly
     for Neutral Day Resolution ("Swap to Neutral Ext down / Below VA for shorts"), and gives
     The Breakdown as the bearish twin of The Runaway; for Breakaway and Value Migration it
     lists only the bullish side, so those bearish variants are inferred by symmetry and
     marked `mirrored=True`.
  2. "Value Migration" is documented against 'TPO Pos (Prev) = Above VA'. In both browser-saved
     snapshots this was built against, that column only ever showed PDH/PDL/PDR values, never a
     Value-Area reading - so the predicate accepts EITHER the documented above_va/below_va or
     the above_pdh/below_pdl proxy. Confirm against a live example before leaning on it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time

import pandas as pd

from utils.logging import get_logger

logger = get_logger(__name__)

#: Shown wherever a scan or score leg is switched off because tpo_pos is empty. The vendor shows
#: relative-to-open text in that field until about 09:45, so the value-area classes do not exist
#: yet; they are not mapped from that text (BUG-021, Option B).
TPO_POS_DISABLED_REASON = (
    "tpo_pos not yet classified (vendor shows relative-to-open text until about 09:45)"
)

#: Last state run_scans reported: None before the first call, then True (classified) or False
#: (whole column empty). It only gates log lines, so a race could at worst repeat or drop one
#: line. The poller runs run_scans from its one loop thread, so no lock is needed. If a second
#: thread ever calls it, guard this with utils.real_threading.Lock (CLAUDE.md, eventlet rules).
_tpo_state: bool | None = None


def tpo_pos_populated(market_profile: pd.DataFrame) -> tuple[int, int]:
    """(rows with a parsed tpo_pos, total rows)."""
    total = len(market_profile)
    if "tpo_pos" not in market_profile.columns:
        return 0, total
    return int(market_profile["tpo_pos"].notna().sum()), total


def tpo_pos_available(market_profile: pd.DataFrame) -> bool:
    """True when at least one row carries a parsed tpo_pos value."""
    return tpo_pos_populated(market_profile)[0] > 0


def _note_tpo_state(market_profile: pd.DataFrame) -> None:
    """Log when tpo_pos switches between empty and classified, not on every poll."""
    global _tpo_state
    populated, total = tpo_pos_populated(market_profile)
    available = populated > 0
    names = [s.name for s in SCANS if s.needs_tpo_pos]
    if not available and _tpo_state is not False:
        logger.warning(
            "BreakingTrade scans disabled (%d): %s. tpo_pos populated on %d/%d rows. Scans: %s",
            len(names),
            TPO_POS_DISABLED_REASON,
            populated,
            total,
            ", ".join(names),
        )
    elif available and _tpo_state is False:
        logger.info(
            "BreakingTrade tpo_pos classified again on %d/%d rows; %d scans re-enabled",
            populated,
            total,
            len(names),
        )
    _tpo_state = available


# ---------------------------------------------------------------------------
# Volume Scanner reference numbers (Full Reference docx, "Live Volume Scanner")
# ---------------------------------------------------------------------------

SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)

# "Volume Factor = today's volume in a session / that same session's 7-day average.
#  1.0 = normal. 2.0 = double the usual."  Colour scale from the same section.
VOLUME_EXTREME = 3.0
VOLUME_STRONG = 1.5
VOLUME_ELEVATED = 1.2
VOLUME_NORMAL = 0.8
VOLUME_THIN = 0.5

_VOLUME_COLOR_SCALE = [
    (VOLUME_EXTREME, "extreme"),
    (VOLUME_STRONG, "strong"),
    (VOLUME_ELEVATED, "elevated"),
    (VOLUME_NORMAL, "normal"),
    (VOLUME_THIN, "thin"),
]

# "Del% Above 70% = genuine ownership (persists); under 30% = intraday churn." Our extracted
# delivery_pct is a FRACTION (0.46, not 46) - see extractor._normalize_volume.
DELIVERY_GENUINE = 0.70
DELIVERY_CHURN = 0.30


def _elapsed_fraction(at: time) -> float | None:
    """Fraction of the 09:15-15:30 session elapsed at `at`. None outside session hours."""
    today = datetime.today().date()
    session_start = datetime.combine(today, SESSION_OPEN)
    session_end = datetime.combine(today, SESSION_CLOSE)
    now = datetime.combine(today, at)
    if now <= session_start or now >= session_end:
        return None
    return (now - session_start).total_seconds() / (session_end - session_start).total_seconds()


def pace_adjusted_surge(surge_x, at: time):
    """ "Judge it by pace, not face value" (cheat sheet) - the live session's Volume Factor is
    partial-volume-so-far vs a COMPLETE prior session, so it always understates early. Rescale
    to a projected full-day pace by dividing out how much of the session has elapsed.
    """
    if surge_x is None or pd.isna(surge_x):
        return None
    frac = _elapsed_fraction(at)
    if not frac:
        return None
    return surge_x / frac


def volume_reading(pace_adjusted_x) -> str:
    if pace_adjusted_x is None or pd.isna(pace_adjusted_x):
        return "no data"
    for threshold, label in _VOLUME_COLOR_SCALE:
        if pace_adjusted_x >= threshold:
            return label
    return "dead"


def delivery_reading(delivery_pct) -> str | None:
    if delivery_pct is None or pd.isna(delivery_pct):
        return None
    if delivery_pct > DELIVERY_GENUINE:
        return "genuine"
    if delivery_pct < DELIVERY_CHURN:
        return "churn"
    return "normal"


# ---------------------------------------------------------------------------
# Named scans
# ---------------------------------------------------------------------------

_ANY_TIME = (time(0, 0), time(23, 59))


@dataclass(frozen=True)
class ScanDef:
    name: str
    direction: str  # "up" | "down"
    window: tuple  # (start, end) as datetime.time; _ANY_TIME for unrestricted
    description: str
    predicate: Callable[[pd.Series], bool]
    sort_ib_ascending: bool = False
    mirrored: bool = False
    needs_tpo_pos: bool = False


def _the_runaway(row) -> bool:
    return (
        row.get("opening") == "gap_up"
        and row.get("open_type") == "open_drive"
        and row.get("open_type_dir") == "up"
        and row.get("tpo_pos") == "above_va"
    )


def _the_breakdown(row) -> bool:
    return (
        row.get("opening") == "gap_down"
        and row.get("open_type") == "open_drive"
        and row.get("open_type_dir") == "down"
        and row.get("tpo_pos") == "below_va"
    )


def _breakaway_above_pdh(row) -> bool:
    return (
        row.get("tpo_pos_prev") == "above_pdh"
        and row.get("tail") == "buy_tail"
        and row.get("day_type") == "Trend"
        and row.get("day_type_dir") == "up"
    )


def _breakaway_below_pdl(row) -> bool:
    return (
        row.get("tpo_pos_prev") == "below_pdl"
        and row.get("tail") == "sell_tail"
        and row.get("day_type") == "Trend"
        and row.get("day_type_dir") == "down"
    )


def _value_migration_up(row) -> bool:
    # documented as 'TPO Pos (Prev) Above VA'; above_pdh accepted as proxy - docstring caveat 2
    return (
        row.get("tpo_pos_prev") in ("above_va", "above_pdh")
        and row.get("day_type") == "Double Distribution"
        and row.get("day_type_dir") == "up"
        and row.get("tpo_pos") == "above_va"
    )


def _value_migration_down(row) -> bool:
    return (
        row.get("tpo_pos_prev") in ("below_va", "below_pdl")
        and row.get("day_type") == "Double Distribution"
        and row.get("day_type_dir") == "down"
        and row.get("tpo_pos") == "below_va"
    )


def _gap_up_trap(row) -> bool:
    """A gap up that got rejected and lost value - resolves DOWN, so a bearish entry."""
    return (
        row.get("opening") == "gap_up"
        and row.get("open_type") == "rejection"
        and row.get("open_type_dir") == "down"
        and row.get("tpo_pos") == "below_va"
    )


def _gap_down_rescue(row) -> bool:
    """A gap down that got bought back with a buy tail - resolves UP."""
    return (
        row.get("opening") == "gap_down"
        and row.get("open_type") == "rejection"
        and row.get("open_type_dir") == "up"
        and row.get("tail") == "buy_tail"
    )


def _neutral_resolution_up(row) -> bool:
    return (
        row.get("day_type") == "Neutral Ext"
        and row.get("day_type_dir") == "up"
        and row.get("tpo_pos") == "above_va"
    )


def _neutral_resolution_down(row) -> bool:
    return (
        row.get("day_type") == "Neutral Ext"
        and row.get("day_type_dir") == "down"
        and row.get("tpo_pos") == "below_va"
    )


def _live_print_up(row) -> bool:
    return (
        row.get("tpo_pos") == "tpo_ext_high"
        and (row.get("tpo_pos_count") or 0) >= 3
        and row.get("day_type") == "Trend"
        and row.get("day_type_dir") == "up"
    )


def _live_print_down(row) -> bool:
    return (
        row.get("tpo_pos") == "tpo_ext_low"
        and (row.get("tpo_pos_count") or 0) >= 3
        and row.get("day_type") == "Trend"
        and row.get("day_type_dir") == "down"
    )


SCANS = [
    ScanDef(
        "The Runaway",
        "up",
        (time(9, 20), time(10, 30)),
        "Gap Up + Open Drive up + TPO Pos Above VA, sort IB% ascending",
        _the_runaway,
        sort_ib_ascending=True,
        needs_tpo_pos=True,
    ),
    ScanDef(
        "The Breakdown",
        "down",
        (time(9, 20), time(10, 30)),
        "Gap Down + Open Drive down + TPO Pos Below VA, sort IB% ascending",
        _the_breakdown,
        sort_ib_ascending=True,
        needs_tpo_pos=True,
    ),
    ScanDef(
        "Breakaway Above PDH",
        "up",
        (time(10, 15), time(13, 0)),
        "TPO Pos (Prev) Above PDH + Buy Tail + Day Type Trend up",
        _breakaway_above_pdh,
    ),
    ScanDef(
        "Breakaway Below PDL",
        "down",
        (time(10, 15), time(13, 0)),
        "(mirrored) TPO Pos (Prev) Below PDL + Sell Tail + Day Type Trend down",
        _breakaway_below_pdl,
        mirrored=True,
    ),
    ScanDef(
        "Value Migration Up",
        "up",
        (time(10, 30), time(14, 0)),
        "TPO Pos (Prev) Above PDH [proxy] + Day Type Double Distribution up + TPO Pos Above VA",
        _value_migration_up,
        needs_tpo_pos=True,
    ),
    ScanDef(
        "Value Migration Down",
        "down",
        (time(10, 30), time(14, 0)),
        "(mirrored) TPO Pos (Prev) Below PDL [proxy] + Day Type Double Distribution down "
        "+ TPO Pos Below VA",
        _value_migration_down,
        mirrored=True,
        needs_tpo_pos=True,
    ),
    ScanDef(
        "The Gap-Up Trap",
        "down",
        (time(9, 30), time(11, 30)),
        "Gap Up + Rejection down + TPO Pos Below VA (failed gap resolving lower)",
        _gap_up_trap,
        needs_tpo_pos=True,
    ),
    ScanDef(
        "Gap-Down Rescue",
        "up",
        (time(9, 25), time(11, 0)),
        "Gap Down + Rejection up + Buy Tail (failed gap bought back)",
        _gap_down_rescue,
    ),
    ScanDef(
        "Neutral Day Resolution Up",
        "up",
        (time(14, 0), time(15, 15)),
        "Day Type Neutral Ext up + TPO Pos Above VA (late resolution, carries to next open)",
        _neutral_resolution_up,
        needs_tpo_pos=True,
    ),
    ScanDef(
        "Neutral Day Resolution Down",
        "down",
        (time(14, 0), time(15, 15)),
        "Day Type Neutral Ext down + TPO Pos Below VA (the guide's stated short mirror)",
        _neutral_resolution_down,
        needs_tpo_pos=True,
    ),
    ScanDef(
        "Live Print in Formation Up",
        "up",
        _ANY_TIME,
        "3+ TPO extension above Day's High + Day Type Trend up",
        _live_print_up,
        needs_tpo_pos=True,
    ),
    ScanDef(
        "Live Print in Formation Down",
        "down",
        _ANY_TIME,
        "(mirrored) 3+ TPO extension below Day's Low + Day Type Trend down",
        _live_print_down,
        mirrored=True,
        needs_tpo_pos=True,
    ),
]


def window_status(scan: ScanDef, at: time | None) -> str:
    if scan.window == _ANY_TIME:
        return "ALWAYS"
    if at is None:
        return "UNKNOWN"
    start, end = scan.window
    if at < start:
        return "NOT YET"
    if at > end:
        return "CLOSED"
    return "OPEN"


_MATCH_COLUMNS = [
    "symbol",
    "sector",
    "price",
    "change_pct",
    "ib_pct",
    "day_type",
    "open_type",
    "tpo_pos",
    "tail",
]


@dataclass
class ScanResult:
    scan: ScanDef
    status: str
    matches: pd.DataFrame
    disabled_reason: str | None = None


def run_scans(
    market_profile: pd.DataFrame,
    volume: pd.DataFrame | None = None,
    captured_at: datetime | None = None,
) -> list:
    """Evaluate every documented scan against one Market Profile snapshot.

    Each ScanResult carries the scan definition, whether its documented time window is
    currently open, and the matching symbols - merged with pace-adjusted volume confirmation
    when a volume snapshot is supplied.
    """
    at = captured_at.time() if captured_at else None
    results = []
    tpo_ok = tpo_pos_available(market_profile)
    _note_tpo_state(market_profile)

    for scan in SCANS:
        if scan.needs_tpo_pos and not tpo_ok:
            results.append(
                ScanResult(
                    scan=scan,
                    status="DISABLED",
                    matches=market_profile.iloc[0:0][
                        [c for c in _MATCH_COLUMNS if c in market_profile.columns]
                    ].reset_index(drop=True),
                    disabled_reason=TPO_POS_DISABLED_REASON,
                )
            )
            continue
        mask = market_profile.apply(scan.predicate, axis=1)
        matched = market_profile[mask]
        if scan.sort_ib_ascending and "ib_pct" in matched.columns:
            matched = matched.sort_values("ib_pct", ascending=True)
        keep = [c for c in _MATCH_COLUMNS if c in matched.columns]
        matched = matched[keep].reset_index(drop=True)

        if volume is not None and not matched.empty:
            matched = matched.merge(
                volume[["symbol", "surge_x", "delivery_pct"]], on="symbol", how="left"
            )
            if at is not None:
                matched["pace_adj_surge"] = matched["surge_x"].apply(
                    lambda s: pace_adjusted_surge(s, at)
                )
                matched["volume_reading"] = matched["pace_adj_surge"].apply(volume_reading)
            matched["delivery_reading"] = matched["delivery_pct"].apply(delivery_reading)

        results.append(ScanResult(scan=scan, status=window_status(scan, at), matches=matched))

    return results
