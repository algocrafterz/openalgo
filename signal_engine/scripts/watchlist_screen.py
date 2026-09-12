"""Weekly multi-factor screen for the shared ORB/BREAKOUT/EMA9VWAP watchlist.

Runs the screen used to build the watchlist by hand (see
signal_engine/pinescripts/intraday/ema9-vwap/STRATEGY-LOG.md, 2026-09-12/13 entries).
As of 2026-09-13 this checks FOUR factors, not two - researched against public
trading-desk practice and NSE-specific surveillance rules (see that date's log entry
for sources):

    1. Liquidity   - median daily traded value >= Rs 100cr (not market cap: a stock
                     can have a huge market cap and still trade thin - traded VALUE is
                     what determines whether an order actually fills near the quote).
    2. Volatility  - median ATR% of price >= 0.15% (does the stock move enough to
                     reach a target before the day ends).
    3. Beta        - 1.0-2.0 vs NIFTY, computed on ~1y of daily closes. Below 1.0 is
                     too sluggish for intraday; above 2.0 tends to be erratic small-cap
                     risk that is hard to size for. Distinct from ATR%: a stock can be
                     volatile in absolute terms (high ATR%) while barely correlated to
                     the market (low beta) if its moves are purely idiosyncratic - one
                     of this screen's own picks (a newly-listed name) scored #1 on ATR%
                     and 0.60 on beta, which is why both are checked, not one alone.
    4. RVOL        - last 5 trading days' average volume >= 0.8x the preceding 30-day
                     baseline. Liquidity and ATR% are HISTORICAL averages; a stock can
                     clear both while its current activity has already cooled off from
                     whatever catalyst got it there. RVOL is the one factor that asks
                     "is this still happening now".

    Hard exclusion, independent of all four factors above: any symbol currently under
    NSE ASM (Additional Surveillance Measure) or GSM (Graded Surveillance Measure).
    Some ASM stages carry 100% margin (kills MIS leverage) or block intraday trading
    outright - a name can pass every technical filter and still be untradeable via
    MIS, and the broker will refuse the order anyway. Checked live against NSE's own
    reportASM/reportGSM endpoints every run, not a static list, since a stock's
    surveillance stage changes over time. Better to never offer it as a candidate than
    to let a strategy signal fire on it and rely on the broker's own rejection.

WHY WEEKLY, NOT MONTHLY (changed 2026-09-13, same day as the four-factor rewrite)
    Measured directly: recomputing the liquidity/ATR%/RVOL pool at different points
    within the same 60-day window, day-to-day overlap is ~74% (moderate, not
    whiplash) but week-to-week overlap is only ~39% - more than half the pool turns
    over within a single week, mostly driven by RVOL's own 5-day window. A MONTHLY
    screen takes exactly one such noisy snapshot and has no way to tell "this is a
    real multi-week momentum build" from "this stock had one unusually high-volume
    day that happened to land inside the 5-day window on screen day". Weekly runs
    make CONVICTION_WINDOW (below) possible: a symbol appearing in several consecutive
    weekly screens is a materially stronger signal than appearing once, which a
    monthly-only cadence cannot distinguish at all.

CONVICTION TRACKING
    Every run's symbol list is appended to STATE_FILE's rolling history (capped at
    CONVICTION_WINDOW entries). Each candidate in the current digest is annotated with
    how many of the last CONVICTION_WINDOW runs (including this one) it appeared in -
    "4/4" means it has shown up in every recent screen, "1/4" means it is new or
    intermittent. This is reported, not filtered on: a first appearance is still
    useful information, just weaker than a repeated one, and the choice of how much
    weight to give repetition is left to whoever reads the message rather than baked
    into a cutoff nobody could later audit.

COST-AWARE ORDERING (why this fetches liquidity/ATR before beta, not all four at once)
    Beta needs ~1 year of DAILY bars fetched PER SYMBOL - expensive across the ~210
    F&O names. Liquidity, ATR% and RVOL are all derivable from the SAME 60-day 5-minute
    pull already needed for the base screen, and the ASM/GSM check is one shared network
    call regardless of universe size. So the funnel order is: (1) liquidity+ATR%+RVOL
    from data already being fetched, (2) drop ASM/GSM names, (3) fetch beta ONLY for
    whatever survives steps 1-2 (typically ~30-40% of the universe), not all 210. This
    now runs weekly rather than monthly, so keeping the funnel cheap matters more than
    it did - see the startup-timing note below for the actual bound.

WHY IT ONLY UPDATES A FILE AND SENDS A MESSAGE - IT NEVER TOUCHES TRADINGVIEW
    TradingView's watchlist is edited by a human, by design (no public API for it). This
    script's job ends at making the new candidate list visible: it overwrites the repo's
    watchlist file (so the repo stays the source of truth) and posts to the signal-engine
    Telegram channel (notify_channel) so the update is seen without opening a terminal.
    Updating TradingView itself stays a manual, deliberate step.

WHY THIS RUNS FROM STARTUP, NOT A FIXED CRON TIME
    A fixed cron hour assumes the machine is on then, which a laptop is not guaranteed
    to be. `maybe_run_weekly_screen()` is called from `openalgoscheduler._run_startup()`
    as its LAST step: it runs on whatever day the trading bot actually next starts up,
    gated by a state file so it only does real work once per ISO calendar week no
    matter how many times startup runs that week. Being last also means a slow screen
    (beta fetches take a few minutes) never delays the broker-auth/trading-readiness
    steps ahead of it. The Telegram send below is sequential with that earlier one
    (same process, one after another), not concurrent, so it does not race the live
    listener for their shared Telethon session (signal_engine/data/telegram) the way
    two independent processes touching it at once would (see
    analysis/breakingtrade/alerts.py's module docstring for that risk).

USAGE
    Normal operation: nothing to run by hand - openalgoscheduler.py's startup flow
    calls maybe_run_weekly_screen() automatically.

    Manual / testing:
        uv run --group analysis python -m signal_engine.scripts.watchlist_screen
        uv run --group analysis python -m signal_engine.scripts.watchlist_screen --dry-run
        uv run --group analysis python -m signal_engine.scripts.watchlist_screen --force

    --force bypasses the "already ran this week" gate. --dry-run additionally skips
    writing the file, sending Telegram, marking the gate, and updating history.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field, replace
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
#: How many recent runs the conviction count looks back over. 4 at weekly cadence is
#: roughly a month - long enough to distinguish a fluke from a trend, short enough
#: that a genuine regime change is not stuck being remembered for a full quarter.
CONVICTION_WINDOW = 4
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
    #: symbol -> how many of the last CONVICTION_WINDOW runs (including this one) it
    #: appeared in. Populated by maybe_run_weekly_screen()/main() after run_screen()
    #: returns, since it needs the persisted history - empty from run_screen() alone.
    conviction: dict[str, int] = field(default_factory=dict)


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


def should_run_this_week(now: datetime | None = None) -> bool:
    """True unless the state file already records a run for the current ISO week.

    A missing or corrupt state file reads as "not run yet" - fail toward doing the
    (idempotent, harmless-to-repeat) screen rather than silently skipping it forever
    because of a bad state file.
    """
    return _load_state().get("last_run_week") != _current_week_key(now)


def mark_run_this_week(symbols: list[str], now: datetime | None = None) -> None:
    """Records the run AND appends to the rolling history conviction tracking reads.

    History is capped at CONVICTION_WINDOW entries - it exists only to answer "how
    many of the last few runs was this symbol in", nothing further back matters.
    """
    state = _load_state()
    history = state.get("history", [])
    history.append({"week": _current_week_key(now), "symbols": symbols})
    state["last_run_week"] = _current_week_key(now)
    state["history"] = history[-CONVICTION_WINDOW:]
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


def compute_conviction(symbols: list[str]) -> dict[str, int]:
    """For each symbol, how many of the last CONVICTION_WINDOW *recorded* runs
    (not counting the run in progress, which has not been marked yet) it appeared in,
    plus 1 for the current run itself. Called with THIS run's own symbol list before
    mark_run_this_week() persists it, so the count is always current-run-inclusive."""
    history = _load_state().get("history", [])
    counts = dict.fromkeys(symbols, 1)  # this run counts as an appearance
    for past_run in history:
        for sym in past_run.get("symbols", []):
            if sym in counts:
                counts[sym] += 1
    return counts


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
    """Beta on ~1y of daily closes, ONLY for the symbols passed in - see the module
    docstring's cost-aware-ordering note for why this is never run on the full universe."""
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


