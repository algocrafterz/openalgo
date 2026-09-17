#!/usr/bin/env python
"""Momentum-Rank Rebalance - Python-native, full NSE F&O universe.

Phase 1 of migrating momentum-rank off TradingView Pine. Pine's
request.security() call cap (40) limited the live indicator to 40 hardcoded
names; this script ranks the FULL NSE F&O universe (~200 names) instead,
matching the config validated by the 2026-09-15 fine-tuning grid search - see
signal_engine/pinescripts/swing/momentum-rank/STRATEGY-ANALYSIS.md.

WHAT IT DOES
    On each scheduled run, checks whether enough trading sessions have passed
    since the last rebalance (REBAL_DAYS). If so: ranks the universe by
    12-month return skipping the most recent month (the classic 12-1
    construction), picks the strongest TOP_N, diffs against the
    currently-held basket, and sends a human-readable digest to Telegram.
    There is no stop-loss or target - the exit is a name falling out of the
    ranking at the next rebalance.

    Run frequency is controlled entirely by the /python host's Schedule
    settings (strategy_configs.json's schedule_days/schedule_start/stop),
    NOT by anything in this file - this script has no way to see its own
    schedule. It is currently scheduled WEEKLY, not daily, which is plenty
    granular for a ~30-session (~6-week) rebalance cadence.

SAFETY - READ BEFORE WIRING UP TELEGRAM
    This script sends ONLY a human-readable digest message. It deliberately
    does NOT emit signal_engine/parser.py's "STRATEGY DIRECTION" per-symbol
    alert format, so nothing this script sends can be picked up and
    auto-executed by the live signal_engine Telegram listener. It posts to
    its OWN channel (MOMENTUM_TG_CHAT_ID, "positional-momentum-rank-live"),
    NOT signal_engine's live-trading chat. Read the digest and place CNC BUY/
    SELL orders yourself, equal weight across the Hold list.

    Status: candidate, paper-only. This has never been traded. See
    STRATEGY-ANALYSIS.md's honest limits before funding it with real size.
    Auto-execution (Phase 2) is a deliberately separate, not-yet-started
    change - it needs a new position-sizing mode in signal_engine's risk
    engine, since every existing strategy sizes off stop-loss distance and
    this one has no real stop.

ENVIRONMENT
    Credentials are read from signal_engine/.env (see _load_env_file()
    below), NOT from OS-inherited variables - the /python host only injects
    STRATEGY_ID/STRATEGY_NAME/OPENALGO_STRATEGY_EXCHANGE/OPENALGO_API_KEY/
    OPENALGO_HOST and inherits OpenAlgo's OWN top-level .env (see
    strategies/README.md), never signal_engine's separate .env file. An OS
    environment variable of the same name still wins if already set, so
    local CLI testing with exported vars works unchanged.

    OPENALGO_API_KEY        required - injected by the /python host
    HOST_SERVER             OpenAlgo REST host, from .env
    OPENALGO_STRATEGY_EXCHANGE / EXCHANGE   defaults to NSE
    BREAKINGTRADE_BOT_TOKEN  the Telegram bot token - SHARED with the
                            breakingtrade strategy (same bot, different
                            channels), from signal_engine/.env. Deliberately
                            not a separate MOMENTUM_TG_BOT_TOKEN - one bot,
                            no duplicated secret to keep in sync.
    MOMENTUM_TG_CHAT_ID     this strategy's OWN channel id
                            ("positional-momentum-rank-live"), from
                            signal_engine/.env
    MOMENTUM_STATE_PATH     where held-basket state persists between runs,
                            default log/strategies/momentum_rank_state.json
    MOMENTUM_DRY_RUN        "true" to compute + print only, no Telegram send,
                            no state write - equivalent to --dry-run

    NOTE: as of 2026-09-16, blueprints/python_strategy.py's /python upload
    form does not actually read/store custom per-strategy parameters despite
    strategies/README.md documenting that feature (checked: new_strategy()'s
    request.form.get calls are limited to strategy_name/exchange/schedule_*)
    - irrelevant now that credentials come from signal_engine/.env directly,
    but worth knowing if this script's design changes later.

SAFETY NOTE ON STATE
    State is only written after a digest is actually sent to Telegram. If
    the bot token/chat id aren't configured (or the send fails), the run
    computes and prints the basket but leaves state untouched - so a
    rebalance the trader never actually saw is never silently marked done,
    and the same rebalance is recomputed and retried on the next check
    instead of being skipped forever.

USAGE
    python momentum_rank_strategy.py --dry-run   # compute + print only
    python momentum_rank_strategy.py             # run the recheck loop

NOTE ON THIS FILE'S SHAPE
    OpenAlgo's /python Strategy Host accepts exactly ONE uploaded .py file
    (blueprints/python_strategy.py's new_strategy() takes a single
    request.files["strategy_file"]) - so the ranking logic that used to live
    in a separate momentum_rank_core.py is inlined below instead of imported.
    strategies/examples/tests/test_momentum_rank_core.py imports these same
    functions straight from this file.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from pathlib import Path

from openalgo import api

# ---------------------------------------------------------------------------
# Pure ranking logic (no I/O) - formerly momentum_rank_core.py, inlined here
# because the /python host only accepts a single uploaded file. MUST stay
# numerically identical to signal_engine/backtest/portfolio.py's
# build_factor(..., "mom_skip", ...) - see the tests directory's parity check.
# ---------------------------------------------------------------------------


def momentum_score(closes: list[float], lookback: int, skip: int) -> float | None:
    """12-1 momentum: close[skip sessions ago] / close[lookback sessions ago] - 1.

    `closes` must be sorted OLDEST -> NEWEST, with closes[-1] being the most
    recent session. Returns None if there isn't enough history yet.
    """
    if len(closes) <= lookback:
        return None
    recent = closes[-(skip + 1)]
    past = closes[-(lookback + 1)]
    if not past:
        return None
    return recent / past - 1.0


def rank_universe(scores: dict[str, float | None], prices: dict[str, float],
                   top_n: int, min_price: float) -> list[str]:
    """Highest score wins. Only names with a real score AND a price >= min_price
    are eligible - mirrors portfolio.py's PortfolioBacktest.run() `valid` mask."""
    eligible = [s for s, v in scores.items()
                if v is not None and prices.get(s, 0.0) >= min_price]
    eligible.sort(key=lambda s: scores[s], reverse=True)
    return eligible[:top_n]


