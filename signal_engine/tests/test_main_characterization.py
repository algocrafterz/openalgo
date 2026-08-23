"""Characterization tests for main.py exit and entry branches.

These tests pin the CURRENT observable behaviour of branches that had no
coverage before the maintainability refactor. They are deliberately written
against observable effects (which broker calls fire, in what order, which
notifications are sent, how tracker/risk state changes) rather than internal
structure, so they survive the code being reorganised.

If one of these fails during a refactor, behaviour changed — that is the signal.
"""

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine.main import _handle_entry, _handle_exit
from signal_engine.models import (
    Direction,
    Order,
    OrderStatus,
    Action,
    Signal,
    TradeResult,
)
from signal_engine.tracker import PositionTracker, TrackedPosition


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------

def _settings_stub(**overrides):
    """Stand-in for signal_engine.config.settings with every attribute main.py reads."""
    base = dict(
        exchange="NSE",
        product="MIS",
        mis_margin_pct=0.20,
        strategy_profiles={"ORB": {"tp_levels": {"TP1": 1.0}}},
        bracket_tp_exit_retries=1,
        bracket_retry_delay=0.0,
        bracket_enabled=True,
        bracket_cnc_sl_enabled=False,
        max_trades_per_day=8,
        tp1_runner_sl_buffer=0.1,
        broker_mis_rejected=set(),
        min_capital_for_entry=0.0,
        risk_per_trade=0.01,
        max_sl_pct_for_sizing=0.0,
        slippage_factor=0.10,
        test_qty_cap=0,
        allow_off_hours_testing=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _order_stub():
    return Order(
        symbol="RELIANCE", exchange="NSE", action=Action.SELL, quantity=1,
        price=0.0, order_type="MARKET", product="MIS", strategy_tag="ORB",
    )


def _ok(order_id="X1"):
    return TradeResult(order_id=order_id, status=OrderStatus.SUCCESS, message="ok")


def _fail(msg="broker down"):
    return TradeResult(order_id="", status=OrderStatus.ERROR, message=msg)


def _risk_stub():
    """Mock RiskEngine with the attributes main.py and PositionTracker read."""
    risk = MagicMock()
    risk.open_positions = 0
    risk.max_open_positions = 2
    risk._last_known_capital = 100000.0
    risk.get_sizing_capital.return_value = 100000.0
    risk.calculate_quantity.return_value = 10
    risk.check_exposure.return_value = True
    risk.can_trade_symbol.return_value = True
    risk.can_trade_sector.return_value = True
    risk.capacity_status.return_value = "1/2 slots"
    return risk


def _real_tracker():
    """PositionTracker backed by a mock risk engine — real register/unregister/counters."""
    return PositionTracker(_risk_stub(), poll_interval=30)


def _position(**overrides) -> TrackedPosition:
    defaults = dict(
        symbol="RELIANCE", strategy="ORB", exchange="NSE", product="MIS",
        entry_price=2500.0, quantity=50, sl=2485.0, tp=2540.0,
        direction=Direction.LONG, entry_order_id="E1", sl_order_id="SL1",
        fill_price=2500.0,
    )
    defaults.update(overrides)
    return TrackedPosition(**defaults)


def _exit_signal(**overrides) -> Signal:
    defaults = dict(
        strategy="ORB", direction=Direction.EXIT, symbol="RELIANCE",
        entry=0.0, sl=0.0, tp=0.0, raw_message="exit",
    )
    defaults.update(overrides)
    return Signal(**defaults)


def _entry_signal(**overrides) -> Signal:
    defaults = dict(
        strategy="ORB", direction=Direction.LONG, symbol="RELIANCE",
        entry=2500.0, sl=2485.0, tp=2540.0, raw_message="entry",
    )
    defaults.update(overrides)
    return Signal(**defaults)


class Harness:
    """Patched view of main.py's module-level collaborators."""

    def __init__(self, stack: ExitStack, settings, tracker, risk_engine):
        self.stack = stack
        self.settings = settings
        self.tracker = tracker
        self.risk = risk_engine
        self.calls: list[str] = []

    def record(self, name):
        """Wrap an AsyncMock so its invocation order is captured in self.calls."""
        def _hook(*_a, **_kw):
            self.calls.append(name)
        return _hook


def harness(settings=None, tracker=None, **stubs):
    """Patch every main.py collaborator. Returns an ExitStack-managed Harness.

    Any keyword overrides a default stub, e.g. send_order=AsyncMock(...).
    """
    settings = settings or _settings_stub()
    tracker = tracker if tracker is not None else _real_tracker()
    risk_engine = tracker._risk_engine if isinstance(tracker, PositionTracker) else _risk_stub()

    defaults = dict(
        cancel_order=AsyncMock(return_value=True),
        send_order=AsyncMock(return_value=_ok("EX1")),
        fetch_realised_pnl=AsyncMock(return_value=0.0),
        fetch_open_position=AsyncMock(return_value=0),
        fetch_order_status=AsyncMock(return_value="complete"),
        fetch_order_fill_price=AsyncMock(return_value=2500.0),
        fetch_available_capital=AsyncMock(return_value=100000.0),
        fetch_trading_mode=AsyncMock(return_value=("live", False)),
        fetch_margin=AsyncMock(return_value=10000.0),
        place_sl_order=AsyncMock(return_value=_ok("SL2")),
        send_bracket_legs=AsyncMock(return_value=(_ok("SL2"), None)),
        fetch_last_entry_trade=MagicMock(return_value=None),
        build_exit_order=MagicMock(return_value=_order_stub()),
        build_order=MagicMock(return_value=_order_stub()),
        save=MagicMock(),
        _is_be_series=MagicMock(return_value=False),
    )
    defaults.update(stubs)

    stack = ExitStack()
    h = Harness(stack, settings, tracker, risk_engine)
    stack.enter_context(patch("signal_engine.main.settings", settings))
    stack.enter_context(patch("signal_engine.main.tracker", tracker))
    stack.enter_context(patch("signal_engine.main.risk_engine", risk_engine))
    # Close notifications are emitted by PositionTracker.book_close, entry/partial
    # notifications by main — patch both module references with the same mock so a
    # test can assert on either without caring which module owns the call.
    h.notifier = stack.enter_context(
        patch("signal_engine.main.notifier", new_callable=AsyncMock)
    )
    stack.enter_context(patch("signal_engine.tracker.notifier", h.notifier))
    for name, stub in defaults.items():
        setattr(h, name, stack.enter_context(patch(f"signal_engine.main.{name}", stub)))
    return h


# --------------------------------------------------------------------------
# Exit path: guards that abort before any broker order
# --------------------------------------------------------------------------

class TestExitGuards:
    @pytest.mark.asyncio
    async def test_time_exit_in_progress_aborts_exit(self):
        """A TP signal arriving during time_exit_all() must place no order at all."""
        tracker = _real_tracker()
        tracker._time_exit_active = True
        tracker.register(_position())

        h = harness(tracker=tracker)
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="TP1"))
            h.send_order.assert_not_awaited()
            h.cancel_order.assert_not_awaited()
        # position stays registered — time_exit owns the close
        assert tracker.find_position("RELIANCE", "ORB") is not None

    @pytest.mark.asyncio
    async def test_duplicate_exit_signal_is_dropped(self):
        """exit_pending guards against a second concurrent TP alert."""
        tracker = _real_tracker()
        tracker.register(_position(exit_pending=True))

        h = harness(tracker=tracker)
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="TP1"))
            h.send_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_position_anywhere_notifies_and_stops(self):
        """Tracker miss + broker flat -> notify_exit_no_position, no order."""
        h = harness(tracker=_real_tracker(), fetch_open_position=AsyncMock(return_value=0))
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="TP1"))
            h.notifier.notify_exit_no_position.assert_awaited_once_with("RELIANCE", "ORB")
            h.send_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_broker_api_error_treated_as_no_position(self):
        """fetch_open_position returning -1 (API error) must NOT place an exit order."""
        h = harness(tracker=_real_tracker(), fetch_open_position=AsyncMock(return_value=-1))
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="TP1"))
            h.notifier.notify_exit_no_position.assert_awaited_once()
            h.send_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejected_entry_blocks_exit_and_releases_slot(self):
        """Phantom-exit guard: never SELL against an entry the broker rejected."""
        tracker = _real_tracker()
        tracker.register(_position(fill_price=0.0, entry_order_id="E1", sl_order_id="SL1"))

        h = harness(tracker=tracker, fetch_order_status=AsyncMock(return_value="REJECTED"))
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="TP1"))
            h.send_order.assert_not_awaited()
            h.cancel_order.assert_awaited_once_with("SL1", "ORB")
            h.risk.record_rejection.assert_called_once_with(symbol="RELIANCE")
            h.notifier.notify_orphaned_position.assert_awaited_once()
        assert tracker.find_position("RELIANCE", "ORB") is None


