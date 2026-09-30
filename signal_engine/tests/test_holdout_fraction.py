"""One hold-out standard: the last 30 percent of sessions (G3.1), everywhere."""

import inspect

from signal_engine.backtest import record_run, types
from signal_engine.backtest.harness import Backtest
from signal_engine.backtest.portfolio import PortfolioConfig


def test_single_constant_in_types():
    assert types.HOLDOUT_OOS_FRACTION == 0.30


def test_record_run_uses_the_shared_constant():
    assert record_run.OOS_FRACTION == types.HOLDOUT_OOS_FRACTION


def test_harness_default_in_sample_fraction_is_its_complement():
    default = inspect.signature(Backtest.__init__).parameters["is_fraction"].default
    assert default == 1.0 - types.HOLDOUT_OOS_FRACTION


def test_portfolio_backtest_default_matches():
    assert PortfolioConfig.__dataclass_fields__["is_fraction"].default == (
        1.0 - types.HOLDOUT_OOS_FRACTION
    )
