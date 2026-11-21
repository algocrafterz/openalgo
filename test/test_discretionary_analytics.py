"""Pure aggregation helpers of the Discretionary Performance page (no database, no Flask).

Each helper is checked against a naive loop on randomized trades with fixed seeds, and the
daily buckets are reconciled with ``compute_metrics`` (daily_pnl and realized_gross).
Run with: uv run pytest test/test_discretionary_analytics.py -q
"""

import random
import statistics
from datetime import date, timedelta

import pytest

from services import discretionary_analytics as an
from services.strategy_metrics_service import compute_metrics

SYMBOLS = ("SBIN", "TCS", "INFY", "ITC")


def make_trades(seed, n=60, start=date(2026, 9, 20), days=75):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        day = start + timedelta(days=rng.randrange(days))
        cents = rng.randrange(-90000, 90001)  # exact in 2 decimals
        charges_ok = rng.random() > 0.2
        hold = None if rng.random() < 0.15 else float(rng.randrange(1, 3000))
        out.append(
            {
                "id": i + 1,
                "trade_date": day.isoformat(),
                "closed_at": f"{day.isoformat()} 15:00:00",
                "symbol": rng.choice(SYMBOLS),
                "direction": rng.choice(("LONG", "SHORT")),
                "product": rng.choice(("MIS", "CNC")),
                "overnight": rng.random() < 0.3,
                "hold_minutes": hold,
                "realized_pnl": cents / 100.0,
                "charges": (rng.randrange(0, 5000) / 100.0) if charges_ok else None,
                "charges_status": "ok" if charges_ok else "unavailable",
            }
        )
    return out


def metric_trades(trades):
    return [{"trade_date": t["trade_date"], "realized_pnl": t["realized_pnl"]} for t in trades]