# --------------------------------------------------------------------------
# Exit path: engine-restart recovery
# --------------------------------------------------------------------------

class TestExitRestartRecovery:
    @pytest.mark.asyncio
    async def test_recovers_entry_context_from_trades_db(self):
        """Tracker lost state: entry/sl/tp/order_id come from the audit trail."""
        recovered = {"entry": 2500.0, "sl": 2485.0, "tp": 2540.0, "order_id": "E9"}
        h = harness(
            tracker=_real_tracker(),
            fetch_open_position=AsyncMock(return_value=40),
            fetch_last_entry_trade=MagicMock(return_value=recovered),
        )
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="TP1"))
            h.fetch_last_entry_trade.assert_called_once_with("RELIANCE", "ORB")
            # exit sized from the broker's reported quantity
            assert h.build_exit_order.call_args.kwargs["quantity"] == 40

    @pytest.mark.asyncio
    async def test_negative_broker_qty_recovers_as_short(self):
        """A negative positionbook qty means SHORT; qty is the absolute value."""
        h = harness(
            tracker=_real_tracker(),
            fetch_open_position=AsyncMock(return_value=-30),
            fetch_last_entry_trade=MagicMock(return_value=None),
        )
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="TP1"))
            kwargs = h.build_exit_order.call_args.kwargs
            assert kwargs["quantity"] == 30
            assert kwargs["direction"] == Direction.SHORT

    @pytest.mark.asyncio
    async def test_missing_audit_trail_falls_back_to_signal_prices(self):
        """No trades.db row: the (zero) signal prices are used, exit still proceeds."""
        h = harness(
            tracker=_real_tracker(),
            fetch_open_position=AsyncMock(return_value=25),
            fetch_last_entry_trade=MagicMock(return_value=None),
        )
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="TP1"))
            h.send_order.assert_awaited()


