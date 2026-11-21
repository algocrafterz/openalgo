"""Phase 2C review fixes of the Discretionary Performance backend:

* Historify read errors are not reported as missing data; one connection per computation;
* float noise: a net of 5.55e-17 is break-even everywhere;
* an empty / all-net-unavailable set has no ratios (None), not 0;
* a failing classification / unresolved block degrades the summary instead of failing it.

Run with: pytest -p no:cacheprovider test/test_discretionary_review_fixes.py -q
"""
# ruff: noqa: F811  (fixtures are imported from the neighbouring test modules)

from datetime import datetime

import pandas as pd
import pytest
from test_discretionary_excursions import (  # noqa: F401
    Reader,
    _fresh_excursion_cache,
    long_win,
    nse_open,
)
from test_discretionary_pnl_service import _clock_only, d, env  # noqa: F401

from services import discretionary_analytics as an
from services import discretionary_pnl_service as pnl

NOISE = 0.1 + 0.2 - 0.3  # 5.55e-17
assert 0 < NOISE < 1e-15


def noisy(i, day="2026-11-16", **kw):
    """A position whose net is float noise (realized 0.1 + 0.2, charges 0.3)."""
    row = {
        "id": i,
        "trade_date": day,
        "closed_at": f"{day} 10:{i:02d}:00",
        "opened_at": f"{day} 09:{i:02d}:00",
        "symbol": "SBIN",
        "exchange": "NSE",
        "direction": "LONG",
        "product": "MIS",
        "overnight": False,
        "hold_minutes": 10.0 + i,
        "entry_price": 100.0,
        "exit_price": 100.0,
        "closed_quantity": 1,
        "broker": "flattrade",
        "account": "A",
        "charges_status": "ok",
        "realized_pnl": 0.1 + 0.2,
        "charges": 0.3,
    }
    row.update(kw)
    return row


def win(i, pnl_value=100.0, **kw):
    return noisy(i, realized_pnl=pnl_value, charges=0.0, **kw)


def lose(i, pnl_value=-50.0, **kw):
    return noisy(i, realized_pnl=pnl_value, charges=0.0, **kw)


# ---------------------------------------------------------------- 2: float noise


def test_clean_money_guard():
    assert an.clean_money(NOISE) == 0.0 and an.clean_money(-NOISE) == 0.0
    assert an.clean_money(0.004) == 0.0 and an.clean_money(-0.004) == 0.0
    assert an.clean_money(0.01) == 0.01 and an.clean_money(-12.5) == -12.5
    assert an.clean_money(None) is None


def test_group_completed_net_of_noise_is_exactly_zero():
    leg = {
        "id": 1,
        "position_seq": 1,
        "broker": "flattrade",
        "account": "A",
        "exchange": "NSE",
        "symbol": "SBIN",
        "product": "MIS",
        "direction": "LONG",
        "closed_quantity": 1,
        "entry_price": 100.0,
        "exit_price": 100.0,
        "opened_at": "2026-11-16 09:00:00",
        "closed_at": "2026-11-16 10:00:00",
        "trade_date": "2026-11-16",
        "overnight": False,
        "hold_minutes": 60.0,
        "realized_pnl": 0.1 + 0.2,
        "charges": 0.3,
        "charges_status": "ok",
        "charges_breakdown": {},
    }
    pos = {
        "id": 9,
        "build_id": "b",
        "broker": "flattrade",
        "account": "A",
        "exchange": "NSE",
        "symbol": "SBIN",
        "product": "MIS",
        "position_seq": 1,
        "direction": "LONG",
        "opened_at": "2026-11-16 09:00:00",
        "status": "FLAT",
    }
    assert leg["realized_pnl"] - leg["charges"] != 0  # the noise is real
    (t,) = an.group_completed([leg], [pos])
    assert t["net_pnl"] == 0.0


