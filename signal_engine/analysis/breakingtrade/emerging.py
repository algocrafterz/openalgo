"""Directional setups that have NOT already made their move - "room left".

THE PROBLEM THIS SOLVES

The named scans in scans.py are confirmation filters: by the time Day Type reads Trend and a
name is fifteen TPOs beyond the day's high, the move being confirmed is the move you missed.
The scan is right and the trade is gone. What is wanted instead is the set of names with the
STRUCTURE to move and the DISTANCE still to travel.

HOW "ROOM LEFT" IS MEASURED - AND WHAT WAS REJECTED

The obvious gauge is IB %, since the guide defines it as "how much of the day's range is
already used up" and calls a sub-30% reading a "coiled spring, the classic trend-day
candidate". That premise was TESTED against the 13 sessions available and did not survive:
names with IB% < 30 went on to make SMALLER subsequent moves than names with IB% > 60
(0.95% vs 1.17% mean absolute next-day move, difference -0.22%, t = -2.35). The direction is
the opposite of the folklore, and it is the most statistically solid result measured anywhere
in this exercise. It also has an obvious mechanism: volatility clusters, so a stock quiet for
an hour is most likely to stay quiet. That test was on the NEXT-day horizon rather than the
rest of the same day, so it does not strictly refute the intraday claim - but it is more than
enough reason not to build on it.

What is used instead is TPO EXTENSION COUNT, which measures the same idea directly and
locally: "3 TPO above Day's High" is a break that just happened; "15 TPO above Day's High" is a
move largely over. A name sitting AT the level it is breaking (near or just past the value
area, at the day's edge) has the extension still ahead of it.

The second gauge is relative: a name whose move so far already sits in the top of the day's
cross-section has, by definition, done most of its travelling. That bar comes from the day's
own distribution rather than a fixed percentage, so it self-calibrates to a quiet or wild day.

NOT VALIDATED. The named scans could at least be tested against next-day returns; this one is
about intraday continuation, which the vendor's day navigation cannot answer - it serves one
snapshot per day, at the close. Only live polling can settle it.
"""

from __future__ import annotations

import pandas as pd

from signal_engine.analysis.breakingtrade.scans import pace_adjusted_surge

# Directional day types only. Neutral and Normal are the cheat sheet's fade days and Non-Trend
# is its "don't trade" - none is a statement that one side is winning.
DIRECTIONAL_DAY_TYPES = {"Trend", "Normal Var", "Double Distribution"}

# Every open type EXCEPT a featureless auction earns a place. Open Drive is the strongest, but
# Rejection is what the vendor's own Gap-Up Trap and Gap-Down Rescue scans are built on, and a
# Test Drive is the Normal Variation setup - discarding them throws away most of the real
# opportunities. What matters is that the open type AGREES with where price now sits.
USABLE_OPEN_TYPES = {"open_drive", "test_drive", "rejection"}
OPEN_TYPE_QUALITY = {"open_drive": 3.0, "test_drive": 2.0, "rejection": 1.0}

# Positions meaning "at the level", with the extension still ahead.
FRESH_UP = {"above_va", "near_va_high", "near_day_high", "tpo_ext_high"}
FRESH_DOWN = {"below_va", "near_va_low", "near_day_low", "tpo_ext_low"}

# Beyond this many single-print TPOs past the day's extreme, the move is no longer fresh. The
# vendor's own "Live Print in Formation" scan treats 3+ stacking TPOs as the live confirmation
# ("3+ TPOs stacking beyond the extreme = a single print forming live, in real time"), so 3-5
# is the window where the print has formed but the move has not yet run.
MIN_TPO_EXTENSION = 3
MAX_TPO_EXTENSION = 5

# IB% band, from the 14 sessions measured (3,059 rows), NOT from folklore:
#
#   IB<30   directional day type 37.9% of the time, mean full-day move 0.68% - the QUIETEST
#           bucket and the least directional. The "coiled spring" reading is not supported.
#   30-60   50.9% directional, 0.96%
#   60-90   63.2% directional, 1.08%  <- the best bucket for a directional trade
#   >90     23.0% directional, 1.86%  - big ranges, but mostly Normal/Neutral chop
#
# One caveat kept in view: Day Type "Trend" NEVER appears above IB 60 in the data, but that is
# CIRCULAR - the cheat sheet defines a Trend day as "narrow IB, one-way extension", so the
# vendor cannot label a wide-IB day as Trend. Only the directional-rate and move-size columns
# above are independent evidence.
IB_MAX = 90.0
IB_PREFERRED = (30.0, 90.0)

# A name whose move so far is already above this quantile of the day's absolute moves has done
# most of its travelling.
ALREADY_MOVED_QUANTILE = 0.80

# Volume bar as a percentile of the day's own cross-section - see candidates().
VOLUME_PERCENTILE = 0.75


