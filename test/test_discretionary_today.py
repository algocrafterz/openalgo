"""Discretionary 'today' block: the current session from the same rebuilt book.

Reuses the temp-SQLite ``env`` fixture of test_discretionary_pnl_service (ledger and derived
book in one temp file; quotes / auth mocked, no broker call, no HTTP). Run with:
    pytest -p no:cacheprovider test/test_discretionary_today.py -q
"""
# ruff: noqa: F811  (the env fixture is imported from test_discretionary_pnl_service)

from datetime import datetime

import pytest
from test_discretionary_pnl_service import NOW, _clock_only, d, env  # noqa: F401

from database import discretionary_ledger_db as ledger
from services import discretionary_pnl_service as pnl

TODAY = "2026-11-17"  # session date of NOW (Tuesday 11:00 IST)


def perf(**kw):
    kw.setdefault("period", "30d")
    return pnl.get_performance(**kw)


def today(**kw):
    return perf(**kw)["today"]


def daily_row(out, day=TODAY):
    rows = [r for r in out["by_period"]["daily"] if r["period"] == day]
    return rows[0] if rows else None


def approx(x):
    return pytest.approx(x, abs=1e-3)  # figures are rounded to 4 places


def run(started, finished, status="ok"):
    ledger.record_sync_run(
        broker="flattrade",
        account="A",
        started_at=started,
        finished_at=finished,
        status=status,
        fetched=0,
        inserted=0,
        error=None,
        tag_min_created_at=None,
    )


def assert_reconciles(out):
    """Today's money equals the daily row of the session; counts equal its trades."""
    t = out["today"]
    row = daily_row(out)
    if row is None:
        assert t["realized_gross"] == 0 and t["completed_positions"] == 0
        return
    assert t["realized_gross"] == approx(row["realized_gross"])
    assert t["charges"] == approx(row["charges"])
    assert t["net"] == approx(row["net_of_charges"])
    assert t["charges_unavailable_legs"] == row["charges_unavailable_trades"]
    assert t["completed_positions"] == row["trades"]
    assert t["metrics"]["winning_trades"] == row["wins"]
    assert t["metrics"]["losing_trades"] == row["losses"]


# ---------------------------------------------------------------- lifecycle A / B / C


def test_a_buy_45_and_hold_is_open_with_unrealized_and_no_trade(env):
    env.fill("BUY", 45, 100, d(17, 9, 30))
    t = today()
    assert t["session_date"] == TODAY
    assert (t["open_positions"], t["partially_closed_positions"], t["completed_positions"]) == (
        1,
        0,
        0,
    )
    assert t["realized_gross"] == 0 and t["closed_positions"] == []
    assert t["unrealized"] is None and t["quotes_requested"] is False
    assert t["price_statuses"] == {"not_requested": 1}
    row = t["open_position_rows"][0]
    assert row["status"] == "Open" and row["open_qty"] == 45 and row["total_quantity"] == 45
    env.quotes[("SBIN", "NSE")] = 104.0
    t = today(with_quotes=True)
    assert t["unrealized"] == approx(45 * 4.0)
    assert t["price_statuses"] == {"live": 1}
    assert t["metrics"]["trades_count"] == 0


def test_b_buy_45_sell_45_is_one_completed_position_with_charges_and_net(env):
    env.fill("BUY", 45, 100, d(17, 9, 30))
    env.fill("SELL", 45, 110, d(17, 10, 15))
    out = perf()
    t = out["today"]
    assert (t["completed_positions"], t["open_positions"], t["partially_closed_positions"]) == (
        1,
        0,
        0,
    )
    assert t["realized_gross"] == approx(450.0)
    assert t["charges"] > 0 and t["charges_complete"] is True
    assert t["net"] == approx(450.0 - t["charges"])
    (row,) = t["closed_positions"]
    assert row["symbol"] == "SBIN" and row["direction"] == "LONG" and row["status"] == "Closed"
    assert row["total_quantity"] == 45 and row["legs"] == 1
    assert row["entry_price"] == approx(100) and row["exit_price"] == approx(110)
    assert row["opened_at"] == "2026-11-17 09:30:00" and row["closed_at"] == "2026-11-17 10:15:00"
    assert row["gross_pnl"] == approx(450) and row["net_pnl"] == approx(450 - row["charges"])
    assert row["charges"] == approx(t["charges"]) and row["charges_status"] == "ok"
    m = t["metrics"]
    assert m["trades_count"] == 1 and m["winning_trades"] == 1 and m["win_rate"] == 100
    assert_reconciles(out)


