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
    "DABUR.NS", "DALBHARAT.NS", "DELHIVERY.NS", "DIVISLAB.NS", "DIXON.NS", "DLF.NS",
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
         refresh: bool = False, min_bars: int = 500) -> dict[str, pd.DataFrame]:
    """Download and cache OHLCV. Symbols that return too little data are skipped."""
    import yfinance as yf

    symbols = symbols or NSE_LIQUID
    CACHE.mkdir(parents=True, exist_ok=True)
    out: dict[str, pd.DataFrame] = {}
    for s in symbols:
        f = CACHE / f"{s.replace('.', '_')}_{interval}_{period}.parquet"
        if f.exists() and not refresh:
            df = pd.read_parquet(f)
        else:
            df = yf.download(s, period=period, interval=interval,
                             progress=False, auto_adjust=False)
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
