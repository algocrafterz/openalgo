"""Discretionary dashboard additions: sample context, risk / consistency strip, "How you trade"
breakdowns (time of day, weekday, holding time), data-quality states and the provisional block
of the unresolved fills.

Reuses the temp-SQLite ``env`` fixture of test_discretionary_pnl_service (ledger and derived
book in one temp file; quotes / auth mocked, no broker call, no HTTP). Run with:
    pytest -p no:cacheprovider test/test_discretionary_insights.py -q
"""
# ruff: noqa: F811  (the env fixture is imported from test_discretionary_pnl_service)

import json
from datetime import datetime

import pytest
from test_discretionary_pnl_service import _clock_only, d, env  # noqa: F401

from services import discretionary_analytics as an
from services import discretionary_pnl_service as pnl


def perf(**kw):
    kw.setdefault("period", "30d")
    return pnl.get_performance(**kw)


def approx(x, tol=1e-3):
    return pytest.approx(x, abs=tol)


def t(h, m, s=0, day=17):
    return datetime(2026, 11, day, h, m, s)


def three_known_trades(env):
    """+100 and -40 on Mon 16, +60 on Tue 17 (MIS, NSE)."""
    env.fill("BUY", 10, 100, d(16, 9, 30))
    env.fill("SELL", 10, 110, d(16, 9, 45))  # +100, 15 min, entry 09:30
    env.fill("BUY", 10, 100, d(16, 10, 30))
    env.fill("SELL", 10, 96, d(16, 11, 0))  # -40, 30 min, entry 10:30
    env.fill("BUY", 10, 100, d(17, 9, 20))
    env.fill("SELL", 10, 106, d(17, 9, 22))  # +60, 2 min, entry 09:20


# ---------------------------------------------------------------- known numbers


def test_three_known_trades_metrics_and_consistency(env):
    three_known_trades(env)
    out = perf()
    m = out["metrics"]
    assert m["trades_count"] == 3
    assert m["win_rate"] == approx(66.67, 0.01)
    assert m["profit_factor"] == approx(4.0)
    assert m["avg_win"] == approx(80) and m["avg_loss"] == approx(40)
    assert m["expectancy"] == approx(40)
    c = out["consistency"]
    assert c["largest_win"]["realized_pnl"] == approx(100)
    assert c["largest_loss"]["realized_pnl"] == approx(-40)
    assert c["largest_win"]["build_id"] == out["build_id"]
    assert (c["max_consecutive_wins"], c["max_consecutive_losses"]) == (1, 1)
    # day 16: +60, day 17: +60
    assert (c["trading_days"], c["profitable_days"], c["losing_days"], c["flat_days"]) == (
        2,
        2,
        0,
        0,
    )
    assert c["avg_daily_pnl"] == approx(60)
    assert c["avg_daily_unavailable_reason"] is None
    assert c["max_drawdown"] == approx(m["max_drawdown"])
    assert c["avg_hold_minutes"] == out["hold"]["overall"]["mean_minutes"]
    s = out["sample"]
    assert (s["completed_trades"], s["trading_days"], s["min_trades"], s["min_days"]) == (
        3,
        2,
        30,
        10,
    )
    assert s["sufficient"] is False
    # the largest win opens through the existing build-id protected drill-down
    fills = pnl.get_trade_fills(c["largest_win"]["id"], out["build_id"])
    assert fills["trade"]["realized_pnl"] == approx(100)
    with pytest.raises(pnl.BuildChanged):
        pnl.get_trade_fills(c["largest_win"]["id"], "an-old-build")
    json.dumps(out)  # the whole answer stays JSON


def test_single_trading_day_has_no_average_daily_or_drawdown(env):
    env.fill("BUY", 10, 100, d(17, 9, 30))
    env.fill("SELL", 10, 95, d(17, 9, 45))
    c = perf()["consistency"]
    assert c["trading_days"] == 1 and c["losing_days"] == 1
    assert c["avg_daily_pnl"] is None and c["max_drawdown"] is None
    assert c["avg_daily_unavailable_reason"] == "needs at least 2 trading days"
    assert c["max_drawdown_unavailable_reason"] == "needs at least 2 trading days"
    assert c["largest_win"] is None and c["largest_loss"]["realized_pnl"] == approx(-50)


