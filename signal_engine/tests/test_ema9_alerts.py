"""Contract tests for the EMA9 PineScript alert formats.

These pin the exact message shapes ema9-intraday.pine emits. The script and the
engine are edited independently and there is no compile-time link between them, so
a change to either alert builder or to the normalizer/parser regexes must fail here
rather than silently drop live signals.

Source: signal_engine/pinescripts/intraday/ema9/ema9-intraday.pine
"""

import pytest

from signal_engine.models import Direction
from signal_engine.normalizer import normalize
from signal_engine.parser import parse
from signal_engine.strategies import EMA9

SEP = "------------------------"
URL = "https://www.tradingview.com/chart/?symbol=NSE:RELIANCE&interval=5"


def _entry_alert() -> str:
    return "\n".join([
        f"{EMA9} LONG | RELIANCE", SEP,
        "Setup: C-PULLBACK-ORH",
        "Entry: 1234.55", "Target: 1250.00", "SL: 1225.00", SEP,
        "TP2: 1260.00", "TP3: 1280.00", SEP,
        "Risk: 9.55", "Reward: 15.45", "RR: 1:1.6", "RefQty: 104", SEP,
        "Mode: C: Breakout Pullback", "Tf: 5", "Rvol: 1.45", "Adx: 24.3", "Rsi: 61.2",
        "AtrPct: 0.42", "SlAtr: 0.85", "EmaSlope: 0.062", "SepAtr: 0.31", "Crosses: 2",
        "Vwap: 1230.10", "Ema9: 1231.40", "Htf: BULL", "Level: 1228.00", SEP,
        "09:50 IST", URL,
    ])


def _tp_alert() -> str:
    return "\n".join([
        f"{EMA9} TP1 HIT | RELIANCE", SEP,
        "LONG | Entry: 1234.55", "Exit: 1250.00", SEP,
        "Profit: 15.45 (1.3%)", "Rmult: 1.62", "ExitQtyPct: 100", SEP,
        "09:58 IST", URL,
    ])


def _sl_alert() -> str:
    return "\n".join([
        f"{EMA9} SL HIT | RELIANCE", SEP,
        "LONG | Entry: 1234.55", "Exit: 1225.00", SEP,
        "Loss: -9.55 (-0.8%)", SEP,
        "10:12 IST", URL,
    ])


def _exit_alert(reason: str = "TIME_EXIT") -> str:
    return "\n".join([
        f"{EMA9} EXIT | RELIANCE", SEP,
        f"Reason: {reason}",
        "Side: LONG", "Entry: 1234.55", "Exit: 1241.00", "SL: 1225.00", "TP: 1250.00", SEP,
        "Pnl: 6.45 (0.5%)", "Rmult: 0.68", SEP,
        "14:45 IST", URL,
    ])


def _runner_alert() -> str:
    return "\n".join([
        "RUNNER | RELIANCE", SEP,
        "Reached TP2 after the position closed",
        "Ref Entry 1234.55 | Ref Level 1260.00",
        "Unbooked 2.67R", SEP,
        "11:30 IST", URL,
    ])


class TestEntryAlert:
    def test_parses_with_ema9_strategy_tag(self):
        sig = parse(normalize(_entry_alert()))
        assert sig is not None
        assert sig.strategy == EMA9
        assert sig.direction is Direction.LONG
        assert sig.symbol == "RELIANCE"

    def test_carries_entry_sl_and_tp(self):
        sig = parse(normalize(_entry_alert()))
        # "Target:" is aliased to TP by the normalizer.
        assert (sig.entry, sig.sl, sig.tp) == (1234.55, 1225.00, 1250.00)

    def test_setup_tag_reaches_context(self):
        """Setup: is what splits per-variant performance out of the trade log."""
        sig = parse(normalize(_entry_alert()))
        assert sig.context["setup"] == "C-PULLBACK-ORH"

    @pytest.mark.parametrize("key", [
        "mode", "tf", "rvol", "adx", "rsi", "atrpct", "slatr",
        "emaslope", "sepatr", "crosses", "vwap", "ema9", "htf", "level",
    ])
    def test_filter_context_columns_survive(self, key):
        sig = parse(normalize(_entry_alert()))
        assert key in sig.context

    def test_timestamp_and_url_do_not_become_columns(self):
        """The IST footer and chart URL both match Key: Value by accident."""
        sig = parse(normalize(_entry_alert()))
        assert "https" not in sig.context
        assert not any(k.isdigit() for k in sig.context)

    def test_short_entry_parses(self):
        msg = _entry_alert().replace(f"{EMA9} LONG", f"{EMA9} SHORT")
        sig = parse(normalize(msg))
        assert sig.direction is Direction.SHORT


class TestTpAlert:
    def test_maps_to_exit_with_full_position(self):
        sig = parse(normalize(_tp_alert()))
        assert sig is not None
        assert sig.strategy == EMA9
        assert sig.direction is Direction.EXIT
        assert sig.tp_level == "TP1"
        # ExitQtyPct 100 -> 1.0 fraction. The strategy owns its exit fractions, so no
        # tp_levels entry is needed in config.yaml.
        assert sig.exit_qty_pct == 1.0


class TestSlAlert:
    def test_maps_to_exit(self):
        sig = parse(normalize(_sl_alert()))
        assert sig is not None
        assert sig.strategy == EMA9
        assert sig.direction is Direction.EXIT
        assert sig.symbol == "RELIANCE"


class TestExitAlert:
    @pytest.mark.parametrize("reason", ["TIME_EXIT", "EMA_CROSS", "OPPOSITE_CROSS"])
    def test_all_exit_reasons_parse(self, reason):
        sig = parse(normalize(_exit_alert(reason)))
        assert sig is not None
        assert sig.strategy == EMA9
        assert sig.direction is Direction.EXIT
        assert sig.context["reason"] == reason

    def test_entry_is_on_its_own_line(self):
        """Regression: the compact "LONG | Entry: x" form yields NO entry field.

        parser.py matches ^(\\w+)\\s*:\\s*(.+)$, so "LONG | Entry: 1234.55" produces
        nothing. Unlike TP/SL HIT, a generic EXIT has no rewrite rule to synthesise
        the mandatory fields, so that shape drops the signal entirely.
        """
        sig = parse(normalize(_exit_alert()))
        assert sig.entry == 1234.55
        assert sig.context["side"] == "LONG"

        broken = _exit_alert().replace(
            "Side: LONG\nEntry: 1234.55", "LONG | Entry: 1234.55")
        assert parse(normalize(broken)) is None


class TestRunnerAlertIsNotTradeable:
    def test_runner_is_dropped_by_the_pipeline(self):
        """RUNNER carries no LONG/SHORT/EXIT token, so _parse_header returns None."""
        assert parse(normalize(_runner_alert())) is None


class TestStrategyRegistration:
    def test_ema9_is_registered_as_mis(self):
        """The 15:02 squareoff failsafe only covers strategies listed here."""
        from signal_engine.config import settings
        profile = settings.strategy_profiles.get(EMA9)
        assert profile is not None, "EMA9 missing from config.yaml strategy_profiles"
        assert profile["product"] == "MIS"

    def test_ema9_stop_floor_admits_its_stop_geometry(self):
        """9 EMA stops land in the same tight band as BREAKOUT key-level stops."""
        from signal_engine.config import settings
        assert settings.strategy_profiles[EMA9]["min_sl_pct"] == 0.002
