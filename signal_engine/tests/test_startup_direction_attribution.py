"""Restart attribution must read the direction that trades.db actually returns.

db.fetch_last_entry_trade returns `direction` as a Direction enum. The restart lookups compared
`str(row["direction"]).upper()` with "LONG", and on Python 3.11+ str() of a (str, Enum) member is
"Direction.LONG", so no row ever matched a side. The quantity-and-side tier never fired, and the
2026-09-24 multi-leg split (one tracked position per strategy) never happened: a symbol entered by
two strategies was restored as one merged position again.

The older tests stubbed fetch_last_entry_trade with plain "LONG" strings, which hid this. These
tests write real rows and read them back through the real function.
"""

from types import SimpleNamespace
from unittest.mock import patch

from signal_engine import startup
from signal_engine.db import save
from signal_engine.models import Action, Direction, Order, OrderStatus, TradeResult
from signal_engine.tests.conftest import make_signal


def _save_entry(strategy: str, direction: Direction, qty: int, order_id: str) -> None:
    if direction == Direction.LONG:
        signal = make_signal(strategy=strategy, symbol="SBIN", direction=direction)
    else:
        signal = make_signal(strategy=strategy, symbol="SBIN", direction=direction, sl=2515.0, tp=2460.0)
    order = Order(
        symbol="SBIN",
        exchange="NSE",
        action=Action.BUY if direction == Direction.LONG else Action.SELL,
        quantity=qty,
        price=0.0,
        order_type="MARKET",
        product="MIS",
        strategy_tag=strategy,
    )
    save(signal, order, TradeResult(order_id=order_id, status=OrderStatus.SUCCESS, message="ok"))


def _settings():
    return patch.object(
        startup,
        "settings",
        SimpleNamespace(
            product="MIS", exchange="NSE", strategy_profiles={"BREAKINGTRADE": {}, "BREAKOUT": {}}
        ),
    )


def test_two_same_side_entries_summing_to_the_broker_quantity_are_split():
    _save_entry("BREAKINGTRADE", Direction.LONG, 30, "E-BT")
    _save_entry("BREAKOUT", Direction.LONG, 20, "E-BO")
    with _settings():
        legs = startup._lookup_entry_legs("SBIN", broker_qty=50, is_long=True)
    assert sorted(s for s, _ in legs) == ["BREAKINGTRADE", "BREAKOUT"]


def test_quantity_and_side_match_beats_a_more_recent_row():
    _save_entry("BREAKOUT", Direction.LONG, 40, "E-BO")
    _save_entry("BREAKINGTRADE", Direction.LONG, 25, "E-BT")  # newer, wrong size
    with _settings():
        row, strategy = startup._lookup_entry_trade("SBIN", broker_qty=40, is_long=True)
    assert strategy == "BREAKOUT"
    assert row["order_id"] == "E-BO"


def test_the_side_disqualifies_a_newer_opposite_entry():
    _save_entry("BREAKOUT", Direction.LONG, 40, "E-BO")
    _save_entry("BREAKINGTRADE", Direction.SHORT, 40, "E-BT")  # newer, opposite side
    with _settings():
        _row, strategy = startup._lookup_entry_trade("SBIN", broker_qty=40, is_long=True)
    assert strategy == "BREAKOUT"