def test_empty_book_blocks(env):
    out = perf()
    c = out["consistency"]
    assert c["largest_win"] is None and c["largest_loss"] is None
    assert (c["max_consecutive_wins"], c["max_consecutive_losses"], c["trading_days"]) == (0, 0, 0)
    assert out["breakdowns"] == {"time_of_day": [], "weekday": [], "hold_bucket": []}
    dq = out["data_quality"]
    assert dq["collected"] == 0 and dq["reconciles"] is True
    assert out["provisional"]["trades"] == [] and out["provisional"]["fills"] == 0
    assert out["sample"]["completed_trades"] == 0


# ---------------------------------------------------------------- streaks (pure)


def _tr(i, pnl_value, closed="2026-11-17 10:00:00"):
    return {"id": i, "realized_pnl": pnl_value, "closed_at": closed}


def test_streaks_break_even_ends_a_streak():
    seq = [100, 50, 0, 20, -1, -2, -3]
    trades = [_tr(i, p, f"2026-11-17 10:{i:02d}:00") for i, p in enumerate(seq)]
    assert an.trade_streaks(trades) == (2, 3)
    assert an.trade_streaks([_tr(1, 5, "a"), _tr(2, 0, "b"), _tr(3, 5, "c")]) == (1, 0)
    assert an.trade_streaks([]) == (0, 0)


def test_streak_ties_on_close_time_are_ordered_by_id():
    # same closed_at: id order is +, +, -, regardless of list order
    trades = [_tr(3, -1), _tr(1, 5), _tr(2, 5)]
    assert an.trade_streaks(trades) == (2, 1)
    trades = [_tr(1, -1), _tr(3, -1), _tr(2, 5)]  # -, +, -
    assert an.trade_streaks(trades) == (1, 1)


def test_largest_win_tie_takes_the_earliest():
    trades = [
        {**_tr(2, 50, "2026-11-17 10:00:00"), "trade_date": "2026-11-17"},
        {**_tr(1, 50, "2026-11-17 11:00:00"), "trade_date": "2026-11-17"},
    ]
    for tr in trades:
        tr.update(broker="b", account="a", symbol="S", exchange="NSE", product="MIS")
        tr.update(direction="LONG")
    c = an.consistency(trades, [], {}, None)
    assert c["largest_win"]["id"] == 2


# ---------------------------------------------------------------- buckets (pure)


@pytest.mark.parametrize(
    "stamp,bucket",
    [
        ("2026-11-17 09:14:59", "other"),
        ("2026-11-17 09:15:00", "09:15-10:00"),
        ("2026-11-17 09:59:59", "09:15-10:00"),
        ("2026-11-17 10:00:00", "10:00-11:30"),
        ("2026-11-17 11:30:00", "11:30-13:30"),
        ("2026-11-17 13:30:00", "13:30-15:00"),
        ("2026-11-17 15:00:00", "15:00-15:30"),
        ("2026-11-17 15:29:59", "15:00-15:30"),
        ("2026-11-17 15:30:00", "other"),
        ("2026-11-17 18:05:00", "other"),
        (None, "other"),
    ],
)
def test_time_of_day_bucket_boundaries(stamp, bucket):
    assert an.time_of_day_bucket(stamp) == bucket


def test_time_of_day_accepts_datetimes():
    assert an.time_of_day_bucket(t(9, 46, 18)) == "09:15-10:00"


@pytest.mark.parametrize(
    "stamp,day",
    [
        ("2026-11-16 09:30:00", "Mon"),
        ("2026-11-17 09:30:00", "Tue"),
        ("2026-11-21 10:00:00", "Sat"),  # a weekend session is listed under its weekday
        ("2026-11-17 02:00:00", "Mon"),  # before SESSION_EXPIRY_TIME: the previous session
        (None, "unknown"),
    ],
)
def test_weekday_bucket(stamp, day):
    assert an.weekday_bucket(stamp) == day


