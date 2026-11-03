"""Live and Analyze results must never mix on the Performance page.

Uses the real strategy book (test database): one closed trade per mode for a
unique strategy, then reads the page's service under each mode.

Run with: uv run pytest test/test_strategy_performance_mode_isolation.py -v
"""

import uuid
from datetime import date, datetime, timedelta

import pytest

import services.strategy_daily_performance_service as svc
from database.strategy_book_db import StrategyClosedTrade, db_session, init_strategy_book_db


@pytest.fixture
def strategy():
    init_strategy_book_db()
    name = f"MODETEST-{uuid.uuid4().hex[:8]}"
    today = date.today().isoformat()
    now = datetime.now().replace(microsecond=0)
    holds = {"analyze": 30, "live": 90, "unknown": 5}
    for mode, pnl in (("analyze", 111.0), ("live", -222.0), ("unknown", 999.0)):
        db_session.add(
            StrategyClosedTrade(
                user_id="", strategy=name, symbol="INFY", exchange="NSE", product="MIS",
                mode=mode, direction="LONG", closed_quantity=10.0, entry_price=100.0,
                exit_price=100.0 + pnl / 10, realized_pnl=pnl, trade_date=today,
                opened_at=now - timedelta(minutes=holds[mode]), closed_at=now,
            )
        )
    db_session.commit()
    yield name
    db_session.query(StrategyClosedTrade).filter_by(strategy=name).delete()
    db_session.commit()


@pytest.mark.parametrize(
    "mode,expected", [("analyze", 111.0), ("live", -222.0)]
)
def test_each_mode_sees_only_its_own_trades(monkeypatch, strategy, mode, expected):
    monkeypatch.setattr(svc, "_current_mode", lambda: mode)
    monkeypatch.setattr("services.strategy_metrics_service.LEDGER_RELIABLE_SINCE", date(2020, 1, 1))
    ok, r, _ = svc.get_daily_performance(strategy, "1d")
    assert ok
    assert r["mode"] == mode
    assert r["trades_count"] == 1
    assert r["net_profit"] == expected
    assert [t["realized_pnl"] for t in r["trades"]] == [expected]


def test_comparison_is_also_mode_scoped(monkeypatch, strategy):
    monkeypatch.setattr("services.strategy_metrics_service.LEDGER_RELIABLE_SINCE", date(2020, 1, 1))
    for mode, expected in (("analyze", 111.0), ("live", -222.0)):
        monkeypatch.setattr(svc, "_current_mode", lambda m=mode: m)
        _, r, _ = svc.get_strategy_comparison("1d")
        assert r["mode"] == mode
        assert [row["net_profit"] for row in r["strategies"] if row["strategy"] == strategy] == [expected]


@pytest.mark.parametrize("mode,pnl,hold", [("analyze", 111.0, 30.0), ("live", -222.0, 90.0)])
def test_new_analytics_are_mode_scoped(monkeypatch, strategy, mode, pnl, hold):
    """Holding time, entry hour, hold buckets and the edge check read only the
    current mode's trades, the same as the original metrics."""
    monkeypatch.setattr(svc, "_current_mode", lambda: mode)
    monkeypatch.setattr("services.strategy_metrics_service.LEDGER_RELIABLE_SINCE", date(2020, 1, 1))
    ok, r, _ = svc.get_daily_performance(strategy, "1d")
    assert ok
    assert r["hold_stats"]["total"] == 1 and r["hold_stats"]["avg_min"] == hold
    assert sum(h["trades"] for h in r["by_entry_hour"]) == 1
    assert sum(b["trades"] for b in r["by_hold_bucket"]) == 1
    assert r["edge_trades"] == 1
    assert (r["net_expectancy"] > 0) == (pnl > 0)
    assert [t["hold_min"] for t in r["trades"]] == [hold]


@pytest.mark.parametrize("mode,pnl", [("analyze", 111.0), ("live", -222.0)])
def test_export_and_last_session_are_mode_scoped(monkeypatch, strategy, mode, pnl):
    monkeypatch.setattr(svc, "_current_mode", lambda: mode)
    monkeypatch.setattr("services.strategy_metrics_service.LEDGER_RELIABLE_SINCE", date(2020, 1, 1))
    ok, body, _ = svc.get_trades_for_export(strategy, "1d")
    assert ok
    assert [(t["mode"], t["realized_pnl"]) for t in body["trades"]] == [(mode, pnl)]
    last = svc.get_last_session(mode)
    assert last is not None and last["strategies"][strategy] == pnl


@pytest.mark.parametrize("mode,pnl", [("analyze", 111.0), ("live", -222.0)])
def test_capital_does_not_change_which_trades_each_mode_sees(monkeypatch, strategy, mode, pnl):
    monkeypatch.setattr(svc, "_current_mode", lambda: mode)
    monkeypatch.setattr("services.strategy_metrics_service.LEDGER_RELIABLE_SINCE", date(2020, 1, 1))
    _, plain, _ = svc.get_daily_performance(strategy, "1d")
    ok, r, _ = svc.get_daily_performance(strategy, "1d", capital=100000.0)
    assert ok and r["mode"] == mode
    assert r["capital_view"]["return_pct"] == round(pnl / 100000.0 * 100, 2)
    assert {k: v for k, v in r.items() if k != "capital_view"} == plain
