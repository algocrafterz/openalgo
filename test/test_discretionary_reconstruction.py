"""Pure reconstruction engine: golden cases, determinism, orphans, the P&L identity.

No database, no broker, no clock. Run with:
    uv run pytest test/test_discretionary_reconstruction.py -q
"""

import random
from datetime import datetime, timedelta

import pytest

from services import discretionary_reconstruction as rec


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
    broker="flattrade",
    account="A",
    order_id=None,
    fill_id=None,
):
    return {
        "id": fid,
        "broker": broker,
        "account": account,
        "order_id": order_id if order_id is not None else f"O{fid}",
        "fill_id": fill_id,
        "symbol": symbol,
        "exchange": exchange,
        "product": product,
        "side": side,
        "qty": qty,
        "price": price,
        "fill_time": when,
    }


T0 = datetime(2026, 11, 17, 10, 0, 0)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def naive_reference(fills):
    """Independent average-cost reference (cost-basis formulation, one identity)."""
    qty, cost, realized, trades = 0.0, 0.0, 0.0, []
    for f in sorted(fills, key=rec.sort_key):
        s = f["qty"] if f["side"] == "BUY" else -f["qty"]
        if qty == 0 or (qty > 0) == (s > 0):
            qty += s
            cost += s * f["price"]
            continue
        closing = min(abs(s), abs(qty))
        sign = 1 if qty > 0 else -1
        basis = cost / qty  # signed cost per unit == average entry
        pnl = sign * closing * (f["price"] - basis)
        trades.append(round(pnl, 9))
        realized += pnl
        cost -= sign * closing * basis
        qty -= sign * closing
        left = abs(s) - closing
        if left > 1e-9:
            qty = -sign * left
            cost = qty * f["price"]
        if abs(qty) < 1e-9:
            qty, cost = 0.0, 0.0
    return realized, trades, qty, (cost / qty if qty else 0.0)


# ---------------------------------------------------------------- golden cases


def test_g1_two_buys_one_sell():
    out = rec.reconstruct(
        [mk(1, "BUY", 10, 100, at(0)), mk(2, "BUY", 10, 110, at(1)), mk(3, "SELL", 20, 120, at(2))]
    )
    assert len(out["trades"]) == 1
    t = out["trades"][0]
    assert t["entry_price"] == pytest.approx(105.0)
    assert t["exit_price"] == 120
    assert t["closed_quantity"] == 20
    assert t["realized_pnl"] == pytest.approx(300.0)
    assert t["direction"] == "LONG"
    assert out["positions"][0]["status"] == "FLAT"
    ref, ref_trades, _, _ = naive_reference(
        [mk(1, "BUY", 10, 100, at(0)), mk(2, "BUY", 10, 110, at(1)), mk(3, "SELL", 20, 120, at(2))]
    )
    assert ref == pytest.approx(300.0) and ref_trades == [300.0]


def test_g2_partial_exits_are_legs_of_one_completed_trade():
    """Was ``test_g2_partial_exits_each_make_a_trade``. The engine still emits one closing LEG
    per partial exit (the rupee accounting, fill links and charges depend on them), but the
    legs share the position's ``position_seq`` and the position goes FLAT once: ONE completed
    trade of 30 @ 200 -> (10 * 210 + 20 * 190) / 30 = 196.6667, pnl -100 (a loss, although
    its first leg won)."""
    from services import discretionary_analytics as an

    fills = [
        mk(1, "BUY", 30, 200, at(0)),
        mk(2, "SELL", 10, 210, at(1)),
        mk(3, "SELL", 20, 190, at(2)),
    ]
    out = rec.reconstruct(fills)
    pnls = [t["realized_pnl"] for t in out["trades"]]
    assert pnls == pytest.approx([100.0, -200.0])  # two legs
    assert {t["position_seq"] for t in out["trades"]} == {1}
    legs = [
        dict(t, id=i + 1, charges=0.0, closed_at=t["closed_at"].isoformat())
        for i, t in enumerate(out["trades"])
    ]
    (done,) = an.group_completed(legs, [dict(out["positions"][0], id=7)])
    assert (done["id"], done["legs"], done["closed_quantity"]) == (7, 2, 30.0)
    assert done["exit_price"] == pytest.approx(5900.0 / 30)
    assert done["realized_pnl"] == pytest.approx(-100.0)
    assert done["hold_minutes"] == out["trades"][-1]["hold_minutes"]
    assert sum(pnls) == pytest.approx(-100.0)
    assert out["positions"][0]["status"] == "FLAT"
    assert out["positions"][0]["open_qty"] == 0
    ref, ref_trades, qty, _ = naive_reference(fills)
    assert (
        ref_trades == pytest.approx([100.0, -200.0]) and ref == pytest.approx(-100.0) and qty == 0
    )


