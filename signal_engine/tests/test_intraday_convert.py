"""Intraday conversion contract (INTRADAY-5M-DATASET-PLAN.md sections 4, 6, 7): one test per rule."""

from datetime import date, datetime

import pandas as pd
import pytest

from signal_engine.backtest.data_readiness import intraday_convert as ic
from signal_engine.timeutils import IST

T = ic.load_templates()
PRE = date(2024, 1, 2)      # before the closing auction
CAS = date(2026, 8, 4)      # with the closing auction


def _ts(day, minute, second=0):
    return int(datetime(day.year, day.month, day.day, minute // 60, minute % 60, second,
                        tzinfo=IST).timestamp())


def _bars(day, rows, symbol="AAA"):
    """rows: (minute, open, high, low, close, volume[, second])."""
    out = []
    for r in rows:
        m, o, h, low, c, v = r[:6]
        out.append({"symbol": symbol, "timestamp": _ts(day, m, r[6] if len(r) > 6 else 0),
                    "open": o, "high": h, "low": low, "close": c, "volume": v})
    return pd.DataFrame(out)


def _full(day, scale=1.0, vol=100, last=929, auction=None):
    """A complete session at price 100 (high 110 at 10:00, low 90 at 11:00, close 105)."""
    rows = []
    for m in range(555, last + 1):
        o = h = low = c = 100.0
        if m == 600:
            h = 110.0
        if m == 660:
            low = 90.0
        if m == last:
            c = 105.0
            h = 105.0
        rows.append((m, o * scale, h * scale, low * scale, c * scale, vol))
    if auction is not None:
        rows.append((auction, 106 * scale, 106 * scale, 106 * scale, 106 * scale, vol))
    return _bars(day, rows)


def _ref(day, **kw):
    base = {"symbol": "AAA", "session": pd.Timestamp(day), "raw_open": 100.0, "raw_high": 110.0,
            "raw_low": 90.0, "raw_volume": 37500.0, "price_factor": 1.0, "volume_factor": 1.0,
            "ref_close": 105.0, "raw_close": 105.0}
    base.update(kw)
    return pd.DataFrame([base])


def _ledger(bars, ref):
    return ic.convert_sessions(ic.summarise(bars, T), ref).iloc[0]


# ---------------------------------------------------------------- cleanup

def test_off_minute_and_pre_open_bars_are_removed_and_negative_volume_flagged():
    b = _bars(PRE, [(554, 1, 1, 1, 1, 5), (555, 1, 1, 1, 1, 5), (555, 1, 1, 1, 1, 7, 57),
                    (556, 1, 1, 1, 1, -3)])
    kept, removed, flags = ic.clean(b)
    assert kept.minute.tolist() == [555, 556]
    assert sorted(removed.rule) == ["C1_off_minute", "C2_pre_open"]
    assert flags.contaminated.item() and flags.negative_volume.item()


# ---------------------------------------------------------------- summary and template

def test_reference_close_is_the_last_continuous_bar_before_the_auction_era():
    s = ic.summarise(_full(PRE), T).iloc[0]
    assert s.C == 105.0 and s.missing_minutes == 0 and s.outside == 0 and not s.has_auction


def test_reference_close_is_the_auction_print_in_the_auction_era():
    s = ic.summarise(_full(CAS, last=914, auction=929), T)
    row = s.iloc[0]
    assert row.has_auction and row.n_auction == 1 and row.C == 106.0
    assert row.missing_minutes == 0 and row.outside == 0 and ic.structure_ok(s).item()


def test_bars_outside_every_window_and_zone_are_outside():
    # 16:30 is neither continuous trading, nor the closing zone, nor the auction print
    b = pd.concat([_full(CAS, last=914, auction=929), _bars(CAS, [(990, 1, 1, 1, 1, 0)])])
    s = ic.summarise(b, T)
    assert s.outside.item() == 1 and not ic.structure_ok(s).item()


def test_missing_minutes_count_the_continuous_window_only():
    b = _full(PRE)
    b = b[~b.timestamp.isin([_ts(PRE, 700), _ts(PRE, 701)])]
    assert ic.summarise(b, T).missing_minutes.item() == 2