@pytest.mark.parametrize(
    "row,bucket",
    [
        ({"hold_minutes": 0}, "<5m"),
        ({"hold_minutes": 4.99}, "<5m"),
        ({"hold_minutes": 5}, "5-15m"),
        ({"hold_minutes": 14.99}, "5-15m"),
        ({"hold_minutes": 15}, "15-60m"),
        ({"hold_minutes": 59.99}, "15-60m"),
        ({"hold_minutes": 60}, "1-4h"),
        ({"hold_minutes": 239.99}, "1-4h"),
        ({"hold_minutes": 240}, ">4h same day"),
        ({"hold_minutes": 30, "overnight": True}, "overnight"),
        ({"hold_minutes": None}, "unknown"),
    ],
)
def test_hold_bucket_boundaries(row, bucket):
    assert an.hold_bucket(row) == bucket


# ---------------------------------------------------------------- breakdowns through the service


def test_breakdowns_reuse_the_row_builder_and_reconcile(env):
    three_known_trades(env)
    out = perf()
    b = out["breakdowns"]
    tod = {r["key"]: r for r in b["time_of_day"]}
    assert list(tod) == ["09:15-10:00", "10:00-11:30"]
    assert tod["09:15-10:00"]["trades"] == 2 and tod["09:15-10:00"]["realized_gross"] == approx(160)
    assert tod["09:15-10:00"]["win_rate"] == 100 and tod["09:15-10:00"]["trading_days"] == 2
    assert tod["10:00-11:30"]["losses"] == 1 and tod["10:00-11:30"]["profit_factor"] == 0.0
    week = {r["key"]: r for r in b["weekday"]}
    assert list(week) == ["Mon", "Tue"]
    assert week["Mon"]["trades"] == 2 and week["Mon"]["realized_gross"] == approx(60)
    hold = {r["key"]: r["trades"] for r in b["hold_bucket"]}
    assert hold == {"<5m": 1, "15-60m": 2}  # 2 min; 15 min (lower bound included); 30 min
    for rows in b.values():
        assert sum(r["trades"] for r in rows) == out["metrics"]["trades_count"]
        assert sum(r["realized_gross"] for r in rows) == approx(out["realized_gross"])
        assert all(r["sample_ok"] is False for r in rows)  # 3 trades < 30
        # same row builder as by_symbol: identical field set plus trading_days / sample_ok
        assert set(rows[0]) == set(out["by_symbol"][0]) | {"trading_days", "sample_ok"}
    # symbol / side rows are unchanged
    assert {r["key"] for r in out["by_side"]} == {"LONG"}


def test_sample_ok_follows_the_existing_rule(monkeypatch):
    monkeypatch.setattr(pnl, "SAMPLE_MIN_TRADES", 2)
    monkeypatch.setattr(pnl, "SAMPLE_MIN_DAYS", 2)
    rows = pnl._with_sample([{"trades": 2, "trading_days": 2}, {"trades": 2, "trading_days": 1}])
    assert [r["sample_ok"] for r in rows] == [True, False]
    assert pnl.sample_context(30, 10)["sufficient"] is (
        30 >= pnl.SAMPLE_MIN_TRADES and 10 >= pnl.SAMPLE_MIN_DAYS
    )


def test_partial_exit_money_follows_its_leg_bucket(env):
    env.fill("BUY", 10, 100, d(17, 9, 30))
    env.fill("SELL", 5, 110, d(17, 9, 32))  # leg: 2 min, +50
    env.fill("SELL", 5, 90, d(17, 10, 30))  # leg: 60 min, -50; the trade: 60 min, 0
    out = perf()
    hold = {r["key"]: r for r in out["breakdowns"]["hold_bucket"]}
    assert hold["<5m"]["trades"] == 0 and hold["<5m"]["realized_gross"] == approx(50)
    assert hold["1-4h"]["trades"] == 1 and hold["1-4h"]["realized_gross"] == approx(-50)
    # break-even completed trade: no streak, no largest win / loss
    c = out["consistency"]
    assert c["largest_win"] is None and c["largest_loss"] is None
    assert (c["max_consecutive_wins"], c["max_consecutive_losses"]) == (0, 0)


