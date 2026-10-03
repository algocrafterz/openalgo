"""Per-date eligibility and the universe frame."""

from __future__ import annotations

import time
from datetime import date

import numpy as np
import pandas as pd
import pytest

from signal_engine.backtest import eligibility as el
from signal_engine.backtest import fingerprints as fp
from signal_engine.backtest.session import Calendar
from signal_engine.research.intake import execution, profiles
from signal_engine.tests.intake_fixtures import make_spec

IST = "Asia/Kolkata"
DAYS = [d.date() for d in pd.bdate_range("2026-03-02", "2026-03-13")]  # 10 days
CAL = Calendar(tuple(DAYS), (), "derived_from_market_data", "historify-derived-test0000")


def _frame(days, close=100.0, volume=10.0, per_day=2):
    parts = []
    for d in days:
        idx = pd.date_range(f"{d} 09:15", periods=per_day, freq="1min", tz=IST)
        parts.append(pd.DataFrame({"Close": close, "Volume": volume}, index=idx))
    return pd.concat(parts)


POLICY = el.EligibilityPolicy(min_prior_sessions=3, min_volume=1.0, min_price=10.0)


def test_constants_and_policy_frozen():
    assert el.MEMBERSHIP_SOURCE == "current_symbol_master"
    assert el.SURVIVORSHIP_BIAS is True
    with pytest.raises(AttributeError):
        POLICY.min_price = 1


class TestReasons:
    def test_listing_age_boundary(self):
        # listed mid-panel: first seen at calendar position 6, beyond the 5-session tolerance
        res = el.compute_eligibility({"A": _frame(DAYS[6:]), "B": _frame(DAYS)}, POLICY, CAL)
        bad = res.ineligible["A"]
        assert set(bad) == set(DAYS[6:9])
        assert set(bad.values()) == {"LISTING_AGE"}
        # exactly min_prior_sessions earlier days (DAYS[9]) is eligible
        assert DAYS[9] not in bad

    def test_present_from_panel_start_is_exempt_from_listing_age(self):
        res = el.compute_eligibility({"A": _frame(DAYS)}, POLICY, CAL)
        assert "A" not in res.ineligible

    def test_first_seen_exactly_at_tolerance_is_exempt(self):
        assert el.LISTING_TOLERANCE_SESSIONS == 5
        res = el.compute_eligibility({"A": _frame(DAYS[5:]), "B": _frame(DAYS)}, POLICY, CAL)
        assert "A" not in res.ineligible

    def test_first_seen_one_past_tolerance_is_subject_to_listing_age(self):
        res = el.compute_eligibility({"A": _frame(DAYS[6:]), "B": _frame(DAYS)}, POLICY, CAL)
        assert set(res.ineligible["A"]) == set(DAYS[6:9])

    def test_price_floor_skipped_for_adjusted_prices(self):
        f = _frame(DAYS, close=5.0)
        res = el.compute_eligibility({"A": f}, POLICY, CAL, adjusted_prices=True)
        assert "A" not in res.ineligible
        raw = el.compute_eligibility({"A": f}, POLICY, CAL)
        assert set(raw.ineligible["A"].values()) == {"PRICE_FLOOR"}

    def test_no_trade(self):
        days = [d for d in DAYS if d != DAYS[6]]
        res = el.compute_eligibility({"A": _frame(days)}, POLICY, CAL)
        assert res.ineligible["A"][DAYS[6]] == "NO_TRADE"

    def test_zero_volume(self):
        f = _frame(DAYS)
        f.loc[f.index.date == DAYS[5], "Volume"] = 0.0
        res = el.compute_eligibility({"A": f}, POLICY, CAL)
        assert res.ineligible["A"][DAYS[5]] == "ZERO_VOLUME"

    def test_volume_exactly_min_is_not_above(self):
        f = _frame(DAYS, per_day=1, volume=1.0)
        res = el.compute_eligibility({"A": f}, POLICY, CAL)
        assert res.ineligible["A"][DAYS[5]] == "ZERO_VOLUME"

    def test_price_floor_boundary(self):
        f = _frame(DAYS)
        f.loc[f.index.date == DAYS[4], "Close"] = 9.99
        f.loc[f.index.date == DAYS[5], "Close"] = 10.0
        res = el.compute_eligibility({"A": f}, POLICY, CAL)
        assert res.ineligible["A"][DAYS[4]] == "PRICE_FLOOR"
        assert DAYS[5] not in res.ineligible["A"]

    def test_session_close_is_last_bar(self):
        f = _frame(DAYS)
        day = f.index.date == DAYS[5]
        f.loc[f.index[day][-1], "Close"] = 5.0  # last bar low, earlier bar fine
        res = el.compute_eligibility({"A": f}, POLICY, CAL)
        assert res.ineligible["A"][DAYS[5]] == "PRICE_FLOOR"

    def test_dates_outside_calendar_ignored(self):
        f = _frame([date(2026, 3, 14)] + DAYS)  # a Saturday, not a trading day
        res = el.compute_eligibility({"A": f}, POLICY, CAL)
        assert date(2026, 3, 14) not in res.ineligible.get("A", {})


