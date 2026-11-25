"""Capture, giveback and per-instrument statistics of the Discretionary page.

The real-candle cases come from test/fixtures/discretionary_capture_tcs_2026_11_23.json. Its
expected values were produced by an independent per-minute script (see the fixture comment), not
by the helpers under test, and the arithmetic in this file is written from the definitions:

* realised points = exit-quantity weighted mean of sign * (exit price - average entry in force
  at that exit), gross, charges never deducted;
* capture = realised points / MFE points; giveback = MFE points - realised points;
* a position enters capture only as a net winner (net after charges > 0) with an available
  excursion, at least ``CAPTURE_MIN_BARS_BETWEEN_FILLS`` full-candle bars between its first and
  last fill minutes, and MFE > 0.
"""
# ruff: noqa: F811  (fixtures are imported from the sibling excursion tests)

import json
import random
from datetime import datetime
from pathlib import Path

import pytest
from test_discretionary_excursions import (  # noqa: F401
    F,
    Reader,
    _fresh_excursion_cache,
    at,
    excursions,
    nse_calendar,
    nse_open,
    random_path,
    run,
)
from test_discretionary_pnl_service import _clock_only, env  # noqa: F401

from services import discretionary_analytics as an
from services import discretionary_pnl_service as pnl

FIXTURE = Path(__file__).parent / "fixtures" / "discretionary_capture_tcs_2026_11_23.json"


def _load():
    data = json.loads(FIXTURE.read_text())
    out = {}
    for p in data["positions"]:
        fills = [
            {
                "time": datetime.fromisoformat(f["time"]),
                "role": f["role"],
                "qty": f["qty"],
                "price": f["price"],
            }
            for f in p["fills"]
        ]
        bars = {
            datetime.fromisoformat(f"{c['date']} {c['minute']}"): (c["high"], c["low"])
            for c in p["candles"]
        }
        out[p["id"]] = (p, fills, bars)
    return out


REAL = _load()


# ---------------------------------------------------------------- real candles


@pytest.mark.parametrize("pid", ["P7", "P8", "P11", "P12"])
def test_real_candles_match_the_independent_recompute(pid):
    p, fills, bars = REAL[pid]
    exp = p["expected"]
    got = run(p["direction"], fills, bars)
    assert got["status"] == "available"
    assert (got["mae_points"], got["mfe_points"]) == (exp["mae_points"], exp["mfe_points"])
    assert (got["mae_amount"], got["mfe_amount"]) == (exp["mae_amount"], exp["mfe_amount"])
    assert (got["bars_used"], got["bars_expected"]) == (exp["bars_used"], exp["bars_expected"])
    realized = an.position_realized_points(p["direction"], fills)
    assert realized == pytest.approx(exp["realized_points"], abs=1e-4)


def test_real_net_winners_have_capture_and_giveback_from_the_definitions():
    for pid in ("P7", "P12"):
        p, fills, bars = REAL[pid]
        exp = p["expected"]
        cap = an.position_capture(
            p["net_pnl_book"],
            an.position_realized_points(p["direction"], fills),
            run(p["direction"], fills, bars),
        )
        assert cap["reason"] is None
        assert cap["capture"] == pytest.approx(exp["realized_points"] / exp["mfe_points"], abs=1e-4)
        assert cap["giveback_points"] == pytest.approx(
            exp["mfe_points"] - exp["realized_points"], abs=1e-4
        )
        assert cap["realized_points"] <= exp["mfe_points"]  # never above the MFE
    # a few figures a reader can check by hand
    assert REAL["P7"][0]["expected"]["mfe_points"] == 36.0444
    assert REAL["P12"][0]["expected"]["realized_points"] == 26.8932


def test_real_loser_has_no_capture_and_keeps_mae_mfe():
    p, fills, bars = REAL["P8"]
    exc = run(p["direction"], fills, bars)
    cap = an.position_capture(
        p["net_pnl_book"], an.position_realized_points(p["direction"], fills), exc
    )
    assert p["net_pnl_book"] < 0
    assert cap["capture"] is None and cap["giveback_points"] is None
    assert cap["reason"] == "not_net_winner"
    assert cap["realized_points"] == pytest.approx(-3.9114, abs=1e-4)  # gross points kept
    assert (exc["mae_points"], exc["mfe_points"]) == (3.9114, 8.7857)