def diff_basket(held: list[str], target: list[str]) -> tuple[list[str], list[str]]:
    """(sells, buys): the trades needed to move the book from `held` to `target`.

    Names in both lists need no action - this is what makes it a rotation
    rather than a full liquidate-and-rebuy each period.
    """
    held_set, target_set = set(held), set(target)
    sells = [s for s in held if s not in target_set]
    buys = [s for s in target if s not in held_set]
    return sells, buys


def sessions_elapsed(last_rebalance_date: date | None, trading_days: list[date]) -> int:
    """How many of `trading_days` are strictly after `last_rebalance_date`.

    `trading_days` comes from whatever dates the caller actually observed data
    for on a live history pull - not a separately-maintained holiday calendar -
    so this can never drift from what the broker actually traded.

    None (first-ever run) counts every observed day as elapsed, so the very
    first run always rebalances and establishes the initial basket.
    """
    if last_rebalance_date is None:
        return len(trading_days)
    return sum(1 for d in trading_days if d > last_rebalance_date)

# ---------------------------------------------------------------------------
# Universe: mirrors signal_engine/backtest/data.py:NSE_FNO, copied as a plain
# constant so this single uploaded file has no dependency on the
# signal_engine package inside the /python strategy-host subprocess. Refresh
# from data.py (or data.refresh_fno()) whenever NSE revises the F&O list.
#
# ONE deliberate exception: NIFTYFPI is in data.py's NSE_FNO but NOT here -
# this instance's own symbol master (database.token_db.get_token) has no
# NSE-equity mapping for it (confirmed live, 2026-09-16 dry run: "Symbol
# 'NIFTYFPI' not found for exchange 'NSE'"), so every fetch permanently
# fails. Not fixed in data.py - that constant is shared by other backtests
# and NIFTYFPI's tradeability there is a separate question from whether this
# LIVE script can ever buy it.
# ---------------------------------------------------------------------------
UNIVERSE = [
    "360ONE", "ABB", "ABCAPITAL", "ADANIENSOL", "ADANIENT", "ADANIGREEN", "ADANIPORTS",
    "ADANIPOWER", "ALKEM", "AMBER", "AMBUJACEM", "ANGELONE", "APLAPOLLO", "APOLLOHOSP",
    "ASHOKLEY", "ASIANPAINT", "ASTRAL", "AUBANK", "AUROPHARMA", "AXISBANK",
    "BAJAJ-AUTO", "BAJAJFINSV", "BAJAJHLDNG", "BAJFINANCE", "BANDHANBNK", "BANKBARODA",
    "BANKINDIA", "BDL", "BEL", "BHARATFORG", "BHARTIARTL", "BHEL", "BIOCON",
    "BLUESTARCO", "BOSCHLTD", "BPCL", "BRITANNIA", "BSE", "CAMS", "CANBK", "CDSL",
    "CGPOWER", "CHOLAFIN", "CIPLA", "COALINDIA", "COCHINSHIP", "COFORGE", "COLPAL",
    "CONCOR", "CROMPTON", "CUMMINSIND", "DABUR", "DELHIVERY", "DIVISLAB", "DIXON",
    "DLF", "DMART", "DRREDDY", "EICHERMOT", "ETERNAL", "FEDERALBNK", "FORCEMOT",
    "FORTIS", "GAIL", "GLENMARK", "GMRAIRPORT", "GODFRYPHLP", "GODREJCP", "GODREJPROP",
    "GRASIM", "GVT&D", "HAL", "HAVELLS", "HCLTECH", "HDFCAMC", "HDFCBANK", "HDFCLIFE",
    "HEROMOTOCO", "HINDALCO", "HINDPETRO", "HINDUNILVR", "HINDZINC", "HYUNDAI",
    "ICICIBANK", "ICICIGI", "ICICIPRULI", "IDEA", "IDFCFIRSTB", "IEX", "INDHOTEL",
    "INDIANB", "INDIGO", "INDUSINDBK", "INDUSTOWER", "INFY", "INOXWIND", "IOC",
    "IREDA", "IRFC", "ITC", "JINDALSTEL", "JIOFIN", "JSWENERGY", "JSWSTEEL",
    "JUBLFOOD", "KALYANKJIL", "KAYNES", "KEI", "KFINTECH", "KOTAKBANK", "KPITTECH",
    "LAURUSLABS", "LICHSGFIN", "LICI", "LODHA", "LT", "LTF", "LTM", "LUPIN", "M&M",
    "MANAPPURAM", "MANKIND", "MARICO", "MARUTI", "MAXHEALTH", "MAZDOCK", "MCX", "MFSL",
    "MOTHERSON", "MOTILALOFS", "MPHASIS", "MUTHOOTFIN", "NAM-INDIA", "NATIONALUM",
    "NAUKRI", "NBCC", "NESTLEIND", "NHPC", "NMDC", "NTPC", "NYKAA",
    "OBEROIRLTY", "OFSS", "OIL", "ONGC", "PAGEIND", "PATANJALI", "PAYTM", "PERSISTENT",
    "PETRONET", "PFC", "PGEL", "PHOENIXLTD", "PIDILITIND", "PIIND", "PNB",
    "PNBHOUSING", "POLICYBZR", "POLYCAB", "POWERGRID", "POWERINDIA", "PREMIERENE",
    "PRESTIGE", "RADICO", "RBLBANK", "RECLTD", "RELIANCE", "RVNL", "SAIL", "SBICARD",
    "SBILIFE", "SBIN", "SHREECEM", "SHRIRAMFIN", "SIEMENS", "SOLARINDS", "SONACOMS",
    "SRF", "SUNPHARMA", "SUPREMEIND", "SUZLON", "SWIGGY", "TATACONSUM", "TATAELXSI",
    "TATAPOWER", "TATASTEEL", "TCS", "TECHM", "TIINDIA", "TITAN", "TMPV", "TORNTPHARM",
    "TRENT", "TVSMOTOR", "ULTRACEMCO", "UNIONBANK", "MAHABANK", "ATHERENERG",
    "SAGILITY", "UNITDSPR", "UNOMINDA", "UPL", "VBL", "VEDL", "VMM", "VOLTAS",
    "WAAREEENER", "WIPRO", "YESBANK", "ZYDUSLIFE",
]

