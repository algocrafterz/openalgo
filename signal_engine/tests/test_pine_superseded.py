"""BREAKOUT spec v1.0 item 17: Python is the sole execution source on its universe during parity."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from signal_engine import db, main
from signal_engine.config import RunnerStrategy
from signal_engine.models import Direction, Signal
from signal_engine.pipeline import single_source as ss

PY_ENTRY = RunnerStrategy("python", "tp1_only", "ladder", True, 2, "live", "BREAKOUT")


def cfg(universe=("TCS", "INFY"), enabled=True, strategies=None):
    return SimpleNamespace(
        enabled=enabled, universe=universe, strategies=strategies or {"BREAKOUT-PY": PY_ENTRY}
    )


class TestPineSuperseded:
    def test_a_covered_symbol_is_superseded(self):
        assert ss.pine_superseded(cfg(), "BREAKOUT", "TCS") is True

    def test_a_symbol_the_runner_does_not_cover_still_trades_on_pine(self):
        assert ss.pine_superseded(cfg(), "BREAKOUT", "RECLTD") is False

    def test_the_match_ignores_case(self):
        assert ss.pine_superseded(cfg(), "breakout", "tcs") is True

    def test_fno_all_covers_every_symbol(self):
        assert ss.pine_superseded(cfg(universe="fno_all"), "BREAKOUT", "RECLTD") is True

    def test_nothing_is_superseded_while_the_runner_is_off(self):
        assert ss.pine_superseded(cfg(enabled=False), "BREAKOUT", "TCS") is False

    def test_another_tag_is_untouched(self):
        assert ss.pine_superseded(cfg(), "ORB", "TCS") is False

    def test_a_strategy_that_supersedes_nothing_changes_nothing(self):
        plain = RunnerStrategy("python", "tp1_only", "ladder", True, 2, "live", None)
        assert ss.pine_superseded(cfg(strategies={"BREAKOUT-PY": plain}), "BREAKOUT", "TCS") is False

    def test_a_pine_sourced_entry_cannot_supersede(self):
        pine = RunnerStrategy("pine", "tp1_only", "tracker", True, None, "mode", "BREAKOUT")
        assert ss.pine_superseded(cfg(strategies={"X": pine}), "BREAKOUT", "TCS") is False


@pytest.mark.asyncio
class TestHandleMessage:
    @pytest.fixture(autouse=True)
    def _env(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "_DB_PATH", str(tmp_path / "trades.db"))
        db.reset_connection()
        monkeypatch.setattr(main, "settings", SimpleNamespace(runner=cfg()))
        self.decline = AsyncMock()
        self.dispatch = AsyncMock()
        monkeypatch.setattr(main, "_decline", self.decline)
        monkeypatch.setattr(main, "dispatch_signal", self.dispatch)
        yield
        db.reset_connection()

    @staticmethod
    def _signal(direction, symbol="TCS"):
        return Signal(strategy="BREAKOUT", direction=direction, symbol=symbol, entry=100.0, sl=99.0, tp=102.0,
                      raw_message="x", sig_id=f"{symbol}-20261008-1000")

    async def _send(self, monkeypatch, signal):
        monkeypatch.setattr(main, "parse", lambda text: signal)
        await main.handle_message("x")

    async def test_a_pine_entry_on_a_covered_symbol_is_declined_not_traded(self, monkeypatch):
        await self._send(monkeypatch, self._signal(Direction.LONG))
        self.dispatch.assert_not_awaited()
        assert self.decline.await_args.kwargs["stage"] == "superseded"

    async def test_a_pine_exit_for_a_position_the_engine_tracks_still_passes(self, monkeypatch):
        monkeypatch.setattr(main, "tracker", SimpleNamespace(find_position=lambda sym, strat: object()))
        await self._send(monkeypatch, self._signal(Direction.EXIT))
        self.dispatch.assert_awaited_once()
        self.decline.assert_not_awaited()

    async def test_a_pine_exit_with_no_tracked_position_on_a_superseded_symbol_is_declined(
        self, monkeypatch
    ):
        """2026-10-13 HINDALCO: the Pine BREAKOUT entry was never traded (Python owns the symbol), yet
        its TP3 exit reached broker recovery and sent a BUY 320 that opened an unmanaged long."""
        monkeypatch.setattr(main, "settings", SimpleNamespace(runner=cfg(universe=("HINDALCO",))))
        monkeypatch.setattr(main, "tracker", SimpleNamespace(find_position=lambda sym, strat: None))
        await self._send(monkeypatch, self._signal(Direction.EXIT, "HINDALCO"))
        self.dispatch.assert_not_awaited()
        assert self.decline.await_args.kwargs["stage"] == "superseded"

    async def test_a_pine_exit_with_no_tracked_position_on_an_uncovered_symbol_still_passes(
        self, monkeypatch
    ):
        monkeypatch.setattr(main, "tracker", SimpleNamespace(find_position=lambda sym, strat: None))
        await self._send(monkeypatch, self._signal(Direction.EXIT, "RECLTD"))
        self.dispatch.assert_awaited_once()

    async def test_a_pine_entry_on_an_uncovered_symbol_is_traded(self, monkeypatch):
        await self._send(monkeypatch, self._signal(Direction.LONG, "RECLTD"))
        self.dispatch.assert_awaited_once()
