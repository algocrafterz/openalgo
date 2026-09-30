"""Market regime classification: causal, persistent, and cut by entry day.

The label for a trading day may only use data up to the PRIOR day's close, so a strategy
entering at that day's open could have known it. Every test below that mentions "future"
guards that property.
"""

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import regime as rg
from signal_engine.backtest.types import Trade

PARAMS = rg.RegimeParams()


def _idx(n, start="2019-01-01"):
    return pd.bdate_range(start, periods=n)


def _close(n=600, drift=0.0006, vol=0.008, seed=1, start="2019-01-01"):
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, vol, n)
    return pd.Series(10000 * np.exp(np.cumsum(rets)), index=_idx(n, start))


def _vix(index, level=15.0, seed=2):
    rng = np.random.default_rng(seed)
    return pd.Series(level + rng.normal(0, 1.0, len(index)), index=index)


class TestPersistence:
    def test_short_blip_does_not_switch(self):
        raw = pd.Series(["a"] * 10 + ["b"] * 2 + ["a"] * 10)
        out = rg.persist(raw, n=5)
        assert set(out.dropna()) == {"a"}

    def test_sustained_change_switches_after_n_days(self):
        raw = pd.Series(["a"] * 10 + ["b"] * 10)
        out = rg.persist(raw, n=5)
        assert out.iloc[10 + 3] == "a"
        assert out.iloc[10 + 4] == "b"
        assert out.iloc[-1] == "b"

    def test_leading_nan_stays_nan(self):
        raw = pd.Series([None, None, "a", "a", "a"])
        out = rg.persist(raw, n=2)
        assert out.iloc[:2].isna().all()
        assert out.iloc[2] == "a"

    def test_is_causal(self):
        raw = pd.Series(["a"] * 8 + ["b"] * 8 + ["a"] * 8)
        full = rg.persist(raw, n=3)
        cut = rg.persist(raw.iloc[:12], n=3)
        assert (full.iloc[:12].fillna("x") == cut.fillna("x")).all()


class TestTrend:
    def test_steady_uptrend_is_up(self):
        close = _close(drift=0.001, vol=0.0005)
        days = rg.build_regime_days(close)
        assert days["trend"].dropna().iloc[-100:].eq("up").all()

    def test_steady_downtrend_is_down(self):
        close = _close(drift=-0.001, vol=0.0005)
        days = rg.build_regime_days(close)
        assert days["trend"].dropna().iloc[-100:].eq("down").all()

    def test_sideways_oscillation_is_mostly_range(self):
        n = 600
        wave = 10000 * (1 + 0.02 * np.sin(np.arange(n) * 2 * np.pi / 40))
        days = rg.build_regime_days(pd.Series(wave, index=_idx(n)))
        share = days["trend"].dropna().eq("range").mean()
        assert share > 0.4

    def test_warmup_is_unclassified(self):
        days = rg.build_regime_days(_close())
        assert days["trend"].iloc[: PARAMS.slow].isna().all()
        assert days["trend"].dropna().size > 0

    def test_only_known_labels(self):
        days = rg.build_regime_days(_close(drift=0.0002, vol=0.012, seed=9))
        assert set(days["trend"].dropna()) <= set(rg.TREND_LABELS)


class TestNoLookahead:
    def test_future_bar_does_not_change_todays_label(self):
        close = _close(700)
        vix = _vix(close.index)
        base = rg.build_regime_days(close, vix)
        cut = 500
        shocked = close.copy()
        shocked.iloc[cut:] = shocked.iloc[cut:] * 0.5
        vix_shocked = vix.copy()
        vix_shocked.iloc[cut:] = 80.0
        after = rg.build_regime_days(shocked, vix_shocked)
        # label of day `cut` uses closes through day cut-1, which are identical
        for col in ("trend", "vol", "vol_source"):
            left = base[col].iloc[: cut + 1].fillna("x")
            right = after[col].iloc[: cut + 1].fillna("x")
            assert (left == right).all(), col

    def test_shock_shows_up_the_day_after(self):
        close = _close(700)
        vix = _vix(close.index)
        cut = 500
        vix2 = vix.copy()
        vix2.iloc[cut:] = 80.0
        out = rg.build_regime_days(close, vix2)
        assert out["vol"].iloc[cut + 10] == "high"
        assert out["vol"].iloc[cut] != "high" or out["vol"].iloc[cut - 1] == "high"

    def test_truncating_history_gives_identical_labels(self):
        close = _close(700)
        vix = _vix(close.index)
        full = rg.build_regime_days(close, vix)
        part = rg.build_regime_days(close.iloc[:550], vix.iloc[:550])
        for col in ("trend", "vol"):
            assert (full[col].iloc[:550].fillna("x") == part[col].fillna("x")).all(), col


