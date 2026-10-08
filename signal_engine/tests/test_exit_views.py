"""The economic and strategy views of a run, and the DATA_GAP exception (spec items 19 and 20)."""

import datetime as dt

import pandas as pd
import pytest

from signal_engine.backtest.exit_views import exit_views, split_data_gap
from signal_engine.backtest.tearsheet import build_run_record
from signal_engine.backtest.types import Trade
from signal_engine.strategy_core import trade_policy as tp

T0 = pd.Timestamp("2026-06-04 10:00", tz="Asia/Kolkata")


def trade(reason, r_gross, r_net, symbol="AAA", day=dt.date(2026, 6, 4)):
    return Trade(
        symbol=symbol, day=day, direction=1, tag="T", entry_time=T0, entry=100.0, signal_price=100.0,
        sl=98.0, tp=104.0, risk=2.0, exit_time=T0 + pd.Timedelta(minutes=30), exit=100.0,
        reason=reason, r_gross=r_gross, r_net=r_net, legs=1,
    )


class TestClassifyExit:
    @pytest.mark.parametrize(
        "reason,expected",
        [("SL", tp.STRATEGY_EXIT), ("SL_GAP", tp.STRATEGY_EXIT), ("TP", tp.STRATEGY_EXIT),
         ("TIME_EXIT", tp.TIME_EXIT), ("EOD", tp.TIME_EXIT), ("DATA_GAP", tp.DATA_GAP),
         ("BAD_FILL_CLOSE", tp.BAD_FILL_CLOSE), ("SL_GAP_ENTRY", tp.BAD_FILL_CLOSE)],
    )
    def test_table(self, reason, expected):
        assert tp.classify_exit(reason) == expected


class TestViews:
    TRADES = [
        trade("TP", 1.5, 1.4),
        trade("SL", -1.0, -1.1),
        trade("SL", -1.0, -1.1),
        trade("TIME_EXIT", 0.2, 0.1),
        trade("BAD_FILL_CLOSE", 0.0, -0.3),
    ]

    def test_the_economic_view_carries_every_realised_event(self):
        v = exit_views(self.TRADES)
        assert v["economic"]["trades"] == 5
        assert v["economic"]["net_r_total"] == pytest.approx(1.4 - 1.1 - 1.1 + 0.1 - 0.3)

    def test_the_strategy_view_is_decided_exits_only(self):
        v = exit_views(self.TRADES)["strategy"]
        assert v["decided_trades"] == 3
        assert v["win_rate_net"] == pytest.approx(1 / 3, abs=1e-5)
        assert v["avg_r_net"] == pytest.approx((1.4 - 1.1 - 1.1) / 3, abs=1e-5)

    def test_protection_events_are_listed_separately_with_their_cost(self):
        p = exit_views(self.TRADES)["protection"]
        assert p["TIME_EXIT"] == {"trades": 1, "net_r_total": 0.1, "net_r_mean": 0.1}
        assert p["BAD_FILL_CLOSE"]["net_r_total"] == pytest.approx(-0.3)

    def test_a_bad_fill_loss_never_leaves_the_economic_view(self):
        with_loss = exit_views(self.TRADES)["economic"]["net_r_total"]
        without = exit_views([t for t in self.TRADES if t.reason != "BAD_FILL_CLOSE"])["economic"]["net_r_total"]
        assert with_loss == pytest.approx(without - 0.3)

    def test_an_empty_run_does_not_fail(self):
        v = exit_views([])
        assert v["economic"]["trades"] == 0 and v["strategy"]["decided_trades"] == 0


class TestDataGap:
    def test_data_gap_closes_are_split_out_and_listed_with_date_and_symbol(self):
        gap = trade("DATA_GAP", 0.1, 0.0, symbol="ZZZ", day=dt.date(2026, 6, 5))
        kept, removed = split_data_gap([trade("TP", 1.0, 0.9), gap])
        assert [t.reason for t in kept] == ["TP"] and removed == [gap]
        v = exit_views(kept, removed)
        assert v["data_gap"]["trades"] == 1
        assert v["data_gap"]["closes"] == [{"symbol": "ZZZ", "day": "2026-06-05"}]
        assert v["economic"]["trades"] == 1  # never substituted into the normal evidence


class TestRunRecord:
    @staticmethod
    def _record(trades):
        return build_run_record(
            trades, run_id="r", run_date="2026-10-08", code_version="x", data={"source": "test"},
            period={"start": "2026-06-01", "end": "2026-06-30"},
            costs={"round_trip_bps": 16.0, "model": "flat"},
        )

    def test_the_record_carries_both_views_and_the_gates_read_the_economic_one(self):
        trades = [trade("TP", 1.5, 1.4), trade("BAD_FILL_CLOSE", 0.0, -0.3), trade("TIME_EXIT", 0.2, 0.1)]
        rec = self._record(trades)
        assert rec["exit_views"]["economic"]["trades"] == 3
        assert rec["exit_views"]["strategy"]["decided_trades"] == 1
        assert rec["exit_views"]["protection"]["BAD_FILL_CLOSE"]["trades"] == 1

    def test_a_data_gap_close_leaves_the_metrics_and_is_reported(self):
        base = [trade("TP", 1.5, 1.4), trade("SL", -1.0, -1.1)]
        gap = trade("DATA_GAP", 0.0, -5.0, symbol="ZZZ")
        rec = self._record([*base, gap])
        assert rec["exit_views"]["data_gap"]["trades"] == 1
        assert rec["metrics"] == self._record(base)["metrics"]