def _direction_of(row) -> str | None:
    """The one direction every leg agrees on, or None if they disagree."""
    day = row.get("day_type_dir")
    if day not in ("up", "down"):
        return None
    if row.get("day_type") not in DIRECTIONAL_DAY_TYPES:
        return None

    # Open type may CONTRADICT, but it is not required to be present. The vendor only assigns
    # one when the open was distinctive: on a real session 182 of 220 names carried no open
    # type at all, so demanding one discards 83% of the universe by construction and cut 97
    # directional names to 15. A name can perfectly well build a Normal Variation trend from a
    # nondescript open. So an open type that DISAGREES disqualifies; an absent one does not,
    # it only scores lower (see OPEN_TYPE_QUALITY).
    open_type = row.get("open_type")
    if open_type in USABLE_OPEN_TYPES and row.get("open_type_dir") != day:
        return None

    position = row.get("tpo_pos")
    if day == "up" and position in FRESH_UP:
        return "up"
    if day == "down" and position in FRESH_DOWN:
        return "down"
    return None


def _has_room(row, move_cap: float) -> bool:
    """Is the move still ahead of it rather than behind it?"""
    extension = row.get("tpo_pos_count")
    if pd.notna(extension) and extension > MAX_TPO_EXTENSION:
        return False

    # A range already blown wide open is chop, not a trend in waiting: above IB 90 only 23% of
    # names carried a directional day type, against 63% in the 60-90 band.
    ib_pct = row.get("ib_pct")
    if pd.notna(ib_pct) and float(ib_pct) > IB_MAX:
        return False

    change = row.get("change_pct")
    return pd.isna(change) or abs(float(change)) <= move_cap


def _confirmations(row, direction: str) -> list:
    """Corroborating structure - each is a separate reason to believe the direction.

    Tails and single prints are the cheat sheet's own supporting evidence: a tail is
    "excess - the auction succeeded, strong rejection" and doubles as the defined-risk stop
    ("Buy/Sell Tail = defined-risk stop just beyond the tail"), while a single print is a
    "fast rejection, future S/R" that the guide says to "use as profit targets when trading in
    their direction".
    """
    found = []
    tail = row.get("tail")
    if (direction == "up" and tail == "buy_tail") or (direction == "down" and tail == "sell_tail"):
        found.append("tail")

    if row.get("single_print") == "single_print" and row.get("single_print_dir") == direction:
        found.append("single_print")

    extension = row.get("tpo_pos_count")
    if pd.notna(extension) and MIN_TPO_EXTENSION <= extension <= MAX_TPO_EXTENSION:
        found.append(f"{int(extension)}tpo")

    ib_pct = row.get("ib_pct")
    if pd.notna(ib_pct) and IB_PREFERRED[0] <= float(ib_pct) <= IB_PREFERRED[1]:
        found.append("ib_band")
    return found


def candidates(
    market_profile: pd.DataFrame,
    volume: pd.DataFrame = None,
    captured_at=None,
    require_volume: bool = True,
) -> pd.DataFrame:
    """Names with directional structure that have not yet made the move.

    require_volume applies the guide's own workflow - "find the setup on the Intraday Scanner,
    confirm it here" - using the pace-adjusted volume factor, since a raw factor read at 10:30
    always understates.
    """
    frame = market_profile.copy()
    if volume is not None:
        frame = frame.merge(volume[["symbol", "surge_x", "delivery_pct"]], on="symbol", how="left")

    move_cap = float(frame["change_pct"].abs().quantile(ALREADY_MOVED_QUANTILE))

    frame["direction"] = frame.apply(_direction_of, axis=1)
    frame["room"] = frame.apply(lambda r: _has_room(r, move_cap), axis=1)
    picked = frame[frame["direction"].notna() & frame["room"]].copy()

    if "surge_x" in picked.columns and captured_at is not None:
        picked["pace_surge"] = picked["surge_x"].apply(
            lambda s: pace_adjusted_surge(s, captured_at.time())
        )
        if require_volume:
            # RELATIVE to today's cross-section, not the vendor's 1.2 "elevated" line. That
            # line describes a PER-SESSION volume factor; Surge x is a CUMULATIVE one, and the
            # two are not the same quantity. Measured intraday, pace-adjusted Surge x centres
            # near 0.3 rather than 1.0 (median raw 0.09 at 32% of the session on 04-Sep, and
            # 0.06 at 20% on 03-Sep), so an absolute 1.2 bar rejects essentially the whole
            # market every day - on 04-Sep it left 1 name out of 42, and that name was an
            # index. A cross-sectional bar asks the question that actually matters anyway:
            # is this name busy COMPARED WITH everything else right now.
            bar = frame["surge_x"].dropna()
            if not bar.empty:
                threshold = float(bar.quantile(VOLUME_PERCENTILE))
                picked = picked[picked["surge_x"].notna() & (picked["surge_x"] >= threshold)]

    picked["confirmations"] = picked.apply(
        lambda r: ",".join(_confirmations(r, r["direction"])) or "-", axis=1
    )
    picked["quality"] = (
        picked["open_type"].map(OPEN_TYPE_QUALITY).fillna(0)
        + picked["day_type"].eq("Trend").astype(float)
        + picked["confirmations"].apply(lambda c: 0 if c == "-" else len(c.split(",")))
    )

    columns = [
        c
        for c in (
            "symbol",
            "sector",
            "direction",
            "price",
            "change_pct",
            "day_type",
            "open_type",
            "tpo_pos",
            "tpo_pos_count",
            "ib_pct",
            "confirmations",
            "pace_surge",
            "quality",
        )
        if c in picked.columns
    ]
    return picked.sort_values(["quality", "change_pct"], ascending=[False, True])[
        columns
    ].reset_index(drop=True)
