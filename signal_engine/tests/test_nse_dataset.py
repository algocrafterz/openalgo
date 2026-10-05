"""Adjusted NSE daily staging dataset: raw kept, factors applied, audit-compatible (plan rev. 4, 2d)."""
from __future__ import annotations

from datetime import date

import duckdb
import pandas as pd
import pytest

from signal_engine.backtest.data_readiness import corporate_actions as ca
from signal_engine.backtest.data_readiness import nse_dataset, nse_eod
from signal_engine.tests.dq_fixtures import ts

DAYS = [date(2024, 10, 24), date(2024, 10, 25), date(2024, 10, 28), date(2024, 10, 29)]


def _raw_store(tmp_path, rows):
    con = nse_eod.open_staging(tmp_path / "nse.duckdb")
    df = pd.DataFrame(rows, columns=["symbol", "series", "session", "open", "high", "low", "close",
                                     "volume"])
    df = df.assign(isin="INE000000000", last=df.close, prev_close=df.close, turnover=0.0, trades=1,
                   source_file="f.zip", source_sha256="abc")
    df = df[list(nse_eod.COLUMNS) + ["source_file", "source_sha256"]]
    con.register("_r", df)
    con.execute("INSERT INTO nse_daily_raw SELECT * FROM _r")
    con.close()
    return tmp_path / "nse.duckdb"


def _reliance():
    # RELIANCE 1:1 bonus, ex-date 2024-10-28 (raw close halves on the ex-date)
    closes = [2700.0, 2655.7, 1334.35, 1340.0]
    vols = [100, 100, 200, 200]
    return [("RELIANCE", "EQ", d, c, c, c, c, v) for d, c, v in zip(DAYS, closes, vols, strict=True)]


ANN = [{"symbol": "RELIANCE", "exDate": "28-Oct-2024", "subject": "Bonus 1:1", "faceVal": "10"},
       {"symbol": "RELIANCE", "exDate": "14-Aug-2025", "subject": "Dividend - Rs 5.5 Per Share",
        "faceVal": "10"}]


def _build(tmp_path, rows, anns=ANN, overrides=(), symbols=("RELIANCE",)):
    raw = _raw_store(tmp_path, rows)
    out = tmp_path / "adjusted.duckdb"
    report = nse_dataset.build(raw, out, symbols=symbols, announcements=anns,
                               overrides=overrides, calendar_version="cal-v1")
    return duckdb.connect(str(out), read_only=True), report


def test_adjusted_prices_are_continuous_across_the_bonus(tmp_path):
    con, report = _build(tmp_path, _reliance())
    closes = [r[0] for r in con.execute(
        "SELECT close FROM market_data ORDER BY timestamp").fetchall()]
    assert closes == pytest.approx([1350.0, 1327.85, 1334.35, 1340.0])
    vols = [r[0] for r in con.execute("SELECT volume FROM market_data ORDER BY timestamp").fetchall()]
    assert vols == [200, 200, 200, 200]
    assert report.unresolved == []


def test_raw_values_and_factors_are_kept(tmp_path):
    con, _ = _build(tmp_path, _reliance())
    row = con.execute("SELECT raw_close, price_factor, volume_factor, raw_volume FROM daily_raw_factors "
                      "WHERE session = DATE '2024-10-25'").fetchone()
    assert row == (2655.7, 0.5, 0.5, 100)


def test_market_data_is_audit_compatible(tmp_path):
    con, _ = _build(tmp_path, _reliance())
    cols = [r[1] for r in con.execute("PRAGMA table_info('market_data')").fetchall()]
    assert cols == ["symbol", "exchange", "interval", "timestamp", "open", "high", "low", "close",
                    "volume", "oi", "created_at"]
    first = con.execute("SELECT timestamp, interval, exchange FROM market_data ORDER BY timestamp "
                        "LIMIT 1").fetchone()
    assert first == (ts("2024-10-24", "05:30"), "D", "NSE")


