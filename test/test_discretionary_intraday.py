"""Discretionary intraday overlay: today's confirmed and pending fills in one replay.

Uses the temp-SQLite ``env`` fixture of test_discretionary_pnl_service (quotes mocked, no
broker call, no HTTP). Run with:
    pytest -p no:cacheprovider test/test_discretionary_intraday.py -q
"""
# ruff: noqa: F811  (the env fixture is imported from test_discretionary_pnl_service)

import json
from datetime import datetime

import pytest
from test_discretionary_pnl_service import NOW, _clock_only, d, env  # noqa: F401

from services import discretionary_pnl_service as pnl

TODAY = "2026-11-17"


def perf(**kw):
    kw.setdefault("period", "1d")
    kw.setdefault("with_quotes", True)
    return pnl.get_performance(**kw)


def iday(**kw):
    return perf(**kw)["intraday"]


def approx(x):
    return pytest.approx(x, abs=1e-3)


def ref_average_cost(fills, may_short):
    """Independent reference: (signed qty, average, realized gross) after replaying
    (side, qty, price) tuples in order with average-cost accounting."""
    qty = avg = realized = 0.0
    for side, q, price in fills:
        signed = q if side == "BUY" else -q
        if qty == 0 or (qty > 0) == (signed > 0):
            if qty == 0 and signed < 0 and not may_short:
                continue
            avg = (avg * abs(qty) + price * abs(signed)) / (abs(qty) + abs(signed))
            qty += signed
            continue
        closing = min(abs(signed), abs(qty))
        realized += closing * (price - avg) * (1 if qty > 0 else -1)
        rest = abs(signed) - closing
        qty += signed * closing / abs(signed)
        if abs(qty) < 1e-9:
            qty = avg = 0.0
            if rest > 0 and may_short:
                qty, avg = (rest if side == "BUY" else -rest), price
    return qty, avg, realized


def only(rows, symbol="TCS"):
    (row,) = [r for r in rows if r["symbol"] == symbol]
    return row


# ---------------------------------------------------------------- the golden case


def test_golden_confirmed_plus_two_pending_entries(env):
    env.fill("BUY", 25, 2510.0, d(17, 9, 30), symbol="TCS", product="CNC")
    env.fill("BUY", 10, 2507.1, d(17, 10, 0), symbol="TCS", product="CNC", state="unresolved")
    env.fill("BUY", 10, 2513.9, d(17, 10, 5), symbol="TCS", product="CNC", state="unresolved")
    env.quotes[("TCS", "NSE")] = 2503.0
    out = perf()
    ov = out["intraday"]
    p = only(ov["positions"])
    assert p["direction"] == "LONG" and p["quantity"] == 45
    assert p["avg_entry"] == approx(2510.2222)
    assert p["status"] == "partially_reconciled"
    assert (p["confirmed_fills"], p["pending_fills"]) == (1, 2)
    assert p["unrealized"] == approx(-325.0)
    assert p["not_checked_against_broker"] is True and ov["not_checked_against_broker"] is True
    assert ov["totals"]["unrealized"] == approx(-325.0)
    assert ov["totals"]["total_gross"] == approx(-325.0) and ov["totals"]["total_complete"]
    # the confirmed book still shows only the confirmed 25
    (book_open,) = out["today"]["open_position_rows"]
    assert book_open["open_qty"] == 25


def test_multiple_pending_partial_fills_combine(env):
    env.fill("BUY", 25, 100.0, d(17, 9, 30), symbol="TCS")
    for i, (q, px) in enumerate([(5, 101.0), (5, 102.0), (10, 103.0)]):
        env.fill("BUY", q, px, d(17, 10, i), symbol="TCS", state="unresolved")
    env.quotes[("TCS", "NSE")] = 104.0
    p = only(iday()["positions"])
    qty, avg, _ = ref_average_cost(
        [("BUY", 25, 100.0), ("BUY", 5, 101.0), ("BUY", 5, 102.0), ("BUY", 10, 103.0)], True
    )
    assert p["quantity"] == qty == 45 and p["avg_entry"] == approx(avg)
    assert p["unrealized"] == approx(qty * (104.0 - avg))
    assert (p["confirmed_fills"], p["pending_fills"]) == (1, 3)