def test_g3_flip_long_to_short_then_cover():
    fills = [mk(1, "BUY", 10, 100, at(0)), mk(2, "SELL", 25, 90, at(1))]
    out = rec.reconstruct(fills)
    assert len(out["trades"]) == 1
    t = out["trades"][0]
    assert (t["direction"], t["closed_quantity"], t["realized_pnl"]) == (
        "LONG",
        10,
        pytest.approx(-100.0),
    )
    shorts = [p for p in out["positions"] if p["status"] == "OPEN"]
    assert len(shorts) == 1
    s = shorts[0]
    assert (s["direction"], s["open_qty"], s["avg_entry"], s["position_seq"]) == (
        "SHORT",
        15,
        90,
        2,
    )
    assert s["opened_at"] == at(1)

    fills.append(mk(3, "BUY", 15, 80, at(2)))
    out = rec.reconstruct(fills)
    assert [t["direction"] for t in out["trades"]] == ["LONG", "SHORT"]
    assert out["trades"][1]["realized_pnl"] == pytest.approx(150.0)
    assert out["trades"][1]["position_seq"] == 2
    assert all(p["status"] == "FLAT" for p in out["positions"])
    ref, ref_trades, qty, _ = naive_reference(fills)
    assert ref_trades == pytest.approx([-100.0, 150.0]) and qty == 0


def test_g4_cnc_overnight_hold_and_trade_date():
    out = rec.reconstruct(
        [
            mk(1, "BUY", 10, 500, datetime(2026, 11, 16, 14, 0), product="CNC"),
            mk(2, "SELL", 10, 520, datetime(2026, 11, 17, 10, 0), product="CNC"),
        ]
    )
    t = out["trades"][0]
    assert t["trade_date"] == "2026-11-17"
    assert t["overnight"] is True
    assert t["hold_minutes"] == 1200.0
    assert t["realized_pnl"] == pytest.approx(200.0)
    assert out["positions"][0]["opened_session"] == "2026-11-16"


def test_g5_accounts_never_merge():
    fills = [
        mk(1, "BUY", 10, 100, at(0), account="A"),
        mk(2, "BUY", 10, 120, at(1), account="B"),
        mk(3, "SELL", 10, 110, at(2), account="A"),
        mk(4, "SELL", 10, 115, at(3), account="B"),
    ]
    out = rec.reconstruct(fills)
    by_acct = {t["account"]: t["realized_pnl"] for t in out["trades"]}
    assert by_acct == {"A": pytest.approx(100.0), "B": pytest.approx(-50.0)}
    assert len(out["trades"]) == 2


def test_products_never_merge():
    out = rec.reconstruct(
        [
            mk(1, "BUY", 10, 100, at(0), product="MIS"),
            mk(2, "BUY", 10, 100, at(1), product="CNC"),
            mk(3, "SELL", 10, 110, at(2), product="MIS"),
        ]
    )
    assert len(out["trades"]) == 1 and out["trades"][0]["product"] == "MIS"
    still_open = [p for p in out["positions"] if p["status"] == "OPEN"]
    assert [p["product"] for p in still_open] == ["CNC"]


# ---------------------------------------------------------------- session / hold rules


def test_session_date_rolls_back_before_expiry(monkeypatch):
    monkeypatch.delenv("SESSION_EXPIRY_TIME", raising=False)
    assert rec.session_date(datetime(2026, 11, 17, 2, 59)).isoformat() == "2026-11-16"
    assert rec.session_date(datetime(2026, 11, 17, 3, 0)).isoformat() == "2026-11-17"
    monkeypatch.setenv("SESSION_EXPIRY_TIME", "05:30")
    assert rec.session_date(datetime(2026, 11, 17, 5, 29)).isoformat() == "2026-11-16"


