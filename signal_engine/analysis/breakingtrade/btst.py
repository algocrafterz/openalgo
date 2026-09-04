"""Closing-hour accumulation watchlist for the NEXT session (BTST / carry).

THE IDEA, AND WHOSE IT IS

The vendor's own row-shape table describes it: "Closing Ramp - volume building into K/L/M with
price holding - someone wants overnight ownership - watchlist for tomorrow's open more than a
same-day trade", qualified by the Del% column, "above 70% = genuine ownership (persists for
days); under 30% = intraday churn that can vanish by tomorrow - the column that separates a
multi-day move from a one-day pop".

DEL% IS A CONVICTION FILTER, NOT A DIRECTION SIGNAL

This is the load-bearing design decision here. Delivery percentage says what share of the day's
volume was carried to delivery rather than squared off; it says nothing about who was the
aggressor. High delivery on a falling stock is as easily patient value buying as it is
distribution. So direction is taken from STRUCTURE - where price closed relative to the value
area and the day's range, and what the Day Type resolved to - and Del% only decides whether
that structural read is backed by real ownership or by churn that evaporates overnight.

LONG ONLY, DELIBERATELY

There is no bearish mirror of this list, for a mechanical reason rather than a statistical one:
a BTST short is not possible in the cash segment - you cannot deliver stock you do not own.
Carrying a short overnight means futures or options, which is a different instrument with
different margin and lot sizing, not a flag on this scan. The short side is left to the
morning trend scans in scans.py, which pick up the continuation next session if it is real.

READ IT AS A WATCHLIST, NOT A SIGNAL. It also carries risk the intraday engine does not model:
an overnight position has no stop - a gap goes through it - so anything sized off an intraday
stop distance is sized wrong here.
"""

from __future__ import annotations

from datetime import datetime, time

import pandas as pd

from signal_engine.analysis.breakingtrade import volume_shapes
from signal_engine.analysis.breakingtrade.scans import (
    DELIVERY_GENUINE,  # noqa: F401  (the vendor's reference bar, quoted in delivery_threshold)
)

# The K period opens at 14:15; before that there is no closing ramp to read, and Del% is still
# accumulating. A snapshot earlier than this is flagged rather than silently trusted.
EARLIEST_USEFUL_TIME = time(14, 45)

# Absolute floor under the relative delivery bar - see delivery_threshold().
DELIVERY_FLOOR = 0.45

# Structural readings that say buyers held the day - any one of these qualifies.
BULLISH_TPO_POS = {"above_va", "near_day_high", "tpo_ext_high", "near_va_high"}

# Only DIRECTIONAL day types carry a close overnight. Neutral (either variant) and Normal are
# the cheat sheet's fade days - "fade the extremes", "sell IB high / buy IB low" - so a close
# on one of them is not a statement that buyers won, it is a statement that nobody did. The
# trend scans in scans.py already exclude them; BTST was inconsistent in not doing so.
#
# Measured over the 11 day-pairs available (2026-08-19..09-03): restricting to these day types
# took the sample from 96 trades to 51 and moved daily excess return from +0.05% (t=+0.48) to
# +0.44% (t=+1.85, 7/11 days positive). That t does NOT clear significance, and would not
# survive correcting for the handful of variants tried - it is reported as corroboration for a
# rule adopted on the vendor's documented logic, NOT as the reason for adopting it.
BULLISH_DAY_TYPES = {"Trend", "Normal Var", "Double Distribution"}
FADE_DAY_TYPES = {"Neutral Ext", "Neutral Center", "Normal", "Non-Trend"}


def _closed_strong(row) -> bool:
    """Did buyers finish in control? Structure only - no volume, no delivery."""
    day_type = row.get("day_type")
    if day_type in FADE_DAY_TYPES:
        return False
    if row.get("tpo_pos") in BULLISH_TPO_POS:
        return True
    return day_type in BULLISH_DAY_TYPES and row.get("day_type_dir") == "up"