def test_duplicate_and_bad_ohlc_fail_structure():
    b = pd.concat([_full(PRE), _bars(PRE, [(700, 100, 99, 98, 100, 1)])])
    s = ic.summarise(b, T)
    assert s.duplicates.item() == 1 and s.bad_ohlc.item() == 1 and not ic.structure_ok(s).item()


# ---------------------------------------------------------------- factor k (6.2)

def test_primary_factor_from_high_and_low():
    r = _ledger(_full(PRE, scale=0.5, vol=100), _ref(PRE))
    assert r.method == "high+low" and r.k == pytest.approx(0.5) and r.status == ic.CONVERTED


def test_fallback_open_close_when_the_extremes_lie_inside_the_nse_range():
    # NSE extremes 0.5% outside the minute range on both sides: high/low ratios disagree.
    r = _ledger(_full(PRE), _ref(PRE, raw_high=110.55, raw_low=89.55))
    assert r.method == "fallback:open+close" and r.k == pytest.approx(1.0)
    assert r.status == ic.CONVERTED_FALLBACK


def test_fallback_order_is_fixed():
    # open and close disagree; high agrees with open -> high+open is chosen before high+close.
    r = _ledger(_full(PRE), _ref(PRE, raw_low=89.4, ref_close=105.0 * 1.003))
    assert r.method == "fallback:high+open"


def test_no_agreeing_pair_is_unresolved():
    r = _ledger(_full(PRE), _ref(PRE, raw_open=101.0, raw_high=111.5, raw_low=89.0,
                                 ref_close=104.0))
    assert pd.isna(r.k) and r.status == ic.UNRESOLVED


def test_factor_above_the_bound_is_excluded():
    r = _ledger(_full(PRE, scale=1.02), _ref(PRE))
    assert r.k == pytest.approx(1.02) and r.status == "excluded:k_bound"


# ---------------------------------------------------------------- NSE range (6.3)

def test_minute_range_outside_the_nse_range_is_excluded():
    # open+close agree at k=1 but the minute high exceeds the NSE high.
    r = _ledger(_full(PRE), _ref(PRE, raw_high=108.0, raw_low=89.0))
    assert r.method == "fallback:open+close" and r.status == "excluded:range_not_contained"


def test_fallback_whose_unused_extreme_misses_the_nse_extreme_by_over_1pct_is_excluded():
    r = _ledger(_full(PRE), _ref(PRE, raw_high=112.0, raw_low=89.0))
    assert r.method == "fallback:open+close" and r.status == "excluded:fallback_unconfirmed"


# ---------------------------------------------------------------- volume (6.4)

def test_volume_scaled_inversely_with_a_split_picks_c_one_over_k():
    # 2:1 split adjusted by the vendor: prices halved, volume doubled.
    r = _ledger(_full(PRE, scale=0.5, vol=200), _ref(PRE))
    assert r.vol_c == pytest.approx(2.0) and r.vol_ratio == pytest.approx(1.0)
    assert r.status == ic.CONVERTED


def _valid(row):
    led = pd.DataFrame([row]).assign(structure_ok=True)
    return ic.field_validity(led).iloc[0]


def test_volume_outside_the_band_invalidates_volume_only():
    r = _ledger(_full(PRE, vol=200), _ref(PRE))
    v = _valid(r)
    assert r.status == ic.CONVERTED and not r.volume_ok
    assert v.price_valid and not v.volume_valid and v.final_bar_valid


def test_volume_below_nse_by_pre_open_share_is_accepted():
    r = _ledger(_full(PRE, vol=90), _ref(PRE))
    assert r.vol_ratio == pytest.approx(0.9) and r.status == ic.CONVERTED


# ---------------------------------------------------------------- final-bar convention (B2)

@pytest.mark.parametrize("last, official, want", [
    (105.0, 105.0, "both"), (106.0, 105.0, "official_close"), (105.0, 106.0, "last_trade"),
    (106.0, 107.0, "none")])
def test_final_bar_convention_before_the_auction(last, official, want):
    r = _ledger(_full(PRE), _ref(PRE, ref_close=last, raw_close=official))
    assert r.final_bar_convention == want and r.status == ic.CONVERTED
    assert _valid(r).final_bar_valid == (want != "none") and _valid(r).price_valid


