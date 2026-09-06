"""SigID: the stable key that threads one trade from alert to broker fill.

WHY

`ledger.py` joins on `order_id`, which exists only once the engine has SENT an order. Before
that — signal received, validated, sized, rejected — there is no key at all, and the ledger
falls back to grouping by (strategy, symbol, day) plus arrival order. Its own docstring warns
that this "will silently mis-pair two trades in the same name on the same day".

That is not hypothetical: breakout.pine allows a re-entry on the same symbol after a stop, and
on 2026-09-03 PFC and SBIN fired 5 seconds apart. A delayed or out-of-order Telegram message is
enough to attach an exit to the wrong entry, and nothing in the data would show it happened.

SigID is emitted by the PineScript on the entry alert AND on every TP/SL/EXIT alert for that
same trade, so every leg carries the identity of the position it belongs to before any broker
is involved.
"""

import pytest

from signal_engine.analysis.ledger import build_ledger
from signal_engine.normalizer import normalize
from signal_engine.parser import parse

_ENTRY = """BREAKOUT LONG | LICHSGFIN
SigID: LICHSGFIN-20260904-1045
Entry: 564.2
Target: 566.59
SL: 562.61
Score: 10
"""

_TP = """BREAKOUT TP1 HIT | LICHSGFIN
SigID: LICHSGFIN-20260904-1045
LONG | Entry: 564.2
Exit: 566.59
SL: 562.61
TP: 566.59
ExitQtyPct: 30
"""


def _pipeline(text):
    """The real path a Telegram message takes: normalize, then parse."""
    return parse(normalize(text))


class TestParsing:
    def test_entry_alert_carries_sig_id(self):
        sig = _pipeline(_ENTRY)
        assert sig is not None
        assert sig.sig_id == "LICHSGFIN-20260904-1045"

    def test_exit_alert_carries_the_same_sig_id(self):
        sig = _pipeline(_TP)
        assert sig is not None
        assert sig.sig_id == "LICHSGFIN-20260904-1045"

    def test_absent_sig_id_is_none_not_empty_string(self):
        """Every alert predating this field must still parse — None means 'no key'."""
        sig = _pipeline(_ENTRY.replace("SigID: LICHSGFIN-20260904-1045\n", ""))
        assert sig is not None
        assert sig.sig_id is None

    def test_sig_id_is_not_duplicated_into_context(self):
        """Consumed fields stay out of context, so the trade log has one copy, not two."""
        sig = _pipeline(_ENTRY)
        assert "sigid" not in sig.context

    def test_blank_sig_id_is_treated_as_absent(self):
        sig = _pipeline(_ENTRY.replace("SigID: LICHSGFIN-20260904-1045", "SigID:  "))
        assert sig is not None
        assert sig.sig_id is None


class TestLedgerJoin:
    """The case the ordering rule gets wrong: two round trips, same symbol, same day."""

    def _ev(self, ts, direction, sig_id, order_id, qty, price):
        from datetime import datetime

        from signal_engine.timeutils import IST

        return {
            "strategy": "BREAKOUT",
            "symbol": "SBIN",
            "direction": direction,
            "entry": price,
            "sl": 0.0,
            "tp": 0.0,
            "quantity": qty,
            "order_id": order_id,
            "status": "SUCCESS",
            "context": {},
            "sig_id": sig_id,
            "_ts": datetime(2026, 9, 3, *ts, tzinfo=IST),
        }

    def test_two_round_trips_same_symbol_same_day_stay_separate(self):
        events = [
            self._ev((10, 30), "LONG", "SBIN-20260903-1030", "E1", 100, 1035.7),
            self._ev((11, 15), "LONG", "SBIN-20260903-1115", "E2", 100, 1041.0),
            self._ev((11, 20), "EXIT", "SBIN-20260903-1030", "X1", 100, 1032.6),
            self._ev((11, 45), "EXIT", "SBIN-20260903-1115", "X2", 100, 1050.0),
        ]
        positions = build_ledger(events)
        by_sig = {p.sig_id: p for p in positions}
        assert set(by_sig) == {"SBIN-20260903-1030", "SBIN-20260903-1115"}
        assert by_sig["SBIN-20260903-1030"].exits[0].order_id == "X1"
        assert by_sig["SBIN-20260903-1115"].exits[0].order_id == "X2"

    def test_out_of_order_exit_still_lands_on_its_own_entry(self):
        """The exact failure the ordering rule cannot see: X1 arrives after E2 opened."""
        events = [
            self._ev((10, 30), "LONG", "SBIN-20260903-1030", "E1", 100, 1035.7),
            self._ev((11, 15), "LONG", "SBIN-20260903-1115", "E2", 100, 1041.0),
            self._ev((11, 16), "EXIT", "SBIN-20260903-1030", "X1", 100, 1032.6),
        ]
        positions = build_ledger(events)
        by_sig = {p.sig_id: p for p in positions}
        assert [leg.order_id for leg in by_sig["SBIN-20260903-1030"].exits] == ["X1"]
        assert by_sig["SBIN-20260903-1115"].exits == []

    def test_events_without_sig_id_fall_back_to_the_ordering_rule(self):
        """Historical rows have no SigID and must keep reconciling exactly as before."""
        events = [
            self._ev((10, 30), "LONG", None, "E1", 100, 1035.7),
            self._ev((10, 40), "EXIT", None, "X1", 100, 1032.6),
        ]
        positions = build_ledger(events)
        assert len(positions) == 1
        assert [leg.order_id for leg in positions[0].exits] == ["X1"]

    def test_mixed_sig_id_and_legacy_events_do_not_cross_contaminate(self):
        events = [
            self._ev((10, 30), "LONG", None, "E1", 100, 1035.7),
            self._ev((10, 40), "EXIT", None, "X1", 100, 1032.6),
            self._ev((11, 15), "LONG", "SBIN-20260903-1115", "E2", 100, 1041.0),
            self._ev((11, 45), "EXIT", "SBIN-20260903-1115", "X2", 100, 1050.0),
        ]
        positions = build_ledger(events)
        assert len(positions) == 2
        keyed = {p.sig_id: p for p in positions}
        assert keyed["SBIN-20260903-1115"].exits[0].order_id == "X2"
