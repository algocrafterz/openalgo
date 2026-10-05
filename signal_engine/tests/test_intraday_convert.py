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


def test_bars_between_continuous_end_and_auction_are_outside_the_window():
    b = pd.concat([_full(CAS, last=914, auction=929), _bars(CAS, [(916, 1, 1, 1, 1, 0)])])
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


def test_missing_minute_threshold_invalidates_price():
    r = _ledger(_full(PRE), _ref(PRE))
    led = pd.DataFrame([r]).assign(structure_ok=True, missing_minutes=40)
    assert not ic.field_validity(led, missing_threshold=30).price_valid.item()
    assert ic.field_validity(led, missing_threshold=40).price_valid.item()


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
