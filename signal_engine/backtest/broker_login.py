"""Standalone login and depth probe for a SECOND broker, for backtest data only.

WHY THIS EXISTS

Flattrade's 1-minute history is capped at roughly 12-13 months (measured, see
`backfill.py`'s module docstring). If you hold an account at a broker with a deeper
API - Angel One and mStock are both already OpenAlgo plugins - it can fill that gap.

The one constraint that shapes this whole file: `database.auth_db.upsert_auth` is
keyed by USERNAME ALONE, not by (username, broker). OpenAlgo is single-broker per
instance - see CLAUDE.md's Security and Deployment Model - so logging a second broker
in through the NORMAL web flow overwrites the one stored session and drops whatever
this instance is live-trading on. THIS SCRIPT NEVER CALLS `upsert_auth`. It logs in
directly against the broker's own auth endpoint, keeps the resulting token in this
process's memory only, and hands it straight to `backfill.backfill(..., session=...)`.
Nothing it does is visible to, or reversible by, the live trading session.

CREDENTIALS

Read from `signal_engine/backtest/.secondary_broker.env` (git-ignored - see
.gitignore), NEVER from the repo's main `.env` or `signal_engine/.env`, which hold the
LIVE broker's credentials under the same generic names (`BROKER_API_KEY` etc.) that
these broker plugins expect. Mixing the two files would mean typing a second broker's
password into the file the live trading session reads from.

    # signal_engine/backtest/.secondary_broker.env
    SECOND_BROKER=mstock                  # or: angel

    # --- mstock ---
    MSTOCK_CLIENT_CODE=...                # your mStock login / client code
    MSTOCK_PASSWORD=...
    MSTOCK_TOTP_SECRET=...                # base32 seed from 2FA setup, NOT a 6-digit code

    # --- angel ---
    ANGEL_API_KEY=...                     # app key from smartapi.angelbroking.com
    ANGEL_CLIENT_CODE=...
    ANGEL_PIN=...
    ANGEL_TOTP_SECRET=...

A TOTP secret is the base32 SEED an authenticator app was given when 2FA was set up -
usually shown once, as a QR code / "can't scan? use this key" string. If it was never
saved, the only way to get it is to re-run that broker's 2FA setup, which is a real
account-security action to take deliberately, not a routine one.

USAGE

    # 1. authenticate and report how far back 1-minute data actually goes - no writes
    uv run python -m signal_engine.backtest.broker_login --probe

    # 2. if the probe shows real depth, backfill the F&O universe from it
    uv run python -m signal_engine.backtest.broker_login --backfill --start 2016-01-01
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from utils.logging import get_logger

logger = get_logger(__name__)

ENV_PATH = Path(__file__).parent / ".secondary_broker.env"

#: Same shape as the Flattrade probe in the 2026-09-12 session: a handful of
#: historical weeks, oldest first, so a run can be stopped as soon as one returns
#: nothing - anything further back will too.
PROBE_OFFSETS_DAYS = (30, 90, 180, 365, 545, 730, 1095, 1460, 1825, 2555, 3650)


def _load_env() -> None:
    if not ENV_PATH.exists():
        raise RuntimeError(
            f"{ENV_PATH} not found. Create it with this second broker's credentials - "
            f"see the module docstring for the exact keys - before running this script.")
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH, override=True)


#: What each broker's login actually needs, keyed to the exact names this file reads.
#: Angel and mStock's OWN OpenAlgo login pages ask for the same three account fields
#: (clientcode, pin/password, a live TOTP code) plus the app-level key from their
#: developer portal - a `BROKER_API_KEY`/`BROKER_API_SECRET` pair alone (the generic
#: shape OpenAlgo's main .env uses) is not enough: neither plugin's login function
#: takes a secret, and neither takes clientcode/pin from anywhere but here.
REQUIRED_KEYS = {
    "mstock": ("MSTOCK_CLIENT_CODE", "MSTOCK_PASSWORD", "MSTOCK_TOTP_SECRET"),
    "angel": ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET"),
}


def _require(keys: tuple[str, ...], broker: str) -> None:
    missing = [k for k in keys if not os.environ.get(k, "").strip()]
    if missing:
        raise RuntimeError(
            f"{ENV_PATH} is missing {', '.join(missing)} for SECOND_BROKER={broker!r}. "
            f"A BROKER_API_KEY/BROKER_API_SECRET pair (the generic app credentials "
            f"OpenAlgo's main .env uses) is not enough on its own - {broker}'s login "
            f"also needs the account fields its own OpenAlgo login page asks for "
            f"(clientcode, {'PIN' if broker == 'angel' else 'password'}, a TOTP code) "
            f"plus, for TOTP, the base32 SEED an authenticator app was given during "
            f"2FA setup - not a live 6-digit code. See this module's docstring for "
            f"the exact key names.")


def login() -> tuple[str, str | None, str]:
    """Authenticate directly with the broker named in .secondary_broker.env.

    Returns (auth_token, feed_token, broker_name). Never touches `auth_db`.
    """
    import pyotp

    _load_env()
    broker = os.environ.get("SECOND_BROKER", "").strip().lower()
    if broker not in REQUIRED_KEYS:
        raise RuntimeError(
            f"SECOND_BROKER={broker!r} in {ENV_PATH} - expected 'mstock' or 'angel'")
    _require(REQUIRED_KEYS[broker], broker)

    if broker == "mstock":
        from broker.mstock.api.auth_api import authenticate_with_totp
        os.environ["BROKER_API_KEY"] = os.environ["MSTOCK_CLIENT_CODE"]
        totp = pyotp.TOTP(os.environ["MSTOCK_TOTP_SECRET"]).now()
        auth_token, feed_token, error = authenticate_with_totp(
            os.environ["MSTOCK_PASSWORD"], totp)
    else:
        from broker.angel.api.auth_api import authenticate_broker
        os.environ["BROKER_API_KEY"] = os.environ["ANGEL_API_KEY"]
        totp = pyotp.TOTP(os.environ["ANGEL_TOTP_SECRET"]).now()
        auth_token, feed_token, error = authenticate_broker(
            os.environ["ANGEL_CLIENT_CODE"], os.environ["ANGEL_PIN"], totp)

    if error or not auth_token:
        raise RuntimeError(f"{broker} login failed: {error or 'no token returned'}")
    logger.info(f"logged into {broker} for backtest data only (not stored in auth_db)")
    return auth_token, feed_token, broker


#: Flattrade's own depth turned out to vary unpredictably by symbol - present at 120
#: days back, ABSENT 150-240 days back, present again intermittently 270-360 days
#: back (see PRD.md, 2026-09-13). One symbol's probe is not evidence; several are.
DEFAULT_PROBE_SYMBOLS = ("RELIANCE", "TCS", "SBIN", "TATASTEEL", "HDFCBANK")


def probe_depth(session: tuple[str, str | None, str], symbols=DEFAULT_PROBE_SYMBOLS,
                exchange: str = "NSE", interval: str = "1m") -> list[dict]:
    """How far back this session actually has data, oldest offset that still works.

    Mirrors the bisection done against Flattrade: request a short window at each
    offset, across SEVERAL symbols, and report bar counts, so a genuine depth
    advantage is visible - and so is any symbol-specific gap - before committing to a
    multi-hour backfill.
    """
    from services.history_service import get_history_with_auth

    auth, feed, broker = session
    today = dt.date.today()
    rows = []
    for symbol in symbols:
        for days_back in PROBE_OFFSETS_DAYS:
            d0 = today - dt.timedelta(days=days_back)
            try:
                ok, resp, _ = get_history_with_auth(
                    auth, feed, broker, symbol, exchange, interval,
                    str(d0), str(d0 + dt.timedelta(days=6)))
                data = resp.get("data") if ok and isinstance(resp, dict) else None
                n = len(data) if data else 0
            except Exception as exc:
                n = 0
                logger.debug(f"probe {symbol} {days_back}d back failed: {exc}")
            rows.append({"symbol": symbol, "days_back": days_back,
                        "week_of": str(d0), "bars": n})
    return rows


def main() -> None:
    import argparse

    import pandas as pd

    from signal_engine.backtest import data
    from signal_engine.backtest.backfill import COMPARE_TOLERANCE_PCT, backfill

    ap = argparse.ArgumentParser(prog="signal_engine.backtest.broker_login")
    ap.add_argument("--probe", action="store_true",
                    help="login and report 1m history depth only - no writes (default)")
    ap.add_argument("--backfill", action="store_true",
                    help="after a successful probe, backfill the F&O universe")
    ap.add_argument("--symbols", default=None,
                    help="comma-separated; default the full F&O universe for --backfill, "
                         "or a 5-symbol spread (RELIANCE, TCS, SBIN, TATASTEEL, "
                         "HDFCBANK) for --probe")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--interval", default="1m", choices=["1m", "D"])
    ap.add_argument("--pause", type=float, default=0.4,
                    help="seconds between chunks. Flattrade needs the 0.4s default; "
                         "Angel already rate-limits itself internally (see its data.py "
                         "module docstring), so 0.05-0.1 is safe and much faster there.")
    ap.add_argument("--no-compare", action="store_true",
                    help="skip reading existing bars before overwriting them. Default "
                         "is to compare and log every disagreement beyond "
                         "backfill.COMPARE_TOLERANCE_PCT - the write still happens "
                         "either way, this only controls whether it's audited.")
    args = ap.parse_args()

    session = login()
    print(f"logged into {session[2]} - token held in this process only, "
          f"auth_db untouched\n")

    # Capped at 5 regardless of how many --symbols were passed for the backfill
    # itself: probing is a quick sanity check before a run that can be hundreds of
    # symbols, not a second full pass. Missing this cap once meant a 47-symbol
    # --symbols resume triggered 517 probe calls (47 x 11 offsets) - ~5 minutes
    # spent re-confirming depth on symbols about to be backfilled anyway.
    probe_symbols = ([s.strip().upper() for s in args.symbols.split(",")][:5]
                     if args.symbols else DEFAULT_PROBE_SYMBOLS)
    report = probe_depth(session, symbols=probe_symbols, interval=args.interval)
    df = pd.DataFrame(report)
    print(f"=== depth probe: {', '.join(probe_symbols)} @ {args.interval} ===")
    print(df.to_string(index=False))
    by_symbol = df[df.bars > 0].groupby("symbol").days_back.max()
    print("\nfurthest offset that returned bars, per symbol:")
    for sym in probe_symbols:
        d = int(by_symbol.get(sym, 0))
        print(f"  {sym:12s} {d:>5} days (~{d / 365.25:.1f} years)")
    deepest_with_data = int(by_symbol.max()) if len(by_symbol) else 0
    print("\nFlattrade's own ceiling was ~365-380 days, with a real gap 150-240 days "
          "back on some symbols (see PRD.md 2026-09-13) - compare against that, not "
          "against zero, before deciding this is worth a full backfill.")

    if not args.backfill:
        return
    if deepest_with_data < 400:
        print("\nNo material depth advantage over Flattrade found at the probed "
              "symbol - stopping before a backfill that would mostly waste calls. "
              "Re-run with --backfill anyway if you want to proceed regardless.")
        return

    syms = ([s.strip().upper() for s in args.symbols.split(",")] if args.symbols
            else [s.replace(".NS", "") for s in data.NSE_FNO])
    print(f"\n=== backfilling {len(syms)} symbols {args.start}..{args.end or 'now'} "
          f"via {session[2]} (pause={args.pause}s, compare={not args.no_compare}) ===")
    results = backfill(syms, args.start, args.end, interval=args.interval,
                       session=session, pause=args.pause,
                       compare_existing=not args.no_compare)
    out = pd.DataFrame([vars(r) for r in results])
    print(out.to_string(index=False))
    if not args.no_compare:
        total_compared = int(out["compared"].sum())
        total_mismatch = int(out["mismatches"].sum())
        worst = out["max_diff_pct"].max()
        print("\n=== agreement with the existing store ===")
        print(f"{total_compared} overlapping bars checked, {total_mismatch} disagreed "
              f"by more than {COMPARE_TOLERANCE_PCT}% "
              f"({100 * total_mismatch / total_compared:.3f}% of overlap)"
              if total_compared else "no overlapping bars existed to compare against")
        if total_mismatch:
            print(f"worst single disagreement: {worst:.2f}% - {session[2]}'s value "
                  f"now replaces the old one everywhere they disagreed")


if __name__ == "__main__":
    main()