# --------------------------------------------------------------------------
# Exit path: SL HIT reconcile (broker already closed the position)
# --------------------------------------------------------------------------

class TestSlHitReconcile:
    @pytest.mark.asyncio
    async def test_sl_hit_places_no_order_and_closes_books(self):
        """tp_level='SL' means the broker SL-M already filled — never send another SELL."""
        tracker = _real_tracker()
        tracker.register(_position())
        tracker._last_realised_pnl = 0.0

        h = harness(tracker=tracker, fetch_realised_pnl=AsyncMock(return_value=-750.0))
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="SL"))

            h.send_order.assert_not_awaited()
            h.cancel_order.assert_awaited_once_with("SL1", "ORB")
            h.risk.record_close.assert_called_once_with(pnl=-750.0, symbol="RELIANCE")
            h.notifier.notify_position_closed.assert_awaited_once()

        assert tracker.find_position("RELIANCE", "ORB") is None
        assert tracker._day_trades == 1
        assert tracker._day_losses == 1
        assert len(tracker._completed_trades) == 1
        assert tracker._completed_trades[0].exit_types == ["SL"]

    @pytest.mark.asyncio
    async def test_sl_hit_exit_price_recorded_as_stop_price(self):
        tracker = _real_tracker()
        tracker.register(_position(sl=2485.0))
        h = harness(tracker=tracker, fetch_realised_pnl=AsyncMock(return_value=-750.0))
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="SL"))
        assert tracker._completed_trades[0].exit_price == 2485.0


# --------------------------------------------------------------------------
# Exit path: order placement, retries, failure handling
# --------------------------------------------------------------------------

