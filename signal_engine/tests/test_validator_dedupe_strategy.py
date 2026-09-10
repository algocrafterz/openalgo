"""H3: duplicate suppression must not collide across strategies.

The key was (symbol, direction, tp_level or entry) with no strategy component. For an EXIT
the entry is the synthesized 0.0, so the key reduced to (SYMBOL, "EXIT", "TP1") — shared by
every strategy holding that name. BREAKINGTRADE and BREAKINGTRADE-WATCHLIST are built to
hold the same symbol at once with the same TP math, so two simultaneous "TP1 HIT" alerts
meant the second was dropped as a duplicate and THAT POSITION NEVER EXITED — it rode to the
14:45 time exit. ORB and BREAKOUT share the same NSE universe and can collide the same way.
"""

import pytest

from signal_engine.models import Direction, ValidationStatus
from signal_engine.tests.conftest import make_signal as _make_signal
from signal_engine.validator import _recent_signals, validate


@pytest.fixture(autouse=True)
def _clear_dedupe_state():
    _recent_signals.clear()
    yield
    _recent_signals.clear()


class TestExitSignalsDoNotCollideAcrossStrategies:
    def _exit(self, strategy: str, tp_level: str = "TP1"):
        return _make_signal(
            strategy=strategy, direction=Direction.EXIT, symbol="TATAMOTORS",
            entry=0.0, sl=0.0, tp=0.0, tp_level=tp_level,
        )

    def test_two_strategies_may_exit_the_same_symbol_at_the_same_tp(self):
        assert validate(self._exit("BREAKINGTRADE")).status == ValidationStatus.VALID
        assert validate(self._exit("BREAKINGTRADE-WATCHLIST")).status == ValidationStatus.VALID

    def test_same_strategy_repeating_itself_is_still_a_duplicate(self):
        assert validate(self._exit("BREAKINGTRADE")).status == ValidationStatus.VALID
        result = validate(self._exit("BREAKINGTRADE"))
        assert result.status == ValidationStatus.IGNORED
        assert "Duplicate" in result.reason

    def test_different_tp_levels_of_one_strategy_both_pass(self):
        assert validate(self._exit("ORB", "TP1")).status == ValidationStatus.VALID
        assert validate(self._exit("ORB", "TP1.5")).status == ValidationStatus.VALID


class TestEntrySignalsDoNotCollideAcrossStrategies:
    def test_same_symbol_direction_and_entry_from_two_strategies(self):
        a = _make_signal(strategy="ORB", symbol="SBIN", entry=800.0, sl=796.0, tp=810.0)
        b = _make_signal(strategy="BREAKOUT", symbol="SBIN", entry=800.0, sl=796.0, tp=810.0)
        assert validate(a).status == ValidationStatus.VALID
        assert validate(b).status == ValidationStatus.VALID

    def test_same_strategy_repeating_itself_is_still_a_duplicate(self):
        a = _make_signal(strategy="ORB", symbol="SBIN", entry=800.0, sl=796.0, tp=810.0)
        assert validate(a).status == ValidationStatus.VALID
        assert validate(a).status == ValidationStatus.IGNORED