def test_noise_net_is_break_even_in_every_statistic():
    trades = [win(1), noisy(2), win(3), noisy(4), lose(5), noisy(6, hold_minutes=999.0)]
    # break-even trades are neither wins nor losses; the noise ones also END a streak
    s = an.summary_stats(trades)
    assert (s["wins"], s["losses"], s["net_basis_trades"]) == (2, 1, 6)
    assert s["win_rate"] == pytest.approx(2 / 6 * 100, abs=0.01)
    assert s["profit_factor"] == pytest.approx(4.0)  # 200 / 50 (the noise nets add nothing)
    assert an.trade_streaks(trades) == (1, 1)  # W, BE, W, BE, L, BE: never 2 in a row
    assert an.current_streak(trades) == {"type": None, "length": 0}

    dd = an.drawdown(trades)
    # equity 100, 100, 200, 200, 150, 150: the only drawdown is the 50 loss
    assert dd["max_drawdown"] == 50.0 and dd["latest_equity"] == 150.0

    tm = an.trade_management(trades)
    assert tm["winners"]["count"] == 2 and tm["losers"]["count"] == 1
    assert tm["break_even_excluded"] == 3
    assert tm["winners"]["mean_minutes"] == pytest.approx(12.0)  # 11 and 13
    assert tm["losers"]["mean_minutes"] == pytest.approx(15.0)

    hold = an.hold_summary(trades)
    assert hold["winners"]["count"] == 2 and hold["losers"]["count"] == 1

    (month,) = an.monthly_table(trades)
    assert (month["wins"], month["losses"]) == (2, 1)
    assert month["win_rate"] == pytest.approx(2 / 6 * 100, abs=0.01)
    weekday = {r["weekday"]: r for r in an.weekday_table(trades)}
    assert (weekday["Mon"]["wins"], weekday["Mon"]["losses"]) == (2, 1)

    daily = an.period_breakdown(trades, "daily")
    assert (daily[0]["wins"], daily[0]["losses"]) == (2, 1)

    cons = an.consistency(trades, an.period_breakdown(trades, "daily"), hold)
    assert cons["largest_win"]["id"] == 1 and cons["largest_loss"]["id"] == 5

    pts = an.points_by_instrument(trades)[0]
    assert (pts["winners"], pts["losers"]) == (2, 1)

    only_noise = [noisy(1), noisy(2)]
    assert an.summary_stats(only_noise)["wins"] == 0
    assert an.summary_stats(only_noise)["losses"] == 0
    assert an.summary_stats(only_noise)["win_rate"] == 0.0  # has nets: a real 0 %
    assert an.trade_streaks(only_noise) == (0, 0)


def test_noise_net_is_not_a_winner_for_profit_capture_or_excursion_groups():
    assert an.profit_capture(NOISE, 0.0, 5.0) is None  # break-even is not a winner
    assert an.profit_capture(-NOISE, 1.0, 5.0) is None
    assert an.profit_capture(0.01, 1.0, 5.0) == pytest.approx(0.2)  # a real paise wins
    rows = [
        {"status": "available", "net_pnl": NOISE, "mae_points": 1, "mfe_points": 2}
        | {"mae_amount": 1, "mfe_amount": 2, "profit_capture": None},
        {"status": "available", "net_pnl": -NOISE, "mae_points": 1, "mfe_points": 2}
        | {"mae_amount": 1, "mfe_amount": 2, "profit_capture": None},
        {"status": "available", "net_pnl": 5.0, "mae_points": 3, "mfe_points": 4}
        | {"mae_amount": 3, "mfe_amount": 4, "profit_capture": 0.5},
    ]
    agg = an.excursion_aggregates(rows)
    assert agg["winners"]["mae_points"]["count"] == 1  # only the +5.0 row
    assert agg["losers"]["mae_points"]["count"] == 0  # the -noise row is break-even too


# ---------------------------------------------------------------- 3: no data is not 0

RATIOS = ("win_rate", "profit_factor", "expectancy", "avg_win", "avg_loss")


def test_empty_and_all_net_unavailable_sets_have_no_ratios_but_keep_money():
    for trades in ([], [noisy(1, charges=None), noisy(2, charges=None)]):
        s = an.summary_stats(trades)
        for name in RATIOS:
            assert s[name] is None, name
        assert s["stats_unavailable_reason"]
        assert s["net_basis_trades"] == 0 and s["net_unavailable_trades"] == len(trades)
        assert s["trades"] == len(trades)
        assert s["realized_gross"] == pytest.approx(0.3 * len(trades), abs=1e-4)
        assert s["charges"] == 0.0 and s["charges_unavailable_trades"] == len(trades)


