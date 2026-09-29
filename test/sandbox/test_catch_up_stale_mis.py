"""catch_up_mis_squareoff must not settle a position that traded today.

On 2026-09-29 an ORB INFY short opened at 09:55 IST was force-settled at
10:25 IST as a "stale overnight MIS position" with no exit order. The sandbox
reuses one sandbox_positions row per symbol, so when a symbol reopens the row
keeps its original created_at (INFY: 2026-09-22) and looked "from a previous
day". A position is stale only if it also has no trade dated today.
"""

import uuid
from datetime import datetime, timedelta

import pytest

from database.sandbox_db import SandboxPositions, SandboxTrades, db_session
from sandbox.catch_up_processor import catch_up_mis_squareoff

USER_ID = "catchup-test-user"


def _make_position(symbol, qty):
    db_session.add(
        SandboxPositions(
            user_id=USER_ID,
            symbol=symbol,
            exchange="NSE",
            product="MIS",
            quantity=qty,
            average_price=100,
            ltp=100,
            created_at=datetime.now() - timedelta(days=7),
        )
    )
    db_session.commit()


def _add_trade_today(symbol):
    db_session.add(
        SandboxTrades(
            tradeid=uuid.uuid4().hex,
            orderid=uuid.uuid4().hex,
            user_id=USER_ID,
            symbol=symbol,
            exchange="NSE",
            action="SELL",
            quantity=45,
            price=100,
            product="MIS",
            trade_timestamp=datetime.now(),
        )
    )
    db_session.commit()


def _qty(symbol):
    db_session.expire_all()
    return SandboxPositions.query.filter_by(user_id=USER_ID, symbol=symbol).one().quantity


@pytest.fixture(autouse=True)
def _clean():
    yield
    SandboxTrades.query.filter_by(user_id=USER_ID).delete()
    SandboxPositions.query.filter_by(user_id=USER_ID).delete()
    db_session.commit()


def test_overnight_position_with_no_trade_today_is_settled():
    _make_position("CUOLD", -45)
    catch_up_mis_squareoff()
    assert _qty("CUOLD") == 0


def test_reopened_position_with_trade_today_is_left_open():
    _make_position("CUREOPEN", -45)
    _add_trade_today("CUREOPEN")
    catch_up_mis_squareoff()
    assert _qty("CUREOPEN") == -45


def test_trade_today_on_another_symbol_does_not_protect_old_position():
    _make_position("CUOLD2", 10)
    _make_position("CUOTHER", -5)
    _add_trade_today("CUOTHER")
    catch_up_mis_squareoff()
    assert _qty("CUOLD2") == 0
    assert _qty("CUOTHER") == -5
