"""Idempotency store: last processed bar per symbol and seen sig_ids, restart-safe."""

import datetime as dt

import pandas as pd

from signal_engine.pipeline.state import RunnerStateStore

IST = "Asia/Kolkata"
DAY = dt.date(2026, 9, 30)


def t(s):
    return pd.Timestamp(f"2026-09-30 {s}", tz=IST)


class TestLastBar:
    def test_unknown_is_none(self, tmp_path):
        with RunnerStateStore(str(tmp_path / "s.db")) as st:
            assert st.last_bar("analyze", DAY, "BREAKOUT", "TCS") is None

    def test_mark_then_read(self, tmp_path):
        with RunnerStateStore(str(tmp_path / "s.db")) as st:
            st.mark("analyze", DAY, "BREAKOUT", "TCS", t("09:55"))
            assert st.last_bar("analyze", DAY, "BREAKOUT", "TCS") == t("09:55")

    def test_mark_never_moves_backwards(self, tmp_path):
        with RunnerStateStore(str(tmp_path / "s.db")) as st:
            st.mark("analyze", DAY, "BREAKOUT", "TCS", t("10:00"))
            st.mark("analyze", DAY, "BREAKOUT", "TCS", t("09:55"))
            assert st.last_bar("analyze", DAY, "BREAKOUT", "TCS") == t("10:00")

    def test_keyed_by_mode_day_strategy_symbol(self, tmp_path):
        with RunnerStateStore(str(tmp_path / "s.db")) as st:
            st.mark("analyze", DAY, "BREAKOUT", "TCS", t("09:55"))
            assert st.last_bar("live", DAY, "BREAKOUT", "TCS") is None
            assert st.last_bar("analyze", DAY + dt.timedelta(days=1), "BREAKOUT", "TCS") is None
            assert st.last_bar("analyze", DAY, "ORB", "TCS") is None
            assert st.last_bar("analyze", DAY, "BREAKOUT", "INFY") is None

    def test_survives_a_restart(self, tmp_path):
        path = str(tmp_path / "s.db")
        with RunnerStateStore(path) as st:
            st.mark("analyze", DAY, "BREAKOUT", "TCS", t("09:55"))
        with RunnerStateStore(path) as st:
            assert st.last_bar("analyze", DAY, "BREAKOUT", "TCS") == t("09:55")


class TestSigIds:
    def test_recorded_id_is_seen(self, tmp_path):
        with RunnerStateStore(str(tmp_path / "s.db"), trades_lookup=lambda s: False) as st:
            assert not st.seen_sig_id("TCS-20260930-0955")
            st.record_sig_id("TCS-20260930-0955")
            assert st.seen_sig_id("TCS-20260930-0955")

    def test_id_in_trades_db_counts_as_seen(self, tmp_path):
        with RunnerStateStore(
            str(tmp_path / "s.db"), trades_lookup=lambda s: s == "TCS-20260930-0955"
        ) as st:
            assert st.seen_sig_id("TCS-20260930-0955")
            assert not st.seen_sig_id("TCS-20260930-1000")

    def test_recording_twice_is_harmless(self, tmp_path):
        with RunnerStateStore(str(tmp_path / "s.db"), trades_lookup=lambda s: False) as st:
            st.record_sig_id("X")
            st.record_sig_id("X")
            assert st.seen_sig_id("X")

    def test_default_lookup_reads_the_trades_db_including_declined_rows(self, tmp_path):
        from signal_engine import db
        from signal_engine.models import Direction, Signal

        sig = Signal(
            strategy="BREAKOUT",
            direction=Direction.LONG,
            symbol="TCS",
            entry=100.0,
            sl=99.0,
            tp=101.0,
            sig_id="TCS-20260930-0955",
        )
        db.save_declined(sig, stage="test", reason="declined")
        assert db.sig_id_exists("TCS-20260930-0955")
        assert not db.sig_id_exists("NOPE")
        with RunnerStateStore(str(tmp_path / "s.db")) as st:
            assert st.seen_sig_id("TCS-20260930-0955")


class TestClose:
    def test_close_is_idempotent(self, tmp_path):
        st = RunnerStateStore(str(tmp_path / "s.db"))
        st.close()
        st.close()


class TestShadowEvents:
    def test_signal_and_skip_round_trip(self, tmp_path):
        with RunnerStateStore(str(tmp_path / "s.db"), trades_lookup=lambda s: False) as st:
            st.record_shadow(
                DAY, "BREAKOUT", "TCS", t("09:55"), "signal",
                direction="LONG", sig_id="TCS-20260930-0955", entry=100.0, sl=99.0, tp=102.0,
            )
            st.record_shadow(DAY, "BREAKOUT", "INFY", t("10:00"), "skip", reason="price above 5000")
            rows = st.shadow_events(DAY, "BREAKOUT")
        assert [(r.symbol, r.kind) for r in rows] == [("TCS", "signal"), ("INFY", "skip")]
        sig = rows[0]
        assert sig.bar_time == t("09:55") and sig.direction == "LONG"
        assert (sig.entry, sig.sl, sig.tp) == (100.0, 99.0, 102.0)
        assert rows[1].reason == "price above 5000"

    def test_same_bar_and_kind_is_idempotent(self, tmp_path):
        with RunnerStateStore(str(tmp_path / "s.db"), trades_lookup=lambda s: False) as st:
            for _ in range(2):
                st.record_shadow(DAY, "BREAKOUT", "TCS", t("10:00"), "skip", reason="late")
            assert len(st.shadow_events(DAY, "BREAKOUT")) == 1

    def test_filters_by_day_and_strategy_and_survives_restart(self, tmp_path):
        path = str(tmp_path / "s.db")
        with RunnerStateStore(path, trades_lookup=lambda s: False) as st:
            st.record_shadow(DAY, "BREAKOUT", "TCS", t("10:00"), "skip", reason="late")
        with RunnerStateStore(path, trades_lookup=lambda s: False) as st:
            assert len(st.shadow_events(DAY, "BREAKOUT")) == 1
            assert st.shadow_events(DAY, "ORB") == []
            assert st.shadow_events(DAY + dt.timedelta(days=1), "BREAKOUT") == []

    def test_last_bars_takes_the_newest_across_modes(self, tmp_path):
        with RunnerStateStore(str(tmp_path / "s.db"), trades_lookup=lambda s: False) as st:
            st.mark("analyze", DAY, "BREAKOUT", "TCS", t("09:55"))
            st.mark("live", DAY, "BREAKOUT", "TCS", t("10:00"))
            st.mark("analyze", DAY, "BREAKOUT", "INFY", t("09:50"))
            assert st.last_bars(DAY, "BREAKOUT") == {"TCS": t("10:00"), "INFY": t("09:50")}


class TestSeenSigIdScope:
    def test_own_store_only_ignores_trades_db(self, tmp_path):
        with RunnerStateStore(str(tmp_path / "s.db"), trades_lookup=lambda s: True) as st:
            assert st.seen_sig_id("X-1")
            assert not st.seen_sig_id("X-1", include_trades=False)
            st.record_sig_id("X-1")
            assert st.seen_sig_id("X-1", include_trades=False)
