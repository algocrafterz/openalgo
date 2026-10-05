"""NSE EOD ingestion: the canonical daily source (plan revision 4, D21 and D26).

For every session the official NSE calendar expects, fetch the CM bhavcopy, keep the file unchanged
in the landing directory with its sha256, and parse its rows into a staging DuckDB. Values are stored
exactly as NSE publishes them: no adjustment, normalization or repair happens here. A session whose
file is missing is reported, never skipped or filled from another source.

    landing/<YYYY>/<file>.zip       write-once; a later fetch that differs is an error
    staging: nse_daily_raw          one row per (symbol, series, session), every series NSE lists
             landing_files          one row per session: url, path, sha256, status

    uv run python -m signal_engine.backtest.data_readiness.nse_eod --start 2016-01-01 \
        --end 2026-10-03 --staging db/staging/nse-D.duckdb --landing db/landing/nse/cm
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import time
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import duckdb
import pandas as pd

from signal_engine.backtest.data_readiness import staging as _staging
from signal_engine.timeutils import IST
from utils.logging import get_logger

logger = get_logger(__name__)

#: NSE replaced the "cmDDMONYYYYbhav" file with the UDiFF bhavcopy from this session on.
UDIFF_FROM = date(2024, 7, 8)
_OLD_URL = ("https://nsearchives.nseindia.com/content/historical/EQUITIES/{y}/{mon}/"
            "cm{d}{mon}{y}bhav.csv.zip")
_NEW_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip"
_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 "
                          "Safari/537.36", "Accept": "*/*"}

_OLD_COLS = {"SYMBOL": "symbol", "SERIES": "series", "OPEN": "open", "HIGH": "high", "LOW": "low",
             "CLOSE": "close", "LAST": "last", "PREVCLOSE": "prev_close", "TOTTRDQTY": "volume",
             "TOTTRDVAL": "turnover", "TOTALTRADES": "trades", "ISIN": "isin", "TIMESTAMP": "_day"}
_NEW_COLS = {"TckrSymb": "symbol", "SctySrs": "series", "OpnPric": "open", "HghPric": "high",
             "LwPric": "low", "ClsPric": "close", "LastPric": "last", "PrvsClsgPric": "prev_close",
             "TtlTradgVol": "volume", "TtlTrfVal": "turnover", "TtlNbOfTxsExctd": "trades",
             "ISIN": "isin", "TradDt": "_day"}
COLUMNS = ("symbol", "isin", "series", "session", "open", "high", "low", "close", "last",
           "prev_close", "volume", "turnover", "trades")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nse_daily_raw (
    symbol VARCHAR NOT NULL, isin VARCHAR, series VARCHAR NOT NULL, session DATE NOT NULL,
    open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, last DOUBLE, prev_close DOUBLE,
    volume BIGINT, turnover DOUBLE, trades BIGINT, source_file VARCHAR NOT NULL,
    source_sha256 VARCHAR NOT NULL, PRIMARY KEY (symbol, series, session));
CREATE TABLE IF NOT EXISTS landing_files (
    session DATE PRIMARY KEY, url VARCHAR, path VARCHAR, sha256 VARCHAR, bytes BIGINT,
    status VARCHAR, rows BIGINT, recorded_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS nse_meta (key VARCHAR PRIMARY KEY, value VARCHAR);
"""

Fetch = Callable[[str], tuple[int, bytes]]


class IngestError(RuntimeError):
    """The ingestion cannot continue safely."""


@dataclass
class IngestReport:
    ok: int = 0
    missing: list[date] = field(default_factory=list)


def bhavcopy_url(day: date) -> str:
    if day >= UDIFF_FROM:
        return _NEW_URL.format(ymd=day.strftime("%Y%m%d"))
    mon = day.strftime("%b").upper()
    return _OLD_URL.format(y=day.year, mon=mon, d=day.strftime("%d"))


def landing_path(day: date) -> Path:
    """The file's path relative to the landing directory."""
    return Path(str(day.year)) / bhavcopy_url(day).rsplit("/", 1)[1]


def open_staging(path: str | Path) -> duckdb.DuckDBPyConnection:
    """Open (or create) the NSE staging DuckDB. Refuses the landing store and any existing DuckDB
    file that is not an NSE staging file."""
    target = Path(path).resolve()
    if _staging._is_landing_store(target):
        raise IngestError(f"refusing to stage into the landing store {target}")
    existed = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(target))
    if existed and not con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'nse_meta'").fetchone():
        con.close()
        raise IngestError(f"{target} exists and is not an NSE staging file")
    con.execute(_SCHEMA)
    return con


