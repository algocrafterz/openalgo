"""NSE EOD ingestion: bhavcopy files land unchanged (with sha256) and parse into a staging table."""
from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import date

import pytest

from signal_engine.backtest.data_readiness import nse_eod

OLD_HEADER = ("SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,"
              "TOTALTRADES,ISIN,\n")
NEW_HEADER = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
              "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,"
              "LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,"
              "TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4\n")


def _zip(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, text)
    return buf.getvalue()


def old_file(day: date, rows=(("SBIN", "EQ", 590.0, 595.0, 588.0, 591.5, 591.0, 589.0, 1000),)):
    stamp = day.strftime("%d-%b-%Y").upper()
    body = "".join(f"{s},{se},{o},{h},{lo},{c},{la},{pc},{v},{v * c},{stamp},10,INE062A01020,\n"
                   for s, se, o, h, lo, c, la, pc, v in rows)
    return _zip(f"cm{day.strftime('%d%b%Y').upper()}bhav.csv", OLD_HEADER + body)


def new_file(day: date, rows=(("RELIANCE", "EQ", 1337.0, 1340.0, 1320.0, 1334.35, 1334.0, 2655.7, 500),)):
    d = day.isoformat()
    body = "".join(f"{d},{d},CM,NSE,STK,2885,INE002A01018,{s},{se},,,,,RELIANCE LTD,{o},{h},{lo},"
                   f"{c},{la},{pc},,{c},,,{v},{v * c},20,F1,1,,,,,\n"
                   for s, se, o, h, lo, c, la, pc, v in rows)
    return _zip(f"BhavCopy_NSE_CM_0_0_0_{day:%Y%m%d}_F_0000.csv", NEW_HEADER + body)


class FakeNse:
    def __init__(self, files):
        self.files, self.calls = files, []

    def __call__(self, url):
        self.calls.append(url)
        for day, blob in self.files.items():
            if day.strftime("%Y%m%d") in url or day.strftime("%d%b%Y").upper() in url:
                return 200, blob
        return 404, b""


def _ingest(tmp_path, files, sessions):
    con = nse_eod.open_staging(tmp_path / "nse.duckdb")
    fake = FakeNse(files)
    report = nse_eod.ingest(con, sessions, tmp_path / "landing", fetch=fake)
    return con, report, fake


def test_url_switches_format_on_2024_07_08():
    assert nse_eod.bhavcopy_url(date(2024, 7, 5)).endswith("cm05JUL2024bhav.csv.zip")
    assert "/EQUITIES/2024/JUL/" in nse_eod.bhavcopy_url(date(2024, 7, 5))
    assert nse_eod.bhavcopy_url(date(2024, 7, 8)).endswith(
        "BhavCopy_NSE_CM_0_0_0_20240708_F_0000.csv.zip")


def test_both_formats_parse_to_one_schema(tmp_path):
    d1, d2 = date(2023, 9, 28), date(2024, 10, 28)
    con, report, _ = _ingest(tmp_path, {d1: old_file(d1), d2: new_file(d2)}, [d1, d2])
    rows = con.execute("SELECT symbol, series, session, open, close, last, prev_close, volume, isin "
                       "FROM nse_daily_raw ORDER BY session").fetchall()
    assert rows == [("SBIN", "EQ", d1, 590.0, 591.5, 591.0, 589.0, 1000, "INE062A01020"),
                    ("RELIANCE", "EQ", d2, 1337.0, 1334.35, 1334.0, 2655.7, 500, "INE002A01018")]
    assert report.ok == 2 and report.missing == []


def test_values_are_stored_exactly_as_published(tmp_path):
    d = date(2023, 9, 28)
    con, _, _ = _ingest(tmp_path, {d: old_file(d, (("HAL", "EQ", 1934.8, 1950.0, 1880.0, 1889.15,
                                                    1889.0, 3837.7, 7),))}, [d])
    assert con.execute("SELECT prev_close, close FROM nse_daily_raw").fetchone() == (3837.7, 1889.15)


def test_landing_file_is_kept_with_its_sha256(tmp_path):
    d = date(2023, 9, 28)
    blob = old_file(d)
    con, _, _ = _ingest(tmp_path, {d: blob}, [d])
    path, sha = con.execute("SELECT path, sha256 FROM landing_files").fetchone()
    assert sha == hashlib.sha256(blob).hexdigest()
    assert (tmp_path / "landing" / path).read_bytes() == blob


