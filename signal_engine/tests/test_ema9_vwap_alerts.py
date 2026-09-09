"""Contract tests for the EMA9VWAP PineScript alert formats.

These pin the exact message shapes ema9-vwap.pine emits. The script and the engine
are edited independently and there is no compile-time link between them, so a change
to either alert builder or to the normalizer/parser regexes must fail here rather
than silently drop live signals.

Source: signal_engine/pinescripts/intraday/ema9-vwap/ema9-vwap.pine
"""

import pytest

from signal_engine.models import Direction
from signal_engine.normalizer import normalize
from signal_engine.parser import parse
from signal_engine.strategies import EMA9_VWAP

SEP = "------------------------"
URL = "https://www.tradingview.com/chart/?symbol=NSE:TCS&interval=5"


def _entry_alert(side: str = "LONG") -> str:
    return "\n".join([
        f"{EMA9_VWAP} {side} | TCS", SEP,
        "Setup: cross",
        "Entry: 3120.50", "Target: 3145.90", "SL: 3107.80",
        "RR: 1:2.0",
        "AtrPct: 0.38", "SlAtr: 1.05", "GapAtr: 0.02",
        "Vwap: 3118.25", "NearLevel: no", SEP,
        "10:25 IST", URL,
    ])


def _tp_alert() -> str:
    return "\n".join([
        f"{EMA9_VWAP} TP1 HIT | TCS", SEP,
        "Side: LONG", "Entry: 3120.50", "Exit: 3145.90",
        "ExitQtyPct: 100", SEP,
        "11:40 IST", URL,
    ])


def _sl_alert() -> str:
    return "\n".join([
        f"{EMA9_VWAP} SL HIT | TCS", SEP,
        "Side: LONG", "Entry: 3120.50", "Exit: 3107.80", SEP,
        "10:55 IST", URL,
    ])


def _exit_alert(reason: str = "TIME_EXIT") -> str:
    return "\n".join([
        f"{EMA9_VWAP} EXIT | TCS", SEP,
        f"Reason: {reason}",
        "Side: LONG", "Entry: 3120.50", "Exit: 3128.00",
        "SL: 3107.80", "TP: 3145.90", SEP,
        "14:45 IST", URL,
    ])


class TestEntryAlert:
    def test_parses_with_the_ema9vwap_tag(self):
        sig = parse(normalize(_entry_alert()))
        assert sig is not None
        assert sig.strategy == EMA9_VWAP
        assert sig.direction is Direction.LONG
        assert sig.symbol == "TCS"

    def test_carries_entry_sl_and_tp(self):
        sig = parse(normalize(_entry_alert()))
        # "Target:" is aliased to TP by the normalizer.
        assert (sig.entry, sig.sl, sig.tp) == (3120.50, 3107.80, 3145.90)

    def test_short_entry_parses(self):
        sig = parse(normalize(_entry_alert("SHORT")))
        assert sig.direction is Direction.SHORT

    @pytest.mark.parametrize("key", ["setup", "atrpct", "slatr", "gapatr", "vwap", "nearlevel"])
    def test_context_columns_survive(self, key):
        """Each becomes a column in the trade log, which is how the forward record
        gets compared against the backtest that says this entry has no edge."""
        sig = parse(normalize(_entry_alert()))
        assert key in sig.context

    def test_timestamp_and_url_do_not_become_columns(self):
        """The IST footer and the chart URL both match Key: Value by accident."""
        sig = parse(normalize(_entry_alert()))
        assert "https" not in sig.context
        assert not any(k.isdigit() for k in sig.context)


class TestTpAlert:
    def test_maps_to_exit_with_full_position(self):
        sig = parse(normalize(_tp_alert()))
        assert sig is not None
        assert sig.strategy == EMA9_VWAP
        assert sig.direction is Direction.EXIT
        assert sig.tp_level == "TP1"
        # ExitQtyPct 100 -> 1.0. The strategy owns its exit fractions, so config.yaml
        # needs no tp_levels entry for this tag.
        assert sig.exit_qty_pct == 1.0


class TestSlAlert:
    def test_maps_to_exit(self):
        sig = parse(normalize(_sl_alert()))
        assert sig is not None
        assert sig.strategy == EMA9_VWAP
        assert sig.direction is Direction.EXIT
        assert sig.symbol == "TCS"


class TestExitAlert:
    @pytest.mark.parametrize("reason", ["TIME_EXIT", "EMA_RECROSS"])
    def test_all_exit_reasons_parse(self, reason):
        sig = parse(normalize(_exit_alert(reason)))
        assert sig is not None
        assert sig.strategy == EMA9_VWAP
        assert sig.direction is Direction.EXIT
        assert sig.context["reason"] == reason

    def test_entry_is_on_its_own_line(self):
        """Regression: the compact "LONG | Entry: x" form yields NO entry field.

        parser.py matches ^(\\w+)\\s*:\\s*(.+)$, so "LONG | Entry: 3120.50" produces
        nothing. Unlike TP/SL HIT, a generic EXIT has no rewrite rule to synthesise
        the mandatory fields, so that shape drops the signal entirely.
        """
        sig = parse(normalize(_exit_alert()))
        assert sig.entry == 3120.50
        assert sig.context["side"] == "LONG"

        broken = _exit_alert().replace(
            "Side: LONG\nEntry: 3120.50", "LONG | Entry: 3120.50")
        assert parse(normalize(broken)) is None

    def test_tag_leads_the_header(self):
        """Regression: "TIME EXIT | TCS" parses as a strategy literally named TIME,
        because the pipe regex reads the word before EXIT as the tag."""
        broken = _exit_alert().replace(f"{EMA9_VWAP} EXIT | TCS", "TIME EXIT | TCS")
        sig = parse(normalize(broken))
        assert sig is None or sig.strategy != EMA9_VWAP