class TestExitOrderPlacement:
    @pytest.mark.asyncio
    async def test_failed_sl_cancel_does_not_block_the_exit(self):
        """Losing the SL cancel is logged but the exit MUST still be attempted."""
        tracker = _real_tracker()
        tracker.register(_position())
        h = harness(tracker=tracker, cancel_order=AsyncMock(return_value=False))
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="TP1"))
            h.send_order.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_exit_retries_then_succeeds(self):
        """A transient failure is retried up to bracket_tp_exit_retries."""
        tracker = _real_tracker()
        tracker.register(_position())
        send = AsyncMock(side_effect=[_fail("timeout"), _ok("EX2")])
        h = harness(
            tracker=tracker,
            settings=_settings_stub(bracket_tp_exit_retries=3, bracket_retry_delay=0.0),
            send_order=send,
        )
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="TP1"))
        assert send.await_count == 2
        assert tracker.find_position("RELIANCE", "ORB") is None

    @pytest.mark.asyncio
    async def test_exit_exhausts_retries_and_broker_flat_cleans_up_silently(self):
        """All retries failed but broker qty is 0 -> SL fired; clean up, don't alarm."""
        tracker = _real_tracker()
        tracker.register(_position())
        h = harness(
            tracker=tracker,
            settings=_settings_stub(bracket_tp_exit_retries=2, bracket_retry_delay=0.0),
            send_order=AsyncMock(return_value=_fail()),
            fetch_open_position=AsyncMock(return_value=0),
        )
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="TP1"))
            h.notifier.notify_exit_failed.assert_not_awaited()
            h.risk.record_close.assert_called_once_with(pnl=0.0, symbol="RELIANCE")
        assert tracker.find_position("RELIANCE", "ORB") is None

    @pytest.mark.asyncio
    async def test_exit_failure_with_open_position_alerts_and_allows_retry(self):
        """Broker still holds the position -> alert the trader and clear exit_pending."""
        tracker = _real_tracker()
        pos = _position()
        tracker.register(pos)
        h = harness(
            tracker=tracker,
            settings=_settings_stub(bracket_tp_exit_retries=1, bracket_retry_delay=0.0),
            send_order=AsyncMock(return_value=_fail("rejected")),
            fetch_open_position=AsyncMock(return_value=50),
        )
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="TP1"))
            h.notifier.notify_exit_failed.assert_awaited_once()
        assert tracker.find_position("RELIANCE", "ORB") is pos
        assert pos.exit_pending is False


# --------------------------------------------------------------------------
# Exit path: partial exits
# --------------------------------------------------------------------------

class TestPartialExitEdges:
    @pytest.mark.asyncio
    async def test_invalid_remainder_converts_to_full_exit(self):
        """Defensive branch: a partial that leaves <=0 shares closes the trade instead."""
        tracker = _real_tracker()
        tracker.register(_position(quantity=1))
        h = harness(
            tracker=tracker,
            settings=_settings_stub(
                strategy_profiles={"ORB": {"tp_levels": {"TP1": 0.5}}},
            ),
            fetch_realised_pnl=AsyncMock(return_value=200.0),
        )
        with h.stack:
            # qty=1, 50% -> floor(0.5)=0 -> clamped to 1 -> equals quantity -> full exit
            await _handle_exit(_exit_signal(tp_level="TP1"))
            h.notifier.notify_position_closed.assert_awaited_once()
            h.notifier.notify_partial_exit.assert_not_awaited()
        assert tracker.find_position("RELIANCE", "ORB") is None

    @pytest.mark.asyncio
    async def test_pinescript_exit_pct_overrides_config(self):
        """signal.exit_qty_pct is the source of truth over strategy_profiles."""
        tracker = _real_tracker()
        tracker.register(_position(quantity=100))
        h = harness(
            tracker=tracker,
            settings=_settings_stub(
                strategy_profiles={"ORB": {"tp_levels": {"TP1": 1.0}}},
            ),
            fetch_realised_pnl=AsyncMock(return_value=300.0),
        )
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="TP1", exit_qty_pct=0.25))
            assert h.build_exit_order.call_args.kwargs["quantity"] == 25
            h.notifier.notify_partial_exit.assert_awaited_once()
        pos = tracker.find_position("RELIANCE", "ORB")
        assert pos is not None and pos.quantity == 75

    @pytest.mark.asyncio
    async def test_partial_exit_failed_sl_replacement_alerts(self):
        """Runner left unprotected after a failed SL re-place must raise an alert."""
        tracker = _real_tracker()
        tracker.register(_position(quantity=100))
        h = harness(
            tracker=tracker,
            fetch_realised_pnl=AsyncMock(return_value=300.0),
            place_sl_order=AsyncMock(return_value=_fail("sl rejected")),
        )
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="TP1", exit_qty_pct=0.5))
            h.notifier.notify_sl_failed.assert_awaited_once()
        pos = tracker.find_position("RELIANCE", "ORB")
        assert pos is not None and pos.sl_order_id == ""