class TestUniverseFrame:
    def _result(self):
        frames = {"B": _frame(DAYS[2:]), "A": _frame(DAYS, volume=5.0)}
        return el.compute_eligibility(frames, POLICY, CAL)

    def test_schema_and_order(self):
        u = self._result().universe_frame
        assert list(u.columns) == [
            "symbol",
            "first_date",
            "last_date",
            "sessions",
            "eligible_sessions",
            "ineligible_sessions",
            "median_daily_volume",
        ]
        assert list(u["symbol"]) == ["A", "B"]
        assert u["first_date"].tolist() == ["2026-03-02", "2026-03-04"]
        assert u["last_date"].tolist() == ["2026-03-13", "2026-03-13"]
        assert u["sessions"].tolist() == [10, 8]
        assert u["eligible_sessions"].tolist() == [10, 8]
        assert u["ineligible_sessions"].tolist() == [0, 0]
        assert u["median_daily_volume"].tolist() == [10.0, 20.0]
        assert str(u["sessions"].dtype) == "int64"
        assert str(u["median_daily_volume"].dtype) == "float64"
        assert u.index.tolist() == [0, 1]

    def test_ineligible_count_includes_calendar_days_without_trade(self):
        # B did not trade DAYS[0:2]; those pre-listing days are not counted against it
        u = self._result().universe_frame
        assert u.loc[1, "sessions"] + 2 == len(DAYS)

    def test_deterministic(self):
        a, b = self._result(), self._result()
        pd.testing.assert_frame_equal(a.universe_frame, b.universe_frame)
        assert a.ineligible == b.ineligible

    def test_parquet_friendly(self):
        import io

        buf = io.BytesIO()
        self._result().universe_frame.to_parquet(buf)
        assert buf.getvalue()

    def test_empty_frames(self):
        res = el.compute_eligibility({}, POLICY, CAL)
        assert len(res.universe_frame) == 0 and res.ineligible == {}
        assert "symbol" in res.universe_frame.columns


def test_performance_one_symbol_190k_bars():
    days = [d.date() for d in pd.bdate_range("2024-01-01", "2026-03-31")][:506]
    idx = np.concatenate(
        [
            (
                pd.Timestamp(f"{d} 09:15", tz=IST) + pd.to_timedelta(np.arange(375), unit="min")
            ).values
            for d in days
        ]
    )
    f = pd.DataFrame(
        {"Close": 100.0, "Volume": 10.0}, index=pd.DatetimeIndex(idx, tz="UTC").tz_convert(IST)
    )
    cal = Calendar(tuple(days), (), "derived_from_market_data", "v")
    assert len(f) > 189_000
    t = time.perf_counter()
    el.compute_eligibility({"A": f}, el.EligibilityPolicy(60, 1.0, 10.0), cal)
    assert time.perf_counter() - t < 1.0


class TestExecutionFingerprint:
    def _resolved(self, **bump):
        r = execution.resolve_execution(make_spec(), profiles.load_profile("flattrade-nse-cash"))
        out = {v.field: {"value": v.value, "ledger_id": "A"} for v in r.values}
        for k, v in bump.items():
            out["execution." + k]["value"] = v
        return out

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("eligibility_min_prior_sessions", 61),
            ("eligibility_min_volume", 2),
            ("eligibility_min_price", 11.0),
        ],
    )
    def test_each_field_moves_execution_only(self, field, value):
        kw = {
            "intent": {"name": "x", "parameters": {}},
            "cost_model": None,
            "symbols": ["A"],
            "universe_definition": "u",
            "profile_content": {"n": 1},
            "dataset_fingerprint": "d" * 64,
        }
        base = fp.component_fingerprints(resolved=self._resolved(), **kw)
        new = fp.component_fingerprints(resolved=self._resolved(**{field: value}), **kw)
        assert {k for k in base if base[k] != new[k]} == {"execution"}