def test_pending_only_position_is_provisional(env):
    env.fill("BUY", 10, 50.0, d(17, 10, 0), state="unresolved")
    env.quotes[("SBIN", "NSE")] = 52.0
    p = only(iday()["positions"], "SBIN")
    assert p["status"] == "provisional" and p["unrealized"] == approx(20.0)


def test_confirmed_only_position_is_confirmed(env):
    env.fill("BUY", 10, 50.0, d(17, 10, 0))
    env.quotes[("SBIN", "NSE")] = 52.0
    assert only(iday()["positions"], "SBIN")["status"] == "confirmed"


# ---------------------------------------------------------------- exits


def test_pending_partial_exit_against_confirmed_entry_no_phantom_short(env):
    env.fill("BUY", 25, 2510.0, d(17, 9, 30), symbol="TCS", product="CNC")
    env.fill("SELL", 10, 2520.0, d(17, 10, 0), symbol="TCS", product="CNC", state="unresolved")
    env.quotes[("TCS", "NSE")] = 2530.0
    ov = iday()
    p = only(ov["positions"])
    assert (p["direction"], p["quantity"], p["avg_entry"]) == ("LONG", 15, approx(2510.0))
    assert p["status"] == "partially_reconciled"
    assert p["realized_so_far_provisional"] == approx(100.0)
    assert p["realized_so_far_confirmed"] == 0
    assert len(ov["positions"]) == 1
    assert ov["totals"]["provisional_realized"] == approx(100.0)
    assert ov["totals"]["confirmed_realized"] == 0
    (leg,) = ov["closed"]
    assert leg["status"] == "provisional" and leg["charges_estimated"] is True
    assert ov["unmatched"] == []


def test_confirmed_exit_with_pending_entry_mis_goes_flat_and_realizes(env):
    env.fill("BUY", 10, 100.0, d(17, 9, 30), state="unresolved")
    env.fill("SELL", 10, 110.0, d(17, 10, 0))
    ov = iday()
    assert ov["positions"] == []
    (leg,) = ov["closed"]
    # the leg rests on a pending entry, so it is provisional, not confirmed
    assert leg["status"] == "provisional" and leg["realized_gross"] == approx(100.0)
    assert ov["totals"]["confirmed_realized"] == 0
    assert ov["totals"]["provisional_realized"] == approx(100.0)


def test_confirmed_exit_with_pending_entry_cnc_is_unmatched_not_guessed(env):
    # the exit is confirmed but the entry is pending: the pending entry IS replayed, so it
    # matches; a CNC exit with no entry at all is an orphan listed separately.
    env.fill("SELL", 10, 110.0, d(17, 10, 0), product="CNC")
    ov = iday()
    assert ov["positions"] == [] and ov["closed"] == []
    (u,) = ov["unmatched"]
    assert u["reason"] == "opening_unknown" and u["today"] is True


def test_full_exit_fully_closed_is_confirmed(env):
    env.fill("BUY", 10, 100.0, d(17, 9, 30))
    env.fill("SELL", 10, 105.0, d(17, 10, 0))
    ov = iday()
    assert ov["positions"] == []
    (leg,) = ov["closed"]
    assert leg["status"] == "confirmed" and leg["charges_estimated"] is False
    assert ov["totals"]["confirmed_realized"] == approx(50.0)
    assert ov["totals"]["provisional_realized"] == 0


def test_short_with_pending_cover(env):
    env.fill("SELL", 20, 200.0, d(17, 9, 30))
    env.fill("BUY", 5, 190.0, d(17, 10, 0), state="unresolved")
    env.quotes[("SBIN", "NSE")] = 195.0
    p = only(iday()["positions"], "SBIN")
    qty, avg, realized = ref_average_cost([("SELL", 20, 200.0), ("BUY", 5, 190.0)], True)
    assert (p["direction"], p["quantity"]) == ("SHORT", 15) and qty == -15
    assert p["realized_so_far_provisional"] == approx(realized) == approx(50.0)
    assert p["unrealized"] == approx(15 * (200.0 - 195.0))