# --------------------------------------------------------------------------
# Entry path: pre-flight rejections
# --------------------------------------------------------------------------

class TestEntryPreflightRejections:
    @pytest.mark.asyncio
    async def test_be_series_symbol_rejected_for_mis(self):
        h = harness(_is_be_series=MagicMock(return_value=True))
        with h.stack:
            await _handle_entry(_entry_signal())
            h.notifier.notify_order_rejected.assert_awaited_once()
            h.send_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_broker_mis_reject_list_short_circuits(self):
        h = harness(settings=_settings_stub(broker_mis_rejected={"RELIANCE"}))
        with h.stack:
            await _handle_entry(_entry_signal())
            h.notifier.notify_order_rejected.assert_awaited_once()
            h.send_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_be_series_check_skipped_for_cnc(self):
        """T2T restriction only applies to MIS — CNC entries bypass the lookup."""
        h = harness(_is_be_series=MagicMock(return_value=True))
        with h.stack:
            await _handle_entry(_entry_signal(product="CNC"))
            h._is_be_series.assert_not_called()

    @pytest.mark.asyncio
    async def test_capital_below_floor_blocks_entry(self):
        h = harness(
            settings=_settings_stub(min_capital_for_entry=50000.0),
            fetch_available_capital=AsyncMock(return_value=20000.0),
        )
        with h.stack:
            await _handle_entry(_entry_signal())
            h.notifier.notify_order_rejected.assert_awaited_once()
            h.send_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_zero_capital_aborts_without_notification(self):
        """Capital fetch failure is a log-only abort — no Telegram noise."""
        h = harness(fetch_available_capital=AsyncMock(return_value=0.0))
        with h.stack:
            await _handle_entry(_entry_signal())
            h.notifier.notify_order_rejected.assert_not_awaited()
            h.send_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sizing_zero_rejects_entry(self):
        h = harness()
        h.risk.calculate_quantity.return_value = 0
        h.risk.get_sizing_capital.return_value = 100000.0
        with h.stack:
            await _handle_entry(_entry_signal())
            h.notifier.notify_order_rejected.assert_awaited_once()
            h.send_order.assert_not_awaited()


# --------------------------------------------------------------------------
# Entry path: margin handling
# --------------------------------------------------------------------------

class TestEntryMargin:
    @pytest.mark.asyncio
    async def test_margin_api_error_aborts_the_trade(self):
        from signal_engine.main import MarginAPIError

        h = harness()
        h.risk.get_sizing_capital.return_value = 100000.0
        h.risk.calculate_quantity.return_value = 10
        with h.stack:
            with patch(
                "signal_engine.main.adjust_qty_for_margin",
                new=AsyncMock(side_effect=MarginAPIError("span down")),
            ):
                await _handle_entry(_entry_signal())
            h.notifier.notify_order_rejected.assert_awaited_once()
            h.send_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_margin_floor_rejects_when_qty_scales_to_zero(self):
        h = harness()
        h.risk.get_sizing_capital.return_value = 100000.0
        h.risk.calculate_quantity.return_value = 10
        with h.stack:
            with patch(
                "signal_engine.main.adjust_qty_for_margin", new=AsyncMock(return_value=0)
            ):
                await _handle_entry(_entry_signal())
            h.notifier.notify_order_rejected.assert_awaited_once()
            h.send_order.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_analyze_mode_skips_margin_check_entirely(self):
        h = harness(fetch_trading_mode=AsyncMock(return_value=("analyze", True)))
        h.risk.get_sizing_capital.return_value = 100000.0
        h.risk.calculate_quantity.return_value = 10
        with h.stack:
            with patch(
                "signal_engine.main.adjust_qty_for_margin", new=AsyncMock()
            ) as adj:
                await _handle_entry(_entry_signal())
            adj.assert_not_awaited()
            h.send_order.assert_awaited()

    @pytest.mark.asyncio
    async def test_test_qty_cap_clamps_quantity(self):
        h = harness(settings=_settings_stub(test_qty_cap=3))
        h.risk.get_sizing_capital.return_value = 100000.0
        h.risk.calculate_quantity.return_value = 50
        with h.stack:
            with patch(
                "signal_engine.main.adjust_qty_for_margin", new=AsyncMock(return_value=50)
            ):
                await _handle_entry(_entry_signal())
            assert h.build_order.call_args[0][1] == 3


