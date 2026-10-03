"""Per-trade cost decomposition: components sum to the engine's modelled cost."""

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import costs
from signal_engine.backtest.types import Trade

COMPONENT_COLUMNS = [
    "cost_brokerage_bps",
    "cost_stt_bps",
    "cost_exchange_bps",
    "cost_gst_bps",
    "cost_sebi_bps",
    "cost_stamp_bps",
    "cost_slippage_bps",
]


def _trade(entry=100.0, risk=1.0, direction=1):
    return Trade(
        symbol="A.NS",
        day="2026-01-05",
        direction=direction,
        tag="t",
        entry_time="2026-01-05 09:20",
        entry=entry,
        signal_price=entry,
        sl=entry - direction * risk,
        tp=0.0,
        risk=risk,
        exit_time="2026-01-05 14:30",
        exit=entry + direction * risk,
        r_gross=1.0,
        r_net=0.9,
    )


def _model(**kw):
    base = {
        "capital": 100_000.0,
        "risk_per_trade": 0.01,
        "leverage": 5.0,
        "product": "MIS",
        "exchange": "NSE",
        "slippage_bps_per_side": 4.0,
    }
    return costs.CostModel(**{**base, **kw})


# small (brokerage % bound), mid, large (brokerage capped), leverage-capped
_SHAPES = [(100.0, 50.0), (100.0, 4.0), (100.0, 1.0), (500.0, 0.5), (2500.0, 1.0)]


@pytest.mark.parametrize("product", ["MIS", "CNC"])
@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("mult", [1.0, 2.0])
@pytest.mark.parametrize("entry,risk", _SHAPES)
def test_components_sum_equals_trade_cost(product, direction, mult, entry, risk):
    model = _model(product=product)
    t = _trade(entry, risk, direction)
    comps = model.components(t, mult)
    assert sum(comps[c] for c in COMPONENT_COLUMNS) == pytest.approx(
        model.trade_cost_bps(t, mult), abs=1e-9
    )
    assert comps["modeled_cost_bps"] == pytest.approx(model.trade_cost_bps(t, mult), abs=1e-9)


def test_components_keys_and_non_negative():
    comps = _model().components(_trade())
    assert list(comps) == [*COMPONENT_COLUMNS, "modeled_cost_bps"]
    assert all(v >= 0 for v in comps.values())


def test_intraday_stt_is_sell_side_only_and_stamp_buy_side_only():
    model = _model()
    t = _trade(entry=100.0, risk=1.0)
    comps = model.components(t)
    notional = model.notional(t)
    # sell leg only: 0.025% of notional -> 2.5 bps of entry notional
    assert comps["cost_stt_bps"] == pytest.approx(2.5)
    # buy leg only: 0.003% -> 0.3 bps
    assert comps["cost_stamp_bps"] == pytest.approx(0.3)
    assert notional > 0


def test_delivery_differs_from_intraday():
    t = _trade(entry=100.0, risk=1.0)
    mis = _model(product="MIS").components(t)
    cnc = _model(product="CNC").components(t)
    assert cnc["cost_stt_bps"] == pytest.approx(20.0)  # 0.1% on both legs
    assert cnc["cost_stamp_bps"] == pytest.approx(1.5)  # 0.015% buy leg
    assert cnc["cost_brokerage_bps"] == 0.0  # delivery brokerage is free
    assert cnc["cost_gst_bps"] < mis["cost_gst_bps"]
    assert mis["cost_brokerage_bps"] > 0.0


def test_exchange_sebi_gst_values():
    t = _trade(entry=100.0, risk=1.0)
    c = _model().components(t)
    # turnover is 2x notional: 0.00307% x 2 = 0.614 bps of entry notional
    assert c["cost_exchange_bps"] == pytest.approx(0.614)
    assert c["cost_sebi_bps"] == pytest.approx(0.02)
    taxed = c["cost_brokerage_bps"] + c["cost_exchange_bps"] + c["cost_sebi_bps"]
    assert c["cost_gst_bps"] == pytest.approx(0.18 * taxed)


def test_slippage_scales_with_multiplier_and_is_two_legs():
    model = _model(slippage_bps_per_side=3.0)
    t = _trade()
    assert model.components(t)["cost_slippage_bps"] == pytest.approx(6.0)
    assert model.components(t, 2.5)["cost_slippage_bps"] == pytest.approx(15.0)
    zero = _model(slippage_bps_per_side=0.0).components(t)
    assert zero["cost_slippage_bps"] == 0.0


def test_statutory_part_unchanged():
    model = _model()
    t = _trade(entry=100.0, risk=2.0)
    comps = model.components(t)
    stat = comps["modeled_cost_bps"] - comps["cost_slippage_bps"]
    assert stat == pytest.approx(model.statutory_bps(t), abs=1e-9)


class TestDecomposeTrades:
    def _trades(self):
        return [_trade(e, r, d) for (e, r) in _SHAPES for d in (1, -1)]

    @pytest.mark.parametrize("product", ["MIS", "CNC"])
    def test_matches_scalar_in_input_order(self, product):
        model = _model(product=product)
        trades = self._trades()
        df = costs.decompose_trades(trades, model)
        assert isinstance(df, pd.DataFrame)
        assert list(df.columns) == [*COMPONENT_COLUMNS, "modeled_cost_bps"]
        assert len(df) == len(trades)
        for i, t in enumerate(trades):
            scalar = model.components(t)
            for col in df.columns:
                assert df[col].iloc[i] == pytest.approx(scalar[col], abs=1e-9)
            assert df["modeled_cost_bps"].iloc[i] == pytest.approx(
                model.trade_cost_bps(t), abs=1e-9
            )

    def test_slippage_mult(self):
        model = _model()
        trades = self._trades()
        df = costs.decompose_trades(trades, model, slippage_mult=2.0)
        for i, t in enumerate(trades):
            assert df["modeled_cost_bps"].iloc[i] == pytest.approx(
                model.trade_cost_bps(t, 2.0), abs=1e-9
            )

    def test_empty(self):
        df = costs.decompose_trades([], _model())
        assert len(df) == 0
        assert list(df.columns) == [*COMPONENT_COLUMNS, "modeled_cost_bps"]

    def test_float64_and_nonnegative(self):
        df = costs.decompose_trades(self._trades(), _model())
        assert all(df[c].dtype == np.float64 for c in df.columns)
        assert (df >= 0).all().all()

    def test_row_sum_equals_modeled(self):
        df = costs.decompose_trades(self._trades(), _model())
        np.testing.assert_allclose(
            df[COMPONENT_COLUMNS].sum(axis=1), df["modeled_cost_bps"], atol=1e-9
        )


def test_cache_is_bounded():
    info = costs._component_bps.cache_info()
    assert info.maxsize is not None and info.maxsize <= 8192
    model = _model(leverage=1_000.0)
    # many distinct notionals map into rupee buckets; cache must not exceed maxsize
    trades = [_trade(100.0, 0.001 + i * 0.0003) for i in range(3000)]
    costs.decompose_trades(trades, model)
    assert costs._component_bps.cache_info().currsize <= info.maxsize


def test_shares_bucket_cache_with_trade_cost():
    costs._component_bps.cache_clear()
    costs._statutory_bps.cache_clear()
    model = _model()
    t = _trade()
    model.components(t)
    model.trade_cost_bps(t)
    # same notional bucket: a single entry each, nothing recomputed per trade
    assert costs._component_bps.cache_info().currsize == 1