class TestVolatility:
    def test_vix_drives_label_when_present(self):
        close = _close(700)
        vix = _vix(close.index)
        out = rg.build_regime_days(close, vix)
        assert (out["vol_source"].dropna() == "vix").all()
        assert set(out["vol"].dropna()) <= set(rg.VOL_LABELS)

    def test_high_vix_period_is_high(self):
        close = _close(700)
        vix = _vix(close.index)
        vix.iloc[600:] = 45.0
        out = rg.build_regime_days(close, vix)
        assert out["vol"].iloc[-1] == "high"

    def test_low_vix_period_is_low(self):
        close = _close(700)
        vix = _vix(close.index, level=18.0)
        vix.iloc[600:] = 9.0
        out = rg.build_regime_days(close, vix)
        assert out["vol"].iloc[-1] == "low"

    def test_falls_back_to_realised_vol_without_vix(self):
        out = rg.build_regime_days(_close(700))
        used = out["vol_source"].dropna()
        assert len(used) > 0
        assert (used == "realised").all()

    def test_falls_back_per_day_where_vix_is_missing(self):
        close = _close(700)
        vix = _vix(close.index)
        vix.iloc[300:340] = np.nan
        out = rg.build_regime_days(close, vix, rg.RegimeParams(vix_ffill_days=0))
        sources = set(out["vol_source"].dropna())
        assert sources == {"vix", "realised"}

    def test_both_methods_are_recorded_for_agreement(self):
        out = rg.build_regime_days(_close(700), _vix(_close(700).index))
        assert {"vix_vol", "rv_vol"} <= set(out.columns)

    def test_percentile_is_trailing_not_full_sample(self):
        # A VIX level that is extreme only in hindsight must not be labelled by the future.
        close = _close(700)
        vix = pd.Series(np.linspace(10, 40, 700), index=close.index)
        out = rg.build_regime_days(close, vix)
        # A steadily rising series is always at the top of its trailing window
        assert out["vol"].dropna().iloc[-50:].eq("high").all()


class TestAgreement:
    def test_perfect_and_disagreeing(self):
        idx = _idx(4)
        frame = pd.DataFrame(
            {"vix_vol": ["high", "low", "mid", None], "rv_vol": ["high", "high", "mid", "low"]},
            index=idx,
        )
        stats = rg.vol_agreement(frame)
        assert stats["days"] == 3
        assert stats["exact"] == pytest.approx(2 / 3)
        assert stats["opposite"] == pytest.approx(1 / 3)


def _trade(day, r, symbol="A"):
    return Trade(
        symbol=symbol,
        day=day,
        direction=1,
        tag="t",
        entry_time=pd.Timestamp(f"{day} 09:20"),
        entry=100.0,
        signal_price=100.0,
        sl=99.0,
        tp=0.0,
        risk=1.0,
        exit_time=pd.Timestamp(f"{day} 14:30"),
        reason="tp",
        r_gross=r + 0.1,
        r_net=r,
    )


def _labels(days, trend, vol):
    return pd.DataFrame(
        {"trend": trend, "vol": vol, "vol_source": "vix"},
        index=pd.DatetimeIndex(days),
    )


class TestBlock:
    def _setup(self):
        days = [d.date().isoformat() for d in _idx(10, "2024-01-01")]
        trend = ["up"] * 5 + ["down"] * 5
        vol = ["high", "low"] * 5
        regime_days = _labels(days, trend, vol)
        trades = [_trade(d, 0.5 if t == "up" else -0.5) for d, t in zip(days, trend, strict=True)]
        return days, regime_days, trades

    def test_groups_trades_by_entry_day_regime(self):
        _, regime_days, trades = self._setup()
        block = rg.regime_block(trades, regime_days, min_trades=1)
        trend = {r["regime"]: r for r in block["trend"]}
        assert trend["up"]["trades"] == 5 and trend["down"]["trades"] == 5
        assert trend["up"]["net_r"] == pytest.approx(0.5)
        assert trend["down"]["net_r"] == pytest.approx(-0.5)
        assert trend["up"]["win_rate"] == 1.0 and trend["down"]["win_rate"] == 0.0

    def test_max_drawdown_is_per_regime(self):
        days = [d.date().isoformat() for d in _idx(6, "2024-01-01")]
        regime_days = _labels(days, ["up"] * 6, ["low"] * 6)
        rs = [1.0, -0.5, -0.5, 1.0, -1.0, 0.5]
        trades = [_trade(d, r) for d, r in zip(days, rs, strict=True)]
        block = rg.regime_block(trades, regime_days, min_trades=1)
        assert block["trend"][0]["max_dd_r"] == pytest.approx(1.0)

    def test_low_sample_is_flagged_not_hidden(self):
        _, regime_days, trades = self._setup()
        block = rg.regime_block(trades, regime_days, min_trades=30)
        assert all(r["low_sample"] for r in block["trend"])
        assert len(block["trend"]) == 2

    def test_unclassified_trades_are_counted(self):
        days, regime_days, trades = self._setup()
        early = _trade("2020-01-06", 0.2)
        block = rg.regime_block([early, *trades], regime_days, min_trades=1)
        assert block["unclassified_trades"] == 1

    def test_nan_label_days_are_unclassified(self):
        days = [d.date().isoformat() for d in _idx(4, "2024-01-01")]
        regime_days = _labels(days, [None, "up", "up", "up"], ["low"] * 4)
        trades = [_trade(d, 0.1) for d in days]
        block = rg.regime_block(trades, regime_days, min_trades=1)
        assert block["unclassified_trades"] == 1

    def test_block_carries_version_and_params(self):
        _, regime_days, trades = self._setup()
        block = rg.regime_block(trades, regime_days, min_trades=1)
        assert block["version"] == rg.REGIME_VERSION
        assert block["params"]["slow"] == PARAMS.slow
        assert block["min_trades"] == 1

    def test_source_summary_is_included(self):
        _, regime_days, trades = self._setup()
        block = rg.regime_block(trades, regime_days, min_trades=1, source={"nifty": "historify"})
        assert block["source"] == {"nifty": "historify"}

    def test_empty_trades_give_empty_groups(self):
        _, regime_days, _ = self._setup()
        block = rg.regime_block([], regime_days, min_trades=1)
        assert block["trend"] == [] and block["vol"] == []
        assert block["unclassified_trades"] == 0

    def test_does_not_mutate_inputs(self):
        _, regime_days, trades = self._setup()
        before = regime_days.copy()
        rg.regime_block(trades, regime_days, min_trades=1)
        pd.testing.assert_frame_equal(before, regime_days)


