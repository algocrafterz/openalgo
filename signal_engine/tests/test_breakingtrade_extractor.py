"""Extractor/scorer for breakingtrade.com scanner exports - see
signal_engine/analysis/breakingtrade/. Covers the two run-on-string parsing traps that
motivated keyword matching over generic string cleanup (see extractor.py's module
docstring), and the two real sample snapshots committed under
signal_engine/pinescripts/intraday/breaking-trade/excel/.
"""

import os
from datetime import datetime

import pandas as pd
import pytest

from signal_engine.analysis.breakingtrade import extractor, scorer

_EXCEL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "pinescripts",
    "intraday",
    "breaking-trade",
    "excel",
)
_MARKET_PROFILE_FILE = os.path.join(
    _EXCEL_DIR,
    "Market Profile Charts India  AI-Powered Live TPO Charts  Scanner  BreakingTrade.xlsx",
)
_VOLUME_FILE = os.path.join(
    _EXCEL_DIR,
    "Market Profile Charts India  AI-Powered Live TPO Charts  Scanner  BreakingTrade (1).xlsx",
)
# Produced by a DOM scrape rather than the browser's "save page as Excel" - different sheet
# layout, no '#' column, headers on row 3, Change % stored as a fraction.
_GENERATED_WORKBOOK_FILE = os.path.join(
    _EXCEL_DIR, "BreakingTrade_Intraday_Scan_20260903_1231IST.xlsx"
)


# ---------------------------------------------------------------------------
# Cell-level normalization
# ---------------------------------------------------------------------------


def test_split_corporate_action_tag():
    assert extractor._split_corporate_action("CANBK  Fund Raise") == ("CANBK", "Fund Raise")


def test_split_corporate_action_no_tag():
    # single space = legitimate multi-word name, not a corporate-action tag
    assert extractor._split_corporate_action("BANKNIFTY FUT") == ("BANKNIFTY FUT", None)


def test_normalize_cell_strips_camelcase_machine_code():
    assert (
        extractor._normalize_cell("Above VAH aboveYesterdayVAH", extractor._OPENING_KEYWORDS)
        == "above_prior_vah"
    )


def test_normalize_cell_empty_dash_is_none():
    assert extractor._normalize_cell("—", extractor._OPENING_KEYWORDS) is None


def test_normalize_cell_strips_trailing_bracket_tag():
    assert (
        extractor._normalize_cell("Failed High FailedToGoHigh {B}", extractor._SINGLEPRINT_KEYWORDS)
        == "failed_high"
    )


def test_normalize_tpo_pos_extension_count():
    code, count = extractor._normalize_tpo_pos("3 TPO ↑ High 3Single TPO above Days High {C}")
    assert code == "tpo_ext_high"
    assert count == 3


def test_normalize_tpo_pos_plain_position():
    code, count = extractor._normalize_tpo_pos("Near VA Hi Near Value Area High {C}")
    assert code == "near_va_high"
    assert count is None


def test_normalize_day_type_direction():
    base, direction = extractor._normalize_day_type("Normal Var ↓")
    assert base == "Normal Var"
    assert direction == "down"


def test_normalize_day_type_no_direction():
    base, direction = extractor._normalize_day_type("Non-Trend")
    assert base == "Non-Trend"
    assert direction is None


# ---------------------------------------------------------------------------
# Whole-file loading (real committed sample snapshots)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def market_profile_snapshot():
    return extractor.load_snapshot(_MARKET_PROFILE_FILE)


@pytest.fixture(scope="module")
def volume_snapshot():
    return extractor.load_snapshot(_VOLUME_FILE)


def test_market_profile_snapshot_kind_and_time(market_profile_snapshot):
    assert market_profile_snapshot.kind == "market_profile"
    assert market_profile_snapshot.captured_at is not None


def test_volume_snapshot_kind(volume_snapshot):
    assert volume_snapshot.kind == "volume"


def test_corporate_action_tag_does_not_corrupt_symbol(market_profile_snapshot):
    frame = market_profile_snapshot.frame
    row = frame[frame["symbol"] == "CANBK"]
    assert len(row) == 1
    assert row.iloc[0]["corporate_action"] == "Fund Raise"


def test_multiword_symbol_survives_intact(market_profile_snapshot):
    frame = market_profile_snapshot.frame
    assert "BANKNIFTY FUT" in frame["symbol"].values


def test_sector_enriched_from_sectors_yaml(market_profile_snapshot):
    frame = market_profile_snapshot.frame
    row = frame[frame["symbol"] == "HDFCBANK"]
    assert row.iloc[0]["sector"] == "BANKING"


def test_loads_dom_scraped_workbook_from_a_later_sheet():
    """The Chrome-extension workbook leads with a summary tab and carries the scanner table
    on sheet 2, with headers on row 3 and no '#' column - every sheet must be tried."""
    snapshot = extractor.load_snapshot(_GENERATED_WORKBOOK_FILE)
    assert snapshot.kind == "market_profile"
    assert len(snapshot.frame) > 200


def test_captured_at_falls_back_to_title_row_when_no_latest_time_column():
    snapshot = extractor.load_snapshot(_GENERATED_WORKBOOK_FILE)
    assert snapshot.captured_at == datetime(2026, 9, 3, 12, 31)


def test_fractional_change_column_is_rescaled_to_percent():
    """The browser export writes -1.75 for -1.75%; the DOM-scraped workbook writes -0.0175."""
    snapshot = extractor.load_snapshot(_GENERATED_WORKBOOK_FILE)
    assert snapshot.frame["change_pct"].abs().max() > 1.0


def test_unrecognized_columns_raise_format_error(tmp_path):
    bogus = tmp_path / "bogus.xlsx"
    pd.DataFrame({"Foo": [1], "Bar": [2]}).to_excel(bogus, index=False)
    with pytest.raises(extractor.SnapshotFormatError):
        extractor.load_snapshot(str(bogus))


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_rank_excludes_non_trend_day_type(market_profile_snapshot):
    watchlist = scorer.rank(market_profile_snapshot.frame, top_n=500, min_confirming_legs=0)
    assert watchlist.excluded_non_trend > 0
    ranked_symbols = set(watchlist.bullish["symbol"]) | set(watchlist.bearish["symbol"])
    non_trend_symbols = set(
        market_profile_snapshot.frame.loc[
            market_profile_snapshot.frame["day_type"] == "Non-Trend", "symbol"
        ]
    )
    assert ranked_symbols.isdisjoint(non_trend_symbols)


def test_rank_bullish_all_positive_bearish_all_negative(market_profile_snapshot):
    watchlist = scorer.rank(market_profile_snapshot.frame, top_n=500, min_confirming_legs=0)
    assert (watchlist.bullish["net_score"] > 0).all()
    assert (watchlist.bearish["net_score"] < 0).all()


def test_rank_respects_min_confirming_legs(market_profile_snapshot):
    loose = scorer.rank(market_profile_snapshot.frame, top_n=500, min_confirming_legs=0)
    strict = scorer.rank(market_profile_snapshot.frame, top_n=500, min_confirming_legs=6)
    assert len(strict.bullish) + len(strict.bearish) <= len(loose.bullish) + len(loose.bearish)


def test_rank_merges_volume_surge(market_profile_snapshot, volume_snapshot):
    watchlist = scorer.rank(
        market_profile_snapshot.frame,
        volume=volume_snapshot.frame,
        top_n=500,
        min_confirming_legs=0,
    )
    assert "surge_x" in watchlist.bullish.columns
