"""Unit tests for the dedicated value-area rejection-fade adapter. Same style as
`test_open_drive.py`: hand-built bars, `prepare()` + `entry()` driven directly, no
stateful hooks to replay.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest.strategies.value_area_fade import FadeParams, ValueAreaFade
from signal_engine.backtest.types import Ctx

IST = "Asia/Kolkata"

ALL_OFF = dict(use_clv=False, use_volume=False, use_vwap_filter=False)


def params(**overrides) -> FadeParams:
    return FadeParams(**{**ALL_OFF, **overrides})


def _bars(day: str, start: str, rows: list[tuple]) -> pd.DataFrame:
    idx = pd.date_range(f"{day} {start}", periods=len(rows), freq="1min", tz=IST)
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"], index=idx)


def _quiet_context_days(base_price: float = 100.0, n_days: int = 3) -> list[pd.DataFrame]:
    """Flat sessions so VAH=VAL=POC=base_price and ATR converges to a small value."""
    out = []
    for k in range(n_days):
        rows = [(base_price, base_price + 0.05, base_price - 0.05, base_price, 50)
                for _ in range(30)]
        out.append(_bars(f"2026-06-0{k + 1}", "09:15", rows))
    return out


def _prepared_ctx(test_day_rows: list[tuple], p: FadeParams, test_day="2026-06-10",
                  start="09:15"):
    frames = _quiet_context_days() + [_bars(test_day, start, test_day_rows)]
    df = pd.concat(frames).sort_index()
    strat = ValueAreaFade()
    prepared = strat.prepare(df, p)
    return strat, Ctx(prepared, "TEST")


def test_rejection_fires_and_stop_clears_the_wick():
    rows = [(99.0, 101.5, 95.0, 101.0, 50)]   # Low(95) < VAL(100), Close(101) > VAL
    p = params()
    strat, c = _prepared_ctx(rows, p)
    last = len(c.index) - 1
    sig = strat.entry(c, last, p, 1)
    assert sig is not None
    assert sig.tag == "VAL_REJ"
    assert sig.sl < 95.0    # clears the wick, not just the level


def test_short_side_mirrors_long():
    rows = [(101.0, 105.0, 99.5, 99.0, 50)]   # High(105) > VAH(100), Close(99) < VAH
    p = params()
    strat, c = _prepared_ctx(rows, p)
    last = len(c.index) - 1
    sig = strat.entry(c, last, p, -1)
    assert sig is not None
    assert sig.tag == "VAH_REJ"
    assert sig.sl > 105.0


def test_clv_filter_blocks_a_weak_reclaim():
    # Wicks below VAL and closes back above it, but only barely - closing near the
    # bottom of its own range, not a decisive reclaim.
    rows = [(99.0, 101.5, 95.0, 95.5, 50)]
    p = params(use_clv=True)
    strat, c = _prepared_ctx(rows, p)
    last = len(c.index) - 1
    assert strat.entry(c, last, p, 1) is None


def test_lunch_window_is_excluded():
    rows = [(99.0, 101.5, 95.0, 101.0, 50)]
    p = params()
    strat, c = _prepared_ctx(rows, p, start="12:00")   # inside the excluded lunch window
    last = len(c.index) - 1
    assert strat.entry(c, last, p, 1) is None


def test_afternoon_window_is_allowed():
    rows = [(99.0, 101.5, 95.0, 101.0, 50)]
    p = params()
    strat, c = _prepared_ctx(rows, p, start="13:30")   # inside the afternoon window
    last = len(c.index) - 1
    assert strat.entry(c, last, p, 1) is not None


def test_target_is_poc_when_it_lies_meaningfully_ahead():
    # A flat context pins POC=VAL=VAH identically, which can never leave POC "ahead"
    # of a valid reclaim (Close must already be > VAL, so it can never also sit
    # below a POC equal to VAL). This needs POC and VAL genuinely separated: an
    # asymmetric volume profile (heaviest at 100, expansion favouring the low side -
    # same technique as test_volume_profile.py) pins POC~100.5, VAL~50.25, verified
    # below rather than hand-predicted, since the adaptive bucket size shifts exact
    # values slightly. Levels are spread wide (50/70/100/130/150) and walked through
    # gradually (not jumped to) so ATR - and therefore the stop buffer and risk -
    # stays small enough that the tp_min_r floor cannot mask the POC branch, the
    # mistake the first version of this fixture made.
    path = [50, 50, 50, 70, 70, 100, 100, 100, 100, 100, 130, 150]
    vols = [100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 50, 50]
    ctx_rows = [(pr, pr + 0.05, pr - 0.05, pr, v) for pr, v in zip(path, vols)]
    frames = [_bars(f"2026-06-0{k + 1}", "09:15", ctx_rows) for k in range(3)]
    # Reclaim closes BETWEEN VAL(~50.25) and POC(~100.5) - POC lies ahead of entry.
    test_day = _bars("2026-06-10", "09:15", [(70.0, 66.0, 49.0, 65.0, 50)])
    df = pd.concat(frames + [test_day]).sort_index()
    strat = ValueAreaFade()
    p = params(tp_mode="poc", tp_min_r=1.0)
    prepared = strat.prepare(df, p)
    c = Ctx(prepared, "TEST")
    last = len(c.index) - 1
    assert c["ppoc"][last] > c["Close"][last] > c["pval"][last]    # sanity: fixture is valid
    sig = strat.entry(c, last, p, 1)
    assert sig is not None
    assert sig.tp == pytest.approx(c["ppoc"][last])


def test_target_floors_at_minimum_r_when_poc_sits_too_close():
    # A shallow wick reclaiming just above VAL(100) at 100.05 - POC(100) is BEHIND
    # this entry, not ahead of it, so the floor/fallback path must engage rather
    # than promising an almost-zero-distance "target".
    rows = [(99.9, 100.5, 99.0, 100.05, 50)]
    p = params(tp_mode="poc", tp_min_r=1.0, tp_r_fallback=1.5)
    strat, c = _prepared_ctx(rows, p)
    last = len(c.index) - 1
    sig = strat.entry(c, last, p, 1)
    assert sig is not None
    px, risk = c["Close"][last], abs(c["Close"][last] - sig.sl)
    assert sig.tp - px >= risk * 1.0 - 1e-9   # never trivially close to entry


def test_min_wick_depth_blocks_a_shallow_wick_but_allows_a_deep_one():
    # Both wick below VAL(100), both close back above it - only the DEPTH differs.
    # ATR from the quiet context is small (~0.1), so "shallow" must be a fraction of
    # that, not merely a fraction of price, to actually stay under a 0.5-ATR bar.
    shallow = [(99.9, 100.5, 99.98, 100.2, 50)]   # wick to 99.98, barely past VAL
    deep = [(99.9, 100.5, 90.0, 100.2, 50)]       # wick to 90.0, far past VAL
    p = params(min_wick_depth_atr=0.5)
    _, c_shallow = _prepared_ctx(shallow, p)
    _, c_deep = _prepared_ctx(deep, p)
    strat = ValueAreaFade()
    last = len(c_shallow.index) - 1
    assert strat.entry(c_shallow, last, p, 1) is None
    assert strat.entry(c_deep, last, p, 1) is not None


def test_min_wick_depth_zero_reproduces_unfiltered_behaviour():
    rows = [(99.9, 100.5, 99.5, 100.2, 50)]   # the same shallow wick as above
    p = params(min_wick_depth_atr=0.0)
    strat, c = _prepared_ctx(rows, p)
    last = len(c.index) - 1
    assert strat.entry(c, last, p, 1) is not None


def test_day_type_filter_blocks_a_trend_day_allows_a_balance_day():
    # Prior session (the one whose VAL/VAH is being faded) is a Trend day: it spends
    # its first hour in a narrow IB then runs far beyond it, closing near the extreme.
    trend_ib = [(100 + k * 0.02, 100 + k * 0.02 + 0.05, 100 + k * 0.02 - 0.05,
                 100 + k * 0.02, 50) for k in range(60)]     # tight first-hour IB
    trend_run = [(101.2 + k * 0.3, 101.2 + k * 0.3 + 0.05, 101.2 + k * 0.3 - 0.05,
                  101.2 + k * 0.3, 50) for k in range(10)]    # runs far beyond IB
    trend_day = _bars("2026-06-04", "09:15", trend_ib + trend_run)
    balance_day = _bars("2026-06-05", "09:15",
                        [(100.0, 100.5, 99.5, 100.0, 50) for _ in range(30)])  # IB ~ day range

    rows = [(99.0, 101.5, 95.0, 101.0, 50)]   # a valid VAL-REJ wick, independent of day type
    p = params(day_type_ib_ratio_max=2.0)

    frames = _quiet_context_days(n_days=1) + [trend_day, _bars("2026-06-10", "09:15", rows)]
    df = pd.concat(frames).sort_index()
    strat = ValueAreaFade()
    prepared = strat.prepare(df, p)
    c = Ctx(prepared, "TEST")
    last = len(c.index) - 1
    assert strat.entry(c, last, p, 1) is None   # prior day was a Trend day - blocked

    frames = _quiet_context_days(n_days=1) + [balance_day, _bars("2026-06-10", "09:15", rows)]
    df = pd.concat(frames).sort_index()
    prepared = strat.prepare(df, p)
    c = Ctx(prepared, "TEST")
    last = len(c.index) - 1
    assert strat.entry(c, last, p, 1) is not None   # prior day was a Normal day - allowed


def test_va_confluence_filter_requires_agreement_with_session_n_minus_2():
    # Session N-2 has a flat, well-separated value area at 90.0 - far from the level
    # (VAL~100) being faded on the test day, so confluence must fail.
    far_context = _bars("2026-06-01", "09:15",
                        [(90.0, 90.5, 89.5, 90.0, 50) for _ in range(30)])
    near_va_day = _bars("2026-06-02", "09:15",
                        [(100.0, 100.5, 99.5, 100.0, 50) for _ in range(30)])
    rows = [(99.0, 101.5, 95.0, 101.0, 50)]   # VAL-REJ against near_va_day's VAL=100
    p = params(va_confluence_atr_mult=1.0)

    df = pd.concat([far_context, near_va_day, _bars("2026-06-10", "09:15", rows)]).sort_index()
    strat = ValueAreaFade()
    prepared = strat.prepare(df, p)
    c = Ctx(prepared, "TEST")
    last = len(c.index) - 1
    assert strat.entry(c, last, p, 1) is None   # N-2's value area (90.0) is far away

    # Same setup, but N-2 also sits at 100.0 - agrees with N-1, confluence passes.
    near_context = _bars("2026-06-01", "09:15",
                         [(100.0, 100.5, 99.5, 100.0, 50) for _ in range(30)])
    df = pd.concat([near_context, near_va_day, _bars("2026-06-10", "09:15", rows)]).sort_index()
    prepared = strat.prepare(df, p)
    c = Ctx(prepared, "TEST")
    last = len(c.index) - 1
    assert strat.entry(c, last, p, 1) is not None


def test_htf_trend_filter_blocks_a_long_fade_into_a_strong_downtrend():
    # A long steady multi-day downtrend so `prior_htf_z` reads a large negative value
    # by the test day, then a valid VAL-REJ wick (a fade UP, against that downtrend).
    down_days = [_bars(f"2026-05-{d:02d}", "09:15",
                       [(p, p + 0.05, p - 0.05, p, 50) for _ in range(30)])
                 for d, p in zip(range(1, 26), [200 - k * 6 for k in range(25)])]
    rows = [(99.0, 101.5, 95.0, 101.0, 50)]
    p = params(htf_trend_atr_mult=1.0, htf_ema_len=5, atr_len=5)

    df = pd.concat(down_days + [_bars("2026-06-10", "09:15", rows)]).sort_index()
    strat = ValueAreaFade()
    prepared = strat.prepare(df, p)
    c = Ctx(prepared, "TEST")
    last = len(c.index) - 1
    assert np.isfinite(c["prior_htf_z"][last]) and c["prior_htf_z"][last] < -1.0
    assert strat.entry(c, last, p, 1) is None   # long fade into an established downtrend


def test_r_fallback_mode_ignores_poc_entirely():
    rows = [(95.0, 101.5, 90.0, 101.0, 50)]
    p = params(tp_mode="r", tp_r_fallback=2.0)
    strat, c = _prepared_ctx(rows, p)
    last = len(c.index) - 1
    sig = strat.entry(c, last, p, 1)
    assert sig is not None
    px = c["Close"][last]
    risk = abs(px - sig.sl)
    assert sig.tp == pytest.approx(px + risk * 2.0)