def test_real_position_without_a_candle_between_fills_is_left_out_of_capture():
    p, fills, bars = REAL["P11"]
    exc = run(p["direction"], fills, bars)
    # MAE / MFE are shown as before (fill prices only), the position has no interior bar
    assert (exc["mae_points"], exc["mfe_points"], exc["bars_used"]) == (0.0, 0.7, 0)
    realized = an.position_realized_points(p["direction"], fills)
    assert realized == pytest.approx(0.7, abs=1e-4) and p["net_pnl_book"] < 0
    # stored net is a loss after charges, so it is not a winner ...
    assert an.position_capture(p["net_pnl_book"], realized, exc)["reason"] == "not_net_winner"
    # ... and even as a net winner the coverage rule stops it
    cap = an.position_capture(5.0, realized, exc)
    assert cap["reason"] == "insufficient_candle_coverage" and cap["capture"] is None
    assert an.CAPTURE_MIN_BARS_BETWEEN_FILLS == 1


# ---------------------------------------------------------------- hand-computed paths


def _flat(first, last, high, low, **override):
    out = {}
    m = first
    from datetime import timedelta

    while m <= last:
        out[m] = override.get(m.strftime("%H:%M"), (high, low))
        m += timedelta(minutes=1)
    return out


def test_exit_time_basis_differs_from_the_final_average_entry():
    # LONG: 10@100, exit 5@104, add 10@110, exit 15@108.
    fills = [
        F("ENTRY", 10, 100.0, at(9, 30, 20)),
        F("EXIT", 5, 104.0, at(9, 33, 10)),
        F("ENTRY", 10, 110.0, at(9, 35, 10)),
        F("EXIT", 15, 108.0, at(9, 38, 10)),
    ]
    # exit 1 against the average then in force (100): +4.0 x 5 = 20
    # exit 2 against 5@100 + 10@110 -> (500 + 1100) / 15 = 106.6667: +1.3333 x 15 = 20
    # realised = 40 / 20 = 2.0 per unit; the final whole-position average (105) would give the
    # first exit -1.0, which is not what that exit earned.
    assert an.position_realized_points("LONG", fills) == pytest.approx(2.0)
    bars = _flat(at(9, 30), at(9, 38), 105.0, 103.0)
    exc = run("LONG", fills, bars)
    # MFE 10 = the 110 add observed against the average in force before it (100)
    # MAE 3.6667 = 106.6667 - 103 from the minutes after the add
    assert (exc["mfe_points"], exc["mae_points"]) == (10.0, 3.6667)
    cap = an.position_capture(50.0, an.position_realized_points("LONG", fills), exc)
    assert cap["reason"] is None
    assert cap["capture"] == pytest.approx(0.2) and cap["giveback_points"] == pytest.approx(8.0)


def test_short_path_capture():
    fills = [F("ENTRY", 10, 100.0, at(9, 30, 20)), F("EXIT", 10, 97.0, at(9, 36, 10))]
    bars = _flat(at(9, 30), at(9, 36), 101.0, 99.0, **{"09:33": (100.5, 95.0)})
    exc = run("SHORT", fills, bars)
    assert (exc["mfe_points"], exc["mae_points"], exc["bars_used"]) == (5.0, 1.0, 5)
    realized = an.position_realized_points("SHORT", fills)
    assert realized == pytest.approx(3.0)
    cap = an.position_capture(10.0, realized, exc)
    assert cap["capture"] == pytest.approx(0.6) and cap["giveback_points"] == pytest.approx(2.0)


def test_fill_minute_candle_is_not_used_and_bars_are_start_stamped():
    fills = [F("ENTRY", 10, 100.0, at(9, 30, 20)), F("EXIT", 10, 101.0, at(9, 35, 10))]
    # the 09:30 and 09:35 candles hold wild extremes; they must not count
    bars = _flat(
        at(9, 30), at(9, 35), 101.0, 99.5, **{"09:30": (500.0, 1.0), "09:35": (500.0, 1.0)}
    )
    exc = run("LONG", fills, bars)
    assert (exc["mfe_points"], exc["mae_points"], exc["bars_used"]) == (1.0, 0.5, 4)
    # a bar keyed 09:36 (the END of the 09:35 candle) is not needed and not used
    assert exc["bars_expected"] == 6
    assert run("LONG", fills, {**bars, at(9, 36): (900.0, 1.0)})["mfe_points"] == 1.0


