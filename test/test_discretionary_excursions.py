"""MAE / MFE of discretionary positions (Phase 2A): the pure quantity-path calculation, the
lazy service read (Historify bars replaced by a fake reader, no DuckDB) and the aggregates.

The pure tests check ``an.position_excursion`` against an independent reference (plain loops,
a different algorithm: state lookup per minute instead of interval iteration) on randomized
paths, plus hand-computed scenarios. Run with:
    pytest -p no:cacheprovider test/test_discretionary_excursions.py -q
"""
# ruff: noqa: F811  (the env fixture is imported from test_discretionary_pnl_service)

import math
import random
import statistics
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest
from test_discretionary_pnl_service import _clock_only, d, env  # noqa: F401

from services import discretionary_analytics as an
from services import discretionary_pnl_service as pnl

IST = timezone(timedelta(hours=5, minutes=30))


def nse_calendar(day, exchange):
    """NSE 09:15-15:30 on weekdays."""
    if day.weekday() >= 5:
        return None
    base = datetime.combine(day, datetime.min.time())
    return an.SessionWindow(base.replace(hour=9, minute=15), base.replace(hour=15, minute=30))


def at(h, m, s=0, day=16):
    return datetime(2026, 11, day, h, m, s)


def F(role, qty, price, when):
    return {"time": when, "role": role, "qty": qty, "price": price}


def bars_for(first, last, high, low, **override):
    """Flat bars (high, low) for every minute from ``first`` to ``last`` inclusive;
    ``override`` maps 'HH:MM' to (high, low)."""
    out = {}
    m = first.replace(second=0)
    while m <= last:
        out[m] = override.get(m.strftime("%H:%M"), (high, low))
        m += timedelta(minutes=1)
    return out


def run(direction, fills, bars, exchange="NSE", provider=nse_calendar):
    return an.position_excursion(direction, fills, bars, exchange, provider)


# ---------------------------------------------------------------- reference implementation


def ref_excursion(direction, fills, bars):
    """Reference: replay the fills for the state, then per observation compute the per-unit
    excursions. A bar contributes its extremes only in a minute with no fill (strictly inside
    the first..last fill minutes) against the state after the last fill of an earlier minute;
    every fill price is observed against the state before and after that fill."""
    sign = 1.0 if direction == "LONG" else -1.0
    qty = avg = 0.0
    states = []
    for f in fills:
        before = (qty, avg)
        if f["role"] == "ENTRY":
            avg = (avg * qty + f["price"] * f["qty"]) / (qty + f["qty"])
            qty += f["qty"]
        else:
            qty -= f["qty"]
        states.append((f, before, (qty, avg)))
    obs = []
    for f, before, after in states:
        obs += [(f["price"], f["price"], *before), (f["price"], f["price"], *after)]
    minute = lambda t: t.replace(second=0, microsecond=0)  # noqa: E731
    fill_minutes = {minute(f["time"]) for f in fills}
    lo, hi = minute(fills[0]["time"]), minute(fills[-1]["time"])
    for m, (high, low) in bars.items():
        if lo < m < hi and m not in fill_minutes:
            prior = [after for f, _b, after in states if minute(f["time"]) < m]
            obs.append((high, low, *prior[-1]))
    mae = mfe = mae_amt = mfe_amt = 0.0
    for high, low, q, a in obs:
        if q <= 1e-9:
            continue
        adverse = max(0.0, sign * (a - (low if sign > 0 else high)))
        favourable = max(0.0, sign * ((high if sign > 0 else low) - a))
        mae, mfe = max(mae, adverse), max(mfe, favourable)
        mae_amt, mfe_amt = max(mae_amt, adverse * q), max(mfe_amt, favourable * q)
    return mae, mfe, mae_amt, mfe_amt


