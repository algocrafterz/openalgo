"""An entry the broker accepted must be in trades.db before anything else can fail.

Before 2026-10-23 the entry row was written last, after the Telegram notice, the stop order, the
fill lookup and the bad-fill guard. A crash in between left a broker position with no row, and a
restart then skipped it ("no entry trade in trades.db today"): no stop recovery, no tracking, no
time exit. The row is now written the moment the broker accepts the order and its fill price is
filled in afterwards. Restart also raises a critical alert for a broker position it cannot match.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import db, startup
from signal_engine.main import _adopt_untracked_entry, _establish_position, _handle_entry
from signal_engine.models import Action, Direction, Order, OrderStatus, Signal, TradeResult
from signal_engine.tests.test_main_characterization import (
    _entry_signal,
    _fail,
    _real_tracker,
    harness,
)


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", str(tmp_path / "trades.db"))
    db.reset_connection()
    db.set_trade_mode("analyze")
    yield
    db.reset_connection()


def _rows():
    return db._get_connection().execute(
        "SELECT direction, status, order_id, fill_price FROM trades ORDER BY id"
    ).fetchall()


def _sized(h, qty=10):
    h.risk.get_sizing_capital.return_value = 100000.0
    h.risk.calculate_quantity.return_value = qty
    return patch("signal_engine.main.adjust_qty_for_margin", new=AsyncMock(return_value=qty))


def _boom(*_a, **_k):
    raise RuntimeError("process died here")


class TestTheAcceptedEntryIsSavedFirst:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("crash_at", ["notify_order_placed", "send_bracket_legs", "fetch_order_fill_price"])
    async def test_a_crash_after_acceptance_leaves_the_entry_row(self, crash_at):
        stubs = {"tracker": _real_tracker(), "save": db.save}
        if crash_at != "notify_order_placed":
            stubs[crash_at] = AsyncMock(side_effect=_boom)
        h = harness(**stubs)
        with h.stack, _sized(h):
            if crash_at == "notify_order_placed":
                h.notifier.notify_order_placed = AsyncMock(side_effect=_boom)
            with pytest.raises(RuntimeError):
                await _handle_entry(_entry_signal())
        assert _rows() == [("LONG", "SUCCESS", "EX1", None)]

    @pytest.mark.asyncio
    async def test_a_normal_entry_writes_one_row_with_its_fill(self):
        h = harness(tracker=_real_tracker(), save=db.save)
        with h.stack, _sized(h):
            await _handle_entry(_entry_signal())
        assert _rows() == [("LONG", "SUCCESS", "EX1", 2500.0)]

    @pytest.mark.asyncio
    async def test_a_rejected_entry_is_still_recorded_once(self):
        h = harness(tracker=_real_tracker(), save=db.save, send_order=AsyncMock(return_value=_fail("no funds")))
        with h.stack, _sized(h):
            await _handle_entry(_entry_signal())
        assert [(r[0], r[1]) for r in _rows()] == [("LONG", "ERROR")]

    @pytest.mark.asyncio
    async def test_a_restart_after_the_crash_finds_the_entry(self):
        h = harness(tracker=_real_tracker(), save=db.save, send_bracket_legs=AsyncMock(side_effect=_boom))
        with h.stack, _sized(h):
            with pytest.raises(RuntimeError):
                await _handle_entry(_entry_signal())
        found = db.fetch_last_entry_trade("RELIANCE", "ORB")
        assert found is not None and found["order_id"] == "EX1"
        assert found["direction"] == Direction.LONG


def _pfc_signal():
    return Signal(
        strategy="BREAKOUT-PY", direction=Direction.LONG, symbol="PFC", entry=325.60, sl=324.62,
        tp=327.07, raw_message="x", sig_id="BREAKOUT-PY-PFC-20261006-1120",
    )


def _pfc_order():
    return Order(
        symbol="PFC", exchange="NSE", action=Action.BUY, quantity=930, price=0.0,
        order_type="MARKET", product="MIS", strategy_tag="BREAKOUT-PY",
    )


async def _establish_bad_fill(entry_row_id):
    settings = SimpleNamespace(exchange="NSE", product="MIS", max_entry_slippage_r=0.5, bracket_enabled=True)
    notifier = MagicMock()
    for name in ("notify_order_rejected", "notify_exit_failed", "notify_entry_filled"):
        setattr(notifier, name, AsyncMock())
    tracker = MagicMock()
    tracker.fetch_own_position = AsyncMock(return_value=0)
    with (
        patch("signal_engine.main.settings", settings),
        patch("signal_engine.main.send_order", AsyncMock(return_value=TradeResult(status=OrderStatus.SUCCESS, order_id="CLOSE-1"))),
        patch("signal_engine.main.cancel_order", AsyncMock(return_value=True)),
        patch("signal_engine.main._place_entry_bracket", AsyncMock(side_effect=["SL-1", "SL-2"])),
        patch("signal_engine.main._fetch_entry_fill", AsyncMock(return_value=326.10)),
        patch("signal_engine.main._find_recent_market_exit_fill", AsyncMock(return_value=None)),
        patch("signal_engine.main.fetch_order_fill_price", AsyncMock(return_value=326.00)),
        patch("signal_engine.main._book_realised_pnl_delta", AsyncMock(return_value=(-90.0, 0.0))),
        patch("signal_engine.main.notifier", notifier),
        patch("signal_engine.main.risk_engine", MagicMock()),
        patch("signal_engine.main.tracker", tracker),
    ):
        result = TradeResult(status=OrderStatus.SUCCESS, order_id="ENTRY-1")
        return await _establish_position(_pfc_signal(), _pfc_order(), 930, result, entry_row_id=entry_row_id)


class TestBadFillCloseKeepsOneEntryRow:
    @pytest.mark.asyncio
    async def test_an_entry_saved_by_the_caller_is_updated_not_written_again(self):
        result = TradeResult(status=OrderStatus.SUCCESS, order_id="ENTRY-1")
        row_id = db.save(_pfc_signal(), _pfc_order(), result)
        assert await _establish_bad_fill(row_id) is False
        assert _rows() == [("LONG", "SUCCESS", "ENTRY-1", 326.10), ("EXIT", "SUCCESS", "CLOSE-1", 326.0)]


class TestAdoptedEntry:
    @pytest.mark.asyncio
    async def test_an_entry_adopted_after_a_timeout_is_written_once(self):
        h = harness(tracker=_real_tracker(), save=db.save)
        pending = SimpleNamespace(
            signal=_entry_signal(), order=h.build_order.return_value, action="BUY", quantity=1,
            symbol="RELIANCE", strategy="ORB",
        )
        with h.stack:
            await _adopt_untracked_entry(pending, "LATE-1")
        assert [(r[0], r[2]) for r in _rows()] == [("LONG", "LATE-1")]


class TestTheEntryWriteFails:
    """The broker accepted the order but trades.db did not take the row (locked, disk full)."""

    @pytest.mark.asyncio
    async def test_a_failed_first_write_is_retried_once_and_leaves_one_row(self):
        calls = []

        def flaky_save(*args):
            calls.append(1)
            return None if len(calls) == 1 else db.save(*args)

        h = harness(tracker=_real_tracker(), save=flaky_save)
        with h.stack, _sized(h):
            await _handle_entry(_entry_signal())
        assert _rows() == [("LONG", "SUCCESS", "EX1", 2500.0)]
        h.notifier.notify.assert_not_awaited()  # recovered, nothing to alert

    @pytest.mark.asyncio
    async def test_both_writes_failing_keeps_the_position_protected_and_alerts(self):
        tracker = _real_tracker()
        h = harness(tracker=tracker, save=lambda *a: None)
        with h.stack, _sized(h):
            await _handle_entry(_entry_signal())
            h.send_bracket_legs.assert_awaited_once()  # the stop still goes in
            alerts = [c.args[0] for c in h.notifier.notify.await_args_list]
        assert _rows() == []
        assert tracker.find_position("RELIANCE", "ORB") is not None  # managed for this session
        assert any("ENTRY NOT RECORDED" in a and "RELIANCE" in a for a in alerts)

    def test_a_restart_after_an_unrecorded_entry_alerts_and_writes_nothing(self):
        tracker = MagicMock()
        tracker.find_position.return_value = None
        unmatched = []
        with (
            patch.object(startup, "settings", SimpleNamespace(exchange="NSE", strategy_profiles={})),
        ):
            restored = startup._restore_tracker_positions(
                tracker, [{"symbol": "RELIANCE", "quantity": 10, "exchange": "NSE"}], "MIS",
                unmatched=unmatched,
            )
        assert restored == 0 and unmatched == [("RELIANCE", 10)]
        assert _rows() == []  # restart never invents an entry row

    @pytest.mark.asyncio
    async def test_a_restart_after_a_crash_restores_from_the_row_without_a_second_row(self):
        h = harness(tracker=_real_tracker(), save=db.save, send_bracket_legs=AsyncMock(side_effect=_boom))
        with h.stack, _sized(h):
            with pytest.raises(RuntimeError):
                await _handle_entry(_entry_signal())
        tracker = MagicMock()
        tracker.find_position.return_value = None
        with patch.object(startup, "settings", SimpleNamespace(exchange="NSE", strategy_profiles={"ORB": {}})):
            restored = startup._restore_tracker_positions(
                tracker, [{"symbol": "RELIANCE", "quantity": 1, "exchange": "NSE", "average_price": 2500.5}], "MIS",
            )
        assert restored == 1
        pos = tracker.register.call_args.args[0]
        assert (pos.entry_order_id, pos.quantity, pos.fill_price) == ("EX1", 1, 2500.5)
        assert _rows() == [("LONG", "SUCCESS", "EX1", None)]  # still one row, status unchanged


class TestRestartAlertsOnAnUnmatchedBrokerPosition:
    def test_an_unmatched_position_is_reported(self):
        tracker = MagicMock()
        tracker.find_position.return_value = None
        unmatched = []
        with (
            patch.object(startup, "settings", SimpleNamespace(exchange="NSE", strategy_profiles={})),
            patch.object(startup, "fetch_last_entry_trade", return_value=None),
        ):
            restored = startup._restore_tracker_positions(
                tracker, [{"symbol": "SBIN", "quantity": 40, "exchange": "NSE"}], "MIS",
                unmatched=unmatched,
            )
        assert restored == 0
        assert unmatched == [("SBIN", 40)]

    @pytest.mark.asyncio
    async def test_reconcile_sends_one_critical_alert(self):
        sent = AsyncMock()
        with patch.object(startup.notifier, "notify_event", sent):
            await startup._alert_unmatched_positions([("SBIN", 40), ("INFY", -25)])
        sent.assert_awaited_once()
        event, text = sent.await_args.args[:2]
        assert event == "untracked_broker_position" and "SBIN" in text and "INFY" in text

    @pytest.mark.asyncio
    async def test_nothing_unmatched_sends_nothing(self):
        sent = AsyncMock()
        with patch.object(startup.notifier, "notify_event", sent):
            await startup._alert_unmatched_positions([])
        sent.assert_not_awaited()