def test_reversal_with_pending_sell_opens_short_by_the_engine_rules(env):
    env.fill("BUY", 25, 100.0, d(17, 9, 30))
    env.fill("SELL", 30, 110.0, d(17, 10, 0), state="unresolved")
    env.quotes[("SBIN", "NSE")] = 108.0
    ov = iday()
    p = only(ov["positions"], "SBIN")
    assert (p["direction"], p["quantity"], p["avg_entry"]) == ("SHORT", 5, approx(110.0))
    assert p["status"] == "provisional"
    assert ov["totals"]["provisional_realized"] == approx(250.0)
    assert p["unrealized"] == approx(5 * (110.0 - 108.0))


def test_reversal_on_cnc_leaves_the_excess_unmatched(env):
    env.fill("BUY", 25, 100.0, d(17, 9, 30), product="CNC")
    env.fill("SELL", 30, 110.0, d(17, 10, 0), product="CNC", state="unresolved")
    ov = iday()
    assert ov["positions"] == []
    assert ov["totals"]["provisional_realized"] == approx(250.0)
    assert [u["reason"] for u in ov["unmatched"]] == ["excess_over_position_opening_unknown"]


# ---------------------------------------------------------------- identities


def test_multiple_instruments_and_accounts_and_products_never_merge(env):
    env.fill("BUY", 10, 100.0, d(17, 9, 30), symbol="A1")
    env.fill("BUY", 5, 200.0, d(17, 9, 31), symbol="B1", state="unresolved")
    env.fill("BUY", 7, 100.0, d(17, 9, 32), symbol="A1", account="B")
    env.fill("BUY", 3, 100.0, d(17, 9, 33), symbol="A1", product="NRML")
    env.fill("BUY", 4, 100.0, d(17, 9, 34), symbol="A1", state="unresolved")
    ov = iday()
    got = {(r["symbol"], r["account"], r["product"]): r["quantity"] for r in ov["positions"]}
    assert got == {
        ("A1", "A", "MIS"): 14,
        ("B1", "A", "MIS"): 5,
        ("A1", "B", "MIS"): 7,
        ("A1", "A", "NRML"): 3,
    }


def test_broker_account_filters_apply(env):
    env.fill("BUY", 10, 100.0, d(17, 9, 30), symbol="A1")
    env.fill("BUY", 10, 100.0, d(17, 9, 30), symbol="B1", state="unresolved")
    ov = iday(symbol="B1")
    assert [r["symbol"] for r in ov["positions"]] == ["B1"]


# ---------------------------------------------------------------- attribution transitions


def _core(ov):
    return (
        [(r["symbol"], r["direction"], r["quantity"], r["avg_entry"]) for r in ov["positions"]],
        ov["totals"]["confirmed_realized"] + ov["totals"]["provisional_realized"],
        ov["totals"]["total_gross"],
    )


def test_pending_to_manual_keeps_the_figures_and_only_changes_the_status(env):
    env.fill("BUY", 25, 100.0, d(17, 9, 30))
    n = env.fill("BUY", 20, 103.0, d(17, 10, 0), state="unresolved")
    env.fill("SELL", 5, 110.0, d(17, 10, 5), state="unresolved")
    env.quotes[("SBIN", "NSE")] = 105.0
    before = iday()
    for fid in (n, n + 1):
        with env.eng.begin() as c:
            c.exec_driver_sql(
                "UPDATE ledger_fills SET attribution_state='manual' WHERE id=?", (fid,)
            )
    after = iday()
    assert _core(before) == _core(after)
    assert only(before["positions"], "SBIN")["status"] == "partially_reconciled"
    assert only(after["positions"], "SBIN")["status"] == "confirmed"
    assert after["totals"]["provisional_realized"] == 0
    assert after["totals"]["confirmed_realized"] == before["totals"]["provisional_realized"]