def test_c_partial_exit_then_final_exit(env):
    env.fill("BUY", 45, 100, d(17, 9, 30))
    env.fill("SELL", 20, 110, d(17, 9, 45))  # +200 on 20
    env.quotes[("SBIN", "NSE")] = 90.0
    out = perf(with_quotes=True)
    t = out["today"]
    assert t["realized_gross"] == approx(200)
    assert (t["completed_positions"], t["open_positions"], t["partially_closed_positions"]) == (
        0,
        1,
        1,
    )
    assert t["closed_positions"] == [] and t["metrics"]["trades_count"] == 0
    row = t["open_position_rows"][0]
    assert row["status"] == "Partially closed"
    assert (row["open_qty"], row["closed_qty"], row["total_quantity"]) == (25, 20, 45)
    assert row["realized_so_far"] == approx(200) and row["realized_today"] == approx(200)
    assert t["unrealized"] == approx(25 * (90 - 100))
    # the partial exit counts as money on its day, never as a completed trade
    assert out["metrics"]["trades_count"] == 0 and out["realized_gross"] == approx(200)
    assert_reconciles(out)

    env.fill("SELL", 25, 80, d(17, 10, 40))  # -500 on 25: the round trip loses 300
    out = perf()
    t = out["today"]
    assert (t["completed_positions"], t["open_positions"], t["partially_closed_positions"]) == (
        1,
        0,
        0,
    )
    assert t["realized_gross"] == approx(-300)
    (row,) = t["closed_positions"]
    assert row["total_quantity"] == 45 and row["legs"] == 2 and row["gross_pnl"] == approx(-300)
    assert row["exit_price"] == approx((20 * 110 + 25 * 80) / 45)
    m = t["metrics"]
    assert (m["trades_count"], m["winning_trades"], m["losing_trades"]) == (1, 0, 1)
    assert m["avg_loss"] == approx(300) and m["win_rate"] == 0
    assert_reconciles(out)


def test_multiple_partial_exits_20_10_15(env):
    env.fill("BUY", 45, 100, d(17, 9, 30))
    states = []
    for qty, price, mm in ((20, 101, 40), (10, 102, 50), (15, 103, 59)):
        env.fill("SELL", qty, price, d(17, 9, mm))
        t = today()
        states.append(
            (t["completed_positions"], t["open_positions"], t["partially_closed_positions"])
        )
    assert states == [(0, 1, 1), (0, 1, 1), (1, 0, 0)]
    out = perf()
    t = out["today"]
    assert t["exit_legs"] == 3 and t["realized_gross"] == approx(20 + 20 + 45)
    (row,) = t["closed_positions"]
    assert row["legs"] == 3 and row["total_quantity"] == 45
    assert t["metrics"]["trades_count"] == 1
    assert_reconciles(out)


# ---------------------------------------------------------------- several positions / accounts


def test_closed_round_trip_then_new_open_position_in_same_symbol(env):
    env.fill("BUY", 10, 100, d(17, 9, 20))
    env.fill("SELL", 10, 105, d(17, 9, 30))
    env.fill("BUY", 7, 106, d(17, 9, 40))
    t = today()
    assert (t["completed_positions"], t["open_positions"], t["partially_closed_positions"]) == (
        1,
        1,
        0,
    )
    assert t["open_position_rows"][0]["total_quantity"] == 7
    assert t["open_position_rows"][0]["realized_so_far"] == 0


def test_two_accounts_never_merge_and_filters_apply(env):
    env.fill("BUY", 10, 100, d(17, 9, 20), account="A")
    env.fill("BUY", 10, 200, d(17, 9, 21), account="B")
    env.fill("SELL", 10, 110, d(17, 9, 30), account="A")
    env.fill("SELL", 4, 190, d(17, 9, 31), account="B")
    t = today()
    assert (t["completed_positions"], t["open_positions"], t["partially_closed_positions"]) == (
        1,
        1,
        1,
    )
    assert t["realized_gross"] == approx(100 - 40)
    a = today(account="A")
    b = today(account="B")
    assert (a["completed_positions"], a["open_positions"], a["realized_gross"]) == (1, 0, 100)
    assert (b["completed_positions"], b["partially_closed_positions"]) == (0, 1)
    assert b["realized_gross"] == approx(-40)
    assert today(symbol="ITC")["realized_gross"] == 0
    assert today(broker="other")["open_positions"] == 0


