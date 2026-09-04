"""BreakingTrade's own documented "ready-made scans" (scans.py) and latest-file
auto-discovery (extractor.discover_latest) - see signal_engine/analysis/breakingtrade/.
"""

import os
from datetime import datetime, time

import pandas as pd
import pytest

from signal_engine.analysis.breakingtrade import extractor, scans

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


# ---------------------------------------------------------------------------
# Volume Factor pace adjustment / colour scale
# ---------------------------------------------------------------------------


def test_pace_adjusted_surge_scales_by_elapsed_fraction():
    # Halfway through the session (12:22:30), a raw 1.0x surge implies 2.0x full-day pace.
    adjusted = scans.pace_adjusted_surge(1.0, time(12, 22, 30))
    assert adjusted == pytest.approx(2.0, rel=0.02)


def test_pace_adjusted_surge_none_outside_session():
    assert scans.pace_adjusted_surge(1.0, time(8, 0)) is None
    assert scans.pace_adjusted_surge(1.0, time(16, 0)) is None


def test_pace_adjusted_surge_none_for_missing_value():
    assert scans.pace_adjusted_surge(None, time(11, 0)) is None
    assert scans.pace_adjusted_surge(float("nan"), time(11, 0)) is None


@pytest.mark.parametrize(
    "value,expected",
    [
        (3.5, "extreme"),
        (2.0, "strong"),
        (1.3, "elevated"),
        (1.0, "normal"),
        (0.6, "thin"),
        (0.1, "dead"),
    ],
)
def test_volume_reading_color_scale(value, expected):
    assert scans.volume_reading(value) == expected


def test_volume_reading_no_data():
    assert scans.volume_reading(None) == "no data"


def test_delivery_reading_thresholds():
    assert scans.delivery_reading(0.75) == "genuine"
    assert scans.delivery_reading(0.20) == "churn"
    assert scans.delivery_reading(0.50) == "normal"
    assert scans.delivery_reading(None) is None


# ---------------------------------------------------------------------------
# Window status
# ---------------------------------------------------------------------------


def test_window_status_open_not_yet_closed():
    scan = scans.SCANS[0]  # The Runaway: 09:20-10:30
    assert scans.window_status(scan, time(9, 0)) == "NOT YET"
    assert scans.window_status(scan, time(9, 45)) == "OPEN"
    assert scans.window_status(scan, time(11, 0)) == "CLOSED"


def test_window_status_always_for_any_time_scan():
    live_print_scan = next(s for s in scans.SCANS if s.name == "Live Print in Formation Up")
    assert scans.window_status(live_print_scan, time(6, 0)) == "ALWAYS"


# ---------------------------------------------------------------------------
# Scan predicates - synthetic rows, so matches are exact rather than depending on
# whatever happened to be true in the live 2026-09-03 snapshot.
# ---------------------------------------------------------------------------


def _row(**overrides):
    base = {
        "opening": None,
        "open_type": None,
        "open_type_dir": None,
        "tail": None,
        "day_type": None,
        "day_type_dir": None,
        "tpo_pos": None,
        "tpo_pos_count": None,
        "tpo_pos_prev": None,
    }
    base.update(overrides)
    return pd.Series(base)


def test_the_runaway_matches_documented_combo():
    row = _row(opening="gap_up", open_type="open_drive", open_type_dir="up", tpo_pos="above_va")
    assert scans._the_runaway(row)


def test_the_runaway_rejects_partial_match():
    row = _row(opening="gap_up", open_type="open_drive", open_type_dir="up", tpo_pos="in_va")
    assert not scans._the_runaway(row)


def test_breakaway_above_pdh_matches():
    row = _row(tpo_pos_prev="above_pdh", tail="buy_tail", day_type="Trend", day_type_dir="up")
    assert scans._breakaway_above_pdh(row)


