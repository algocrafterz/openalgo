"""Broad conviction ranking across the whole snapshot - a fitted composite score, NOT
breakingtrade.com's own documented filters (those live in scans.py - use that as the primary
tool; see its module docstring for why). This is a secondary, exploratory view: cast a wider
net than the strict named scans and rank what falls out.

The weights encode the same reading a discretionary Market Profile trader gives this
vocabulary, corrected against the vendor's own cheat sheet
(signal_engine/pinescripts/intraday/breaking-trade/BreakingTrade_Cheat_Sheet.docx):

  - Day Type is the terminal read. "Trend" / "Double Distribution" are what the cheat sheet
    says to "hold with trailing stop - never fade"; "Normal Variation" is a moderate breakout
    ("trade the breakout, take profit early"). "Normal" and "Neutral" (any variant) are
    explicitly FADE days in the cheat sheet ("fade the extremes") - excluded here, not merely
    down-weighted, since they contradict a trend-only read. "Non-Trend" is "don't trade".
  - Open Drive > Test Drive > Rejection in conviction (cheat sheet: Open Drive -> "Trend Day",
    highest conviction; Rejection -> "Normal Day", a gap-fill expectation, not a trend one).
  - A directional Tail, a Single Print run, and today's IB extending outside yesterday's
    Value Area (Opening, TPO Pos vs prior) all corroborate the SAME auction reading when
    they agree - so agreement is rewarded multiplicatively (more confirming legs, not just
    more points), and a symbol with no majority direction scores 0 and is dropped.
  - IB % does not vote a direction. The live scanner guide defines the column as "how much of
    the day's range is already used up?" and puts <30% in its "Trend" bucket - "the narrowest
    first hours float to the top - these are coiled springs, the classic trend-day
    candidates". A wide IB means the range is already spent, which is a rotational/fade read,
    so it is a penalty here rather than a smaller bonus.
    (The cheat sheet docx's "narrow <80% / wide >120% of AVERAGE IB" line is a general Market
    Profile rule about IB width vs its own average - a DIFFERENT metric from this column.
    Using those numbers here was wrong; the scanner-guide numbers are the ones that apply.)

This produces a WATCHLIST, not a trade signal - see the package docstring.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

# (weight, sign) contribution when this feature fires in the "up" direction; sign flips for "down".
# Only Trend / Double Distribution / Normal Var are trend setups per the cheat sheet - Normal,
# Neutral Center and Neutral Ext are all explicitly "fade the extremes" days, excluded outright
# same as Non-Trend rather than merely down-weighted.
_DAY_TYPE_TREND_POTENTIAL = {
    "Trend": 3,
    "Double Distribution": 3,
    "Normal Var": 2,
    "Neutral Ext": None,
    "Neutral Center": None,
    "Normal": None,
    "Non-Trend": None,  # sentinel: excluded outright, see _is_tradeable_day
}
_OPEN_TYPE_WEIGHT = {"open_drive": 3.0, "test_drive": 2.0, "rejection": 1.0}
_OPENING_DIRECTION = {
    "above_prior_vah": ("up", 1.5),
    "gap_up": ("up", 1.0),
    "below_prior_val": ("down", 1.5),
    "gap_down": ("down", 1.0),
    "in_prior_value": (None, 0.0),
}
_TPO_POS_DIRECTION = {
    "above_va": ("up", 2.0),
    "near_va_high": ("up", 1.0),
    "near_day_high": ("up", 1.5),
    "tpo_ext_high": ("up", 2.5),
    "below_va": ("down", 2.0),
    "near_va_low": ("down", 1.0),
    "near_day_low": ("down", 1.5),
    "tpo_ext_low": ("down", 2.5),
    "in_va": (None, 0.0),
}
_TPO_POS_PREV_DIRECTION = {
    "above_pdh": ("up", 1.5),
    "below_pdl": ("down", 1.5),
    "in_pdr": (None, 0.0),
}

IB_PCT_NARROW = 30.0  # < = "coiled spring", the guide's own Trend bucket (scanner-guide)
IB_PCT_WIDE = 90.0  # > = normal (range) day per the glossary's own IB% buckets


def _is_tradeable_day(day_type: str | None) -> bool:
    return day_type is not None and _DAY_TYPE_TREND_POTENTIAL.get(day_type) is not None


def _signed(direction: str | None, weight: float) -> float:
    if direction == "up":
        return weight
    if direction == "down":
        return -weight
    return 0.0


def _score_row(row: pd.Series) -> pd.Series:
    day_type = row.get("day_type")
    if not _is_tradeable_day(day_type):
        return pd.Series(
            {"net_score": 0.0, "conviction": 0.0, "direction": None, "confirming_legs": 0}
        )

    contributions = []

    contributions.append(_signed(row.get("day_type_dir"), _DAY_TYPE_TREND_POTENTIAL[day_type]))

    open_type = row.get("open_type")
    if open_type in _OPEN_TYPE_WEIGHT:
        contributions.append(_signed(row.get("open_type_dir"), _OPEN_TYPE_WEIGHT[open_type]))

    opening = row.get("opening")
    if opening in _OPENING_DIRECTION:
        direction, weight = _OPENING_DIRECTION[opening]
        contributions.append(_signed(direction, weight))

    tail = row.get("tail")
    if tail == "buy_tail":
        contributions.append(2.0)
    elif tail == "sell_tail":
        contributions.append(-2.0)

    single_print = row.get("single_print")
    if single_print == "single_print":
        contributions.append(_signed(row.get("single_print_dir"), 2.0))
    elif single_print == "failed_high":
        contributions.append(-2.0)  # failed to extend the high - bearish reversal tell
    elif single_print == "failed_low":
        contributions.append(2.0)

    poor_hl = row.get("poor_hl")
    if poor_hl == "poor_high":
        contributions.append(0.5)  # unresolved high, susceptible to a further push up
    elif poor_hl == "poor_low":
        contributions.append(-0.5)

    tpo_pos = row.get("tpo_pos")
    if tpo_pos in _TPO_POS_DIRECTION:
        direction, weight = _TPO_POS_DIRECTION[tpo_pos]
        contributions.append(_signed(direction, weight))

    tpo_pos_prev = row.get("tpo_pos_prev")
    if tpo_pos_prev in _TPO_POS_PREV_DIRECTION:
        direction, weight = _TPO_POS_PREV_DIRECTION[tpo_pos_prev]
        contributions.append(_signed(direction, weight))

    ib_pct = row.get("ib_pct")
    ib_bonus = 0.0
    if pd.notna(ib_pct):
        if ib_pct < IB_PCT_NARROW:
            ib_bonus = 1.15
        elif ib_pct > IB_PCT_WIDE:
            ib_bonus = 0.75
        else:
            ib_bonus = 1.0

    net_score = sum(contributions) * ib_bonus
    confirming_legs = sum(1 for c in contributions if c != 0)
    direction = "up" if net_score > 0 else "down" if net_score < 0 else None

    return pd.Series(
        {
            "net_score": round(net_score, 2),
            "conviction": round(abs(net_score), 2),
            "direction": direction,
            "confirming_legs": confirming_legs,
        }
    )


@dataclass
class RankedWatchlist:
    bullish: pd.DataFrame
    bearish: pd.DataFrame
    excluded_non_trend: int


def rank(
    market_profile: pd.DataFrame,
    volume: pd.DataFrame | None = None,
    top_n: int = 20,
    min_confirming_legs: int = 3,
) -> RankedWatchlist:
    """Score every symbol and split into ranked bullish / bearish candidate tables.

    min_confirming_legs guards against a single strong feature (e.g. just a Buy Tail)
    dominating the score with no other corroborating structure - require several
    independent Market Profile legs to agree before a symbol counts as a candidate.
    """
    scored = market_profile.join(market_profile.apply(_score_row, axis=1))

    if volume is not None:
        scored = scored.merge(
            volume[["symbol", "surge_x", "delivery_pct"]], on="symbol", how="left"
        )

    excluded_non_trend = int((~scored["day_type"].apply(_is_tradeable_day)).sum())

    candidates = scored[scored["confirming_legs"] >= min_confirming_legs]

    keep_cols = [
        c
        for c in [
            "symbol",
            "sector",
            "price",
            "change_pct",
            "day_type",
            "open_type",
            "ib_pct",
            "net_score",
            "conviction",
            "confirming_legs",
            "surge_x",
            "delivery_pct",
        ]
        if c in candidates.columns
    ]

    bullish = (
        candidates[candidates["direction"] == "up"][keep_cols]
        .sort_values("conviction", ascending=False)
        .head(top_n)
        .reset_index(drop=True)
    )
    bearish = (
        candidates[candidates["direction"] == "down"][keep_cols]
        .sort_values("conviction", ascending=False)
        .head(top_n)
        .reset_index(drop=True)
    )

    return RankedWatchlist(bullish=bullish, bearish=bearish, excluded_non_trend=excluded_non_trend)