def test_today_ignores_the_page_period(env, monkeypatch):
    monkeypatch.setenv("DISCRETIONARY_RELIABLE_SINCE", "2026-11-01")
    env.fill("BUY", 10, 100, d(17, 9, 20))
    env.fill("SELL", 10, 110, d(17, 9, 30))
    past = perf(period="custom", start="2026-11-02", end="2026-11-03")
    assert past["realized_gross"] == 0
    assert past["today"]["realized_gross"] == approx(100)
    assert past["today"]["completed_positions"] == 1


# ---------------------------------------------------------------- days


def test_previous_day_position_closed_today_counts_today_with_its_entry_date(env):
    env.fill("BUY", 10, 100, d(16, 10, 0), product="CNC")
    env.fill("SELL", 4, 104, d(16, 14, 0), product="CNC")  # partial exit yesterday: +16
    t = today()
    assert t["realized_gross"] == 0 and t["partially_closed_positions"] == 1
    assert t["open_position_rows"][0]["realized_today"] == 0
    assert t["open_position_rows"][0]["realized_so_far"] == approx(16)
    env.fill("SELL", 6, 110, d(17, 9, 30), product="CNC")  # final exit today: +60
    out = perf()
    t = out["today"]
    assert t["completed_positions"] == 1 and t["realized_gross"] == approx(60)
    (row,) = t["closed_positions"]
    assert row["opened_session"] == "2026-11-16" and row["gross_pnl"] == approx(76)
    assert row["realized_today"] == approx(60) and row["total_quantity"] == 10
    yesterday = daily_row(out, "2026-11-16")
    assert yesterday is not None and yesterday["realized_gross"] == approx(16)
    assert_reconciles(out)


def test_before_session_expiry_today_is_the_previous_session(env):
    env.fill("BUY", 10, 100, d(17, 10, 0))
    env.fill("SELL", 10, 101, d(17, 11, 0))
    env.now = datetime(2026, 11, 18, 2, 30)  # before SESSION_EXPIRY_TIME 03:00
    assert today()["session_date"] == TODAY and today()["completed_positions"] == 1
    env.now = datetime(2026, 11, 18, 9, 0)
    assert today()["session_date"] == "2026-11-18" and today()["completed_positions"] == 0


def test_today_reconciles_with_headline_for_1d_and_ratios_match(env):
    env.fill("BUY", 10, 100, d(17, 9, 20))
    env.fill("SELL", 10, 110, d(17, 9, 30))  # +100
    env.fill("BUY", 5, 200, d(17, 9, 40), symbol="ITC")
    env.fill("SELL", 5, 190, d(17, 9, 50), symbol="ITC")  # -50
    env.fill("BUY", 8, 50, d(17, 10, 0), symbol="TCS")
    env.fill("SELL", 3, 52, d(17, 10, 5), symbol="TCS")  # partial +6
    out = perf(period="1d")
    t = out["today"]
    assert out["period"]["start"] == out["period"]["end"] == TODAY
    assert t["realized_gross"] == approx(out["realized_gross"]) == approx(56)
    assert t["charges"] == approx(out["charges"]) and t["net"] == approx(out["net"])
    for name in pnl.TODAY_METRICS:
        assert t["metrics"][name] == out["metrics"][name], name
    m = t["metrics"]
    assert (m["trades_count"], m["winning_trades"], m["losing_trades"]) == (2, 1, 1)
    assert m["win_rate"] == 50 and m["profit_factor"] == 2
    assert m["avg_win"] == 100 and m["avg_loss"] == 50
    assert m["expectancy"] == approx(0.5 * 100 - 0.5 * 50)
    assert t["partially_closed_positions"] == 1 and t["completed_positions"] == 2
    assert_reconciles(out)


# ---------------------------------------------------------------- late fills / rebuilds


def test_late_earlier_fill_rebuilds_and_totals_stay_consistent(env):
    env.fill("BUY", 45, 100, d(17, 10, 0))
    env.fill("SELL", 45, 110, d(17, 10, 30))
    first = perf()
    assert first["today"]["completed_positions"] == 1
    # a fill from 09:30 is collected only now (out of time order)
    env.fill("BUY", 10, 90, d(17, 9, 30))
    out = perf()
    assert out["build_id"] != first["build_id"]
    t = out["today"]
    # 55 bought (avg (900 + 4500) / 55), 45 sold: one partially closed position, no trade
    avg = (10 * 90 + 45 * 100) / 55
    assert (t["completed_positions"], t["open_positions"], t["partially_closed_positions"]) == (
        0,
        1,
        1,
    )
    assert t["exit_legs"] == 1 and t["realized_gross"] == approx(45 * (110 - avg))
    row = t["open_position_rows"][0]
    assert (row["open_qty"], row["closed_qty"], row["total_quantity"]) == (10, 45, 55)
    assert out["metrics"]["trades_count"] == 0
    assert_reconciles(out)