def random_path(rng):
    """A consistent random position path inside one session: entries, maybe scale-ins and
    partial exits, always flat at the end."""
    direction = rng.choice(("LONG", "SHORT"))
    when = at(9, 20) + timedelta(seconds=rng.randrange(0, 600))
    fills = []
    qty = 0
    for _ in range(rng.randrange(1, 4)):
        amount = rng.randrange(1, 20)
        fills.append(F("ENTRY", amount, round(rng.uniform(90, 110), 2), when))
        qty += amount
        when += timedelta(seconds=rng.randrange(61, 900))
    while qty > 0:
        amount = qty if rng.random() < 0.5 else rng.randrange(1, qty + 1)
        fills.append(F("EXIT", amount, round(rng.uniform(85, 115), 2), when))
        qty -= amount
        when += timedelta(seconds=rng.randrange(61, 900))
    return direction, fills


@pytest.mark.parametrize("seed", range(40))
def test_path_excursion_matches_reference_on_random_paths(seed):
    rng = random.Random(seed)
    direction, fills = random_path(rng)
    bars = {}
    m = fills[0]["time"].replace(second=0)
    while m <= fills[-1]["time"]:
        low = round(rng.uniform(80, 105), 2)
        bars[m] = (round(low + rng.uniform(0, 12), 2), low)
        m += timedelta(minutes=1)
    got = run(direction, fills, bars)
    assert got["status"] == "available", got
    mae, mfe, mae_amt, mfe_amt = ref_excursion(direction, fills, bars)
    assert got["mae_points"] == pytest.approx(mae, abs=1e-3)
    assert got["mfe_points"] == pytest.approx(mfe, abs=1e-3)
    assert got["mae_amount"] == pytest.approx(mae_amt, abs=0.01)
    assert got["mfe_amount"] == pytest.approx(mfe_amt, abs=0.01)
    assert got["minute_bar_precision"] is True and got["bound"] == "lower_bound"
    assert math.isfinite(got["mae_points"]) and got["mae_points"] >= 0 <= got["mfe_points"]


# ---------------------------------------------------------------- hand-computed scenarios


def test_long_path_known_values_and_edge_bars_are_not_used_for_extremes():
    fills = [F("ENTRY", 10, 100.0, at(9, 30, 30)), F("EXIT", 10, 104.0, at(9, 35, 10))]
    bars = bars_for(
        at(9, 30),
        at(9, 35),
        101,
        99,
        **{"09:30": (150, 50), "09:35": (150, 50), "09:32": (103, 98), "09:33": (105, 100)},
    )
    got = run("LONG", fills, bars)
    # entry / exit minute bars (150 / 50) are ignored: only the fill prices count there
    assert (got["mae_points"], got["mfe_points"]) == (2.0, 5.0)  # low 98, high 105 vs avg 100
    assert (got["mae_amount"], got["mfe_amount"]) == (20.0, 50.0)
    assert got["bars_used"] == 4 and got["bars_expected"] == 6


def test_short_path_is_mirrored():
    fills = [F("ENTRY", 10, 100.0, at(9, 30, 30)), F("EXIT", 10, 97.0, at(9, 36, 0))]
    bars = bars_for(at(9, 30), at(9, 36), 100.5, 99.5, **{"09:32": (103, 99), "09:33": (101, 95)})
    got = run("SHORT", fills, bars)
    # adverse = high - avg = 3, favourable = avg - low = 5
    assert (got["mae_points"], got["mfe_points"]) == (3.0, 5.0)
    assert (got["mae_amount"], got["mfe_amount"]) == (30.0, 50.0)


def test_scale_in_uses_the_weighted_average_and_amount_may_differ_from_points():
    fills = [
        F("ENTRY", 10, 100.0, at(9, 30, 5)),
        F("ENTRY", 10, 110.0, at(9, 34, 5)),  # average 105 from here, 20 units
        F("EXIT", 20, 108.0, at(9, 38, 5)),
    ]
    bars = bars_for(
        at(9, 30),
        at(9, 38),
        104,
        104,
        **{
            "09:31": (101, 97),  # qty 10, avg 100: adverse 3
            "09:36": (111, 101),  # qty 20, avg 105: adverse 4, favourable 6
        },
    )
    got = run("LONG", fills, bars)
    # the second fill price 110 is a favourable 10 against the OLD average (100, 10 units)
    assert got["mfe_points"] == 10.0 and got["mae_points"] == 4.0
    assert got["mfe_amount"] == 120.0  # 6 points x 20 units beats 10 points x 10 units
    assert got["mae_amount"] == 80.0  # 4 x 20 beats 3 x 10


