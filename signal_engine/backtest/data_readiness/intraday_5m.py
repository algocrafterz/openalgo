"""5m aggregation of canonical 1m bars, and its independent validation (plan section 8).

`aggregate` builds the 5m bars in pandas. `validate` re-derives every expectation in DuckDB SQL
from the 1m bars, so a defect in the aggregation code cannot hide inside its own check.

Bins are fixed 5-minute intervals anchored at 09:15 IST and labelled with their start. The
closing-auction print (15:28/15:29 from 2026-08-03) falls in the 15:25 bin, alone, because
continuous trading ends at 15:14 then.
"""

from __future__ import annotations

import duckdb
import pandas as pd

from signal_engine.backtest.data_readiness import intraday_convert as ic

ANCHOR = ic.SESSION_OPEN_MIN          # 09:15
BIN = 5
MAX_BARS_PRE_CAS = 75
MAX_BARS_CAS = 73
VOLUME_RTOL = 1e-12                   # float sums taken in a different order


def _session_epoch(session: pd.Series) -> pd.Series:
    return (session.dt.tz_localize("Asia/Kolkata").astype("int64") // 10**9).astype("int64")


def aggregate(bars: pd.DataFrame) -> pd.DataFrame:
    """bars: canonical 1m (symbol, timestamp, open, high, low, close, volume)."""
    b = ic.with_clock(bars[["symbol", "timestamp", "open", "high", "low", "close", "volume"]])
    b = b.sort_values(["symbol", "timestamp"], kind="stable")
    b["bin"] = ANCHOR + (b.minute - ANCHOR) // BIN * BIN
    out = b.groupby(["symbol", "session", "bin"], sort=True).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum"),
        n_minutes=("timestamp", "size")).reset_index()
    out["timestamp"] = _session_epoch(out.session) + out.bin.astype("int64") * 60
    return out[["symbol", "session", "timestamp", "open", "high", "low", "close", "volume",
                "n_minutes"]]