def test_pending_to_strategy_removes_the_fill(env):
    env.fill("BUY", 25, 100.0, d(17, 9, 30))
    n = env.fill("BUY", 20, 103.0, d(17, 10, 0), state="unresolved")
    env.quotes[("SBIN", "NSE")] = 105.0
    assert only(iday()["positions"], "SBIN")["quantity"] == 45
    with env.eng.begin() as c:
        c.exec_driver_sql(
            "UPDATE ledger_fills SET attribution_state='strategy_tag' WHERE id=?", (n,)
        )
    p = only(iday()["positions"], "SBIN")
    assert p["quantity"] == 25 and p["status"] == "confirmed"


def test_strategy_evidence_pending_fill_is_listed_not_replayed(env):
    env.fill("BUY", 25, 100.0, d(17, 9, 30))
    n = env.fill("BUY", 20, 103.0, d(17, 10, 0), state="unresolved")
    ev = json.dumps({"ob": "ok", "os": None, "rk": "tag", "tc": "ABC", "tm": "L"})
    with env.eng.begin() as c:
        c.exec_driver_sql("UPDATE ledger_fills SET source_evidence=? WHERE id=?", (ev, n))
    ov = iday()
    assert only(ov["positions"], "SBIN")["quantity"] == 25
    (u,) = ov["unmatched"]
    assert u["reason"] == "strategy_evidence" and u["fill_ref"] == n


# ---------------------------------------------------------------- invariant and sessions


def test_no_pending_fills_equals_the_confirmed_book(env):
    env.fill("BUY", 25, 100.0, d(16, 9, 30), symbol="CARRY", product="CNC")  # earlier session
    env.fill("BUY", 10, 50.0, d(17, 9, 30), symbol="X1")
    env.fill("SELL", 4, 55.0, d(17, 9, 40), symbol="X1")
    env.fill("BUY", 10, 70.0, d(17, 9, 31), symbol="X2")
    env.fill("SELL", 10, 75.0, d(17, 9, 50), symbol="X2")
    env.fill("SELL", 3, 60.0, d(17, 10, 0), symbol="CARRY", product="CNC")
    env.quotes.update({("X1", "NSE"): 51.0, ("CARRY", "NSE"): 101.0})
    out = perf()
    ov, t = out["intraday"], out["today"]
    book = {
        (r["symbol"], r["direction"], r["open_qty"], r["avg_entry"])
        for r in t["open_position_rows"]
    }
    mine = {(r["symbol"], r["direction"], r["quantity"], r["avg_entry"]) for r in ov["positions"]}
    assert mine == book and len(mine) == 2
    assert ov["totals"]["confirmed_realized"] == approx(t["realized_gross"])
    assert ov["totals"]["provisional_realized"] == 0 and ov["totals"]["estimated_charges"] == 0
    assert ov["totals"]["confirmed_charges"] == approx(t["charges"])
    assert {r["status"] for r in ov["positions"]} == {"confirmed"}
    assert ov["totals"]["unrealized"] == approx(t["unrealized"])


def test_earlier_session_pending_fills_are_excluded_but_stay_in_the_provisional_block(env):
    env.fill("BUY", 10, 100.0, d(17, 9, 30))
    for i in range(1, 4):
        env.fill("BUY", 1, 90.0, d(16, 9, 30 + i), symbol="OLD", state="unresolved")
    env.fill("BUY", 2, 91.0, d(16, 9, 40), state="unresolved")  # same identity as the open one
    env.quotes[("SBIN", "NSE")] = 101.0
    out = perf()
    ov = out["intraday"]
    assert [r["symbol"] for r in ov["positions"]] == ["SBIN"]
    assert only(ov["positions"], "SBIN")["quantity"] == 10
    assert ov["totals"]["pending_fills"] == 0
    # the earlier-session fill on an identity open today is listed, not added
    assert [(u["reason"], u["symbol"]) for u in ov["unmatched"]] == [
        ("earlier_session_unresolved", "SBIN")
    ]
    assert out["provisional"]["fills"] == 4
    assert {r["symbol"] for r in out["provisional"]["open_positions"]} == {"OLD", "SBIN"}


