"""Reporting extras for the strategy performance pages: breakdowns, net equity,
excursions, last session, CSV export, strategy list.

Run with: uv run pytest test/test_strategy_report_extras.py -v
"""

import sqlite3
from datetime import date

import pandas as pd
import pytest
from flask import Flask

import blueprints.auth as auth_bp_module
import blueprints.strategy_pnl as route_mod
import services.strategy_daily_performance_service as svc
from limiter import limiter
from services.strategy_trade_enrichment import (
    add_excursions,
    hour_breakdown,
    net_equity_curve,
    weekday_breakdown,
)


def _t(**kw):
    base = {
        "strategy": "ORB",
        "symbol": "INFY",
        "exchange": "NSE",
        "product": "MIS",
        "mode": "analyze",
        "direction": "LONG",
        "closed_quantity": 10.0,
        "entry_price": 100.0,
        "exit_price": 101.0,
        "realized_pnl": 10.0,
        "trade_date": "2026-10-30",
        "closed_at": "2026-10-30T10:40:00",
    }
    base.update(kw)
    return base


def test_hour_breakdown_by_exit_hour():
    rows = hour_breakdown(
        [
            _t(realized_pnl=10, closed_at="2026-10-30T10:05:00"),
            _t(realized_pnl=-4, closed_at="2026-10-30T10:55:00"),
            _t(realized_pnl=7, closed_at="2026-10-30T09:20:00"),
            _t(realized_pnl=1, closed_at=None),
        ]
    )
    assert rows == [
        {"hour": 9, "trades": 1, "net_pnl": 7.0, "win_rate": 100.0},
        {"hour": 10, "trades": 2, "net_pnl": 6.0, "win_rate": 50.0},
    ]


def test_weekday_breakdown_mon_first_present_only():
    rows = weekday_breakdown(
        [
            _t(realized_pnl=5, trade_date="2026-10-30"),  # Friday
            _t(realized_pnl=-5, trade_date="2026-10-26"),  # Monday
            _t(realized_pnl=3, trade_date="2026-10-30"),
        ]
    )
    assert [r["weekday"] for r in rows] == ["Mon", "Fri"]
    assert rows[1] == {"weekday": "Fri", "trades": 2, "net_pnl": 8.0, "win_rate": 100.0}


def test_net_equity_curve_subtracts_costs_and_reports_coverage():
    gross = [{"date": "2026-10-29", "value": 100.0}, {"date": "2026-10-30", "value": 150.0}]
    enriched = [
        _t(trade_date="2026-10-29", cost=10.0),
        _t(trade_date="2026-10-30", cost=5.0),
        _t(trade_date="2026-10-30", cost=None),
    ]
    curve, cov = net_equity_curve(gross, enriched)
    assert curve == [
        {"date": "2026-10-29", "value": 90.0},
        {"date": "2026-10-30", "value": 135.0},
    ]
    assert cov == {"covered": 2, "total": 3}


@pytest.fixture
def signals_db(tmp_path):
    path = tmp_path / "trades.db"
    con = sqlite3.connect(path)
    con.execute(
        "create table trades (id integer primary key, strategy text, direction text, "
        "symbol text, entry real, sl real, status text, trade_mode text, executed_at text)"
    )
    con.execute(
        "insert into trades (strategy,direction,symbol,entry,sl,status,trade_mode,executed_at) "
        "values ('ORB','LONG','INFY',100,98,'SUCCESS','analyze','2026-10-30 10:00:30')"
    )
    con.commit()
    con.close()
    return path


def _bars(rows):
    return pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"])


def test_excursion_long_uses_entry_minute_through_exit(signals_db):
    seen = {}

    def reader(symbol, exchange, start_ts, end_ts):
        seen["args"] = (symbol, exchange, start_ts, end_ts)
        return _bars([(start_ts, 100, 103, 99, 101), (start_ts + 60, 101, 105, 98, 104)])

    out, covered, total = add_excursions(
        [_t()], "2026-10-30", "2026-10-30", db_path=signals_db, bar_reader=reader
    )
    assert out[0]["mfe"] == 50.0 and out[0]["mae"] == 20.0
    assert (covered, total) == (1, 1)
    # entry minute 10:00 IST = 04:30 UTC; exit 10:40 IST
    assert seen["args"][2] % 60 == 0
    # 10:00 IST on 2026-10-30 is 04:30 UTC
    assert seen["args"][2] == int(pd.Timestamp("2026-10-30 04:30", tz="UTC").timestamp())
    assert seen["args"][3] - seen["args"][2] == 40 * 60


def test_excursion_short_is_mirrored_and_floored(signals_db):
    sigcon = sqlite3.connect(signals_db)
    sigcon.execute("update trades set direction='SHORT'")
    sigcon.commit()
    sigcon.close()

    def reader(symbol, exchange, s, e):
        return _bars([(s, 100, 102, 100.5, 101)])

    out, _, _ = add_excursions(
        [_t(direction="SHORT")], "2026-10-30", "2026-10-30", db_path=signals_db, bar_reader=reader
    )
    assert out[0]["mfe"] == 0.0  # entry 100 - low 100.5 floored
    assert out[0]["mae"] == 20.0