def test_partial_exit_reduces_the_open_quantity_for_later_bars():
    fills = [
        F("ENTRY", 20, 100.0, at(9, 30, 5)),
        F("EXIT", 10, 106.0, at(9, 33, 5)),
        F("EXIT", 10, 98.0, at(9, 37, 5)),
    ]
    bars = bars_for(
        at(9, 30),
        at(9, 37),
        101,
        99,
        **{"09:31": (104, 99), "09:35": (101, 95)},  # before the exit: 20 units; after: 10
    )
    got = run("LONG", fills, bars)
    assert got["mfe_points"] == 6.0 and got["mfe_amount"] == 120.0  # 106 fill, 20 units before
    assert got["mae_points"] == 5.0 and got["mae_amount"] == 50.0  # low 95 with 10 units left


def test_never_negative_when_price_never_goes_against_or_for():
    fills = [F("ENTRY", 5, 100.0, at(9, 30, 5)), F("EXIT", 5, 100.0, at(9, 33, 5))]
    got = run("LONG", fills, bars_for(at(9, 30), at(9, 33), 100, 100))
    assert (got["mae_points"], got["mfe_points"], got["mae_amount"], got["mfe_amount"]) == (
        0,
        0,
        0,
        0,
    )


def test_two_fills_in_one_minute_have_no_interior_bars_but_are_observed():
    fills = [F("ENTRY", 10, 100.0, at(9, 30, 5)), F("EXIT", 10, 103.0, at(9, 30, 50))]
    got = run("LONG", fills, {at(9, 30): (200.0, 1.0)})
    assert (got["mae_points"], got["mfe_points"]) == (0.0, 3.0) and got["bars_used"] == 0


# ---------------------------------------------------------------- unavailable, never estimated


def test_missing_minute_bar_is_unavailable_with_no_numbers():
    fills = [F("ENTRY", 10, 100.0, at(9, 30, 5)), F("EXIT", 10, 104.0, at(9, 35, 5))]
    bars = bars_for(at(9, 30), at(9, 35), 101, 99)
    for gone in ("09:30", "09:33", "09:35"):  # edge minutes count as coverage too
        cut = {m: v for m, v in bars.items() if m.strftime("%H:%M") != gone}
        got = run("LONG", fills, cut)
        assert got["status"] == "unavailable" and got["reason"] == "missing_bars"
        assert got["mae_points"] is None and got["mfe_points"] is None
        assert got["mae_amount"] is None and got["mfe_amount"] is None
    assert run("LONG", fills, {})["reason"] == "missing_bars"


def test_invalid_bars_are_rejected():
    fills = [F("ENTRY", 10, 100.0, at(9, 30, 5)), F("EXIT", 10, 104.0, at(9, 32, 5))]
    for bad in ((float("nan"), 99.0), (98.0, 99.0), (101.0, 0.0), (float("inf"), 99.0)):
        bars = bars_for(at(9, 30), at(9, 32), 101, 99, **{"09:31": bad})
        assert run("LONG", fills, bars)["reason"] == "invalid_bars"


def test_fill_time_problems_make_it_unavailable_not_average_entry():
    ok = [F("ENTRY", 10, 100.0, at(9, 30, 5)), F("EXIT", 10, 104.0, at(9, 35, 5))]
    bars = bars_for(at(9, 30), at(9, 35), 101, 99)
    assert run("LONG", ok, bars)["status"] == "available"
    missing = [dict(ok[0], time=None), ok[1]]
    assert run("LONG", missing, bars)["reason"] == "fill_times_unreliable"
    assert run("LONG", [ok[1], ok[0]], bars)["reason"] == "fill_times_unreliable"  # backwards
    same = [F("ENTRY", 10, 100.0, at(9, 30, 5)), F("EXIT", 10, 104.0, at(9, 30, 5))]
    assert run("LONG", same, bars)["reason"] == "fill_times_unreliable"  # ambiguous order
    # equal timestamps of the SAME role are fine (the order does not matter)
    scale = [
        F("ENTRY", 5, 100.0, at(9, 30, 5)),
        F("ENTRY", 5, 102.0, at(9, 30, 5)),
        F("EXIT", 10, 104.0, at(9, 35, 5)),
    ]
    assert run("LONG", scale, bars)["status"] == "available"
    assert run("LONG", [], bars)["reason"] == "no_fills"