def test_charges_unavailable_marks_net_incomplete_everywhere(env):
    env.fill("BUY", 50, 100, d(17, 9, 30), symbol="NIFTYFUT", exchange="NFO", product="NRML")
    env.fill("SELL", 50, 101, d(17, 9, 40), symbol="NIFTYFUT", exchange="NFO", product="NRML")
    out = perf()
    for rows in out["breakdowns"].values():
        assert rows[0]["net_charges_complete"] is False
        assert rows[0]["charges_unavailable_trades"] == 1
    assert out["consistency"]["largest_win"]["charges_status"] == "unavailable"
    assert out["consistency"]["largest_win"]["net_pnl"] is None
    assert out["by_period"]["daily"][0]["net_charges_complete"] is False
    assert out["consistency"]["avg_daily_net_complete"] is False
    dq = out["data_quality"]
    assert dq["charges_incomplete_legs"] == 1 and dq["charges_incomplete_trades"] == 1


# ---------------------------------------------------------------- data quality


def test_data_quality_four_states_reconcile(env):
    env.fill("BUY", 10, 100, d(17, 9, 20))  # included
    env.fill("SELL", 10, 101, d(17, 9, 25))  # included
    env.fill("BUY", 10, 100, d(17, 9, 30), symbol="ITC")  # open remainder
    env.fill("SELL", 4, 101, d(17, 9, 35), symbol="ITC")  # leg of an open position
    env.fill("SELL", 5, 100, d(17, 9, 40), symbol="HDFC", product="CNC")  # orphan: not placed
    env.fill("BUY", 10, 100, d(17, 9, 45), symbol="REV")  # included (closed by the reversal)
    env.fill("SELL", 15, 99, d(17, 9, 50), symbol="REV")  # closes REV and opens a short: open
    env.fill("BUY", 3, 50, d(17, 10, 5), symbol="UNR", state="unresolved")
    env.fill("SELL", 3, 51, d(17, 10, 6), symbol="UNR", state="unresolved")
    env.fill("BUY", 2, 60, d(17, 10, 6), symbol="STR", state="strategy_tag")  # never counted
    env.fill("BUY", 1, 10, d(3, 9, 20), symbol="OLD")  # outside the 7d period
    out = perf(period="7d")
    dq = out["data_quality"]
    assert dq["collected"] == 9 == out["executions_collected"]
    assert dq["awaiting_attribution"] == 2 == out["executions_unresolved"]
    assert dq["awaiting_grace"] + dq["awaiting_other"] == 2
    assert (dq["attributed_open"], dq["included"], dq["not_placed"]) == (3, 3, 1)
    assert dq["collected"] == (
        dq["awaiting_attribution"] + dq["attributed_open"] + dq["included"] + dq["not_placed"]
    )
    assert dq["reconciles"] is True
    # filters narrow every state
    dq = perf(period="7d", symbol="ITC")["data_quality"]
    assert (dq["collected"], dq["attributed_open"], dq["included"]) == (2, 2, 0)


def test_data_quality_prices_and_sync(env):
    env.fill("BUY", 10, 100, d(17, 9, 20))
    env.fill("BUY", 10, 100, d(17, 9, 21), symbol="ITC")
    dq = perf()["data_quality"]
    assert dq["prices"]["requested"] is False and dq["prices"]["statuses"] == {}
    env.quotes[("SBIN", "NSE")] = 101.0
    env.quotes[("ITC", "NSE")] = None  # no usable price
    out = perf(with_quotes=True)
    dq = out["data_quality"]
    assert dq["prices"]["requested"] is True
    assert dq["prices"]["statuses"] == {"live": 1, "unavailable": 1}
    assert dq["prices"]["missing"] == 1
    itc = [p for p in out["positions"] if p["symbol"] == "ITC"][0]
    assert itc["unrealized"] is None  # no price: unknown, never 0
    assert out["unrealized_total"] == approx(10.0)
    assert "last_successful_sync" in dq["sync"] and isinstance(dq["sync"]["problems"], list)
    assert not any(p.startswith("last successful sync:") for p in dq["sync"]["problems"])
    assert "reconciliation" not in dq


# ---------------------------------------------------------------- provisional