def test_excursion_none_without_match_bars_or_on_error(signals_db):
    good = lambda *a: _bars([(a[2], 100, 101, 99, 100)])  # noqa: E731
    no_match = _t(symbol="TCS")
    out, covered, total = add_excursions(
        [no_match], "2026-10-30", "2026-10-30", db_path=signals_db, bar_reader=good
    )
    assert out[0]["mfe"] is None and covered == 0 and total == 1

    out, covered, _ = add_excursions(
        [_t()], "2026-10-30", "2026-10-30", db_path=signals_db, bar_reader=lambda *a: _bars([])
    )
    assert out[0]["mae"] is None and covered == 0

    def boom(*a):
        raise RuntimeError("db down")

    out, covered, _ = add_excursions(
        [_t(), _t()], "2026-10-30", "2026-10-30", db_path=signals_db, bar_reader=boom
    )
    assert [o["mfe"] for o in out] == [None, None] and covered == 0


def test_excursion_missing_signal_db(tmp_path):
    out, covered, total = add_excursions(
        [_t()], "2026-10-30", "2026-10-30", db_path=tmp_path / "none.db", bar_reader=lambda *a: None
    )
    assert out[0]["mfe"] is None and (covered, total) == (0, 1)


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    trades = [
        _t(realized_pnl=100.0, trade_date="2026-10-29", closed_at="2026-10-29T10:00:00"),
        _t(strategy="EMA", realized_pnl=-30.0),
        _t(strategy="ORB", realized_pnl=20.0, symbol="=EVIL"),
    ]
    monkeypatch.setattr(svc, "get_closed_trades", lambda **kw: list(trades))
    monkeypatch.setattr(svc, "_current_mode", lambda: "analyze")
    monkeypatch.setattr(svc, "_today", lambda: date(2026, 11, 1))
    monkeypatch.setattr(
        "services.strategy_trade_enrichment.SIGNAL_TRADES_DB", tmp_path / "absent.db"
    )
    monkeypatch.setattr("services.strategy_metrics_service.LEDGER_RELIABLE_SINCE", date(2020, 1, 1))
    return trades


def test_last_session_summary(ledger):
    assert svc.get_last_session("analyze") == {
        "date": "2026-10-30",
        "strategies": {"EMA": -30.0, "ORB": 20.0},
        "total": -10.0,
        "trades_count": 2,
    }


def test_last_session_degrades_to_none(monkeypatch):
    def boom(**kw):
        raise RuntimeError("x")

    monkeypatch.setattr(svc, "get_closed_trades", boom)
    assert svc.get_last_session("analyze") is None


def test_daily_response_has_new_fields(ledger):
    ok, r, _ = svc.get_daily_performance(None, "1d")
    assert ok
    assert r["by_hour"] and r["by_weekday"] and r["equity_curve_net"]
    assert r["equity_net_cost_coverage"]["total"] == 2
    assert r["excursion_total"] == 2 and r["excursion_covered"] == 0
    assert all(t["mfe"] is None for t in r["trades"])
    ok, r, _ = svc.get_daily_performance(None, "7d")
    assert "excursion_total" not in r and "by_hour" in r


def test_compare_lists_all_strategies(ledger):
    ok, r, _ = svc.get_strategy_comparison("1d")
    assert r["all_strategies"] == ["EMA", "ORB"]


def _client(monkeypatch):
    import utils.session as session_utils

    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(session_utils, "is_session_expiry_disabled", lambda: True)
    app = Flask(__name__)
    app.secret_key = "k"
    app.register_blueprint(route_mod.strategy_pnl_bp)
    app.register_blueprint(auth_bp_module.auth_bp)
    client = app.test_client()
    with client.session_transaction() as s:
        s["user"] = "u"
        s["logged_in"] = True
        s["login_time"] = "2026-01-01T09:00:00+05:30"
        s["broker"] = "flattrade"
    return client


def test_export_csv_guards_formulas_not_numbers(ledger, monkeypatch):
    client = _client(monkeypatch)
    r = client.get("/api/strategy-pnl/trades/export?period=1d")
    assert r.status_code == 200
    assert r.mimetype == "text/csv"
    assert "strategy-trades-2026-10-30_2026-10-30.csv" in r.headers["Content-Disposition"]
    lines = r.get_data(as_text=True).strip().splitlines()
    assert lines[0].startswith(
        "trade_date,opened_at,closed_at,hold_min,entry_hour,weekday,mode,strategy"
    )
    assert any("'=EVIL" in ln for ln in lines)
    assert any(",-30.0," in ln for ln in lines)  # negative number not quoted


def test_export_rejects_bad_period(ledger, monkeypatch):
    client = _client(monkeypatch)
    r = client.get("/api/strategy-pnl/trades/export?period=bogus")
    assert r.status_code == 400 and r.get_json()["status"] == "error"