def test_inconsistent_quantity_path_is_unavailable():
    bars = bars_for(at(9, 30), at(9, 35), 101, 99)
    over = [F("ENTRY", 10, 100.0, at(9, 30, 5)), F("EXIT", 12, 104.0, at(9, 35, 5))]
    assert run("LONG", over, bars)["reason"] == "inconsistent_fills"
    open_end = [F("ENTRY", 10, 100.0, at(9, 30, 5)), F("EXIT", 4, 104.0, at(9, 35, 5))]
    assert run("LONG", open_end, bars)["reason"] == "inconsistent_fills"
    exit_first = [F("EXIT", 10, 100.0, at(9, 30, 5)), F("ENTRY", 10, 104.0, at(9, 35, 5))]
    assert run("LONG", exit_first, bars)["reason"] == "inconsistent_fills"
    for bad in (float("nan"), float("inf"), 0.0, -1.0):
        fills = [F("ENTRY", 10, bad, at(9, 30, 5)), F("EXIT", 10, 104.0, at(9, 35, 5))]
        assert run("LONG", fills, bars)["reason"] == "inconsistent_fills"
    assert run("FLAT", over, bars)["status"] == "unavailable"


def test_fill_outside_the_session_or_calendar_down():
    pre_open = [F("ENTRY", 10, 100.0, at(9, 10)), F("EXIT", 10, 104.0, at(9, 35, 5))]
    assert run("LONG", pre_open, {})["reason"] == "fill_outside_session"
    saturday = [F("ENTRY", 10, 100.0, at(10, 10, day=14)), F("EXIT", 10, 104.0, at(10, 11, day=14))]
    assert run("LONG", saturday, {})["reason"] == "fill_outside_session"

    def down(day, exchange):
        raise an.SessionCalendarUnavailable("down")

    ok = [F("ENTRY", 10, 100.0, at(9, 30, 5)), F("EXIT", 10, 104.0, at(9, 35, 5))]
    assert run("LONG", ok, {}, provider=down)["reason"] == "session_calendar_unavailable"


def test_overnight_position_needs_every_session_minute_of_both_days():
    fills = [F("ENTRY", 10, 100.0, at(15, 0, 5)), F("EXIT", 10, 104.0, at(9, 30, 5, day=17))]
    bars = {}
    m = at(15, 0)
    while m <= at(15, 29):
        bars[m] = (101.0, 99.0)
        m += timedelta(minutes=1)
    m = at(9, 15, day=17)
    while m <= at(9, 30, day=17):
        bars[m] = (101.0, 99.0)
        m += timedelta(minutes=1)
    got = run("LONG", fills, bars)
    # the window of day 17 is clipped to the first..last fill minutes: 09:15..09:30
    assert got["status"] == "available" and got["bars_expected"] == 30 + 16
    del bars[at(9, 20, day=17)]
    assert run("LONG", fills, bars)["reason"] == "missing_bars"


# ---------------------------------------------------------------- segment, profit capture, aggregates


@pytest.mark.parametrize(
    ("exchange", "symbol", "ok"),
    [
        ("NSE", "SBIN", True),
        ("nse", "RELIANCE", True),
        ("NSE", "M&M", True),
        ("NFO", "NIFTY26NOV2624500CE", False),
        ("NSE", "NIFTY26NOV2624500CE", False),
        ("NSE", "NIFTY26NOV26FUT", False),
        ("NSE", "NIFTY INDEX", False),
        ("BSE", "SBIN", False),
        ("BFO", "SENSEX26NOV26FUT", False),
        ("MCX", "CRUDEOIL", False),
        ("CDS", "USDINR", False),
        ("NSE", "", False),
        (None, "SBIN", False),
    ],
)
def test_segment_support_is_nse_equity_only(exchange, symbol, ok):
    assert an.excursion_segment_supported(exchange, symbol) is ok