def parse(blob: bytes, day: date) -> pd.DataFrame:
    """The bhavcopy rows, unchanged, with the standard column names. Raises IngestError when the
    file is not a bhavcopy for `day`."""
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".csv")]
            if len(names) != 1:
                raise IngestError(f"{day}: expected one csv in the zip, found {names}")
            # No NA coercion: a symbol such as "NA" stays text; only an empty cell is missing.
            raw = pd.read_csv(z.open(names[0]), dtype=str, keep_default_na=False,
                              na_values=[""])
    except zipfile.BadZipFile as exc:
        raise IngestError(f"{day}: not a zip file") from exc
    raw.columns = [c.strip() for c in raw.columns]
    cols = _NEW_COLS if "TckrSymb" in raw.columns else _OLD_COLS
    missing = [c for c in cols if c not in raw.columns]
    if missing:
        raise IngestError(f"{day}: bhavcopy is missing columns {missing}")
    df = raw[list(cols)].rename(columns=cols)
    for c in ("symbol", "series", "isin", "_day"):
        df[c] = df[c].str.strip()
    # Some debt instruments carry no series (2016-01-01: IIFLFIN, IRFC bonds); keep them as "".
    df["series"] = df["series"].fillna("")
    days = set(pd.to_datetime(df["_day"], format="mixed").dt.date)
    if days != {day}:
        raise IngestError(f"{day}: file carries date(s) {sorted(days)}")
    for c in ("open", "high", "low", "close", "last", "prev_close", "turnover"):
        df[c] = pd.to_numeric(df[c], errors="raise")
    for c in ("volume", "trades"):
        df[c] = pd.to_numeric(df[c], errors="raise").astype("int64")
    df["session"] = day
    return df[list(COLUMNS)]


def _write_once(path: Path, blob: bytes) -> None:
    """Write via a temporary file and an atomic rename, so a crash never leaves a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(blob)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _land(landing: Path, day: date, fetch: Fetch) -> tuple[bytes | None, str]:
    """The file for `day`, from the landing directory or fetched once and kept. A fetched file
    is parsed before it is kept, so an error page or a truncated body never lands."""
    path = landing / landing_path(day)
    if path.exists():
        return path.read_bytes(), "reused"
    status, blob = fetch(bhavcopy_url(day))
    if status == 404:
        return None, "missing"
    if status != 200 or not blob:
        raise IngestError(f"{day}: HTTP {status} from NSE")
    parse(blob, day)
    _write_once(path, blob)
    return blob, "fetched"


def _record(con, day, url, rel, sha, size, status, rows):
    con.execute("INSERT OR REPLACE INTO landing_files VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [day, url, rel, sha, size, status, rows,
                 datetime.now(IST).replace(tzinfo=None, microsecond=0)])


def _ingest_day(con, day: date, landing: Path, fetch: Fetch, report: IngestReport) -> None:
    rel = str(landing_path(day))
    blob, how = _land(landing, day, fetch)
    recorded = con.execute("SELECT status FROM landing_files WHERE session = ?", [day]).fetchone()
    if blob is None and recorded and recorded[0] == "ok":
        raise IngestError(f"{day}: NSE no longer serves a file that was recorded as ok, and the "
                          "landing copy is gone; restore the landing file")
    if blob is None:
        report.missing.append(day)
        _record(con, day, bhavcopy_url(day), None, None, 0, "missing", 0)
        logger.warning(f"NSE bhavcopy missing for calendar session {day}")
        return
    sha = hashlib.sha256(blob).hexdigest()
    known = con.execute("SELECT sha256 FROM landing_files WHERE session = ? AND sha256 IS NOT NULL",
                        [day]).fetchone()
    if known and known[0] != sha:
        raise IngestError(f"{day}: landing file sha256 {sha[:12]} differs from the recorded "
                          f"{known[0][:12]}; the landing file changed")
    df = parse(blob, day).assign(source_file=rel, source_sha256=sha)
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute("DELETE FROM nse_daily_raw WHERE session = ?", [day])
        con.register("_day_rows", df)
        con.execute("INSERT INTO nse_daily_raw SELECT * FROM _day_rows")
        con.unregister("_day_rows")
        _record(con, day, bhavcopy_url(day), rel, sha, len(blob), "ok", len(df))
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    report.ok += 1
    logger.debug(f"NSE {day}: {how}, {len(df)} rows")


def ingest(con, sessions: Iterable[date], landing: str | Path, *, fetch: Fetch,
           pause: float = 0.0) -> IngestReport:
    """Land and parse the bhavcopy of every session. Missing files are reported in the result and
    in `landing_files`; any other failure raises."""
    landing = Path(landing)
    report = IngestReport()
    for day in sessions:
        _ingest_day(con, day, landing, fetch, report)
        if pause:
            time.sleep(pause)
    return report


def http_fetch(url: str) -> tuple[int, bytes]:
    from utils.httpx_client import get_httpx_client

    r = get_httpx_client().get(url, headers=_HEADERS, timeout=60.0)
    return r.status_code, r.content


def main(argv: list[str] | None = None) -> int:
    from signal_engine.backtest.data_readiness.calendar import load_calendar

    ap = argparse.ArgumentParser(prog="signal_engine.backtest.data_readiness.nse_eod")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--staging", required=True)
    ap.add_argument("--landing", default="db/landing/nse/cm")
    ap.add_argument("--pause", type=float, default=0.5)
    args = ap.parse_args(argv)
    cal = load_calendar()
    sessions = cal.expected_sessions(date.fromisoformat(args.start), date.fromisoformat(args.end))
    con = open_staging(args.staging)
    try:
        con.executemany("INSERT OR REPLACE INTO nse_meta VALUES (?, ?)",
                        [("source", "NSE CM bhavcopy"), ("calendar_version", cal.version),
                         ("requested_start", args.start), ("requested_end", args.end)])
        report = ingest(con, sessions, args.landing, fetch=http_fetch, pause=args.pause)
    finally:
        con.close()
    print(f"sessions {len(sessions)}: ok {report.ok}, missing {len(report.missing)} "
          f"{[str(d) for d in report.missing[:20]]}")
    return 1 if report.missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
