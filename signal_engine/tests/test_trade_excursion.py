"""MAE/MFE per recorded trade (read-only diagnostic on a trade list and 5-minute bars)."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import trade_excursion as te

T0 = pd.Timestamp("2024-01-01 04:00", tz="UTC")


def _bars(rows, start=T0):
    """rows of (open, high, low, close); consecutive 5-minute bars."""
    idx = pd.date_range(start, periods=len(rows), freq="5min")
    return pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close"])


def _trade(side, entry, exit_price, reason, bar_from, bar_to, risk=2.0, symbol="AAA", gross_r=0.0):
    return {
        "trade_id": f"{symbol}-{bar_from}-{bar_to}-{reason}",
        "symbol": symbol,
        "side": side,
        "entry_time": T0 + pd.Timedelta(minutes=5 * bar_from),
        "exit_time": T0 + pd.Timedelta(minutes=5 * bar_to),
        "entry_price": entry,
        "exit_price": exit_price,
        "exit_reason": reason,
        "gross_r": gross_r,
        "net_r": gross_r - 0.2,
        "risk": risk,
    }


def _one(trade, rows, symbol="AAA"):
    return te.measure(pd.DataFrame([trade]), {symbol: _bars(rows)}).iloc[0]


class TestLongTrades:
    def test_target_exit_known_and_full_readings(self):
        r = _one(
            _trade("BUY", 100.0, 102.0, "TP", 0, 1),
            [(100.0, 101.0, 99.5, 100.5), (100.5, 103.0, 100.2, 102.5)],
        )
        assert r.status == "ok"
        assert r.mfe_known == pytest.approx(2.0) and r.mfe_full == pytest.approx(2.0)
        assert r.mae_known == pytest.approx(0.5) and r.mae_full == pytest.approx(0.5)
        assert r.mfe_known_r == pytest.approx(1.0) and r.mae_known_r == pytest.approx(0.25)
        assert r.mae_known_bps == pytest.approx(50.0)

    def test_target_exit_bar_low_counts_only_in_full_bar(self):
        r = _one(
            _trade("BUY", 100.0, 102.0, "TP", 0, 1),
            [(100.0, 100.4, 99.9, 100.2), (100.1, 103.0, 98.5, 102.5)],
        )
        assert r.mae_known == pytest.approx(0.1)  # entry bar low; the exit bar opens above E
        assert r.mae_full == pytest.approx(1.5)  # the exit bar low, an upper bound

    def test_stop_exit_credits_no_favourable_move_after_the_open(self):
        r = _one(
            _trade("BUY", 100.0, 98.0, "SL", 0, 1),
            [(100.0, 101.0, 99.5, 100.5), (99.8, 101.5, 97.5, 98.2)],
        )
        assert r.mae_known == pytest.approx(2.0) and r.mae_full == pytest.approx(2.0)
        assert r.mfe_known == pytest.approx(1.0)
        assert r.mfe_full == pytest.approx(1.5)

    def test_gap_stop_uses_the_open_only(self):
        r = _one(
            _trade("BUY", 100.0, 97.0, "SL_GAP", 0, 1),
            [(100.0, 100.6, 99.8, 100.1), (97.0, 98.0, 96.0, 96.5)],
        )
        assert r.mae_known == pytest.approx(3.0)
        assert r.mfe_known == pytest.approx(0.6) and r.mfe_full == pytest.approx(0.6)

    def test_time_exit_counts_the_whole_exit_bar_under_both_readings(self):
        r = _one(_trade("BUY", 100.0, 101.0, "TIME_EXIT", 0, 0), [(100.0, 102.0, 99.0, 101.0)])
        assert (r.mfe_known, r.mfe_full) == (pytest.approx(2.0), pytest.approx(2.0))
        assert (r.mae_known, r.mae_full) == (pytest.approx(1.0), pytest.approx(1.0))
        assert r.exit_at_close == 1.0

    def test_excursions_are_never_negative(self):
        r = _one(_trade("BUY", 100.0, 100.5, "TIME_EXIT", 0, 0), [(100.0, 100.9, 100.0, 100.5)])
        assert r.mae_known == 0.0 and r.mfe_known == pytest.approx(0.9)


class TestShortTrades:
    def test_stop_exit_mirrors_the_long_case(self):
        r = _one(
            _trade("SELL", 100.0, 102.0, "SL", 0, 1),
            [(100.0, 100.5, 98.0, 99.0), (100.2, 103.0, 99.5, 102.5)],
        )
        assert r.mae_known == pytest.approx(2.0)
        assert r.mfe_known == pytest.approx(2.0)  # entry bar low, 2.0 below E
        assert r.mfe_full == pytest.approx(2.0)
        assert r.mfe_known_r == pytest.approx(1.0)

    def test_target_exit_known_path_ignores_exit_bar_high_after_open(self):
        r = _one(
            _trade("SELL", 100.0, 98.0, "TP", 0, 1),
            [(100.0, 100.3, 99.5, 99.8), (99.7, 101.0, 97.5, 98.2)],
        )
        assert r.mae_known == pytest.approx(0.3)
        assert r.mae_full == pytest.approx(1.0)
        assert r.mfe_known == pytest.approx(2.0)


class TestPathProblems:
    def test_symbol_without_bars(self):
        t = _trade("BUY", 100, 101, "TP", 0, 0, symbol="ZZZ")
        assert _one(t, [(100, 101, 99, 100)], symbol="AAA").status == "no_bars"

    def test_entry_bar_missing(self):
        t = _trade("BUY", 100, 101, "TP", 5, 6)
        assert _one(t, [(100, 101, 99, 100)] * 3).status == "entry_bar_missing"

    def test_exit_bar_missing(self):
        t = _trade("BUY", 100, 101, "TP", 0, 9)
        assert _one(t, [(100, 101, 99, 100)] * 3).status == "exit_bar_missing"

    def test_gap_inside_the_path(self):
        bars = _bars([(100, 101, 99, 100)] * 4).drop(T0 + pd.Timedelta(minutes=10))
        out = te.measure(pd.DataFrame([_trade("BUY", 100, 101, "TP", 0, 3)]), {"AAA": bars}).iloc[0]
        assert out.status == "path_gap" and np.isnan(out.mae_known)

    def test_exit_before_entry(self):
        t = _trade("BUY", 100, 101, "TP", 2, 1)
        assert _one(t, [(100, 101, 99, 100)] * 3).status == "exit_bar_missing"

    @pytest.mark.parametrize("risk", [0.0, -1.0, float("nan")])
    def test_invalid_risk_leaves_r_undefined_but_keeps_price_and_bps(self, risk):
        t = _trade("BUY", 100.0, 102.0, "TP", 0, 1, risk=risk)
        r = _one(t, [(100.0, 101.0, 99.5, 100.5), (100.5, 103.0, 100.2, 102.5)])
        assert r.status == "ok" and not r.risk_defined
        assert np.isnan(r.mae_known_r) and np.isnan(r.mfe_known_r)
        assert r.mfe_known == pytest.approx(2.0) and r.mfe_known_bps == pytest.approx(200.0)

    def test_empty_and_missing_columns_raise(self):
        with pytest.raises(te.ExcursionError):
            te.measure(pd.DataFrame(columns=te.REQUIRED), {})
        with pytest.raises(te.ExcursionError):
            te.measure(pd.DataFrame([{"symbol": "A"}]), {})


class TestSummary:
    def _many(self, n=40):
        rng = np.random.default_rng(1)
        rows = []
        for _ in range(n):
            rows += [(100.0, 100.0 + rng.random(), 99.0 - rng.random(), 100.0)] * 2
        trades = [
            _trade(
                "BUY" if k % 2 else "SELL", 100.0, 100.0, "TIME_EXIT", 2 * k, 2 * k + 1,
                gross_r=float(rng.normal()),
            )
            for k in range(n)
        ]
        return pd.DataFrame(trades), _bars(rows)

    def test_distributions_by_side_and_year(self):
        trades, bars = self._many()
        s = te.summarise(te.measure(trades, {"AAA": bars}))
        assert s["trades_with_defined_r"] == 40
        assert set(s["by_side"]) == {"BUY", "SELL"}
        assert s["by_year"]["2024"]["trades"] == 40
        assert "p50" in s["all"]["mae_known_r"]

    def test_small_groups_are_withheld(self):
        trades, bars = self._many(10)
        s = te.summarise(te.measure(trades, {"AAA": bars}))
        assert "withheld" in s["all"]["mae_known_r"] and "p50" not in s["all"]["mae_known_r"]

    def test_undefined_risk_trades_are_counted_not_averaged(self):
        trades, bars = self._many()
        trades.loc[0, "risk"] = 0.0
        s = te.summarise(te.measure(trades, {"AAA": bars}))
        assert s["trades_with_undefined_risk"] == 1 and s["trades_with_defined_r"] == 39

    def test_buckets_partition_the_trades(self):
        trades, bars = self._many()
        s = te.summarise(te.measure(trades, {"AAA": bars}))
        assert sum(b["trades"] for b in s["mae_buckets_known_r"]) == 40
        assert sum(b["trades"] for b in s["mfe_buckets_known_r"]) == 40

    def test_consistency_flags_a_target_trade_that_reached_the_stop(self):
        trades, bars = self._many()
        trades["exit_reason"] = "TP"
        trades["stop_r"] = 0.1  # every path ran at least this far against the trade
        s = te.summarise(te.measure(trades, {"AAA": bars}))
        assert s["consistency"]["target_trades_whose_known_path_reached_the_stop"] > 0

    def test_no_usable_trades_gives_counts_only(self):
        s = te.summarise(_one_frame_without_bars())
        assert s["trades_with_complete_path"] == 0 and "all" not in s


def _one_frame_without_bars():
    t = _trade("BUY", 100, 101, "TP", 0, 0, symbol="ZZZ")
    return te.measure(pd.DataFrame([t]), {})


class TestEvidence:
    BARS = [(100.0, 101.0, 99.5, 100.5), (100.5, 103.0, 100.2, 102.5)]

    def _run(self, tmp_path):
        run = tmp_path / "run"
        (run / "evidence").mkdir(parents=True)
        trades = pd.DataFrame([_trade("BUY", 100.0, 102.0, "TP", 0, 1, risk=2.0, gross_r=1.0)])
        trades["signal_id"] = "S1"
        trades = trades.drop(columns=["risk"])  # recorded trades carry risk only via signals
        trades.to_parquet(run / "evidence" / "trades.parquet")
        pd.DataFrame(
            [{"signal_id": "S1", "stop": 98.0, "target": 102.0, "risk": 2.0}]
        ).to_parquet(run / "evidence" / "signals.parquet")
        return run

    def test_join_gives_stop_and_target_distance_in_r(self, tmp_path):
        m = te.trades_with_risk(self._run(tmp_path))
        assert m["stop_r"].iloc[0] == pytest.approx(1.0)
        assert m["target_r"].iloc[0] == pytest.approx(1.0)

    def test_sealed_payload_is_valid_deterministic_and_read_only(self, tmp_path):
        run = self._run(tmp_path)
        bars = {"AAA": _bars(self.BARS)}
        before = sorted(p.name for p in run.rglob("*"))
        ev1, _ = te.evidence_for_run(run, bars)
        ev2, _ = te.evidence_for_run(run, bars)
        assert ev1 == ev2 and te.validate_evidence(ev1) == []
        assert ev1["report_only"] and ev1["diagnostic_not_trials"]
        json.dumps(ev1, allow_nan=False)
        assert sorted(p.name for p in run.rglob("*")) == before

    def test_tampering_breaks_the_seal(self, tmp_path):
        ev, _ = te.evidence_for_run(self._run(tmp_path), {"AAA": _bars(self.BARS)})
        ev["summary"]["trades_total"] = 99
        assert any("content_sha256" in p for p in te.validate_evidence(ev))