def test_only_eq_series_and_requested_symbols(tmp_path):
    rows = _reliance() + [("RELIANCE", "BL", DAYS[0], 1, 1, 1, 1, 5),
                          ("OTHER", "EQ", DAYS[0], 5, 5, 5, 5, 5)]
    con, _ = _build(tmp_path, rows)
    assert con.execute("SELECT COUNT(*), COUNT(DISTINCT symbol) FROM market_data").fetchone() == (4, 1)


def test_unresolved_event_is_recorded_and_reported(tmp_path):
    anns = ANN + [{"symbol": "RELIANCE", "exDate": "29-Oct-2024", "subject": "Demerger",
                   "faceVal": "10"}]
    con, report = _build(tmp_path, _reliance(), anns=anns)
    assert [(u.symbol, u.kind) for u in report.unresolved] == [("RELIANCE", ca.DEMERGER)]
    assert con.execute("SELECT symbol, kind FROM ca_unresolved").fetchall() == [
        ("RELIANCE", "demerger")]


def test_meta_records_versions(tmp_path):
    con, report = _build(tmp_path, _reliance())
    meta = dict(con.execute("SELECT key, value FROM dataset_meta").fetchall())
    assert meta["source"] == nse_dataset.SOURCE
    assert meta["calendar_version"] == "cal-v1"
    assert meta["corporate_action_version"] == report.events_version
    assert meta["price_basis"].startswith("raw NSE")
    assert "dividends" in meta["known_limitations"]


def test_rights_factor_uses_the_raw_close_before_the_ex_date(tmp_path):
    rows = [("X", "EQ", d, 100.0, 100.0, 100.0, 100.0, 10) for d in DAYS]
    anns = [{"symbol": "X", "exDate": "28-Oct-2024", "subject": "Rights 1:4 @ Premium Rs 40",
             "faceVal": "10"}]
    con, report = _build(tmp_path, rows, anns=anns, symbols=("X",))
    (e,) = report.events
    assert e.price_factor == pytest.approx(((4 * 100 + 1 * 50) / 5) / 100)


def test_refuses_to_overwrite_an_existing_output(tmp_path):
    _build(tmp_path, _reliance())
    with pytest.raises(nse_dataset.DatasetError):
        nse_dataset.build(tmp_path / "nse.duckdb", tmp_path / "adjusted.duckdb",
                          symbols=("RELIANCE",), announcements=ANN, overrides=(),
                          calendar_version="cal-v1")


# ---- review findings (2026-10-05) ----------------------------------------------------------

def test_unresolved_events_mark_the_build_blocked(tmp_path):
    anns = ANN + [{"symbol": "RELIANCE", "exDate": "29-Oct-2024", "subject": "Demerger",
                   "faceVal": "10"}]
    con, _ = _build(tmp_path, _reliance(), anns=anns)
    assert dict(con.execute("SELECT key, value FROM dataset_meta").fetchall())["status"] == "BLOCKED"


def test_a_clean_build_is_marked_ok(tmp_path):
    con, _ = _build(tmp_path, _reliance())
    assert dict(con.execute("SELECT key, value FROM dataset_meta").fetchall())["status"] == "OK"


def test_sessions_traded_only_in_an_unused_series_are_recorded(tmp_path):
    # Owner rule 2026-10-05: EQ, else BE. A session with only BZ (or BL) stays missing and is
    # recorded as a series gap.
    rows = _reliance()[:3] + [("RELIANCE", "BZ", DAYS[3], 1340.0, 1340.0, 1340.0, 1340.0, 9)]
    con, _ = _build(tmp_path, rows)
    assert con.execute("SELECT symbol, session, series FROM series_gaps").fetchall() == [
        ("RELIANCE", DAYS[3], "BZ")]


