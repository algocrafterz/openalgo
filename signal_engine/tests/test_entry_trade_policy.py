"""main._entry_rejected_by_trade_policy: the per-direction daily cap for paper and live (spec item 15).

BREAKOUT-PY took INFY LONG three times on 2026-10-01 because the paper path had no per-direction
rule. The frozen v1.0 rule: at most 2 FILLED entries per symbol and direction per day, a re-entry
only after the position is flat, counted on the ledger so a restart cannot reset it.
"""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from signal_engine import db, main
from signal_engine.config import RunnerStrategy
from signal_engine.models import Action, Direction, Order, OrderStatus, Signal, TradeResult
from signal_engine.timeutils import IST

PY = "BREAKOUT-PY"

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_DB_PATH", str(tmp_path / "trades.db"))
    db.reset_connection()
    db.set_trade_mode("analyze")
    runner = SimpleNamespace(
        strategies={
            PY: RunnerStrategy("python", "tp1_only", "ladder", True, 2),
            "NOCAP": RunnerStrategy("python", "tp1_only", "ladder", True, None),
        }
    )
    monkeypatch.setattr(main, "settings", SimpleNamespace(runner=runner))
    held = {"pos": None}
    monkeypatch.setattr(main, "tracker", SimpleNamespace(find_position=lambda s, st: held["pos"]))
    monkeypatch.setattr(main, "_decline", AsyncMock())
    monkeypatch.setattr(main, "_held", held, raising=False)  # test handle
    yield
    db.reset_connection()


def _signal(strategy=PY, direction=Direction.LONG, symbol="INFY"):
    return Signal(
        strategy=strategy, direction=direction, symbol=symbol, entry=100.0, sl=99.0, tp=101.5,
        raw_message="x", sig_id=f"{strategy}-{symbol}-{datetime.now(IST):%f}",
    )


def _row(strategy=PY, direction=Direction.LONG, symbol="INFY", fill=100.1, status=OrderStatus.SUCCESS):
    sig = _signal(strategy, direction, symbol)
    order = Order(
        symbol=symbol, exchange="NSE", action=Action.SELL if direction == Direction.SHORT else Action.BUY,
        quantity=10, price=0.0, order_type="MARKET", product="MIS", strategy_tag=strategy,
    )
    db.save(sig, order, TradeResult(order_id="O1", status=status, fill_price=fill,
                                    timestamp=datetime.now(IST).replace(tzinfo=None)))


def _held(direction):
    main._held["pos"] = SimpleNamespace(direction=direction)


async def _rejected(signal=None):
    return await main._entry_rejected_by_trade_policy(signal or _signal())


class TestPerDirectionCap:
    async def test_first_entry_is_allowed(self):
        assert await _rejected() is False

    async def test_long_exit_long_is_allowed(self):
        _row()  # one LONG filled, now flat
        assert await _rejected() is False

    async def test_a_third_long_is_declined_with_its_own_stage(self):
        _row()
        _row()
        assert await _rejected() is True
        main._decline.assert_awaited_once()
        assert main._decline.await_args.kwargs["stage"] == "trade_policy"
        assert "DIRECTION_ALREADY_TAKEN" in main._decline.await_args.kwargs["reason"]

    async def test_the_short_side_is_counted_separately(self):
        _row()
        _row()
        assert await _rejected(_signal(direction=Direction.SHORT)) is False

    async def test_another_symbol_is_not_counted(self):
        _row(symbol="TCS")
        _row(symbol="TCS")
        assert await _rejected() is False

    async def test_a_reversal_after_flat_is_allowed(self):
        _row()
        assert await _rejected(_signal(direction=Direction.SHORT)) is False


class TestOnlyFilledEntriesCount:
    async def test_a_rejected_signal_is_not_counted(self):
        for _ in range(3):
            _row(status=OrderStatus.REJECTED, fill=None)
        assert await _rejected() is False

    async def test_a_success_row_without_a_fill_is_not_counted(self):
        _row(fill=None)
        _row(fill=None)
        assert await _rejected() is False

    async def test_an_exit_row_is_not_an_entry(self):
        _row()
        _row(direction=Direction.EXIT)
        _row(direction=Direction.EXIT)
        assert await _rejected() is False  # one filled LONG only

    async def test_two_filled_entries_use_the_cap_even_when_both_closed_at_once(self):
        # a bad-fill entry (item 18) is written with its fill before the guard closes it
        _row(fill=100.8)
        _row(fill=100.8)
        assert await _rejected() is True


class TestOpenPosition:
    async def test_no_second_entry_while_a_position_is_open(self):
        _held(Direction.LONG)
        assert await _rejected() is True
        assert "already has a position" in main._decline.await_args.kwargs["reason"]

    async def test_an_opposite_entry_needs_the_position_flat(self):
        _held(Direction.LONG)
        assert await _rejected(_signal(direction=Direction.SHORT)) is True


class TestWhoIsAffected:
    async def test_a_strategy_not_in_the_runner_list_is_untouched(self):
        for _ in range(5):
            _row(strategy="BREAKOUT")
        assert await _rejected(_signal(strategy="BREAKOUT")) is False
        main._decline.assert_not_awaited()

    async def test_a_null_cap_means_no_policy(self):
        for _ in range(5):
            _row(strategy="NOCAP")
        assert await _rejected(_signal(strategy="NOCAP")) is False

    async def test_the_paper_ledger_does_not_count_live_entries(self):
        db.set_trade_mode("live")
        _row()
        _row()
        db.set_trade_mode("analyze")
        assert await _rejected() is False


class TestUnreadableLedger:
    async def test_the_entry_is_declined_not_taken_on_an_unchecked_count(self, monkeypatch):
        monkeypatch.setattr(main, "fetch_entry_fill_directions", MagicMock(side_effect=RuntimeError("db locked")))
        assert await _rejected() is True
        assert "could not be read" in main._decline.await_args.kwargs["reason"]


class TestHandleEntryUsesIt:
    async def test_a_declined_entry_never_reaches_the_risk_gates_or_the_broker(self, monkeypatch):
        _row()
        _row()
        gates = AsyncMock()
        monkeypatch.setattr(main, "_entry_halted", AsyncMock(return_value=False))
        monkeypatch.setattr(main, "_entry_rejected_by_symbol_rules", AsyncMock(return_value=False))
        monkeypatch.setattr(main, "_entry_passes_risk_gates", gates)
        monkeypatch.setattr(main, "send_order", AsyncMock())
        await main._handle_entry(_signal())
        gates.assert_not_awaited()
        main.send_order.assert_not_awaited()