def tcs_round_trip(env, account="A", state="unresolved"):
    env.fill(
        "BUY",
        4,
        2392.6,
        t(9, 46, 18),
        symbol="TCS",
        order_id=f"B1{account}",
        account=account,
        state=state,
    )
    env.fill(
        "BUY",
        21,
        2392.0,
        t(9, 46, 18),
        symbol="TCS",
        order_id=f"B1{account}",
        account=account,
        state=state,
    )
    env.fill(
        "BUY",
        20,
        2401.8,
        t(10, 7, 49),
        symbol="TCS",
        order_id=f"B2{account}",
        account=account,
        state=state,
    )
    env.fill(
        "SELL",
        30,
        2395.2,
        t(10, 16, 38),
        symbol="TCS",
        order_id=f"S1{account}",
        account=account,
        state=state,
    )
    env.fill(
        "SELL",
        15,
        2395.2,
        t(10, 16, 38),
        symbol="TCS",
        order_id=f"S1{account}",
        account=account,
        state=state,
    )


HEADLINE_KEYS = (
    "realized_gross",
    "charges",
    "net",
    "realized_net_of_charges",
    "unrealized_total",
    "open_positions",
    "trades_total",
    "positions_total",
    "by_period",
    "by_symbol",
    "by_side",
    "by_product",
    "by_holding",
    "hold",
    "breakdowns",
    "consistency",
    "sample",
    "trades",
    "positions",
)


def _headline(out):
    keep = {k: out[k] for k in HEADLINE_KEYS}
    keep["metrics"] = out["metrics"]
    today = dict(out["today"])
    today.pop("pending_attribution")
    keep["today"] = today

    def strip(v):
        if isinstance(v, dict):
            return {k: strip(x) for k, x in v.items() if k != "build_id"}
        if isinstance(v, list):
            return [strip(x) for x in v]
        return v

    return strip(keep)


def test_provisional_tcs_round_trip_numbers(env):
    tcs_round_trip(env)
    out = perf()
    p = out["provisional"]
    assert p["fills"] == 5
    (tr,) = p["trades"]
    assert (tr["symbol"], tr["direction"], tr["product"], tr["account"]) == (
        "TCS",
        "LONG",
        "MIS",
        "A",
    )
    assert tr["closed_quantity"] == 45 and tr["legs"] == 2
    assert tr["entry_price"] == approx((4 * 2392.6 + 21 * 2392.0 + 20 * 2401.8) / 45)
    assert tr["entry_price"] == approx(2396.4089, 1e-4)
    assert tr["exit_price"] == approx(2395.2)
    assert tr["realized_pnl"] == approx(-54.4)
    assert tr["charges_status"] == "ok"
    assert tr["charges"] == approx(38.25, 0.05)
    assert tr["net_pnl"] == approx(tr["realized_pnl"] - tr["charges"])
    assert tr["net_pnl"] == approx(-92.65, 0.05)
    assert tr["opened_at"] == "2026-11-17 09:46:18" and tr["closed_at"] == "2026-11-17 10:16:38"
    assert tr["hold_minutes"] == approx(30.33, 0.01)
    assert tr["opening_unknown"] is False and tr["provisional"] is True
    assert "build_id" not in tr
    roles = [(f["role"], f["side"], f["qty_used"]) for f in tr["fills"]]
    assert sorted(roles) == sorted(
        [
            ("ENTRY", "BUY", 4),
            ("ENTRY", "BUY", 21),
            ("ENTRY", "BUY", 20),
            ("EXIT", "SELL", 30),
            ("EXIT", "SELL", 15),
        ]
    )
    assert sum(f["charge_allocated"] for f in tr["fills"]) == approx(tr["charges"])
    pm = p["metrics"]
    assert (pm["trades"], pm["wins"], pm["losses"], pm["win_rate"]) == (1, 0, 1, 0.0)
    assert pm["avg_loss"] == approx(54.4) and pm["expectancy"] == approx(-54.4)
    assert pm["realized_gross"] == approx(-54.4) and pm["net_charges_complete"] is True
    assert p["sample"]["sufficient"] is False
    # nothing of it reaches any figure
    assert out["realized_gross"] == 0 and out["metrics"]["trades_count"] == 0
    assert out["trades"] == [] and out["by_period"]["daily"] == []
    assert out["today"]["realized_gross"] == 0 and out["today"]["completed_positions"] == 0
    assert out["executions_unresolved"] == 5 and out["data_quality"]["awaiting_attribution"] == 5


