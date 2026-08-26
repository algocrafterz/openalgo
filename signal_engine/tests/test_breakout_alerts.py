"""Contract tests for breakout.pine alert formats.

Source: signal_engine/pinescripts/intraday/orb/breakout.pine
"""

from signal_engine.models import Direction
from signal_engine.normalizer import normalize
from signal_engine.parser import parse

SEP = "------------------------"
URL = "https://www.tradingview.com/chart/?symbol=NSE:VEDL&interval=5"


def _time_exit_alert() -> str:
    return "\n".join([
        "⏰ BREAKOUT EXIT | VEDL", SEP,
        "Reason: TIME_EXIT",
        "Side: \U0001f7e2 LONG",
        "Entry: 440.00", "Exit: 441.00", "SL: 435.00", "TP: 450.00", SEP,
        "P&L: +1.00 (+0.2%)", "Risk: 5.00 | Reward: 10.00", "R:R was 1:2.0", SEP,
        "14:45 IST", URL,
    ])


class TestTimeExitAlertRegression:
    """The time-exit alert used to be dropped silently by the pipeline.

    The old shape was "TIME EXIT | SYM" over "LONG | Entry: 440.00". parser.py matches
    ^(\\w+)\\s*:\\s*(.+)$, so the compact direction+entry line produced no `entry` field,
    and unlike TP HIT / SL HIT there is no rewrite rule to synthesise the mandatory
    fields — so the whole message parsed to None and never reached the engine.
    """

    def test_parses_and_keeps_the_breakout_tag(self):
        sig = parse(normalize(_time_exit_alert()))
        assert sig is not None, "time exit alert must not be dropped"
        assert sig.strategy == "BREAKOUT"
        assert sig.direction is Direction.EXIT
        assert sig.symbol == "VEDL"

    def test_mandatory_price_fields_survive(self):
        sig = parse(normalize(_time_exit_alert()))
        assert (sig.entry, sig.sl, sig.tp) == (440.00, 435.00, 450.00)

    def test_reason_and_side_reach_context(self):
        sig = parse(normalize(_time_exit_alert()))
        assert sig.context["reason"] == "TIME_EXIT"
        assert sig.context["side"] == "LONG"

    def test_old_compact_shape_would_still_be_dropped(self):
        """Pins WHY the fix was needed, so the shape cannot be reintroduced."""
        old = "\n".join([
            "⏰ TIME EXIT | VEDL", SEP,
            "\U0001f7e2 LONG | Entry: 440.00", "Exit: 441.00", SEP,
            "P&L: +1.00 (+0.2%)", SEP, "14:45 IST", URL,
        ])
        assert parse(normalize(old)) is None
