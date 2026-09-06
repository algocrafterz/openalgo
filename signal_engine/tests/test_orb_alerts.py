"""Contract tests for the ORB PineScript alert formats.

These pin the exact message shapes orb.pine emits. The script and the engine are
edited independently and there is no compile-time link between them, so a change to
either alert builder or to the normalizer/parser regexes must fail here rather than
silently drop live signals.

The time-exit case is the reason this file exists. The shipped shape was

    TIME EXIT | SYM
    LONG | Entry: 440

which the pipeline dropped twice over: `_parse_header` reads the word before EXIT as
the strategy tag, so the tag became "TIME"; and `^(\\w+)\\s*:\\s*(.+)$` cannot match an
`Entry:` that sits behind a pipe, so the message carried no entry price either. Unlike
TP/SL HIT there is no normalizer rewrite rule to synthesise the missing fields, so every
ORB time exit vanished between April and August 2026 - 47% of that channel's signals
never received an exit of any kind. Both halves are asserted below.

Source: signal_engine/pinescripts/intraday/orb/orb.pine
"""

import pytest

from signal_engine.models import Direction
from signal_engine.normalizer import normalize
from signal_engine.parser import parse
from signal_engine.strategies import ORB

SEP = "------------------------"
URL = "https://www.tradingview.com/chart/?symbol=NSE:SBIN&interval=5"


def _entry_alert() -> str:
    return "\n".join([
        f"\U0001f7e2 {ORB} LONG | SBIN", SEP,
        "Entry: 1044.3", "Target: 1055.09", "SL: 1030.81", SEP,
        "TP1.5: 1060.48", "TP2: 1065.88", "TP3: 1076.66", SEP,
        "Risk: 13.49 | Reward: 10.79", "R:R 1:0.8", SEP,
        "09:45 IST", URL,
    ])


def _tp_alert() -> str:
    return "\n".join([
        f"✅ {ORB} TP1 HIT | SBIN", SEP,
        "\U0001f7e2 LONG | Entry: 1044.3", "Exit: 1055.09", SEP,
        "Profit: +10.79 (+1.0%)", "Risk: 13.49 | Reward: 10.79",
        "R:R 1:0.8", "ExitQtyPct: 50", SEP,
        "11:20 IST", URL,
    ])


def _sl_alert() -> str:
    return "\n".join([
        f"❌ {ORB} SL HIT | SBIN", SEP,
        "\U0001f7e2 LONG | Entry: 1044.3", "Exit: 1030.81", SEP,
        "Loss: -13.49 (-1.3%)", "Risk: 13.49 | Reward: 10.79", "R:R was 1:0.8", SEP,
        "10:15 IST", URL,
    ])


def _time_exit_alert() -> str:
    """The CURRENT shape - tag in the header, one field per line."""
    return "\n".join([
        f"⏰ {ORB} EXIT | SBIN", SEP,
        "Reason: TIME_EXIT", "Side: \U0001f7e2 LONG",
        "Entry: 1044.3", "Exit: 1048.0", "SL: 1030.81", "TP: 1055.09", SEP,
        "P&L: +3.7 (+0.4%)", "Risk: 13.49 | Reward: 10.79", "R:R was 1:0.8", SEP,
        "15:00 IST", URL,
    ])


def _legacy_time_exit_alert() -> str:
    """The shape that was live until this fix. Must STAY unparseable."""
    return "\n".join([
        "⏰ TIME EXIT | SBIN", SEP,
        "\U0001f7e2 LONG | Entry: 1044.3", "Exit: 1048.0", SEP,
        "P&L: +3.7 (+0.4%)", "Risk: 13.49 | Reward: 10.79", "R:R was 1:0.8", SEP,
        "15:00 IST", URL,
    ])


def test_entry_alert_round_trips():
    sig = parse(normalize(_entry_alert()))
    assert sig is not None
    assert sig.strategy == ORB
    assert sig.symbol == "SBIN"
    assert sig.direction is Direction.LONG
    assert sig.entry == pytest.approx(1044.3)
    assert sig.sl == pytest.approx(1030.81)
    assert sig.tp == pytest.approx(1055.09)


@pytest.mark.parametrize("build", [_tp_alert, _sl_alert])
def test_exit_alerts_round_trip(build):
    sig = parse(normalize(build()))
    assert sig is not None
    assert sig.strategy == ORB
    assert sig.symbol == "SBIN"
    assert sig.direction is Direction.EXIT


def test_tp_alert_carries_partial_exit_fraction():
    """The script owns its exit fractions; the engine must see ExitQtyPct, not guess.\n\n    main._resolve_exit_qty treats signal.exit_qty_pct as the source of truth and only\n    falls back to strategy_profiles.tp_levels when it is absent, so losing this field\n    silently converts a 50% partial into a full exit.\n    """
    sig = parse(normalize(_tp_alert()))
    assert sig is not None
    assert sig.exit_qty_pct == pytest.approx(0.5)   # stored as a fraction


def test_time_exit_alert_round_trips():
    """The regression this file was written for."""
    sig = parse(normalize(_time_exit_alert()))
    assert sig is not None, "ORB time exit no longer parses - the residual position leaks"
    assert sig.strategy == ORB
    assert sig.symbol == "SBIN"
    assert sig.direction is Direction.EXIT
    assert sig.entry == pytest.approx(1044.3)
    assert sig.sl == pytest.approx(1030.81)
    assert sig.context.get("reason") == "TIME_EXIT"


def test_legacy_time_exit_shape_still_fails():
    """Pins the bug so the broken shape cannot be reintroduced by a copy-paste."""
    assert parse(normalize(_legacy_time_exit_alert())) is None
