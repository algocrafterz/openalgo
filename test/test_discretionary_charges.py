"""Discretionary charges: code-verified numbers against hand arithmetic, per-order allocation.

Pure functions only (no DB, no broker). Run with:
    uv run pytest test/test_discretionary_charges.py -q
"""

from datetime import datetime, timedelta

import pytest

from portfolio.costs import india_delivery, india_intraday
from services import discretionary_charges as ch
from services import discretionary_reconstruction as rec

T0 = datetime(2026, 11, 17, 10, 0, 0)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def mk(
    fid,
    side,
    qty,
    price,
    when,
    *,
    product="MIS",
    symbol="SBIN",
    exchange="NSE",
    broker="default",
    account="A",
    order_id=None,
):
    return {
        "id": fid,
        "broker": broker,
        "account": account,
        "order_id": order_id if order_id is not None else f"O{fid}",
        "fill_id": None,
        "symbol": symbol,
        "exchange": exchange,
        "product": product,
        "side": side,
        "qty": qty,
        "price": price,
        "fill_time": when,
    }


def run(fills):
    out = rec.reconstruct(fills)
    ch.allocate_charges(out, {f["id"]: f for f in fills})
    return out


# Hand arithmetic from the brief: NSE MIS buy 100@1000 (value 100,000), sell 100@1010 (101,000)
HAND = {
    "brokerage": 40.0,  # 20 + 20 (0.03% = 30 and 30.3, both capped at 20 per order)
    "stt": 25.25,  # 0.025% of 101,000 (sell only)
    "exchange_txn": 6.1707,  # 0.00307% of 201,000
    "sebi": 0.201,  # Rs 10 / crore of 201,000
    "stamp_duty": 3.0,  # 0.003% of 100,000 (buy only)
    "tax": 8.3469,  # 18% of (40 + 6.1707 + 0.201)
}
HAND_TOTAL = 82.97
HAND_FLATTRADE_TOTAL = 35.77  # brokerage 0: 25.25 + 6.1707 + 0.201 + 3 + 0.18 * 6.3717


def test_code_numbers_match_hand_numbers_default_schedule():
    sched = india_intraday("NSE")
    code = sched.breakdown(100_000, 101_000, orders=2)
    for key, hand in HAND.items():
        assert code[key] == pytest.approx(hand, abs=5e-5), key
    assert round(code["total"], 2) == HAND_TOTAL
    assert code["total"] == pytest.approx(sum(HAND.values()), abs=1e-4)


def test_code_numbers_match_hand_numbers_flattrade():
    sched = india_intraday("NSE", broker="flattrade")
    code = sched.breakdown(100_000, 101_000, orders=2)
    assert code["brokerage"] == 0.0
    assert round(code["total"], 2) == HAND_FLATTRADE_TOTAL
    assert code["tax"] == pytest.approx(0.18 * (6.1707 + 0.201), abs=1e-6)


def test_per_order_charges_equal_the_two_order_roundtrip():
    """breakdown(buy,0,1) + breakdown(0,sell,1) is the same money as breakdown(buy,sell,2)."""
    for broker in (None, "flattrade"):
        sched = india_intraday("NSE", broker=broker)
        one = sched.breakdown(100_000, 0, 1)["total"] + sched.breakdown(0, 101_000, 1)["total"]
        assert one == pytest.approx(sched.breakdown(100_000, 101_000, 2)["total"], abs=1e-9)


def test_trade_charges_default_broker_end_to_end():
    out = run([mk(1, "BUY", 100, 1000, at(0)), mk(2, "SELL", 100, 1010, at(5))])
    (t,) = out["trades"]
    assert t["charges_status"] == "ok"
    assert round(t["charges"], 2) == HAND_TOTAL
    assert t["net_pnl"] == pytest.approx(1000 - t["charges"])
    for key, hand in HAND.items():
        assert t["charges_breakdown"][key] == pytest.approx(hand, abs=5e-5), key
    assert out["positions"][0]["entry_charges_carried"] == 0.0  # flat