def test_current_view_uses_today_while_history_totals_are_unchanged(env):
    env.fill("BUY", 10, 100.0, d(16, 9, 30))
    env.fill("SELL", 10, 110.0, d(16, 10, 0))
    env.fill("BUY", 10, 100.0, d(17, 9, 30))
    env.fill("SELL", 10, 104.0, d(17, 10, 0), state="unresolved")
    plain = perf(period="all")
    ov1, ov30 = perf(period="1d")["intraday"], perf(period="30d")["intraday"]
    assert ov1 == ov30  # the overlay ignores the period
    assert ov1["session_date"] == TODAY
    assert ov1["totals"]["confirmed_realized"] == 0
    assert ov1["totals"]["provisional_realized"] == approx(40.0)
    # existing figures do not move: pending is in no total
    assert plain["realized_gross"] == approx(100.0)
    assert plain["today"]["realized_gross"] == 0


def test_before_session_expiry_today_is_the_previous_session(env):
    env.now = datetime(2026, 11, 18, 1, 0)
    env.fill("BUY", 10, 100.0, d(17, 9, 30), state="unresolved")
    ov = iday()
    assert ov["session_date"] == "2026-11-17"
    assert only(ov["positions"], "SBIN")["status"] == "provisional"


# ---------------------------------------------------------------- prices


def test_missing_ltp_is_null_never_zero_and_the_total_is_partial(env):
    env.fill("BUY", 10, 100.0, d(17, 9, 30), symbol="A1")
    env.fill("BUY", 10, 100.0, d(17, 9, 30), symbol="B1", state="unresolved")
    env.fill("BUY", 10, 100.0, d(17, 9, 30), symbol="C1")
    env.fill("SELL", 10, 103.0, d(17, 9, 40), symbol="C1")
    env.quotes[("A1", "NSE")] = 102.0  # B1 has no quote
    ov = iday()
    a, b = only(ov["positions"], "A1"), only(ov["positions"], "B1")
    assert a["unrealized"] == approx(20.0)
    assert b["unrealized"] is None and b["price_status"] == "unavailable"
    assert b["unrealized_reason"]
    t = ov["totals"]
    assert t["unrealized"] == approx(20.0)  # the priced one only
    assert (t["priced_positions"], t["unpriced_positions"]) == (1, 1)
    assert t["total_complete"] is False and "1 open position" in t["partial_reason"]
    assert t["total_gross"] == approx(30.0 + 20.0)


def test_all_unpriced_has_no_unrealized_figure(env):
    env.fill("BUY", 10, 100.0, d(17, 9, 30), state="unresolved")
    t = iday()["totals"]
    assert t["unrealized"] is None and t["unpriced_positions"] == 1


def test_stale_price_is_not_used(env, monkeypatch):
    env.fill("BUY", 10, 100.0, d(17, 9, 30))
    env.quotes[("SBIN", "NSE")] = 120.0
    monkeypatch.setattr(pnl, "stale_after_s", lambda: -1.0)
    p = only(iday()["positions"], "SBIN")
    assert p["price_status"] == "stale" and p["unrealized"] is None
    assert iday()["totals"]["unpriced_positions"] == 1


def test_cached_price_is_not_used(env):
    env.fill("BUY", 10, 100.0, d(17, 9, 30))
    env.quotes[("SBIN", "NSE")] = 120.0
    perf()
    env.quote_fail = True
    p = only(iday()["positions"], "SBIN")
    assert p["price_status"] in ("cached", "unavailable") and p["unrealized"] is None


def test_no_quotes_requested_gives_null_unrealized(env):
    env.fill("BUY", 10, 100.0, d(17, 9, 30))
    ov = iday(with_quotes=False)
    p = only(ov["positions"], "SBIN")
    assert p["unrealized"] is None and p["price_status"] == "not_requested"


