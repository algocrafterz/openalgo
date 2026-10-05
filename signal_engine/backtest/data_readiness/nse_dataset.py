"""Adjusted NSE daily staging dataset (plan revision 4, Phase 2d).

Builds a new DuckDB from the NSE raw staging table and the corporate-action events:

    daily_raw_factors  raw NSE OHLCV, the series used (EQ, else BE), the price and volume factors,
                       the source file
    market_data        the adjusted values in the landing-store layout, so the readiness audit and
                       its rules run unchanged (daily bars stamped 05:30 IST, exchange NSE)
    ca_events          every applied event with its factor, method and source
    ca_unresolved      every announcement that could not be resolved (blocks publication)
    series_gaps        sessions where a symbol traded only in a series the rule does not use
                       (BZ, BL, ...); these sessions are missing by construction
    dataset_meta       source, price basis, versions, known limitations, and status (OK, or
                       BLOCKED when any announcement is unresolved)

The output file must not exist: a build never modifies an earlier build. The build writes a
temporary file and renames it at the end, so a failed build leaves nothing behind. Raw values are copied
unchanged; adjusted = raw * price_factor, adjusted volume = round(raw volume / volume_factor).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import duckdb
import pandas as pd

from signal_engine.backtest.data_readiness import corporate_actions as ca
from signal_engine.backtest.data_readiness import staging as _staging
from signal_engine.timeutils import IST
from utils.logging import get_logger

logger = get_logger(__name__)

SOURCE = "NSE CM bhavcopy, series EQ (BE when EQ is absent)"
#: Owner decision 2026-10-05: EQ and BE are the same security (BE is trade-for-trade settlement).
SERIES_RULE = "EQ, else BE from the same NSE file; BL and others never used"
SERIES_RULE_VERSION = "1"
USED_SERIES = ("EQ", "BE")
PRICE_BASIS = ("raw NSE official OHLCV, backward-adjusted for splits, bonuses, rights and resolved "
               "demergers by the corporate-action layer; raw values kept in daily_raw_factors")
LIMITATIONS = ("dividends and bonus debentures/NCRPS not adjusted (price return, not total return); "
               "demerger factor = NSE ex-date open / previous close, an approximation that "
               "includes the ex-date market move; "
               "history not joined across symbol renames; universe is today's F&O list "
               "(survivorship)")


class DatasetError(RuntimeError):
    """The dataset cannot be built safely."""


@dataclass
class BuildReport:
    rows: int = 0
    events: list[ca.Event] = field(default_factory=list)
    unresolved: list[ca.Unresolved] = field(default_factory=list)
    events_version: str = ""


def _raw_rows(raw_db: Path, symbols: Sequence[str]) -> pd.DataFrame:
    con = duckdb.connect(str(raw_db), read_only=True)
    try:
        return con.execute(
            # One row per (symbol, session): EQ when present, else BE.
            "SELECT symbol, session, series, open, high, low, close, volume, source_file, "
            "source_sha256 FROM nse_daily_raw WHERE list_contains(?::VARCHAR[], series) "
            "AND list_contains(?::VARCHAR[], symbol) "
            "QUALIFY row_number() OVER (PARTITION BY symbol, session "
            "ORDER BY CASE series WHEN 'EQ' THEN 0 ELSE 1 END) = 1 "
            "ORDER BY symbol, session", [list(USED_SERIES), list(symbols)]).df().pipe(_as_dates)
    finally:
        con.close()


def _as_dates(df: pd.DataFrame) -> pd.DataFrame:
    """Session as Python dates, so the DuckDB column stays DATE (pandas would make it TIMESTAMP)."""
    return df.assign(session=pd.to_datetime(df["session"]).dt.date)


def _series_gaps(raw_db: Path, symbols: Sequence[str]) -> pd.DataFrame:
    con = duckdb.connect(str(raw_db), read_only=True)
    try:
        return con.execute(
            "SELECT o.symbol, o.session, string_agg(DISTINCT o.series, ',') AS series "
            "FROM nse_daily_raw o WHERE NOT list_contains(?::VARCHAR[], o.series) "
            "AND list_contains(?::VARCHAR[], o.symbol) "
            "AND NOT EXISTS (SELECT 1 FROM nse_daily_raw e WHERE list_contains(?::VARCHAR[], "
            "e.series) AND e.symbol = o.symbol AND e.session = o.session) "
            "GROUP BY 1, 2 ORDER BY 1, 2",
            [list(USED_SERIES), list(symbols), list(USED_SERIES)]).df().pipe(_as_dates)
    finally:
        con.close()


def _ex_open(raw: pd.DataFrame):
    opens = {(s, d): float(o) for s, d, o in zip(raw["symbol"], raw["session"], raw["open"],
                                                  strict=True)}
    return lambda symbol, ex_date: opens.get((symbol, ex_date))


def _prev_close(raw: pd.DataFrame):
    by_symbol = {s: g[["session", "close"]].reset_index(drop=True) for s, g in raw.groupby("symbol")}

    def lookup(symbol: str, ex_date: date) -> float | None:
        g = by_symbol.get(symbol)
        if g is None:
            return None
        before = g[pd.to_datetime(g["session"]).dt.date < ex_date]
        return None if before.empty else float(before["close"].iloc[-1])

    return lookup


def _market_data(raw: pd.DataFrame) -> pd.DataFrame:
    built = datetime.now(IST).replace(tzinfo=None, microsecond=0)
    # 00:00 UTC of the session = 05:30 IST; the unit is fixed explicitly, never inferred.
    epoch = pd.to_datetime(raw["session"]).astype("datetime64[s]").astype("int64")
    return pd.DataFrame({
        "symbol": raw["symbol"], "exchange": "NSE", "interval": "D", "timestamp": epoch,
        "open": raw["open"] * raw["price_factor"], "high": raw["high"] * raw["price_factor"],
        "low": raw["low"] * raw["price_factor"], "close": raw["close"] * raw["price_factor"],
        "volume": (raw["volume"] / raw["volume_factor"]).round().astype("int64"),
        "oi": 0, "created_at": built,
    })


def build(raw_db: str | Path, out: str | Path, *, symbols: Sequence[str],
          announcements: Iterable[dict], overrides: Sequence[ca.Override],
          calendar_version: str, announcements_source: str = "",
          overrides_source: str = "") -> BuildReport:
    """Build the adjusted staging dataset at `out` (a new file). Returns what was applied."""
    out = Path(out).resolve()
    if out.exists():
        raise DatasetError(f"{out} exists; a build never modifies an earlier build")
    if _staging._is_landing_store(out):
        raise DatasetError("refusing to write the landing store")
    raw = _raw_rows(Path(raw_db), symbols)
    wanted = set(symbols)
    # Only an event inside a symbol's own data window changes a row: an ex-date on or before
    # the first bar, or after the last bar, adjusts nothing and is not evaluated.
    window = {s: (g["session"].min(), g["session"].max()) for s, g in raw.groupby("symbol")}
    def in_window(a: dict) -> bool:
        ex = ca.ex_date_of(a)
        lo, hi = window[a["symbol"]]
        return ex is None or lo < ex <= hi     # no ex-date: kept, so build_events can judge it

    anns = [a for a in announcements if a.get("symbol") in window and in_window(a)]
    events, unresolved = ca.build_events(anns, prev_close=_prev_close(raw), ex_open=_ex_open(raw),
                                         overrides=overrides)
    factored = ca.apply_factors(raw[["symbol", "session"]], events)
    raw = raw.assign(price_factor=factored["price_factor"].to_numpy(),
                     volume_factor=factored["volume_factor"].to_numpy())
    report = BuildReport(len(raw), events, unresolved, ca.events_version(events))
    gaps = _series_gaps(Path(raw_db), symbols)
    meta = {"status": "BLOCKED" if unresolved else "OK", "source": SOURCE,
            "series_rule": SERIES_RULE, "series_rule_version": SERIES_RULE_VERSION, "price_basis": PRICE_BASIS, "known_limitations": LIMITATIONS,
            "calendar_version": calendar_version, "corporate_action_version": report.events_version,
            "symbols": json.dumps(sorted(wanted)), "rows": str(len(raw)),
            "built_at_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
            "raw_staging": str(raw_db), "announcements_source": announcements_source,
            "overrides_source": overrides_source}
    tmp = out.with_name(out.name + ".tmp")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    try:
        factors = raw.rename(columns={c: f"raw_{c}" for c in
                                      ("open", "high", "low", "close", "volume")})
        md = _market_data(raw)
        ev = pd.DataFrame([e.__dict__ for e in events], columns=list(ca.Event.__dataclass_fields__))
        un = pd.DataFrame([u.__dict__ for u in unresolved],
                          columns=list(ca.Unresolved.__dataclass_fields__))
        for name, df in (("daily_raw_factors", factors), ("market_data", md), ("ca_events", ev),
                         ("ca_unresolved", un), ("series_gaps", gaps)):
            con.register("_t", df)
            con.execute(f"CREATE TABLE {name} AS SELECT * FROM _t")
            con.unregister("_t")
        con.execute("CREATE TABLE dataset_meta (key VARCHAR PRIMARY KEY, value VARCHAR)")
        con.executemany("INSERT INTO dataset_meta VALUES (?, ?)", list(meta.items()))
        con.close()
        os.replace(tmp, out)
    except BaseException:
        con.close()
        tmp.unlink(missing_ok=True)
        raise
    logger.info(f"built {out}: {len(raw)} rows, {len(events)} events, "
                f"{len(unresolved)} unresolved")
    return report


_REPO = Path(__file__).resolve().parents[3]


def _file_ref(path: Path) -> str:
    if not Path(path).is_file():
        return "none"
    resolved = Path(path).resolve()
    shown = resolved.relative_to(_REPO) if resolved.is_relative_to(_REPO) else resolved
    return f"{shown.as_posix()} sha256 {hashlib.sha256(resolved.read_bytes()).hexdigest()}"


def main(argv: list[str] | None = None) -> int:
    from signal_engine.backtest.data_readiness.calendar import load_calendar

    ap = argparse.ArgumentParser(prog="signal_engine.backtest.data_readiness.nse_dataset")
    ap.add_argument("--raw", required=True, help="NSE raw staging DuckDB (nse_eod)")
    ap.add_argument("--announcements", required=True, help="NSE corporate-action feed JSON")
    ap.add_argument("--symbols", required=True, help="file with one symbol per line")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    blob = Path(args.announcements).read_bytes()
    feed = json.loads(blob)
    source = f"{args.announcements} sha256 {hashlib.sha256(blob).hexdigest()}"
    anns = [a for rows in feed.values() for a in rows] if isinstance(feed, dict) else feed
    symbols = [s.strip() for s in Path(args.symbols).read_text().split() if s.strip()]
    report = build(args.raw, args.out, symbols=symbols, announcements=anns,
                   overrides=ca.load_overrides(), calendar_version=load_calendar().version,
                   announcements_source=source, overrides_source=_file_ref(ca.DEFAULT_OVERRIDES))
    print(f"rows {report.rows}, events {len(report.events)}, unresolved {len(report.unresolved)}")
    for u in report.unresolved:
        print(f"  UNRESOLVED {u.symbol} {u.ex_date} {u.kind}: {u.subject} ({u.reason})")
    return 1 if report.unresolved else 0


if __name__ == "__main__":
    raise SystemExit(main())
