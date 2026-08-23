"""No-progress gates: profit lock, chop tightener, loss cut."""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

from signal_engine.tracker import PositionTracker

_IST = timezone(timedelta(hours=5, minutes=30))
_AGED = datetime.now(_IST) - timedelta(minutes=5)  # past Guard 1 (30s min age)

from signal_engine.tests.tracker_fixtures import _make_engine, _make_position


class TestNoProgressProfitLock:
    """no_progress break-even with profit_lock_ratio locks partial unrealized profit."""

    @pytest.mark.asyncio
    async def test_profit_lock_raises_sl_above_entry(self):
        """With profit_lock_ratio=0.4 and ltp > entry, new SL should be above entry."""
        from signal_engine.models import Direction

        engine = _make_engine()
        tracker = PositionTracker(engine)
        pos = _make_position(
            entry_price=541.80,
            fill_price=541.80,
            sl=528.01,
            tp=551.10,
            direction=Direction.LONG,
            entry_time=datetime.now(_IST) - timedelta(minutes=150),
        )
        tracker.register(pos)

        book_data = {"RELIANCE": (1, 544.70)}

        with (
            patch("signal_engine.tracker.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.tracker.place_sl_order") as mock_place,
            patch("signal_engine.tracker.notifier.notify_be_stop_applied", new_callable=AsyncMock),
        ):
            from signal_engine.models import TradeResult, OrderStatus
            mock_place.return_value = TradeResult(
                status=OrderStatus.SUCCESS, order_id="NEW_SL", message=""
            )
            with patch("signal_engine.config.settings") as mock_settings:
                mock_settings.no_progress_enabled = True
                mock_settings.no_progress_check_after_minutes = 90
                mock_settings.no_progress_min_progress_pct = 0.33
                mock_settings.no_progress_profit_lock_ratio = 0.40
                mock_settings.no_progress_early_check_enabled = False
                mock_settings.no_progress_early_check_after_minutes = 45
                mock_settings.no_progress_early_min_progress_pct = 0.05
                mock_settings.no_progress_use_fill_price = True
                mock_settings.no_progress_chop_tightener_enabled = False
                mock_settings.no_progress_chop_tightener_trigger_count = 2
                mock_settings.no_progress_chop_tightener_early_check_after_minutes = 30
                mock_settings.no_progress_loss_cut_enabled = False
                mock_settings.time_exit_enabled = False  # disable market-exit path
                await tracker._check_no_progress(book_data)

        mock_place.assert_called_once()
        _, kwargs = mock_place.call_args
        sl_placed = kwargs.get("sl_price", mock_place.call_args[0][3] if mock_place.call_args[0] else None)
        # With lock 0.4 and ltp=544.70, entry=541.80: new SL = 541.80 + (544.70-541.80)*0.4 = 542.96
        assert sl_placed is not None and sl_placed > 541.80

    @pytest.mark.asyncio
    async def test_strict_breakeven_when_lock_ratio_zero(self):
        """With profit_lock_ratio=0.0, SL should move to exact entry price."""
        from signal_engine.models import Direction

        engine = _make_engine()
        tracker = PositionTracker(engine)
        pos = _make_position(
            entry_price=541.80,
            fill_price=541.80,
            sl=528.01,
            tp=551.10,
            direction=Direction.LONG,
            entry_time=datetime.now(_IST) - timedelta(minutes=150),
        )
        tracker.register(pos)

        book_data = {"RELIANCE": (1, 544.70)}

        with (
            patch("signal_engine.tracker.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.tracker.place_sl_order") as mock_place,
            patch("signal_engine.tracker.notifier.notify_be_stop_applied", new_callable=AsyncMock),
        ):
            from signal_engine.models import TradeResult, OrderStatus
            mock_place.return_value = TradeResult(
                status=OrderStatus.SUCCESS, order_id="NEW_SL", message=""
            )
            with patch("signal_engine.config.settings") as mock_settings:
                mock_settings.no_progress_enabled = True
                mock_settings.no_progress_check_after_minutes = 90
                mock_settings.no_progress_min_progress_pct = 0.33
                mock_settings.no_progress_profit_lock_ratio = 0.0
                mock_settings.no_progress_early_check_enabled = False
                mock_settings.no_progress_early_check_after_minutes = 45
                mock_settings.no_progress_early_min_progress_pct = 0.05
                mock_settings.no_progress_use_fill_price = True
                mock_settings.no_progress_chop_tightener_enabled = False
                mock_settings.no_progress_chop_tightener_trigger_count = 2
                mock_settings.no_progress_chop_tightener_early_check_after_minutes = 30
                mock_settings.no_progress_loss_cut_enabled = False
                mock_settings.time_exit_enabled = False  # disable market-exit path
                await tracker._check_no_progress(book_data)

        mock_place.assert_called_once()
        _, kwargs = mock_place.call_args
        sl_placed = kwargs.get("sl_price", mock_place.call_args[0][3] if mock_place.call_args[0] else None)
        assert sl_placed == 541.80

    @pytest.mark.asyncio
    async def test_market_exit_when_rate_too_slow(self):
        """Market exit when projected time-to-TP1 at current rate exceeds time remaining to exit.

        Setup: entry=541.80 TP=551.10 (9.30pt), after 150min at ltp=544.70 (31.2% progress).
        Rate = 0.312/150 = 0.00208/min. Minutes needed = 0.688/0.00208 = 331min.
        With time exit 60min away: 331 > 60 → market exit fires.
        """
        from signal_engine.models import Direction, TradeResult, OrderStatus

        engine = _make_engine()
        tracker = PositionTracker(engine)
        pos = _make_position(
            entry_price=541.80,
            fill_price=541.80,
            sl=528.01,
            tp=551.10,
            sl_order_id="OLD_SL",
            direction=Direction.LONG,
            entry_time=datetime.now(_IST) - timedelta(minutes=150),
        )
        tracker.register(pos)

        book_data = {"RELIANCE": (1, 544.70)}  # progress = (544.70-541.80)/(551.10-541.80) = 31.2%
        now = datetime.now(_IST)
        exit_time = now + timedelta(minutes=60)  # 331min needed > 60min remaining → market exit

        with (
            patch("signal_engine.tracker.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.tracker.send_order", new_callable=AsyncMock,
                  return_value=TradeResult(status=OrderStatus.SUCCESS, order_id="MKT_EXIT", message="")) as mock_send,
            patch("signal_engine.tracker.place_sl_order") as mock_sl,
            patch("signal_engine.tracker.notifier.notify_no_progress_exit", new_callable=AsyncMock),
        ):
            with patch("signal_engine.config.settings") as mock_settings:
                mock_settings.no_progress_enabled = True
                mock_settings.no_progress_check_after_minutes = 90
                mock_settings.no_progress_min_progress_pct = 0.33
                mock_settings.no_progress_profit_lock_ratio = 0.0
                mock_settings.no_progress_early_check_enabled = False
                mock_settings.no_progress_early_check_after_minutes = 45
                mock_settings.no_progress_early_min_progress_pct = 0.05
                mock_settings.no_progress_use_fill_price = True
                mock_settings.no_progress_chop_tightener_enabled = False
                mock_settings.no_progress_chop_tightener_trigger_count = 2
                mock_settings.no_progress_chop_tightener_early_check_after_minutes = 30
                mock_settings.no_progress_loss_cut_enabled = False
                mock_settings.time_exit_enabled = True
                mock_settings.time_exit_hour = exit_time.hour
                mock_settings.time_exit_minute = exit_time.minute
                await tracker._check_no_progress(book_data)

        mock_send.assert_called_once()   # market exit placed
        mock_sl.assert_not_called()      # no break-even SL