# ---------------------------------------------------------------------------
# Fine-tuned parameters (2026-09-15 full-universe grid search - see
# STRATEGY-ANALYSIS.md). lookback/skip in SESSIONS, matching the Pine script's
# own convention.
# ---------------------------------------------------------------------------
LOOKBACK = 300
SKIP = 21
TOP_N = 12
REBAL_DAYS = 30
MIN_PRICE = 20.0

# History pulled per symbol: LOOKBACK+SKIP sessions of buffer, converted to
# calendar days (~1.55x for weekends/holidays) plus slack.
_CALENDAR_DAYS_BACK = int((LOOKBACK + SKIP + 40) * 1.55)

# ---------------------------------------------------------------------------
# Every backtest number in STRATEGY-ANALYSIS.md was produced from SETTLED
# daily closes (signal_engine/backtest/data.py's from_historify_daily()).
# NSE cash closes at 15:30 IST. If this script is ever run intraday (its own
# host schedule cannot be seen from inside this file - see the module
# docstring), a broker's "today" daily bar can be a live, still-forming
# candle rather than the session's real close. Using it would silently feed
# a materially different kind of price than the backtest assumed, so any
# bar dated "today" (IST) is dropped until a buffer past close. This file
# stays a single self-contained upload (see the module docstring), so IST is
# redefined locally rather than imported from signal_engine/timeutils.py.
# ---------------------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))
_NSE_CLOSE_IST = dt_time(15, 30)
_EOD_SETTLE_BUFFER_MIN = 10


def _today_bar_is_incomplete(bar_date: date, now_ist: datetime) -> bool:
    """True if `bar_date` is today (IST) and the session may not have settled yet."""
    if bar_date != now_ist.date():
        return False
    cutoff = (datetime.combine(now_ist.date(), _NSE_CLOSE_IST, tzinfo=IST)
              + timedelta(minutes=_EOD_SETTLE_BUFFER_MIN))
    return now_ist < cutoff

# How often this process's OWN while-loop rechecks, IF the /python host keeps
# it running that long. In practice the host starts/stops this script on its
# own schedule (see strategy_configs.json), currently a ~20 min weekly
# window - well short of this constant - so this loop rarely gets to sleep
# and wake more than once per invocation. It only matters if the schedule is
# ever widened to a long-running window.
_POLL_SECONDS = 6 * 3600

# ---------------------------------------------------------------------------
# Environment: OS-injected vars (see strategies/README.md) take priority -
# that's how OPENALGO_API_KEY reaches this script, and it lets local CLI
# testing override anything via exported vars. Telegram credentials fall
# back to signal_engine/.env, which the /python host does NOT inherit (it
# only inherits OpenAlgo's own top-level .env).
# ---------------------------------------------------------------------------


def _load_env_file(env: dict[str, str]) -> None:
    """Parse signal_engine/.env into `env`, without overwriting keys already
    passed in. Walks up from this file's own location to find the repo root,
    so it works regardless of the process's cwd or which copy of this script
    is running (strategies/examples/ or the uploaded strategies/scripts/
    copy).
    """
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        candidate = parent / "signal_engine" / ".env"
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key and key not in env:
                    env[key] = value.strip()
            return
    print("[momentum-rank] signal_engine/.env not found - Telegram credentials "
          "must come from the OS environment instead")


_ENV_FILE: dict[str, str] = {}
_load_env_file(_ENV_FILE)


def _cfg(key: str, default: str = "") -> str:
    return os.getenv(key) or _ENV_FILE.get(key, default)


def _clean_bot_token(token: str) -> str:
    """Normalize a pasted bot token.

    The Telegram API path is /bot<TOKEN>/method, so BotFather's token is
    often copied WITH the "bot" prefix already attached - which produces
    /botbot123.../ and a bare 404 that looks exactly like an invalid token.
    Also strips quotes and any stray CR from a CRLF .env file. Mirrors
    signal_engine/analysis/breakingtrade/alerts.py's _clean_token() - same
    bug class, same fix, reimplemented here for this file's single-file
    constraint rather than imported.
    """
    if not token:
        return token
    token = token.strip().strip("\r").strip("\"'")
    if token.lower().startswith("bot") and ":" in token[3:]:
        token = token[3:]
    return token