# --------------------------------------------------------------------------
# Entry path: post-fill handling
# --------------------------------------------------------------------------

class TestEntryPostFill:
    def _sized(self, h, qty=10):
        h.risk.get_sizing_capital.return_value = 100000.0
        h.risk.calculate_quantity.return_value = qty
        return patch(
            "signal_engine.main.adjust_qty_for_margin", new=AsyncMock(return_value=qty)
        )

    @pytest.mark.asyncio
    async def test_bracket_sl_failure_alerts_but_keeps_position(self):
        tracker = _real_tracker()
        h = harness(
            tracker=tracker,
            send_bracket_legs=AsyncMock(return_value=(_fail("sl rejected"), None)),
        )
        with h.stack, self._sized(h):
            await _handle_entry(_entry_signal())
            h.notifier.notify_sl_failed.assert_awaited_once()
        assert tracker.find_position("RELIANCE", "ORB") is not None

    @pytest.mark.asyncio
    async def test_cnc_entry_skips_bracket_when_disabled(self):
        h = harness(settings=_settings_stub(bracket_cnc_sl_enabled=False))
        with h.stack, self._sized(h):
            await _handle_entry(_entry_signal(product="CNC"))
            h.send_bracket_legs.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_off_hours_analyze_mode_overrides_mis_to_cnc(self):
        h = harness(
            settings=_settings_stub(allow_off_hours_testing=True),
            fetch_trading_mode=AsyncMock(return_value=("analyze", True)),
        )
        with h.stack, self._sized(h):
            await _handle_entry(_entry_signal(product="MIS"))
            assert h.build_order.call_args.kwargs["product"] == "CNC"

    @pytest.mark.asyncio
    async def test_rejected_entry_order_notifies_and_tracks_nothing(self):
        tracker = _real_tracker()
        h = harness(tracker=tracker, send_order=AsyncMock(return_value=_fail("no funds")))
        with h.stack, self._sized(h):
            await _handle_entry(_entry_signal())
            h.notifier.notify_order_rejected.assert_awaited_once()
        assert tracker.tracked_count == 0

    @pytest.mark.asyncio
    async def test_fill_price_unavailable_still_registers_position(self):
        tracker = _real_tracker()
        h = harness(tracker=tracker, fetch_order_fill_price=AsyncMock(return_value=None))
        with h.stack, self._sized(h):
            await _handle_entry(_entry_signal())
        pos = tracker.find_position("RELIANCE", "ORB")
        assert pos is not None and pos.fill_price == 0.0


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