def test_auction_era_final_bar_must_be_the_official_close():
    b = _full(CAS, last=914, auction=929)
    ok = _ref(CAS, ref_close=106.0, raw_close=106.0, raw_volume=36100.0)
    assert _ledger(b, ok).final_bar_convention == "official_close"
    bad = _ledger(b, ok.assign(ref_close=105.0, raw_close=105.0))
    assert bad.final_bar_convention == "none" and bad.status == ic.CONVERTED


def test_session_missing_its_opening_or_closing_minutes_is_partial():
    late = _full(PRE)
    late = late[late.timestamp >= late.timestamp.min() + 20 * 60]
    s = ic.summarise(late, T)
    assert not s.edge_ok.item()
    r = ic.convert_sessions(s, _ref(PRE)).assign(structure_ok=True)
    assert not ic.field_validity(r).price_valid.item()
    assert ic.summarise(_full(PRE), T).edge_ok.item()


def test_missing_minute_threshold_invalidates_price_unless_volume_is_complete():
    r = _ledger(_full(PRE), _ref(PRE))
    led = pd.DataFrame([r]).assign(structure_ok=True, missing_minutes=40, volume_ok=False)
    assert not ic.field_validity(led, missing_threshold=30).price_valid.item()
    assert ic.field_validity(led, missing_threshold=40).price_valid.item()
    # P2: the same sparse session with NSE-complete volume is kept and flagged
    full = led.assign(volume_ok=True)
    v = ic.field_validity(full, missing_threshold=30)
    assert v.price_valid.item() and v.sparse_complete.item()


def test_session_without_an_nse_row_is_excluded():
    assert _ledger(_full(PRE), _ref(PRE).assign(symbol="ZZZ")).status == "excluded:no_nse_row"


def test_open_difference_is_reported_not_blocking():
    r = _ledger(_full(PRE), _ref(PRE, raw_open=101.0))
    assert not r.open_ok and r.status == ic.CONVERTED


# ---------------------------------------------------------------- canonical bars

def test_canonical_bars_apply_k_price_factor_c_and_volume_factor():
    bars = _full(PRE, scale=0.5, vol=200)
    ledger = ic.convert_sessions(ic.summarise(bars, T),
                                 _ref(PRE, price_factor=0.25, volume_factor=0.5))
    out = ic.convert_bars(ic.with_clock(bars), ledger)
    assert out.high.max() == pytest.approx(110 * 0.25)
    assert out.volume.iloc[0] == pytest.approx(200 / 2 / 0.5)
    assert len(out) == len(bars)


def test_excluded_sessions_produce_no_bars():
    bars = _full(PRE)
    ledger = ic.convert_sessions(ic.summarise(bars, T), _ref(PRE, raw_high=108.0, raw_low=89.0))
    assert ic.convert_bars(ic.with_clock(bars), ledger).empty


def test_dr_session_uses_its_two_windows():
    day = date(2024, 3, 2)
    b = _bars(day, [(m, 100, 100, 100, 100, 10) for m in [*range(555, 600), *range(690, 750)]])
    s = ic.summarise(b, T)
    assert s.missing_minutes.item() == 0 and s.outside.item() == 0
    s = ic.summarise(pd.concat([b, _bars(day, [(650, 100, 100, 100, 100, 1)])]), T)
    assert s.outside.item() == 1


def test_template_covers_every_session_from_2016():
    assert ic.template_for(date(2016, 1, 1), T).windows == ((555, 929),)
    assert ic.template_for(date(2026, 7, 31), T).auction == ()
    assert ic.template_for(date(2026, 8, 3), T).auction == (928, 929)
    with pytest.raises(ValueError):
        ic.template_for(date(2015, 12, 31), T)


def test_constants_are_the_contract():
    assert (ic.AGREE, ic.K_MAX, ic.CONTAIN_REL, ic.CONTAIN_ABS, ic.FALLBACK_REACH) == (
        0.001, 1.01, 0.0005, 0.05, 0.01)
    assert (ic.VOL_LO, ic.VOL_HI, ic.OPEN_TOL) == (0.85, 1.02, 0.005)
    assert ic.FALLBACK_PAIRS == (("open", "close"), ("high", "open"), ("high", "close"),
                                 ("low", "open"), ("low", "close"))


# ---------------------------------------------------------------- timestamp alignment (B7)

UPL_DAY = date(2019, 6, 3)