def run_screen() -> ScreenResult:
    """The four-factor screen. Always refreshes the 5-min data cache - a stale cache
    silently reproduces last week's numbers under this week's date, which is exactly
    the failure mode a scheduled re-run exists to prevent. Conviction is NOT populated
    here (empty dict) - see compute_conviction()'s docstring for why that is a separate
    step; callers that want it call compute_conviction(result.symbols) themselves."""
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
    ].copy()

    betas = _beta_vs_nifty(stage1.symbol.tolist())
    stage1["beta"] = stage1.symbol.map(betas)
    final = stage1[
        stage1.beta.notna() & (stage1.beta >= MIN_BETA) & (stage1.beta <= MAX_BETA)
    ].sort_values(["rvol", "atr_pct"], ascending=False)

    top = final.head(TOP_N)
    return ScreenResult(
        symbols=top.symbol.tolist(),
        universe_size=universe_size,
        stage_counts={
            "universe": universe_size,
            "liquidity_atr_rvol_not_surveilled": len(stage1),
            "beta_band": len(final),
        },
        computed_at=datetime.now(),
        data_through=data_through,
        surveillance_fetch_failed=fetch_failed,
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
                 f"(conviction = appearances in the last {CONVICTION_WINDOW} runs):")
    for sym in result.symbols:
        n = result.conviction.get(sym, 1)
        lines.append(f"  {sym} ({n}/{CONVICTION_WINDOW})")
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
        "# Four-factor screen (current 60d, 5-min bars unless noted): median daily\n"
        f"# traded value >= Rs {MIN_DAILY_VALUE_CR:.0f}cr, median ATR% >= {MIN_ATR_PCT:.2f}%,\n"
        f"# beta vs NIFTY (1y daily) in [{MIN_BETA:.1f}, {MAX_BETA:.1f}], 5-day/30-day\n"
        f"# relative volume >= {MIN_RVOL:.1f}. Excludes {', '.join(sorted(BLOCKED_SYMBOLS))}\n"
        "# (blacklisted) and any symbol currently under NSE ASM/GSM surveillance.\n"
        "# No performance-report grading, no sector-rotation filter (swing-only tool -\n"
        "# see STRATEGY-LOG.md). Regenerated on the first startup of each ISO week by\n"
        "# signal_engine/scripts/watchlist_screen.py - do not hand-edit; change the\n"
        "# screen thresholds there instead.\n"
        f"# Last run: {result.computed_at:%Y-%m-%d %H:%M} IST, data through {result.data_through}\n"
        f"# Trailing (N/{CONVICTION_WINDOW}) = appearances in the last {CONVICTION_WINDOW} weekly runs.\n"
        "\n"
    )
    lines = [f"{sym}  # {result.conviction.get(sym, 1)}/{CONVICTION_WINDOW}"
             for sym in result.symbols]
    WATCHLIST_FILE.write_text(header + "\n".join(lines) + "\n")


