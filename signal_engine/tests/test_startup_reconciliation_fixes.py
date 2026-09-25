"""M4, M5, M6 — three ways startup reconciliation could get a position wrong.

M4  The position book was filtered by the GLOBAL settings.product only, ignoring
    strategy_profiles.<TAG>.product. A strategy overridden to CNC had its still-open
    position excluded from open_broker_symbols, then found in by_symbol and booked as a
    RECONCILED EXIT — closing a live position in the ledger while it was open at the broker.
M5  _lookup_entry_trade() tried "ORB" first, then strategy_profiles in dict order, so a
    symbol traded by two strategies today was attributed to whichever came first.
M6  Only strategies present in the position book were corrected, leaving a phantom slot on
    any strategy whose counter had drifted.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import startup


def _settings(product="MIS", strategy_profiles=None):
    """settings is a frozen dataclass, so swap the whole reference rather than a field."""
    return patch.object(
        startup,
        "settings",
        SimpleNamespace(product=product, strategy_profiles=strategy_profiles or {}, exchange="NSE"),
    )


class TestConfiguredProducts:
    def test_includes_the_global_product(self):
        with _settings():
            assert "MIS" in startup._configured_products()

    def test_includes_every_per_strategy_override(self):
        profiles = {"ORB": {"product": "MIS"}, "RSI-TP-MR": {"product": "CNC"}}
        with _settings(strategy_profiles=profiles):
            assert startup._configured_products() >= {"MIS", "CNC"}

    def test_includes_the_broker_single_letter_codes(self):
        """OpenAlgo reports MIS as "I" and CNC as "C" depending on the broker."""
        profiles = {"RSI-TP-MR": {"product": "CNC"}}
        with _settings(strategy_profiles=profiles):
            products = startup._configured_products()
        assert {"MIS", "I", "CNC", "C"} <= products

    def test_a_profile_without_a_product_key_is_ignored(self):
        with _settings(strategy_profiles={"ORB": {"min_sl_pct": 0.002}}):
            assert startup._configured_products() == {"MIS", "I"}


class TestCncPositionIsNotBookedAsClosed:
    @pytest.mark.asyncio
    async def test_an_open_cnc_position_is_left_alone(self):
        risk = MagicMock()
        risk.isolates_per_strategy = True
        risk.total_open_positions.return_value = 1
        risk.open_positions_for.return_value = 1
        risk.known_strategies.return_value = {"RSI-TP-MR"}
        tracker = MagicMock()
        tracker.find_position.return_value = None

        locally_open = [
            {
                "symbol": "SBIN",
                "strategy": "RSI-TP-MR",
                "entry": 800.0,
                "sl": 0.0,
                "tp": 850.0,
                "quantity": 10,
            }
        ]
        book = [
            {"symbol": "SBIN", "quantity": 10, "product": "CNC", "average_price": 800.0, "pnl": 0}
        ]

        with (
            _settings(strategy_profiles={"RSI-TP-MR": {"product": "CNC"}}),
            patch("signal_engine.db.fetch_all_open_positions", return_value=locally_open),
            patch("signal_engine.db.save_reconciled_exit") as save_exit,
            patch("signal_engine.api_client.fetch_positionbook", AsyncMock(return_value=book)),
            patch.object(startup, "_recover_sl_order_ids", AsyncMock(return_value={})),
            patch.object(startup, "_restore_tracker_positions", return_value=0),
        ):
            await startup.reconcile_open_positions(risk, tracker)

        save_exit.assert_not_called()
        risk.record_close.assert_not_called()


class TestIdleStrategyCountersAreCorrected:
    @pytest.mark.asyncio
    async def test_a_phantom_slot_with_no_local_rows_is_cleared(self):
        risk = MagicMock()
        risk.isolates_per_strategy = True
        risk.total_open_positions.return_value = 2
        risk.known_strategies.return_value = {"ORB", "BREAKOUT"}
        risk.open_positions_for.side_effect = lambda s: {"ORB": 1, "BREAKOUT": 1}[s]
        tracker = MagicMock()
        tracker.find_position.return_value = None

        # Only ORB is genuinely open; BREAKOUT's counter is a leftover.
        locally_open = [
            {
                "symbol": "SBIN",
                "strategy": "ORB",
                "entry": 800.0,
                "sl": 796.0,
                "tp": 810.0,
                "quantity": 100,
            }
        ]
        book = [{"symbol": "SBIN", "quantity": 100, "product": "MIS", "average_price": 800.0}]

        with (
            _settings(),
            patch("signal_engine.db.fetch_all_open_positions", return_value=locally_open),
            patch("signal_engine.api_client.fetch_positionbook", AsyncMock(return_value=book)),
            patch.object(startup, "_recover_sl_order_ids", AsyncMock(return_value={})),
            patch.object(startup, "_restore_tracker_positions", return_value=0),
        ):
            await startup.reconcile_open_positions(risk, tracker)

        corrected = {c.args[0]: c.args[1] for c in risk.set_open_positions.call_args_list}
        assert corrected.get("BREAKOUT") == 0
        assert "ORB" not in corrected  # already matched, left alone


class TestEntryTradeAttribution:
    def test_prefers_the_strategy_whose_quantity_and_side_match(self):
        rows = {
            ("SBIN", "ORB"): {
                "entry": 800.0,
                "sl": 796.0,
                "tp": 810.0,
                "quantity": 50,
                "order_id": "E-ORB",
                "direction": "LONG",
            },
            ("SBIN", "BREAKOUT"): {
                "entry": 801.0,
                "sl": 797.0,
                "tp": 812.0,
                "quantity": 100,
                "order_id": "E-BRK",
                "direction": "LONG",
            },
        }
        with (
            _settings(strategy_profiles={"ORB": {}, "BREAKOUT": {}}),
            patch(
                "signal_engine.startup.fetch_last_entry_trade",
                side_effect=lambda sym, strat: rows.get((sym, strat)),
            ),
        ):
            found, strategy = startup._lookup_entry_trade("SBIN", broker_qty=100, is_long=True)
        assert strategy == "BREAKOUT"
        assert found["order_id"] == "E-BRK"

    def test_falls_back_to_the_most_recent_row_when_nothing_matches(self):
        rows = {
            ("SBIN", "ORB"): {
                "entry": 800.0,
                "sl": 796.0,
                "tp": 810.0,
                "quantity": 50,
                "order_id": "E-ORB",
                "direction": "LONG",
                "executed_at": "2026-09-11 09:30:00",
            },
            ("SBIN", "BREAKOUT"): {
                "entry": 801.0,
                "sl": 797.0,
                "tp": 812.0,
                "quantity": 70,
                "order_id": "E-BRK",
                "direction": "LONG",
                "executed_at": "2026-09-11 10:15:00",
            },
        }
        with (
            _settings(strategy_profiles={"ORB": {}, "BREAKOUT": {}}),
            patch(
                "signal_engine.startup.fetch_last_entry_trade",
                side_effect=lambda sym, strat: rows.get((sym, strat)),
            ),
        ):
            found, strategy = startup._lookup_entry_trade("SBIN", broker_qty=999, is_long=True)
        assert strategy == "BREAKOUT"  # latest executed_at wins

    def test_direction_disqualifies_a_quantity_match(self):
        rows = {
            ("SBIN", "ORB"): {
                "entry": 800.0,
                "sl": 804.0,
                "tp": 790.0,
                "quantity": 100,
                "order_id": "E-ORB",
                "direction": "SHORT",
                "executed_at": "2026-09-11 09:30:00",
            },
            ("SBIN", "BREAKOUT"): {
                "entry": 801.0,
                "sl": 797.0,
                "tp": 812.0,
                "quantity": 100,
                "order_id": "E-BRK",
                "direction": "LONG",
                "executed_at": "2026-09-11 09:00:00",
            },
        }
        with (
            _settings(strategy_profiles={"ORB": {}, "BREAKOUT": {}}),
            patch(
                "signal_engine.startup.fetch_last_entry_trade",
                side_effect=lambda sym, strat: rows.get((sym, strat)),
            ),
        ):
            found, strategy = startup._lookup_entry_trade("SBIN", broker_qty=100, is_long=True)
        assert strategy == "BREAKOUT"

    def test_no_rows_at_all_returns_none(self):
        with (
            _settings(strategy_profiles={"ORB": {}}),
            patch("signal_engine.startup.fetch_last_entry_trade", return_value=None),
        ):
            found, _ = startup._lookup_entry_trade("SBIN", broker_qty=100, is_long=True)
        assert found is None


class TestMultiLegRestoreSplitsEachEntry:
    """2026-09-24: restart merged two same-symbol entries (BREAKINGTRADE-WATCHLIST +
    BREAKINGTRADE) into ONE TrackedPosition under the broker's combined quantity, so the
    eventual close wrote one exit row under one entry's order_id for the FULL merged
    quantity — permanently orphaning the other entry's own exit. Confirmed for 7 symbols
    that day; APLAPOLLO's real numbers (WATCHLIST 46 @ 2176.1, BREAKINGTRADE 38 @ 2180.9,
    broker qty 84) are used here rather than synthetic values.
    """

    _APLAPOLLO_ROWS = {
        ("APLAPOLLO", "ORB"): None,
        ("APLAPOLLO", "BREAKINGTRADE"): {
            "entry": 2180.9,
            "sl": 2157.01,
            "tp": 2207.7,
            "quantity": 38,
            "order_id": "E-BT",
            "direction": "LONG",
            "executed_at": "2026-09-24T11:01:35",
            "sig_id": "",
        },
        ("APLAPOLLO", "BREAKINGTRADE-WATCHLIST"): {
            "entry": 2176.1,
            "sl": 2156.37,
            "tp": 2202.9,
            "quantity": 46,
            "order_id": "E-WL",
            "direction": "LONG",
            "executed_at": "2026-09-24T10:25:23",
            "sig_id": "",
        },
    }

    def test_quantities_summing_exactly_to_broker_qty_split_into_two_legs(self):
        with (
            _settings(strategy_profiles={"BREAKINGTRADE": {}, "BREAKINGTRADE-WATCHLIST": {}}),
            patch(
                "signal_engine.startup.fetch_last_entry_trade",
                side_effect=lambda sym, strat: self._APLAPOLLO_ROWS.get((sym, strat)),
            ),
        ):
            legs = startup._lookup_entry_legs("APLAPOLLO", broker_qty=84, is_long=True)
        assert [s for s, _ in legs] == ["BREAKINGTRADE-WATCHLIST", "BREAKINGTRADE"]  # oldest first
        assert legs[0][1]["quantity"] == 46
        assert legs[1][1]["quantity"] == 38

    def test_a_single_strategy_still_returns_one_leg(self):
        rows = {
            ("SBIN", "ORB"): {
                "entry": 800.0,
                "sl": 796.0,
                "tp": 810.0,
                "quantity": 50,
                "order_id": "E-1",
                "direction": "LONG",
            }
        }
        with (
            _settings(strategy_profiles={}),
            patch(
                "signal_engine.startup.fetch_last_entry_trade",
                side_effect=lambda sym, strat: rows.get((sym, strat)),
            ),
        ):
            legs = startup._lookup_entry_legs("SBIN", broker_qty=50, is_long=True)
        assert len(legs) == 1
        assert legs[0][1]["quantity"] == 50

    def test_quantities_not_summing_to_broker_qty_falls_back_to_single_best_match(self):
        """A partial fill, a manual adjustment, anything that doesn't cleanly explain the
        broker's quantity — must never be guessed at. Falls back to the existing
        single-best-match behaviour instead of fabricating a split."""
        rows = {
            ("SBIN", "ORB"): {
                "entry": 800.0,
                "sl": 796.0,
                "tp": 810.0,
                "quantity": 50,
                "order_id": "E-ORB",
                "direction": "LONG",
                "executed_at": "2026-09-24T09:00:00",
            },
            ("SBIN", "BREAKOUT"): {
                "entry": 801.0,
                "sl": 797.0,
                "tp": 812.0,
                "quantity": 40,
                "order_id": "E-BRK",
                "direction": "LONG",
                "executed_at": "2026-09-24T10:00:00",
            },
        }
        with (
            _settings(strategy_profiles={"ORB": {}, "BREAKOUT": {}}),
            patch(
                "signal_engine.startup.fetch_last_entry_trade",
                side_effect=lambda sym, strat: rows.get((sym, strat)),
            ),
        ):
            legs = startup._lookup_entry_legs("SBIN", broker_qty=999, is_long=True)
        assert len(legs) == 1  # 50 + 40 != 999 — no clean split, single best match instead

    def test_restore_registers_one_tracked_position_per_leg_with_its_own_quantity(self):
        from signal_engine.startup import _restore_tracker_positions

        class _Tracker:
            def __init__(self):
                self.registered = []

            def find_position(self, *_a):
                return None

            def register(self, pos):
                self.registered.append(pos)

        tracker = _Tracker()
        broker_positions = [{"symbol": "APLAPOLLO", "quantity": 84, "average_price": 2179.0}]
        legs = [
            (
                "BREAKINGTRADE-WATCHLIST",
                {
                    "entry": 2176.1,
                    "sl": 2156.37,
                    "tp": 2202.9,
                    "quantity": 46,
                    "order_id": "E-WL",
                    "sig_id": "",
                },
            ),
            (
                "BREAKINGTRADE",
                {
                    "entry": 2180.9,
                    "sl": 2157.01,
                    "tp": 2207.7,
                    "quantity": 38,
                    "order_id": "E-BT",
                    "sig_id": "",
                },
            ),
        ]
        with patch("signal_engine.startup._lookup_entry_legs", return_value=legs):
            restored = _restore_tracker_positions(
                tracker,
                broker_positions,
                "MIS",
                sl_orders={"APLAPOLLO": "SL-WL"},
            )

        assert restored == 2
        by_strategy = {p.strategy: p for p in tracker.registered}
        assert by_strategy["BREAKINGTRADE-WATCHLIST"].quantity == 46
        assert by_strategy["BREAKINGTRADE"].quantity == 38
        # Combined quantity matches the broker's — the bug this fixes silently doubled it
        # under the merged-position path (both legs carrying the broker's full 84).
        assert (
            by_strategy["BREAKINGTRADE-WATCHLIST"].quantity + by_strategy["BREAKINGTRADE"].quantity
            == 84
        )
        # Only the first (oldest) leg gets the one recoverable sl_order_id.
        assert by_strategy["BREAKINGTRADE-WATCHLIST"].sl_order_id == "SL-WL"
        assert by_strategy["BREAKINGTRADE"].sl_order_id == ""
