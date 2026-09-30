"""Purge and embargo at the in-sample / out-of-sample cut (G3.5)."""

import pandas as pd
import pytest

from signal_engine.backtest import splits
from signal_engine.backtest.types import Trade


def _t(day, exit_day=None, r=0.1):
    exit_day = exit_day or day
    return Trade(
        symbol="A.NS",
        day=day,
        direction=1,
        tag="t",
        entry_time=pd.Timestamp(f"{day} 09:20"),
        entry=100.0,
        signal_price=100.0,
        sl=99.0,
        tp=0.0,
        risk=1.0,
        exit_time=pd.Timestamp(f"{exit_day} 15:00"),
        exit=101.0,
        r_gross=r,
        r_net=r,
    )


class TestPurgeAndEmbargo:
    def test_trade_straddling_the_cut_is_dropped_from_in_sample(self):
        trades = [_t("2026-01-02"), _t("2026-01-09", exit_day="2026-01-14")]
        ins, oos, info = splits.purge_and_embargo(trades, "2026-01-12", embargo_sessions=0)
        assert [t.day for t in ins] == ["2026-01-02"]
        assert info["purged_in_sample"] == 1
        assert oos == []

    def test_embargo_skips_the_first_out_of_sample_sessions(self):
        trades = [_t("2026-01-12"), _t("2026-01-13"), _t("2026-01-14")]
        ins, oos, info = splits.purge_and_embargo(trades, "2026-01-12", embargo_sessions=1)
        assert [t.day for t in oos] == ["2026-01-13", "2026-01-14"]
        assert info["embargoed_oos"] == 1

    def test_embargo_counts_business_days_across_a_weekend(self):
        trades = [_t("2026-01-09"), _t("2026-01-12"), _t("2026-01-13")]
        _, oos, _ = splits.purge_and_embargo(trades, "2026-01-09", embargo_sessions=1)
        assert [t.day for t in oos] == ["2026-01-12", "2026-01-13"]

    def test_zero_embargo_and_no_straddlers_changes_nothing(self):
        trades = [_t("2026-01-02"), _t("2026-01-12")]
        ins, oos, info = splits.purge_and_embargo(trades, "2026-01-12", embargo_sessions=0)
        assert len(ins) == 1 and len(oos) == 1
        assert info == {"embargo_sessions": 0, "purged_in_sample": 0, "embargoed_oos": 0}

    def test_inputs_are_not_modified(self):
        trades = [_t("2026-01-02"), _t("2026-01-12")]
        snapshot = list(trades)
        splits.purge_and_embargo(trades, "2026-01-12", embargo_sessions=1)
        assert trades == snapshot

    def test_negative_embargo_is_rejected(self):
        with pytest.raises(ValueError):
            splits.purge_and_embargo([], "2026-01-12", embargo_sessions=-1)


class TestStraddlers:
    def test_counts_trades_that_span_the_cut(self):
        trades = [_t("2026-01-09", exit_day="2026-01-14"), _t("2026-01-02")]
        assert splits.count_straddlers(trades, "2026-01-12") == 1

    def test_missing_exit_time_is_not_counted(self):
        t = _t("2026-01-09")
        t = Trade(**{**t.__dict__, "exit_time": None})
        assert splits.count_straddlers([t], "2026-01-12") == 0
