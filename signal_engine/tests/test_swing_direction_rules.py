"""Swing engine direction rule: Indian equity swing (CNC) is long-only."""

import pytest

from signal_engine.backtest.swing_engine import SwingRunConfig


class TestSwingDirectionRules:
    def test_default_is_long_only(self):
        run = SwingRunConfig()
        assert run.allow_longs is True
        assert run.allow_shorts is False

    def test_shorts_rejected_at_construction(self):
        with pytest.raises(ValueError, match="long-only"):
            SwingRunConfig(allow_shorts=True)

    def test_longs_disabled_rejected(self):
        with pytest.raises(ValueError, match="long-only"):
            SwingRunConfig(allow_longs=False)

    def test_with_revalidates(self):
        with pytest.raises(ValueError, match="long-only"):
            SwingRunConfig().with_(allow_shorts=True)

    def test_other_overrides_still_work(self):
        run = SwingRunConfig().with_(cost_bps=24.0, max_hold_bars=60)
        assert run.cost_bps == 24.0
        assert run.max_hold_bars == 60
        assert run.allow_shorts is False


class TestSwingStrategyHarnessContract:
    def test_swing_strategy_has_prepare_for_delegating_to_prepare(self):
        import pandas as pd

        from signal_engine.backtest.strategies.swing_base import SwingStrategy

        class Dummy(SwingStrategy):
            def prepare(self, df, p):
                return df.assign(marker=1)

            def entry_at_open(self, c, i, p, direction):
                return None

        df = pd.DataFrame({"Close": [1.0, 2.0]})
        out = Dummy().prepare_for("SBIN", df, None)
        assert list(out["marker"]) == [1, 1]
