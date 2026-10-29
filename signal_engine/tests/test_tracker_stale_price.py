"""A positionbook price that has stopped moving must not drive a decision as if it were current.

OpenAlgo's sandbox keeps the last price it stored when a valuation refresh is refused (the
Flattrade background lane can refuse one), and the positionbook carries no timestamp for it. So
the tracker watches each held symbol's price itself: unchanged for longer than
_STALE_PRICE_SECONDS means "unconfirmed". An unconfirmed price never opens a no-progress exit,
and never changes a leg's reported unrealised loss.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from signal_engine.models import Direction
from signal_engine.tests.tracker_fixtures import _make_position
from signal_engine.tracker import (
    _STALE_PRICE_SECONDS,
    BookEntry,
    PositionTracker,
    TrackedPosition,
)

_IST = timezone(timedelta(hours=5, minutes=30))


def _pos(symbol="SBIN", strategy="ORB", qty=100, fill=800.0, direction=Direction.LONG):
    return TrackedPosition(
        symbol=symbol, strategy=strategy, exchange="NSE", product="MIS",
        entry_price=fill, quantity=qty, sl=796.0, tp=810.0, direction=direction,
        entry_order_id="1", fill_price=fill, ever_seen_nonzero_qty=True,
    )


def _tracker():
    return PositionTracker(MagicMock())


class TestStalePriceDetection:
    def test_a_price_seen_for_the_first_time_is_fresh(self):
        t = _tracker()
        out = t._mark_stale_prices({"SBIN": BookEntry(100, 790.0)}, now=1000.0)
        assert out["SBIN"].stale is False

    def test_unchanged_within_the_window_is_still_fresh(self):
        t = _tracker()
        t._mark_stale_prices({"SBIN": BookEntry(100, 790.0)}, now=1000.0)
        out = t._mark_stale_prices(
            {"SBIN": BookEntry(100, 790.0)}, now=1000.0 + _STALE_PRICE_SECONDS - 1
        )
        assert out["SBIN"].stale is False

    def test_unchanged_beyond_the_window_is_stale(self):
        t = _tracker()
        t._mark_stale_prices({"SBIN": BookEntry(100, 790.0)}, now=1000.0)
        out = t._mark_stale_prices(
            {"SBIN": BookEntry(100, 790.0)}, now=1000.0 + _STALE_PRICE_SECONDS + 1
        )
        assert out["SBIN"].stale is True

    def test_a_changed_price_restarts_the_clock(self):
        t = _tracker()
        t._mark_stale_prices({"SBIN": BookEntry(100, 790.0)}, now=1000.0)
        t._mark_stale_prices({"SBIN": BookEntry(100, 790.05)}, now=1100.0)
        out = t._mark_stale_prices(
            {"SBIN": BookEntry(100, 790.05)}, now=1100.0 + _STALE_PRICE_SECONDS - 1
        )
        assert out["SBIN"].stale is False

    def test_recovery_clears_the_flag(self):
        t = _tracker()
        t._mark_stale_prices({"SBIN": BookEntry(100, 790.0)}, now=1000.0)
        t._mark_stale_prices({"SBIN": BookEntry(100, 790.0)}, now=1000.0 + _STALE_PRICE_SECONDS + 5)
        out = t._mark_stale_prices(
            {"SBIN": BookEntry(100, 791.0)}, now=1000.0 + _STALE_PRICE_SECONDS + 10
        )
        assert out["SBIN"].stale is False

    def test_a_closed_position_is_never_flagged(self):
        """A closed row's price is a fixed execution price by design, not a frozen feed."""
        t = _tracker()
        t._mark_stale_prices({"SBIN": BookEntry(0, 790.0)}, now=1000.0)
        out = t._mark_stale_prices(
            {"SBIN": BookEntry(0, 790.0)}, now=1000.0 + _STALE_PRICE_SECONDS + 500
        )
        assert out["SBIN"].stale is False

    def test_a_zero_price_is_not_flagged_it_is_already_unusable(self):
        t = _tracker()
        t._mark_stale_prices({"SBIN": BookEntry(100, 0.0)}, now=1000.0)
        out = t._mark_stale_prices(
            {"SBIN": BookEntry(100, 0.0)}, now=1000.0 + _STALE_PRICE_SECONDS + 5
        )
        assert out["SBIN"].stale is False

    def test_a_symbol_that_left_the_book_starts_fresh_when_it_returns(self):
        t = _tracker()
        t._mark_stale_prices({"SBIN": BookEntry(100, 790.0)}, now=1000.0)
        t._mark_stale_prices({}, now=1000.0 + 50)
        out = t._mark_stale_prices(
            {"SBIN": BookEntry(100, 790.0)}, now=1000.0 + _STALE_PRICE_SECONDS + 500
        )
        assert out["SBIN"].stale is False

    def test_the_input_is_not_mutated(self):
        t = _tracker()
        t._mark_stale_prices({"SBIN": BookEntry(100, 790.0)}, now=1000.0)
        original = {"SBIN": BookEntry(100, 790.0)}
        t._mark_stale_prices(original, now=1000.0 + _STALE_PRICE_SECONDS + 5)
        assert original["SBIN"].stale is False