def test_profit_capture_winners_only_and_none_without_mfe():
    assert an.profit_capture(100.0, 4.0, 5.0) == pytest.approx(0.8)
    assert an.profit_capture(-1.0, 4.0, 5.0) is None  # not a winner (net <= 0)
    assert an.profit_capture(0.0, 4.0, 5.0) is None
    assert an.profit_capture(100.0, 4.0, 0.0) is None  # MFE zero
    assert an.profit_capture(100.0, 4.0, None) is None  # MFE unavailable
    assert an.profit_capture(None, 4.0, 5.0) is None  # no net: winner unknown


def test_aggregates_average_median_counts_and_coverage():
    def row(status, net, mae=None, mfe=None, capture=None, reason=None):
        return {
            "status": status,
            "reason": reason,
            "net_pnl": net,
            "mae_points": mae,
            "mfe_points": mfe,
            "mae_amount": None if mae is None else mae * 10,
            "mfe_amount": None if mfe is None else mfe * 10,
            "profit_capture": capture,
        }

    rows = [
        row("available", 50.0, 1.0, 5.0, 0.8),
        row("available", 20.0, 3.0, 4.0, 0.5),
        row("available", 10.0, 2.0, 0.0, None),  # winner with MFE 0: no capture
        row("available", -30.0, 6.0, 1.0),
        row("available", -10.0, 2.0, 2.0),
        row("available", 0.0, 9.0, 9.0),  # break-even: coverage only
        row("unavailable", 5.0, reason="missing_bars"),
        row("unavailable", -5.0, reason="unsupported_segment"),
        row("unavailable", 5.0, reason="missing_bars"),
    ]
    agg = an.excursion_aggregates(rows)
    w, lo = agg["winners"], agg["losers"]
    assert w["mae_points"] == {"count": 3, "average": 2.0, "median": 2.0}
    assert w["mfe_points"]["average"] == pytest.approx(3.0) and w["mfe_points"]["median"] == 4.0
    assert w["profit_capture"] == {"count": 2, "average": 0.65, "median": 0.65}
    assert lo["mae_points"] == {"count": 2, "average": 4.0, "median": 4.0}
    assert lo["mfe_amount"]["average"] == pytest.approx(15.0) and "profit_capture" not in lo
    cov = agg["coverage"]
    assert (cov["available"], cov["total"]) == (6, 9)
    assert cov["label"] == "6 of 9 positions available"
    assert cov["unavailable_reasons"] == {"missing_bars": 2, "unsupported_segment": 1}
    assert agg["minute_bar_precision"] is True
    empty = an.excursion_aggregates([])
    assert empty["coverage"]["label"] == "0 of 0 positions available"
    assert empty["winners"]["mae_points"] == {"count": 0, "average": None, "median": None}
    mid = statistics.median([1.0, 3.0, 2.0])
    assert mid == 2.0


# ---------------------------------------------------------------- service: lazy read through the DB


def window_ms(day, start=(9, 15), end=(15, 30)):
    def ms(hh, mm):
        naive = datetime(2026, 11, day, hh, mm) - datetime(1970, 1, 1)
        return int(naive.total_seconds() * 1000) - 19_800_000

    return {"start_ms": ms(*start), "end_ms": ms(*end), "is_special": False}


@pytest.fixture(autouse=True)
def _fresh_excursion_cache():
    """Past-day results are cached for hours; every test starts with an empty cache."""
    pnl._excursion_cache.clear()
    yield
    pnl._excursion_cache.clear()


@pytest.fixture
def nse_open(monkeypatch):
    """The market calendar answers NSE weekdays 09:15-15:30 (holiday check: closed on
    weekends only)."""

    def info(day, exchange):
        if exchange != "NSE" or day.weekday() >= 5:
            return None
        return window_ms(day.day)

    monkeypatch.setattr(pnl, "_calendar_window_info", info)
    monkeypatch.setattr(pnl, "_confirmed_closed", lambda day, ex: day.weekday() >= 5)