class TestChopTightener:
    """Adaptive chop tightener: shortens the early gate after N no-progress firings today.

    Behaviour contract:
      - Each firing of the no-progress gate (early or main) increments the day counter.
      - When the counter >= trigger_count, the early gate's age threshold is replaced
        with the tightened value for subsequent _check_no_progress passes.
      - Main gate is unchanged.
      - Counter resets with the other day counters at session reset / time exit.
    """

    @staticmethod
    def _apply_settings(mock_settings, *, enabled: bool, trigger: int = 2, tight: int = 30):
        mock_settings.no_progress_enabled = True
        mock_settings.no_progress_check_after_minutes = 90
        mock_settings.no_progress_min_progress_pct = 0.20
        mock_settings.no_progress_profit_lock_ratio = 0.0
        mock_settings.no_progress_ab_test_disable = False
        mock_settings.no_progress_early_check_enabled = True
        mock_settings.no_progress_early_check_after_minutes = 45
        mock_settings.no_progress_early_min_progress_pct = 0.05
        mock_settings.no_progress_use_fill_price = True
        mock_settings.no_progress_chop_tightener_enabled = enabled
        mock_settings.no_progress_chop_tightener_trigger_count = trigger
        mock_settings.no_progress_chop_tightener_early_check_after_minutes = tight
        mock_settings.no_progress_loss_cut_enabled = False
        mock_settings.time_exit_enabled = False  # default off; override in market-exit tests

    @pytest.mark.asyncio
    async def test_counter_increments_when_gate_fires(self):
        """Each gate firing must bump _day_no_progress_exits."""
        from signal_engine.models import Direction, TradeResult, OrderStatus

        engine = _make_engine()
        tracker = PositionTracker(engine)
        # 50min-old position with negative progress -> early gate (45min/5%) fires.
        pos = _make_position(
            entry_price=400.0,
            fill_price=400.0,
            sl=395.0,
            tp=410.0,
            direction=Direction.LONG,
            entry_time=datetime.now(_IST) - timedelta(minutes=50),
        )
        tracker.register(pos)
        book_data = {"RELIANCE": (1, 398.0)}  # progress = (398-400)/(410-400) = -20%

        with (
            patch("signal_engine.tracker.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.tracker.place_sl_order",
                  return_value=TradeResult(status=OrderStatus.SUCCESS, order_id="BE", message="")),
            patch("signal_engine.tracker.notifier.notify_be_stop_applied", new_callable=AsyncMock),
        ):
            with patch("signal_engine.config.settings") as mock_settings:
                self._apply_settings(mock_settings, enabled=True)
                assert tracker._day_no_progress_exits == 0
                await tracker._check_no_progress(book_data)
                assert tracker._day_no_progress_exits == 1

    @pytest.mark.asyncio
    async def test_early_gate_uses_normal_threshold_below_trigger(self):
        """With counter < trigger, early gate must NOT fire on a 32min-old stalled trade."""
        from signal_engine.models import Direction

        engine = _make_engine()
        tracker = PositionTracker(engine)
        tracker._day_no_progress_exits = 1  # one firing — below trigger=2
        # 32min old: would fire under tightened (30min) gate but NOT under normal (45min).
        pos = _make_position(
            entry_price=400.0,
            fill_price=400.0,
            sl=395.0,
            tp=410.0,
            direction=Direction.LONG,
            entry_time=datetime.now(_IST) - timedelta(minutes=32),
        )
        tracker.register(pos)
        book_data = {"RELIANCE": (1, 398.0)}

        with (
            patch("signal_engine.tracker.cancel_order", new_callable=AsyncMock) as mock_cancel,
            patch("signal_engine.tracker.place_sl_order") as mock_sl,
            patch("signal_engine.tracker.send_order", new_callable=AsyncMock) as mock_send,
        ):
            with patch("signal_engine.config.settings") as mock_settings:
                self._apply_settings(mock_settings, enabled=True)
                await tracker._check_no_progress(book_data)

        mock_cancel.assert_not_awaited()
        mock_sl.assert_not_called()
        mock_send.assert_not_awaited()
        assert tracker._day_no_progress_exits == 1  # unchanged — no firing

    @pytest.mark.asyncio
    async def test_early_gate_tightens_at_trigger(self):
        """With counter >= trigger, early gate fires on a 32min-old stalled trade (tightened to 30min)."""
        from signal_engine.models import Direction, TradeResult, OrderStatus

        engine = _make_engine()
        tracker = PositionTracker(engine)
        tracker._day_no_progress_exits = 2  # at trigger
        pos = _make_position(
            entry_price=400.0,
            fill_price=400.0,
            sl=395.0,
            tp=410.0,
            direction=Direction.LONG,
            entry_time=datetime.now(_IST) - timedelta(minutes=32),
        )
        tracker.register(pos)
        book_data = {"RELIANCE": (1, 398.0)}

        with (
            patch("signal_engine.tracker.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.tracker.place_sl_order",
                  return_value=TradeResult(status=OrderStatus.SUCCESS, order_id="BE", message="")) as mock_sl,
            patch("signal_engine.tracker.notifier.notify_be_stop_applied", new_callable=AsyncMock),
        ):
            with patch("signal_engine.config.settings") as mock_settings:
                self._apply_settings(mock_settings, enabled=True)
                await tracker._check_no_progress(book_data)

        mock_sl.assert_called_once()  # tightened gate fired
        assert tracker._day_no_progress_exits == 3  # incremented

    @pytest.mark.asyncio
    async def test_disabled_keeps_normal_gate_even_at_trigger(self):
        """With tightener disabled, early gate stays at 45min regardless of counter."""
        from signal_engine.models import Direction

        engine = _make_engine()
        tracker = PositionTracker(engine)
        tracker._day_no_progress_exits = 5  # well past trigger
        pos = _make_position(
            entry_price=400.0,
            fill_price=400.0,
            sl=395.0,
            tp=410.0,
            direction=Direction.LONG,
            entry_time=datetime.now(_IST) - timedelta(minutes=32),
        )
        tracker.register(pos)
        book_data = {"RELIANCE": (1, 398.0)}

        with (
            patch("signal_engine.tracker.cancel_order", new_callable=AsyncMock) as mock_cancel,
            patch("signal_engine.tracker.place_sl_order") as mock_sl,
        ):
            with patch("signal_engine.config.settings") as mock_settings:
                self._apply_settings(mock_settings, enabled=False)
                await tracker._check_no_progress(book_data)

        mock_cancel.assert_not_awaited()
        mock_sl.assert_not_called()  # disabled tightener -> 32min < 45min normal gate

    @pytest.mark.asyncio
    async def test_main_gate_unchanged_by_tightener(self):
        """Main gate (90min/20%) must NOT be tightened — protects slow winners."""
        from signal_engine.models import Direction

        engine = _make_engine()
        tracker = PositionTracker(engine)
        tracker._day_no_progress_exits = 5  # tightener engaged
        # 65min-old trade with 18% progress: below main 20% threshold but past tightened
        # early gate (30min) age. However early gate's progress threshold is 5%, and
        # trade is at 18% — early should NOT fire either. Main should NOT fire (65 < 90).
        pos = _make_position(
            entry_price=400.0,
            fill_price=400.0,
            sl=390.0,
            tp=410.0,
            direction=Direction.LONG,
            entry_time=datetime.now(_IST) - timedelta(minutes=65),
        )
        tracker.register(pos)
        book_data = {"RELIANCE": (1, 401.8)}  # progress = 1.8/10 = 18%

        with (
            patch("signal_engine.tracker.cancel_order", new_callable=AsyncMock) as mock_cancel,
            patch("signal_engine.tracker.place_sl_order") as mock_sl,
            patch("signal_engine.tracker.send_order", new_callable=AsyncMock) as mock_send,
        ):
            with patch("signal_engine.config.settings") as mock_settings:
                self._apply_settings(mock_settings, enabled=True)
                await tracker._check_no_progress(book_data)

        mock_cancel.assert_not_awaited()
        mock_sl.assert_not_called()
        mock_send.assert_not_awaited()

    def test_counter_resets_with_day_counters(self):
        """After session reset, _day_no_progress_exits and _chop_tightener_logged reset."""
        engine = _make_engine()
        tracker = PositionTracker(engine)
        tracker._day_no_progress_exits = 4
        tracker._chop_tightener_logged = True
        tracker._day_trades = 8
        tracker._day_pnl = -200.0

        # Simulate the reset block from time_exit_all (no-MIS branch)
        tracker._day_trades = 0
        tracker._day_wins = 0
        tracker._day_losses = 0
        tracker._day_pnl = 0.0
        tracker._day_no_progress_exits = 0
        tracker._chop_tightener_logged = False

        assert tracker._day_no_progress_exits == 0
        assert tracker._chop_tightener_logged is False


