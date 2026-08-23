"""Shared factories for the main-pipeline test modules.

Split out of the former 1,878-line test_main.py so the entry, exit, partial-exit
and helper suites can each live in their own file without duplicating setup.
"""

from unittest.mock import MagicMock

from signal_engine.models import Direction, OrderStatus, TradeResult


def _bracket_trade_results():
    sl_result = TradeResult(order_id="SL001", status=OrderStatus.SUCCESS, message="ok")
    tp_result = TradeResult(order_id="TP001", status=OrderStatus.SUCCESS, message="ok")
    return sl_result, tp_result


def _valid_message():
    return "ORB LONG\nSymbol: RELIANCE\nEntry: 2500\nSL: 2485\nTP: 2540"


def _mock_signal():
    sig = MagicMock()
    sig.strategy = "ORB"
    sig.direction = Direction.LONG
    sig.symbol = "RELIANCE"
    sig.entry = 2500.0
    sig.sl = 2485.0
    sig.tp = 2540.0
    return sig