def test_book_quote_is_reused_and_no_broker_position_call_is_made(env):
    env.fill("BUY", 10, 100.0, d(17, 9, 30))
    env.fill("BUY", 5, 101.0, d(17, 9, 40), state="unresolved")
    env.quotes[("SBIN", "NSE")] = 102.0
    iday()
    assert env.positionbook_calls == 0
    assert len(env.quote_calls) == 1  # one fetch serves the book and the overlay


# ---------------------------------------------------------------- reconciliation


def test_pnl_and_charges_reconcile_against_an_independent_recompute(env):
    seq = [
        ("BUY", 20, 100.0, 30, "manual"),
        ("BUY", 10, 103.0, 31, "unresolved"),
        ("SELL", 12, 108.0, 40, "unresolved"),
        ("SELL", 8, 107.0, 41, "manual"),
    ]
    for side, q, px, mm, st in seq:
        env.fill(side, q, px, d(17, 9, mm), state=st)
    env.quotes[("SBIN", "NSE")] = 106.0
    ov = iday()
    qty, avg, realized = ref_average_cost([(s, q, px) for s, q, px, _, _ in seq], True)
    p = only(ov["positions"], "SBIN")
    assert (p["quantity"], p["avg_entry"]) == (qty, approx(avg))
    t = ov["totals"]
    assert t["confirmed_realized"] + t["provisional_realized"] == approx(realized)
    assert t["unrealized"] == approx(qty * (106.0 - avg))
    assert t["total_gross"] == approx(realized + qty * (106.0 - avg))
    leg_charges = sum(leg["charges"] for leg in ov["closed"])
    assert t["confirmed_charges"] + t["estimated_charges"] == approx(leg_charges)
    assert t["total_net"] == approx(t["total_gross"] - leg_charges)
    # a leg is confirmed only when its exit and every consumed entry are confirmed
    assert {leg["status"] for leg in ov["closed"]} == {"provisional"}


def test_confirmed_and_provisional_legs_split(env):
    env.fill("BUY", 10, 100.0, d(17, 9, 30), symbol="A1")
    env.fill("SELL", 10, 104.0, d(17, 9, 40), symbol="A1")
    env.fill("BUY", 10, 100.0, d(17, 9, 30), symbol="B1")
    env.fill("SELL", 10, 102.0, d(17, 9, 40), symbol="B1", state="unresolved")
    ov = iday()
    t = ov["totals"]
    assert t["confirmed_realized"] == approx(40.0) and t["provisional_realized"] == approx(20.0)
    assert t["confirmed_legs"] == 1 and t["provisional_legs"] == 1
    assert t["confirmed_charges"] > 0 and t["estimated_charges"] > 0
    est = [leg for leg in ov["closed"] if leg["charges_estimated"]]
    assert [leg["symbol"] for leg in est] == ["B1"]


def test_unmatched_is_separate_from_positions(env):
    env.fill("SELL", 10, 110.0, d(17, 10, 0), product="CNC", symbol="Z1")
    env.fill("BUY", 5, 50.0, d(17, 10, 5), symbol="Y1")
    ov = iday()
    assert [r["symbol"] for r in ov["positions"]] == ["Y1"]
    assert [u["symbol"] for u in ov["unmatched"]] == ["Z1"]


def test_the_block_is_read_only(env):
    env.fill("BUY", 10, 100.0, d(17, 9, 30))
    env.fill("BUY", 5, 101.0, d(17, 9, 40), state="unresolved")
    perf()
    before = env.snapshot()
    iday()
    assert env.snapshot() == before


def test_block_failure_degrades_to_a_warning(env, monkeypatch):
    env.fill("BUY", 10, 100.0, d(17, 9, 30))

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(pnl, "intraday_block", boom)
    out = perf()
    assert out["intraday"] is None
    assert any("intraday block unavailable" in w for w in out["warnings"])
    assert out["realized_gross"] == 0  # the rest of the page is unaffected


