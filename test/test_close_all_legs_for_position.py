"""
Unit tests: close_all_legs_for_position() force-closes a strategy leg at a
given exit price, bypassing the normal order-tag/apply_fill path. Added
2026-09-28 alongside the fix for a bug that left strategy_positions rows
permanently stuck "open" after sandbox/position_manager.py's close_position,
_settle_expired_position, cleanup_expired_contracts, and
sandbox/catch_up_processor.py's catch_up_mis_squareoff all flattened the real
position with no order.placed/order.update event ever firing - see
docs/strategy-pnl-fork-modification.md's "Bug 1" for the incident.

Run with: uv run pytest test/test_close_all_legs_for_position.py -v
"""

import uuid

import pytest

from database.strategy_book_db import (
    apply_fill,
    close_all_legs_for_position,
    get_closed_trades,
    get_strategy_legs,
    init_strategy_book_db,
    record_order_tag,
)


@pytest.fixture(scope="module", autouse=True)
def _init_book():
    init_strategy_book_db()


def _oid():
    return f"test-close-legs-{uuid.uuid4().hex}"


def _open_leg(strategy, symbol, exchange, product, mode, qty, price, action):
    oid = _oid()
    assert record_order_tag(
        orderid=oid,
        user_id="",
        strategy=strategy,
        symbol=symbol,
        exchange=exchange,
        product=product,
        mode=mode,
    )
    apply_fill(oid, filled_quantity=qty, average_price=price, action=action)


def _leg(strategy, symbol, exchange, product, mode):
    legs = get_strategy_legs(strategy=strategy, mode=mode)
    matches = [
        leg for leg in legs if leg["symbol"] == symbol and leg["exchange"] == exchange and leg["product"] == product
    ]
    assert len(matches) == 1, f"expected exactly one leg, got {matches}"
    return matches[0]


def test_closes_a_long_leg_at_the_given_exit_price():
    strategy = f"CLOSEALL-LONG-{uuid.uuid4().hex[:8]}"
    symbol = "CLSLONG" + uuid.uuid4().hex[:6].upper()
    exchange, product, mode = "NSE", "MIS", "analyze"
    _open_leg(strategy, symbol, exchange, product, mode, qty=10, price=100.0, action="BUY")

    closed = close_all_legs_for_position(
        user_id="",
        symbol=symbol,
        exchange=exchange,
        product=product,
        mode=mode,
        exit_price=110.0,
        book_today_pnl=True,
    )

    assert len(closed) == 1
    assert closed[0]["strategy"] == strategy
    assert closed[0]["realized_pnl"] == pytest.approx(100.0)  # 10 * (110 - 100)

    leg = _leg(strategy, symbol, exchange, product, mode)
    assert leg["quantity"] == 0
    assert leg["realized_pnl"] == pytest.approx(100.0)
    assert leg["today_realized_pnl"] == pytest.approx(100.0)


def test_closes_a_short_leg_with_correct_sign():
    strategy = f"CLOSEALL-SHORT-{uuid.uuid4().hex[:8]}"
    symbol = "CLSSHORT" + uuid.uuid4().hex[:6].upper()
    exchange, product, mode = "NSE", "MIS", "analyze"
    _open_leg(strategy, symbol, exchange, product, mode, qty=5, price=200.0, action="SELL")

    closed = close_all_legs_for_position(
        user_id="",
        symbol=symbol,
        exchange=exchange,
        product=product,
        mode=mode,
        exit_price=190.0,
        book_today_pnl=True,
    )

    assert closed[0]["realized_pnl"] == pytest.approx(50.0)  # 5 * (200 - 190)
    leg = _leg(strategy, symbol, exchange, product, mode)
    assert leg["quantity"] == 0


def test_book_today_pnl_false_leaves_todays_figure_untouched():
    strategy = f"CLOSEALL-STALE-{uuid.uuid4().hex[:8]}"
    symbol = "CLSSTALE" + uuid.uuid4().hex[:6].upper()
    exchange, product, mode = "NSE", "MIS", "analyze"
    _open_leg(strategy, symbol, exchange, product, mode, qty=10, price=100.0, action="BUY")

    close_all_legs_for_position(
        user_id="",
        symbol=symbol,
        exchange=exchange,
        product=product,
        mode=mode,
        exit_price=105.0,
        book_today_pnl=False,
    )

    leg = _leg(strategy, symbol, exchange, product, mode)
    # All-time realized always gets the P&L; today's figure does not, mirroring
    # catch_up_mis_squareoff's own funds accounting for a stale multi-day
    # position that did not close "today" from the trader's perspective.
    assert leg["quantity"] == 0
    assert leg["realized_pnl"] == pytest.approx(50.0)
    assert leg["today_realized_pnl"] == 0.0