def _shifted(day, symbol="UPL", seconds=(58, 59, 56), n=375):
    rows = []
    for i, m in enumerate(range(555, 555 + n)):
        sec = seconds[i % len(seconds)]
        ts = _ts(day, m - 1, sec)            # stamped a few seconds before minute m
        rows.append({"symbol": symbol, "timestamp": ts, "open": 100.0, "high": 100.0,
                     "low": 100.0, "close": 100.0, "volume": 10})
    return pd.DataFrame(rows)


def test_alignment_moves_every_stamp_up_to_its_minute_and_keeps_ohlcv():
    raw = _shifted(UPL_DAY)
    out, log = ic.align_timestamps(raw)
    assert log.applied.item() and log.reason.item() == "aligned"
    assert (out.second == 0).all() and out.minute.min() == 555 and out.minute.max() == 929
    pd.testing.assert_frame_equal(out[["open", "high", "low", "close", "volume"]].reset_index(
        drop=True), raw[["open", "high", "low", "close", "volume"]].reset_index(drop=True))
    assert ic.summarise(out, T).missing_minutes.item() == 0


@pytest.mark.parametrize("raw, reason", [
    # a session that mixes on-the-minute and early bars is never aligned
    (_shifted(UPL_DAY, seconds=(58, 0)), "not every bar 1-59 s before a minute"),
    # nothing to align
    (_shifted(UPL_DAY, seconds=(0,)), "already on the minute"),
])
def test_alignment_is_refused_unless_every_bar_matches(raw, reason):
    out, log = ic.align_timestamps(raw)
    assert not log.applied.item() and log.reason.item() == reason
    assert out.timestamp.tolist() == raw.timestamp.tolist()


def test_alignment_accepts_the_whole_59_second_window():
    # policy v2 (approved 2026-10-20): the measured early-stamp range for UPL is 1..59 s
    # (bar-level median 2 s, p99 20 s, max 59 s). Stamp 1 s early (second 59) up to 59 s early
    # (second 1) and everything between.
    raw = _shifted(UPL_DAY, seconds=(59, 1, 30, 45, 20))
    out, log = ic.align_timestamps(raw)
    assert log.applied.item() and log.second_min.item() == 1 and log.second_max.item() == 59
    assert (out.second == 0).all() and out.minute.min() == 555 and out.minute.max() == 929
    assert (out.timestamp >= raw.timestamp).all() and ((out.timestamp - raw.timestamp) < 60).all()


def test_alignment_never_moves_a_stamp_down_or_past_the_next_minute():
    raw = _shifted(UPL_DAY, seconds=(59, 1))
    out, _ = ic.align_timestamps(raw)
    moved = (out.timestamp - raw.timestamp).tolist()
    assert min(moved) >= 1 and max(moved) <= 59


def test_the_alignment_policy_scope_is_unchanged_and_only_the_window_widened():
    assert ic.ALIGNMENT["symbols"] == ["UPL"]
    assert str(ic.ALIGNMENT["first_session"]) == "2017-01-01"
    assert str(ic.ALIGNMENT["last_session"]) == "2021-03-04"
    assert ic.ALIGNMENT["max_seconds_before_minute"] == 59


def test_alignment_refuses_collisions():
    raw = _shifted(UPL_DAY, n=3)
    raw = pd.concat([raw, raw.iloc[[0]].assign(timestamp=raw.timestamp.iloc[0] + 1)])
    out, log = ic.align_timestamps(raw)
    assert not log.applied.item() and "two bars in one minute" in log.reason.item()


@pytest.mark.parametrize("symbol, day", [("SBIN", UPL_DAY), ("UPL", date(2021, 3, 5)),
                                         ("UPL", date(2016, 12, 30))])
def test_alignment_never_applies_outside_its_scope(symbol, day):
    raw = _shifted(day, symbol=symbol, n=5)
    out, log = ic.align_timestamps(raw)
    assert log.empty and out.timestamp.tolist() == raw.timestamp.tolist()


# ---------------------------------------------------------------- sparse_complete (P2)

def _sessions(**over):
    """One converted session that is sparse (250 missing minutes against N = 190)."""
    base = {"status": ic.CONVERTED, "structure_ok": True, "edge_ok": True, "missing_minutes": 250,
            "volume_ok": True, "final_bar_convention": "official_close"}
    base.update(over)
    return pd.DataFrame([base])