def test_existing_landing_file_is_reused_not_refetched(tmp_path):
    d = date(2023, 9, 28)
    _ingest(tmp_path, {d: old_file(d)}, [d])
    con, report, fake = _ingest(tmp_path, {d: old_file(d)}, [d])
    assert fake.calls == [] and report.ok == 1


def test_changed_landing_file_is_an_error_not_overwritten(tmp_path):
    d = date(2023, 9, 28)
    _ingest(tmp_path, {d: old_file(d)}, [d])
    (tmp_path / "landing" / nse_eod.landing_path(d)).write_bytes(b"tampered")
    with pytest.raises(nse_eod.IngestError, match="sha256"):
        _ingest(tmp_path, {d: old_file(d)}, [d])


def test_missing_file_for_a_calendar_session_is_reported(tmp_path):
    d1, d2 = date(2023, 9, 28), date(2023, 9, 29)
    con, report, _ = _ingest(tmp_path, {d1: old_file(d1)}, [d1, d2])
    assert report.missing == [d2]
    assert con.execute("SELECT status FROM landing_files WHERE session = ?", [d2]).fetchone() == (
        "missing",)


def test_file_for_another_date_is_rejected(tmp_path):
    d1, d2 = date(2023, 9, 28), date(2023, 9, 29)
    with pytest.raises(nse_eod.IngestError, match="date"):
        _ingest(tmp_path, {d2: old_file(d1)}, [d2])


def test_reingest_of_a_session_does_not_duplicate_rows(tmp_path):
    d = date(2023, 9, 28)
    con = nse_eod.open_staging(tmp_path / "nse.duckdb")
    for _ in range(2):
        nse_eod.ingest(con, [d], tmp_path / "landing", fetch=FakeNse({d: old_file(d)}))
    assert con.execute("SELECT COUNT(*) FROM nse_daily_raw").fetchone()[0] == 1


def test_refuses_the_landing_store():
    from signal_engine.backtest.data_readiness import staging
    with pytest.raises(nse_eod.IngestError):
        nse_eod.open_staging(staging.LANDING_STORE)


def test_blank_series_rows_are_kept_with_an_empty_series(tmp_path):
    # NSE lists some debt instruments with no series (2016-01-01: IIFLFIN and IRFC bonds).
    d = date(2016, 1, 1)
    blob = old_file(d, (("SBIN", "EQ", 1, 2, 1, 2, 2, 1, 5), ("IRFC", "", 1199.55, 1199.55, 1167,
                                                               1167, 1167, 1166, 2)))
    con, report, _ = _ingest(tmp_path, {d: blob}, [d])
    assert con.execute("SELECT symbol, series FROM nse_daily_raw ORDER BY symbol").fetchall() == [
        ("IRFC", ""), ("SBIN", "EQ")]


# ---- review findings (2026-10-05) ----------------------------------------------------------

def test_an_invalid_download_is_not_kept_in_the_landing_directory(tmp_path):
    d = date(2023, 9, 28)
    with pytest.raises(nse_eod.IngestError):
        _ingest(tmp_path, {d: b"<html>error page</html>"}, [d])
    assert not (tmp_path / "landing" / nse_eod.landing_path(d)).exists()


def test_na_like_text_is_not_turned_into_a_missing_value(tmp_path):
    d = date(2023, 9, 28)
    con, _, _ = _ingest(tmp_path, {d: old_file(d, (("NA", "EQ", 1, 2, 1, 2, 2, 1, 5),))}, [d])
    assert con.execute("SELECT symbol FROM nse_daily_raw").fetchone() == ("NA",)


def test_a_missing_file_never_erases_a_recorded_ok_file(tmp_path):
    d = date(2023, 9, 28)
    con, _, _ = _ingest(tmp_path, {d: old_file(d)}, [d])
    (tmp_path / "landing" / nse_eod.landing_path(d)).unlink()
    with pytest.raises(nse_eod.IngestError, match="recorded"):
        nse_eod.ingest(con, [d], tmp_path / "landing", fetch=FakeNse({}))
