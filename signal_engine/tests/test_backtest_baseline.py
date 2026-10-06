"""Random-entry baseline: what the same names would have earned entered on arbitrary days.

A long-only strategy tested through a rising market earns the market. Dividend growth
showed +380 bps a trade and was still rejected because random entry with the same holding
period earned more. The baseline puts that comparison in every run record, so a result that
is only beta cannot pass for skill.
"""

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest.baseline import random_entry_baseline
from signal_engine.backtest.types import Trade

IST = "Asia/Kolkata"


def _frame(closes, start="2024-01-01"):
    idx = pd.bdate_range(start, periods=len(closes), tz=IST)
    close = pd.Series(np.asarray(closes, dtype=float), index=idx)
    # Each bar opens at the previous close, so entering at the open of bar j and leaving at
    # the close of bar j+h spans h+1 bars of the underlying move.
    return pd.DataFrame({"Open": close.shift(1).fillna(close), "Close": close})


def _drift(n=400, daily=0.001):
    return _frame(100.0 * (1 + daily) ** np.arange(n))


def _trade(frame, i0, i1, symbol="A.NS", direction=1, r_net=0.0, risk_pct=1.0, cost_bps=0.0):
    price = 100.0
    return Trade(
        symbol=symbol,
        day=frame.index[i0].date(),
        direction=direction,
        tag="t",
        entry_time=frame.index[i0],
        entry=price,
        signal_price=price,
        sl=price * (1 - risk_pct / 100),
        tp=0.0,
        risk=price * risk_pct / 100,
        exit_time=frame.index[i1],
        exit=price,
        reason="x",
        r_gross=r_net + cost_bps / (risk_pct * 100),
        r_net=r_net,
    )


class TestBaselineLevel:
    def test_constant_drift_gives_the_exact_compounded_return_whatever_the_entry_day(self):
        frame = _drift()
        out = random_entry_baseline([_trade(frame, 50, 60)], {"A.NS": frame})
        assert out["gross_bps"] == pytest.approx(((1.001**11) - 1) * 1e4, rel=1e-3)

    def test_costs_are_deducted_once_per_round_trip(self):
        frame = _drift()
        out = random_entry_baseline([_trade(frame, 50, 60, cost_bps=24.0)], {"A.NS": frame})
        assert out["net_bps"] == pytest.approx(out["gross_bps"] - 24.0)

    def test_short_trades_use_the_opposite_sign(self):
        frame = _drift()
        long_ = random_entry_baseline([_trade(frame, 50, 60)], {"A.NS": frame})
        short = random_entry_baseline([_trade(frame, 50, 60, direction=-1)], {"A.NS": frame})
        assert short["gross_bps"] < 0 < long_["gross_bps"]

    def test_hold_is_counted_in_bars_not_calendar_days(self):
        # Friday entry, Monday exit is ONE bar later, not three days: two bars of move.
        frame = _drift()
        fri = int(np.where(frame.index.dayofweek == 4)[0][3])
        out = random_entry_baseline([_trade(frame, fri, fri + 1)], {"A.NS": frame})
        assert out["gross_bps"] == pytest.approx(((1.001**2) - 1) * 1e4, rel=1e-3)

    def test_a_same_day_exit_is_matched_open_to_close(self):
        # A swing engine can enter and leave on the same daily bar. That is a zero-bar hold
        # and must be matched, not dropped, or the excess compares a biased subset.
        frame = _drift()
        out = random_entry_baseline([_trade(frame, 50, 50)], {"A.NS": frame})
        assert out is not None and out["skipped"] == 0
        assert out["gross_bps"] == pytest.approx(0.001 * 1e4, rel=1e-3)

    def test_an_exit_before_the_entry_is_skipped(self):
        frame = _drift()
        out = random_entry_baseline([_trade(frame, 50, 60), _trade(frame, 70, 60)], {"A.NS": frame})
        assert out["trades"] == 1 and out["skipped"] == 1