def t(day, pnl, **kw):
    base = {
        "trade_date": day,
        "realized_pnl": pnl,
        "charges": 1.0,
        "symbol": "SBIN",
        "direction": "LONG",
        "product": "MIS",
        "overnight": False,
        "hold_minutes": 10.0,
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------- period_breakdown


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
@pytest.mark.parametrize("granularity", an.GRANULARITIES)
def test_period_breakdown_matches_naive_loop(seed, granularity):
    trades = make_trades(seed)

    def key(day):
        d = date.fromisoformat(day)
        if granularity == "daily":
            return d.isoformat()
        if granularity == "weekly":
            return (d - timedelta(days=d.weekday())).isoformat()
        return f"{d.year}-{d.month:02d}"

    naive = {}
    for x in trades:
        b = naive.setdefault(
            key(x["trade_date"]), {"n": 0, "w": 0, "l": 0, "g": 0.0, "c": 0.0, "u": 0}
        )
        b["n"] += 1
        # net basis: a trade without a charge estimate is neither a win nor a loss
        if x["charges"] is not None:
            b["w"] += x["realized_pnl"] - x["charges"] > 0
            b["l"] += x["realized_pnl"] - x["charges"] < 0
        b["g"] += x["realized_pnl"]
        if x["charges"] is None:
            b["u"] += 1
        else:
            b["c"] += x["charges"]
    rows = an.period_breakdown(trades, granularity)
    assert [r["period"] for r in rows] == sorted(naive)
    for r in rows:
        b = naive[r["period"]]
        assert (r["trades"], r["wins"], r["losses"]) == (b["n"], b["w"], b["l"])
        assert r["realized_gross"] == pytest.approx(b["g"], abs=1e-6)
        assert r["charges"] == pytest.approx(b["c"], abs=1e-6)
        assert r["charges_unavailable_trades"] == b["u"]
        assert r["net_of_charges"] == pytest.approx(b["g"] - b["c"], abs=1e-6)
        assert r["net_charges_complete"] is (b["u"] == 0)
        assert r["start"] <= r["period"] <= r["end"] or granularity == "monthly"


@pytest.mark.parametrize("seed", [11, 12, 13])
def test_daily_buckets_reconcile_exactly_with_compute_metrics(seed):
    trades = make_trades(seed)
    start, end = "2026-09-01", "2026-12-31"
    m = compute_metrics(metric_trades(trades), start, end, capital=None)
    daily = {r["period"]: r for r in an.period_breakdown(trades, "daily")}
    for point in m["daily_pnl"]:
        if point["date"] in daily:
            assert round(daily[point["date"]]["realized_gross"], 2) == point["pnl"]
        else:
            assert point["pnl"] == 0.0
    total = sum(r["realized_gross"] for r in daily.values())
    assert total == pytest.approx(sum(x["realized_pnl"] for x in trades), abs=1e-6)
    assert round(total, 2) == m["net_profit"]  # net_profit is the gross realized sum
    assert sum(r["trades"] for r in daily.values()) == m["trades_count"]
    assert sum(r["wins"] for r in daily.values()) >= 0


@pytest.mark.parametrize("granularity", ["weekly", "monthly"])
def test_coarser_buckets_sum_to_the_daily_total(granularity):
    trades = make_trades(21, n=120)
    total = sum(x["realized_pnl"] for x in trades)
    rows = an.period_breakdown(trades, granularity)
    assert sum(r["realized_gross"] for r in rows) == pytest.approx(total, abs=1e-6)
    assert sum(r["trades"] for r in rows) == len(trades)


def test_week_boundaries_sunday_and_monday():
    # 2026-11-15 is a Sunday, 2026-11-16 a Monday (ISO weeks start Monday)
    rows = an.period_breakdown(
        [t("2026-11-15", 10.0), t("2026-11-16", 20.0), t("2026-11-22", 5.0)], "weekly"
    )
    assert [r["period"] for r in rows] == ["2026-11-09", "2026-11-16"]
    assert rows[0]["realized_gross"] == 10.0 and rows[0]["end"] == "2026-11-15"
    assert rows[1]["realized_gross"] == 25.0 and rows[1]["trades"] == 2
    assert rows[1]["label"] == "2026-W47" and rows[1]["start"] == "2026-11-16"


def test_iso_week_label_crosses_the_year():
    rows = an.period_breakdown([t("2026-12-31", 1.0), t("2027-01-01", 2.0)], "weekly")
    assert len(rows) == 1 and rows[0]["label"] == "2026-W53"
    assert rows[0]["start"] == "2026-12-28" and rows[0]["end"] == "2027-01-03"


def test_month_boundaries():
    rows = an.period_breakdown(
        [t("2026-10-31", 1.0), t("2026-11-01", 2.0), t("2026-11-30", 3.0), t("2026-12-01", 4.0)],
        "monthly",
    )
    assert [(r["period"], r["trades"], r["realized_gross"]) for r in rows] == [
        ("2026-10", 1, 1.0),
        ("2026-11", 2, 5.0),
        ("2026-12", 1, 4.0),
    ]
    assert (rows[1]["start"], rows[1]["end"]) == ("2026-11-01", "2026-11-30")
    assert an.period_breakdown([t("2024-02-10", 1.0)], "monthly")[0]["end"] == "2024-02-29"
    assert an.period_breakdown([t("2026-12-10", 1.0)], "monthly")[0]["end"] == "2026-12-31"


def test_trade_date_is_the_session_date_not_the_close_clock_date():
    # closed after midnight but belongs to the previous session: trade_date wins
    trade = t("2026-11-13", 50.0, closed_at="2026-11-14 00:30:00")
    (row,) = an.period_breakdown([trade], "daily")
    assert row["period"] == "2026-11-13"
    (week,) = an.period_breakdown([trade], "weekly")
    assert week["period"] == "2026-11-09"


def test_none_charges_are_reported_not_hidden():
    rows = an.period_breakdown(
        [t("2026-11-16", 100.0, charges=10.0), t("2026-11-16", 50.0, charges=None)], "daily"
    )
    (r,) = rows
    assert r["charges"] == 10.0 and r["charges_unavailable_trades"] == 1
    assert r["net_of_charges"] == 140.0 and r["net_charges_complete"] is False
    assert r["realized_gross"] == 150.0


def test_empty_and_invalid_inputs():
    for g in an.GRANULARITIES:
        assert an.period_breakdown([], g) == []
    with pytest.raises(ValueError):
        an.period_breakdown([], "yearly")
    for key in ("symbol", "direction", "product"):
        assert an.group_breakdown([], key) == []
    with pytest.raises(ValueError):
        an.group_breakdown([], "strategy")
    h = an.holding_breakdown([])
    assert set(h) == {"intraday", "overnight"} and h["intraday"]["trades"] == 0
    # nothing has a net P&L: ratios are unknown (None), not 0; money totals stay
    assert h["intraday"]["profit_factor"] is None and h["intraday"]["avg_hold_minutes"] is None
    for name in ("win_rate", "expectancy", "avg_win", "avg_loss"):
        assert h["intraday"][name] is None
    assert h["intraday"]["stats_unavailable_reason"] == "no positions with a net P&L"
    assert h["intraday"]["realized_gross"] == 0.0 and h["intraday"]["charges"] == 0.0
    assert an.hold_summary([]) == {
        "overall": {"count": 0, "mean_minutes": None, "median_minutes": None},
        "winners": {"count": 0, "mean_minutes": None, "median_minutes": None},
        "losers": {"count": 0, "mean_minutes": None, "median_minutes": None},
    }


# ---------------------------------------------------------------- group_breakdown


def naive_stats(items):
    """Reference statistics on the NET basis (realized - charges); a trade whose charges are
    unavailable has no net: it is counted in ``trades`` but not in any ratio."""
    pnls = [x["realized_pnl"] - x["charges"] for x in items if x["charges"] is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = len(pnls)
    if n == 0:
        return {
            "trades": len(items),
            "net_basis_trades": 0,
            "net_unavailable_trades": len(items),
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "expectancy": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
        }
    wr = len(wins) / n
    aw = sum(wins) / len(wins) if wins else 0.0
    al = abs(sum(losses)) / len(losses) if losses else 0.0
    gl = abs(sum(losses))
    pf = sum(wins) / gl if gl > 0 else (None if sum(wins) > 0 else 0.0)
    return {
        "trades": len(items),
        "net_basis_trades": n,
        "net_unavailable_trades": len(items) - n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(wr * 100, 2),
        "profit_factor": None if pf is None else round(pf, 2),
        "expectancy": round(wr * aw - (1 - wr) * al, 2),
        "avg_win": round(aw, 2),
        "avg_loss": round(al, 2),
    }


@pytest.mark.parametrize("seed", [5, 6, 7])
@pytest.mark.parametrize("key", ["symbol", "direction", "product"])
def test_group_breakdown_matches_naive_loop(seed, key):
    trades = make_trades(seed)
    rows = an.group_breakdown(trades, key)
    names = sorted({x[key] for x in trades})
    assert sorted(r["key"] for r in rows) == names
    assert [r["realized_gross"] for r in rows] == sorted(
        (r["realized_gross"] for r in rows), reverse=True
    )
    for r in rows:
        items = [x for x in trades if x[key] == r["key"]]
        for field, value in naive_stats(items).items():
            assert r[field] == value, (r["key"], field)
        assert r["realized_gross"] == pytest.approx(sum(x["realized_pnl"] for x in items), abs=1e-6)
        charges = [x["charges"] for x in items if x["charges"] is not None]
        assert r["charges"] == pytest.approx(sum(charges), abs=1e-6)
        assert r["charges_unavailable_trades"] == len(items) - len(charges)
        holds = [x["hold_minutes"] for x in items if x["hold_minutes"] is not None]
        if holds:
            assert r["avg_hold_minutes"] == pytest.approx(sum(holds) / len(holds), abs=0.01)
            assert r["median_hold_minutes"] == pytest.approx(statistics.median(holds), abs=0.01)
        else:
            assert r["avg_hold_minutes"] is None and r["median_hold_minutes"] is None
    assert sum(r["trades"] for r in rows) == len(trades)


def test_group_ratios_equal_the_headline_for_one_group():
    trades = make_trades(8)
    for x in trades:
        x["symbol"] = "ONE"
    (row,) = an.group_breakdown(trades, "symbol")
    # net basis: compute_metrics over the net P&L of the trades that have one
    net_rows = [
        {"trade_date": x["trade_date"], "realized_pnl": x["realized_pnl"] - x["charges"]}
        for x in trades
        if x["charges"] is not None
    ]
    m = compute_metrics(net_rows, "2026-09-01", "2026-12-31", capital=None)
    assert row["win_rate"] == m["win_rate"]
    assert row["profit_factor"] == m["profit_factor"]
    assert row["expectancy"] == m["expectancy"]
    assert row["avg_win"] == m["avg_win"] and row["avg_loss"] == m["avg_loss"]
    gross = compute_metrics(metric_trades(trades), "2026-09-01", "2026-12-31", capital=None)
    assert row["realized_gross"] == pytest.approx(gross["net_profit"], abs=0.005)


def test_profit_factor_edge_cases_and_breakeven():
    only_wins = an.group_breakdown([t("2026-11-16", 10.0)], "symbol")[0]
    assert only_wins["profit_factor"] is None and only_wins["avg_loss"] == 0.0
    only_loss = an.group_breakdown([t("2026-11-16", -10.0)], "symbol")[0]
    assert only_loss["profit_factor"] == 0.0 and only_loss["win_rate"] == 0.0
    # net basis: break-even is net == 0 (gross 0 with no charges)
    flat = an.group_breakdown([t("2026-11-16", 0.0, charges=0.0)], "symbol")[0]
    assert (flat["wins"], flat["losses"], flat["profit_factor"], flat["expectancy"]) == (
        0,
        0,
        0.0,
        0.0,
    )
    # a gross-zero trade that paid charges is a NET loser (was break-even on the gross basis)
    paid = an.group_breakdown([t("2026-11-16", 0.0, charges=1.0)], "symbol")[0]
    assert (paid["wins"], paid["losses"], paid["avg_loss"], paid["expectancy"]) == (0, 1, 1.0, -1.0)
    # a gross winner whose charges exceed it is a net loser
    fee_eaten = an.group_breakdown([t("2026-11-16", 0.5, charges=1.0)], "symbol")[0]
    assert (fee_eaten["wins"], fee_eaten["losses"], fee_eaten["win_rate"]) == (0, 1, 0.0)


# ---------------------------------------------------------------- holding / hold


@pytest.mark.parametrize("seed", [31, 32])
def test_holding_breakdown_splits_on_the_overnight_flag(seed):
    trades = make_trades(seed)
    h = an.holding_breakdown(trades)
    for name, flag in (("intraday", False), ("overnight", True)):
        items = [x for x in trades if x["overnight"] is flag]
        assert h[name]["trades"] == len(items)
        assert h[name]["realized_gross"] == pytest.approx(
            sum(x["realized_pnl"] for x in items), abs=1e-6
        )
        if items:
            for field, value in naive_stats(items).items():
                assert h[name][field] == value
    assert h["intraday"]["trades"] + h["overnight"]["trades"] == len(trades)


@pytest.mark.parametrize("seed", [41, 42, 43])
def test_hold_summary_matches_naive_and_skips_none(seed):
    trades = make_trades(seed)
    out = an.hold_summary(trades)
    sets = {
        "overall": trades,
        "winners": [
            x for x in trades if x["charges"] is not None and x["realized_pnl"] - x["charges"] > 0
        ],
        "losers": [
            x for x in trades if x["charges"] is not None and x["realized_pnl"] - x["charges"] < 0
        ],
    }
    for name, items in sets.items():
        holds = [x["hold_minutes"] for x in items if x["hold_minutes"] is not None]
        assert out[name]["count"] == len(holds)
        if holds:
            assert out[name]["mean_minutes"] == pytest.approx(sum(holds) / len(holds), abs=0.01)
            assert out[name]["median_minutes"] == pytest.approx(statistics.median(holds), abs=0.01)
        else:
            assert out[name]["mean_minutes"] is None


def test_hold_summary_all_none_and_breakeven_only_in_overall():
    out = an.hold_summary(
        [
            t("2026-11-16", 0.0, charges=0.0, hold_minutes=30.0),  # net 0: break-even
            t("2026-11-16", 5.0, hold_minutes=None),
        ]
    )
    assert out["overall"] == {"count": 1, "mean_minutes": 30.0, "median_minutes": 30.0}
    assert out["winners"]["count"] == 0 and out["losers"]["count"] == 0


def test_inputs_are_not_mutated():
    trades = make_trades(51, n=10)
    before = [dict(x) for x in trades]
    an.period_breakdown(trades, "weekly")
    an.group_breakdown(trades, "symbol")
    an.holding_breakdown(trades)
    an.hold_summary(trades)
    assert trades == before


# ---------------------------------------------------------------- completed trades vs legs


def _leg(i, seq, qty, entry, exit_, pnl, day, hh, *, symbol="SBIN", charges=1.0, overnight=False):
    return {
        "id": i,
        "broker": "flattrade",
        "account": "A",
        "exchange": "NSE",
        "symbol": symbol,
        "product": "MIS",
        "position_seq": seq,
        "direction": "LONG",
        "closed_quantity": qty,
        "entry_price": entry,
        "exit_price": exit_,
        "opened_at": "2026-11-16 09:00:00",
        "closed_at": f"2026-11-{day:02d} {hh:02d}:00:00",
        "trade_date": f"2026-11-{day:02d}",
        "overnight": overnight,
        "hold_minutes": 60.0 * hh,
        "realized_pnl": pnl,
        "charges": charges,
        "charges_status": "ok" if charges is not None else "unavailable",
        "charges_breakdown": {"brokerage": charges} if charges is not None else {},
    }


def _pos(pid, seq, status="FLAT", symbol="SBIN"):
    return {
        "id": pid,
        "build_id": "b1",
        "broker": "flattrade",
        "account": "A",
        "exchange": "NSE",
        "symbol": symbol,
        "product": "MIS",
        "position_seq": seq,
        "direction": "LONG",
        "opened_at": "2026-11-16 09:00:00",
        "status": status,
    }


def test_group_completed_sums_legs_and_weights_prices():
    legs = [
        _leg(1, 1, 20, 100, 110, 200.0, 16, 10),
        _leg(2, 1, 25, 100, 90, -250.0, 17, 11, overnight=True),
        _leg(3, 2, 5, 50, 55, 25.0, 17, 12, charges=None),  # position 2 still open
    ]
    out = an.group_completed(legs, [_pos(9, 1), _pos(10, 2, status="OPEN")])
    (t,) = out  # an OPEN position is never a completed trade
    assert (t["id"], t["legs"], t["closed_quantity"]) == (9, 2, 45)
    assert t["entry_price"] == pytest.approx(100.0)
    assert t["exit_price"] == pytest.approx((20 * 110 + 25 * 90) / 45)
    assert t["realized_pnl"] == pytest.approx(-50.0)
    assert (t["charges"], t["charges_status"], t["net_pnl"]) == (2.0, "ok", -52.0)
    assert t["charges_breakdown"] == {"brokerage": 2.0}
    # date, close, hold and overnight of the FINAL leg; opening of the position
    assert (t["trade_date"], t["closed_at"]) == ("2026-11-17", "2026-11-17 11:00:00")
    assert (t["hold_minutes"], t["overnight"], t["opened_at"]) == (
        660.0,
        True,
        "2026-11-16 09:00:00",
    )


def test_group_completed_charges_unavailable_when_any_leg_is():
    legs = [_leg(1, 1, 1, 10, 11, 1.0, 17, 9), _leg(2, 1, 1, 10, 12, 2.0, 17, 10, charges=None)]
    (t,) = an.group_completed(legs, [_pos(1, 1)])
    assert (t["charges"], t["charges_status"], t["net_pnl"]) == (None, "unavailable", None)


def test_money_follows_legs_and_counts_follow_completed_trades():
    legs = [
        _leg(1, 1, 20, 100, 110, 200.0, 16, 10),  # partial exit, day 16
        _leg(2, 1, 25, 100, 90, -250.0, 17, 11, overnight=True),  # final close, day 17
        _leg(3, 1, 5, 50, 55, 25.0, 17, 12, symbol="ITC"),  # ITC still open: a leg only
    ]
    done = an.group_completed(legs, [_pos(9, 1), _pos(10, 1, status="OPEN", symbol="ITC")])
    daily = an.period_breakdown(done, "daily", legs)
    assert [(r["period"], r["trades"], r["losses"], r["realized_gross"]) for r in daily] == [
        ("2026-11-16", 0, 0, 200.0),
        ("2026-11-17", 1, 1, -225.0),
    ]
    sym = {r["key"]: r for r in an.group_breakdown(done, "symbol", legs)}
    assert (sym["SBIN"]["trades"], sym["SBIN"]["realized_gross"]) == (1, -50.0)
    assert (sym["ITC"]["trades"], sym["ITC"]["realized_gross"]) == (0, 25.0)
    # net basis: the position lost 50.0 gross and paid 2.0 charges
    assert sym["SBIN"]["win_rate"] == 0.0 and sym["SBIN"]["avg_loss"] == 52.0
    hold = an.holding_breakdown(done, legs)
    # money by each leg's own flag; the completed trade is overnight (its final close)
    assert (hold["overnight"]["trades"], hold["overnight"]["realized_gross"]) == (1, -250.0)
    assert (hold["intraday"]["trades"], hold["intraday"]["realized_gross"]) == (0, 225.0)
    assert an.hold_summary(done)["overall"]["count"] == 1


# ================================================================ Phase 2A pure formulas
# Independent references below use plain loops over ``statistics`` / ``math``; nothing is
# shared with the module under test.

import math  # noqa: E402
from datetime import datetime  # noqa: E402


def pos(
    i,
    day,
    closed_hm,
    pnl,
    charges,
    *,
    entry=100.0,
    exit_=None,
    direction="LONG",
    qty=10,
    symbol="SBIN",
    exchange="NSE",
    hold=30.0,
):
    """A completed-position row as ``group_completed`` produces it (net_pnl explicit)."""
    if exit_ is None:
        exit_ = entry + pnl / qty if direction == "LONG" else entry - pnl / qty
    return {
        "id": i,
        "trade_date": day,
        "closed_at": f"{day} {closed_hm}:00",
        "symbol": symbol,
        "exchange": exchange,
        "direction": direction,
        "closed_quantity": qty,
        "entry_price": entry,
        "exit_price": exit_,
        "realized_pnl": pnl,
        "charges": charges,
        "net_pnl": None if charges is None else pnl - charges,
        "hold_minutes": hold,
    }


def ref_equity_stats(rows):
    """Reference drawdown / streaks over (closed_at, id) with explicit loops."""
    seq = sorted(rows, key=lambda r: (r["closed_at"], r["id"]))
    equity = peak = worst = 0.0
    cur = None
    cur_n = best_w = best_l = 0
    for r in seq:
        v = r["net_pnl"]
        if v is None:
            continue
        equity += v
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
        kind = "win" if v > 0 else "loss" if v < 0 else None
        if kind is None:
            cur, cur_n = None, 0
        elif kind == cur:
            cur_n += 1
        else:
            cur, cur_n = kind, 1
        if cur == "win":
            best_w = max(best_w, cur_n)
        if cur == "loss":
            best_l = max(best_l, cur_n)
    return {
        "max_dd": worst,
        "cur_dd": peak - equity,
        "peak": peak,
        "latest": equity,
        "best_w": best_w,
        "best_l": best_l,
        "cur": cur,
        "cur_n": cur_n,
    }


@pytest.mark.parametrize("seed", [101, 102, 103, 104, 105])
def test_risk_block_matches_reference(seed):
    rng = random.Random(seed)
    rows = []
    for i in range(1, 41):
        day = date(2026, 10, 1) + timedelta(days=rng.randrange(40))
        pnl_ = rng.randrange(-5000, 5001) / 100.0
        charges = None if rng.random() < 0.1 else rng.randrange(0, 300) / 100.0
        hm = f"{rng.randrange(9, 15):02d}:{rng.randrange(60):02d}"
        rows.append(pos(i, day.isoformat(), hm, pnl_, charges))
    out = an.risk_block(rows)
    ref = ref_equity_stats(rows)
    assert out["max_drawdown"] == pytest.approx(ref["max_dd"], abs=0.01)
    assert out["current_drawdown"] == pytest.approx(ref["cur_dd"], abs=0.01)
    assert out["peak_equity"] == pytest.approx(ref["peak"], abs=0.01)
    assert out["latest_equity"] == pytest.approx(ref["latest"], abs=0.01)
    assert (out["max_consecutive_wins"], out["max_consecutive_losses"]) == (
        ref["best_w"],
        ref["best_l"],
    )
    assert out["current_streak"] == {"type": ref["cur"], "length": ref["cur_n"]}
    assert out["net_unavailable_positions"] == sum(1 for r in rows if r["net_pnl"] is None)
    assert out["max_drawdown_pct"] is None
    assert out["percent_unavailable_reason"] == "no capital basis"


def test_drawdown_known_sequence_and_new_high_is_zero():
    rows = [
        pos(1, "2026-11-02", "10:00", 100.0, 0.0),
        pos(2, "2026-11-02", "11:00", -30.0, 0.0),
        pos(3, "2026-11-03", "10:00", -50.0, 0.0),
        pos(4, "2026-11-04", "10:00", 20.0, 0.0),
    ]
    out = an.drawdown(rows)
    assert (out["max_drawdown"], out["current_drawdown"]) == (80.0, 60.0)
    assert (out["peak_equity"], out["latest_equity"]) == (100.0, 40.0)
    rows.append(pos(5, "2026-11-05", "10:00", 70.0, 0.0))  # 110: a new high
    out = an.drawdown(rows)
    assert (out["current_drawdown"], out["max_drawdown"]) == (0.0, 80.0)


def test_drawdown_first_trade_loss_counts_and_charges_make_it_net():
    out = an.drawdown([pos(1, "2026-11-02", "10:00", -10.0, 2.0)])
    assert out["max_drawdown"] == 12.0 and out["current_drawdown"] == 12.0
    # a gross winner eaten by charges is a net drawdown
    assert an.drawdown([pos(1, "2026-11-02", "10:00", 1.0, 3.0)])["max_drawdown"] == 2.0


def test_drawdown_orders_by_close_time_not_input_order():
    a = pos(1, "2026-11-02", "10:00", -50.0, 0.0)
    b = pos(2, "2026-11-02", "09:30", 100.0, 0.0)  # closed first
    assert an.drawdown([a, b])["max_drawdown"] == 50.0
    assert an.drawdown([b, a])["max_drawdown"] == 50.0


def test_drawdown_empty_single_and_no_net():
    assert an.drawdown([])["max_drawdown"] == 0.0
    assert an.drawdown([pos(1, "2026-11-02", "10:00", 5.0, 0.0)])["current_drawdown"] == 0.0
    out = an.drawdown([pos(1, "2026-11-02", "10:00", -5.0, None)])
    assert out["positions"] == 0 and out["net_unavailable_positions"] == 1
    assert out["max_drawdown"] == 0.0


def test_current_streak_all_wins_all_losses_breakeven_and_skip():
    wins = [pos(i, "2026-11-02", f"10:{i:02d}", 10.0, 1.0) for i in range(1, 4)]
    assert an.current_streak(wins) == {"type": "win", "length": 3}
    losses = [pos(i, "2026-11-02", f"10:{i:02d}", -10.0, 1.0) for i in range(1, 3)]
    assert an.current_streak(losses) == {"type": "loss", "length": 2}
    even = pos(9, "2026-11-02", "11:00", 1.0, 1.0)  # net exactly 0 ends the streak
    assert an.current_streak([*wins, even]) == {"type": None, "length": 0}
    unknown = pos(9, "2026-11-02", "11:00", 99.0, None)  # no net: skipped
    assert an.current_streak([*wins, unknown]) == {"type": "win", "length": 3}
    assert an.current_streak([]) == {"type": None, "length": 0}


def test_nan_and_inf_are_rejected_at_entry():
    bad = pos(1, "2026-11-02", "10:00", 1.0, 0.0)
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            an.net_value({**bad, "net_pnl": value})
        with pytest.raises(ValueError):
            an.position_points(100.0, value, "LONG")
        with pytest.raises(ValueError):
            an.position_points(value, 100.0, "SHORT")
    with pytest.raises(ValueError):
        an.group_breakdown([{**bad, "net_pnl": float("nan")}], "symbol")


# ---------------------------------------------------------------- points per instrument


def test_position_points_long_short_and_equals_gross_over_quantity():
    assert an.position_points(100.0, 104.5, "LONG") == pytest.approx(4.5)
    assert an.position_points(100.0, 104.5, "SHORT") == pytest.approx(-4.5)
    assert an.position_points(100.0, 97.0, "SHORT") == pytest.approx(3.0)
    legs = [_leg(1, 1, 20, 100, 110, 200.0, 16, 10), _leg(2, 1, 25, 100, 90, -250.0, 17, 11)]
    (done,) = an.group_completed(legs, [_pos(9, 1)])
    assert done["points_per_unit"] == pytest.approx(done["realized_pnl"] / done["closed_quantity"])


def ref_points(rows):
    """Reference per-instrument points with explicit loops."""
    out = {}
    for r in rows:
        key = (r["symbol"], r["exchange"])
        pts = (r["exit_price"] - r["entry_price"]) * (1 if r["direction"] == "LONG" else -1)
        out.setdefault(key, []).append((pts, r["net_pnl"]))
    res = {}
    for key, items in out.items():
        pts = [p for p, _ in items]
        wins = [p for p, n in items if n is not None and n > 0]
        losses = [p for p, n in items if n is not None and n < 0]
        res[key] = {
            "gained": sum(p for p in pts if p > 0),
            "lost": sum(-p for p in pts if p < 0),
            "net": sum(pts),
            "avg": sum(pts) / len(pts),
            "avg_win_points": sum(wins) / len(wins) if wins else None,
            "avg_loss_points": sum(losses) / len(losses) if losses else None,
            "largest_win_points": max(wins) if wins else None,
            "largest_loss_points": min(losses) if losses else None,
        }
    return res


@pytest.mark.parametrize("seed", [7, 8, 9])
def test_points_by_instrument_matches_reference_and_never_mixes_instruments(seed):
    rng = random.Random(seed)
    rows = []
    for i in range(1, 31):
        entry = rng.choice((50.0, 400.0, 2500.0))
        exit_ = entry + rng.randrange(-300, 301) / 100.0
        direction = rng.choice(("LONG", "SHORT"))
        symbol = rng.choice(("SBIN", "TCS", "ITC"))
        exchange = rng.choice(("NSE", "BSE")) if symbol == "ITC" else "NSE"
        pnl_ = (exit_ - entry) * 10 * (1 if direction == "LONG" else -1)
        charges = rng.choice((None, 1.0, 5.0))
        rows.append(
            pos(
                i,
                "2026-11-02",
                "10:00",
                pnl_,
                charges,
                entry=entry,
                exit_=exit_,
                direction=direction,
                symbol=symbol,
                exchange=exchange,
            )
        )
    result = an.points_by_instrument(rows)
    got = {(r["symbol"], r["exchange"]): r for r in result}
    ref = ref_points(rows)
    assert set(got) == set(ref)  # ITC on NSE and on BSE are different instruments
    for key, r in got.items():
        e = ref[key]
        assert r["gross_points_gained"] == pytest.approx(e["gained"], abs=1e-3)
        assert r["gross_points_lost"] == pytest.approx(e["lost"], abs=1e-3)
        assert r["net_points"] == pytest.approx(e["net"], abs=1e-3)
        assert r["avg_points_per_position"] == pytest.approx(e["avg"], abs=1e-3)
        for field in (
            "avg_win_points",
            "avg_loss_points",
            "largest_win_points",
            "largest_loss_points",
        ):
            assert (r[field] is None) == (e[field] is None)
            if e[field] is not None:
                assert r[field] == pytest.approx(e[field], abs=1e-3)
        gained_minus_lost = r["gross_points_gained"] - r["gross_points_lost"]
        assert r["net_points"] == pytest.approx(gained_minus_lost, abs=1e-3)
    assert not any("total" in k for k in result[0])  # never a combined cross-instrument sum


def test_points_winner_is_net_positive_not_gross_positive():
    # gross +0.5 point but charges make it a net loser: the points count in
    # gross_points_gained, the position is a LOSER for avg_loss_points
    row = pos(1, "2026-11-02", "10:00", 5.0, 9.0, entry=100.0, exit_=100.5, qty=10)
    (r,) = an.points_by_instrument([row])
    assert r["gross_points_gained"] == 0.5 and r["gross_points_lost"] == 0.0
    assert (r["winners"], r["losers"]) == (0, 1)
    assert r["avg_loss_points"] == 0.5 and r["avg_win_points"] is None


def test_points_single_zero_all_wins_all_losses():
    (r,) = an.points_by_instrument([pos(1, "2026-11-02", "10:00", 0.0, 0.0)])
    assert (r["net_points"], r["winners"], r["losers"], r["avg_win_points"]) == (0.0, 0, 0, None)
    wins = [pos(i, "2026-11-02", "10:00", 10.0, 1.0) for i in (1, 2)]
    (r,) = an.points_by_instrument(wins)
    assert r["gross_points_lost"] == 0.0 and r["largest_loss_points"] is None
    assert r["largest_win_points"] == 1.0
    losses = [pos(i, "2026-11-02", "10:00", -10.0, 1.0) for i in (1, 2)]
    (r,) = an.points_by_instrument(losses)
    assert r["gross_points_gained"] == 0.0 and r["largest_loss_points"] == -1.0


# ---------------------------------------------------------------- trade management, costs, tables


def test_trade_management_median_and_hold_by_winner_loser():
    rows = [
        pos(1, "2026-11-02", "10:00", 30.0, 1.0, hold=10.0),  # net 29 winner
        pos(2, "2026-11-02", "10:10", 50.0, 1.0, hold=30.0),  # net 49 winner
        pos(3, "2026-11-02", "10:20", -20.0, 1.0, hold=60.0),  # net -21 loser
        pos(4, "2026-11-02", "10:30", 1.0, 1.0, hold=99.0),  # net 0 break-even: excluded
        pos(5, "2026-11-02", "10:40", 9.0, None, hold=5.0),  # no net: excluded
    ]
    out = an.trade_management(rows)
    assert out["median_net_pnl"] == statistics.median([29.0, 49.0, -21.0, 0.0])
    assert out["winners"] == {"count": 2, "mean_minutes": 20.0, "median_minutes": 20.0}
    assert out["losers"]["count"] == 1 and out["losers"]["mean_minutes"] == 60.0
    assert out["break_even_excluded"] == 1 and out["net_unavailable_positions"] == 1


def test_costs_charges_pct_of_gross_profit_and_no_profit_is_none_not_inf():
    rows = [
        pos(1, "2026-11-02", "10:00", 100.0, 5.0),
        pos(2, "2026-11-02", "10:10", 50.0, 5.0),
        pos(3, "2026-11-02", "10:20", -80.0, 10.0),
        pos(4, "2026-11-02", "10:30", 20.0, None),
    ]
    out = an.costs_summary(rows)
    # charges 20 / gross profit (100 + 50 + 20) = 11.76 %; the no-estimate row has no charges
    assert out["total_charges"] == 20.0 and out["gross_profit"] == 170.0
    assert out["charges_pct_of_gross_profit"] == pytest.approx(20 / 170 * 100, abs=0.01)
    assert out["charges_unavailable_positions"] == 1 and out["reason"] is None
    for losers in (
        [],
        [pos(1, "2026-11-02", "10:00", -5.0, 1.0)],
        [pos(1, "2026-11-02", "10:00", 0.0, 1.0)],
    ):
        out = an.costs_summary(losers)
        assert out["charges_pct_of_gross_profit"] is None and out["reason"] == "no gross profit"
        assert not any(isinstance(v, float) and not math.isfinite(v) for v in out.values())


def test_monthly_and_weekday_tables_by_close_date_with_net_win_rate():
    rows = [
        pos(1, "2026-10-30", "10:00", 100.0, 1.0),  # Fri, Oct
        pos(2, "2026-11-02", "10:00", -40.0, 1.0),  # Mon, Nov
        pos(3, "2026-11-02", "11:00", 10.0, 1.0),  # Mon, Nov
        pos(4, "2026-11-03", "11:00", 8.0, None),  # Tue, Nov, no net
        pos(5, "2026-11-07", "11:00", 5.0, 0.0),  # Sat (special session)
    ]
    months = {r["period"]: r for r in an.monthly_table(rows)}
    assert list(months) == ["2026-10", "2026-11"]
    nov = months["2026-11"]
    assert (nov["positions"], nov["wins"], nov["losses"]) == (4, 2, 1)
    assert nov["win_rate"] == pytest.approx(2 / 3 * 100, abs=0.01)  # of the 3 with a net
    assert nov["realized_gross"] == -17.0 and nov["charges"] == 2.0
    assert nov["net_of_charges"] == pytest.approx(-40 - 1 + 10 - 1 + 5.0)
    assert (nov["charges_unavailable_positions"], nov["net_unavailable_positions"]) == (1, 1)
    days = {r["weekday"]: r for r in an.weekday_table(rows)}
    assert list(days)[:5] == ["Mon", "Tue", "Wed", "Thu", "Fri"] and "Sat" in days
    assert days["Mon"]["positions"] == 2 and days["Wed"]["positions"] == 0
    assert days["Wed"]["win_rate"] is None and days["Fri"]["net_of_charges"] == 99.0
    assert "Sun" not in days
    # weekday is the CLOSE date (trade_date), not the opening date
    assert an.weekday_table([pos(1, "2026-11-06", "10:00", 1.0, 0.0)])[4]["positions"] == 1


# ---------------------------------------------------------------- position reliability fields


def test_group_completed_carries_opening_unknown_orphans_and_points():
    legs = [_leg(1, 1, 10, 100, 110, 100.0, 16, 10)]
    flat = _pos(9, 1)
    flat["opening_unknown"] = True
    ident = {"broker": "flattrade", "account": "A", "exchange": "NSE", "product": "MIS"}
    orphans = [
        {**ident, "symbol": "SBIN", "fill_time": datetime(2026, 11, 16, 9, 30)},
        {**ident, "symbol": "SBIN", "fill_time": datetime(2026, 11, 20, 9, 30)},  # after close
        {**ident, "symbol": "TCS", "fill_time": datetime(2026, 11, 16, 9, 30)},  # other symbol
    ]
    (t,) = an.group_completed(legs, [flat], orphans)
    assert t["opening_unknown"] is True
    assert (t["orphan_fills"], t["has_orphan_fills"]) == (1, True)
    assert t["points_per_unit"] == pytest.approx(10.0)
    (clean,) = an.group_completed(legs, [_pos(9, 1)])
    assert clean["opening_unknown"] is False
    assert (clean["orphan_fills"], clean["has_orphan_fills"]) == (0, False)


# ---------------------------------------------------------------- classification


def _dt(day, hh, mm=0, ss=0):
    return datetime(2026, 11, day, hh, mm, ss)


def _window(day, start, end, special=False):
    return an.SessionWindow(
        datetime.combine(day, datetime.min.time()).replace(hour=start[0], minute=start[1]),
        datetime.combine(day, datetime.min.time()).replace(hour=end[0], minute=end[1]),
        special,
    )


def calendar(special=(), holidays=(), failing=()):
    """NSE 09:15-15:30 and MCX 09:00-23:55 on weekdays; holidays and weekends closed; special
    days get their own window; failing days raise like a down calendar."""
    special = dict(special)

    def provider(day, exchange):
        if day in failing:
            raise an.SessionCalendarUnavailable(str(day))
        if day in special:
            return _window(day, *special[day], special=True)
        if day.weekday() >= 5 or day in holidays:
            return None
        if exchange == "MCX":
            return _window(day, (9, 0), (23, 55))
        return _window(day, (9, 15), (15, 30))

    return provider


def cp(opened, closed, **kw):
    return {"exchange": "NSE", "opened_at": opened, "closed_at": closed, **kw}


def test_classification_intraday_overnight_and_partial_then_overnight():
    cal = calendar()
    assert an.classify_with_reason(cp(_dt(16, 9, 30), _dt(16, 14, 0)), cal) == ("intraday", None)
    assert an.classify_with_reason(cp(_dt(16, 14, 0), _dt(17, 10, 0)), cal) == ("swing", None)
    # a partial exit on day 1 does not matter: the position FINALLY closed on day 2
    assert an.classify_position(cp(_dt(16, 10), _dt(17, 9, 20), legs=2), cal) == "swing"
    for product in ("MIS", "CNC", "NRML"):  # the product is never used
        same = cp(_dt(16, 9, 30), _dt(16, 10), product=product)
        later = cp(_dt(16, 9, 30), _dt(17, 10), product=product)
        assert an.classify_position(same, cal) == "intraday"
        assert an.classify_position(later, cal) == "swing"


def test_classification_session_boundaries():
    cal = calendar()
    outside = ("unclassified", "fill_outside_session")
    assert an.classify_with_reason(cp(_dt(16, 9, 14, 59), _dt(16, 10)), cal) == outside
    assert an.classify_position(cp(_dt(16, 9, 15, 0), _dt(16, 15, 29, 59)), cal) == "intraday"
    # closed at / after the close on the same day: in no session window, never guessed
    assert an.classify_with_reason(cp(_dt(16, 9, 30), _dt(16, 15, 30, 0)), cal) == outside
    assert an.classify_with_reason(cp(_dt(16, 9, 30), _dt(16, 15, 31)), cal) == outside
    # the next session's first second is a valid swing close, one second earlier is not
    assert an.classify_position(cp(_dt(16, 15, 29, 59), _dt(17, 9, 15, 0)), cal) == "swing"
    assert an.classify_with_reason(cp(_dt(16, 15, 29), _dt(17, 9, 14, 59)), cal) == outside


def test_classification_holidays_weekends_and_gaps():
    cal = calendar(holidays=(date(2026, 11, 26),))
    outside = ("unclassified", "fill_outside_session")
    # Wed 25 -> Fri 27 over the Thursday holiday: swing
    assert an.classify_position(cp(_dt(25, 10), _dt(27, 10)), cal) == "swing"
    # opened on the holiday / on a Saturday: no session
    assert an.classify_with_reason(cp(_dt(26, 10), _dt(27, 10)), cal) == outside
    assert an.classify_with_reason(cp(_dt(14, 10), _dt(16, 10)), cal) == outside
    # Friday -> Monday over the weekend
    assert an.classify_position(cp(_dt(13, 10), _dt(16, 10)), cal) == "swing"


def test_classification_mcx_evening_session():
    cal = calendar()
    mcx = {"exchange": "MCX"}
    late = {**mcx, "opened_at": _dt(16, 20), "closed_at": _dt(16, 23, 50)}
    assert an.classify_with_reason(late, cal) == ("intraday", None)
    assert an.classify_position({**late, "closed_at": _dt(17, 10)}, cal) == "swing"
    assert an.classify_position({**late, "closed_at": _dt(16, 23, 55)}, cal) == "unclassified"
    # the NSE window would call the same evening times outside the session
    assert an.classify_position(cp(_dt(16, 20), _dt(16, 21)), cal) == "unclassified"


def test_classification_special_session_and_invalid_times():
    cal = calendar(special={date(2026, 11, 8): ((18, 15), (19, 15))})
    got = an.classify_with_reason(cp(_dt(8, 18, 30), _dt(8, 19, 0)), cal)
    assert got == ("unclassified", "special_session")
    # opened in a normal session, closed in the next (special) one: still a swing
    assert an.classify_position(cp(_dt(6, 10), _dt(8, 18, 30)), cal) == "swing"
    assert an.classify_with_reason(cp(_dt(16, 11), _dt(16, 10)), cal) == (
        "unclassified",
        "invalid_times",
    )
    assert an.classify_with_reason(cp(None, _dt(16, 10)), cal) == ("unclassified", "invalid_times")


def test_classification_unreliable_positions_are_never_guessed():
    cal = calendar()
    base = (_dt(16, 9, 30), _dt(16, 14, 0))
    assert an.classify_with_reason(cp(*base, opening_unknown=True), cal) == (
        "unclassified",
        "opening_unknown",
    )
    assert an.classify_with_reason(cp(*base, has_orphan_fills=True), cal) == (
        "unclassified",
        "orphan_fills",
    )
    assert an.classify_with_reason(cp(*base, orphan_fills=2), cal) == (
        "unclassified",
        "orphan_fills",
    )
    assert an.classify_with_reason(cp(*base), cal, reliable_since=date(2026, 11, 17)) == (
        "unclassified",
        "before_reliable_since",
    )
    assert an.classify_position(cp(*base), cal, reliable_since=date(2026, 11, 16)) == "intraday"


def test_classification_missing_calendar_uses_the_boundary_only_when_unambiguous():
    down = calendar(failing=(date(2026, 11, 16), date(2026, 11, 17)))
    # later session date => open across a session end: swing, reason recorded
    assert an.classify_with_reason(cp(_dt(16, 14), _dt(17, 10)), down) == (
        "swing",
        "fallback_boundary",
    )
    # the same session date would need the real session end: unclassified
    assert an.classify_with_reason(cp(_dt(16, 9, 30), _dt(16, 14)), down) == (
        "unclassified",
        "fallback_boundary",
    )
    # 00:30 on the 17th still belongs to the 16th session (03:00 boundary)
    assert an.classify_with_reason(cp(_dt(16, 14), _dt(17, 0, 30)), down) == (
        "unclassified",
        "fallback_boundary",
    )
    assert an.classify_position(cp(_dt(16, 14), _dt(17, 3, 0)), down) == "swing"


def test_classification_open_positions_use_now():
    cal = calendar()
    open_pos = {"exchange": "NSE", "opened_at": _dt(16, 10), "closed_at": None}
    still = ("unclassified", "still_open_in_session")
    assert an.classify_with_reason(open_pos, cal, now=_dt(16, 11)) == still
    assert an.classify_with_reason(open_pos, cal) == still
    assert an.classify_position(open_pos, cal, now=_dt(16, 15, 30)) == "swing"
    assert an.classify_position(open_pos, cal, now=_dt(17, 9, 30)) == "swing"


def test_category_counts_report_unclassified_reasons():
    counts = an.category_counts(
        [
            ("intraday", None),
            ("swing", None),
            ("unclassified", "special_session"),
            ("unclassified", "special_session"),
            ("unclassified", "opening_unknown"),
        ]
    )
    assert counts == {
        "intraday": 1,
        "swing": 1,
        "unclassified": 3,
        "unclassified_reasons": {"opening_unknown": 1, "special_session": 2},
    }
