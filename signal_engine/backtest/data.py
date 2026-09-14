"""Bar data for backtests, with an on-disk cache.

yfinance is the default source because it needs no credentials, but it caps intraday
history hard: 5-minute bars reach back 60 days, 1-minute only 7. That cap is the
binding constraint on every intraday result produced here - roughly 59 sessions - and
it is why conclusions lean on a BASKET of symbols and an out-of-sample split rather
than on one symbol's total.

For longer history, load from OpenAlgo's own historify.duckdb via `from_openalgo`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pandas as pd

CACHE = Path(os.environ.get("BACKTEST_CACHE", Path(__file__).parent / ".cache"))

#: NSE F&O stock universe - the correct backtest population for an intraday system.
#: These are the names that actually carry intraday liquidity, MIS leverage and tight
#: spreads, so a result here transfers to what you would really trade. A hand-picked
#: "liquid large caps" list is a selection choice made before seeing any data, and it
#: quietly decides the answer.
#:
#: Generated from THIS instance's symbol master, so it tracks NSE's own list:
#:     SELECT DISTINCT symbol FROM symtoken
#:      WHERE exchange='NFO' AND instrumenttype LIKE '%FUT%'
#: then strip the expiry suffix and drop indices. Refresh with refresh_fno() below
#: whenever NSE revises the F&O list (it is reviewed periodically).
NSE_FNO = [
    "360ONE.NS", "ABB.NS", "ABCAPITAL.NS", "ADANIENSOL.NS", "ADANIENT.NS", "ADANIGREEN.NS",
    "ADANIPORTS.NS", "ADANIPOWER.NS", "ALKEM.NS", "AMBER.NS", "AMBUJACEM.NS", "ANGELONE.NS",
    "APLAPOLLO.NS", "APOLLOHOSP.NS", "ASHOKLEY.NS", "ASIANPAINT.NS", "ASTRAL.NS",
    "AUBANK.NS", "AUROPHARMA.NS", "AXISBANK.NS", "BAJAJ-AUTO.NS", "BAJAJFINSV.NS",
    "BAJAJHLDNG.NS", "BAJFINANCE.NS", "BANDHANBNK.NS", "BANKBARODA.NS", "BANKINDIA.NS",
    "BDL.NS", "BEL.NS", "BHARATFORG.NS", "BHARTIARTL.NS", "BHEL.NS", "BIOCON.NS",
    "BLUESTARCO.NS", "BOSCHLTD.NS", "BPCL.NS", "BRITANNIA.NS", "BSE.NS", "CAMS.NS",
    "CANBK.NS", "CDSL.NS", "CGPOWER.NS", "CHOLAFIN.NS", "CIPLA.NS", "COALINDIA.NS",
    "COCHINSHIP.NS", "COFORGE.NS", "COLPAL.NS", "CONCOR.NS", "CROMPTON.NS", "CUMMINSIND.NS",
    "DABUR.NS", "DELHIVERY.NS", "DIVISLAB.NS", "DIXON.NS", "DLF.NS",
    "DMART.NS", "DRREDDY.NS", "EICHERMOT.NS", "ETERNAL.NS", "FEDERALBNK.NS", "FORCEMOT.NS",
    "FORTIS.NS", "GAIL.NS", "GLENMARK.NS", "GMRAIRPORT.NS", "GODFRYPHLP.NS", "GODREJCP.NS",
    "GODREJPROP.NS", "GRASIM.NS", "GVT&D.NS", "HAL.NS", "HAVELLS.NS", "HCLTECH.NS",
    "HDFCAMC.NS", "HDFCBANK.NS", "HDFCLIFE.NS", "HEROMOTOCO.NS", "HINDALCO.NS",
    "HINDPETRO.NS", "HINDUNILVR.NS", "HINDZINC.NS", "HYUNDAI.NS", "ICICIBANK.NS",
    "ICICIGI.NS", "ICICIPRULI.NS", "IDEA.NS", "IDFCFIRSTB.NS", "IEX.NS", "INDHOTEL.NS",
    "INDIANB.NS", "INDIGO.NS", "INDUSINDBK.NS", "INDUSTOWER.NS", "INFY.NS", "INOXWIND.NS",
    "IOC.NS", "IREDA.NS", "IRFC.NS", "ITC.NS", "JINDALSTEL.NS", "JIOFIN.NS", "JSWENERGY.NS",
    "JSWSTEEL.NS", "JUBLFOOD.NS", "KALYANKJIL.NS", "KAYNES.NS", "KEI.NS", "KFINTECH.NS",
    "KOTAKBANK.NS", "KPITTECH.NS", "LAURUSLABS.NS", "LICHSGFIN.NS", "LICI.NS", "LODHA.NS",
    "LT.NS", "LTF.NS", "LTM.NS", "LUPIN.NS", "M&M.NS", "MANAPPURAM.NS", "MANKIND.NS",
    "MARICO.NS", "MARUTI.NS", "MAXHEALTH.NS", "MAZDOCK.NS", "MCX.NS", "MFSL.NS",
    "MOTHERSON.NS", "MOTILALOFS.NS", "MPHASIS.NS", "MUTHOOTFIN.NS", "NAM-INDIA.NS",
    "NATIONALUM.NS", "NAUKRI.NS", "NBCC.NS", "NESTLEIND.NS", "NHPC.NS", "NIFTYFPI.NS",
    "NMDC.NS", "NTPC.NS", "NYKAA.NS", "OBEROIRLTY.NS", "OFSS.NS", "OIL.NS", "ONGC.NS",
    "PAGEIND.NS", "PATANJALI.NS", "PAYTM.NS", "PERSISTENT.NS", "PETRONET.NS", "PFC.NS",
    "PGEL.NS", "PHOENIXLTD.NS", "PIDILITIND.NS", "PIIND.NS", "PNB.NS", "PNBHOUSING.NS",
    "POLICYBZR.NS", "POLYCAB.NS", "POWERGRID.NS", "POWERINDIA.NS", "PREMIERENE.NS",
    "PRESTIGE.NS", "RADICO.NS", "RBLBANK.NS", "RECLTD.NS", "RELIANCE.NS", "RVNL.NS",
    "SAIL.NS", "SBICARD.NS", "SBILIFE.NS", "SBIN.NS", "SHREECEM.NS", "SHRIRAMFIN.NS",
    "SIEMENS.NS", "SOLARINDS.NS", "SONACOMS.NS", "SRF.NS", "SUNPHARMA.NS", "SUPREMEIND.NS",
    "SUZLON.NS", "SWIGGY.NS", "TATACONSUM.NS", "TATAELXSI.NS", "TATAPOWER.NS",
    "TATASTEEL.NS", "TCS.NS", "TECHM.NS", "TIINDIA.NS", "TITAN.NS", "TMPV.NS",
    "TORNTPHARM.NS", "TRENT.NS", "TVSMOTOR.NS", "ULTRACEMCO.NS", "UNIONBANK.NS",
    "MAHABANK.NS", "ATHERENERG.NS", "SAGILITY.NS",
    "UNITDSPR.NS", "UNOMINDA.NS", "UPL.NS", "VBL.NS", "VEDL.NS", "VMM.NS", "VOLTAS.NS",
    "WAAREEENER.NS", "WIPRO.NS", "YESBANK.NS", "ZYDUSLIFE.NS",
]

#: Small cross-sector subset, for a quick check while iterating. Never report from it -
#: 30 symbols is not enough to separate a result from a run of luck.
NSE_SAMPLE = [
    "TCS.NS", "INFY.NS", "HCLTECH.NS", "RELIANCE.NS", "ONGC.NS", "COALINDIA.NS",
    "HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS", "AXISBANK.NS", "TATASTEEL.NS",
    "JSWSTEEL.NS", "HINDALCO.NS", "MARUTI.NS", "ITC.NS", "SUNPHARMA.NS",
    "BHARTIARTL.NS", "LT.NS", "ADANIENT.NS", "BAJFINANCE.NS",
]

#: Default universe for reports.
NSE_LIQUID = NSE_FNO


def refresh_fno(db_path: str = "db/openalgo.db") -> list[str]:
    """Re-read the F&O underlyings from the local symbol master.

    Returns yfinance tickers. Paste the result over NSE_FNO when NSE revises its list.
    """
    import re
    import sqlite3
    con = sqlite3.connect(db_path)
    try:
        rows = [r[0] for r in con.execute(
            "SELECT DISTINCT symbol FROM symtoken "
            "WHERE exchange='NFO' AND instrumenttype LIKE '%FUT%'")]
    finally:
        con.close()
    indices = {"BANKNIFTY", "NIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "BANKEX", "SENSEX"}
    und = sorted({re.sub(r"\d{2}[A-Z]{3}\d{2}FUT$", "", s) for s in rows})
    return [f"{u}.NS" for u in und
            if u and u not in indices and re.fullmatch(r"[A-Z0-9&\-]+", u)]


def load(symbols=None, period: str = "60d", interval: str = "5m",
         refresh: bool = False, min_bars: int = 500,
         auto_adjust: bool = False) -> dict[str, pd.DataFrame]:
    """Download and cache OHLCV. Symbols that return too little data are skipped.

    `auto_adjust` is False for intraday work, where the window is short enough that a
    corporate action is unlikely and raw prices match what was actually tradeable. Set
    it True for multi-year DAILY history: unadjusted prices carry every split and bonus
    as a price jump, and a 1:5 split reads as a -80% gap. Any strategy keying on gaps or
    on percentage returns will otherwise trade dozens of corporate actions that never
    happened.
    """
    import yfinance as yf

    symbols = symbols or NSE_LIQUID
    CACHE.mkdir(parents=True, exist_ok=True)
    out: dict[str, pd.DataFrame] = {}
    for s in symbols:
        adj = "_adj" if auto_adjust else ""
        f = CACHE / f"{s.replace('.', '_')}_{interval}_{period}{adj}.parquet"
        if f.exists() and not refresh:
            df = pd.read_parquet(f)
        else:
            df = yf.download(s, period=period, interval=interval,
                             progress=False, auto_adjust=auto_adjust)
            if df is None or df.empty:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df[["Open", "High", "Low", "Close", "Volume"]]
            df.to_parquet(f)
        if len(df) >= min_bars:
            out[s] = df
    if not out:
        raise RuntimeError("no usable data - check symbols, network, or the interval cap")
    return out


def from_openalgo(symbols, exchange="NSE", interval="5m", start=None, end=None) -> dict[str, pd.DataFrame]:
    """Bars from the running OpenAlgo instance, for history beyond yfinance's cap.

    Requires OPENALGO_API_KEY and a reachable host; returns the same
    {symbol: OHLCV DataFrame} shape as load().
    """
    from openalgo import api  # imported lazily: optional path

    client = api(api_key=os.environ["OPENALGO_API_KEY"],
                 host=os.environ.get("OPENALGO_HOST", "http://127.0.0.1:5000"))
    out = {}
    for s in symbols:
        df = client.history(symbol=s, exchange=exchange, interval=interval,
                            start_date=start, end_date=end)
        if df is None or len(df) == 0:
            continue
        df = df.rename(columns=str.capitalize)
        out[s] = df[["Open", "High", "Low", "Close", "Volume"]]
    return out


def sessions(frames: dict[str, pd.DataFrame]) -> list:
    return sorted({d for df in frames.values() for d in pd.Series(df.index.date).unique()})


# ---------------------------------------------------------------------------
# OpenAlgo's own historical store
# ---------------------------------------------------------------------------

#: Where the Historify DuckDB lives, relative to the repo root.
HISTORIFY_DB = Path(os.environ.get("HISTORIFY_DB", "db/historify.duckdb"))

#: NSE continuous session. Bars outside this are broker-feed artefacts (pre-open
#: crossings, post-close corrections, and in one case a whole second download made
#: against a different timezone) and are dropped rather than traded.
SESSION_OPEN, SESSION_CLOSE = "09:15", "15:29"

#: An overnight close-to-open ratio beyond this is a corporate action, not a gap.
#: Historify stores RAW broker prices - a 1:10 split reads as a -90% overnight move
#: and any strategy keying on the previous close would trade it as the gap of a
#: lifetime. 1:2 is the smallest split NSE does, so 0.75/1.33 catches every one while
#: leaving real gaps (the largest honest overnight move in the sample is ~25%) alone.
SPLIT_LO, SPLIT_HI = 0.75, 1.33


def _freq(interval: str) -> str:
    """yfinance-style interval -> a pandas offset alias.

    Not cosmetic: pandas reads "5m" as FIVE MONTHS, so resampling with the interval
    string straight from the CLI silently produces a handful of monthly bars instead
    of 5-minute ones, and every downstream number is quietly about a different thing.
    """
    m = re.fullmatch(r"(\d+)\s*([mhd])", interval.strip().lower())
    if not m:
        raise ValueError(f"unsupported interval {interval!r}; use e.g. 1m, 5m, 15m, 1h, 1d")
    n, unit = m.group(1), m.group(2)
    return f"{n}{ {'m': 'min', 'h': 'h', 'd': 'D'}[unit] }"


def historify_symbols(interval: str = "1m", db_path=None) -> list[str]:
    """Symbols the local store actually holds at this interval, longest history first."""
    import duckdb
    con = duckdb.connect(str(db_path or HISTORIFY_DB), read_only=True)
    try:
        return [r[0] for r in con.execute(
            "SELECT symbol FROM market_data WHERE interval = ? "
            "GROUP BY symbol ORDER BY count(*) DESC", [interval]).fetchall()]
    finally:
        con.close()


def _downcast(df: pd.DataFrame) -> pd.DataFrame:
    """Shrink OHLCV to the smallest safe dtype.

    from_historify()/from_historify_daily() hold every requested symbol's frame in
    memory for the life of the run, so this is a real, load-bearing reduction rather
    than cosmetic: float64->float32 halves the four price columns, and volume never
    approaches uint32's ~4.3B cap for a single NSE bar. No precision loss that matters
    at paise tick sizes - this is one of the fixes for the full-universe OOM crash.
    """
    for col in ("Open", "High", "Low", "Close"):
        df[col] = df[col].astype("float32")
    df["Volume"] = df["Volume"].clip(lower=0).astype("uint32")
    return df


def _clean_session(df: pd.DataFrame) -> pd.DataFrame:
    """Drop everything that is not a tradeable NSE bar.

    Three defects are present in the store and each one silently corrupts a result:
    bars outside market hours (a strategy's session anchor lands on a 03:00 bar),
    duplicate timestamps from a re-run download job (one session counted twice), and
    bars whose high/low do not bracket the open/close (an impossible bar that will
    trigger a stop that never existed).
    """
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df.between_time(SESSION_OPEN, SESSION_CLOSE)
    o, h, lo_, c = df["Open"], df["High"], df["Low"], df["Close"]
    sane = (h >= lo_) & (h >= o) & (h >= c) & (lo_ <= o) & (lo_ <= c) & (lo_ > 0)
    return df[sane]


def split_days(df: pd.DataFrame) -> set:
    """Sessions whose OPEN follows a corporate action, on raw unadjusted prices.

    Intraday strategies reset every session, so a split between two days does not
    corrupt anything computed inside a day. It corrupts exactly the cross-day
    features - previous close, overnight gap, previous day's high/low - so the days
    are returned for a strategy to skip rather than the prices being rewritten.
    """
    daily = df["Close"].resample("1D").last().dropna()
    opens = df["Open"].resample("1D").first().dropna()
    ratio = opens / daily.shift(1)
    hit = ratio[(ratio < SPLIT_LO) | (ratio > SPLIT_HI)]
    return {ts.date() for ts in hit.index}


def from_historify(symbols=None, interval: str = "5m", start=None, end=None,
                   min_sessions: int = 250, db_path=None,
                   drop_split_days: bool = True) -> dict[str, pd.DataFrame]:
    """Bars from OpenAlgo's Historify store, resampled from stored 1-minute data.

    This is the loader to prefer over `load()` for anything reported. yfinance caps
    5-minute history at 60 days - about 59 sessions - which is far too short to
    separate an intraday edge from a run of luck, and it is the reason a backtest run
    against it can only ever be indicative. Historify holds years of 1-minute bars
    pulled from the broker's own feed, so a 5-minute study runs on roughly 25x the
    sample and on the prices that actually traded.

    Prices are RAW - the store keeps what the broker sent. `drop_split_days` removes
    the session after each corporate action so a cross-day feature cannot read a split
    as a gap; within-day logic is unaffected either way.
    """
    import duckdb

    con = duckdb.connect(str(db_path or HISTORIFY_DB), read_only=True)
    try:
        symbols = symbols or historify_symbols("1m", db_path)
        out: dict[str, pd.DataFrame] = {}
        for s in symbols:
            q = ("SELECT timestamp, open, high, low, close, volume FROM market_data "
                 "WHERE symbol = ? AND interval = '1m'")
            args: list = [s]
            if start:
                q += " AND timestamp >= ?"
                args.append(int(pd.Timestamp(start, tz="Asia/Kolkata").timestamp()))
            if end:
                q += " AND timestamp <= ?"
                args.append(int(pd.Timestamp(end, tz="Asia/Kolkata").timestamp()))
            raw = con.execute(q + " ORDER BY timestamp", args).df()
            if raw.empty:
                continue
            raw.index = (pd.to_datetime(raw.pop("timestamp"), unit="s", utc=True)
                         .dt.tz_convert("Asia/Kolkata"))
            raw.columns = ["Open", "High", "Low", "Close", "Volume"]
            df = _clean_session(raw)
            if df.empty:
                continue

            if interval != "1m":
                # label='left' so a bar is stamped with the minute it OPENS, matching
                # both yfinance and the live feed. closed='left' keeps 09:15 .. 09:19
                # in the 09:15 bar.
                #
                # Single pass, not a per-day groupby - `origin` pinned to a 09:15
                # timestamp makes every day's bucket grid start at 09:15 (5/15/etc.
                # minutes all divide 24h evenly, so the grid never drifts across
                # days), and `_clean_session` has already dropped every bar outside
                # the 09:15-15:29 session, so there is no overnight data for a bucket
                # to straddle - the per-day groupby produced byte-identical output in
                # a 10-year, 895k-row verification and cost 36-106x longer. That cost
                # was the real driver of the OOM this replaces: ~2,500 trading days x
                # 214 symbols is ~535k tiny per-day resample calls, and glibc's
                # allocator does not hand the resulting fragmentation back to the OS,
                # so RSS ratcheted upward for the whole run regardless of how much
                # data was actually live.
                agg = {"Open": "first", "High": "max", "Low": "min",
                       "Close": "last", "Volume": "sum"}
                freq = _freq(interval)
                origin = df.index[0].normalize() + pd.Timedelta(hours=9, minutes=15)
                df = (df.resample(freq, label="left", closed="left", origin=origin)
                        .agg(agg).dropna())
            if drop_split_days:
                bad = split_days(df)
                if bad:
                    df = df[~pd.Series(df.index.date, index=df.index).isin(bad).to_numpy()]
            if pd.Series(df.index.date).nunique() >= min_sessions:
                out[s] = _downcast(df)
        if not out:
            raise RuntimeError(
                f"no symbol in {db_path or HISTORIFY_DB} has {min_sessions}+ sessions "
                f"at {interval} - download history in the Historify UI first")
        return out
    finally:
        con.close()


def _split_adjust_daily(df: pd.DataFrame) -> pd.DataFrame:
    """Back-adjust raw daily O/H/L/C onto the CURRENT share count.

    Historify stores whatever the broker's daily candle actually was that day - no
    adjustment, so a stock's own multi-year series carries every split and bonus as a
    price cliff. A strategy computing a 252-day return, an RSI(2), or a trailing
    high/low across that boundary sees a fabricated -80% move, which is worse than
    losing the history entirely because it looks like real data.

    The fix is the standard back-adjustment: walk splits from the MOST RECENT
    backward, and multiply every bar strictly BEFORE each split by that split's ratio,
    so the whole series reads on today's share count. Bars from AND after a split need
    no adjustment - they are already on the modern scale. This is the same convention
    `data.load(..., auto_adjust=True)` gets from yfinance, so swapping one source for
    the other must not silently change what a lookback indicator sees.

    Volume is left as reported. These strategies use it in a local, rolling sense (an
    N-day average gate), which already re-bases itself across a split; forcing it onto
    the adjusted scale as well would help nothing and risks getting the direction of
    the correction backwards.
    """
    o, c = df["Open"], df["Close"]
    ratio = o / c.shift(1)
    is_split = (ratio < SPLIT_LO) | (ratio > SPLIT_HI)
    if not is_split.any():
        return df

    per_bar_ratio = pd.Series(1.0, index=df.index)
    per_bar_ratio[is_split] = ratio[is_split]
    # cum[t] = product of every ratio from t to the end; shifting one step earlier
    # drops ratio[t] itself; a split ON day t rescales days BEFORE t, not day t.
    cum_from_t = per_bar_ratio[::-1].cumprod()[::-1]
    factor = cum_from_t.shift(-1).fillna(1.0)

    out = df.copy()
    for col in ("Open", "High", "Low", "Close"):
        out[col] = out[col] * factor
    return out


def from_historify_daily(symbols=None, start=None, end=None, min_sessions: int = 500,
                         db_path=None) -> dict[str, pd.DataFrame]:
    """Split-adjusted daily bars from Historify, for the swing engine.

    The alternative to `data.load(..., interval="1d", auto_adjust=True)`: same shape,
    same adjustment guarantee, but sourced from the broker feed already in this
    instance rather than yfinance - no per-symbol rate limit, and one fewer external
    dependency in the loop. Historify's daily coverage reaches further back than its
    1-minute coverage (the broker retains small daily candles far longer than the full
    tick-level minute series), so this is usually the DEEPEST panel available here.
    """
    import duckdb

    con = duckdb.connect(str(db_path or HISTORIFY_DB), read_only=True)
    try:
        symbols = symbols or historify_symbols("D", db_path)
        out: dict[str, pd.DataFrame] = {}
        for s in symbols:
            q = ("SELECT timestamp, open, high, low, close, volume FROM market_data "
                 "WHERE symbol = ? AND interval = 'D'")
            args: list = [s]
            if start:
                q += " AND timestamp >= ?"
                args.append(int(pd.Timestamp(start, tz="Asia/Kolkata").timestamp()))
            if end:
                q += " AND timestamp <= ?"
                args.append(int(pd.Timestamp(end, tz="Asia/Kolkata").timestamp()))
            raw = con.execute(q + " ORDER BY timestamp", args).df()
            if raw.empty or len(raw) < min_sessions:
                continue
            raw.index = (pd.to_datetime(raw.pop("timestamp"), unit="s", utc=True)
                         .dt.tz_convert("Asia/Kolkata").dt.normalize())
            raw.columns = ["Open", "High", "Low", "Close", "Volume"]
            raw = raw[~raw.index.duplicated(keep="last")].sort_index()
            sane = ((raw["High"] >= raw["Low"]) & (raw["High"] >= raw["Open"]) &
                    (raw["High"] >= raw["Close"]) & (raw["Low"] <= raw["Open"]) &
                    (raw["Low"] <= raw["Close"]) & (raw["Low"] > 0))
            raw = raw[sane]
            if len(raw) < min_sessions:
                continue
            out[s] = _downcast(_split_adjust_daily(raw))
        if not out:
            raise RuntimeError(
                f"no symbol in {db_path or HISTORIFY_DB} has {min_sessions}+ daily "
                f"sessions - run backfill.py --interval D first")
        return out
    finally:
        con.close()