class TestAdjustQtyForMargin:
    @pytest.mark.asyncio
    async def test_equity_within_margin_returns_full_qty(self):
        from signal_engine.main import adjust_qty_for_margin

        sig = _entry_signal(exchange="NSE")
        with patch("signal_engine.main.settings", _settings_stub(mis_margin_pct=0.20)):
            # 10 * 2500 * 0.20 = 5,000 <= 50,000
            assert await adjust_qty_for_margin(sig, 10, 50000.0) == 10

    @pytest.mark.asyncio
    async def test_equity_over_margin_is_binary_reject_not_scaled(self):
        """Equity never scales down — a part-sized position is not worth the costs."""
        from signal_engine.main import adjust_qty_for_margin

        sig = _entry_signal(exchange="NSE")
        with patch("signal_engine.main.settings", _settings_stub(mis_margin_pct=0.20)):
            # 100 * 2500 * 0.20 = 50,000 > 10,000
            assert await adjust_qty_for_margin(sig, 100, 10000.0) == 0

    @pytest.mark.asyncio
    async def test_derivatives_within_margin_returns_full_qty(self):
        from signal_engine.main import adjust_qty_for_margin

        sig = _entry_signal(exchange="NFO")
        with (
            patch("signal_engine.main.settings", _settings_stub()),
            patch("signal_engine.main.fetch_margin", new=AsyncMock(return_value=8000.0)),
        ):
            assert await adjust_qty_for_margin(sig, 50, 20000.0) == 50

    @pytest.mark.asyncio
    async def test_derivatives_over_margin_scales_proportionally(self):
        """Unlike equity, derivatives DO scale down — margin is linear with qty."""
        from signal_engine.main import adjust_qty_for_margin

        sig = _entry_signal(exchange="NFO")
        with (
            patch("signal_engine.main.settings", _settings_stub()),
            patch("signal_engine.main.fetch_margin", new=AsyncMock(return_value=40000.0)),
        ):
            # floor(50 * 10000 / 40000) = 12
            assert await adjust_qty_for_margin(sig, 50, 10000.0) == 12

    @pytest.mark.asyncio
    async def test_derivatives_margin_api_error_propagates(self):
        from signal_engine.main import MarginAPIError, adjust_qty_for_margin

        sig = _entry_signal(exchange="NFO")
        with (
            patch("signal_engine.main.settings", _settings_stub()),
            patch(
                "signal_engine.main.fetch_margin",
                new=AsyncMock(side_effect=MarginAPIError("span down")),
            ),
        ):
            with pytest.raises(MarginAPIError):
                await adjust_qty_for_margin(sig, 50, 10000.0)


class TestResolveExitQty:
    def _resolve(self, signal, pos, settings=None):
        from signal_engine.main import _resolve_exit_qty

        with patch("signal_engine.main.settings", settings or _settings_stub()):
            return _resolve_exit_qty(signal, pos)

    def test_no_hint_anywhere_defaults_to_full_exit(self):
        pos = _position(quantity=50)
        assert self._resolve(_exit_signal(), pos) == (50, True)

    def test_pct_below_one_share_clamps_to_one(self):
        pos = _position(quantity=3)
        # floor(3 * 0.1) = 0 -> clamped to 1
        assert self._resolve(_exit_signal(exit_qty_pct=0.1), pos) == (1, False)

    def test_pct_at_or_above_full_qty_is_a_full_exit(self):
        pos = _position(quantity=2)
        # floor(2 * 0.9) = 1 -> partial; but 0.99 of 2 also floors to 1
        assert self._resolve(_exit_signal(exit_qty_pct=1.0), pos) == (2, True)

    def test_config_tp_levels_used_when_signal_has_no_pct(self):
        pos = _position(quantity=100)
        settings = _settings_stub(strategy_profiles={"ORB": {"tp_levels": {"TP1": 0.4}}})
        assert self._resolve(_exit_signal(tp_level="TP1"), pos, settings) == (40, False)

    def test_unknown_tp_level_in_config_falls_back_to_full_exit(self):
        pos = _position(quantity=100)
        settings = _settings_stub(strategy_profiles={"ORB": {"tp_levels": {"TP1": 0.4}}})
        assert self._resolve(_exit_signal(tp_level="TP9"), pos, settings) == (100, True)


class TestComputeNextTpEdges:
    def test_zero_r_distance_returns_none(self):
        from signal_engine.main import compute_next_tp

        pos = _position(entry_price=2500.0, tp=2500.0)
        assert compute_next_tp(pos, "TP1") is None

    def test_terminal_level_returns_none(self):
        from signal_engine.main import compute_next_tp

        assert compute_next_tp(_position(), "TP2") is None

    def test_short_next_tp_is_below_entry(self):
        from signal_engine.main import compute_next_tp

        pos = _position(direction=Direction.SHORT, entry_price=2500.0, tp=2460.0)
        label, price = compute_next_tp(pos, "TP1")
        assert label == "TP1.5"
        assert price == pytest.approx(2440.0)