N = 190


def test_a_sparse_session_with_complete_volume_is_kept_and_flagged():
    v = ic.field_validity(_sessions(), missing_threshold=N)
    assert v.price_valid.item() and v.sparse_complete.item() and v.volume_valid.item()


@pytest.mark.parametrize("fail", [
    {"volume_ok": False},                       # volume outside the NSE band: not complete
    {"edge_ok": False},                         # no bar in the first or last 15 minutes
    {"structure_ok": False},                    # duplicate / outside-window / bad OHLC ...
    {"status": ic.UNRESOLVED},                  # price conversion failed
    {"status": "excluded:range_not_contained"},
    {"status": "excluded:fallback_unconfirmed"},
])
def test_a_sparse_session_failing_any_one_condition_stays_excluded(fail):
    v = ic.field_validity(_sessions(**fail), missing_threshold=N)
    assert not v.price_valid.item() and not v.sparse_complete.item()


def test_a_fallback_converted_sparse_session_qualifies_when_every_gate_passes():
    v = ic.field_validity(_sessions(status=ic.CONVERTED_FALLBACK), missing_threshold=N)
    assert v.price_valid.item() and v.sparse_complete.item()


def test_a_dense_session_is_valid_and_not_flagged_sparse():
    v = ic.field_validity(_sessions(missing_minutes=N), missing_threshold=N)
    assert v.price_valid.item() and not v.sparse_complete.item()


def test_the_sparse_boundary_is_strictly_above_n():
    assert not ic.field_validity(_sessions(missing_minutes=N), missing_threshold=N
                                 ).sparse_complete.item()
    assert ic.field_validity(_sessions(missing_minutes=N + 1), missing_threshold=N
                             ).sparse_complete.item()


def test_without_a_threshold_nothing_is_sparse():
    v = ic.field_validity(_sessions(), missing_threshold=None)
    assert v.price_valid.item() and not v.sparse_complete.item()


def test_sparse_complete_does_not_touch_the_final_bar_family():
    v = ic.field_validity(_sessions(final_bar_convention="none"), missing_threshold=N)
    assert v.price_valid.item() and not v.final_bar_valid.item()


def test_the_conversion_spec_version_is_two():
    assert ic.CONVERSION_SPEC_VERSION == "2"


# ---------------------------------------------------------------- closing-window separation (P3a)

def _cas_session(extra=()):
    return pd.concat([_full(CAS, last=914, auction=929), _bars(CAS, list(extra))]) if extra else \
        _full(CAS, last=914, auction=929)


def test_closing_zone_bars_are_quarantined_noise_not_structure_errors():
    # the vendor counter artefacts of 2026-08-03 onward: zero volume at 15:16, a negative
    # volume at 15:20 (15:15-15:29 is the closing window; the regular session ends 15:14)
    b = _cas_session([(916, 1, 1, 1, 1, 0), (920, 1, 1, 1, 1, -500)])
    s = ic.summarise(b, T)
    row = s.iloc[0]
    assert ic.structure_ok(s).item()
    assert (row.closing_noise_bars, row.closing_negvol_bars, row.negvol, row.outside) == (2, 1, 0, 0)
    assert row.auction_price == 106.0 and row.auction_volume == 100
    # the regular-window volume excludes both the noise and the auction print
    assert row.V == 100 * (914 - 555 + 1)


def test_negative_volume_inside_the_regular_window_still_fails_structure():
    s = ic.summarise(_cas_session([(700, 100, 100, 100, 100, -5)]), T)
    # (a second bar at minute 700 is also a duplicate; either way the session fails)
    assert s.negvol.item() == 1 and not ic.structure_ok(s).item()


def test_two_auction_prints_still_fail_structure():
    s = ic.summarise(_cas_session([(928, 105, 105, 105, 105, 10)]), T)
    assert s.n_auction.item() == 2 and not ic.structure_ok(s).item()


def test_without_an_auction_print_the_reference_close_is_the_last_regular_bar():
    s = ic.summarise(_full(CAS, last=914), T).iloc[0]
    assert s.n_auction == 0 and s.C == 105.0 and pd.isna(s.auction_price)
    led = ic.convert_sessions(ic.summarise(_full(CAS, last=914), T),
                              _ref(CAS, ref_close=105.0)).iloc[0]
    assert led.final_bar_convention == "none"          # the last bar is not the official close