def test_a_set_with_nets_keeps_numeric_ratios_and_no_reason():
    s = an.summary_stats([win(1), lose(2)])
    assert s["win_rate"] == 50.0 and s["profit_factor"] == 2.0
    assert s["stats_unavailable_reason"] is None
    # a real zero stays a number: all losers => profit factor 0.0, win rate 0.0
    s = an.summary_stats([lose(1)])
    assert s["win_rate"] == 0.0 and s["profit_factor"] == 0.0 and s["expectancy"] == -50.0


def test_service_metrics_are_none_when_no_completed_trade_has_a_net():
    legs = [noisy(1, charges=None)]
    m = pnl.leg_and_trade_metrics(legs, legs, "2026-11-01", "2026-11-30")
    for name in (*RATIOS, "payoff_ratio"):
        assert m[name] is None, name
    assert m["count_unavailable_reason"] and m["net_basis_trades"] == 0
    assert m["trades_count"] == 1 and m["net_unavailable_trades"] == 1
    ok = pnl.leg_and_trade_metrics([win(1), lose(2)], [win(1), lose(2)], "2026-11-01", "2026-11-30")
    assert ok["win_rate"] == 50.0 and ok["count_unavailable_reason"] is None


# ---------------------------------------------------------------- 1 + 5: Historify reads


class FakeConnections:
    """Replaces ``database.historify_db.get_connection``: counts opens / closes, records the
    SQL and parameters, serves flat 1-minute bars."""

    def __init__(self, fail_open=False, fail_execute=False):
        self.opened = self.closed = 0
        self.fail_open, self.fail_execute = fail_open, fail_execute
        self.queries = []

    def __call__(self, *a, **k):
        outer = self

        class Ctx:
            def __enter__(self_inner):
                outer.opened += 1
                if outer.fail_open:
                    raise RuntimeError("cannot open")
                return Conn(outer)

            def __exit__(self_inner, *exc):
                outer.closed += 1
                return False

        return Ctx()


class Conn:
    def __init__(self, owner):
        self.owner = owner

    def execute(self, sql, params=None):
        self.owner.queries.append((sql, params))
        if self.owner.fail_execute:
            raise RuntimeError("IO Error: database is locked")
        start, end = params[2], params[3]
        rows = [{"timestamp": ts, "high": 101.0, "low": 99.0} for ts in range(start, end + 1, 60)]
        frame = pd.DataFrame(rows, columns=["timestamp", "high", "low"])

        class Result:
            def fetchdf(self_inner):
                return frame

        return Result()


def use_fake(monkeypatch, **kw):
    import database.historify_db as hdb

    fake = FakeConnections(**kw)
    monkeypatch.setattr(hdb, "get_connection", fake)
    return fake


def test_real_reader_failure_is_bar_read_failed_not_missing_bars(env, nse_open, monkeypatch):
    long_win(env)
    fake = use_fake(monkeypatch, fail_open=True)  # the REAL _read_bars path, connection fails
    body = pnl.get_excursions(period="all")
    (row,) = body["positions"]
    assert row["status"] == "unavailable" and row["reason"] == "bar_read_failed"
    assert body["aggregates"]["coverage"]["unavailable_reasons"] == {"bar_read_failed": 1}
    assert fake.opened == 1

    pnl._excursion_cache.clear()
    use_fake(monkeypatch, fail_execute=True)  # the SELECT itself raises
    (row,) = pnl.get_excursions(period="all")["positions"]
    assert row["reason"] == "bar_read_failed"


def test_failed_results_are_never_cached(env, nse_open, monkeypatch):
    long_win(env)
    use_fake(monkeypatch, fail_execute=True)
    pnl.get_excursions(period="all")
    fake = use_fake(monkeypatch)
    (row,) = pnl.get_excursions(period="all")["positions"]
    assert row["status"] == "available" and fake.opened == 1  # re-read, now fine
    again = use_fake(monkeypatch)
    pnl.get_excursions(period="all")
    assert again.opened == 0  # the available past-day result is cached: no connection at all