class TestBeSeriesLookup:
    def test_db_error_fails_open_and_allows_the_trade(self):
        """A bad symtoken lookup must never silently block a valid trade."""
        from signal_engine.main import _is_be_series

        with patch("signal_engine.main.sqlite3.connect", side_effect=OSError("locked")):
            assert _is_be_series("RELIANCE", "NSE") is False


class TestSizingLogBranches:
    @pytest.mark.asyncio
    async def test_wide_sl_is_capped_for_sizing(self):
        """max_sl_pct_for_sizing caps the risk-per-share used in the sizing log."""
        h = harness(settings=_settings_stub(max_sl_pct_for_sizing=0.002))
        with h.stack:
            with patch(
                "signal_engine.main.adjust_qty_for_margin", new=AsyncMock(return_value=10)
            ):
                # risk/share = 15.0, cap = 2500 * 0.002 = 5.0 -> capped
                await _handle_entry(_entry_signal())
            h.send_order.assert_awaited()


class TestPartialExitDefensiveGuard:
    @pytest.mark.asyncio
    async def test_non_positive_remainder_is_converted_to_a_full_exit(self):
        """Defensive branch: _resolve_exit_qty should never yield this, but if it does
        the position must be closed and unregistered rather than left with qty<=0."""
        tracker = _real_tracker()
        tracker.register(_position(quantity=10))
        h = harness(tracker=tracker, fetch_realised_pnl=AsyncMock(return_value=400.0))
        with h.stack:
            # Force the impossible combination: partial flag with a full-size qty
            with patch("signal_engine.main._resolve_exit_qty", return_value=(10, False)):
                await _handle_exit(_exit_signal(tp_level="TP1"))
            h.notifier.notify_position_closed.assert_awaited_once()
            h.notifier.notify_partial_exit.assert_not_awaited()
            h.risk.record_close.assert_called_once_with(pnl=400.0, symbol="RELIANCE")
        assert tracker.find_position("RELIANCE", "ORB") is None
        assert tracker._day_trades == 1


class TestFullExitDayContext:
    """The day-context line in a close notification must count the closing trade.

    Before the exit paths were unified, the full-exit path computed this with
    projected_day_context() because record_exit ran afterwards. book_close now
    records first and reads day_context_line(). These pin that the resulting
    notification is unchanged.
    """

    @pytest.mark.asyncio
    async def test_winning_full_exit_counts_itself_in_day_context(self):
        tracker = _real_tracker()
        tracker.register(_position(quantity=50))
        h = harness(tracker=tracker, fetch_realised_pnl=AsyncMock(return_value=900.0))
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="TP1"))
            # the day context is built from the counters *after* this trade is recorded
            ctx_args = h.notifier.format_day_context.call_args.kwargs
        assert tracker._day_trades == 1
        assert tracker._day_wins == 1
        assert ctx_args["day_trades"] == 1
        assert ctx_args["day_wins"] == 1
        assert ctx_args["day_losses"] == 0

    @pytest.mark.asyncio
    async def test_losing_full_exit_counts_itself_as_a_loss(self):
        tracker = _real_tracker()
        tracker.register(_position(quantity=50))
        h = harness(tracker=tracker, fetch_realised_pnl=AsyncMock(return_value=-400.0))
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="TP1"))
        assert tracker._day_trades == 1
        assert tracker._day_losses == 1
        assert tracker._day_pnl == -400.0

    @pytest.mark.asyncio
    async def test_partial_exit_does_not_count_a_trade(self):
        """Only the final close counts; a partial leg banks P&L only."""
        tracker = _real_tracker()
        tracker.register(_position(quantity=100))
        h = harness(tracker=tracker, fetch_realised_pnl=AsyncMock(return_value=300.0))
        with h.stack:
            await _handle_exit(_exit_signal(tp_level="TP1", exit_qty_pct=0.5))
        assert tracker._day_trades == 0
        assert tracker._day_pnl == 300.0