def test_missing_candle_makes_the_excursion_unavailable_and_capture_excluded():
    fills = [F("ENTRY", 10, 100.0, at(9, 30, 20)), F("EXIT", 10, 104.0, at(9, 35, 10))]
    bars = _flat(at(9, 30), at(9, 35), 101.0, 99.0)
    del bars[at(9, 33)]
    exc = run("LONG", fills, bars)
    assert exc["status"] == "unavailable" and exc["mfe_points"] is None
    cap = an.position_capture(80.0, an.position_realized_points("LONG", fills), exc)
    assert cap["reason"] == "excursion_unavailable" and cap["capture"] is None
    assert cap["realized_points"] == pytest.approx(4.0)  # known from fills, still gross


def test_zero_mfe_and_losers_have_no_capture():
    fills = [F("ENTRY", 10, 100.0, at(9, 30, 20)), F("EXIT", 10, 100.0, at(9, 35, 10))]
    exc = run("LONG", fills, _flat(at(9, 30), at(9, 35), 100.0, 99.0))
    assert exc["mfe_points"] == 0.0 and exc["bars_used"] == 4
    cap = an.position_capture(5.0, 0.0, exc)
    assert cap["reason"] == "mfe_not_positive" and cap["capture"] is None
    assert an.position_capture(-5.0, 2.0, exc)["reason"] == "not_net_winner"
    assert an.position_capture(0.0, 2.0, exc)["reason"] == "not_net_winner"


def test_net_after_charges_decides_the_winner_and_points_stay_gross():
    fills = [F("ENTRY", 10, 100.0, at(9, 30, 20)), F("EXIT", 10, 100.5, at(9, 35, 10))]
    exc = run("LONG", fills, _flat(at(9, 30), at(9, 35), 101.0, 99.0))
    realized = an.position_realized_points("LONG", fills)
    assert realized == pytest.approx(0.5)  # gross price movement, positive
    gone = an.position_capture(-1.2, realized, exc)  # gross +5.0 rupees, charges above it
    assert gone["reason"] == "not_net_winner" and gone["realized_points"] == pytest.approx(0.5)
    kept = an.position_capture(3.8, realized, exc)  # same points, net still positive
    assert kept["reason"] is None and kept["realized_points"] == pytest.approx(0.5)
    assert kept["capture"] == pytest.approx(0.5 / 1.0)  # points untouched by charges
    assert an.position_capture(None, realized, exc)["reason"] == "net_unavailable"


def test_capture_above_100_percent_is_never_clipped_only_excluded():
    exc = {"status": "available", "mfe_points": 3.0, "bars_used": 5}
    cap = an.position_capture(50.0, 5.0, exc)  # an exit outside the recorded path
    assert cap["reason"] == "data_inconsistency"
    assert cap["capture"] is None and cap["giveback_points"] is None
    assert cap["realized_points"] == 5.0  # reported as it is, not clipped
    # exactly at the MFE is allowed: 100%, not an inconsistency
    assert an.position_capture(50.0, 3.0, exc)["capture"] == pytest.approx(1.0)


@pytest.mark.parametrize("seed", range(60))
def test_realised_points_never_exceed_mfe_on_random_paths(seed):
    """On the in-force basis every exit price is itself observed against the average in force at
    that exit, so each exit's favourable move is <= MFE and so is their weighted mean."""
    from datetime import timedelta

    rng = random.Random(seed)
    direction, fills = random_path(rng)
    bars = {}
    m = fills[0]["time"].replace(second=0)
    while m <= fills[-1]["time"]:
        low = round(rng.uniform(80, 105), 2)
        bars[m] = (round(low + rng.uniform(0, 12), 2), low)
        m += timedelta(minutes=1)
    exc = run(direction, fills, bars)
    realized = an.position_realized_points(direction, fills)
    assert realized is not None and exc["status"] == "available"
    assert round(realized, 4) <= exc["mfe_points"]
    cap = an.position_capture(1.0, realized, exc)
    assert cap["reason"] != "data_inconsistency"
    if cap["capture"] is not None:
        assert cap["capture"] <= 1.0 + 1e-12
        assert cap["giveback_points"] >= 0.0


# ---------------------------------------------------------------- per-instrument statistics


