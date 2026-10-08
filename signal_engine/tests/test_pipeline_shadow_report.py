"""Shadow-vs-Pine comparison: informational report over what the runner logged and Pine sent."""

import datetime as dt
import sqlite3

import pandas as pd
import pytest

from signal_engine.pipeline import shadow_report as sr
from signal_engine.pipeline.state import RunnerStateStore

IST = "Asia/Kolkata"
DAY = dt.date(2026, 9, 30)


def t(s):
    return pd.Timestamp(f"2026-09-30 {s}", tz=IST)


def make_trades_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, strategy TEXT, direction TEXT,"
        " symbol TEXT, status TEXT, message TEXT, sig_id TEXT)"
    )
    conn.executemany(
        "INSERT INTO trades (strategy, direction, symbol, status, message, sig_id)"
        " VALUES (?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


@pytest.fixture
def store(tmp_path):
    st = RunnerStateStore(str(tmp_path / "state.db"), trades_lookup=lambda s: False)
    yield st
    st.close()


def sig(st, symbol, at, direction="LONG"):
    st.record_shadow(
        DAY, "BREAKOUT", symbol, t(at), "signal", direction=direction,
        sig_id=f"{symbol}-20260930-{at.replace(':', '')}", entry=100.0, sl=99.0, tp=102.0,
    )


class TestLoadPine:
    def test_reads_only_entry_rows_for_the_strategy_and_day(self, tmp_path):
        db = str(tmp_path / "trades.db")
        make_trades_db(
            db,
            [
                ("BREAKOUT", "LONG", "TCS", "SUCCESS", "success", "TCS-20260930-0955"),
                ("BREAKOUT", "EXIT", "TCS", "SUCCESS", "closed", "TCS-20260930-0955"),
                ("BREAKOUT", "SHORT", "INFY", "DECLINED", "slots full", "INFY-20260930-1010"),
                ("ORB", "LONG", "SBIN", "SUCCESS", "ok", "SBIN-20260930-0950"),
                ("BREAKOUT", "LONG", "SBIN", "SUCCESS", "ok", "SBIN-20260929-0950"),
                ("BREAKOUT", "LONG", "ITC", "SUCCESS", "ok", None),
            ],
        )
        got = sr.load_pine_signals(DAY, "BREAKOUT", db)
        assert [(p.symbol, p.direction, p.status, p.bar_time) for p in got] == [
            ("TCS", "LONG", "SUCCESS", t("09:55")),
            ("INFY", "SHORT", "DECLINED", t("10:10")),
        ]
        assert got[1].message == "slots full"

    def test_missing_db_is_an_error_not_an_empty_list(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            sr.load_pine_signals(DAY, "BREAKOUT", str(tmp_path / "nope.db"))


class TestCompare:
    def pine(self, symbol, at, direction="LONG", status="SUCCESS"):
        """A Pine row for a setup the runner arms on bar `at`; Pine stamps the fill bar, 5 min on."""
        stamp = t(at) + pd.Timedelta(minutes=5)
        return sr.PineSignal(
            f"{symbol}-20260930-{stamp:%H%M}", symbol, direction, stamp, status, ""
        )

    def test_pine_fill_bar_stamp_matches_the_runner_arming_bar_by_default(self, store):
        sig(store, "PFC", "10:15")
        pine = sr.PineSignal("PFC-20260930-1020", "PFC", "LONG", t("10:20"), "SUCCESS", "")
        rep = sr.compare(
            store.shadow_events(DAY, "BREAKOUT"), [pine],
            universe={"PFC"}, last_bars={"PFC": t("11:00")},
        )
        assert [m.sig_id for m in rep.matched] == ["PFC-20260930-1015"]
        assert rep.runner_only == [] and rep.pine_only == []

    def test_a_zero_offset_reproduces_the_old_exact_key(self, store):
        sig(store, "PFC", "10:15")
        pine = sr.PineSignal("PFC-20260930-1015", "PFC", "LONG", t("10:15"), "SUCCESS", "")
        rep = sr.compare(
            store.shadow_events(DAY, "BREAKOUT"), [pine],
            universe={"PFC"}, last_bars={"PFC": t("11:00")}, pine_bar_offset=pd.Timedelta(0),
        )
        assert len(rep.matched) == 1

    def test_any_other_offset_is_a_divergence_not_a_match(self, store):
        sig(store, "SBIN", "10:15")
        pine = sr.PineSignal("SBIN-20260930-1010", "SBIN", "LONG", t("10:10"), "SUCCESS", "")
        rep = sr.compare(
            store.shadow_events(DAY, "BREAKOUT"), [pine],
            universe={"SBIN"}, last_bars={"SBIN": t("11:00")},
        )
        assert rep.matched == []
        assert [e.sig_id for e in rep.runner_only] == ["SBIN-20260930-1015"]
        assert [p.symbol for p, _ in rep.pine_only] == ["SBIN"]

    def test_raw_exact_matches_count_the_as_stamped_key_and_survive_normalising(self, store):
        sig(store, "TCS", "10:00")
        sig(store, "PFC", "10:15")
        same_stamp = sr.PineSignal("TCS-20260930-1000", "TCS", "LONG", t("10:00"), "SUCCESS", "")
        fill_stamp = sr.PineSignal("PFC-20260930-1020", "PFC", "LONG", t("10:20"), "SUCCESS", "")
        rep = sr.compare(
            store.shadow_events(DAY, "BREAKOUT"), [same_stamp, fill_stamp],
            universe={"TCS", "PFC"}, last_bars={"TCS": t("11:00"), "PFC": t("11:00")},
        )
        assert rep.raw_exact_matches == 1
        assert [m.sig_id for m in rep.matched] == ["PFC-20260930-1015"]

    def test_pine_only_reason_looks_up_the_arming_bar(self, store):
        store.record_shadow(DAY, "BREAKOUT", "SBIN", t("10:05"), "skip", reason="price above 5000")
        pine = sr.PineSignal("SBIN-20260930-1010", "SBIN", "LONG", t("10:10"), "SUCCESS", "")
        rep = sr.compare(
            store.shadow_events(DAY, "BREAKOUT"), [pine],
            universe={"SBIN"}, last_bars={"SBIN": t("11:00")},
        )
        assert "price above 5000" in rep.pine_only[0][1]

    def test_match_runner_only_and_pine_only(self, store):
        sig(store, "TCS", "09:55")
        sig(store, "INFY", "10:00")
        rep = sr.compare(
            store.shadow_events(DAY, "BREAKOUT"),
            [self.pine("TCS", "09:55"), self.pine("SBIN", "10:05")],
            universe={"TCS", "INFY", "SBIN"},
            last_bars={"TCS": t("11:00"), "INFY": t("11:00"), "SBIN": t("11:00")},
        )
        assert [m.sig_id for m in rep.matched] == ["TCS-20260930-0955"]
        assert [r.sig_id for r in rep.runner_only] == ["INFY-20260930-1000"]
        assert [p.symbol for p, _ in rep.pine_only] == ["SBIN"]

    def test_direction_disagreement_is_its_own_bucket(self, store):
        sig(store, "TCS", "09:55", "SHORT")
        rep = sr.compare(
            store.shadow_events(DAY, "BREAKOUT"), [self.pine("TCS", "09:55", "LONG")],
            universe={"TCS"}, last_bars={"TCS": t("11:00")},
        )
        assert rep.matched == [] and len(rep.direction_mismatch) == 1

    @pytest.mark.parametrize(
        ("setup", "expect"),
        [
            ("outside", "outside the runner universe"),
            ("skip", "price above 5000"),
            ("no_fire", "evaluated the bar and did not fire"),
            ("never", "did not process this bar"),
        ],
    )
    def test_pine_only_reason(self, store, setup, expect):
        universe, last = {"SBIN"}, {"SBIN": t("11:00")}
        if setup == "outside":
            universe = {"TCS"}
        elif setup == "skip":
            store.record_shadow(DAY, "BREAKOUT", "SBIN", t("10:05"), "skip", reason="price above 5000")
        elif setup == "never":
            last = {"SBIN": t("09:50")}
        rep = sr.compare(
            store.shadow_events(DAY, "BREAKOUT"), [self.pine("SBIN", "10:05")],
            universe=universe, last_bars=last,
        )
        assert expect in rep.pine_only[0][1]

    def test_runner_skip_on_a_pine_bar_is_not_counted_as_a_runner_signal(self, store):
        store.record_shadow(DAY, "BREAKOUT", "SBIN", t("10:05"), "skip", reason="bar late")
        rep = sr.compare(
            store.shadow_events(DAY, "BREAKOUT"), [self.pine("SBIN", "10:05")],
            universe={"SBIN"}, last_bars={"SBIN": t("11:00")},
        )
        assert rep.runner_only == [] and "bar late" in rep.pine_only[0][1]

    def test_empty_inputs(self):
        rep = sr.compare([], [], universe=set(), last_bars={})
        assert rep.matched == [] and rep.runner_only == [] and rep.pine_only == []


class TestFormatAndMain:
    def test_report_text_names_every_bucket(self, store):
        sig(store, "TCS", "09:55")
        sig(store, "INFY", "10:00")
        rep = sr.compare(
            store.shadow_events(DAY, "BREAKOUT"),
            [TestCompare().pine("TCS", "09:55"), TestCompare().pine("SBIN", "10:05")],
            universe={"TCS", "INFY", "SBIN"}, last_bars={"SBIN": t("11:00")},
        )
        text = sr.format_report(rep, DAY, "BREAKOUT")
        assert "matched 1" in text and "runner-only 1" in text and "pine-only 1" in text
        assert "TCS-20260930-0955" in text and "INFY-20260930-1000" in text and "SBIN" in text

    def test_main_prints_a_report_and_returns_zero(self, tmp_path, capsys):
        state_path = str(tmp_path / "state.db")
        with RunnerStateStore(state_path, trades_lookup=lambda s: False) as st:
            sig(st, "TCS", "09:55")
        db = str(tmp_path / "trades.db")
        make_trades_db(db, [("BREAKOUT", "LONG", "TCS", "SUCCESS", "ok", "TCS-20260930-1000")])
        rc = sr.main(
            ["--day", "2026-09-30", "--state-db", state_path, "--trades-db", db,
             "--symbols", "TCS"]
        )
        assert rc == 0
        assert "matched 1" in capsys.readouterr().out
