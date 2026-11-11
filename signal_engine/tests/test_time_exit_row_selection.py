"""F2: time-exit leg pricing reads the position's own product row, not the last row of the symbol."""

from unittest.mock import AsyncMock, patch

import pytest

from signal_engine.strategies import ORB
from signal_engine.tests.tracker_fixtures import _make_engine, _make_position
from signal_engine.tracker import PositionTracker


@pytest.mark.asyncio
async def test_time_exit_prices_from_the_row_with_the_same_product():
    t = PositionTracker(_make_engine())
    pos = _make_position(symbol="AAA", strategy=ORB, product="MIS", quantity=10,
                         entry_price=100.0, fill_price=100.0)
    # The CNC row comes LAST for the symbol; the MIS row is the position's own.
    book = [
        {"symbol": "AAA", "exchange": "NSE", "product": "MIS", "quantity": 0, "ltp": 110.0},
        {"symbol": "AAA", "exchange": "NSE", "product": "CNC", "quantity": 5, "ltp": 90.0},
    ]
    with patch("signal_engine.tracker.fetch_positionbook", AsyncMock(return_value=book)):
        out = await t._time_exit_leg_pnls({"AAA:ORB": pos})
    assert out == {"AAA:ORB": 100.0}