def test_clean_flags_negative_volume_only_outside_the_closing_zone():
    in_zone = _bars(CAS, [(555, 1, 1, 1, 1, 5), (920, 1, 1, 1, 1, -9)])
    assert not ic.clean(in_zone)[2].negative_volume.item()
    regular = _bars(CAS, [(555, 1, 1, 1, 1, 5), (700, 1, 1, 1, 1, -9)])
    assert ic.clean(regular)[2].negative_volume.item()
    before = _bars(PRE, [(555, 1, 1, 1, 1, 5), (920, 1, 1, 1, 1, -9)])   # no zone before CAS
    assert ic.clean(before)[2].negative_volume.item()


def test_a_source_that_floors_negative_volume_is_invisible_to_c3():
    """Known limitation: from OpenAlgo 2.0.2.8 Flattrade history floors a negative bar volume to 0
    before it reaches the store, so C3 sees an ordinary zero-volume bar. The broker parser warns
    for each floored bar outside the closing session instead (broker/flattrade/api/data.py
    `_report_floored_volume`); this pins that C3 itself cannot catch it."""
    raw = _bars(CAS, [(555, 1, 1, 1, 1, 5), (700, 1, 1, 1, 1, -9)])
    floored = _bars(CAS, [(555, 1, 1, 1, 1, 5), (700, 1, 1, 1, 1, 0)])
    assert ic.clean(raw)[2].negative_volume.item()
    assert not ic.clean(floored)[2].negative_volume.item()


def test_split_closing_quarantines_by_origin_and_keeps_the_original_values():
    b = _cas_session([(916, 1, 2, 0.5, 1, 7), (920, 1, 1, 1, 1, -500), (928, 99, 99, 99, 99, 3)])
    legacy_kept, legacy_q = ic.split_closing(ic.with_clock(b), T, keep_auction=False)
    assert legacy_kept.minute.max() == 914                       # the whole closing window goes
    assert sorted(legacy_q.minute) == [916, 920, 928, 929]
    assert legacy_q[legacy_q.minute == 920].volume.item() == -500   # original values intact
    fetched_kept, fetched_q = ic.split_closing(ic.with_clock(b), T, keep_auction=True)
    assert sorted(fetched_kept[fetched_kept.minute > 914].minute) == [928, 929]
    assert sorted(fetched_q.minute) == [916, 920]
    pre_kept, pre_q = ic.split_closing(ic.with_clock(_full(PRE)), T, keep_auction=False)
    assert len(pre_kept) == len(_full(PRE)) and pre_q.empty      # nothing before CAS


# ---------------------------------------------------------------- special-day hours (P3b)

def test_2024_03_02_dr_preopen_and_closing_session_are_non_regular_not_errors():
    day = date(2024, 3, 2)
    t = ic.template_for(day, T)
    assert t.windows == ((555, 599), (690, 749)) and t.verification == "partial"
    b = _bars(day, [(m, 100, 100, 100, 100, 10) for m in [*range(555, 600), *range(690, 750)]]
              + [(m, 100, 100, 100, 100, 3) for m in [675, 680, 752, 760, 769]])
    s = ic.summarise(b, T).iloc[0]
    assert s.outside == 0 and s.closing_noise_bars == 5 and s.missing_minutes == 0
    stray = ic.summarise(pd.concat([b, _bars(day, [(600, 100, 100, 100, 100, 1)])]), T).iloc[0]
    assert stray.outside == 1                    # 10:00 is in no window or zone: not inferred


def test_2021_02_24_outage_day_uses_the_post_outage_windows():
    day = date(2021, 2, 24)
    t = ic.template_for(day, T)
    assert t.windows == ((555, 701), (944, 1019)) and t.zones == ((930, 943),)
    assert t.verification == "partial"
    b = _bars(day, [(m, 100, 100, 100, 100, 10) for m in [*range(555, 702), *range(944, 1020)]]
              + [(936, 100, 100, 100, 100, 2)])
    s = ic.summarise(b, T).iloc[0]
    assert s.outside == 0 and s.closing_noise_bars == 1 and s.missing_minutes == 0
    midday = ic.summarise(pd.concat([b, _bars(day, [(720, 100, 100, 100, 100, 1)])]), T).iloc[0]
    assert midday.outside == 1                   # 12:00 was inside the halt


