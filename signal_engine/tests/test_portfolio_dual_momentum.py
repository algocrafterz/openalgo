"""Unit tests for the dual-momentum absolute-filter extension to `portfolio.py`.

`PortfolioConfig.require_positive` (default False) is a backward-compatibility-
critical flag: momentum-rank's already-validated numbers, and
`validation.py`'s C+D calibration check, both call `PortfolioBacktest.run()` without
setting it and must keep getting byte-identical arithmetic. These tests build a
small, fully hand-controlled synthetic 3-symbol panel (not real market data) so the
expected book/cash-fraction numbers can be checked by direct arithmetic rather than
trusted from the implementation itself.
"""

from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest

from signal_engine.backtest.portfolio import PortfolioBacktest, PortfolioConfig

SYMS = ["A", "B", "C"]
DATES = pd.date_range("2026-01-01", periods=6, freq="1D")

# Open == Close within each day, for simple arithmetic.
PRICES = {
    "A": [100, 110, 121, 100, 90, 81],
    "B": [100, 105, 110, 100, 100, 100],
    "C": [100, 95, 90, 100, 110, 121],
}


def _frames() -> dict[str, pd.DataFrame]:
    return {
        s: pd.DataFrame({"Open": PRICES[s], "High": PRICES[s], "Low": PRICES[s],
                         "Close": PRICES[s]}, index=DATES)
        for s in SYMS
    }


def _factor() -> pd.DataFrame:
    """Only the two rebalance-signal dates (index 0 and 2) carry real values."""
    f = pd.DataFrame(index=DATES, columns=SYMS, dtype=float)
    # period 1 (rebalance at date0): B is top-2 by rank but has NEGATIVE own momentum
    f.loc[DATES[0]] = {"A": 0.05, "B": -0.02, "C": -0.10}
    # period 2 (rebalance at date2): both top-2 names already have positive momentum
    f.loc[DATES[2]] = {"A": -0.03, "B": 0.08, "C": 0.06}
    return f


def _cfg(**kw) -> PortfolioConfig:
    return PortfolioConfig(rebalance_days=2, top_n=2, cost_bps=0.0, min_price=0.0, **kw)


def test_default_config_does_not_apply_absolute_filter():
    assert PortfolioConfig().require_positive is False


def test_relative_only_keeps_a_negative_momentum_name_in_the_top_n():
    pb = PortfolioBacktest(_frames(), _cfg())
    r = pb.run(_factor())
    assert r["n"].iloc[0] == 2                    # both A and B held, despite B < 0
    assert r["cash_frac"].iloc[0] == 0.0
    expected = ((100 / 110 - 1) + (100 / 105 - 1)) / 2
    assert r["book"].iloc[0] == pytest.approx(expected, abs=1e-9)


def test_dual_momentum_excludes_negative_momentum_name_and_holds_cash():
    pb = PortfolioBacktest(_frames(), _cfg(require_positive=True))
    r = pb.run(_factor())
    assert r["n"].iloc[0] == 1                    # only A survives the positive filter
    assert r["cash_frac"].iloc[0] == pytest.approx(0.5)
    expected = (100 / 110 - 1) * 0.5               # A's return, half-weighted, half cash
    assert r["book"].iloc[0] == pytest.approx(expected, abs=1e-9)


def test_dual_momentum_matches_relative_only_when_top_n_is_already_all_positive():
    plain = PortfolioBacktest(_frames(), _cfg()).run(_factor())
    dual = PortfolioBacktest(_frames(), _cfg(require_positive=True)).run(_factor())
    # period 2: both B and C already have positive own-momentum, so the filter is a no-op
    assert dual["cash_frac"].iloc[1] == 0.0
    assert dual["book"].iloc[1] == pytest.approx(plain["book"].iloc[1], abs=1e-9)


def test_require_positive_is_the_only_field_that_changes_between_configs():
    a, b = _cfg(), _cfg(require_positive=True)
    assert replace(a, require_positive=True) == b
