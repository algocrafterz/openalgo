"""Pure helpers in main.py."""


from signal_engine.main import compute_next_tp
from signal_engine.models import (
    Direction,
)
from signal_engine.tracker import TrackedPosition


class TestComputeNextTp:
    """Unit tests for compute_next_tp helper."""

    def _make_pos(self, entry: float, tp: float, direction: Direction = Direction.LONG) -> TrackedPosition:
        return TrackedPosition(
            symbol="TEST", strategy="ORB", exchange="NSE", product="MIS",
            entry_price=entry, quantity=10, sl=0.0, tp=tp, direction=direction,
        )

    def test_tp1_long_returns_tp1_5(self):
        # TMPV: entry=339.8, TP1=344.65 → R=4.85 → TP1.5=entry+1.5R=347.075
        pos = self._make_pos(entry=339.8, tp=344.65)
        result = compute_next_tp(pos, "TP1")
        assert result is not None
        label, price = result
        assert label == "TP1.5"
        assert abs(price - 347.075) < 0.001

    def test_tp1_5_long_returns_tp2(self):
        # SAIL: entry=165.29, TP1=167.66 → R=2.37 → TP2=entry+2.0R=170.03
        pos = self._make_pos(entry=165.29, tp=167.66)
        result = compute_next_tp(pos, "TP1.5")
        assert result is not None
        label, price = result
        assert label == "TP2"
        assert abs(price - 170.03) < 0.01

    def test_tp2_returns_none(self):
        pos = self._make_pos(entry=339.8, tp=344.65)
        assert compute_next_tp(pos, "TP2") is None

    def test_unknown_level_returns_none(self):
        pos = self._make_pos(entry=339.8, tp=344.65)
        assert compute_next_tp(pos, "TP3") is None

    def test_none_tp_level_returns_none(self):
        pos = self._make_pos(entry=339.8, tp=344.65)
        assert compute_next_tp(pos, None) is None

    def test_zero_tp_returns_none(self):
        pos = self._make_pos(entry=339.8, tp=0.0)
        assert compute_next_tp(pos, "TP1") is None

    def test_zero_entry_returns_none(self):
        pos = self._make_pos(entry=0.0, tp=344.65)
        assert compute_next_tp(pos, "TP1") is None

    def test_tp1_short_returns_tp1_5_below_entry(self):
        # SHORT: entry=500, TP1=490 → R=10 → TP1.5=entry-1.5R=485
        pos = self._make_pos(entry=500.0, tp=490.0, direction=Direction.SHORT)
        result = compute_next_tp(pos, "TP1")
        assert result is not None
        label, price = result
        assert label == "TP1.5"
        assert abs(price - 485.0) < 0.001

    def test_case_insensitive_tp_level(self):
        pos = self._make_pos(entry=339.8, tp=344.65)
        result_upper = compute_next_tp(pos, "TP1")
        result_lower = compute_next_tp(pos, "tp1")
        assert result_upper == result_lower
