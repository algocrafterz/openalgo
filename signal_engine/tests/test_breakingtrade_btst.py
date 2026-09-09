"""Volume row shapes (volume_shapes.py) and the closing-hour carry watchlist (btst.py).

Both encode rules quoted from BreakingTrade_Full_Reference.docx - see those modules' docstrings
for the source wording each test is pinning.
"""

from datetime import datetime

import pandas as pd
import pytest

from signal_engine.analysis.breakingtrade import btst, volume_shapes


def _vol_row(change_pct=1.0, delivery_pct=0.8, **sessions):
    row = {"symbol": "TESTCO", "change_pct": change_pct, "delivery_pct": delivery_pct}
    for letter in volume_shapes.SESSION_LETTERS:
        row[f"vol_{letter}"] = sessions.get(letter, float("nan"))
    row["vol_o"] = sessions.get("o", float("nan"))
    return pd.Series(row)


# ---------------------------------------------------------------------------
# Row shapes
# ---------------------------------------------------------------------------


def test_staircase_needs_a_run_of_elevated_sessions():
    assert volume_shapes.is_staircase(_vol_row(a=1.3, b=1.4, c=1.25))
    assert not volume_shapes.is_staircase(_vol_row(a=1.3, b=0.9, c=1.25))


def test_staircase_excludes_extreme_sessions():
    """'Several consecutive elevated sessions, NOTHING EXTREME' - an extreme cell is a Spike."""
    assert not volume_shapes.is_staircase(_vol_row(a=1.3, b=3.5, c=1.25))


def test_spike_is_one_violent_cell_between_ordinary_ones():
    assert volume_shapes.is_spike(_vol_row(a=0.9, b=4.0, c=1.0))
    assert not volume_shapes.is_spike(_vol_row(a=1.3, b=4.0, c=1.4))


def test_opening_stub_o_is_never_counted():
    """O covers only 09:15-09:20 and runs an order of magnitude hotter; the guide says it
    sits inside session A and must not be double-counted."""
    row = _vol_row(o=118.0, a=0.9, b=0.8)
    assert not volume_shapes.is_spike(row)
    assert volume_shapes.session_factors(row) == [("a", 0.9), ("b", 0.8)]


def test_lunch_anomaly_looks_only_at_g_h_i():
    assert volume_shapes.is_lunch_anomaly(_vol_row(g=1.4))
    assert not volume_shapes.is_lunch_anomaly(_vol_row(a=4.0, b=3.0))


def test_closing_ramp_requires_busy_close_and_price_holding():
    assert volume_shapes.is_closing_ramp(_vol_row(change_pct=2.0, k=1.0, l=1.3, m=1.6))
    # a genuinely quiet close is not a ramp however green the day was
    assert not volume_shapes.is_closing_ramp(_vol_row(change_pct=2.0, k=1.6, l=0.4, m=0.5))
    # a busy close into a RED close is distribution, not overnight accumulation
    assert not volume_shapes.is_closing_ramp(_vol_row(change_pct=-2.0, k=1.0, l=1.3, m=1.6))


def test_closing_ramp_survives_a_single_dip():
    """Half-hour volume is noisy: DELHIVERY closed +2.1% on K 1.20 -> L 3.98 -> M 1.99, which
    a monotonic 'each session beats the last' rule wrongly rejected."""
    assert volume_shapes.is_closing_ramp(_vol_row(change_pct=2.1, k=1.20, l=3.98, m=1.99))


def test_closing_ramp_needs_participation_not_just_shape():
    assert not volume_shapes.is_closing_ramp(_vol_row(change_pct=2.0, k=0.2, l=0.3, m=0.4))


def test_ghost_rally_is_a_price_move_with_no_participation():
    assert volume_shapes.is_ghost_rally(_vol_row(change_pct=2.0, a=0.5, b=0.6, c=0.4))
    # same dead volume but no real move - not a ghost rally
    assert not volume_shapes.is_ghost_rally(_vol_row(change_pct=0.3, a=0.5, b=0.6))
    # real move WITH participation - not a ghost rally
    assert not volume_shapes.is_ghost_rally(_vol_row(change_pct=2.0, a=0.5, b=1.9))


def test_unfinished_sessions_do_not_read_as_dead_volume():
    """NaN means 'has not traded yet'; treating it as 0 would make every morning a ghost."""
    assert not volume_shapes.is_ghost_rally(_vol_row(change_pct=2.0, a=1.5, b=1.8))


# ---------------------------------------------------------------------------
# BTST watchlist
# ---------------------------------------------------------------------------


def _pair(**overrides):
    profile = {
        "symbol": "TESTCO",
        "sector": "IT",
        "price": 100.0,
        "change_pct": 2.0,
        "day_type": "Trend",
        "day_type_dir": "up",
        "tpo_pos": "above_va",
    }
    profile.update(overrides.get("profile", {}))

    volume = {"symbol": "TESTCO", "delivery_pct": 0.8, "change_pct": 2.0}
    for letter in volume_shapes.SESSION_LETTERS:
        volume[f"vol_{letter}"] = float("nan")
    # K is what the executable BTST read uses - it is complete at 14:45, in time to place a
    # delivery order before the 15:15 continuous-trading cutoff for F&O stocks.
    volume.update({"vol_k": 1.4, "vol_l": 1.3, "vol_m": 1.6})
    volume.update(overrides.get("volume", {}))

    return pd.DataFrame([profile]), pd.DataFrame([volume])


def test_qualifying_name_makes_the_watchlist():
    market_profile, volume = _pair()
    assert list(btst.candidates(market_profile, volume)["symbol"]) == ["TESTCO"]