def test_trade_charges_flattrade_uses_the_brokers_own_brokerage():
    out = run(
        [
            mk(1, "BUY", 100, 1000, at(0), broker="flattrade"),
            mk(2, "SELL", 100, 1010, at(5), broker="flattrade"),
        ]
    )
    (t,) = out["trades"]
    assert round(t["charges"], 2) == HAND_FLATTRADE_TOTAL
    assert t["charges_breakdown"]["brokerage"] == 0.0


@pytest.mark.parametrize(
    "exchange,product",
    [
        ("NFO", "NRML"),
        ("NFO", "MIS"),
        ("BFO", "NRML"),
        ("MCX", "MIS"),
        ("CDS", "NRML"),
        ("NSE", "NRML"),
    ],
)
def test_unmodelled_instruments_are_unavailable_never_guessed(exchange, product):
    fills = [
        mk(1, "BUY", 50, 100, at(0), exchange=exchange, product=product),
        mk(2, "SELL", 50, 110, at(5), exchange=exchange, product=product),
    ]
    out = run(fills)
    (t,) = out["trades"]
    assert t["charges"] is None and t["charges_status"] == "unavailable" and t["net_pnl"] is None
    assert t["realized_pnl"] == pytest.approx(500.0)
    assert all(lk["charge_allocated"] is None for lk in out["links"])


def test_bse_uses_the_bse_schedule():
    out = run(
        [
            mk(1, "BUY", 100, 1000, at(0), exchange="BSE"),
            mk(2, "SELL", 100, 1010, at(5), exchange="BSE"),
        ]
    )
    expect = india_intraday("BSE").breakdown(100_000, 101_000, orders=2)["total"]
    assert out["trades"][0]["charges"] == pytest.approx(expect)


def test_entry_charges_not_double_counted_on_partial_exit():
    """Entry order A (100@1000) closed by two exit orders: A is charged once in total."""
    fills = [
        mk(1, "BUY", 100, 1000, at(0), order_id="A"),
        mk(2, "SELL", 40, 1010, at(5), order_id="B"),
        mk(3, "SELL", 60, 1020, at(9), order_id="C"),
    ]
    out = run(fills)
    sched = india_intraday("NSE")
    order_a = ch.order_charge(sched, "BUY", 100_000)["total"]
    order_b = ch.order_charge(sched, "SELL", 40 * 1010)["total"]
    order_c = ch.order_charge(sched, "SELL", 60 * 1020)["total"]
    t1, t2 = out["trades"]
    assert t1["charges"] == pytest.approx(order_b + order_a * 0.4)
    assert t2["charges"] == pytest.approx(order_c + order_a * 0.6)
    assert t1["charges"] + t2["charges"] == pytest.approx(order_a + order_b + order_c)
    # brokerage of the entry order (Rs 20 + GST) appears exactly once across both trades
    entry_brokerage = sum(
        lk["charge_breakdown"]["brokerage"]
        for lk in out["links"]
        if lk["fill_ref"] == 1 and "trade_idx" in lk
    )
    assert entry_brokerage == pytest.approx(20.0)


def test_open_position_carries_the_unclosed_share_of_entry_charges():
    fills = [
        mk(1, "BUY", 100, 1000, at(0), order_id="A"),
        mk(2, "SELL", 40, 1010, at(5), order_id="B"),
    ]
    out = run(fills)
    order_a = ch.order_charge(india_intraday("NSE"), "BUY", 100_000)["total"]
    (pos,) = out["positions"]
    assert pos["status"] == "OPEN" and pos["open_qty"] == 60
    assert pos["entry_charges_carried"] == pytest.approx(order_a * 0.6)
    assert out["trades"][0]["charges"] + pos["entry_charges_carried"] == pytest.approx(
        order_a + ch.order_charge(india_intraday("NSE"), "SELL", 40 * 1010)["total"]
    )