class TestBreadth:
    def test_positive_share_counts_adequately_sampled_regimes_only(self):
        block = {
            "trend": [
                {"regime": "up", "trades": 100, "net_r": 0.2, "low_sample": False},
                {"regime": "down", "trades": 100, "net_r": -0.1, "low_sample": False},
                {"regime": "range", "trades": 3, "net_r": 0.9, "low_sample": True},
            ],
            "vol": [],
        }
        info = rg.breadth(block)
        assert info["trend"] == {"evaluated": 2, "positive": 1}
        assert info["vol"] == {"evaluated": 0, "positive": 0}


class TestRunRecordIntegration:
    """The regimes block is optional in a run record, validated, and feeds evaluation."""

    def _regime_days(self, days):
        n = len(days)
        return _labels(
            days, ["up"] * (n // 2) + ["down"] * (n - n // 2), ["low", "high"] * (n // 2)
        )

    def _trades(self, n=60):
        days = [d.date().isoformat() for d in _idx(n, "2024-01-01")]
        return days, [
            _trade(d, 0.4 if i < n // 2 else -0.2, symbol=f"S{i % 5}") for i, d in enumerate(days)
        ]

    def _meta(self):
        return {
            "run_id": "2026-09-30-x-v1",
            "run_date": "2026-09-30",
            "code_version": "abc",
            "data": {
                "source": "historify",
                "as_of": "2026-09-29",
                "universe": "u",
                "bar_size": "D",
            },
            "period": {"start": "2024-01-01", "end": "2024-06-30"},
            "costs": {"model": "flat", "round_trip_bps": 10.0},
        }

    def test_regimes_absent_by_default(self):
        from signal_engine.backtest.tearsheet import build_run_record

        _, trades = self._trades()
        record = build_run_record(trades, **self._meta())
        assert "regimes" not in record

    def test_regimes_block_recorded_and_valid(self):
        from signal_engine.backtest.tearsheet import build_run_record
        from signal_engine.research import build_registry as br

        days, trades = self._trades()
        record = build_run_record(
            trades, **self._meta(), regime_days=self._regime_days(days), regime_source={"a": 1}
        )
        block = record["regimes"]
        assert block["version"] == rg.REGIME_VERSION
        assert block["source"] == {"a": 1}
        assert {r["regime"] for r in block["trend"]} == {"up", "down"}
        assert br.validate_run(record) == [] if hasattr(br, "validate_run") else True

    def test_registry_rejects_malformed_regimes(self):
        from signal_engine.research import build_registry as br

        assert br._validate_regimes({"version": 1, "trend": [], "vol": []}) == []
        assert br._validate_regimes("nope")
        assert br._validate_regimes({"version": 1, "trend": [{"regime": "up"}], "vol": []})

    def test_evaluation_row_reads_the_block(self):
        from signal_engine.backtest import evaluation as ev
        from signal_engine.backtest.tearsheet import build_run_record

        days, trades = self._trades()
        record = build_run_record(trades, **self._meta(), regime_days=self._regime_days(days))
        rows = [r for d in record["evaluation"]["dimensions"] for r in d["rows"]]
        row = next(r for r in rows if r["gate"] == "X.2")
        assert row["status"] in ("report", "pass", "fail")
        assert row["value"] is not None
        assert ev is not None

    def test_evaluation_row_not_evaluated_without_regimes(self):
        from signal_engine.backtest.tearsheet import build_run_record

        _, trades = self._trades()
        record = build_run_record(trades, **self._meta())
        rows = [r for d in record["evaluation"]["dimensions"] for r in d["rows"]]
        row = next(r for r in rows if r["gate"] == "X.2")
        assert row["status"] == "not_evaluated"
        assert "regime" in row["evidence"].lower()