def delivery_threshold(delivery: pd.Series, floor: float = DELIVERY_FLOOR) -> float:
    """ "High delivery" measured against TODAY'S universe rather than an absolute bar.

    The vendor's "above 70% = genuine ownership" is a statement about the market at large, and
    it does not survive contact with this universe: the scanner covers the 200+ NSE F&O names,
    which are precisely the most day-traded stocks there are, so their delivery runs
    structurally low. Measured on a real session the whole cross-section ran min 21% / median
    50% / max 72% - one single name cleared 70%, and an absolute bar would have reported "no
    candidates" every day while looking like it was working.

    So the bar is the day's top quartile, with an absolute floor so that a churny day cannot
    promote genuinely weak delivery just for being the least bad on offer.
    """
    usable = delivery.dropna()
    if usable.empty:
        return floor
    return max(float(usable.quantile(0.75)), floor)


def candidates(
    market_profile: pd.DataFrame,
    volume: pd.DataFrame,
    min_delivery_pct: float = None,
    executable: bool = True,
) -> pd.DataFrame:
    """Rank tomorrow's carry candidates from a closing-hour snapshot pair.

    A name qualifies on four independent legs: it closed green, structure says buyers held,
    delivery says the buying was real ownership, and the volume row shows a closing ramp.
    """
    if volume is None or volume.empty:
        raise ValueError("BTST needs the volume snapshot - Del% and the K/L/M sessions live there")

    duplicated = [c for c in ("sector", "price", "change_pct") if c in volume.columns]
    merged = market_profile.merge(volume.drop(columns=duplicated), on="symbol", how="inner")
    shaped = volume_shapes.label_shapes(merged)

    # WHICH SESSIONS THE "BUSY INTO THE CLOSE" READ IS ALLOWED TO USE.
    #
    # executable=True keeps to what can be READ IN TIME TO ACT. Since 2026-08-03 the NSE runs a
    # Closing Auction Session, and for F&O stocks - this entire universe - continuous trading
    # ends at 15:15. The L session only completes AT 15:15 and M IS the auction, so any rule
    # needing them yields a decision that cannot be acted on in the continuous market. That was
    # a look-ahead bug in the first version of this list, not a detail.
    #
    # It also costs nothing measurable and gains coverage. Over the 11 day-pairs available:
    #   K+L+M (look-ahead)          excess +0.188%, t=+1.08
    #   K only (actionable at 14:45) excess +0.134%, t=+0.65
    # and L/M are reported for only 74% of names against 100% for K - the old rule silently
    # discarded about 53 names every session.
    ramp_column = "closing_ramp_executable" if executable else "closing_ramp"
    ramp_letters = volume_shapes.EXECUTABLE_LETTERS if executable else volume_shapes.LATE_LETTERS
    required = [f"vol_{letter}" for letter in ramp_letters if f"vol_{letter}" in shaped.columns]

    # Only judge names carrying the data the judgement needs - scoring a name whose sessions are
    # simply absent is scoring missing data, and benchmarking a filtered subset against a
    # universe that still includes those names is not a like-for-like comparison.
    if required:
        shaped = shaped[shaped["delivery_pct"].notna() & shaped[required].notna().any(axis=1)]
    if shaped.empty:
        return shaped.head(0)

    if min_delivery_pct is None:
        min_delivery_pct = delivery_threshold(shaped["delivery_pct"])

    qualified = shaped[
        (shaped["change_pct"] > 0)
        & (shaped["delivery_pct"].notna())
        # >= not >: the threshold is a percentile of this same column, so on a small universe
        # the 75th percentile can equal the highest value and a strict > would exclude every row.
        & (shaped["delivery_pct"] >= min_delivery_pct)
        & shaped[ramp_column]
        & ~shaped["ghost_rally"]  # price up on no participation - the guide says do not chase
        & shaped.apply(_closed_strong, axis=1)
    ].copy()

    # Ranked by ownership first: among names that all passed the same structural test, the one
    # with the highest delivery is the one most likely to still be held tomorrow.
    qualified = qualified.sort_values(
        ["delivery_pct", "change_pct"], ascending=[False, False]
    ).reset_index(drop=True)

    columns = [
        c
        for c in (
            "symbol",
            "sector",
            "price",
            "change_pct",
            "delivery_pct",
            "day_type",
            "tpo_pos",
            "vol_j",
            "vol_k",
            "vol_l",
            "vol_m",
            "staircase",
            "lunch_anomaly",
        )
        if c in qualified.columns
    ]
    return qualified[columns]


def is_snapshot_late_enough(captured_at: datetime) -> bool:
    """The K/L/M sessions must have traded for a closing-ramp read to mean anything."""
    return captured_at is not None and captured_at.time() >= EARLIEST_USEFUL_TIME