# ---------------------------------------------------------------- earlier-session fills (task 3)


def _replay_inputs(monkeypatch):
    """Record the fill ids that intraday_block hands to reconstruct (other callers are ignored)."""
    import sys

    real = pnl.reconstruct
    seen = []

    def spy(fills, *a, **k):
        if sys._getframe(1).f_code.co_name == "intraday_block":
            seen.append([f["id"] for f in fills])
        return real(fills, *a, **k)

    monkeypatch.setattr(pnl, "reconstruct", spy)
    return seen


def test_earlier_session_unresolved_fills_cannot_change_todays_figures(env, monkeypatch):
    env.fill("BUY", 10, 100.0, d(17, 9, 30), symbol="TCS", state="unresolved")
    env.fill("BUY", 5, 102.0, d(17, 9, 40), symbol="TCS")
    env.fill("SELL", 3, 106.0, d(17, 9, 50), symbol="TCS")
    env.quotes[("TCS", "NSE")] = 104.0
    with monkeypatch.context() as m:
        clean_ids = _replay_inputs(m)
        clean = iday()
    # same identity (TCS / MIS / account A), unresolved, an earlier session, wildly priced
    old = [
        env.fill("BUY", 500, 10.0, d(16, 9, 30), symbol="TCS", state="unresolved"),
        env.fill("SELL", 200, 900.0, d(16, 9, 40), symbol="TCS", state="unresolved"),
        env.fill("BUY", 7, 5000.0, d(16, 9, 50), symbol="TCS", state="unresolved"),
    ]
    with monkeypatch.context() as m:
        spy_ids = _replay_inputs(m)
        dirty = iday()
    # the earlier fills are not in the replay input, so nothing they carry reaches a figure
    (ids,) = spy_ids
    assert not set(old) & set(ids)
    assert len(ids) == len(set(ids)) == len(clean_ids[0]) == 3
    # every figure of the overlay is identical; only the informational list differs
    assert dirty["positions"] == clean["positions"]
    assert dirty["closed"] == clean["closed"]
    assert dirty["totals"] == clean["totals"]
    p = only(dirty["positions"])
    qty, avg, realized = ref_average_cost(
        [("BUY", 10, 100.0), ("BUY", 5, 102.0), ("SELL", 3, 106.0)], True
    )
    assert (p["quantity"], p["avg_entry"]) == (qty, approx(avg))
    assert p["unrealized"] == approx(qty * (104.0 - avg))
    assert dirty["totals"]["provisional_realized"] + dirty["totals"][
        "confirmed_realized"
    ] == approx(realized)
    # they are listed, flagged as an earlier session, and never as today's
    assert clean["unmatched"] == []
    assert {u["fill_ref"] for u in dirty["unmatched"]} == set(old)
    assert {u["reason"] for u in dirty["unmatched"]} == {"earlier_session_unresolved"}
    assert not any(u["today"] for u in dirty["unmatched"])


# ---------------------------------------------------------------- transitions (task 6)


def test_partial_transition_one_of_two_pending_becomes_manual(env, monkeypatch):
    env.fill("BUY", 25, 100.0, d(17, 9, 30))
    a = env.fill("BUY", 10, 103.0, d(17, 10, 0), state="unresolved")
    b = env.fill("BUY", 10, 107.0, d(17, 10, 5), state="unresolved")
    env.quotes[("SBIN", "NSE")] = 105.0
    spy = _replay_inputs(monkeypatch)
    before = iday()
    with env.eng.begin() as c:
        c.exec_driver_sql("UPDATE ledger_fills SET attribution_state='manual' WHERE id=?", (a,))
    after = iday()
    assert _core(before) == _core(after)  # same quantity, average, realized and total
    pb, pa = only(before["positions"], "SBIN"), only(after["positions"], "SBIN")
    assert (pb["confirmed_fills"], pb["pending_fills"]) == (1, 2)
    assert (pa["confirmed_fills"], pa["pending_fills"]) == (2, 1)
    assert pa["status"] == "partially_reconciled"
    assert after["totals"]["pending_fills"] == 1 and after["totals"]["confirmed_fills"] == 2
    for ids in spy:  # each fill id once per replay, in both states
        assert sorted(ids) == sorted(set(ids)) and {a, b} <= set(ids)


