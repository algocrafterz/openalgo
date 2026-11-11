"""G5: stale-price bookkeeping is keyed per (symbol, exchange, product) row.

_index_positionbook keeps the LAST row of a symbol as the top row and the earlier ones as
siblings. A change of row order between two polls must not swap the rows' price histories.
"""

from unittest.mock import MagicMock

from signal_engine.tracker import _STALE_PRICE_SECONDS, PositionTracker, _index_positionbook


def _book(*rows):
    return _index_positionbook(
        [
            {"symbol": "SBIN", "exchange": "NSE", "product": product, "quantity": 100, "ltp": ltp}
            for product, ltp in rows
        ]
    )


def test_a_change_of_row_order_does_not_swap_price_histories():
    t = PositionTracker(MagicMock())
    # MIS frozen at 800, CNC moving. First poll lists MIS then CNC; the next lists CNC then MIS.
    t._mark_stale_prices(_book(("MIS", 800.0), ("CNC", 790.0)), now=1000.0)
    later = 1000.0 + _STALE_PRICE_SECONDS + 1
    out = t._mark_stale_prices(_book(("CNC", 795.0), ("MIS", 800.0)), now=later)

    rows = {row.product: row for row in (out["SBIN"], *out["SBIN"].siblings)}
    assert rows["MIS"].stale is True  # unchanged for longer than the window
    assert rows["CNC"].stale is False  # moved, so fresh whatever its position in the book


def test_a_single_row_symbol_still_goes_stale_and_logs_by_symbol_name():
    from loguru import logger

    t = PositionTracker(MagicMock())
    messages = []
    sink = logger.add(lambda m: messages.append(m.record["message"]), level="WARNING")
    try:
        t._mark_stale_prices(_book(("MIS", 800.0)), now=1000.0)
        out = t._mark_stale_prices(_book(("MIS", 800.0)), now=1000.0 + _STALE_PRICE_SECONDS + 1)
    finally:
        logger.remove(sink)
    assert out["SBIN"].stale is True
    assert any(m.startswith("SBIN: price 800.00 unchanged") for m in messages)