def test_provisional_never_changes_any_figure(env):
    three_known_trades(env)
    env.fill("BUY", 10, 100, d(17, 9, 50), symbol="ITC")  # an open manual position
    env.quotes[("ITC", "NSE")] = 103.0
    before = perf(with_quotes=True)
    tcs_round_trip(env)
    env.quotes[("TCS", "NSE")] = 2500.0
    after = perf(with_quotes=True)
    assert after["provisional"]["trades"] and before["provisional"]["trades"] == []
    assert _headline(before) == _headline(after)
    summary = pnl.get_summary(period="30d", with_quotes=False)
    assert summary["provisional"]["trades"][0]["realized_pnl"] == approx(-54.4)
    assert summary["realized_gross"] == approx(120)


def test_provisional_after_classification_moves_into_the_book(env):
    tcs_round_trip(env)
    provisional = perf()["provisional"]["trades"][0]
    with env.eng.begin() as c:
        from sqlalchemy import text

        c.execute(text("UPDATE ledger_fills SET attribution_state='manual'"))
    out = perf()
    assert out["provisional"]["trades"] == []
    (real,) = out["trades"]
    for key in ("realized_pnl", "charges", "net_pnl", "entry_price", "exit_price"):
        assert real[key] == approx(provisional[key])
    assert out["data_quality"]["included"] == 5


def test_provisional_two_accounts_stay_separate_and_orphan_sell(env):
    tcs_round_trip(env, account="A")
    tcs_round_trip(env, account="B")
    env.fill("BUY", 10, 100, d(17, 9, 20), state="unresolved", account="A")
    env.fill("SELL", 10, 101, d(17, 9, 30), state="unresolved", account="B")  # MIS: opens short
    env.fill("SELL", 7, 500, d(17, 9, 35), symbol="HDFC", product="CNC", state="unresolved")
    p = perf()["provisional"]
    tcs = [tr for tr in p["trades"] if tr["symbol"] == "TCS"]
    assert sorted(tr["account"] for tr in tcs) == ["A", "B"]
    assert all(tr["closed_quantity"] == 45 for tr in tcs)
    assert len([tr for tr in p["trades"] if tr["symbol"] == "SBIN"]) == 0  # never merged
    opens = {(o["account"], o["direction"]) for o in p["open_positions"]}
    assert opens == {("A", "LONG"), ("B", "SHORT")}
    (orphan,) = p["orphans"]
    assert orphan["symbol"] == "HDFC" and orphan["reason"] == "opening_unknown"
    assert orphan["fill_time"] == "2026-11-17 09:35:00"
    assert p["metrics"]["trades"] == 2
    # account filter: only that account's fills are replayed
    p = perf(account="B")["provisional"]
    assert {tr["account"] for tr in p["trades"]} == {"B"}
    assert p["orphans"] == []


def test_provisional_orphan_exit_flags_the_reopened_trade(env):
    env.fill("SELL", 5, 100, d(17, 9, 15), product="CNC", state="unresolved")  # orphan
    env.fill("BUY", 5, 100, d(17, 9, 20), product="CNC", state="unresolved")
    env.fill("SELL", 5, 104, d(17, 9, 40), product="CNC", state="unresolved")
    p = perf()["provisional"]
    (tr,) = p["trades"]
    assert tr["opening_unknown"] is True and tr["realized_pnl"] == approx(20)
    assert len(p["orphans"]) == 1


def test_summary_stays_backward_compatible(env):
    three_known_trades(env)
    out = perf()
    for key in (
        "metrics",
        "by_period",
        "by_symbol",
        "by_side",
        "by_product",
        "by_holding",
        "hold",
        "trades",
        "positions",
        "today",
        "executions_collected",
        "withheld",
    ):
        assert key in out
    for key in ("sample", "data_quality", "provisional"):
        assert key in pnl.get_summary(period="30d", with_quotes=False)
    assert set(out["labels"]) >= {"sample", "consistency", "breakdowns", "data_quality"}