def test_other_special_days_keep_their_original_hours():
    assert ic.template_for(date(2024, 5, 18), T).windows == ((555, 599), (690, 749))
    assert ic.template_for(date(2024, 5, 18), T).zones == () and \
        ic.template_for(date(2024, 5, 18), T).verification is None
    assert ic.template_for(date(2021, 2, 23), T).windows == ((555, 929),)
    assert ic.template_for(date(2026, 8, 4), T).zones == ((915, 929),)


# ---------------------------------------------------------------- corporate-action checks

def test_a_primary_conversion_reconciles_with_the_daily_dataset_and_is_not_a_fallback():
    # the vendor series is adjusted by 0.5; the daily dataset's factor for the session is 0.5
    r = _ledger(_full(PRE, scale=0.5), _ref(PRE, price_factor=0.5))
    assert r.k == pytest.approx(0.5) and r.daily_reconciled and not r.fallback_conversion


def test_a_fallback_conversion_is_flagged_and_reports_whether_it_reconciles():
    r = _ledger(_full(PRE), _ref(PRE, raw_high=110.55, raw_low=89.55))      # open+close fallback
    assert r.fallback_conversion and r.status == ic.CONVERTED_FALLBACK
    # the minute extremes sit inside NSE's: canonical high/low differ from the daily's by 0.5%
    assert not r.daily_reconciled


def _led(k, pf, session="2026-01-05", symbol="AAA"):
    return pd.DataFrame({"symbol": [symbol], "session": [pd.Timestamp(session)], "k": [k],
                         "price_factor": [pf]})


EV = pd.DataFrame({"symbol": ["AAA", "AAA", "AAA", "BBB"],
                   "ex_date": ["2026-01-10", "2026-03-01", "2025-06-01", "2026-02-01"],
                   "kind": ["split", "bonus", "split", "demerger"],
                   "price_factor": [0.5, 0.1, 0.25, 0.8]})


@pytest.mark.parametrize("k, pf, want", [
    (1.0, 0.05, "raw"),                       # the vendor series is raw
    (0.05, 0.05, "adjusted_to_daily"),        # the vendor series carries the daily factor
    (0.5, 0.05, "other_factor"),              # something in between
    (float("nan"), 0.05, "no_factor"),
    (1.0, 1.0, "raw"),
])
def test_basis_class(k, pf, want):
    assert ic.ca_checks(_led(k, pf), EV).basis_class.item() == want


def test_an_other_factor_is_explained_by_a_subset_of_the_later_events():
    # events after 2026-01-05: 0.5 (Jan 10) and 0.1 (Mar 1); the 2025 event is already behind
    assert ic.ca_checks(_led(0.5, 0.05), EV).k_explanation.item() == "subset_of_events"
    assert ic.ca_checks(_led(0.1, 0.05), EV).k_explanation.item() == "subset_of_events"
    assert ic.ca_checks(_led(0.07, 0.05), EV).k_explanation.item() == "unexplained"
    assert ic.ca_checks(_led(0.25, 0.05), EV).k_explanation.item() == "unexplained"   # past event
    assert ic.ca_checks(_led(0.97, 1.0), EV).k_explanation.item() == "dividend_like"
    assert pd.isna(ic.ca_checks(_led(1.0, 0.05), EV).k_explanation.item())           # raw


def test_sessions_before_an_approximate_demerger_are_flagged():
    flagged = ic.ca_checks(pd.concat([_led(1.0, 0.8, "2026-01-05", "BBB"),
                                      _led(1.0, 1.0, "2026-02-03", "BBB"),
                                      _led(1.0, 0.5, "2026-01-05", "AAA")], ignore_index=True), EV)
    assert list(flagged.ca_demerger_approx) == [True, False, False]


def test_non_unit_factor_sessions_are_marked_for_reconciliation_reporting():
    out = ic.ca_checks(pd.concat([_led(1.0, 0.8), _led(1.0, 1.0)], ignore_index=True), EV)
    assert list(out.non_unit_factor) == [True, False]
