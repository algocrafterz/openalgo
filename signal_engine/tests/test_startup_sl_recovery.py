"""H5: recover each restored position's live SL order id from the broker orderbook.

_restore_tracker_positions() used to register restored positions with sl_order_id="", and
main._cancel_sl_before_exit() returns early when that is falsy. So after ANY restart the
next TP or EXIT signal placed a SELL while the broker's SL-M was still working — the exact
condition that makes an Indian broker read it as a NEW SHORT and reject it with FUND LIMIT
INSUFFICIENT. That is the bug the OCO handling exists to prevent, reintroduced by the
restart path.

Restarts are routine: the supervisor restarts on crash, the ~03:00 IST broker token rollover
forces one, and the 2026-09-08 flood-wait incident produced sixteen in twenty-four minutes.
"""

from unittest.mock import AsyncMock, patch

import pytest

from signal_engine.startup import _match_open_sl_orders


def _order(order_id, symbol, action="SELL", order_type="SL-M", status="open"):
    return {
        "orderid": order_id,
        "symbol": symbol,
        "action": action,
        "pricetype": order_type,
        "order_status": status,
    }


class TestMatchOpenSlOrders:
    def test_matches_a_working_sl_by_symbol(self):
        book = [_order("SL-1", "SBIN")]
        assert _match_open_sl_orders(book) == {"SBIN": "SL-1"}

    def test_ignores_filled_and_cancelled_orders(self):
        book = [
            _order("SL-old", "SBIN", status="complete"),
            _order("SL-dead", "SBIN", status="cancelled"),
            _order("SL-live", "SBIN", status="trigger pending"),
        ]
        assert _match_open_sl_orders(book) == {"SBIN": "SL-live"}

    def test_ignores_non_stop_order_types(self):
        """A working LIMIT order is not the bracket stop and must not be cancelled as one."""
        book = [_order("LMT-1", "SBIN", order_type="LIMIT")]
        assert _match_open_sl_orders(book) == {}

    def test_accepts_the_sl_limit_variant_too(self):
        book = [_order("SL-1", "SBIN", order_type="SL")]
        assert _match_open_sl_orders(book) == {"SBIN": "SL-1"}

    def test_last_working_order_wins_for_a_symbol(self):
        """After a partial exit the SL is replaced; the later id is the live one."""
        book = [_order("SL-1", "SBIN"), _order("SL-2", "SBIN")]
        assert _match_open_sl_orders(book) == {"SBIN": "SL-2"}

    def test_empty_or_malformed_book_yields_nothing(self):
        assert _match_open_sl_orders([]) == {}
        assert _match_open_sl_orders(None) == {}
        assert _match_open_sl_orders([{"nonsense": 1}]) == {}

    def test_symbols_are_matched_case_insensitively(self):
        assert _match_open_sl_orders([_order("SL-1", "sbin")]) == {"SBIN": "SL-1"}


class TestRestoredPositionsCarryTheirSl:
    def test_restore_attaches_the_recovered_id(self):
        from signal_engine.startup import _restore_tracker_positions

        class _Tracker:
            def __init__(self):
                self.registered = []

            def find_position(self, *_a):
                return None

            def register(self, pos):
                self.registered.append(pos)

        tracker = _Tracker()
        broker_positions = [{"symbol": "SBIN", "quantity": 100, "average_price": 800.0}]
        entry = {"entry": 800.0, "sl": 796.0, "tp": 810.0, "order_id": "E-1"}
        with patch("signal_engine.startup._lookup_entry_legs", return_value=[("ORB", entry)]):
            _restore_tracker_positions(tracker, broker_positions, "MIS", sl_orders={"SBIN": "SL-9"})
        assert tracker.registered[0].sl_order_id == "SL-9"

    def test_no_recovered_id_leaves_it_blank(self):
        from signal_engine.startup import _restore_tracker_positions

        class _Tracker:
            def __init__(self):
                self.registered = []

            def find_position(self, *_a):
                return None

            def register(self, pos):
                self.registered.append(pos)

        tracker = _Tracker()
        broker_positions = [{"symbol": "SBIN", "quantity": 100, "average_price": 800.0}]
        entry = {"entry": 800.0, "sl": 796.0, "tp": 810.0, "order_id": "E-1"}
        with patch("signal_engine.startup._lookup_entry_legs", return_value=[("ORB", entry)]):
            _restore_tracker_positions(tracker, broker_positions, "MIS", sl_orders={})
        assert tracker.registered[0].sl_order_id == ""