def test_same_order_id_new_fill_completes_the_position(env):
    env.fill("BUY", 45, 100, d(17, 9, 30), order_id="X1")
    env.fill("SELL", 20, 105, d(17, 9, 40), order_id="X2", fill_id="X2a")
    assert today()["partially_closed_positions"] == 1
    env.fill("SELL", 25, 105, d(17, 9, 40), order_id="X2", fill_id="X2b")
    t = today()
    assert t["completed_positions"] == 1 and t["partially_closed_positions"] == 0
    assert t["closed_positions"][0]["legs"] == 2
    assert t["realized_gross"] == approx(225)


def _row(n, side, qty, price, when):
    return {
        "broker": "flattrade",
        "account": "A",
        "order_id": f"R{n}",
        "fill_id": f"RF{n}",
        "symbol": "SBIN",
        "exchange": "NSE",
        "product": "MIS",
        "side": side,
        "qty": float(qty),
        "price": float(price),
        "fill_time": when,
        "occurrence_idx": 0,
        "dedup_key": f"dup{n}",
        "attribution_state": "manual",
        "strategy": None,
        "synced_at": when,
        "raw_hash": None,
    }


def test_resync_of_the_same_rows_is_idempotent(env):
    rows = [_row(1, "BUY", 45, 100, d(17, 9, 30)), _row(2, "SELL", 20, 110, d(17, 9, 45))]
    assert ledger.insert_fills([dict(r) for r in rows]) == 2
    first = perf()
    assert ledger.insert_fills([dict(r) for r in rows]) == 0  # duplicate sync
    assert ledger.insert_fills([dict(r) for r in rows]) == 0
    again = perf()
    assert again["build_id"] == first["build_id"]
    assert again["today"] == first["today"]
    assert again["today"]["realized_gross"] == approx(200)
    assert pnl.rebuild()["rebuilt"] is False
    forced = pnl.rebuild(force=True)
    assert forced["rebuilt"] is True
    after = perf()
    strip = {"pending_attribution"}

    def view(t):
        return {
            k: v
            for k, v in t.items()
            if k not in strip and k not in ("closed_positions", "open_position_rows")
        }

    assert view(after["today"]) == view(first["today"])


# ---------------------------------------------------------------- attribution: excluded, visible


def test_unresolved_and_strategy_fills_are_excluded_and_listed_as_pending(env):
    env.fill("BUY", 45, 100, d(17, 9, 30))
    env.fill("SELL", 45, 110, d(17, 9, 40), state="unresolved")
    env.fill("BUY", 5, 50, d(17, 9, 45), symbol="STRAT", state="strategy_tag")
    out = perf()
    t = out["today"]
    # the unresolved exit is not applied: the position stays open, nothing realized
    assert t["realized_gross"] == 0 and t["completed_positions"] == 0 and t["open_positions"] == 1
    p = t["pending_attribution"]
    assert p["grace_count"] + p["other_total"] == 1
    assert out["executions_unresolved"] == 1
    assert all(r["symbol"] != "STRAT" for r in t["open_position_rows"])


def test_grace_held_fills_are_visible_with_attributable_time_and_excluded(env, monkeypatch):
    monkeypatch.delenv("DISCRETIONARY_LEDGER_MANUAL_GRACE_MIN", raising=False)
    env.fill("BUY", 45, 100, d(17, 9, 0))  # manual: an open position
    env.fill("SELL", 45, 105, d(17, 10, 50), state="unresolved")  # 5 min old at the last run
    env.fill("SELL", 3, 99, d(17, 10, 30), symbol="ITC", state="unresolved")  # 25 min: due now
    env.fill("SELL", 1, 99, d(17, 10, 20), symbol="LTIM", state="unresolved")  # 35 min: other
    env.fill("BUY", 2, 10, d(17, 9, 5), symbol="OLD", state="unresolved")  # past grace: other
    env.fill("BUY", 1, 10, d(16, 9, 5), symbol="OLDER", state="unresolved")  # other, not today
    run(datetime(2026, 11, 17, 10, 45), datetime(2026, 11, 17, 10, 46))
    run(datetime(2026, 11, 17, 10, 55), datetime(2026, 11, 17, 10, 56))
    env.fill("BUY", 4, 20, d(17, 10, 58), symbol="NEW", state="unresolved")  # after the last run
    before = env.snapshot()["ledger_fills"], env.snapshot()["ledger_attribution_audit"]
    t = today()
    p = t["pending_attribution"]
    assert p["grace_minutes"] == 30 and p["last_evaluated_at"] == "2026-11-17 10:55:00"
    got = {r["symbol"]: r for r in p["grace_fills"]}
    assert set(got) == {"SBIN", "ITC", "NEW"} and p["grace_count"] == 3
    assert got["SBIN"]["attributable_from"] == "2026-11-17 11:20:00"
    assert got["SBIN"]["due"] is False and got["SBIN"]["affects_open_position"] is True
    assert got["ITC"]["attributable_from"] == "2026-11-17 11:00:00" and got["ITC"]["due"] is True
    assert got["NEW"]["attributable_from"] == "2026-11-17 11:28:00"
    assert p["earliest_attributable_at"] == "2026-11-17 11:00:00"
    assert (p["other_today"], p["other_total"]) == (2, 3)
    # none of them reaches a figure: the position is still open, nothing realized
    assert t["realized_gross"] == 0 and t["open_positions"] == 1 and t["completed_positions"] == 0
    # read-only: no attribution changed
    assert (env.snapshot()["ledger_fills"], env.snapshot()["ledger_attribution_audit"]) == before


