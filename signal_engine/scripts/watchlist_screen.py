"""Multi-factor screen for the shared ORB/BREAKOUT/EMA9VWAP watchlist.

Runs the screen used to build the watchlist by hand (see
signal_engine/pinescripts/intraday/ema9-vwap/STRATEGY-LOG.md, 2026-09-12/13 entries).
Checks FOUR factors, split into two speeds - see COMPUTE VS DECIDE below:

    1. Liquidity   - median daily traded value >= Rs 100cr (not market cap: a stock
                     can have a huge market cap and still trade thin - traded VALUE is
                     what determines whether an order actually fills near the quote).
    2. Volatility  - median ATR% of price >= 0.15% (does the stock move enough to
                     reach a target before the day ends).
    3. RVOL        - last 5 trading days' average volume >= 0.8x the preceding 30-day
                     baseline. Liquidity and ATR% are HISTORICAL averages; a stock can
                     clear both while its current activity has already cooled off from
                     whatever catalyst got it there. RVOL is the one factor that asks
                     "is this still happening now" - and because it is a 5-DAY window,
                     it is also the one factor that moves meaningfully from one day to
                     the next, which is why it is checked DAILY, not weekly.
    4. Beta        - 1.0-2.0 vs NIFTY, computed on ~1y of daily closes. Below 1.0 is
                     too sluggish for intraday; above 2.0 tends to be erratic small-cap
                     risk that is hard to size for. Distinct from ATR%: a stock can be
                     volatile in absolute terms (high ATR%) while barely correlated to
                     the market (low beta) if its moves are purely idiosyncratic - one
                     of this screen's own picks (a newly-listed name) scored #1 on ATR%
                     and 0.60 on beta. Built from a YEAR of history, so one more day of
                     data changes it by a rounding error - recomputing it daily buys
                     nothing and costs a real, separate network fetch per symbol.

    Hard exclusion, independent of all four factors above: any symbol currently under
    NSE ASM (Additional Surveillance Measure) or GSM (Graded Surveillance Measure).
    Some ASM stages carry 100% margin (kills MIS leverage) or block intraday trading
    outright - a name can pass every technical filter and still be untradeable via
    MIS, and the broker will refuse the order anyway. Checked live against NSE's own
    reportASM/reportGSM endpoints every run, not a static list. Better to never offer
    it as a candidate than to let a strategy signal fire on it and rely on the
    broker's own rejection.

COMPUTE VS DECIDE - why this is not simply "weekly" or simply "daily"
    The first version of this screen computed everything (all four factors) and
    notified on the same cadence, and picking ONE cadence for both was the mistake:
    liquidity/ATR%/RVOL barely cost anything beyond the 60-day data pull already
    needed, so there is no reason not to run them daily - and running them daily gives
    a much richer, DAILY-resolution read on which stocks are showing up consistently
    (DAILY_WINDOW=10 trading days of data instead of 4 weekly snapshots). Beta is
    the opposite: it is expensive (a full year of DAILY bars fetched PER SYMBOL,
    across ~150 survivors) and nearly static day to day, so recomputing it daily would
    spend real time and network calls to move a number that has not meaningfully
    changed. So: `run_daily_scan()` (factors 1-3 + ASM/GSM) runs on EVERY startup and
    only appends to a rolling history - it does not notify anyone and does not fetch
    beta. `run_weekly_screen()` runs on top of that history once a week: it fetches
    beta (the one genuinely slow-changing, genuinely expensive part), builds the final
    Top-N, and is the only thing that sends a Telegram message and rewrites the
    watchlist file. The result: fresher, denser data feeding the decision, without a
    daily beta refetch or a daily message asking for a manual TradingView update.

CONVICTION TRACKING
    Every daily scan's qualifying pool (factors 1-3 + not under surveillance) is kept
    in a rolling window of the last DAILY_WINDOW trading days. Each candidate in the
    weekly digest is annotated with how many of those days it cleared the daily bar -
    "8/10" is a stock that has been consistently active for two weeks; "1/10" showed
    up today and has no track record yet. This is reported, not filtered on: a first
    appearance is still useful information, just weaker than a repeated one.

WHY IT ONLY UPDATES A FILE AND SENDS A MESSAGE - IT NEVER TOUCHES TRADINGVIEW
    TradingView's watchlist is edited by a human, by design (no public API for it). The
    weekly step's job ends at making the new candidate list visible: it overwrites the
    repo's watchlist file (so the repo stays the source of truth) and posts to the
    signal-engine Telegram channel (notify_channel) so the update is seen without
    opening a terminal. Updating TradingView itself stays a manual, deliberate step.

WHY THIS RUNS FROM STARTUP, NOT A FIXED CRON TIME
    A fixed cron hour assumes the machine is on then, which a laptop is not guaranteed
    to be. `maybe_run_weekly_screen()` is called from `openalgoscheduler._run_startup()`
    as its LAST step, on EVERY startup: the daily scan inside it runs unconditionally
    (cheap, idempotent per calendar day), and the weekly finalize/notify runs only when
    due. Being last means neither part can delay the broker-auth/trading-readiness
    steps ahead of it. The Telegram send is sequential with the earlier startup
    notification (same process, one after another), not concurrent, so it does not
    race the live listener for their shared Telethon session
    (signal_engine/data/telegram) the way two independent processes touching it at
    once would (see analysis/breakingtrade/alerts.py's module docstring for that risk).

USAGE
    Normal operation: nothing to run by hand - openalgoscheduler.py's startup flow
    calls maybe_run_weekly_screen() automatically on every startup.

    Manual / testing:
        uv run --group analysis python -m signal_engine.scripts.watchlist_screen
        uv run --group analysis python -m signal_engine.scripts.watchlist_screen --dry-run
        uv run --group analysis python -m signal_engine.scripts.watchlist_screen --force

    --force bypasses the "already finalized this week" gate (the daily scan always
    runs regardless). --dry-run additionally skips writing the file, sending Telegram,
    and updating the weekly gate (the daily history is still recorded for real).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WATCHLIST_FILE = (
    REPO_ROOT
    / "signal_engine/pinescripts/intraday/intraday-stocks-watchlist-tradingview"
)
#: Runtime state, not repo content - signal_engine/data/ is fully gitignored (same
#: directory the Telethon session and other small state files already live in).
STATE_FILE = REPO_ROOT / "signal_engine/data/watchlist_screen_state.json"

#: Screen thresholds. Keep in sync with the STRATEGY-LOG.md entry that justifies them -
#: a change here without a matching log entry is a change nobody can audit later.
MIN_DAILY_VALUE_CR = 100.0
MIN_ATR_PCT = 0.15
MIN_BETA = 1.0
MAX_BETA = 2.0
MIN_RVOL = 0.8
TOP_N = 20
#: How many trading days of daily-scan history conviction looks back over. 10 is two
#: trading weeks - enough resolution to separate a fluke from a trend, short enough
#: that an old regime is not remembered for a full quarter.
DAILY_WINDOW = 10
#: config.yaml blacklist._global and strategy_profiles hard blocks - excluded regardless
#: of how well they screen, since a signal on them is rejected downstream anyway.
BLOCKED_SYMBOLS = frozenset({"YESBANK", "BHEL"})

NSE_ASM_URL = "https://www.nseindia.com/api/reportASM"
NSE_GSM_URL = "https://www.nseindia.com/api/reportGSM"
#: A browser-like header is required - NSE's API otherwise refuses the request outright.
_NSE_HEADERS = {"User-Agent": "Mozilla/5.0"}


@dataclass(frozen=True)
class ScreenResult:
    symbols: list[str]
    universe_size: int
    stage_counts: dict[str, int]
    computed_at: datetime
    data_through: str
    surveillance_fetch_failed: bool = field(default=False)
    #: symbol -> how many of the last DAILY_WINDOW daily scans it cleared. Populated by
    #: run_weekly_screen() after run_daily_scan() has updated history.
    conviction: dict[str, int] = field(default_factory=dict)


def _today_key(now: datetime | None = None) -> str:
    return (now or datetime.now()).date().isoformat()


def _current_week_key(now: datetime | None = None) -> str:
    """ISO year-week, e.g. "2026-W37" - correctly handles the turn of the year,
    unlike a naive (year, day // 7) computation."""
    iso = (now or datetime.now()).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


def should_run_this_week(now: datetime | None = None) -> bool:
    """True unless the state file already records a WEEKLY finalize for the current
    ISO week. The daily scan is unconditional and has no gate of its own - see
    run_daily_scan()'s docstring for why running it again same-day is harmless."""
    return _load_state().get("last_run_week") != _current_week_key(now)


def fetch_surveillance_symbols() -> tuple[set[str], bool]:
    """Every symbol currently under NSE ASM or GSM. Returns (symbols, fetch_failed).

    On failure, fetch_failed=True and an empty set is returned - the caller falls back
    to BLOCKED_SYMBOLS only rather than aborting the whole screen over one ancillary
    endpoint being briefly unreachable, but the digest says so explicitly so a degraded
    run is never mistaken for a clean one.
    """
    # Plain httpx, not utils.httpx_client - that shared client assumes a Flask app
    # context (it touches flask.g), which does not exist in this standalone script.
    # analysis/breakingtrade/alerts.py (also standalone) uses plain httpx for the
    # same reason.
    import httpx

    try:
        with httpx.Client(headers=_NSE_HEADERS, timeout=20) as client:
            asm = client.get(NSE_ASM_URL).json()
            gsm = client.get(NSE_GSM_URL).json()
        symbols = {row["symbol"] for row in asm["longterm"]["data"]}
        symbols |= {row["symbol"] for row in asm["shortterm"]["data"]}
        symbols |= {row["symbol"] for row in gsm}
        return symbols, False
    except Exception:
        from utils.logging import get_logger

        get_logger(__name__).exception(
            "ASM/GSM fetch failed - falling back to the static blacklist only"
        )
        return set(), True


def _beta_vs_nifty(symbols: list[str]) -> dict[str, float]:
    """Beta on ~1y of daily closes, ONLY for the symbols passed in - the expensive,
    slow-changing factor, fetched only when run_weekly_screen() actually finalizes."""
    import numpy as np
    import pandas as pd
    import yfinance as yf

    nifty = yf.download("^NSEI", period="1y", interval="1d", progress=False, auto_adjust=True)
    if isinstance(nifty.columns, pd.MultiIndex):
        nifty.columns = nifty.columns.get_level_values(0)
    nifty_ret = nifty["Close"].pct_change().dropna()

    betas: dict[str, float] = {}
    for sym in symbols:
        try:
            df = yf.download(sym + ".NS", period="1y", interval="1d",
                             progress=False, auto_adjust=True)
            if df is None or df.empty:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            ret = df["Close"].pct_change().dropna()
            aligned = pd.concat([ret, nifty_ret], axis=1, join="inner")
            aligned.columns = ["stock", "bench"]
            if len(aligned) < 60:
                continue
            var = aligned["bench"].var()
            if not var or np.isnan(var):
                continue
            betas[sym] = float(aligned["stock"].cov(aligned["bench"]) / var)
        except Exception:
            continue
    return betas


@dataclass(frozen=True)
class DailyScan:
    """Output of the cheap, daily-safe part of the screen - no beta, no notification."""
    qualifying_symbols: list[str]
    universe_size: int
    surveillance_fetch_failed: bool
    data_through: str
    #: symbol -> (atr_pct, rvol) for every qualifying symbol, today's values. Carried
    #: through to run_weekly_screen() purely as a sort TIE-BREAK: conviction is the
    #: primary ranking key, but early on (or whenever several symbols tie on
    #: conviction) falling back to alphabetical order would silently discard real
    #: signal - this keeps today's RVOL/ATR% as the secondary key, same as before
    #: conviction tracking existed.
    metrics: dict[str, tuple[float, float]] = field(default_factory=dict)


def run_daily_scan() -> DailyScan:
    """Factors 1-3 (liquidity, ATR%, RVOL) plus the ASM/GSM exclusion. Cheap enough to
    run on every startup: it reuses the same 60-day 5-minute pull the old single-speed
    screen needed anyway, with no per-symbol daily-bar fetch on top of it. Appends to
    STATE_FILE's daily history, keyed by calendar date so re-running the same day
    (e.g. the bot restarted twice) overwrites today's entry instead of duplicating it."""
    import pandas as pd

    from signal_engine.backtest import data
    from signal_engine.backtest import indicators as ind

    frames = data.load(period="60d", interval="5m", refresh=True)
    data_through = max(df.index.max() for df in frames.values()).date().isoformat()
    universe_size = len(frames)

    rows = []
    for sym, df in frames.items():
        name = sym.replace(".NS", "")
        d = ind.add_session_columns(df)
        atr_pct = float((ind.atr(d, 14) / d["Close"] * 100).median())
        daily_vol = df["Volume"].groupby(df.index.date).sum()
        daily_value_cr = float(
            (df["Close"] * df["Volume"]).groupby(df.index.date).sum().median() / 1e7
        )
        rvol = float("nan")
        if len(daily_vol) >= 35:
            baseline = daily_vol.iloc[-35:-5].mean()
            if baseline > 0:
                rvol = float(daily_vol.iloc[-5:].mean() / baseline)
        rows.append({"symbol": name, "atr_pct": atr_pct,
                     "daily_value_cr": daily_value_cr, "rvol": rvol})

    tech = pd.DataFrame(rows)
    surveillance, fetch_failed = fetch_surveillance_symbols()
    blocked = BLOCKED_SYMBOLS | surveillance

    stage1 = tech[
        (tech.daily_value_cr >= MIN_DAILY_VALUE_CR)
        & (tech.atr_pct >= MIN_ATR_PCT)
        & (tech.rvol >= MIN_RVOL)
        & (~tech.symbol.isin(blocked))
    ]
    qualifying = sorted(stage1.symbol.tolist())
    metrics = {
        row.symbol: (float(row.atr_pct), float(row.rvol))
        for row in stage1.itertuples()
    }

    state = _load_state()
    history = state.get("daily_history", [])
    today = _today_key()
    history = [entry for entry in history if entry.get("date") != today]
    history.append({"date": today, "symbols": qualifying})
    history.sort(key=lambda e: e["date"])
    state["daily_history"] = history[-DAILY_WINDOW:]
    _save_state(state)

    return DailyScan(qualifying, universe_size, fetch_failed, data_through, metrics)


def compute_conviction(symbols: list[str]) -> dict[str, int]:
    """For each symbol, how many of the daily scans currently in history it appears
    in (out of up to DAILY_WINDOW). Reads whatever run_daily_scan() has already
    persisted - call this AFTER run_daily_scan(), not instead of it."""
    history = _load_state().get("daily_history", [])
    counts = dict.fromkeys(symbols, 0)
    for day in history:
        for sym in day.get("symbols", []):
            if sym in counts:
                counts[sym] += 1
    return counts


def run_weekly_screen(daily: DailyScan) -> ScreenResult:
    """The expensive, slow-changing part: beta, on top of whatever run_daily_scan()
    just found. Only called when the weekly gate says it is time to finalize."""
    import pandas as pd

    betas = _beta_vs_nifty(daily.qualifying_symbols)
    beta_df = pd.DataFrame(
        {"symbol": daily.qualifying_symbols,
         "beta": [betas.get(s) for s in daily.qualifying_symbols]}
    )
    final = beta_df[beta_df.beta.notna() & beta_df.beta.between(MIN_BETA, MAX_BETA)]

    conviction = compute_conviction(final.symbol.tolist())
    # Conviction is the primary key; today's (ATR%, RVOL) breaks ties instead of
    # falling back to alphabetical order - see DailyScan.metrics' docstring for why.
    def _rank(sym: str) -> tuple[int, float, float]:
        atr_pct, rvol = daily.metrics.get(sym, (0.0, 0.0))
        return (conviction.get(sym, 0), rvol, atr_pct)

    top = sorted(final.symbol.tolist(), key=_rank, reverse=True)[:TOP_N]

    return ScreenResult(
        symbols=top,
        universe_size=daily.universe_size,
        stage_counts={
            "universe": daily.universe_size,
            "liquidity_atr_rvol_not_surveilled": len(daily.qualifying_symbols),
            "beta_band": len(final),
        },
        computed_at=datetime.now(),
        data_through=daily.data_through,
        surveillance_fetch_failed=daily.surveillance_fetch_failed,
        conviction=conviction,
    )


def format_digest(result: ScreenResult) -> str:
    """Plain-text Telegram message. No LONG/SHORT/EXIT token in the header - see
    test_watchlist_screen.py for why that matters even on an unparsed admin channel."""
    week_label = result.computed_at.strftime("week of %d %b %Y")
    sc = result.stage_counts
    lines = [
        f"Watchlist screen | {week_label}",
        "------------------------",
        f"{sc.get('universe', '?')} F&O names -> "
        f"{sc.get('liquidity_atr_rvol_not_surveilled', '?')} pass liquidity/ATR%/RVOL/"
        f"surveillance -> {sc.get('beta_band', '?')} pass beta band",
    ]
    if result.surveillance_fetch_failed:
        lines.append(
            "WARNING: NSE ASM/GSM check failed this run - only the static blacklist "
            "was applied. Verify candidates manually before trading."
        )
    lines.append(f"Top {len(result.symbols)}, data through {result.data_through} "
                 f"(conviction = daily-scan hits in the last {DAILY_WINDOW} trading days):")
    for sym in result.symbols:
        n = result.conviction.get(sym, 0)
        lines.append(f"  {sym} ({n}/{DAILY_WINDOW})")
    lines += [
        "------------------------",
        "For ORB / BREAKOUT / EMA9VWAP. Update TradingView's watchlist manually - this",
        "message does not do that for you. Full reasoning: ema9-vwap/STRATEGY-LOG.md",
    ]
    return "\n".join(lines)


def update_watchlist_file(result: ScreenResult) -> None:
    """Overwrite the repo's watchlist file. This is the record; TradingView is separate
    and manual - see the module docstring. Symbols are annotated with a trailing
    conviction comment so a diff against the previous run shows what actually changed."""
    header = (
        "# Shared intraday watchlist: ORB, BREAKOUT, EMA9VWAP\n"
        "# Four-factor screen: liquidity/ATR%/RVOL/ASM-GSM checked DAILY (median daily\n"
        f"# traded value >= Rs {MIN_DAILY_VALUE_CR:.0f}cr, median ATR% >= {MIN_ATR_PCT:.2f}%,\n"
        f"# 5-day/30-day relative volume >= {MIN_RVOL:.1f}, excludes NSE ASM/GSM and\n"
        f"# {', '.join(sorted(BLOCKED_SYMBOLS))}); beta vs NIFTY (1y daily, band "
        f"[{MIN_BETA:.1f}, {MAX_BETA:.1f}])\n"
        "# checked WEEKLY, since a year of history barely moves day to day. See\n"
        "# STRATEGY-LOG.md for why compute and notify run on different schedules.\n"
        "# No performance-report grading, no sector-rotation filter (swing-only tool).\n"
        "# Regenerated on the first startup of each ISO week by\n"
        "# signal_engine/scripts/watchlist_screen.py - do not hand-edit; change the\n"
        "# screen thresholds there instead.\n"
        f"# Last run: {result.computed_at:%Y-%m-%d %H:%M} IST, data through {result.data_through}\n"
        f"# Trailing (N/{DAILY_WINDOW}) = daily-scan hits in the last {DAILY_WINDOW} trading days.\n"
        "\n"
    )
    lines = [f"{sym}  # {result.conviction.get(sym, 0)}/{DAILY_WINDOW}"
             for sym in result.symbols]
    WATCHLIST_FILE.write_text(header + "\n".join(lines) + "\n")


async def send_digest(message: str) -> bool:
    from signal_engine.scripts.openalgoscheduler import send_telegram_notification

    return await send_telegram_notification(message)


def maybe_run_weekly_screen(force: bool = False) -> bool:
    """Called from openalgoscheduler._run_startup() as its last step, on EVERY
    startup. The daily scan always runs (cheap, idempotent per day); the expensive
    beta fetch and the Telegram/file write only happen when the weekly gate is due.

    Returns True if the weekly finalize ran (regardless of delivery success), False if
    it was gated off (the daily scan still ran either way). Never raises - a screen
    failure must not be allowed to look like a startup failure; the caller logs but
    does not fail on a False/exception from this, matching every other non-fatal step
    in openalgoscheduler.py's own convention.
    """
    daily = run_daily_scan()

    if not force and not should_run_this_week():
        return False

    result = run_weekly_screen(daily)
    message = format_digest(result)
    update_watchlist_file(result)
    asyncio.run(send_digest(message))

    state = _load_state()
    state["last_run_week"] = _current_week_key()
    _save_state(state)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run the daily scan and (if due) the weekly finalize, print the digest, "
             "but do not write the watchlist file, send Telegram, or mark the gate.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Finalize even if this ISO week has already been finalized.",
    )
    args = parser.parse_args()

    print(f"=== daily scan, {datetime.now():%Y-%m-%d %H:%M} ===")
    daily = run_daily_scan()
    print(f"{daily.universe_size} F&O names -> {len(daily.qualifying_symbols)} "
          f"pass liquidity/ATR%/RVOL/surveillance today")

    if not args.force and not should_run_this_week():
        print(f"\nNot yet due for {_current_week_key()} - use --force to finalize anyway.")
        return 0

    print("\n=== weekly finalize ===")
    result = run_weekly_screen(daily)
    message = format_digest(result)
    print(message)

    if args.dry_run:
        print("\n(--dry-run: file not written, message not sent, gate not marked - "
              "daily history above was still recorded for real)")
        return 0

    update_watchlist_file(result)
    print(f"\nwatchlist file updated -> {WATCHLIST_FILE}")

    sent = asyncio.run(send_digest(message))
    print(f"telegram notify_channel send: {'ok' if sent else 'FAILED or not configured'}")

    state = _load_state()
    state["last_run_week"] = _current_week_key()
    _save_state(state)
    return 0 if sent else 1


if __name__ == "__main__":
    raise SystemExit(main())