def test_writes_a_closed_trade_ledger_row():
    strategy = f"CLOSEALL-LEDGER-{uuid.uuid4().hex[:8]}"
    symbol = "CLSLEDGER" + uuid.uuid4().hex[:6].upper()
    exchange, product, mode = "NSE", "MIS", "analyze"
    _open_leg(strategy, symbol, exchange, product, mode, qty=20, price=50.0, action="BUY")

    close_all_legs_for_position(
        user_id="",
        symbol=symbol,
        exchange=exchange,
        product=product,
        mode=mode,
        exit_price=55.0,
        book_today_pnl=True,
    )

    trades = get_closed_trades(strategy=strategy, mode=mode)
    assert len(trades) == 1
    assert trades[0]["direction"] == "LONG"
    assert trades[0]["closed_quantity"] == 20
    assert trades[0]["exit_price"] == pytest.approx(55.0)
    assert trades[0]["realized_pnl"] == pytest.approx(100.0)


def test_no_matching_leg_is_a_safe_no_op():
    closed = close_all_legs_for_position(
        user_id="",
        symbol="DOES-NOT-EXIST",
        exchange="NSE",
        product="MIS",
        mode="analyze",
        exit_price=100.0,
        book_today_pnl=True,
    )
    assert closed == []


def test_already_flat_leg_is_not_reopened_or_reclosed():
    strategy = f"CLOSEALL-FLAT-{uuid.uuid4().hex[:8]}"
    symbol = "CLSFLAT" + uuid.uuid4().hex[:6].upper()
    exchange, product, mode = "NSE", "MIS", "analyze"
    open_oid, close_oid = _oid(), _oid()
    for oid in (open_oid, close_oid):
        assert record_order_tag(
            orderid=oid,
            user_id="",
            strategy=strategy,
            symbol=symbol,
            exchange=exchange,
            product=product,
            mode=mode,
        )
    apply_fill(open_oid, filled_quantity=10, average_price=100.0, action="BUY")
    apply_fill(close_oid, filled_quantity=10, average_price=105.0, action="SELL")

    leg_before = _leg(strategy, symbol, exchange, product, mode)
    assert leg_before["quantity"] == 0

    closed = close_all_legs_for_position(
        user_id="",
        symbol=symbol,
        exchange=exchange,
        product=product,
        mode=mode,
        exit_price=999.0,
        book_today_pnl=True,
    )
    # quantity == 0 legs are filtered out - nothing to close, nothing booked.
    assert closed == []
    leg_after = _leg(strategy, symbol, exchange, product, mode)
    assert leg_after["realized_pnl"] == leg_before["realized_pnl"]


def test_closes_every_strategy_sharing_the_same_symbol_independently():
    """Confirmed in production (2026-09-28 audit): more than one strategy can
    hold the same (symbol, exchange, product) at once (JIOFIN, HDFCBANK, TCS
    were each traded by two strategies concurrently). A single square-off
    order has one exit price to attribute - every sharing strategy's own leg
    must close independently at that price, not just the first one found.
    """
    exchange, product, mode = "NSE", "MIS", "analyze"
    symbol = "SHARED" + uuid.uuid4().hex[:8].upper()
    strat_a = f"CLOSEALL-SHARE-A-{uuid.uuid4().hex[:8]}"
    strat_b = f"CLOSEALL-SHARE-B-{uuid.uuid4().hex[:8]}"
    _open_leg(strat_a, symbol, exchange, product, mode, qty=10, price=100.0, action="BUY")
    _open_leg(strat_b, symbol, exchange, product, mode, qty=5, price=200.0, action="BUY")

    closed = close_all_legs_for_position(
        user_id="",
        symbol=symbol,
        exchange=exchange,
        product=product,
        mode=mode,
        exit_price=110.0,
        book_today_pnl=True,
    )

    assert len(closed) == 2
    by_strategy = {c["strategy"]: c for c in closed}
    assert by_strategy[strat_a]["realized_pnl"] == pytest.approx(100.0)  # 10 * (110-100)
    assert by_strategy[strat_b]["realized_pnl"] == pytest.approx(-450.0)  # 5 * (110-200)
    assert _leg(strat_a, symbol, exchange, product, mode)["quantity"] == 0
    assert _leg(strat_b, symbol, exchange, product, mode)["quantity"] == 0


def test_mode_isolation_only_closes_the_matching_mode():
    symbol, exchange, product = "MODEISO" + uuid.uuid4().hex[:6].upper(), "NSE", "MIS"
    strategy = f"CLOSEALL-MODE-{uuid.uuid4().hex[:8]}"
    _open_leg(strategy, symbol, exchange, product, "analyze", qty=10, price=100.0, action="BUY")
    _open_leg(strategy, symbol, exchange, product, "live", qty=7, price=100.0, action="BUY")

    closed = close_all_legs_for_position(
        user_id="",
        symbol=symbol,
        exchange=exchange,
        product=product,
        mode="analyze",
        exit_price=110.0,
        book_today_pnl=True,
    )

    assert len(closed) == 1
    assert _leg(strategy, symbol, exchange, product, "analyze")["quantity"] == 0
    live_leg = _leg(strategy, symbol, exchange, product, "live")
    assert live_leg["quantity"] == 7  # untouched