def test_grace_zero_reports_no_grace_hold(env, monkeypatch):
    monkeypatch.setenv("DISCRETIONARY_LEDGER_MANUAL_GRACE_MIN", "0")
    env.fill("SELL", 3, 99, d(17, 10, 50), state="unresolved")
    run(datetime(2026, 11, 17, 10, 55), datetime(2026, 11, 17, 10, 56))
    p = today()["pending_attribution"]
    assert p["grace_count"] == 0 and p["other_total"] == 1


def test_only_unresolved_fills_today_shows_zeros_and_pending(env):
    env.fill("BUY", 45, 100, d(17, 10, 50), state="unresolved")
    out = perf()
    t = out["today"]
    assert t["realized_gross"] == 0 and t["open_positions"] == 0 and t["closed_positions"] == []
    assert t["pending_attribution"]["grace_count"] == 1  # never evaluated by a sync
    assert out["executions_collected"] == 1 and out["executions_unresolved"] == 1


def test_build_id_contract_unchanged_for_closed_rows(env):
    env.fill("BUY", 45, 100, d(17, 9, 30))
    env.fill("SELL", 45, 110, d(17, 9, 40))
    out = perf()
    row = out["today"]["closed_positions"][0]
    assert row["build_id"] == out["build_id"]
    fills = pnl.get_trade_fills(row["id"], build_id=out["build_id"])
    assert fills is not None and fills["trade"]["closed_quantity"] == 45
    env.fill("BUY", 1, 10, d(17, 10, 0), symbol="ITC")
    with pytest.raises(pnl.BuildChanged):
        pnl.get_trade_fills(row["id"], build_id=out["build_id"])


def test_charges_unavailable_marks_today_incomplete(env, monkeypatch):
    env.fill("BUY", 10, 100, d(17, 9, 30), exchange="NFO", product="NRML", symbol="OPT")
    env.fill("SELL", 10, 110, d(17, 9, 40), exchange="NFO", product="NRML", symbol="OPT")
    out = perf()
    t = out["today"]
    if out["charges_unavailable_trades"] == 0:
        pytest.skip("NFO charges are modelled; nothing unavailable to show")
    assert t["charges_complete"] is False and t["charges_unavailable_legs"] == 1
    assert t["closed_positions"][0]["charges_status"] == "unavailable"
    assert t["closed_positions"][0]["net_pnl"] is None
    assert_reconciles(out)


def test_now_fixture_is_tuesday_market_hours():
    assert NOW.weekday() == 1 and NOW.hour == 11


def test_exit_classified_by_a_later_sync_completes_the_position_on_the_next_read(env):
    env.fill("BUY", 45, 100, d(17, 9, 30))
    exit_id = env.fill("SELL", 45, 110, d(17, 10, 40), state="unresolved")  # grace-held
    before = perf()
    t = before["today"]
    assert (t["completed_positions"], t["open_positions"], t["realized_gross"]) == (0, 1, 0)
    assert t["pending_attribution"]["grace_fills"][0]["affects_open_position"] is True
    # the sync after the grace window classifies it (what attribute_unresolved writes)
    assert ledger.set_attribution(exit_id, "manual", None, "absent from tags and engine", "none")
    after = perf()
    assert after["build_id"] != before["build_id"]  # fingerprint covers the state: rebuilt
    t = after["today"]
    assert (t["completed_positions"], t["open_positions"], t["realized_gross"]) == (1, 0, 450)
    assert t["pending_attribution"]["grace_count"] == 0
    assert_reconciles(after)