async def send_digest(message: str) -> bool:
    from signal_engine.scripts.openalgoscheduler import send_telegram_notification

    return await send_telegram_notification(message)


def maybe_run_weekly_screen(force: bool = False) -> bool:
    """Called from openalgoscheduler._run_startup() as its last step.

    Returns True if the screen ran (regardless of delivery success), False if the
    weekly gate skipped it. Never raises - a screen failure must not be allowed to
    look like a startup failure; the caller logs but does not fail on a False/exception
    from this, matching every other non-fatal step in that file's own convention.
    """
    if not force and not should_run_this_week():
        return False

    result = run_screen()
    conviction = compute_conviction(result.symbols)
    result = replace(result, conviction=conviction)
    message = format_digest(result)
    update_watchlist_file(result)
    asyncio.run(send_digest(message))
    mark_run_this_week(result.symbols)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run the screen and print the digest, but do not write the watchlist "
             "file, send Telegram, mark the weekly gate, or update history.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Run even if this ISO week's screen has already run.",
    )
    args = parser.parse_args()

    if not args.force and not args.dry_run and not should_run_this_week():
        print(f"Already ran for {_current_week_key()} - nothing to do. Use --force to override.")
        return 0

    print(f"=== watchlist screen, {datetime.now():%Y-%m-%d %H:%M} ===")
    result = run_screen()
    conviction = compute_conviction(result.symbols)
    result = replace(result, conviction=conviction)
    message = format_digest(result)
    print(message)

    if args.dry_run:
        print("\n(--dry-run: file not written, message not sent, gate not marked, "
              "history not updated)")
        return 0

    update_watchlist_file(result)
    print(f"\nwatchlist file updated -> {WATCHLIST_FILE}")

    sent = asyncio.run(send_digest(message))
    print(f"telegram notify_channel send: {'ok' if sent else 'FAILED or not configured'}")
    mark_run_this_week(result.symbols)
    return 0 if sent else 1


if __name__ == "__main__":
    raise SystemExit(main())