def test_one_connection_for_all_positions_and_it_is_closed(env, nse_open, monkeypatch):
    for day in (10, 11, 12, 13):
        long_win(env, day=day)
    fake = use_fake(monkeypatch)
    body = pnl.get_excursions(period="all")
    assert body["computed"] == 4 and all(r["status"] == "available" for r in body["positions"])
    assert fake.opened == 1 and fake.closed == 1
    assert len(fake.queries) == 4
    for sql, params in fake.queries:
        assert sql.count("?") == 4 and "SBIN" not in sql  # parameterised, never interpolated
        assert params[:2] == ["SBIN", "NSE"] and params[2] <= params[3]
        assert sql.lstrip().upper().startswith("SELECT")


def test_connection_is_closed_when_a_read_raises(env, nse_open, monkeypatch):
    for day in (10, 11):
        long_win(env, day=day)
    fake = use_fake(monkeypatch, fail_execute=True)
    body = pnl.get_excursions(period="all")
    assert {r["reason"] for r in body["positions"]} == {"bar_read_failed"}
    assert fake.opened == 1 and fake.closed == 1
    assert pnl._active_connection.get() is None  # the per-request holder is reset


def test_connection_is_closed_when_the_computation_itself_raises(env, nse_open, monkeypatch):
    long_win(env)
    fake = use_fake(monkeypatch)

    def explode(*a, **k):
        raise KeyError("unexpected")

    monkeypatch.setattr(pnl, "position_excursion_result", explode)
    with pytest.raises(KeyError):
        pnl.get_excursions(period="all")
    assert fake.opened == fake.closed == 0  # nothing was read yet: nothing to leak
    with pytest.raises(RuntimeError):
        with pnl._shared_connection() as lazy:
            lazy.get()
            raise RuntimeError("after the open")
    assert fake.opened == 1 and fake.closed == 1


def test_injected_reader_opens_no_connection(env, nse_open, monkeypatch):
    long_win(env)
    fake = use_fake(monkeypatch)
    pnl.get_excursions(period="all", reader=Reader())
    assert fake.opened == 0


# ---------------------------------------------------------------- 4b: degraded blocks


def one_trade(env):
    env.fill("BUY", 10, 100, d(16, 9, 30))
    env.fill("SELL", 10, 104, d(16, 9, 40))


def test_classification_failure_is_unclassified_never_intraday_or_swing(env, monkeypatch):
    one_trade(env)
    env.fill("BUY", 5, 50, d(17, 10, 0), symbol="ITC")  # an open position

    def boom(*a, **k):
        raise RuntimeError("calendar exploded")

    monkeypatch.setattr(pnl, "classify_positions", boom)
    out = pnl.get_performance(period="all")
    assert out["classification"]["closed"]["intraday"] == 0
    assert out["classification"]["closed"]["swing"] == 0
    assert out["classification"]["closed"]["unclassified"] == 1
    assert out["classification"]["open"]["unclassified"] == 1
    assert out["classification"]["closed"]["unclassified_reasons"] == {pnl.CLASSIFICATION_FAILED: 1}
    assert {t["category"] for t in out["trades"]} == {"unclassified"}
    assert any("classification failed" in w for w in out["warnings"])
    assert out["metrics"]["trades_count"] == 1  # totals unaffected in 'all'
    for category in ("intraday", "swing"):
        scoped = pnl.get_performance(period="all", category=category)
        assert scoped["trades"] == [] and scoped["metrics"]["trades_count"] == 0


def test_unresolved_block_failure_gives_null_block_and_warning(env, monkeypatch):
    one_trade(env)

    def boom(*a, **k):
        raise RuntimeError("ledger count failed")

    monkeypatch.setattr(pnl, "unresolved_in_range", boom)
    out = pnl.get_performance(period="all")
    assert out["unresolved_in_range"] is None
    assert any("unresolved_in_range block unavailable" in w for w in out["warnings"])
    assert out["metrics"]["trades_count"] == 1 and out["realized_gross"] == pytest.approx(40.0)
