"""Engine P1 step 5c: the data and session layer carried into the recorded run.

Collector wiring, eligibility drop, warm-up, manifest fields, data_quality.json, universe.parquet,
reproduction of the calendar, and the six evaluators (G0.3, G0.4, G0.6, G0.8, G4.7, G6.2).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import re

import duckdb
import pandas as pd
import pytest

from signal_engine.backtest import dq_run, trades_io
from signal_engine.backtest import evaluation as ev
from signal_engine.backtest import ledgers as lg
from signal_engine.backtest import manifest as mf
from signal_engine.backtest import record_run as rr
from signal_engine.backtest import reproduce as rp
from signal_engine.backtest import run_artifact as ra
from signal_engine.backtest.data_collect import DataCollector
from signal_engine.backtest.eligibility import (
    MEMBERSHIP_SOURCE,
    EligibilityPolicy,
    apply_eligibility,
    compute_eligibility,
)
from signal_engine.backtest.engine import simulate
from signal_engine.backtest.fingerprints import canonical_json
from signal_engine.backtest.session import Calendar, derive_calendar
from signal_engine.backtest.swing_engine import SwingRunConfig, simulate_swing
from signal_engine.backtest.types import Ctx, RunConfig
from signal_engine.tests import test_evidence_ledgers as tel
from signal_engine.tests import test_record_run as base
from signal_engine.tests import test_run_artifact_write as raw
from signal_engine.tests.test_backtest_ledgers import (
    FLAT,
    RUN,
    Script,
    SwingScript,
    codes,
    make_frame,
    strip,
)
from signal_engine.tests.test_record_run import SUPPORTED

root = base.root
_stub_provenance = base._stub_provenance

IST = "Asia/Kolkata"
POLICY = EligibilityPolicy(min_prior_sessions=2, min_volume=1.0, min_price=10.0)


def day_str(k: int, start: str = "2026-06-01") -> str:
    return str((pd.Timestamp(start) + pd.Timedelta(days=k)).date())


def full_sessions(n_days: int, bars: int = 75):
    return [(day_str(k), [FLAT] * bars) for k in range(n_days)]


# ---- apply_eligibility ---------------------------------------------------------------------


class TestApplyEligibility:
    """Eight symbols present from the panel start anchor the calendar; AAA and BBB list on
    session 6, beyond the listing tolerance, so their first sessions are too young."""

    def _frames(self):
        frames = {f"A{k}": make_frame(full_sessions(12, bars=4)) for k in range(8)}
        late = full_sessions(12, bars=4)[6:]
        frames["AAA"] = make_frame(late)
        frames["BBB"] = make_frame(late)
        return frames

    def test_listing_age_sessions_are_dropped_and_reported(self):
        frames = self._frames()
        cal = derive_calendar(frames)
        result = compute_eligibility(frames, POLICY, cal)
        new, dropped = apply_eligibility(frames, result)
        first_two = {dt.date.fromisoformat(day_str(6)), dt.date.fromisoformat(day_str(7))}
        assert sorted(dropped) == ["AAA", "BBB"]  # anchors are exempt
        for sym in ("AAA", "BBB"):
            assert dropped[sym] == dict.fromkeys(sorted(first_two), "LISTING_AGE")
            assert not first_two & set(new[sym].index.date)
            assert len(new[sym]) == 4 * 4
        assert len(new["A0"]) == len(frames["A0"])

    def test_input_frames_are_not_mutated(self):
        frames = self._frames()
        before = {k: v.copy() for k, v in frames.items()}
        cal = derive_calendar(frames)
        apply_eligibility(frames, compute_eligibility(frames, POLICY, cal))
        for k in frames:
            pd.testing.assert_frame_equal(frames[k], before[k])

    def test_fully_eligible_symbol_is_returned_unchanged(self):
        frames = self._frames()
        cal = derive_calendar(frames)
        policy = EligibilityPolicy(0, 1.0, 10.0)
        new, dropped = apply_eligibility(frames, compute_eligibility(frames, policy, cal))
        assert dropped == {}
        for k in frames:
            pd.testing.assert_frame_equal(new[k], frames[k])

    def test_symbol_with_no_eligible_session_is_omitted_but_still_reported(self):
        frames = self._frames()
        penny = make_frame(full_sessions(12, bars=4))
        penny[["Open", "High", "Low", "Close"]] = 5.0
        frames["PENNY"] = penny
        cal = derive_calendar(frames)
        new, dropped = apply_eligibility(frames, compute_eligibility(frames, POLICY, cal))
        assert "PENNY" not in new
        assert set(dropped["PENNY"].values()) == {"PRICE_FLOOR"}
        assert len(dropped["PENNY"]) == 12

    def test_only_dates_with_rows_are_reported(self):
        frames = self._frames()
        gap = make_frame([s for k, s in enumerate(full_sessions(12, bars=4)) if k != 9])
        gap["Volume"] = 0.0
        frames["GAP"] = gap
        cal = derive_calendar(frames)
        _, dropped = apply_eligibility(frames, compute_eligibility(frames, POLICY, cal))
        assert dt.date.fromisoformat(day_str(9)) not in dropped["GAP"]
        assert len(dropped["GAP"]) == 11


# ---- warm-up --------------------------------------------------------------------------------


def _plan(n, every=1):
    return dict.fromkeys(range(0, n, every), (1, 90.0, 110.0))


class TestWarmupIntraday:
    def _df(self):
        return make_frame([(day_str(0), [FLAT] * 12), (day_str(1), [FLAT] * 12)])

    def test_default_is_zero(self):
        assert RunConfig().warmup_bars == 0
        assert SwingRunConfig().warmup_bars == 0

    def test_zero_leaves_trades_identical_and_records_no_warmup(self):
        df = self._df()
        rec = lg.LedgerRecorder()
        with_zero = simulate(
            Ctx(df, "AAA"), Script(_plan(24)), None, RUN.with_(warmup_bars=0), rec=rec
        )
        plain = simulate(Ctx(df, "AAA"), Script(_plan(24)), None, RUN)
        assert strip(with_zero) == strip(plain)
        assert "WARMUP" not in codes(rec.to_frames())

    def test_bars_before_warmup_cannot_signal_and_are_counted_per_day(self):
        df = self._df()
        rec = lg.LedgerRecorder()
        run = RUN.with_(warmup_bars=14)
        trades = simulate(Ctx(df, "AAA"), Script(_plan(24)), None, run, rec=rec)
        frames = rec.to_frames()
        assert codes(frames)["WARMUP"] == 14
        rows = frames["exclusions"]
        rows = rows[rows["reason_code"] == "WARMUP"]
        assert sorted(rows["count"].tolist()) == [2, 12]  # one aggregate row per day
        first_signal = frames["signals"]["bar_time"].min()
        assert first_signal >= df.index[14]
        assert all(t.entry_time > df.index[14] for t in trades)
        assert rec.reconcile(frames, trades) == []

    def test_warmup_without_recorder_blocks_the_same_signals(self):
        df = self._df()
        run = RUN.with_(warmup_bars=14)
        plain = simulate(Ctx(df, "AAA"), Script(_plan(24)), None, run)
        rec = lg.LedgerRecorder()
        recorded = simulate(Ctx(df, "AAA"), Script(_plan(24)), None, run, rec=rec)
        assert strip(plain) == strip(recorded)
        assert all(t.entry_time > df.index[14] for t in plain)


class TestWarmupSwing:
    def _df(self):
        return make_frame([(day_str(k), [FLAT]) for k in range(8)])

    def test_zero_is_identical(self):
        df = self._df()
        plan = {1: (1, 99.0, 110.0), 5: (1, 99.0, 110.0)}
        a = simulate_swing(Ctx(df, "AAA"), SwingScript(plan), None, SwingRunConfig())
        b = simulate_swing(Ctx(df, "AAA"), SwingScript(plan), None, SwingRunConfig(warmup_bars=0))
        assert strip(a) == strip(b) and len(a) >= 1

    def test_warmup_blocks_early_signal_and_counts_it(self):
        df = self._df()
        plan = {1: (1, 99.0, 110.0), 5: (1, 99.0, 110.0)}
        rec = lg.LedgerRecorder()
        trades = simulate_swing(
            Ctx(df, "AAA"), SwingScript(plan), None, SwingRunConfig(warmup_bars=3), rec=rec
        )
        frames = rec.to_frames()
        assert codes(frames)["WARMUP"] == 3
        assert [t.entry_time for t in trades] == [df.index[5]]
        assert frames["signals"]["bar_time"].min() >= df.index[3]
        assert rec.reconcile(frames, trades) == []


# ---- execute_backtest on a real (tiny) Historify store -------------------------------------

EPOCH0 = int(pd.Timestamp("2025-01-01 09:15", tz=IST).timestamp())
SESSION_MINUTES = 375  # 09:15 to 15:29 inclusive
N_DAYS = 12
LATE_FROM = 8  # LATE lists on day index 8, past the 5-session tolerance


def _make_store(path):
    con = duckdb.connect(str(path))
    try:
        con.execute(
            "CREATE TABLE market_data (symbol VARCHAR, interval VARCHAR, timestamp BIGINT, "
            "open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT)"
        )
        rows = []
        for sym in ("AAA", "BBB", "CCC", "DDD", "PENNY", "LATE", "THIN"):
            ndays = 2 if sym == "THIN" else N_DAYS
            for d in range(LATE_FROM if sym == "LATE" else 0, ndays):
                base_ts = EPOCH0 + d * 86400
                px = 5.0 if sym == "PENNY" else 100.0
                if sym == "BBB" and d >= 9:
                    px = 20.0  # a 1:5 split: day index 9 is the split day, dropped by the loader
                for m in range(SESSION_MINUTES):
                    rows.append((sym, "1m", base_ts + m * 60, px, px + 1, px - 1, px, 5))
                if sym == "AAA" and d == 0:
                    rows.append((sym, "1m", base_ts, px, px + 1, px - 1, px, 5))  # duplicate
                    rows.append((sym, "1m", base_ts - 3600, px, px + 1, px - 1, px, 5))  # outside
                    rows.append((sym, "1m", base_ts + 11 * 60, px, px - 5, px - 1, px, 5))  # bad
        frame = pd.DataFrame(
            rows,
            columns=["symbol", "interval", "timestamp", "open", "high", "low", "close", "volume"],
        )
        con.register("rows_df", frame)
        con.execute("INSERT INTO market_data SELECT * FROM rows_df")
    finally:
        con.close()


class _Strat(Script):
    run_overrides = {
        "skip_open_minutes": 0,
        "entry_cutoff_min": 15 * 60,
        "time_exit_min": 15 * 60,
        "max_trades_per_day": 9,
        "min_sl_pct": 0.0,
    }

    def __init__(self):
        super().__init__(dict.fromkeys(range(0, 700, 7), (1, 98.0, 102.0)))

    def prepare(self, df, p):
        out = df.copy()
        day = pd.Series(out.index.date, index=out.index)
        out["day"] = day
        out["mins"] = out.index.hour * 60 + out.index.minute
        out["new_session"] = (day != day.shift(1)).to_numpy()
        out["from_open"] = out["mins"] - day.map(out.groupby(day)["mins"].min())
        return out


@dataclasses.dataclass
class _Params:
    x: int = 1


SETTINGS = {
    "eligibility_min_prior_sessions": 2,
    "eligibility_min_volume": 1,
    "eligibility_min_price": 10.0,
}


@pytest.fixture(scope="module")
def store_db(tmp_path_factory):
    db = tmp_path_factory.mktemp("historify") / "h.duckdb"
    _make_store(db)
    return db


@pytest.fixture
def store(store_db, monkeypatch):
    from signal_engine.backtest import __main__ as cli
    from signal_engine.backtest import data, harness

    db = store_db
    real = data.from_historify
    seen: dict = {}

    def loader(**kw):
        return real(db_path=db, min_sessions=4, **kw)

    class SpyBacktest(harness.Backtest):
        def __init__(self, strategy, frames, run, **kw):
            seen["frames"] = dict(frames)
            seen["run"] = run
            super().__init__(strategy, frames, run, **kw)

    monkeypatch.setattr(cli, "_register", lambda: None)
    monkeypatch.setattr(cli, "REGISTRY", {"k": (_Strat, _Params, "intraday")})
    monkeypatch.setattr(data, "from_historify", loader)
    monkeypatch.setattr(harness, "Backtest", SpyBacktest)
    monkeypatch.setattr(rr, "random_entry_baseline_intraday", lambda *a, **k: None)
    monkeypatch.setattr(rr, "_cost_model_or_none", lambda kind, slip=None: tel.MODEL)
    return seen


def _by_code(exe, code):
    ex = exe.ledgers["exclusions"]
    return ex[ex["reason_code"] == code]


class TestCollectorWiring:
    def test_split_day_rows_one_per_symbol_per_date(self, store):
        exe = rr.execute_backtest("k", settings=SETTINGS)
        rows = _by_code(exe, "SPLIT_DAY")
        assert rows[["symbol", "scope", "count"]].values.tolist() == [["BBB", "symbol_session", 1]]
        assert rows["date"].tolist() == [dt.date(2025, 1, 10)]
        assert exe.data_report["dropped"]["split_days_dropped"] == {"BBB": ["2025-01-10"]}

    def test_min_sessions_rows_are_symbol_scoped(self, store):
        exe = rr.execute_backtest("k", settings=SETTINGS)
        rows = _by_code(exe, "SYMBOL_MIN_SESSIONS")
        assert rows[["symbol", "scope", "count"]].values.tolist() == [["THIN", "symbol", 1]]
        assert "2" in rows["detail"].iloc[0]
        assert exe.data_report["dropped"]["symbols_dropped_min_sessions"] == {"THIN": 2}

    def test_bad_bar_counts_equal_an_independent_count(self, store):
        exe = rr.execute_backtest("k", settings=SETTINGS)
        rows = _by_code(exe, "BAD_BAR")
        assert rows["symbol"].tolist() == ["AAA"] and set(rows["scope"]) == {"symbol"}
        # independent count of the rows added to AAA on day 0: a second 09:15 bar and a second
        # 09:26 bar (two duplicate stamps), one 08:15 bar (outside hours) and the 09:26 repeat
        # is the high-below-low bar that survives de-duplication (one impossible bar)
        assert exe.data_report["dropped"]["clean_dropped"] == {
            "AAA": {"duplicate_ts": 2, "outside_hours": 1, "impossible": 1}
        }
        assert int(rows["count"].iloc[0]) == 4
        detail = rows["detail"].iloc[0]
        for part in ("duplicate_ts=2", "outside_hours=1", "impossible=1"):
            assert part in detail

    def test_not_eligible_rows_equal_an_independent_count(self, store):
        exe = rr.execute_backtest("k", settings=SETTINGS)
        rows = _by_code(exe, "NOT_ELIGIBLE")
        assert set(rows["scope"]) == {"symbol_session"} and set(rows["count"]) == {1}
        per_symbol = rows.groupby("symbol").size().to_dict()
        # AAA to DDD are present from the panel start, so listing age does not apply to them.
        # PENNY loses all twelve sessions to the price floor. LATE lists on day index 8, so its
        # first two sessions (Jan 9 and Jan 10) are too young.
        assert per_symbol == {"PENNY": 12, "LATE": 2}
        reasons = rows[rows["symbol"] == "PENNY"]["detail"].value_counts().to_dict()
        assert reasons == {"PRICE_FLOOR": 12}
        assert set(rows[rows["symbol"] == "LATE"]["detail"]) == {"LISTING_AGE"}
        assert rows[rows["symbol"] == "LATE"]["date"].tolist() == [
            dt.date(2025, 1, 9),
            dt.date(2025, 1, 10),
        ]

    def test_dropped_sessions_are_absent_from_what_the_engine_sees(self, store):
        exe = rr.execute_backtest("k", settings=SETTINGS)
        seen = store["frames"]
        assert sorted(seen) == ["AAA", "BBB", "CCC", "DDD", "LATE"]  # PENNY has none left
        gone = {dt.date(2025, 1, 9), dt.date(2025, 1, 10)}
        assert not gone & set(seen["LATE"].index.date)
        assert dt.date(2025, 1, 11) in set(seen["LATE"].index.date)
        assert dt.date(2025, 1, 1) in set(seen["AAA"].index.date)
        assert exe.ledger_problems == []

    def test_data_quality_is_built_on_the_frames_before_the_drop(self, store):
        exe = rr.execute_backtest("k", settings=SETTINGS)
        rep = exe.data_report
        assert rep["summary"]["symbols"] == 6  # AAA to DDD, PENNY and LATE as loaded
        assert exe.symbols == ["AAA", "BBB", "CCC", "DDD", "LATE", "PENNY"]
        assert exe.data_quality["status"] == rep["status"]
        assert set(exe.data_quality) == {"status", "affects_validity", "items"}
        assert exe.data_quality["items"] == rep["items"]
        assert isinstance(exe.data_quality["affects_validity"], bool)

    def test_calendar_membership_and_universe_are_filled(self, store):
        exe = rr.execute_backtest("k", settings=SETTINGS)
        assert exe.calendar.version.startswith("historify-derived-")
        assert len(exe.calendar.trading_days) == N_DAYS
        assert exe.calendar.trading_days[-1] == dt.date(2025, 1, 12)
        assert exe.calendar.source == "derived_from_market_data"
        assert exe.membership["membership_source"] == MEMBERSHIP_SOURCE
        assert exe.membership["survivorship_bias"] is True
        assert exe.membership["eligibility_policy"] == {
            "min_prior_sessions": 2,
            "min_volume": 1.0,
            "min_price": 10.0,
        }
        assert exe.universe_frame["symbol"].tolist() == [
            "AAA",
            "BBB",
            "CCC",
            "DDD",
            "LATE",
            "PENNY",
        ]

    def test_run_sets_warmup_zero_by_default_and_has_no_warmup_rows(self, store):
        exe = rr.execute_backtest("k", settings=SETTINGS)
        assert store["run"].warmup_bars == 0
        assert len(_by_code(exe, "WARMUP")) == 0

    def test_warmup_from_settings_reaches_the_engine(self, store):
        exe = rr.execute_backtest("k", settings={**SETTINGS, "warmup_bars": 100})
        assert store["run"].warmup_bars == 100
        assert exe.params["run"]["warmup_bars"] == 100
        rows = _by_code(exe, "WARMUP")
        assert rows.groupby("symbol")["count"].sum().to_dict() == {
            "AAA": 100,
            "BBB": 100,
            "CCC": 100,
            "DDD": 100,
            "LATE": 100,
        }
        for sym, df in store["frames"].items():
            first = exe.ledgers["signals"]
            first = first[first["symbol"] == sym]["bar_time"].min()
            assert first >= df.index[100]
        assert exe.ledger_problems == []

    def test_without_eligibility_settings_nothing_is_dropped(self, store):
        exe = rr.execute_backtest("k")
        assert len(_by_code(exe, "NOT_ELIGIBLE")) == 0
        assert exe.universe_frame is None and exe.membership is None
        assert sorted(store["frames"]) == ["AAA", "BBB", "CCC", "DDD", "LATE", "PENNY"]
        assert exe.calendar is not None and exe.data_report is not None

    def test_ledgers_reconcile_with_the_data_layer_rows(self, store):
        exe = rr.execute_backtest("k", settings=SETTINGS)
        assert exe.ledger_problems == []
        assert ev.reconcile_frames(exe.ledgers, exe.trades) == []


class _SwingStrat(SwingScript):
    def __init__(self):
        super().__init__(dict.fromkeys(range(1, 40, 6), (1, 90.0, 120.0)))

    def prepare_for(self, symbol, df, p):
        out = df.copy()
        out["day"] = pd.Series(out.index.date, index=out.index)
        return out


class TestSwingPath:
    @pytest.fixture
    def swing_store(self, tmp_path, monkeypatch):
        from signal_engine.backtest import __main__ as cli
        from signal_engine.backtest import data

        db = tmp_path / "d.duckdb"
        con = duckdb.connect(str(db))
        try:
            con.execute(
                "CREATE TABLE market_data (symbol VARCHAR, interval VARCHAR, timestamp BIGINT, "
                "open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume BIGINT)"
            )
            rows = [
                (sym, "D", EPOCH0 + d * 86400, 100.0, 101.0, 99.5, 100.0, 5)
                for sym in ("AAA", "BBB", "CCC", "DDD", "EEE")
                for d in range(40)
            ]
            rows += [("AAA", "D", EPOCH0 + 3 * 86400, 100.0, 101.0, 99.5, 100.0, 5)]  # duplicate
            con.executemany("INSERT INTO market_data VALUES (?,?,?,?,?,?,?,?)", rows)
        finally:
            con.close()
        real = data.from_historify_daily
        monkeypatch.setattr(cli, "_register", lambda: None)
        monkeypatch.setattr(cli, "REGISTRY", {"s": (_SwingStrat, _Params, "swing")})
        monkeypatch.setattr(
            data,
            "from_historify_daily",
            lambda **kw: real(db_path=db, **{**kw, "min_sessions": 10}),
        )
        monkeypatch.setattr(rr, "random_entry_baseline", lambda *a, **k: None)
        monkeypatch.setattr(rr, "_cost_model_or_none", lambda kind, slip=None: tel.MODEL)

    def test_price_floor_does_not_delete_adjusted_daily_history(self, swing_store):
        settings = {**SETTINGS, "eligibility_min_price": 1000.0}
        exe = rr.execute_backtest("s", settings=settings)
        assert len(_by_code(exe, "NOT_ELIGIBLE")) == 0

    def test_swing_run_carries_the_data_layer_and_warmup(self, swing_store):
        settings = {**SETTINGS, "eligibility_min_prior_sessions": 3, "warmup_bars": 5}
        exe = rr.execute_backtest("s", settings=settings)
        assert exe.params["run"]["warmup_bars"] == 5
        assert exe.calendar is not None and len(exe.calendar.trading_days) == 40
        assert exe.data_report["dropped"]["interval"] == "1d"
        bad = _by_code(exe, "BAD_BAR")
        assert set(bad["symbol"]) == {"AAA"} and int(bad["count"].sum()) == 1
        assert len(_by_code(exe, "NOT_ELIGIBLE")) == 0  # all present from the panel start
        assert int(_by_code(exe, "WARMUP")["count"].sum()) == 5 * 5
        assert exe.ledger_problems == []
        assert exe.membership["survivorship_bias"] is True


class TestBaselinePopulation:
    def _capture(self, store, monkeypatch, settings):
        seen = {}

        def baseline(trades, frames, cost, **kw):
            seen["frames"] = dict(frames)
            seen["trades"] = list(trades)
            return None

        monkeypatch.setattr(rr, "random_entry_baseline_intraday", baseline)
        exe = rr.execute_backtest("k", settings=settings)
        return exe, seen

    def test_baseline_frames_exclude_ineligible_sessions(self, store, monkeypatch):
        exe, seen = self._capture(store, monkeypatch, SETTINGS)
        assert seen["frames"], "the baseline must have been given frames"
        gone = {dt.date(2025, 1, 9), dt.date(2025, 1, 10)}
        for sym, df in seen["frames"].items():
            assert sym != "PENNY"
            if sym == "LATE":
                assert not gone & set(df.index.date)
        for t in seen["trades"]:
            assert t.symbol != "PENNY"

    def test_baseline_frames_skip_warmup_bars(self, store, monkeypatch):
        settings = {**SETTINGS, "warmup_bars": 100}
        exe, seen = self._capture(store, monkeypatch, settings)
        for sym, df in seen["frames"].items():
            engine_frame = store["frames"][sym]
            assert df.index[0] == engine_frame.index[100]

    def test_no_settings_leaves_baseline_frames_untouched(self, store, monkeypatch):
        exe, seen = self._capture(store, monkeypatch, {})
        assert any(len(df) > 0 for df in seen["frames"].values())


class TestCalendarAsOf:
    def test_end_is_passed_as_the_calendar_cutoff(self, store, monkeypatch):
        got = {}
        real = rr.derive_calendar

        def spy(frames, as_of=None):
            got["as_of"] = as_of
            return real(frames, as_of=as_of)

        monkeypatch.setattr(rr, "derive_calendar", spy)
        exe = rr.execute_backtest("k", settings=SETTINGS, end="2025-01-08")
        assert got["as_of"] == dt.date(2025, 1, 8)
        assert exe.calendar.trading_days[-1] <= dt.date(2025, 1, 8)

    def test_without_end_the_last_loaded_session_is_used(self, store, monkeypatch):
        got = {}
        real = rr.derive_calendar
        monkeypatch.setattr(
            rr, "derive_calendar", lambda f, as_of=None: got.update(a=as_of) or real(f, as_of=as_of)
        )
        rr.execute_backtest("k", settings=SETTINGS)
        assert got["a"] == dt.date(2025, 1, 12)

    def test_version_ignores_bars_appended_after_the_cutoff(self):
        short = {
            "AAA": make_frame(full_sessions(6, bars=4)),
            "BBB": make_frame(full_sessions(6, bars=4)),
        }
        longer = {k: make_frame(full_sessions(9, bars=4)) for k in short}
        cut = dt.date.fromisoformat(day_str(5))
        assert derive_calendar(longer, as_of=cut).version == derive_calendar(short).version


# ---- the artifact ---------------------------------------------------------------------------

SESSION_RE = re.compile(r"^\d{2}:\d{2}$")


@pytest.fixture(scope="module")
def run_data():
    trades, rec, days = tel.synth()
    return trades, rec, rec.to_frames(), days


def _layer():
    frames = {
        "AAA": make_frame(full_sessions(6)),
        "BBB": make_frame(full_sessions(6)),
    }
    cal = derive_calendar(frames)
    col = DataCollector(interval="5m", symbols_loaded=2)
    report = dq_run.build_data_quality(frames, col, calendar=cal.trading_days, max_symbols=60)
    elig = compute_eligibility(frames, POLICY, cal)
    return cal, report, elig


def step5_fields(**over):
    cal, report, elig = _layer()
    fields = {
        "calendar": cal,
        "data_report": report,
        "data_quality": {k: report[k] for k in ("status", "affects_validity", "items")},
        "universe_frame": elig.universe_frame,
        "membership": {
            "membership_source": MEMBERSHIP_SOURCE,
            "survivorship_bias": True,
            "eligibility_policy": {"min_prior_sessions": 2, "min_volume": 1.0, "min_price": 10.0},
        },
    }
    fields.update(over)
    return fields


def step5_execution(run_data, **over):
    return tel.ledger_execution(run_data, **step5_fields(**over))


def record(root_, run_data, **over):
    exe = step5_execution(run_data, **over)
    return raw._folder(raw._record(root_, execute=lambda key: exe))


def manifest_of(folder):
    return json.loads((folder / "manifest.json").read_text(encoding="utf-8"))


class TestExecutionDefaults:
    def test_new_fields_default_to_none(self):
        exe = base._execution()
        for name in ("calendar", "data_quality", "universe_frame", "data_report", "membership"):
            assert getattr(exe, name) is None


class TestManifestFields:
    def test_fields_are_filled_and_valid(self, root, run_data):
        folder = record(root, run_data)
        m = manifest_of(folder)
        cal, _, elig = _layer()
        assert mf.validate_manifest(m) == []
        assert m["calendar_version"] == cal.version
        assert m["calendar_version"].startswith("historify-derived-")
        assert set(m["session"]) == {"exchange", "strategy_window", "broker_cutoff", "force_flat"}
        assert m["session"]["exchange"].startswith("NSE")
        assert SESSION_RE.match(m["session"]["broker_cutoff"])
        assert m["session"]["force_flat"] == "15:00"
        assert m["dataset"]["corporate_action_policy"] == dq_run.POLICY["corporate_actions"]

    def test_universe_version_is_a_stable_hash_of_policy_and_frame(self, root, run_data):
        m = manifest_of(record(root, run_data))
        _, _, elig = _layer()
        policy = {"min_prior_sessions": 2, "min_volume": 1.0, "min_price": 10.0}
        records = json.loads(elig.universe_frame.to_json(orient="records"))
        digest = hashlib.sha256(
            canonical_json({"policy": policy, "universe": records}).encode("utf-8")
        ).hexdigest()
        assert m["universe"]["version"] == "eligibility-" + digest[:8]
        again = manifest_of(record(root, run_data))
        assert again["universe"]["version"] == m["universe"]["version"]

    def test_universe_carries_membership_keys(self, root, run_data):
        m = manifest_of(record(root, run_data))
        assert m["universe"]["membership_source"] == "current_symbol_master"
        assert m["universe"]["survivorship_bias"] is True

    def test_only_what_is_still_null_is_named_missing(self, root, run_data):
        caps = manifest_of(record(root, run_data))["missing_capabilities"]
        text = " | ".join(caps)
        for path in (
            "calendar_version",
            "session.exchange",
            "session.broker_cutoff",
            "dataset.corporate_action_policy",
            "universe.version",
        ):
            assert f"{path} not captured" not in text
        assert "random.monte_carlo not captured until engine step 5" in caps

    def test_changing_the_policy_changes_the_universe_version(self, root, run_data):
        a = manifest_of(record(root, run_data))
        b_membership = step5_fields()["membership"]
        b_membership["eligibility_policy"] = {
            "min_prior_sessions": 9,
            "min_volume": 1.0,
            "min_price": 10.0,
        }
        b = manifest_of(record(root, run_data, membership=b_membership))
        assert a["universe"]["version"] != b["universe"]["version"]

    def test_runs_without_the_new_data_are_unchanged(self, root, run_data):
        exe = tel.ledger_execution(run_data)
        folder = raw._folder(raw._record(root, execute=lambda key: exe))
        m = manifest_of(folder)
        assert m["calendar_version"] is None
        assert m["universe"]["version"] is None
        assert "membership_source" not in m["universe"]
        assert "calendar_version not captured until engine step 5" in m["missing_capabilities"]
        found = {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}
        assert found == raw.FILES | tel.LEDGER_FILES
        report = tel.report_of(folder)
        assert report["data_quality"] == {
            "status": "NOT_RUN",
            "affects_validity": False,
            "items": [],
        }


class TestNullNotes:
    def _manifest(self, product, calendar_version="historify-derived-abcd1234"):
        return {
            "calendar_version": calendar_version,
            "session": {
                "exchange": "NSE",
                "broker_cutoff": None,
                "force_flat": None,
                "strategy_window": None,
            },
            "dataset": {"corporate_action_policy": "x"},
            "universe": {"version": "eligibility-12345678"},
            "random": {"monte_carlo": None},
            "deployment_profile": {"product": product},
        }

    def test_cnc_names_force_flat_with_its_reason(self):
        notes = ra._null_notes(self._manifest("CNC"))
        flat = [n for n in notes if "session.force_flat" in n]
        assert len(flat) == 1 and "CNC" in flat[0]
        assert any("session.broker_cutoff" in n for n in notes)
        assert any("random.monte_carlo" in n for n in notes)
        assert not any("calendar_version" in n or "universe.version" in n for n in notes)


class TestEvidenceFiles:
    def test_data_quality_and_universe_files_are_written_listed_and_verified(self, root, run_data):
        folder = record(root, run_data)
        m = manifest_of(folder)
        assert "data_quality.json" in m["artifacts"]
        assert "execution/universe.parquet" in m["artifacts"]
        assert mf.verify_artifacts(folder) == []
        body = json.loads((folder / "data_quality.json").read_text(encoding="utf-8"))
        assert body == json.loads(json.dumps(step5_fields()["data_report"]))
        uni = trades_io.read_ledger_parquet(folder / "execution" / "universe.parquet")
        assert uni["symbol"].tolist() == ["AAA", "BBB"]
        assert "ineligible_sessions" in uni.columns

    def test_files_are_deterministic(self, root, run_data):
        a = record(root, run_data)
        b = record(
            root,
            run_data,
        )
        for rel in ("data_quality.json", "execution/universe.parquet"):
            assert (a / rel).read_bytes() == (b / rel).read_bytes()

    def test_report_carries_the_real_data_quality(self, root, run_data):
        folder = record(root, run_data)
        report = tel.report_of(folder)
        assert set(report["data_quality"]) == {"status", "affects_validity", "items"}
        assert report["data_quality"]["status"] == step5_fields()["data_quality"]["status"]

    def test_warning_that_affects_validity_makes_the_verdict_inconclusive(self, root, run_data):
        bad = {
            "status": "WARNING",
            "affects_validity": True,
            "items": [{"check": "impossible_bars", "severity": "warning", "detail": "many"}],
        }
        report = {**step5_fields()["data_report"], **bad}
        folder = record(root, run_data, data_quality=bad, data_report=report)
        out = tel.report_of(folder)
        assert out["data_quality"]["affects_validity"] is True
        assert out["verdict"]["run_verdict"] == "INCONCLUSIVE"
        assert "data quality" in out["verdict"]["reasons"][0]["text"].lower()

    def test_clean_data_does_not_force_inconclusive_on_its_own(self, root, run_data):
        out = tel.report_of(record(root, run_data))
        reasons = " ".join(r["text"] for r in out["verdict"]["reasons"]).lower()
        assert "data quality problem" not in reasons


# ---- reproduce ------------------------------------------------------------------------------


def _reproduce(folder, root_, execution):
    return rp.reproduce(
        folder,
        root=root_,
        execute=lambda key, settings=None, end=None: execution,
        supported=SUPPORTED,
        git_state_fn=raw._git(raw.CLEAN),
        dataset_fingerprint_fn=lambda s, i, a: "d" * 64,
        now=raw.NOW,
    )


class TestReproduceCalendar:
    def test_unchanged_calendar_passes(self, root, run_data):
        exe = step5_execution(run_data)
        folder = raw._folder(raw._record(root, execute=lambda key: exe))
        report = _reproduce(folder, root, exe)
        assert report.checks["calendar_unchanged"] is True
        assert report.reproducible is True

    def test_changed_calendar_fails_with_a_plain_reason(self, root, run_data):
        exe = step5_execution(run_data)
        folder = raw._folder(raw._record(root, execute=lambda key: exe))
        other = dataclasses.replace(exe.calendar, version="historify-derived-00000000")
        report = _reproduce(folder, root, dataclasses.replace(exe, calendar=other))
        assert report.checks["calendar_unchanged"] is False
        assert report.reproducible is False
        assert any("calendar" in r and "trading days" in r for r in report.reasons)

    def test_rerun_without_a_calendar_fails_when_the_manifest_has_one(self, root, run_data):
        exe = step5_execution(run_data)
        folder = raw._folder(raw._record(root, execute=lambda key: exe))
        report = _reproduce(folder, root, dataclasses.replace(exe, calendar=None))
        assert report.checks["calendar_unchanged"] is False

    def test_manifest_without_a_calendar_is_skipped_with_a_note(self, root, run_data):
        exe = tel.ledger_execution(run_data)
        folder = raw._folder(raw._record(root, execute=lambda key: exe))
        report = _reproduce(folder, root, exe)
        assert report.checks["calendar_unchanged"] is True
        assert report.reproducible is True
        assert any("calendar" in n for n in report.notes)

    def test_check_is_in_the_required_list(self):
        assert "calendar_unchanged" in rp.CHECKS


# ---- evaluators -----------------------------------------------------------------------------


def evaluate(**kw):
    return tel.evaluate([], **kw)


def gate(profile, name):
    return tel.row(profile, name)


def dq_with(**over):
    report = json.loads(json.dumps(step5_fields()["data_report"]))
    report.update(over)
    return report


def ledgers_with_not_eligible(n_listing=2, n_price=1):
    rec = lg.LedgerRecorder()
    for k in range(n_listing):
        rec.add_exclusion(
            "AAA", "symbol_session", "NOT_ELIGIBLE", date=day_str(k), detail="LISTING_AGE"
        )
    for k in range(n_price):
        rec.add_exclusion(
            "BBB", "symbol_session", "NOT_ELIGIBLE", date=day_str(k), detail="PRICE_FLOOR"
        )
    return rec.to_frames()


class TestScopes:
    def test_symbol_session_scope_is_accepted(self):
        rec = lg.LedgerRecorder()
        rec.add_exclusion("A", "symbol_session", "SPLIT_DAY", date=day_str(0))
        assert len(rec.to_frames()["exclusions"]) == 1

    def test_unknown_scope_still_raises(self):
        with pytest.raises(ValueError):
            lg.LedgerRecorder().add_exclusion("A", "galaxy", "SPLIT_DAY")


class TestG03Timestamps:
    def test_not_evaluated_without_data_quality(self):
        assert gate(evaluate(), "G0.3")["status"] == "not_evaluated"

    def test_pass_when_every_ledger_time_is_ist(self, run_data):
        row = gate(evaluate(data_quality=dq_with(), ledgers=run_data[2]), "G0.3")
        assert row["status"] == "pass"
        assert "IST" in row["evidence"]

    def test_fail_when_a_ledger_time_is_not_ist(self, run_data):
        frames = tel.copy_frames(run_data[2])
        frames["signals"]["bar_time"] = frames["signals"]["bar_time"].dt.tz_convert("UTC")
        row = gate(evaluate(data_quality=dq_with(), ledgers=frames), "G0.3")
        assert row["status"] == "fail"
        assert "bar_time" in row["evidence"]

    def test_no_time_columns_found_is_a_report_not_a_pass(self):
        inputs = ev._Inputs(
            trades=[],
            metrics={},
            n_trials=1,
            horizon="intraday",
            cost_bps=16.0,
            cost_model=None,
            oos_start=None,
            position_caps=None,
            params=None,
            ledgers={},
            data_quality=dq_with(),
        )
        status, _, _, evidence = ev._g03(inputs)
        assert status == "report"
        assert "no ledger time columns" in evidence

    def test_report_without_ledgers_states_the_policy(self):
        row = gate(evaluate(data_quality=dq_with()), "G0.3")
        assert row["status"] == "report"
        assert "IST" in row["evidence"]


class TestG04Coverage:
    def _incomplete(self, severity="warning", audited=("AAA", "BBB"), symbols=2):
        report = dq_with()
        report["items"] = [
            {
                "check": "incomplete_sessions",
                "severity": severity,
                "detail": "3 of 12 symbol-sessions below 98% of expected bars",
                "count": 3,
                "symbols": ["AAA"],
            }
        ]
        report["summary"]["audited_symbols"] = list(audited)
        report["summary"]["symbols"] = symbols
        return report

    def test_not_evaluated_without_data_quality(self):
        assert gate(evaluate(), "G0.4")["status"] == "not_evaluated"

    def test_pass_when_every_symbol_was_audited_and_complete(self):
        report = dq_with()
        row = gate(evaluate(data_quality=report), "G0.4")
        assert row["status"] == "pass"

    def test_fail_lists_the_names(self):
        row = gate(evaluate(data_quality=self._incomplete()), "G0.4")
        assert row["status"] == "fail"
        assert "AAA" in row["evidence"]

    def test_info_severity_completeness_item_is_a_report_not_a_fail(self):
        row = gate(evaluate(data_quality=self._incomplete(severity="info")), "G0.4")
        assert row["status"] == "report"
        assert "AAA" in row["evidence"]

    def test_partial_audit_cannot_pass(self):
        report = dq_with()
        report["summary"]["audited_symbols"] = ["AAA"]
        report["summary"]["symbols"] = 200
        row = gate(evaluate(data_quality=report), "G0.4")
        assert row["status"] == "report"
        assert "1 of 200" in row["evidence"]

    def test_daily_bars_are_not_evaluated(self):
        report = dq_with()
        report["dropped"]["interval"] = "1d"
        assert gate(evaluate(data_quality=report), "G0.4")["status"] == "not_evaluated"

    def test_short_sessions_from_the_calendar_are_listed(self):
        cal = Calendar(
            (dt.date(2026, 6, 1), dt.date(2026, 6, 2)),
            (dt.date(2026, 6, 2),),
            "derived_from_market_data",
            "historify-derived-aaaaaaaa",
        )
        row = gate(evaluate(data_quality=dq_with(), calendar=cal), "G0.4")
        assert "2026-06-02" in row["evidence"]


class TestG06ClosingAuction:
    def test_not_evaluated_without_data_quality(self):
        assert gate(evaluate(), "G0.6")["status"] == "not_evaluated"

    def test_reports_the_policy_and_never_passes(self):
        row = gate(evaluate(data_quality=dq_with()), "G0.6")
        assert row["status"] == "report"
        assert "09:15-15:29" in row["evidence"]


class TestG08CorporateActions:
    def test_not_evaluated_without_data_quality(self):
        assert gate(evaluate(), "G0.8")["status"] == "not_evaluated"

    def test_reports_policy_and_limits_and_never_passes(self):
        report = dq_with()
        report["dropped"]["split_days_dropped"] = {"BBB": ["2025-01-05"], "CCC": ["2025-02-01"]}
        row = gate(evaluate(data_quality=report), "G0.8")
        assert row["status"] == "report"
        text = row["evidence"]
        assert "0.75 to 1.33" in text
        assert "NOT detected" in text
        assert "2 symbol-sessions" in text or "2 sessions" in text


class TestG47Survivorship:
    def test_not_evaluated_without_membership(self):
        assert gate(evaluate(), "G4.7")["status"] == "not_evaluated"

    def test_reports_the_bias_and_never_passes(self):
        row = gate(evaluate(membership=step5_fields()["membership"]), "G4.7")
        assert row["status"] == "report"
        assert "current_symbol_master" in row["evidence"]
        assert "not point-in-time" in row["evidence"].lower()
        assert row["value"] is True


class TestG62Eligibility:
    def test_not_evaluated_without_membership_or_ledgers(self, run_data):
        assert gate(evaluate(ledgers=run_data[2]), "G6.2")["status"] == "not_evaluated"
        member = step5_fields()["membership"]
        assert gate(evaluate(membership=member), "G6.2")["status"] == "not_evaluated"

    def test_reports_policy_counts_and_what_is_not_modelled(self):
        row = gate(
            evaluate(membership=step5_fields()["membership"], ledgers=ledgers_with_not_eligible()),
            "G6.2",
        )
        assert row["status"] == "report"
        assert row["value"] == 3
        text = row["evidence"]
        assert "LISTING_AGE=2" in text and "PRICE_FLOOR=1" in text
        assert "min_prior_sessions" in text
        for name in ("ASM", "GSM", "T2T"):
            assert name in text
        assert "not modelled" in text.lower()

    def test_zero_rows_still_reports(self):
        row = gate(
            evaluate(
                membership=step5_fields()["membership"],
                ledgers=ledgers_with_not_eligible(0, 0),
            ),
            "G6.2",
        )
        assert row["status"] == "report" and row["value"] == 0

    def test_swing_horizon_stays_not_applicable(self):
        row = gate(
            evaluate(
                horizon="swing",
                membership=step5_fields()["membership"],
                ledgers=ledgers_with_not_eligible(),
            ),
            "G6.2",
        )
        assert row["status"] == "not_applicable"


class TestDataQualityLayer:
    def _frames(self, n_symbols, half_day=None):
        frames = {}
        for k in range(n_symbols):
            sessions = [(day_str(d), [FLAT] * (30 if d == half_day else 75)) for d in range(4)]
            frames[f"S{k:03d}"] = make_frame(sessions)
        return frames

    def test_short_session_counts_as_incomplete_unless_excluded(self):
        frames = self._frames(3, half_day=2)
        col = DataCollector(interval="5m")
        for s in frames:
            col.record_seen(s, len(frames[s]))
        plain = dq_run.build_data_quality(frames, col)
        assert any(i["check"] == "incomplete_sessions" for i in plain["items"])
        short = (dt.date.fromisoformat(day_str(2)),)
        fixed = dq_run.build_data_quality(frames, col, short_sessions=short)
        assert not any(i["check"] == "incomplete_sessions" for i in fixed["items"])

    def test_healthy_sampled_five_minute_run_stays_pass(self):
        frames = self._frames(150)
        col = DataCollector(interval="5m", symbols_loaded=150)
        for s, df in frames.items():
            col.record_seen(s, len(df) * 5)  # 1-minute rows read before resampling
        col.record_clean("S000", duplicate_ts=2)
        rep = dq_run.build_data_quality(frames, col, max_symbols=60)
        assert len(rep["summary"]["audited_symbols"]) == 60
        assert rep["status"] == "PASS"
        assert rep["affects_validity"] is False

    def test_layer_excludes_calendar_short_sessions(self):
        frames = self._frames(5, half_day=2)
        col = DataCollector(interval="5m")
        for s in frames:
            col.record_seen(s, len(frames[s]))
        layer = rr._build_data_layer(frames, col, {}, as_of=None)
        assert dt.date.fromisoformat(day_str(2)) in layer.calendar.short_sessions
        assert not any(i["check"] == "incomplete_sessions" for i in layer.data_report["items"])


class TestSessionFallback:
    def test_missing_profile_rules_fall_back_to_the_resolved_fields(
        self, root, run_data, monkeypatch
    ):
        monkeypatch.setattr(ra, "_profile_rules", lambda frozen, product: None)
        m = manifest_of(record(root, run_data))
        assert m["session"]["force_flat"] == "15:00"
        assert m["session"]["broker_cutoff"] == "14:55"
        assert mf.validate_manifest(m) == []