API_KEY = _cfg("OPENALGO_API_KEY")
HOST = _cfg("HOST_SERVER") or _cfg("OPENALGO_HOST", "http://127.0.0.1:5000")
EXCHANGE = _cfg("OPENALGO_STRATEGY_EXCHANGE") or _cfg("EXCHANGE", "NSE")
# Shared with the breakingtrade strategy - same bot, different channels. See
# signal_engine/.env's comment above MOMENTUM_TG_CHAT_ID.
TG_BOT_TOKEN = _clean_bot_token(_cfg("BREAKINGTRADE_BOT_TOKEN"))
TG_CHAT_ID = _cfg("MOMENTUM_TG_CHAT_ID")
STATE_PATH = Path(_cfg("MOMENTUM_STATE_PATH",
                        "log/strategies/momentum_rank_state.json"))
DRY_RUN_ENV = _cfg("MOMENTUM_DRY_RUN", "").strip().lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Paper-trading ledger: tracks what actually following each digest would have
# earned, without placing real orders. Same shape and math as the standalone
# momentum_rank_paper_tracker.py (which stays the manual --report tool for
# checking P&L anytime) - MUST stay numerically identical to that file's
# initial_buy()/apply_rebalance()/summary(), duplicated here rather than
# imported for the same reason build_factor()/momentum_score() are: the
# /python host only accepts a single uploaded file.
#
# Fill price convention differs slightly, deliberately: the standalone
# tracker uses the next session's real open; this inline version uses the
# rebalance signal day's CLOSE (already fetched as `last_price` while
# ranking), since STRATEGY-ANALYSIS.md already measured that substitution to
# be a negligible difference (CAGR 37.67% -> 37.91% in that test) and it
# avoids needing a second, later run just to capture "tomorrow's" price.
# ---------------------------------------------------------------------------
PAPER_CAPITAL = float(_cfg("MOMENTUM_PAPER_CAPITAL", "100000"))
PAPER_LEDGER_PATH = Path(_cfg("MOMENTUM_PAPER_LEDGER_PATH",
                              "log/strategies/momentum_rank_paper_ledger.json"))
# Weekly, not daily or monthly: this is a positional strategy with no
# intraday exit - nothing actionable happens between rebalances (~30
# sessions, ~6 weeks apart), so a daily P&L ping would be pure noise for a
# book that cannot move on any daily decision. Monthly would mean only 1-2
# check-ins per rebalance cycle - too sparse to see a trend. Weekly matches
# this script's own existing check cadence and gives ~4-5 data points per
# cycle. Tracked here (not just "whatever the host schedule happens to run
# at") so the cadence is a property of the script itself and stays correct
# even if the host schedule is later changed - the same lesson as the
# earlier "checked once daily" text bug not matching the real schedule.
PAPER_CHECKIN_INTERVAL_DAYS = int(_cfg("MOMENTUM_PAPER_CHECKIN_DAYS", "7"))


def _load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"held": [], "last_rebalance_date": None, "rebalance_count": 0}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def _new_paper_ledger(capital: float = PAPER_CAPITAL) -> dict:
    return {"capital": capital, "cash": capital, "positions": {},
            "closed_trades": [], "last_synced_rebalance_count": 0,
            "last_checkin_date": None}


def _load_paper_ledger() -> dict:
    if PAPER_LEDGER_PATH.exists():
        return json.loads(PAPER_LEDGER_PATH.read_text())
    return _new_paper_ledger()


def _save_paper_ledger(ledger: dict) -> None:
    PAPER_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    PAPER_LEDGER_PATH.write_text(json.dumps(ledger, indent=2))