class TestExcess:
    def test_excess_is_the_trade_result_minus_the_baseline(self):
        frame = _drift()
        # risk 1% and r_net 3 -> 300 bps net for the trade
        trade = _trade(frame, 50, 60, r_net=3.0, risk_pct=1.0)
        out = random_entry_baseline([trade], {"A.NS": frame})
        assert out["excess_net_bps"] == pytest.approx(300.0 - out["net_bps"], abs=1e-3)

    def test_a_strategy_that_only_earns_the_drift_has_no_excess(self):
        frame = _drift()
        gross = ((1.001**11) - 1) * 1e4
        trades = [_trade(frame, 20 + 15 * k, 30 + 15 * k, r_net=gross / 100.0) for k in range(20)]
        out = random_entry_baseline(trades, {"A.NS": frame})
        assert out["excess_net_bps"] == pytest.approx(0.0, abs=1.0)

    def test_trade_net_is_reported_over_the_matched_trades_only(self):
        frame = _drift()
        trades = [
            _trade(frame, 50, 60, r_net=2.0),
            _trade(frame, 50, 60, symbol="ZZ.NS", r_net=100.0),
        ]
        out = random_entry_baseline(trades, {"A.NS": frame})
        assert out["trade_net_bps"] == pytest.approx(
            200.0
        )  # 2R at 1% risk, the unmatched 100R ignored
        assert out["excess_net_bps"] == pytest.approx(
            out["trade_net_bps"] - out["net_bps"], abs=1e-3
        )

    def test_excess_t_stat_needs_three_days(self):
        frame = _drift()
        two = [_trade(frame, 50, 60), _trade(frame, 70, 80)]
        assert "excess_t_stat" not in random_entry_baseline(two, {"A.NS": frame})
        many = [_trade(frame, 20 + 10 * k, 25 + 10 * k, r_net=1.0 + k * 0.01) for k in range(12)]
        assert "excess_t_stat" in random_entry_baseline(many, {"A.NS": frame})


class TestReproducibleAndSafe:
    @staticmethod
    def _walk():
        rng = np.random.default_rng(3)
        return _frame(100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, 500))))

    def test_same_seed_same_answer_and_different_seed_differs(self):
        frame = self._walk()
        trades = [_trade(frame, 40 + 20 * k, 50 + 20 * k) for k in range(10)]
        a = random_entry_baseline(trades, {"A.NS": frame}, seed=1)
        b = random_entry_baseline(trades, {"A.NS": frame}, seed=1)
        c = random_entry_baseline(trades, {"A.NS": frame}, seed=2)
        assert a == b
        assert a["gross_bps"] != c["gross_bps"]

    def test_trades_on_unknown_symbols_are_skipped_and_counted(self):
        frame = _drift()
        trades = [_trade(frame, 50, 60), _trade(frame, 50, 60, symbol="ZZ.NS")]
        out = random_entry_baseline(trades, {"A.NS": frame})
        assert out["trades"] == 1 and out["skipped"] == 1

    def test_no_matchable_trades_returns_none(self):
        frame = _drift()
        assert (
            random_entry_baseline([_trade(frame, 50, 60, symbol="ZZ.NS")], {"A.NS": frame}) is None
        )
        assert random_entry_baseline([], {"A.NS": frame}) is None

    def test_a_hold_as_long_as_the_history_is_skipped_not_crashed(self):
        frame = _drift(n=30)
        out = random_entry_baseline([_trade(frame, 0, 29), _trade(frame, 5, 10)], {"A.NS": frame})
        assert out["trades"] == 1 and out["skipped"] == 1

    def test_inputs_are_not_modified_and_result_is_strict_json(self):
        import json

        frame = self._walk()
        before = frame.copy()
        out = random_entry_baseline([_trade(frame, 40, 50)], {"A.NS": frame})
        pd.testing.assert_frame_equal(frame, before)
        json.dumps(out, allow_nan=False)

    def test_method_is_described_in_the_record(self):
        frame = _drift()
        out = random_entry_baseline([_trade(frame, 50, 60)], {"A.NS": frame})
        assert "random entry" in out["method"] and out["draws_per_trade"] > 0


class TestPerTradeCost:
    def test_baseline_charges_each_trades_own_cost(self):
        frame = _drift()
        cheap = _trade(frame, 50, 60, cost_bps=10.0)
        dear = _trade(frame, 70, 80, cost_bps=50.0)
        alone_cheap = random_entry_baseline([cheap], {"A.NS": frame})
        alone_dear = random_entry_baseline([dear], {"A.NS": frame})
        both = random_entry_baseline([cheap, dear], {"A.NS": frame})
        assert alone_cheap["gross_bps"] - alone_cheap["net_bps"] == pytest.approx(10.0)
        assert alone_dear["gross_bps"] - alone_dear["net_bps"] == pytest.approx(50.0)
        # The pooled baseline pays the average of the two trades' own costs, not one figure.
        assert both["gross_bps"] - both["net_bps"] == pytest.approx(30.0, abs=1e-3)

    def test_baseline_unchanged_for_flat_trades(self):
        # Every trade paying the same flat cost: the result is that flat cost deducted once.
        frame = _drift()
        trades = [_trade(frame, 20 + 15 * k, 30 + 15 * k, cost_bps=16.0) for k in range(10)]
        out = random_entry_baseline(trades, {"A.NS": frame})
        assert out["net_bps"] == pytest.approx(out["gross_bps"] - 16.0, abs=1e-3)