def _row(symbol, exchange="NSE", **kw):
    base = {
        "symbol": symbol,
        "exchange": exchange,
        "status": "available",
        "net_pnl": 10.0,
        "mae_points": 1.0,
        "mfe_points": 4.0,
        "mae_amount": 10.0,
        "mfe_amount": 40.0,
        "bars_used": 3,
        "profit_capture": 0.5,
        "giveback_points": 2.0,
        "capture_reason": None,
    }
    base.update(kw)
    return base


def test_two_instruments_are_never_pooled():
    rows = [
        _row("TCS", profit_capture=0.9, giveback_points=1.0),
        _row("TCS", profit_capture=0.7, giveback_points=3.0),
        _row("INFY", profit_capture=0.1, giveback_points=9.0, mae_points=3.0, mfe_points=5.0),
    ]
    agg = an.excursion_aggregates(rows)
    assert agg["instrument_count"] == 2
    assert agg["winners"]["mae_points"] is None and agg["winners"]["mfe_points"] is None
    assert agg["losers"]["mae_points"] is None and agg["winners"]["profit_capture"] is None
    assert agg["winners"]["mae_amount"]["count"] == 3  # rupees stay on the scope
    by = {i["symbol"]: i for i in agg["instruments"]}
    assert by["TCS"]["capture"]["median_capture"] == pytest.approx(0.8)
    assert by["TCS"]["capture"]["median_giveback_points"] == pytest.approx(2.0)
    assert by["INFY"]["capture"]["median_capture"] == pytest.approx(0.1)
    assert by["TCS"]["winners"]["mae_points"] == {"count": 2, "average": 1.0, "median": 1.0}
    assert by["INFY"]["winners"]["mae_points"]["average"] == 3.0
    # the same symbol on another exchange is another instrument
    both = an.excursion_aggregates([_row("TCS"), _row("TCS", exchange="BSE")])
    assert both["instrument_count"] == 2


def test_single_instrument_keeps_the_pooled_points_with_its_label():
    agg = an.excursion_aggregates([_row("TCS"), _row("TCS", mae_points=3.0)])
    assert agg["instrument_count"] == 1
    assert agg["winners"]["mae_points"] == {"count": 2, "average": 2.0, "median": 1.0 + 1.0}
    assert (agg["instruments"][0]["symbol"], agg["instruments"][0]["exchange"]) == ("TCS", "NSE")


def test_coverage_counts_reconcile_and_reasons_are_counted():
    rows = [
        _row("TCS"),
        _row(
            "TCS",
            net_pnl=-5.0,
            profit_capture=None,
            giveback_points=None,
            capture_reason="not_net_winner",
        ),
        _row(
            "TCS",
            profit_capture=None,
            giveback_points=None,
            bars_used=0,
            capture_reason="insufficient_candle_coverage",
        ),
        _row(
            "TCS",
            profit_capture=None,
            giveback_points=None,
            mfe_points=0.0,
            capture_reason="mfe_not_positive",
        ),
        _row(
            "TCS",
            status="unavailable",
            reason="missing_bars",
            mae_points=None,
            mfe_points=None,
            profit_capture=None,
            giveback_points=None,
            capture_reason="excursion_unavailable",
        ),
        _row("TCS", profit_capture=None, giveback_points=None, capture_reason="data_inconsistency"),
    ]
    cap = an.excursion_aggregates(rows)["instruments"][0]["capture"]
    assert (cap["positions"], cap["captured"], cap["excluded_total"]) == (6, 1, 5)
    assert cap["captured"] + sum(cap["excluded"].values()) == cap["positions"]
    assert cap["excluded"] == {
        "data_inconsistency": 1,
        "excursion_unavailable": 1,
        "insufficient_candle_coverage": 1,
        "mfe_not_positive": 1,
        "not_net_winner": 1,
    }


def test_mae_over_half_denominator_is_reliable_positions_with_positive_mfe():
    rows = [
        _row("TCS", mae_points=2.5, mfe_points=4.0),  # 2.5 > 2.0: counted
        _row("TCS", mae_points=2.0, mfe_points=4.0),  # exactly half: not above
        _row("TCS", mae_points=1.0, mfe_points=4.0, net_pnl=-3.0),  # losers are in the base
        _row("TCS", mae_points=9.0, mfe_points=0.0),  # MFE 0: outside the base
        _row("TCS", mae_points=9.0, mfe_points=4.0, bars_used=0),  # no coverage: outside
        _row("TCS", status="unavailable", reason="missing_bars", mae_points=None, mfe_points=None),
    ]
    half = an.excursion_aggregates(rows)["instruments"][0]["capture"]["mae_over_half"]
    assert half == {"count": 1, "denominator": 3, "share": pytest.approx(1 / 3, abs=1e-4)}
    empty = an.instrument_capture_stats([])["mae_over_half"]
    assert empty == {"count": 0, "denominator": 0, "share": None}


