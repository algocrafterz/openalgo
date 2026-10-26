"""The --dry-run sizing check must not write the risk store.

dry_run_entry_pipeline() sizes its synthetic ORB signal on an engine built with
build_risk_engine()'s default "live" mode, before the broker's mode is known. With a real
store, get_sizing_capital() persisted the fetched capital as the day-start capital of the LIVE
pooled bucket (PORTFOLIO/live), even with the broker in ANALYZE: the sandbox override of
100,000 was written there on 2026-10-05 and 2026-10-14. A LIVE session later that day restores
that row and sizes every trade off it instead of its own capital.
"""

import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

from signal_engine.risk_store import RiskStore
from signal_engine.runtime import build_risk_engine

SANDBOX_CAPITAL = 100000.0
LIVE_CAPITAL = 20000.0
MODES = [("analyze", True), ("live", False)]


def _store_must_not_open(*_args, **_kwargs):
    raise AssertionError("dry_run_entry_pipeline opened a RiskStore")


async def _dry_run(mode, capital=SANDBOX_CAPITAL):
    from signal_engine import smoke_test

    with (
        patch("signal_engine.api_client.fetch_available_capital", AsyncMock(return_value=capital)),
        patch("signal_engine.api_client.fetch_trading_mode", AsyncMock(return_value=mode)),
    ):
        return await smoke_test.dry_run_entry_pipeline()


def _expected_qty(capital):
    """What the dry run must report: the synthetic signal sized by a store-free engine."""
    from signal_engine.normalizer import normalize
    from signal_engine.parser import parse

    signal = parse(
        normalize("ORB LONG\nSymbol: SBIN\nEntry: 800.00\nSL: 790.00\nTP: 820.00\nExchange: NSE\n")
    )
    return build_risk_engine(None).calculate_quantity(signal, capital=capital)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES, ids=["analyze", "live"])
async def test_dry_run_opens_no_risk_store(mode):
    with patch("signal_engine.smoke_test.RiskStore", side_effect=_store_must_not_open):
        msg = await _dry_run(mode)

    qty = _expected_qty(SANDBOX_CAPITAL)
    assert qty > 0
    assert msg.startswith("OK")
    assert f"capital={SANDBOX_CAPITAL:,.0f} qty={qty} " in msg


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES, ids=["analyze", "live"])
async def test_dry_run_leaves_the_risk_db_without_rows(mode, tmp_path, monkeypatch):
    db = tmp_path / "risk.db"
    RiskStore(str(db)).close()  # the table exists, as it does on a real install
    monkeypatch.setattr("signal_engine.smoke_test.RISK_DB_PATH", str(db))

    await _dry_run(mode)

    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT * FROM risk_counters").fetchall() == []
    finally:
        con.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES, ids=["analyze", "live"])
async def test_later_live_session_sizes_off_its_own_capital(mode, tmp_path, monkeypatch):
    db = str(tmp_path / "risk.db")
    monkeypatch.setattr("signal_engine.smoke_test.RISK_DB_PATH", db)

    await _dry_run(mode, capital=SANDBOX_CAPITAL)

    with RiskStore(db) as store:
        live = build_risk_engine(store, trade_mode="live")
        assert live.get_sizing_capital(LIVE_CAPITAL, "BREAKOUT") == LIVE_CAPITAL


@pytest.mark.asyncio
async def test_dry_run_sizes_off_fresh_capital_and_keeps_the_cached_day_start(
    tmp_path, monkeypatch
):
    """Documented trade-off: a day-start capital already cached today is neither read nor changed."""
    db = str(tmp_path / "risk.db")
    with RiskStore(db) as store:
        build_risk_engine(store, trade_mode="live").get_sizing_capital(LIVE_CAPITAL, "ORB")
    monkeypatch.setattr("signal_engine.smoke_test.RISK_DB_PATH", db)

    msg = await _dry_run(("live", False), capital=SANDBOX_CAPITAL)

    assert f"capital={SANDBOX_CAPITAL:,.0f} qty={_expected_qty(SANDBOX_CAPITAL)} " in msg
    with RiskStore(db) as store:
        live = build_risk_engine(store, trade_mode="live")
        assert live.get_sizing_capital(SANDBOX_CAPITAL, "ORB") == LIVE_CAPITAL