_ONE = """
CREATE TEMP TABLE one AS
SELECT symbol, timestamp, open, high, low, close, volume, CAST(t AS DATE) AS session,
       hour(t) * 60 + minute(t) AS mod, 555 + (hour(t) * 60 + minute(t) - 555) // 5 * 5 AS bin
FROM (SELECT *, timezone('Asia/Kolkata', to_timestamp(timestamp)) AS t FROM m1)"""
_FIVE = """
CREATE TEMP TABLE five AS
SELECT symbol, timestamp, open, high, low, close, volume, n_minutes,
       CAST(session AS DATE) AS session, CAST(t AS DATE) AS tday, hour(t) * 60 + minute(t) AS mod
FROM (SELECT *, timezone('Asia/Kolkata', to_timestamp(timestamp)) AS t FROM m5)"""
_RE = """
CREATE TEMP TABLE re AS
SELECT symbol, session, bin, arg_min(open, timestamp) AS open, max(high) AS high,
       min(low) AS low, arg_max(close, timestamp) AS close, sum(volume) AS volume, count(*) AS n
FROM one GROUP BY ALL"""
_VOL = f"abs(a.volume - b.volume) > {VOLUME_RTOL} * greatest(abs(b.volume), 1)"
_CHECKS = {
    "1_bin_start": "SELECT count(*) FROM five WHERE mod < 555 OR (mod - 555) % 5 <> 0",
    "2_first_bin": """SELECT count(*) FROM (SELECT symbol, session, min(mod) AS f FROM five
                      GROUP BY ALL) a JOIN (SELECT symbol, session, min(bin) AS f FROM one
                      GROUP BY ALL) b USING (symbol, session) WHERE a.f <> b.f""",
    "3_cross_session": "SELECT count(*) FROM five WHERE tday <> session",
    "4_ohlc": """SELECT count(*) FROM five a FULL JOIN re b
                 ON a.symbol = b.symbol AND a.session = b.session AND a.mod = b.bin
                 WHERE a.symbol IS NULL OR b.symbol IS NULL OR a.open <> b.open
                    OR a.high <> b.high OR a.low <> b.low OR a.close <> b.close
                    OR a.n_minutes <> b.n""",
    "5_volume": f"""SELECT count(*) FROM five a JOIN re b
                    ON a.symbol = b.symbol AND a.session = b.session AND a.mod = b.bin
                    WHERE {_VOL}""",
    "6_duplicate": """SELECT count(*) FROM (SELECT symbol, timestamp FROM five GROUP BY ALL
                      HAVING count(*) > 1)""",
    "7_bar_count": f"""SELECT count(*) FROM (SELECT symbol, session, any_value(cas) AS cas,
                       count(*) AS n FROM five JOIN tf USING (session) GROUP BY symbol, session)
                       WHERE n > CASE WHEN cas THEN {MAX_BARS_CAS} ELSE {MAX_BARS_PRE_CAS} END""",
    "8_unexpected_session": """SELECT count(*) FROM (SELECT DISTINCT symbol, session FROM five) a
                               ANTI JOIN exp b USING (symbol, session)""",
    "9_auction": """SELECT (SELECT count(*) FROM five JOIN tf USING (session)
                            WHERE cas AND ((mod = 925 AND n_minutes <> 1) OR mod IN (915, 920)))
                         + (SELECT count(*) FROM five a JOIN tf USING (session)
                            JOIN (SELECT symbol, session, arg_max(close, timestamp) AS c FROM one
                                  WHERE mod IN (928, 929) GROUP BY ALL) b
                            USING (symbol, session) WHERE cas AND a.mod = 925 AND a.close <> b.c)""",
    "10_daily_rollup": f"""SELECT count(*) FROM
        (SELECT symbol, session, arg_min(open, timestamp) AS open, max(high) AS high,
                min(low) AS low, arg_max(close, timestamp) AS close, sum(volume) AS volume
         FROM five GROUP BY ALL) a
        FULL JOIN
        (SELECT symbol, session, arg_min(open, timestamp) AS open, max(high) AS high,
                min(low) AS low, arg_max(close, timestamp) AS close, sum(volume) AS volume
         FROM one GROUP BY ALL) b USING (symbol, session)
        WHERE a.open IS NULL OR b.open IS NULL OR a.open <> b.open OR a.high <> b.high
           OR a.low <> b.low OR a.close <> b.close OR {_VOL}""",
}


def validate(bars_1m: pd.DataFrame, bars_5m: pd.DataFrame, templates,
             expected_sessions: pd.DataFrame) -> dict[str, int]:
    """Section 8.2: violation counts per check; every count must be 0.

    expected_sessions: the (symbol, session) rows the 5m data may contain."""
    days = pd.to_datetime(pd.Series(pd.unique(bars_5m.session)))
    tf = pd.DataFrame({"session": days.dt.date,
                       "cas": [bool(ic.template_for(d.date(), templates).auction) for d in days]})
    exp = expected_sessions[["symbol", "session"]].assign(
        session=pd.to_datetime(expected_sessions.session).dt.date)
    con = duckdb.connect(":memory:")
    try:
        con.execute("SET TimeZone = 'UTC'")
        con.register("m1", bars_1m[["symbol", "timestamp", "open", "high", "low", "close",
                                    "volume"]])
        con.register("m5", bars_5m)
        con.register("tf", tf)
        con.register("exp", exp)
        for sql in (_ONE, _FIVE, _RE):
            con.execute(sql)
        return {name: int(con.execute(sql).fetchone()[0]) for name, sql in _CHECKS.items()}
    finally:
        con.close()


def bar_count_distribution(bars_5m: pd.DataFrame) -> pd.DataFrame:
    """Bars per session by year (reported, plan 8.2 item 7)."""
    n = bars_5m.groupby(["symbol", "session"]).size().rename("bars").reset_index()
    n["year"] = pd.to_datetime(n.session).dt.year
    return n.groupby("year").bars.describe(percentiles=[0.01, 0.05, 0.5]).round(1)