def test_no_partial_output_after_a_failed_build(tmp_path, monkeypatch):
    raw = _raw_store(tmp_path, _reliance())
    monkeypatch.setattr(nse_dataset, "_market_data", lambda raw: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        nse_dataset.build(raw, tmp_path / "out.duckdb", symbols=("RELIANCE",), announcements=ANN,
                          overrides=(), calendar_version="c")
    assert not (tmp_path / "out.duckdb").exists()


def test_events_outside_a_symbols_data_window_are_ignored(tmp_path):
    # An event on or before the first bar, or after the last bar, changes no row.
    anns = ANN + [{"symbol": "RELIANCE", "exDate": "05-Jun-2005", "subject": "Unreadable old text",
                   "faceVal": "10"},
                  {"symbol": "RELIANCE", "exDate": "20-Jul-2027", "subject": "Demerger",
                   "faceVal": "10"}]
    con, report = _build(tmp_path, _reliance(), anns=anns)
    assert report.unresolved == []
    assert dict(con.execute("SELECT key, value FROM dataset_meta").fetchall())["status"] == "OK"


def test_price_affecting_announcement_without_an_ex_date_blocks(tmp_path):
    anns = ANN + [{"symbol": "RELIANCE", "exDate": "-", "subject": "Bonus 1:1", "faceVal": "10"},
                  {"symbol": "RELIANCE", "exDate": "-", "subject": "Dividend - Rs 1", "faceVal": "10"}]
    con, report = _build(tmp_path, _reliance(), anns=anns)
    assert [(u.symbol, u.reason) for u in report.unresolved] == [("RELIANCE", "no ex-date")]


# ---- owner decisions 2026-10-05 ------------------------------------------------------------

def test_be_series_is_used_when_eq_is_absent_and_the_series_is_recorded(tmp_path):
    rows = _reliance()[:3] + [("RELIANCE", "BE", DAYS[3], 1340.0, 1340.0, 1340.0, 1340.0, 9)]
    con, _ = _build(tmp_path, rows)
    got = con.execute("SELECT session, series FROM daily_raw_factors ORDER BY session").fetchall()
    assert got == [(DAYS[0], "EQ"), (DAYS[1], "EQ"), (DAYS[2], "EQ"), (DAYS[3], "BE")]
    assert con.execute("SELECT COUNT(*) FROM market_data").fetchone()[0] == 4


def test_eq_is_preferred_when_both_series_exist(tmp_path):
    rows = _reliance() + [("RELIANCE", "BE", DAYS[3], 1.0, 1.0, 1.0, 1.0, 1)]
    con, _ = _build(tmp_path, rows)
    assert con.execute("SELECT series, raw_close FROM daily_raw_factors WHERE session = ?",
                       [DAYS[3]]).fetchone() == ("EQ", 1340.0)


def test_block_deal_series_is_never_used(tmp_path):
    rows = _reliance()[:3] + [("RELIANCE", "BL", DAYS[3], 1.0, 1.0, 1.0, 1.0, 1)]
    con, _ = _build(tmp_path, rows)
    assert con.execute("SELECT COUNT(*) FROM daily_raw_factors").fetchone()[0] == 3


def test_meta_records_the_series_rule_and_version(tmp_path):
    con, _ = _build(tmp_path, _reliance())
    meta = dict(con.execute("SELECT key, value FROM dataset_meta").fetchall())
    assert meta["series_rule"] == "EQ, else BE from the same NSE file; BL and others never used"
    assert meta["series_rule_version"] == "1"
    assert "approximation" in meta["known_limitations"]


def test_demerger_uses_the_official_ex_date_open(tmp_path):
    rows = [("X", "EQ", DAYS[0], 100.0, 100.0, 100.0, 100.0, 10),
            ("X", "EQ", DAYS[1], 100.0, 100.0, 100.0, 100.0, 10),
            ("X", "EQ", DAYS[2], 90.0, 92.0, 89.0, 91.0, 10),
            ("X", "EQ", DAYS[3], 91.0, 91.0, 91.0, 91.0, 10)]
    anns = [{"symbol": "X", "exDate": "28-Oct-2024", "subject": "Demerger", "faceVal": "10"}]
    _, report = _build(tmp_path, rows, anns=anns, symbols=("X",))
    (e,) = report.events
    assert e.price_factor == pytest.approx(0.9) and report.unresolved == []


def test_file_reference_is_repo_relative_with_its_hash():
    ref = nse_dataset._file_ref(ca.DEFAULT_OVERRIDES)
    path, _, digest = ref.partition(" sha256 ")
    assert path == "signal_engine/research/reference/corporate_action_overrides.yaml"
    assert len(digest) == 64