def test_hold_minutes_same_rule_as_enrichment():
    from services.strategy_trade_enrichment import hold_minutes as enrich_hold

    a, b = datetime(2026, 11, 17, 10, 0, 0), datetime(2026, 11, 17, 10, 7, 30)
    assert rec.hold_minutes(a, b) == enrich_hold(
        {"opened_at": a.isoformat(), "closed_at": b.isoformat()}
    )
    assert rec.hold_minutes(b, a) is None


def test_intraday_trade_is_not_overnight():
    out = rec.reconstruct([mk(1, "BUY", 5, 10, at(0)), mk(2, "SELL", 5, 11, at(30))])
    assert out["trades"][0]["overnight"] is False
    assert out["trades"][0]["hold_minutes"] == 30.0


# ---------------------------------------------------------------- orphans / opening unknown


def test_cnc_sell_without_buy_is_orphaned_and_flagged():
    fills = [
        mk(1, "SELL", 10, 520, at(0), product="CNC"),
        mk(2, "BUY", 5, 500, at(1), product="CNC"),
    ]
    out = rec.reconstruct(fills)
    assert out["trades"] == []
    assert [o["fill_ref"] for o in out["orphans"]] == [1]
    assert out["orphans"][0]["reason"] == rec.REASON_OPENING_UNKNOWN
    (pos,) = out["positions"]
    assert pos["open_qty"] == 5 and pos["opening_unknown"] is True  # opened after the orphan


def test_cnc_sell_beyond_position_orphans_the_excess_only():
    out = rec.reconstruct(
        [mk(1, "BUY", 10, 100, at(0), product="CNC"), mk(2, "SELL", 15, 110, at(1), product="CNC")]
    )
    assert len(out["trades"]) == 1 and out["trades"][0]["closed_quantity"] == 10
    assert out["orphans"][0]["qty"] == pytest.approx(5.0)
    assert out["orphans"][0]["reason"] == rec.REASON_EXCESS_OPENING_UNKNOWN
    assert all(p["status"] == "FLAT" for p in out["positions"])


def test_nfo_nrml_option_sell_to_open_then_buy_back_is_one_short_trade():
    fills = [
        mk(1, "SELL", 75, 120, at(0), product="NRML", exchange="NFO", symbol="OPT"),
        mk(2, "BUY", 75, 80, at(1), product="NRML", exchange="NFO", symbol="OPT"),
    ]
    out = rec.reconstruct(fills)
    assert out["orphans"] == []
    (t,) = out["trades"]
    assert (t["direction"], t["closed_quantity"]) == ("SHORT", 75)
    assert t["realized_pnl"] == pytest.approx(3000.0)
    (pos,) = out["positions"]
    assert pos["status"] == "FLAT" and pos["direction"] == "SHORT"
    realized, ref_trades, qty, _avg = naive_reference(fills)
    assert (realized, ref_trades, qty) == (pytest.approx(3000.0), [3000.0], 0.0)


def test_nrml_futures_long_then_oversell_then_buy_back_keeps_the_short_leg():
    fills = [
        mk(1, "BUY", 50, 100, at(0), product="NRML", exchange="NFO", symbol="FUT"),
        mk(2, "SELL", 100, 110, at(1), product="NRML", exchange="NFO", symbol="FUT"),
        mk(3, "BUY", 50, 105, at(2), product="NRML", exchange="NFO", symbol="FUT"),
    ]
    out = rec.reconstruct(fills)
    assert out["orphans"] == []
    assert [(t["direction"], t["closed_quantity"]) for t in out["trades"]] == [
        ("LONG", 50),
        ("SHORT", 50),
    ]
    assert [t["realized_pnl"] for t in out["trades"]] == [
        pytest.approx(500.0),
        pytest.approx(250.0),
    ]
    assert all(p["status"] == "FLAT" for p in out["positions"])
    realized, ref_trades, qty, _avg = naive_reference(fills)
    assert qty == 0.0
    assert ref_trades == [pytest.approx(t["realized_pnl"]) for t in out["trades"]]
    assert realized == pytest.approx(sum(t["realized_pnl"] for t in out["trades"]))


@pytest.mark.parametrize("exchange", ["NFO", "BFO", "MCX", "CDS", "BCD", "NCO"])
@pytest.mark.parametrize("product", ["NRML", "CNC", "MIS"])
def test_derivative_exchanges_open_shorts_for_any_product(exchange, product):
    out = rec.reconstruct([mk(1, "SELL", 10, 100, at(0), product=product, exchange=exchange)])
    assert out["orphans"] == []
    (pos,) = out["positions"]
    assert pos["direction"] == "SHORT" and pos["open_qty"] == 10