def test_live_print_up_requires_count_of_3_or_more():
    strong = _row(tpo_pos="tpo_ext_high", tpo_pos_count=3, day_type="Trend", day_type_dir="up")
    weak = _row(tpo_pos="tpo_ext_high", tpo_pos_count=2, day_type="Trend", day_type_dir="up")
    assert scans._live_print_up(strong)
    assert not scans._live_print_up(weak)


def test_gap_up_trap_is_a_bearish_scan():
    """A gap up rejected back below value resolves DOWN - the scan must be direction 'down'."""
    row = _row(opening="gap_up", open_type="rejection", open_type_dir="down", tpo_pos="below_va")
    assert scans._gap_up_trap(row)
    scan = next(s for s in scans.SCANS if s.name == "The Gap-Up Trap")
    assert scan.direction == "down"


def test_gap_down_rescue_requires_buy_tail():
    with_tail = _row(opening="gap_down", open_type="rejection", open_type_dir="up", tail="buy_tail")
    without_tail = _row(opening="gap_down", open_type="rejection", open_type_dir="up")
    assert scans._gap_down_rescue(with_tail)
    assert not scans._gap_down_rescue(without_tail)


def test_neutral_day_resolution_matches_both_directions():
    up = _row(day_type="Neutral Ext", day_type_dir="up", tpo_pos="above_va")
    down = _row(day_type="Neutral Ext", day_type_dir="down", tpo_pos="below_va")
    assert scans._neutral_resolution_up(up)
    assert scans._neutral_resolution_down(down)
    # the guide restricts this one to the closing stretch
    scan = next(s for s in scans.SCANS if s.name == "Neutral Day Resolution Up")
    assert scans.window_status(scan, time(11, 0)) == "NOT YET"
    assert scans.window_status(scan, time(14, 30)) == "OPEN"


def test_value_migration_accepts_documented_va_and_pdh_proxy():
    documented = _row(
        tpo_pos_prev="above_va",
        day_type="Double Distribution",
        day_type_dir="up",
        tpo_pos="above_va",
    )
    proxy = _row(
        tpo_pos_prev="above_pdh",
        day_type="Double Distribution",
        day_type_dir="up",
        tpo_pos="above_va",
    )
    assert scans._value_migration_up(documented)
    assert scans._value_migration_up(proxy)


def test_run_scans_returns_one_result_per_scan_definition():
    row_dict = _row(
        opening="gap_up",
        open_type="open_drive",
        open_type_dir="up",
        tpo_pos="above_va",
        day_type="Trend",
        day_type_dir="up",
    ).to_dict()
    row_dict.update(
        {"symbol": "TESTCO", "sector": "IT", "price": 100.0, "change_pct": 1.0, "ib_pct": 40.0}
    )
    market_profile = pd.DataFrame([row_dict])
    results = scans.run_scans(market_profile, volume=None, captured_at=datetime(2026, 9, 3, 9, 45))
    assert len(results) == len(scans.SCANS)
    runaway = next(r for r in results if r.scan.name == "The Runaway")
    assert "TESTCO" in runaway.matches["symbol"].values
    assert runaway.status == "OPEN"


# ---------------------------------------------------------------------------
# Latest-file auto-discovery
# ---------------------------------------------------------------------------


def test_discover_latest_finds_both_kinds():
    found = extractor.discover_latest(_EXCEL_DIR)
    assert set(found.keys()) == {"market_profile", "volume"}
    assert found["market_profile"].kind == "market_profile"
    assert found["volume"].kind == "volume"


def test_discover_latest_skips_unrecognized_file(tmp_path):
    bogus = tmp_path / "not_a_scanner_export.xlsx"
    pd.DataFrame({"Foo": [1], "Bar": [2]}).to_excel(bogus, index=False)

    real_mp = pd.read_excel(_MARKET_PROFILE_FILE, header=None)
    real_mp.to_excel(tmp_path / "market_profile.xlsx", index=False, header=False)

    found = extractor.discover_latest(str(tmp_path))
    assert "market_profile" in found