class Reader:
    """A stand-in for the Historify read: records calls, serves flat bars with overrides."""

    def __init__(self, high=101.0, low=99.0, overrides=None, drop=()):
        self.calls = []
        self.high, self.low = high, low
        self.overrides = overrides or {}
        self.drop = set(drop)

    def __call__(self, symbol, exchange, start_ts, end_ts):
        self.calls.append((symbol, exchange, start_ts, end_ts))
        rows = []
        for ts in range(start_ts, end_ts + 1, 60):
            minute = datetime.fromtimestamp(ts, tz=IST).strftime("%H:%M")
            if minute in self.drop:
                continue
            high, low = self.overrides.get(minute, (self.high, self.low))
            rows.append({"timestamp": ts, "high": high, "low": low})
        return pd.DataFrame(rows, columns=["timestamp", "high", "low"])


def epoch(dt):
    return int(dt.replace(tzinfo=IST).timestamp())


def long_win(env, day=16, symbol="SBIN"):
    env.fill("BUY", 10, 100, datetime(2026, 11, day, 9, 30, 20), symbol=symbol)
    env.fill("SELL", 10, 104, datetime(2026, 11, day, 9, 35, 10), symbol=symbol)


def excursions(reader, **kw):
    kw.setdefault("period", "all")
    return pnl.get_excursions(reader=reader, **kw)


def test_service_available_result_aggregates_note_and_exact_window(env, nse_open):
    long_win(env)
    reader = Reader(overrides={"09:32": (103, 98), "09:33": (105, 100)})
    body = excursions(reader)
    (row,) = body["positions"]
    assert row["status"] == "available" and row["reason"] is None
    assert (row["mae_points"], row["mfe_points"]) == (2.0, 5.0)
    assert (row["mae_amount"], row["mfe_amount"]) == (20.0, 50.0)
    assert row["points_per_unit"] == 4.0 and row["net_pnl"] > 0
    assert row["profit_capture"] == pytest.approx(4.0 / 5.0)
    assert row["minute_bar_precision"] is True and row["category"] == "intraday"
    # the read is the EXACT window: first fill minute .. last fill minute, one read
    assert reader.calls == [
        ("SBIN", "NSE", epoch(datetime(2026, 11, 16, 9, 30)), epoch(datetime(2026, 11, 16, 9, 35)))
    ]
    assert body["aggregates"]["coverage"]["label"] == "1 of 1 positions available"
    assert body["aggregates"]["winners"]["profit_capture"]["count"] == 1
    assert "after market close" in body["note"] and "price data processing" in body["note"]
    assert body["cap"] == pnl.EXCURSION_MAX_POSITIONS and body["computed"] == 1


def test_service_scale_in_and_partial_exit_use_the_real_fills(env, nse_open):
    env.fill("BUY", 10, 100, datetime(2026, 11, 16, 9, 30, 5))
    env.fill("BUY", 10, 110, datetime(2026, 11, 16, 9, 34, 5))
    env.fill("SELL", 10, 108, datetime(2026, 11, 16, 9, 38, 5))
    env.fill("SELL", 10, 106, datetime(2026, 11, 16, 9, 39, 5))
    reader = Reader(high=104, low=104, overrides={"09:31": (101, 97), "09:36": (111, 101)})
    (row,) = excursions(reader)["positions"]
    assert row["status"] == "available"
    # fills of the position in order: ENTRY 10@100, ENTRY 10@110, EXIT 10@108, EXIT 10@106
    ref = ref_excursion(
        "LONG",
        [
            F("ENTRY", 10, 100.0, datetime(2026, 11, 16, 9, 30, 5)),
            F("ENTRY", 10, 110.0, datetime(2026, 11, 16, 9, 34, 5)),
            F("EXIT", 10, 108.0, datetime(2026, 11, 16, 9, 38, 5)),
            F("EXIT", 10, 106.0, datetime(2026, 11, 16, 9, 39, 5)),
        ],
        bars_for(
            datetime(2026, 11, 16, 9, 30),
            datetime(2026, 11, 16, 9, 39),
            104,
            104,
            **{"09:31": (101, 97), "09:36": (111, 101)},
        ),
    )
    assert (row["mae_points"], row["mfe_points"]) == (pytest.approx(ref[0]), pytest.approx(ref[1]))
    assert (row["mae_amount"], row["mfe_amount"]) == (pytest.approx(ref[2]), pytest.approx(ref[3]))