class TestUnrealisedHoldsTheLastConfirmedFigureWhenStale:
    def test_a_stale_leg_keeps_its_last_confirmed_loss(self):
        risk = MagicMock()
        t = PositionTracker(risk)
        t.register(_pos(qty=100, fill=800.0))
        t._push_unrealised({"SBIN": BookEntry(100, 790.0)})  # confirmed: -1,000
        risk.update_unrealised.reset_mock()
        # The stale row shows a price that would read as a profit; it must not be used.
        t._push_unrealised({"SBIN": BookEntry(100, 815.0, stale=True)})
        risk.update_unrealised.assert_called_once_with(1000.0, "ORB")

    def test_a_stale_leg_with_no_confirmed_figure_contributes_nothing(self):
        risk = MagicMock()
        t = PositionTracker(risk)
        t.register(_pos(qty=100, fill=800.0))
        t._push_unrealised({"SBIN": BookEntry(100, 790.0, stale=True)})
        risk.update_unrealised.assert_called_once_with(0.0, "ORB")

    def test_a_fresh_price_after_a_stale_spell_is_used_again(self):
        risk = MagicMock()
        t = PositionTracker(risk)
        t.register(_pos(qty=100, fill=800.0))
        t._push_unrealised({"SBIN": BookEntry(100, 790.0)})
        t._push_unrealised({"SBIN": BookEntry(100, 790.0, stale=True)})
        risk.update_unrealised.reset_mock()
        t._push_unrealised({"SBIN": BookEntry(100, 780.0)})  # -2,000
        risk.update_unrealised.assert_called_once_with(2000.0, "ORB")

    def test_a_closed_leg_does_not_leave_a_remembered_loss_behind(self):
        risk = MagicMock()
        t = PositionTracker(risk)
        t.register(_pos(qty=100, fill=800.0))
        t._push_unrealised({"SBIN": BookEntry(100, 790.0)})
        t._positions.clear()
        t._push_unrealised({})  # the leg is gone: its remembered loss is dropped
        t.register(_pos(qty=100, fill=800.0))  # a new position in the same symbol
        risk.update_unrealised.reset_mock()
        t._push_unrealised({"SBIN": BookEntry(100, 790.0, stale=True)})
        risk.update_unrealised.assert_called_once_with(0.0, "ORB")


class TestNoProgressIgnoresAStalePrice:
    def _fire(self, entry: BookEntry):
        t = _tracker()
        pos = _make_position(
            entry_price=541.80, fill_price=541.80, sl=528.01, tp=551.10,
            direction=Direction.LONG,
            entry_time=datetime.now(_IST) - timedelta(minutes=150),
        )
        gates = [(timedelta(minutes=90), 0.33, "main")]
        settings = SimpleNamespace(no_progress_use_fill_price=True)
        return t._evaluate_no_progress(
            pos, datetime.now(_IST), gates, {pos.symbol: entry}, settings
        )

    def test_a_fresh_stalled_price_fires_the_gate(self):
        assert self._fire(BookEntry(1, 541.90)) is not None

    def test_the_same_price_marked_stale_does_not_fire(self):
        assert self._fire(BookEntry(1, 541.90, stale=True)) is None