def test_a_repeated_fill_row_is_replayed_once(env, monkeypatch):
    env.fill("BUY", 25, 100.0, d(17, 9, 30))
    env.fill("BUY", 10, 103.0, d(17, 10, 0), state="unresolved")
    env.quotes[("SBIN", "NSE")] = 105.0
    captured = {}
    real = pnl.intraday_block

    def spy(fills, *a, **k):
        captured["args"] = (fills, *a)
        return real(fills, *a, **k)

    monkeypatch.setattr(pnl, "intraday_block", spy)
    base = iday()
    fills, *rest = captured["args"]
    doubled = list(fills) + list(fills)  # every row twice, as a defect upstream might produce
    seen = _replay_inputs(monkeypatch)
    again = real(doubled, *rest)
    (ids,) = seen
    assert len(ids) == len(set(ids)) == 2
    assert again["positions"] == base["positions"] and again["totals"] == base["totals"]
    assert only(again["positions"], "SBIN")["quantity"] == 35


def test_in_grace_strategy_evidence_then_manual_is_replayed_exactly_once(env, monkeypatch):
    env.fill("BUY", 25, 100.0, d(17, 9, 30))
    n = env.fill("BUY", 20, 103.0, d(17, 10, 55), state="unresolved")  # minutes old: in grace
    ev = json.dumps({"ob": "ok", "os": None, "rk": "tag", "tc": "ABC", "tm": "L"})
    with env.eng.begin() as c:
        c.exec_driver_sql("UPDATE ledger_fills SET source_evidence=? WHERE id=?", (ev, n))
    env.quotes[("SBIN", "NSE")] = 105.0
    seen = _replay_inputs(monkeypatch)
    first = iday()
    assert [u["fill_ref"] for u in first["unmatched"]] == [n]
    assert only(first["positions"], "SBIN")["quantity"] == 25
    assert n not in seen[0]
    with env.eng.begin() as c:
        c.exec_driver_sql("UPDATE ledger_fills SET attribution_state='manual' WHERE id=?", (n,))
    second = iday()
    assert second["unmatched"] == []
    assert seen[1].count(n) == 1
    p = only(second["positions"], "SBIN")
    assert p["quantity"] == 45 and p["status"] == "confirmed"
    assert second["totals"]["confirmed_fills"] == 2


def test_last_close_after_market_hours_is_counted_for_book_and_pending_only_symbols(env):
    env.fill("BUY", 10, 100.0, d(17, 9, 30))  # SBIN: confirmed, priced by the book
    env.fill("BUY", 10, 50.0, d(17, 9, 40), symbol="PEND", state="unresolved")  # fetched
    env.quotes.update({("SBIN", "NSE"): 104.0, ("PEND", "NSE"): 51.0})
    env.now = datetime(2026, 11, 17, 18, 0, 0)  # market shut, same session
    ov = iday()
    assert {p["price_status"] for p in ov["positions"]} == {"last_close"}
    assert only(ov["positions"], "SBIN")["unrealized"] == approx(40.0)
    assert only(ov["positions"], "PEND")["unrealized"] == approx(10.0)
    t = ov["totals"]
    assert t["unrealized"] == approx(50.0) and t["unpriced_positions"] == 0
    assert t["total_complete"] is True and t["partial_reason"] is None


def test_every_unreliable_status_gives_a_null_unrealized_and_a_trader_readable_reason(env):
    for status, text in pnl.INTRADAY_REASONS.items():
        assert text and text[0].isupper() and text.endswith(".")
        assert not any(w in text.lower() for w in ("status", "http", "null", "none", "error"))
        assert status in ("stale", "cached", "unavailable", "not_requested")
