"""Live and Analyze results must never mix on the Performance page.

Uses the real strategy book (test database): one closed trade per mode for a
unique strategy, then reads the page's service under each mode.

Run with: uv run pytest test/test_strategy_performance_mode_isolation.py -v
"""

import uuid
from datetime import date

import pytest

import services.strategy_daily_performance_service as svc
from database.strategy_book_db import StrategyClosedTrade, db_session, init_strategy_book_db


@pytest.fixture
def strategy():
    init_strategy_book_db()
    name = f"MODETEST-{uuid.uuid4().hex[:8]}"
    today = date.today().isoformat()
    for mode, pnl in (("analyze", 111.0), ("live", -222.0), ("unknown", 999.0)):
        db_session.add(
            StrategyClosedTrade(
                user_id="", strategy=name, symbol="INFY", exchange="NSE", product="MIS",
                mode=mode, direction="LONG", closed_quantity=10.0, entry_price=100.0,
                exit_price=100.0 + pnl / 10, realized_pnl=pnl, trade_date=today,
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
    monkeypatch.setattr(svc, "list_strategies", lambda: [strategy])
    for mode, expected in (("analyze", 111.0), ("live", -222.0)):
        monkeypatch.setattr(svc, "_current_mode", lambda m=mode: m)
        _, r, _ = svc.get_strategy_comparison("1d")
        assert r["mode"] == mode
        assert [row["net_profit"] for row in r["strategies"]] == [expected]