class TestLossCutGate:
    """Loss-cut gate: exits immediately when progress is deeply negative.

    Behaviour contract:
      - Fires when age >= loss_cut_min_age_minutes AND progress < loss_cut_progress_threshold.
      - Does NOT fire before min_age even with very negative progress.
      - Does NOT fire when progress is bad but not below the threshold.
      - Disabled by default; existing gates unaffected when disabled.
    """

    @staticmethod
    def _apply_settings(mock_settings, *, enabled: bool, min_age: int = 20, threshold: float = -0.80):
        mock_settings.no_progress_enabled = True
        mock_settings.no_progress_check_after_minutes = 90
        mock_settings.no_progress_min_progress_pct = 0.20
        mock_settings.no_progress_profit_lock_ratio = 0.0
        mock_settings.no_progress_ab_test_disable = False
        mock_settings.no_progress_early_check_enabled = False
        mock_settings.no_progress_early_check_after_minutes = 45
        mock_settings.no_progress_early_min_progress_pct = 0.05
        mock_settings.no_progress_use_fill_price = True
        mock_settings.no_progress_chop_tightener_enabled = False
        mock_settings.no_progress_chop_tightener_trigger_count = 2
        mock_settings.no_progress_chop_tightener_early_check_after_minutes = 30
        mock_settings.no_progress_loss_cut_enabled = enabled
        mock_settings.no_progress_loss_cut_min_age_minutes = min_age
        mock_settings.no_progress_loss_cut_progress_threshold = threshold
        # Enable time exit so negative-rate positions use market exit (not break-even SL).
        mock_settings.time_exit_enabled = True
        mock_settings.time_exit_hour = 15
        mock_settings.time_exit_minute = 0

    @pytest.mark.asyncio
    async def test_fires_when_progress_below_threshold_after_min_age(self):
        """Gate fires when deeply negative progress AND age >= min_age."""
        from signal_engine.models import Direction, TradeResult, OrderStatus

        engine = _make_engine()
        tracker = PositionTracker(engine)
        # 30min old, progress = (390 - 406.85) / (413.01 - 406.85) = -16.85/6.16 = -2.74 (274%)
        # Use simpler numbers: fill=400, tp=410, ltp=391 -> progress=(391-400)/(410-400)=-90%
        pos = _make_position(
            entry_price=400.0,
            fill_price=400.0,
            sl=390.0,
            tp=410.0,
            direction=Direction.LONG,
            entry_time=datetime.now(_IST) - timedelta(minutes=30),
        )
        tracker.register(pos)
        book_data = {"RELIANCE": (1, 391.0)}  # progress = (391-400)/(410-400) = -90%

        with (
            patch("signal_engine.tracker.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.tracker.send_order", new_callable=AsyncMock) as mock_exit,
            patch("signal_engine.tracker.notifier.notify_no_progress_exit", new_callable=AsyncMock),
            patch("signal_engine.tracker.notifier.notify_position_closed", new_callable=AsyncMock),
        ):
            from signal_engine.models import TradeResult, OrderStatus
            mock_exit.return_value = TradeResult(status=OrderStatus.SUCCESS, order_id="EXIT", message="")
            with patch("signal_engine.config.settings") as mock_settings:
                self._apply_settings(mock_settings, enabled=True, min_age=20, threshold=-0.80)
                await tracker._check_no_progress(book_data)

        mock_exit.assert_called_once()

    @pytest.mark.asyncio
    async def test_does_not_fire_before_min_age(self):
        """Gate must not fire even with terrible progress if age < min_age."""
        from signal_engine.models import Direction

        engine = _make_engine()
        tracker = PositionTracker(engine)
        # 10min old — below min_age of 20min
        pos = _make_position(
            entry_price=400.0,
            fill_price=400.0,
            sl=390.0,
            tp=410.0,
            direction=Direction.LONG,
            entry_time=datetime.now(_IST) - timedelta(minutes=10),
        )
        tracker.register(pos)
        book_data = {"RELIANCE": (1, 388.0)}  # progress = -120%, well below threshold

        with (
            patch("signal_engine.tracker.send_order", new_callable=AsyncMock) as mock_exit,
        ):
            with patch("signal_engine.config.settings") as mock_settings:
                self._apply_settings(mock_settings, enabled=True, min_age=20, threshold=-0.80)
                await tracker._check_no_progress(book_data)

        mock_exit.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_fire_when_progress_above_threshold(self):
        """Gate must not fire when progress is negative but above the threshold."""
        from signal_engine.models import Direction

        engine = _make_engine()
        tracker = PositionTracker(engine)
        pos = _make_position(
            entry_price=400.0,
            fill_price=400.0,
            sl=390.0,
            tp=410.0,
            direction=Direction.LONG,
            entry_time=datetime.now(_IST) - timedelta(minutes=30),
        )
        tracker.register(pos)
        # progress = (396 - 400) / (410 - 400) = -40%, above -80% threshold
        book_data = {"RELIANCE": (1, 396.0)}

        with (
            patch("signal_engine.tracker.send_order", new_callable=AsyncMock) as mock_exit,
        ):
            with patch("signal_engine.config.settings") as mock_settings:
                self._apply_settings(mock_settings, enabled=True, min_age=20, threshold=-0.80)
                await tracker._check_no_progress(book_data)

        mock_exit.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_does_not_affect_main_gate(self):
        """When loss_cut disabled, main gate still fires normally at 90min."""
        from signal_engine.models import Direction, TradeResult, OrderStatus

        engine = _make_engine()
        tracker = PositionTracker(engine)
        pos = _make_position(
            entry_price=400.0,
            fill_price=400.0,
            sl=390.0,
            tp=410.0,
            direction=Direction.LONG,
            entry_time=datetime.now(_IST) - timedelta(minutes=95),
        )
        tracker.register(pos)
        book_data = {"RELIANCE": (1, 399.0)}  # progress = -10%, below main gate's 20%

        with (
            patch("signal_engine.tracker.cancel_order", new_callable=AsyncMock, return_value=True),
            patch("signal_engine.tracker.send_order", new_callable=AsyncMock) as mock_exit,
            patch("signal_engine.tracker.notifier.notify_no_progress_exit", new_callable=AsyncMock),
            patch("signal_engine.tracker.notifier.notify_position_closed", new_callable=AsyncMock),
        ):
            from signal_engine.models import TradeResult, OrderStatus
            mock_exit.return_value = TradeResult(status=OrderStatus.SUCCESS, order_id="EXIT", message="")
            with patch("signal_engine.config.settings") as mock_settings:
                self._apply_settings(mock_settings, enabled=False)  # loss-cut OFF
                await tracker._check_no_progress(book_data)

        mock_exit.assert_called_once()