def test_low_delivery_is_rejected_as_churn():
    market_profile, volume = _pair(volume={"delivery_pct": 0.25})
    assert btst.candidates(market_profile, volume).empty


def test_red_close_is_rejected():
    market_profile, volume = _pair(profile={"change_pct": -1.0}, volume={"change_pct": -1.0})
    assert btst.candidates(market_profile, volume).empty


def test_weak_closing_structure_is_rejected():
    """High delivery and a ramp are not enough if buyers did not finish in control."""
    market_profile, volume = _pair(
        profile={"tpo_pos": "below_va", "day_type": "Normal Var", "day_type_dir": "down"}
    )
    assert btst.candidates(market_profile, volume).empty


def test_ghost_rally_is_excluded_even_with_high_delivery():
    market_profile, volume = _pair(
        volume={"vol_k": 0.1, "vol_l": 0.2, "vol_m": 0.3, "vol_a": 0.4}
    )  # dead volume everywhere
    assert btst.candidates(market_profile, volume).empty


def test_btst_requires_a_volume_snapshot():
    market_profile, _ = _pair()
    with pytest.raises(ValueError):
        btst.candidates(market_profile, None)


def test_snapshot_time_gate():
    assert not btst.is_snapshot_late_enough(datetime(2026, 9, 3, 12, 31))
    assert btst.is_snapshot_late_enough(datetime(2026, 9, 3, 15, 20))
    assert not btst.is_snapshot_late_enough(None)


# ---------------------------------------------------------------------------
# K/L coverage-aware fallback (2026-09-09: live 14:50 polls found K barely
# published minutes after the session closed - see the long comment at
# candidates()'s ramp_column selection).
# ---------------------------------------------------------------------------


def _qualifying_row(symbol, **volume_overrides):
    profile = {
        "symbol": symbol, "sector": "IT", "price": 100.0, "change_pct": 2.0,
        "day_type": "Trend", "day_type_dir": "up", "tpo_pos": "above_va",
    }
    volume = {"symbol": symbol, "delivery_pct": 0.8, "change_pct": 2.0}
    for letter in volume_shapes.SESSION_LETTERS:
        volume[f"vol_{letter}"] = float("nan")
    volume.update(volume_overrides)
    return profile, volume


def _many(rows: list) -> tuple:
    """rows: list of (profile_dict, volume_dict) - build a multi-symbol snapshot pair, needed
    because coverage is a FRACTION across the universe, not a single-row property."""
    profiles, volumes = zip(*rows)
    return pd.DataFrame(list(profiles)), pd.DataFrame(list(volumes))


def test_sparse_k_falls_back_to_late_when_late_has_more_data():
    """K published for only 1/6 names (well under MIN_EXECUTABLE_COVERAGE), L+M published for
    all 6 - the wider read must be used, and the qualifying name (real L/M ramp) must appear."""
    rows = [_qualifying_row(f"FILLER{i}", vol_k=float("nan"), vol_l=1.4, vol_m=1.5) for i in range(5)]
    rows.append(_qualifying_row("TESTCO", vol_k=1.4, vol_l=1.3, vol_m=1.6))
    market_profile, volume = _many(rows)

    result = btst.candidates(market_profile, volume)

    assert "TESTCO" in list(result["symbol"])


def test_sparse_k_and_sparse_late_prefers_k_on_a_tie_or_better():
    """Both reads thin, but K still has at least as much data as L+M (the 2026-09-09 case at
    14:50) - must NOT fall back, since that made the real incident strictly worse (L+M was
    even sparser than K, 0% vs 3%)."""
    rows = [
        _qualifying_row(f"FILLER{i}", vol_k=1.4, vol_l=float("nan"), vol_m=float("nan"))
        for i in range(1)
    ]
    rows += [_qualifying_row(f"EMPTY{i}") for i in range(10)]  # no late-session data at all
    market_profile, volume = _many(rows)

    result = btst.candidates(market_profile, volume)

    # The one name with K data and a real ramp must still be findable via the K read.
    assert list(result["symbol"]) == ["FILLER0"]


def test_healthy_k_coverage_never_falls_back():
    """Above MIN_EXECUTABLE_COVERAGE, the executable (K-only) read is used exactly as before -
    this must not regress the common, healthy case."""
    rows = [_qualifying_row(f"FILLER{i}", vol_k=1.4, vol_l=float("nan")) for i in range(9)]
    rows.append(_qualifying_row("TESTCO", vol_k=1.4, vol_l=1.3, vol_m=1.6))
    market_profile, volume = _many(rows)

    result = btst.candidates(market_profile, volume)

    assert "TESTCO" in list(result["symbol"])
    assert "FILLER0" in list(result["symbol"])  # K-qualified names still included


def test_retrospective_candidates_uses_the_full_k_l_m_read():
    """A name with no K data but a real L+M ramp must still surface - retrospective_candidates()
    is meant to see everything the live/executable read structurally cannot."""
    market_profile, volume = _pair(volume={"vol_k": float("nan"), "vol_l": 1.4, "vol_m": 1.6})
    assert list(btst.retrospective_candidates(market_profile, volume)["symbol"]) == ["TESTCO"]


def test_non_executable_mode_is_unaffected_by_the_fallback():
    """executable=False already means 'use the wide read' - the coverage comparison only
    applies to the executable=True path."""
    market_profile, volume = _pair()
    result_default = btst.candidates(market_profile, volume, executable=False)
    assert list(result_default["symbol"]) == ["TESTCO"]