# ---------------------------------------------------------------- service, API and CSV


def _pair(env, symbol, day, h, m, qty, entry, exit_, minutes=5):
    env.fill("BUY", qty, entry, datetime(2026, 11, day, h, m, 20), symbol=symbol)
    env.fill("SELL", qty, exit_, datetime(2026, 11, day, h, m + minutes, 10), symbol=symbol)


def test_service_row_api_and_csv_share_one_definition(env, nse_open):
    _pair(env, "TCS", 16, 9, 30, 100, 100, 104)  # clear net winner, 4 interior bars
    _pair(env, "INFY", 16, 10, 30, 100, 100, 104)
    _pair(env, "TCS", 13, 9, 30, 10, 100, 100.02)  # gross positive, net after charges not
    _pair(env, "INFY", 13, 9, 30, 100, 100, 104, minutes=1)  # adjacent minutes: no interior bar
    reader = Reader(overrides={"09:33": (105, 100)})
    body = excursions(reader)
    rows = {(r["symbol"], r["opened_at"][:10]): r for r in body["positions"]}
    win = rows[("TCS", "2026-11-16")]
    assert win["status"] == "available" and win["bars_used"] == 4 and win["bars_expected"] == 6
    assert win["realized_points"] == 4.0 and win["mfe_points"] == 5.0
    assert win["profit_capture"] == pytest.approx(0.8) and win["giveback_points"] == 1.0
    assert win["capture_reason"] is None
    charged = rows[("TCS", "2026-11-13")]
    assert charged["points_per_unit"] > 0 and charged["net_pnl"] <= 0
    assert charged["profit_capture"] is None and charged["capture_reason"] == "not_net_winner"
    assert charged["realized_points"] == pytest.approx(0.02, abs=1e-4)  # points stay gross
    adjacent = rows[("INFY", "2026-11-13")]
    assert adjacent["net_pnl"] > 0 and adjacent["bars_used"] == 0
    assert adjacent["mfe_points"] is not None  # MAE / MFE still shown
    assert adjacent["profit_capture"] is None
    assert adjacent["capture_reason"] == "insufficient_candle_coverage"
    # two instruments in scope: no pooled points, per-instrument statistics reconcile
    agg = body["aggregates"]
    assert agg["instrument_count"] == 2 and agg["winners"]["mae_points"] is None
    for inst in agg["instruments"]:
        c = inst["capture"]
        assert c["captured"] + c["excluded_total"] == c["positions"] == 2
    tcs = next(i for i in agg["instruments"] if i["symbol"] == "TCS")["capture"]
    assert tcs["captured"] == 1 and tcs["excluded"] == {"not_net_winner": 1}
    # CSV rows carry the same values and the same eligibility
    export = pnl.get_export(period="all", reader=reader)
    assert export["columns"][-5:] == [
        "realized_points",
        "giveback_points",
        "capture_reason",
        "bars_used",
        "bars_expected",
    ]
    assert export["columns"].index("profit_capture") < export["columns"].index("excursion_status")
    for csv in export["rows"]:
        api = rows[(csv["symbol"], csv["opened_at"][:10])]
        for key in (
            "profit_capture",
            "realized_points",
            "giveback_points",
            "capture_reason",
            "bars_used",
            "bars_expected",
            "mae_points",
            "mfe_points",
        ):
            assert csv[key] == api[key], key


def test_service_unsupported_segment_has_no_capture_and_no_zero(env, nse_open):
    env.fill(
        "BUY", 10, 100, at(9, 30, 20), symbol="NIFTY26NOV26FUT", exchange="NFO", product="NRML"
    )
    env.fill(
        "SELL", 10, 104, at(9, 35, 10), symbol="NIFTY26NOV26FUT", exchange="NFO", product="NRML"
    )
    (row,) = excursions(Reader())["positions"]
    assert row["status"] == "unavailable"
    for key in ("realized_points", "profit_capture", "giveback_points", "bars_used"):
        assert row[key] is None, key
    assert row["capture_reason"] in ("excursion_unavailable", "net_unavailable")
