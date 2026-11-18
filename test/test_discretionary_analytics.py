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
        b["w"] += x["realized_pnl"] > 0
        b["l"] += x["realized_pnl"] < 0
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
    assert h["intraday"]["profit_factor"] == 0.0 and h["intraday"]["avg_hold_minutes"] is None
    assert an.hold_summary([]) == {
        "overall": {"count": 0, "mean_minutes": None, "median_minutes": None},
        "winners": {"count": 0, "mean_minutes": None, "median_minutes": None},
        "losers": {"count": 0, "mean_minutes": None, "median_minutes": None},
    }


# ---------------------------------------------------------------- group_breakdown


def naive_stats(items):
    pnls = [x["realized_pnl"] for x in items]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = len(pnls)
    wr = len(wins) / n
    aw = sum(wins) / len(wins) if wins else 0.0
    al = abs(sum(losses)) / len(losses) if losses else 0.0
    gl = abs(sum(losses))
    pf = sum(wins) / gl if gl > 0 else (None if sum(wins) > 0 else 0.0)
    return {
        "trades": n,
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
    m = compute_metrics(metric_trades(trades), "2026-09-01", "2026-12-31", capital=None)
    assert row["win_rate"] == m["win_rate"]
    assert row["profit_factor"] == m["profit_factor"]
    assert row["expectancy"] == m["expectancy"]
    assert row["avg_win"] == m["avg_win"] and row["avg_loss"] == m["avg_loss"]
    assert row["realized_gross"] == pytest.approx(m["net_profit"], abs=0.005)


def test_profit_factor_edge_cases_and_breakeven():
    only_wins = an.group_breakdown([t("2026-11-16", 10.0)], "symbol")[0]
    assert only_wins["profit_factor"] is None and only_wins["avg_loss"] == 0.0
    only_loss = an.group_breakdown([t("2026-11-16", -10.0)], "symbol")[0]
    assert only_loss["profit_factor"] == 0.0 and only_loss["win_rate"] == 0.0
    flat = an.group_breakdown([t("2026-11-16", 0.0)], "symbol")[0]
    assert (flat["wins"], flat["losses"], flat["profit_factor"], flat["expectancy"]) == (
        0,
        0,
        0.0,
        0.0,
    )


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
        "winners": [x for x in trades if x["realized_pnl"] > 0],
        "losers": [x for x in trades if x["realized_pnl"] < 0],
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
        [t("2026-11-16", 0.0, hold_minutes=30.0), t("2026-11-16", 5.0, hold_minutes=None)]
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