def test_service_short_position(env, nse_open):
    env.fill("SELL", 10, 100, datetime(2026, 11, 16, 9, 30, 20))
    env.fill("BUY", 10, 97, datetime(2026, 11, 16, 9, 36, 0))
    (row,) = excursions(Reader(overrides={"09:32": (103, 99), "09:33": (101, 95)}))["positions"]
    assert (row["mae_points"], row["mfe_points"]) == (3.0, 5.0)
    assert row["direction"] == "SHORT" and row["points_per_unit"] == 3.0


@pytest.mark.parametrize(
    ("symbol", "exchange", "product"),
    [("NIFTY26NOV26FUT", "NFO", "NRML"), ("SENSEX", "BSE", "MIS"), ("CRUDE", "MCX", "NRML")],
)
def test_service_unsupported_segments_are_unavailable_without_a_read(
    env, nse_open, symbol, exchange, product
):
    env.fill(
        "BUY",
        10,
        100,
        datetime(2026, 11, 16, 9, 30, 20),
        symbol=symbol,
        exchange=exchange,
        product=product,
    )
    env.fill(
        "SELL",
        10,
        104,
        datetime(2026, 11, 16, 9, 35, 10),
        symbol=symbol,
        exchange=exchange,
        product=product,
    )
    reader = Reader()
    body = excursions(reader)
    (row,) = body["positions"]
    assert (row["status"], row["reason"], row["reason_text"]) == (
        "unavailable",
        "unsupported_segment",
        "unsupported segment",
    )
    assert row["mae_points"] is None and row["mfe_points"] is None and row["profit_capture"] is None
    assert reader.calls == []
    assert body["aggregates"]["coverage"]["label"] == "0 of 1 positions available"


def test_service_missing_bars_are_unavailable_not_estimated(env, nse_open):
    long_win(env)
    (row,) = excursions(Reader(drop={"09:33"}))["positions"]
    assert row["status"] == "unavailable" and row["reason"] == "missing_bars"
    assert row["mae_points"] is None and row["mfe_points"] is None


def test_service_closed_today_before_the_session_ends_is_after_market_close(env, nse_open):
    # env.now is Tue 17 11:00, the market is open: nothing is computed, nothing is read
    long_win(env, day=17)
    reader = Reader()
    (row,) = excursions(reader)["positions"]
    assert row["status"] == "unavailable" and row["reason"] == "after_market_close"
    assert reader.calls == []


def test_service_closed_today_after_the_close_waits_for_complete_data(env, nse_open):
    long_win(env, day=17)
    env.now = datetime(2026, 11, 17, 16, 0)
    (row,) = excursions(Reader())["positions"]
    assert row["status"] == "available"
    (row,) = excursions(Reader(drop={"09:34"}))["positions"]
    assert row["status"] == "unavailable" and row["reason"] == "historify_incomplete"


def test_service_calendar_down_is_unavailable_not_guessed(env):
    long_win(env)  # the autouse fixture keeps the calendar down
    reader = Reader()
    (row,) = excursions(reader)["positions"]
    assert row["status"] == "unavailable" and row["reason"] == "session_calendar_unavailable"
    assert reader.calls == []


def test_service_reader_failure_is_reported(env, nse_open):
    long_win(env)

    def boom(*args):
        raise RuntimeError("duckdb busy")

    (row,) = excursions(boom)["positions"]
    assert row["status"] == "unavailable" and row["reason"] == "bar_read_failed"