def test_nse_cnc_sell_with_no_buy_is_still_orphaned():
    out = rec.reconstruct([mk(1, "SELL", 50, 100, at(0), product="CNC", exchange="NSE")])
    assert out["positions"] == [] and out["orphans"][0]["reason"] == rec.REASON_OPENING_UNKNOWN


@pytest.mark.parametrize("exchange", ["NSE", "BSE"])
def test_cash_nrml_sell_with_no_buy_is_orphaned(exchange):
    out = rec.reconstruct([mk(1, "SELL", 50, 100, at(0), product="NRML", exchange=exchange)])
    assert out["positions"] == [] and out["trades"] == []
    assert out["orphans"][0]["reason"] == rec.REASON_OPENING_UNKNOWN


def test_cnc_never_shorts_even_when_flat_after_a_round_trip():
    fills = [
        mk(1, "BUY", 10, 100, at(0), product="CNC"),
        mk(2, "SELL", 15, 110, at(1), product="CNC"),
    ]
    out = rec.reconstruct(fills)
    assert out["orphans"][0]["reason"] == rec.REASON_EXCESS_OPENING_UNKNOWN
    assert all(p["status"] == "FLAT" for p in out["positions"])


def test_engine_version_bumped_for_the_short_rule():
    assert rec.ENGINE_VERSION != "1"


def test_mis_short_is_allowed():
    out = rec.reconstruct([mk(1, "SELL", 10, 100, at(0)), mk(2, "BUY", 10, 90, at(1))])
    assert out["orphans"] == []
    t = out["trades"][0]
    assert (t["direction"], t["realized_pnl"]) == ("SHORT", pytest.approx(100.0))


@pytest.mark.parametrize(
    "qty,price", [(0, 100), (-5, 100), (5, 0), (5, -1), (float("nan"), 10), (5, float("inf"))]
)
def test_invalid_fills_are_orphaned(qty, price):
    out = rec.reconstruct([mk(1, "BUY", qty, price, at(0))])
    assert out["positions"] == [] and out["trades"] == []
    assert out["orphans"][0]["reason"] == rec.REASON_INVALID


def test_float_residue_closes_flat():
    fills = [
        mk(1, "BUY", 0.1, 100, at(0)),
        mk(2, "BUY", 0.2, 100, at(1)),
        mk(3, "SELL", 0.3, 101, at(2)),
    ]
    out = rec.reconstruct(fills)
    assert out["positions"][0]["status"] == "FLAT" and out["positions"][0]["open_qty"] == 0
    assert len(out["trades"]) == 1


# ---------------------------------------------------------------- determinism


def test_input_order_does_not_matter():
    fills = [
        mk(1, "BUY", 10, 100, at(0)),
        mk(2, "SELL", 4, 105, at(5), order_id="B"),
        mk(3, "SELL", 6, 95, at(5), order_id="A"),
        mk(4, "BUY", 3, 99, at(9)),
    ]
    base = rec.reconstruct(fills)
    for seed in range(5):
        shuffled = fills[:]
        random.Random(seed).shuffle(shuffled)
        assert rec.reconstruct(shuffled) == base


def test_equal_timestamps_tie_break_on_order_then_fill_then_id():
    same = at(0)
    fills = [
        mk(3, "SELL", 10, 100, same, order_id="B"),
        mk(1, "BUY", 10, 100, same, order_id="A", fill_id="2"),
        mk(2, "BUY", 5, 100, same, order_id="A", fill_id="1"),
    ]
    ordered = sorted(fills, key=rec.sort_key)
    assert [f["id"] for f in ordered] == [2, 1, 3]  # A/fill 1, A/fill 2, then B
    # id breaks the tie when order and fill id are equal
    a, b = mk(7, "BUY", 1, 1, same, order_id="X"), mk(5, "BUY", 1, 1, same, order_id="X")
    assert [f["id"] for f in sorted([a, b], key=rec.sort_key)] == [5, 7]


def test_reconstruct_does_not_modify_input():
    fills = [mk(1, "BUY", 10, 100, at(0)), mk(2, "SELL", 4, 110, at(1))]
    import copy

    before = copy.deepcopy(fills)
    rec.reconstruct(fills)
    assert fills == before