def test_one_order_filled_in_pieces_gets_brokerage_once_with_the_cap_on_the_whole_order():
    """Three fills of one order: brokerage is min(0.03% x total value, 20), not 3 x cap."""
    fills = [
        mk(1, "BUY", 100, 1000, at(0), order_id="A"),
        mk(2, "BUY", 100, 1000, at(1), order_id="A"),
        mk(3, "BUY", 100, 1000, at(2), order_id="A"),
        mk(4, "SELL", 300, 1000, at(9), order_id="B"),
    ]
    out = run(fills)
    (t,) = out["trades"]
    sched = india_intraday("NSE")
    expected = (
        ch.order_charge(sched, "BUY", 300_000)["total"]
        + ch.order_charge(sched, "SELL", 300_000)["total"]
    )
    assert t["charges"] == pytest.approx(expected)
    assert t["charges_breakdown"]["brokerage"] == pytest.approx(40.0)


def test_flip_splits_the_closing_order_between_trade_and_new_position():
    fills = [mk(1, "BUY", 10, 100, at(0), order_id="A"), mk(2, "SELL", 25, 90, at(1), order_id="B")]
    out = run(fills)
    order_b = ch.order_charge(india_intraday("NSE"), "SELL", 25 * 90)["total"]
    shorts = [p for p in out["positions"] if p["status"] == "OPEN"]
    assert shorts[0]["entry_charges_carried"] == pytest.approx(order_b * 15 / 25)
    order_a = ch.order_charge(india_intraday("NSE"), "BUY", 1000)["total"]
    assert out["trades"][0]["charges"] == pytest.approx(order_a + order_b * 10 / 25)


def test_dp_charge_once_per_account_symbol_sell_session():
    fills = [
        mk(1, "BUY", 100, 500, datetime(2026, 11, 16, 11, 0), product="CNC", broker="flattrade"),
        mk(2, "SELL", 40, 510, datetime(2026, 11, 17, 10, 0), product="CNC", broker="flattrade"),
        mk(3, "SELL", 60, 520, datetime(2026, 11, 17, 11, 0), product="CNC", broker="flattrade"),
    ]
    out = run(fills)
    dps = [t["charges_breakdown"].get("dp", 0.0) for t in out["trades"]]
    assert dps == [pytest.approx(20.0 * 1.18), 0.0]
    # a second symbol the same day pays its own DP; the same symbol on another account too
    other = [
        mk(
            4,
            "BUY",
            10,
            100,
            datetime(2026, 11, 16, 11, 0),
            product="CNC",
            symbol="TCS",
            broker="flattrade",
        ),
        mk(
            5,
            "SELL",
            10,
            101,
            datetime(2026, 11, 17, 10, 0),
            product="CNC",
            symbol="TCS",
            broker="flattrade",
        ),
        mk(
            6,
            "BUY",
            10,
            100,
            datetime(2026, 11, 16, 11, 0),
            product="CNC",
            account="B",
            broker="flattrade",
        ),
        mk(
            7,
            "SELL",
            10,
            101,
            datetime(2026, 11, 17, 10, 0),
            product="CNC",
            account="B",
            broker="flattrade",
        ),
    ]
    out = run(fills + other)
    assert sum(1 for t in out["trades"] if t["charges_breakdown"].get("dp")) == 3


def test_cnc_uses_the_delivery_schedule_and_no_dp_for_default_broker():
    out = run(
        [
            mk(1, "BUY", 100, 500, datetime(2026, 11, 16, 11, 0), product="CNC"),
            mk(2, "SELL", 100, 510, datetime(2026, 11, 17, 11, 0), product="CNC"),
        ]
    )
    expect = india_delivery("NSE", broker="default").breakdown(50_000, 51_000, orders=2)["total"]
    assert out["trades"][0]["charges"] == pytest.approx(expect)
    assert "dp" not in out["trades"][0]["charges_breakdown"]


def test_fill_without_order_id_is_its_own_order():
    fills = [
        mk(1, "BUY", 100, 1000, at(0), order_id=""),
        mk(2, "BUY", 100, 1000, at(1), order_id=""),
        mk(3, "SELL", 200, 1000, at(5), order_id=""),
    ]
    out = run(fills)
    sched = india_intraday("NSE")
    expected = (
        2 * ch.order_charge(sched, "BUY", 100_000)["total"]
        + ch.order_charge(sched, "SELL", 200_000)["total"]
    )
    assert out["trades"][0]["charges"] == pytest.approx(expected)