def test_service_trade_cap_marks_the_rest_not_computed(env, nse_open, monkeypatch):
    monkeypatch.setattr(pnl, "EXCURSION_MAX_POSITIONS", 2)
    for day in (10, 11, 12):
        long_win(env, day=day)
    reader = Reader()
    body = excursions(reader)
    assert body["computed"] == 2 and len(reader.calls) == 2 and body["positions_total"] == 3
    by_day = {int(r["closed_at"][8:10]): r for r in body["positions"]}
    assert by_day[12]["status"] == by_day[11]["status"] == "available"  # the newest are read
    assert by_day[10]["reason"] == "not_computed_cap" and by_day[10]["mae_points"] is None
    assert body["aggregates"]["coverage"]["label"] == "2 of 3 positions available"


def test_service_past_day_result_is_cached_unavailable_is_not(env, nse_open):
    long_win(env)
    pnl._excursion_cache.clear()
    first = Reader()
    excursions(first)
    second = Reader()
    excursions(second)
    assert len(first.calls) == 1 and second.calls == []  # final past-day bars are cached
    pnl._excursion_cache.clear()
    gappy = Reader(drop={"09:33"})
    excursions(gappy)
    again = Reader()
    (row,) = excursions(again)["positions"]
    assert len(again.calls) == 1 and row["status"] == "available"  # a gap is re-read later


def test_service_category_filter_and_unresolved_fills(env, nse_open):
    long_win(env, day=10)  # intraday
    env.fill("BUY", 10, 100, datetime(2026, 11, 11, 14, 0), symbol="TCS", product="CNC")
    env.fill("SELL", 10, 104, datetime(2026, 11, 12, 10, 0), symbol="TCS", product="CNC")  # swing
    env.fill("BUY", 3, 50, datetime(2026, 11, 12, 10, 5), symbol="UNRES", state="unresolved")
    reader = Reader()
    both = excursions(reader)
    assert {r["symbol"] for r in both["positions"]} == {"SBIN", "TCS"}  # unresolved never in
    only_intraday = excursions(Reader(), category="intraday")
    assert [r["symbol"] for r in only_intraday["positions"]] == ["SBIN"]
    only_swing = excursions(Reader(), category="swing")
    assert [r["symbol"] for r in only_swing["positions"]] == ["TCS"]
    with pytest.raises(ValueError):
        excursions(Reader(), category="positional")


def test_excursions_are_not_in_the_summary(env, nse_open):
    long_win(env)
    out = pnl.get_performance(period="all")
    assert "excursions" not in out and "aggregates" not in out
    assert all("mae_points" not in row for row in out["trades"])


def test_service_reversal_fill_contributes_only_its_closing_part(env, nse_open):
    # SELL 15 closes the 10 long and opens a 5 short: position 1 sees EXIT 10, position 2
    # ENTRY 5 (the same fill, split). Reconstruction partial fills feed the quantity path.
    env.fill("BUY", 10, 100, datetime(2026, 11, 16, 9, 30, 5))
    env.fill("SELL", 15, 104, datetime(2026, 11, 16, 9, 40, 5))
    env.fill("BUY", 5, 102, datetime(2026, 11, 16, 9, 50, 5))
    rows = {r["direction"]: r for r in excursions(Reader())["positions"]}
    assert set(rows) == {"LONG", "SHORT"}
    long_row, short_row = rows["LONG"], rows["SHORT"]
    assert long_row["status"] == short_row["status"] == "available"
    assert long_row["closed_quantity"] == 10 and short_row["closed_quantity"] == 5
    # long: fill prices 100 -> 104 (favourable 4 x 10 units at the exit), bars 101 / 99
    assert long_row["mfe_points"] == 4.0 and long_row["mfe_amount"] == 40.0
    assert long_row["mae_points"] == 1.0 and long_row["mae_amount"] == 10.0
    # short from 104: bars 101 / 99 are 5 favourable, 3 adverse... high 101 < 104 so no adverse
    assert short_row["mfe_points"] == 5.0 and short_row["mfe_amount"] == 25.0
    assert short_row["mae_points"] == 0.0