# ---------------------------------------------------------------- links


def test_links_cover_every_used_quantity():
    fills = [
        mk(1, "BUY", 10, 100, at(0)),
        mk(2, "BUY", 10, 110, at(1)),
        mk(3, "SELL", 25, 120, at(2)),
    ]
    out = rec.reconstruct(fills)
    used = {}
    for lk in out["links"]:
        used[lk["fill_ref"]] = used.get(lk["fill_ref"], 0.0) + lk["qty_used"]
    assert used == {1: pytest.approx(10), 2: pytest.approx(10), 3: pytest.approx(25)}
    exits = [lk for lk in out["links"] if lk["role"] == rec.ROLE_EXIT]
    assert [(lk["fill_ref"], lk["qty_used"]) for lk in exits] == [(3, 20)]
    open_links = [lk for lk in out["links"] if "pos_idx" in lk]
    assert [(lk["fill_ref"], lk["qty_used"]) for lk in open_links] == [(3, pytest.approx(5))]


def test_partial_exit_consumes_entries_pro_rata():
    out = rec.reconstruct(
        [mk(1, "BUY", 10, 100, at(0)), mk(2, "BUY", 30, 100, at(1)), mk(3, "SELL", 20, 101, at(2))]
    )
    entry_links = [lk for lk in out["links"] if lk["role"] == "ENTRY" and "trade_idx" in lk]
    assert {lk["fill_ref"]: lk["qty_used"] for lk in entry_links} == {
        1: pytest.approx(5),
        2: pytest.approx(15),
    }


# ---------------------------------------------------------------- property: P&L identity


def _random_fills(rng, n, product="MIS"):
    fills = []
    for i in range(n):
        fills.append(
            mk(
                i + 1,
                rng.choice(["BUY", "SELL"]),
                rng.randint(1, 40),
                round(rng.uniform(50, 150), 2),
                at(i),
                product=product,
                symbol=rng.choice(["SBIN", "TCS"]),
                account=rng.choice(["A", "B"]),
            )
        )
    return fills


@pytest.mark.parametrize("seed", range(40))
def test_pnl_identity_for_random_sequences(seed):
    rng = random.Random(seed)
    fills = _random_fills(rng, rng.randint(1, 30))
    out = rec.reconstruct(fills)
    assert out["orphans"] == []
    marks = {}
    for f in fills:
        marks[(f["account"], f["symbol"])] = None
    for k in marks:
        marks[k] = round(rng.uniform(40, 160), 2)
    realized = sum(t["realized_pnl"] for t in out["trades"])
    unreal = 0.0
    open_signed_mark = 0.0
    for p in out["positions"]:
        if p["status"] != "OPEN":
            continue
        sign = 1 if p["direction"] == "LONG" else -1
        mark = marks[(p["account"], p["symbol"])]
        unreal += sign * p["open_qty"] * (mark - p["avg_entry"])
        open_signed_mark += sign * p["open_qty"] * mark
    sells = sum(f["qty"] * f["price"] for f in fills if f["side"] == "SELL")
    buys = sum(f["qty"] * f["price"] for f in fills if f["side"] == "BUY")
    assert realized + unreal == pytest.approx(sells - buys + open_signed_mark, abs=1e-6)
    # and the naive reference agrees per identity
    for acct in ("A", "B"):
        for sym in ("SBIN", "TCS"):
            sub = [f for f in fills if f["account"] == acct and f["symbol"] == sym]
            if not sub:
                continue
            ref, ref_trades, qty, avg = naive_reference(sub)
            mine = [t for t in out["trades"] if t["account"] == acct and t["symbol"] == sym]
            assert sum(t["realized_pnl"] for t in mine) == pytest.approx(ref, abs=1e-6)
            assert [round(t["realized_pnl"], 9) for t in mine] == pytest.approx(
                ref_trades, abs=1e-6
            )
            opens = [
                p
                for p in out["positions"]
                if p["status"] == "OPEN" and p["account"] == acct and p["symbol"] == sym
            ]
            if qty:
                assert opens[0]["open_qty"] == pytest.approx(abs(qty))
                assert opens[0]["avg_entry"] == pytest.approx(abs(avg))
            else:
                assert not opens