def _paper_allocate_equal_weight(
    candidates: list[str], slot_target: float, cash: float,
    fill_prices: dict[str, float],
) -> tuple[dict[str, dict], float, list[str]]:
    """Spend an equal `slot_target` on each of `candidates`, whole shares only.

    Mirrors momentum_rank_paper_tracker.py's _allocate_equal_weight() exactly - these two
    implementations are duplicated (not imported) because the /python host only accepts a
    single uploaded file (see the module comment above), but must never numerically diverge;
    see test_matches_standalone_paper_tracker_numerically.

    2026-09-17: POWERINDIA at Rs30,435 against an Rs8,333 (capital/TOP_N) slot bought 0
    shares - a stock priced above its slot allocation used to still get a "position" entry
    with qty=0, silently converting that entire slot into idle cash while cluttering the
    ledger (inflating open_positions in every later report) with a holding of nothing.

    Fix: a candidate that can't afford even one share at slot_target is skipped - no
    position is opened for it - and its target is redistributed, split equally, across the
    OTHER candidates in this same batch, iterating until every remaining candidate is
    affordable or none are left. `cash` is not floored or capped: a buy is sized off the
    slot's fair share of current total book value, the same pre-existing model that can
    size a buy larger than currently-idle cash covers.

    Returns (fills, cash_remaining, skipped) where fills is {symbol: {"qty", "price"}}.
    """
    if not candidates or slot_target <= 0:
        return {}, cash, list(candidates)

    remaining = list(candidates)
    skipped: list[str] = []
    pool = slot_target * len(candidates)
    while remaining:
        target = pool / len(remaining)
        unaffordable = [
            s for s in remaining if fill_prices.get(s, 0) <= 0 or fill_prices[s] > target
        ]
        if not unaffordable:
            break
        for s in unaffordable:
            remaining.remove(s)
        skipped.extend(unaffordable)

    fills: dict[str, dict] = {}
    if remaining:
        target = pool / len(remaining)
        for sym in remaining:
            price = fill_prices[sym]
            qty = int(target // price)
            if qty == 0:
                skipped.append(sym)
                continue
            cash -= qty * price
            fills[sym] = {"qty": qty, "price": price}

    for sym in skipped:
        price = fill_prices.get(sym, 0.0)
        print(f"[momentum-rank] paper-ledger WARNING: {sym} @ Rs {price:,.2f} exceeds "
              f"even its redistributed equal-weight share (target Rs {slot_target:,.2f}"
              f"/slot) - skipped, no position opened. Cash retained for the next rebalance.")

    return fills, cash, skipped


def _paper_initial_buy(ledger: dict, as_of: str, target: list[str],
                       fill_prices: dict[str, float], top_n: int = TOP_N) -> dict:
    per_slot = ledger["cash"] / top_n
    fills, cash, _skipped = _paper_allocate_equal_weight(
        target, per_slot, ledger["cash"], fill_prices)
    positions = dict(ledger["positions"])
    for sym, fill in fills.items():
        positions[sym] = {"qty": fill["qty"], "entry_price": fill["price"], "entry_date": as_of}
    return {**ledger, "positions": positions, "cash": cash}


def _paper_apply_rebalance(ledger: dict, as_of: str, sells: list[str],
                           buys: list[str], fill_prices: dict[str, float],
                           top_n: int = TOP_N) -> dict:
    positions = dict(ledger["positions"])
    closed = list(ledger["closed_trades"])
    cash = ledger["cash"]
    for sym in sells:
        pos = positions.pop(sym)
        exit_price = fill_prices[sym]
        proceeds = pos["qty"] * exit_price
        cash += proceeds
        cost_basis = pos["qty"] * pos["entry_price"]
        pnl = proceeds - cost_basis
        closed.append({
            "symbol": sym, "qty": pos["qty"], "entry_date": pos["entry_date"],
            "entry_price": pos["entry_price"], "exit_date": as_of,
            "exit_price": exit_price, "pnl": pnl,
            "pnl_pct": (pnl / cost_basis * 100.0) if cost_basis else 0.0,
        })
    remaining_value = sum(pos["qty"] * fill_prices.get(sym, pos["entry_price"])
                          for sym, pos in positions.items())
    per_slot = (cash + remaining_value) / top_n
    fills, cash, _skipped = _paper_allocate_equal_weight(buys, per_slot, cash, fill_prices)
    for sym, fill in fills.items():
        positions[sym] = {"qty": fill["qty"], "entry_price": fill["price"], "entry_date": as_of}
    return {**ledger, "positions": positions, "cash": cash, "closed_trades": closed}


def _paper_summary(ledger: dict, current_prices: dict[str, float]) -> dict:
    market_value = sum(pos["qty"] * current_prices.get(sym, pos["entry_price"])
                       for sym, pos in ledger["positions"].items())
    total = ledger["cash"] + market_value
    realized = sum(t["pnl"] for t in ledger["closed_trades"])
    unrealized = sum(
        pos["qty"] * (current_prices.get(sym, pos["entry_price"]) - pos["entry_price"])
        for sym, pos in ledger["positions"].items())
    return {
        "total_value": total, "realized_pnl": realized, "unrealized_pnl": unrealized,
        "total_return_pct": (total - ledger["capital"]) / ledger["capital"] * 100.0,
        "open_positions": len(ledger["positions"]),
        "closed_trades": len(ledger["closed_trades"]),
    }


def _paper_summary_lines(ledger: dict, current_prices: dict[str, float]) -> list[str]:
    """Very concise paper-P&L block appended to the digest - positions and
    P&L, not the full per-symbol breakdown (that's what --report is for)."""
    s = _paper_summary(ledger, current_prices)
    return [
        "-" * 44,
        f"PAPER PORTFOLIO (Rs {ledger['capital']:,.0f} tracked since first "
        f"rebalance):",
        f"  Value: Rs {s['total_value']:,.0f} ({s['total_return_pct']:+.2f}%) | "
        f"Realized: Rs {s['realized_pnl']:+,.0f} | "
        f"Unrealized: Rs {s['unrealized_pnl']:+,.0f}",
        f"  Positions: {s['open_positions']} open, {s['closed_trades']} closed "
        f"(see momentum_rank_paper_tracker.py --report for per-symbol detail)",
    ]


def _paper_checkin_message(ledger: dict, current_prices: dict[str, float],
                           as_of, elapsed: int) -> str:
    """Weekly (see PAPER_CHECKIN_INTERVAL_DAYS) position + P&L snapshot, sent
    on a NON-rebalance check so a trader isn't only hearing from this
    strategy once every ~6 weeks. Unlike `_paper_summary_lines()` (a few
    lines appended to a rebalance digest), this includes the full per-symbol
    quantity and P&L breakdown - the point of a check-in is exactly that
    detail.
    """
    s = _paper_summary(ledger, current_prices)
    lines = [
        f"MOMENTUM-RANK PAPER CHECK-IN - {as_of}",
        "=" * 44,
        f"Not a rebalance day ({elapsed}/{REBAL_DAYS} sessions since last "
        f"rebalance) - this is a position/P&L snapshot only.",
        "",
        f"Capital: Rs {ledger['capital']:,.0f}  |  "
        f"Value: Rs {s['total_value']:,.0f} ({s['total_return_pct']:+.2f}%)",
        f"Realized: Rs {s['realized_pnl']:+,.0f}  |  "
        f"Unrealized: Rs {s['unrealized_pnl']:+,.0f}  |  Cash: Rs {ledger['cash']:,.0f}",
        f"Positions: {s['open_positions']} open, {s['closed_trades']} closed",
        "-" * 44,
    ]
    for sym, pos in sorted(ledger["positions"].items()):
        cur = current_prices.get(sym, pos["entry_price"])
        pnl_pct = ((cur - pos["entry_price"]) / pos["entry_price"] * 100.0
                  if pos["entry_price"] else 0.0)
        lines.append(f"  {sym}: qty {pos['qty']} @ entry Rs {pos['entry_price']:,.2f} "
                    f"-> cur Rs {cur:,.2f}  ({pnl_pct:+.2f}%)")
    lines += [
        "-" * 44,
        "This is a PAPER-TRACKING CHECK-IN ONLY - no order has been placed.",
    ]
    return "\n".join(lines)


def _maybe_send_paper_checkin(as_of: str, elapsed: int,
                              current_prices: dict[str, float],
                              dry_run: bool) -> None:
    """Sends a paper P&L check-in on a non-rebalance run, at most once every
    PAPER_CHECKIN_INTERVAL_DAYS (see that constant for why weekly). Silent
    no-op if no paper position has ever been opened yet - nothing to report.
    """
    ledger = _load_paper_ledger()
    if not ledger["positions"]:
        return
    last_checkin = ledger.get("last_checkin_date")
    if last_checkin:
        days_since = (date.fromisoformat(as_of) - date.fromisoformat(last_checkin)).days
        if days_since < PAPER_CHECKIN_INTERVAL_DAYS:
            print(f"[momentum-rank] paper check-in not due yet "
                  f"({days_since}/{PAPER_CHECKIN_INTERVAL_DAYS} days since last)")
            return
    message = _paper_checkin_message(ledger, current_prices, as_of, elapsed)
    print(message)
    if dry_run:
        print("[momentum-rank] --dry-run: not sending paper check-in, not "
              "updating ledger")
        return
    if _send_telegram(message):
        ledger["last_checkin_date"] = as_of
        _save_paper_ledger(ledger)
    else:
        print("[momentum-rank] paper check-in not delivered - "
              "last_checkin_date left unchanged, will retry next check")


def _send_telegram(text: str) -> bool:
    """Returns True only if the digest was actually delivered."""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("[momentum-rank] BREAKINGTRADE_BOT_TOKEN/MOMENTUM_TG_CHAT_ID not set "
              "(checked OS environment and signal_engine/.env) - digest printed "
              "above only, nothing sent")
        return False
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": TG_CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(url, data=payload,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return True
    except urllib.error.URLError as e:
        print(f"[momentum-rank] Telegram send failed: {e}")
        return False


def _section(label: str, symbols: list[str]) -> list[str]:
    """One symbol per line, alphabetical - no score, no rank order. This
    strategy was validated buying the FULL top_n basket (see
    STRATEGY-ANALYSIS.md's concentration backtest: top-3/top-5 subsets
    UNDERPERFORMED the full basket, including losing to the benchmark
    out-of-sample). Showing a score or a best-to-worst order here would
    invite exactly the partial-basket cherry-picking that data argues
    against, so every name is presented as equally mandatory.
    """
    if not symbols:
        return [f"{label} (0): none"]
    return [f"{label} ({len(symbols)}):"] + [f"  {s}" for s in sorted(symbols)]


def _build_digest(trading_days: list, target: list[str], sells: list[str],
                   buys: list[str], rebalance_number: int,
                   fetch_errors: int = 0, universe_size: int = 0,
                   today_truncated: int = 0) -> str:
    """The full rotation notification: what to do, and when the next one is
    expected. `trading_days` is the full observed window (oldest -> newest),
    used both as the "as of" date and to estimate the next rebalance date
    from the actual calendar-days-per-session ratio in this data, since
    REBAL_DAYS counts trading sessions, not calendar days.

    `fetch_errors`/`universe_size` surface real data-quality gaps (broker
    outage, corrupt ticks) - the ranking may be missing eligible names.
    `today_truncated` is a separate, non-alarming note: symbols whose
    still-forming "today" bar was excluded because this ran before the
    session settled - expected on a pre-close check, not a data problem.
    """
    as_of = trading_days[-1]
    if len(trading_days) > 1:
        span_days = (trading_days[-1] - trading_days[0]).days
        cal_days_per_session = span_days / (len(trading_days) - 1)
    else:
        cal_days_per_session = 365.0 / 252.0  # NSE sessions/year fallback
    next_estimate = as_of + timedelta(days=round(cal_days_per_session * REBAL_DAYS))
    held_unchanged = [s for s in target if s not in buys]
    per_slot_pct = 100.0 / TOP_N
    action_line = (
        f"ACTION REQUIRED: place CNC MARKET orders, equal weight "
        f"(~{per_slot_pct:.1f}% of capital each)"
        if sells or buys else
        "NO ACTION NEEDED: basket unchanged this rebalance"
    )

    lines = [
        f"MOMENTUM-RANK REBALANCE #{rebalance_number} - {as_of}",
        "=" * 44,
        action_line,
        "Buy ALL names below, equal weight - this strategy was tested as a "
        "full basket. Buying only a few underperformed badly in backtest, "
        "see STRATEGY-ANALYSIS.md.",
        "",
        *_section("SELL", sells),
        "",
        *_section("BUY", buys),
        "",
        *_section("HOLD, no action", held_unchanged),
        "-" * 44,
        f"Cadence: rebalances every {REBAL_DAYS} trading sessions (~6 weeks)",
        "Next check: per this strategy's schedule (currently weekly, not "
        "daily) - a check is not a rebalance, see below",
        f"Next rebalance expected: around {next_estimate} "
        "(estimate - depends on trading sessions elapsed, not the calendar)",
    ]
    if fetch_errors:
        lines += [
            "-" * 44,
            f"Data note: {fetch_errors}/{universe_size} universe symbols "
            "excluded this run (fetch failure or bad broker data) - the "
            "ranking above may be missing eligible names.",
        ]
    if today_truncated:
        lines += [
            "-" * 44,
            f"Pre-settlement note: {today_truncated}/{universe_size} symbols' "
            "still-forming today bar was excluded - ranking uses each "
            "symbol's last fully settled session, not a live/partial price.",
        ]
    lines += [
        "-" * 44,
        "This is a DIGEST ONLY - no order has been placed automatically.",
        "Status: candidate strategy, paper-tracked - see this strategy's "
        "STRATEGY-ANALYSIS.md before sizing up.",
    ]
    return "\n".join(lines)


# Retries: one extra attempt after the first failure. A deterministic
# broker-side bug (e.g. GVT&D/M&M's "jData is not valid json object",
# observed live 2026-09-16) will fail identically both times and still end
# up excluded - a retry only helps the genuinely transient case (rate limit,
# momentary network blip), and costs one extra rate-limited call at worst.
_FETCH_ATTEMPTS = 2
_FETCH_RETRY_DELAY_SECONDS = 1.5


def _fetch_symbol_history(client, sym: str, start: str, end: str,
                          attempts: int = _FETCH_ATTEMPTS,
                          delay: float = _FETCH_RETRY_DELAY_SECONDS):
    """One symbol's raw history, retrying failures up to `attempts` times.

    Returns (df, error_message). `df` is None with no error for "fetched
    fine, just no rows" (e.g. a brand-new listing with no history yet) -
    that is not retried, since trying again cannot produce data that does
    not exist. `error_message` is set only after every attempt failed.
    """
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            df = client.history(symbol=sym, exchange=EXCHANGE, interval="D",
                                 start_date=start, end_date=end)
        except Exception as e:
            last_error = str(e)
        else:
            # client.history() returns a DataFrame on success, or an error
            # dict on failure (bad symbol, broker/session issue, rate limit,
            # etc.) - per its own documented contract. A dict has no .empty
            # attribute, so checking that first (instead of isinstance
            # DataFrame) avoids importing pandas just for this.
            if isinstance(df, dict):
                last_error = str(df.get("message", df))
            elif df is None or df.empty or "close" not in df.columns:
                return None, None
            else:
                return df, None
        if attempt < attempts:
            time.sleep(delay)
    return None, last_error


def fetch_universe_closes(
    client, now_ist: datetime | None = None,
) -> tuple[dict[str, list[float]], list, dict[str, float], int, int]:
    """Daily closes for the full universe, oldest -> newest.

    Returns (closes_by_symbol, sorted_trading_days_observed, last_close_by_symbol,
    error_count, today_truncated_count). `trading_days` is derived from what the
    broker feed actually returned, not a separately-maintained calendar, so it
    can never drift from live reality.

    `error_count` is symbols DROPPED this run (fetch failure, broker error, or
    corrupt data) - a real data-quality problem, surfaced as a "Data note" in
    the digest. `today_truncated_count` is symbols whose still-forming "today"
    bar was trimmed off, keeping the symbol itself - expected, correct
    behaviour when this runs before the settle cutoff, NOT a data-quality
    problem, so it is reported separately and must never be added to
    `error_count` (doing so would make an ordinary intraday run look like a
    broken-data run to the trader).

    `now_ist` is injectable for tests; defaults to the real current time.
    """
    now_ist = now_ist or datetime.now(IST)
    start = (datetime.now() - timedelta(days=_CALENDAR_DAYS_BACK)).strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")
    closes: dict[str, list[float]] = {}
    last_price: dict[str, float] = {}
    trading_days: set = set()
    errors = 0
    today_truncated = 0
    for sym in UNIVERSE:
        df, error = _fetch_symbol_history(client, sym, start, end)
        if error:
            print(f"[momentum-rank] {sym}: history fetch failed after "
                  f"{_FETCH_ATTEMPTS} attempt(s) - {error}")
            errors += 1
            continue
        if df is None:
            continue
        df = df.sort_index()
        close_col = df["close"]
        if (close_col <= 0).any():
            # A non-positive close is broker/feed corruption, not a real price -
            # dividing by it (or by it as the lookback anchor) would silently
            # produce a nonsense momentum ratio that could rank a garbage
            # symbol at the top. Drop the whole symbol for this run rather
            # than trust a partially-corrupt series.
            print(f"[momentum-rank] {sym}: non-positive close in history - "
                  f"excluding this run, likely bad broker/feed data")
            errors += 1
            continue
        dates = [d.date() if hasattr(d, "date") else d for d in df.index]
        if dates and _today_bar_is_incomplete(dates[-1], now_ist):
            # The backtest that validated this strategy only ever used
            # SETTLED closes. A bar dated today, fetched before 15:30 IST
            # (+ settle buffer), may still be forming - drop it rather than
            # rank off a price that can still move before the real close.
            # Expected/correct, not a data-quality problem - counted
            # separately from `errors`.
            dates = dates[:-1]
            close_col = close_col.iloc[:-1]
            today_truncated += 1
            if close_col.empty:
                continue
        closes[sym] = close_col.tolist()
        last_price[sym] = float(close_col.iloc[-1])
        trading_days.update(dates)
    if errors:
        print(f"[momentum-rank] {errors}/{len(UNIVERSE)} symbols failed to fetch "
              f"or had corrupt data - proceeding with the remaining {len(closes)}")
    if today_truncated:
        print(f"[momentum-rank] {today_truncated}/{len(UNIVERSE)} symbols had "
              f"today's still-forming bar excluded (pre-settlement run) - "
              f"ranking uses each symbol's last fully settled session")
    return closes, sorted(trading_days), last_price, errors, today_truncated


def run_once(dry_run: bool = False) -> None:
    state = _load_state()
    held = state["held"]
    last_rebalance = (
        datetime.strptime(state["last_rebalance_date"], "%Y-%m-%d").date()
        if state["last_rebalance_date"] else None)

    client = api(api_key=API_KEY, host=HOST)
    closes, trading_days, last_price, fetch_errors, today_truncated = (
        fetch_universe_closes(client))
    if not trading_days:
        print("[momentum-rank] no data returned for any symbol - aborting this run")
        return

    elapsed = sessions_elapsed(last_rebalance, trading_days)
    print(f"[momentum-rank] {trading_days[-1]}: {elapsed}/{REBAL_DAYS} sessions "
          f"since last rebalance ({last_rebalance})")
    if elapsed < REBAL_DAYS:
        _maybe_send_paper_checkin(str(trading_days[-1]), elapsed, last_price, dry_run)
        return

    scores = {s: momentum_score(c, LOOKBACK, SKIP) for s, c in closes.items()}
    usable = {s: v for s, v in scores.items() if v is not None}
    if len(usable) < TOP_N:
        print(f"[momentum-rank] only {len(usable)} of {len(UNIVERSE)} names have "
              f"{LOOKBACK}+ sessions of history - skipping this rebalance, will "
              f"retry next check")
        return

    target = rank_universe(usable, last_price, TOP_N, MIN_PRICE)
    # Mirrors portfolio.py's PortfolioBacktest.run(): only ever trade a FULL
    # top_n basket. The backtest that validated this strategy never held a
    # partial book, so a smaller live basket (e.g. because the min-price
    # filter removed some of the score-eligible names) would silently run an
    # untested, more-concentrated, partly-uninvested portfolio at the same
    # per-slot weight. Skip and retry rather than ship that.
    if len(target) < TOP_N:
        print(f"[momentum-rank] only {len(target)} of {TOP_N} names pass the "
              f"₹{MIN_PRICE:.0f} min-price filter this rebalance - skipping "
              f"to avoid trading an under-tested partial basket, will retry "
              f"next check")
        return

    sells, buys = diff_basket(held, target)
    rebalance_number = int(state.get("rebalance_count", 0)) + 1

    digest = _build_digest(trading_days, target, sells, buys, rebalance_number,
                            fetch_errors, len(UNIVERSE),
                            today_truncated=today_truncated)

    paper_ledger = _load_paper_ledger()
    as_of = str(trading_days[-1])
    fill_prices: dict[str, float] = {}
    for sym in set(target) | set(sells):
        if sym in last_price:
            fill_prices[sym] = last_price[sym]
        else:
            # target/buys always have a last_price (rank_universe's min-price
            # eligibility check requires it) - this can only happen for a
            # `sell` whose fresh price failed to fetch this run. Fall back to
            # its own last recorded entry price: P&L-neutral on this trade
            # rather than crashing the whole rebalance over one stale price.
            fallback = paper_ledger["positions"].get(sym, {}).get("entry_price")
            if fallback is not None:
                print(f"[momentum-rank] paper-ledger: no fresh price for {sym} "
                      f"this run - using its last recorded entry price "
                      f"Rs {fallback:.2f} as a placeholder fill")
                fill_prices[sym] = fallback
    if not paper_ledger["positions"] and not paper_ledger["closed_trades"]:
        paper_ledger = _paper_initial_buy(paper_ledger, as_of, target, fill_prices)
    else:
        paper_ledger = _paper_apply_rebalance(paper_ledger, as_of, sells, buys,
                                              fill_prices)
    digest += "\n" + "\n".join(_paper_summary_lines(paper_ledger, last_price))
    print(digest)

    if dry_run:
        print("[momentum-rank] --dry-run: not sending Telegram, not updating "
              "state, not updating paper ledger")
        return

    if _send_telegram(digest):
        _save_state({"held": target, "last_rebalance_date": as_of,
                     "rebalance_count": rebalance_number})
        paper_ledger["last_synced_rebalance_count"] = rebalance_number
        _save_paper_ledger(paper_ledger)
    else:
        print("[momentum-rank] digest not delivered - state and paper ledger "
              "left unchanged, this rebalance will be recomputed and retried "
              "on the next check")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                         help="compute and print the basket/diff without sending "
                              "Telegram or writing state")
    args = parser.parse_args()

    # The /python host always launches this script with no CLI args of its
    # own, so MOMENTUM_DRY_RUN (settable as an upload-time parameter) is the
    # only way to run a dry-run test through the host's Start/Schedule flow.
    dry_run = args.dry_run or DRY_RUN_ENV

    if not API_KEY:
        print("Error: OPENALGO_API_KEY environment variable not set")
        sys.exit(1)

    if dry_run:
        print("[momentum-rank] DRY RUN - no Telegram send, no state write, "
              "running once then exiting")
        run_once(dry_run=True)
        return

    print(f"[momentum-rank] started {datetime.now()}, rechecking every "
          f"{_POLL_SECONDS // 3600}h while this process stays up - actual "
          f"cadence follows the /python host's Schedule (currently weekly, "
          f"not daily) (rebalance every {REBAL_DAYS} sessions, top {TOP_N} "
          f"of {len(UNIVERSE)} names)")
    while True:
        try:
            run_once(dry_run=False)
        except KeyboardInterrupt:
            print("[momentum-rank] stopped")
            break
        except Exception as e:
            print(f"[momentum-rank] error: {e}")
        time.sleep(_POLL_SECONDS)


if __name__ == "__main__":
    main()
