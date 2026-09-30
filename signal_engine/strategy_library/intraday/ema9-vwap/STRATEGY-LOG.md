# EMA9 x VWAP — Strategy Log

Plain 9 EMA / session VWAP crossover, NSE equity intraday, 5-minute.
Script: `ema9-vwap.pine` | Adapter: `signal_engine/backtest/strategies/ema9_vwap.py`

---

## How entry, stop-loss and target are computed

This section explains the mechanics in plain terms. It is reference material, kept up
to date whenever the logic changes; it is not a changelog entry itself.

**Entry — the trigger.** The script watches two lines: the 9 EMA (a fast average of the
last 9 candles) and VWAP (the average price the stock has traded at today, weighted by
volume — roughly "what most of today's money paid"). At the close of a 5-minute candle,
if the 9 EMA is below VWAP and then closes above it, that is a LONG. If it is above VWAP
and closes below it, that is a SHORT. Nothing else has to agree by default — no
candlestick pattern, no volume check, no trend filter. The signal only evaluates once a
candle has fully closed, so it cannot flicker mid-candle and cannot fire on a price that
later reverses within the same candle.

**Stop-loss — how far is "wrong."** The default stop style is Swing: look back over the
last 8 candles, find the lowest low (for a long) or highest high (for a short), and place
the stop a little beyond it — a small buffer equal to about a tenth of the stock's
typical 5-minute range, so ordinary noise does not tag it. In plain terms: "the most
recent turning point, plus a hair of room." Two alternative stop styles exist (a fixed
ATR distance, and a stop parked on the far side of VWAP) but neither measured better, so
Swing is the default. If that stop would work out to less than 0.20% of the share price,
the trade is skipped rather than sent with a stop too tight to survive — this mirrors a
check the live order system performs anyway, so a signal it would reject is never sent.

**Take-profit — where to book it.** A single multiple of the risk: whatever distance the
stop sits at, the target is twice that distance in the trade's favour (a "2R" target). If
the stop risks Rs 5, the target is Rs 10 away. There is no trailing stop and no partial
exit — one target, full position closed.

**Everything else that can end the trade.** The stop or target being hit closes it.
Independently, a hard cutoff at 14:45 IST flattens any open position regardless of price,
matching the platform-wide time exit. An optional exit (off by default) closes the trade
if the 9 EMA crosses back the other way, for anyone who prefers to leave early rather
than wait for the stop.

**Why these particular choices.** None of the stop style or target multiple was picked
because it measured best — the measurement work below shows nothing performs
meaningfully differently from anything else, because price moves about as far against a
crossover as it does in its favour. Given that, the simplest, most defensible version was
kept rather than tuning a knob until one combination looked good on this data.

---

### 2026-09-13 15:34 — Compute daily, decide weekly: the previous entry conflated the two

**What changed**

Direct pushback on the previous entry's explanation: "why not compute daily and use
it weekly - your explanation is not understandable." The pushback was correct. The
previous entry picked ONE cadence (weekly) for all four factors because it only
compared "run everything weekly" against "run everything daily" and weekly won on
cost. That comparison was never the right one to make - cost only applies to ONE of
the four factors (beta), not all of them, and bundling them together was the actual
mistake, not the weekly conclusion itself.

**In plain terms, before the technical detail:** two of the checks (how much a stock
trades, how much it moves) are like a person's long-term fitness level - they do not
swing day to day. One check (is it unusually busy right now) is like a heart rate -
it changes fast, sometimes within a day. One check (how closely it moves with the
whole market) is built from a full year of history, so one more day of data changes
it about as much as one more day changes a year-long average - essentially not at
all. Treating all four as if they needed the same refresh schedule was the error.

**What actually changed in the code:** the screen is now split into two functions.
`run_daily_scan()` - liquidity, ATR%, RVOL, and the ASM/GSM exclusion - runs on
EVERY startup, unconditionally, and only appends today's qualifying pool to a
rolling 10-trading-day history. It does not fetch beta and does not notify anyone.
`run_weekly_screen()` runs on top of that history once a week: it is the only place
that fetches beta (the genuinely expensive, genuinely slow-changing factor), builds
the final Top 20, writes the watchlist file, and sends the Telegram message. The
result is a richer, 10-point daily-resolution conviction count (`N/10`) instead of
the previous 4-point weekly one (`N/4`), without paying for a beta refetch or asking
for a manual TradingView update more than once a week.

**A second, real bug found while implementing this, not just a rename:** the
previous version's final ranking sorted candidates by conviction alone. On the very
first run under any conviction system, everything ties (see the 2026-09-13 03:51
entry's own first output - all 20 candidates were `1/4`), and Python's sort then
falls back to whatever order the list was already in - alphabetical, in this case.
That silently discarded the RVOL/ATR% ranking that mattered before conviction
tracking existed. Fixed by carrying today's (ATR%, RVOL) through as an explicit
tie-break: conviction is still the primary sort key, but ties now fall back to the
same ranking the screen has always used, not the alphabet. Verified directly: a
synthetic case where a stock named to sort first alphabetically has the weaker RVOL
correctly loses the tie to the one with genuinely higher current volume.

**Entry** — Not affected for any strategy; still a watchlist-maintenance change.

**Exit (SL)** — Not affected.

**Exit (TP)** — Not affected.

**Consideration** — The daily scan now runs on every startup regardless of the
weekly gate, which means the expensive 210-symbol 5-minute data refresh happens
daily, not weekly. This was already true in spirit (RVOL cannot be checked without
it), so the actual new recurring cost is small; what changed is that this cost is
now paid every day instead of masked by a weekly gate that used to cover all four
factors at once. Beta - the one genuinely expensive per-symbol fetch - still only runs
weekly, which is the whole point of the split. If the daily 5-minute refresh itself
ever becomes a bottleneck, the next lever is caching it independently of both
schedules, not re-coupling the four factors back together.

**NOTE:** Re-ran end to end after the fix: same 64/40 funnel, same Top-20 list as
the pre-conviction version (COCHINSHIP, BANDHANBNK, KEI, DLF, GODREJPROP, IDEA,
ADANIENT, COFORGE, POLYCAB, TATASTEEL, ULTRACEMCO, MARUTI, BANKBARODA, CHOLAFIN,
INDUSINDBK, CDSL, DIXON, LTM, INDIANB, ADANIPORTS) - confirms the tie-break fix
restores the original ranking quality while the conviction machinery sits
underneath it, ready to differentiate once a real history accumulates. `DailyScan`
gained a `metrics: dict[symbol -> (atr_pct, rvol)]` field purely to carry this
tie-break data from the daily scan to the weekly finalize step - no new network
calls, values already computed by run_daily_scan(). 5 new tests (31 total):
`TestRankingTieBreak` plus daily-history/weekly-gate coverage for the split itself.

Reproduce: `uv run --group analysis python -m signal_engine.scripts.watchlist_screen
--dry-run --force`.

---

### 2026-09-13 03:51 — Weekly cadence (measured, not assumed) + conviction tracking; ASM/GSM confirmed as watchlist-level exclusion

**What changed**

Two follow-ups to the four-factor rewrite above, both from direct feedback.

**Cadence.** Asked directly whether weekly beats monthly, with the reasoning that a
symbol showing up repeatedly would be "added conviction." Measured before changing
anything, since RVOL specifically is a 5-day-window statistic and wasn't part of the
screen when monthly was originally chosen: recomputed the liquidity/ATR%/RVOL pool at
several points within the same 60-day cache (today, 1 day back, 1/2/4 weeks back).
Day-to-day overlap is ~74% - moderate, not whiplash. Week-to-week overlap is only
**~39%** - more than half the pool turns over within a single week. A monthly screen
takes exactly one such noisy snapshot with no way to tell a real multi-week build from
a single unusually-high-volume day landing inside the 5-day RVOL window on the one day
a month it happens to look. Moved to weekly (ISO calendar week, `%G-W%V` so the turn
of the year does not break it).

**Conviction tracking**, implementing the actual reasoning offered for weekly rather
than just running the same screen more often: every run's symbol list is now appended
to a rolling history (`signal_engine/data/watchlist_screen_state.json`, capped at
`CONVICTION_WINDOW=4` entries - roughly a month at weekly cadence). Each candidate in
the digest and the watchlist file is annotated `(N/4)` - how many of the last 4 runs
(this one included) it appeared in. This is reported, not filtered on: a first
appearance is still useful information, just weaker than a repeated one, and how much
weight to give repetition is left to whoever reads the message. First real run under
this system: all 20 candidates show `(1/4)`, as expected - there is no history yet.

**ASM/GSM.** Confirmed rather than changed: the broker already refuses orders on
ASM/GSM names (100% margin or an outright intraday block, depending on stage), so
excluding them from the watchlist upstream - never offering them as a candidate at
all - is the right layer, rather than letting a strategy signal fire and rely on the
broker's own rejection downstream. This was already how the screen worked from the
2026-09-13 03:30 entry; no code change here, just recorded as a confirmed decision
rather than left implicit.

**Entry** — Not affected for any strategy; still a watchlist-maintenance change.

**Exit (SL)** — Not affected.

**Exit (TP)** — Not affected.

**Consideration** — Weekly at four factors means the several-minutes beta fetch now
recurs weekly instead of monthly (4x more often). Still the LAST step of startup, so
it cannot delay trading readiness or the startup notification ahead of it - the cost
is a longer gap between "broker login confirmed" and "this script finishes" on the
first startup of each week, not a delay to anything that depends on startup
completing sooner. Accepted as a bounded, weekly (not daily) cost; the user was
explicitly open to daily cadence too ("weekly or daily is also fine"), but daily was
not chosen - it would mean re-running the expensive beta fetch and sending a Telegram
message every single day, and a manual TradingView update burden to match, for a
day-to-day overlap (74%) that is already far more stable than the week-to-week number
that actually motivated this change.

**NOTE:** Churn measurement (current 60-day cache, liquidity+ATR%+RVOL pool only -
beta was not re-measured at each checkpoint since 1y lookback barely moves week to
week):

| checkpoint | pool size | overlap vs today | jaccard |
| --- | --- | --- | --- |
| today | 64 | - | - |
| 1 day ago | 63 | 54 shared | 0.74 |
| 1 week ago | 89 | 43 shared | 0.39 |
| 2 weeks ago | 63 | 33 shared | 0.35 |
| 1 month ago | 62 | 24 shared | 0.24 |

The 1-week-ago pool size (89, well above the ~62-64 seen everywhere else) is itself
evidence for the underlying claim: a single day's unusual volume can swing RVOL
broadly across many names when it lands inside the 5-day window, which is exactly the
kind of artifact conviction tracking exists to separate from a genuine multi-week
trend.

State file shape changed from `{"last_run_month": ...}` to additionally carry
`"last_run_week"` and `"history": [{"week": ..., "symbols": [...]}, ...]`. The old
`last_run_month` key is left in place on existing state files rather than migrated -
harmless, unread by any current code path, not worth risking the history list to
clean up.

Reproduce: `uv run --group analysis python -m signal_engine.scripts.watchlist_screen
--dry-run --force`.

---

### 2026-09-13 03:30 — Screen redesigned: trigger moved to startup, criteria expanded from two factors to four (with sources)

**What changed**

Two follow-ups to the automation above, both from direct feedback rather than
self-review.

First, the trigger. A fixed 07:00 IST cron time assumes the machine is on then, which
a laptop is not guaranteed to be - so the whole automation could silently never fire.
Removed that cron entry entirely. `maybe_run_monthly_screen()` is now called from
`openalgoscheduler._run_startup()` as its LAST step, gated by a state file
(`signal_engine/data/watchlist_screen_state.json`) so it only does real work once per
calendar month no matter how many times startup itself runs. It now runs on whatever
day the system actually next starts up - the "laptop might not be on" problem stops
being a problem because there is no fixed hour to miss. This also turned out to
resolve the Telethon-session risk more cleanly than the time-separation the previous
entry relied on: the digest send is now sequential, inside the same startup script,
strictly after the startup notification has already connected/sent/disconnected -
not two independent processes racing for the same session file.

Second, the criteria. Asked directly whether "market cap and ATR" was a sufficient
screen (worth a correction here: it was traded VALUE, not market cap - a large
market cap does not guarantee a stock actually trades enough volume to fill an order
near the quote - but the substance of the question stood: two factors is thin).
Researched public trading-desk practice and NSE-specific rules before changing
anything; full findings and sources below. Two real gaps came out of it:

1. **No momentum-confirmation dimension.** Liquidity and ATR% are both HISTORICAL
   averages over 60 days. A stock can clear both while its current activity has
   already cooled from whatever got it there. Added relative volume (RVOL): last 5
   trading days' average volume vs the preceding 30-day baseline. Checked directly
   against September's prior list of 24 (liquidity+ATR% only): **15 of 24 failed
   this specific check** - their own recent volume had fallen below their own
   30-day norm, meaning much of that list was already stale within the same month
   it was built.

2. **No NSE-specific tradeability gate.** A stock under ASM (Additional Surveillance
   Measure) or GSM (Graded Surveillance Measure) can carry 100% margin (kills MIS
   leverage) or have intraday trading blocked outright on some stages. Nothing in
   the screen checked this - a name could pass every technical filter and still be
   untradeable via MIS. Now checked LIVE every run against NSE's own
   reportASM/reportGSM endpoints (239 and 75 symbols flagged respectively as of this
   run - genuinely large numbers, not an edge case). On failure the screen falls
   back to the static blacklist only and says so explicitly in the digest - it does
   not silently present a degraded run as clean.

Also added: beta vs NIFTY (1y daily closes), 1.0-2.0 band, because it measures
something ATR% does not - market-relative sensitivity, not absolute movement. One of
the prior list's own top picks by ATR% (a newly-listed name) scored 0.60 on beta,
meaning its volatility was almost entirely idiosyncratic rather than the kind of
market-correlated movement an opening-range or breakout setup is built to catch.

**Entry** — Not affected for any strategy; this is still a watchlist-maintenance
change, not a trading-rule change.

**Exit (SL)** — Not affected.

**Exit (TP)** — Not affected.

**Consideration — a real trade-off, stated plainly.** Beta needs ~1 year of daily
bars fetched PER SYMBOL, which is expensive across ~210 F&O names. The screen now
funnels: liquidity/ATR%/RVOL (free - reuses the 60-day 5-min pull already needed) and
the ASM/GSM check (one shared network call) run first and typically cut the universe
to 60-70 names; beta is fetched only for whatever survives that, not all 210. Even so,
this run took several minutes end to end. Since it is the LAST step of startup, it
cannot delay trading readiness or the startup notification ahead of it - but it does
mean the very first trading day of each month has a several-minute-longer gap between
"broker login confirmed" and "this script finishes" than every other day. Accepted as
a bounded, once-a-month cost; revisit if it ever meaningfully delays something that
depends on startup fully returning.

**NOTE:** Research sources (2026-09-13 web search, cited per the house style of
recording what was actually checked): NSE's own F&O eligibility page
(top-500 by avg daily market cap/traded value, min Rs 75 lakh median quarter-sigma
order size, min Rs 1500cr market-wide position limit) confirms the existing NSE_FNO
universe is already pre-filtered by NSE itself - nothing to re-derive there. ASM/GSM
mechanics (100% margin Stage 1, 5-10% circuit filter, intraday blocked on LT-ASM
stages) came from Bajaj Broking's and 5paisa's explainers and were verified against
NSE's own live `reportASM`/`reportGSM` API responses, not just the descriptive
pages, before being wired into the script. The beta 1.0-2.0 / RVOL >=2x-ideal
guidance came from a MarketNetra intraday-screening checklist and a Screener.in
"high beta for intraday" community screen; the RVOL and relative-strength-ranking
framing came from stockalarm.io's breakout-screening writeup and trade-ideas.com's
momentum-scanner guide. MIN_RVOL was set at 0.8, not the ~2x "hot right now" threshold
those sources describe for picking entries on a given day, because this is a MONTHLY
universe screen, not a same-day trigger - the goal here is "not obviously cooling
off", which a much stricter bar would have reduced to a handful of names.

Full funnel this run: **210** F&O names -> **64** pass liquidity + ATR% + RVOL + not
under blacklist/surveillance -> **40** pass the beta band -> top **20** by RVOL then
ATR% sent. Final list: COCHINSHIP, BANDHANBNK, KEI, DLF, GODREJPROP, IDEA, ADANIENT,
COFORGE, POLYCAB, TATASTEEL, ULTRACEMCO, MARUTI, BANKBARODA, CHOLAFIN, INDUSINDBK,
CDSL, DIXON, LTM, INDIANB, ADANIPORTS. `TOP_N` lowered from 24 to 20, matching the
10-20 range multiple sources gave as the practical size for a workable day-trading
watchlist.

Bug found and fixed while wiring the ASM/GSM fetch: `utils.httpx_client` (the
platform's shared HTTP client) assumes a live Flask app context (`flask.g`) and
raises `RuntimeError: Working outside of application context` when called from a
standalone script - exactly the situation here, and the same reason
`analysis/breakingtrade/alerts.py` (also standalone) uses plain `httpx` directly
rather than the shared client. Switched to match that precedent. The graceful
fallback caught this correctly during testing (logged, fell back to the static
blacklist, digest carried the warning) before the fix was made - proof the
degraded-mode path actually works, not just that it compiles.

Reproduce: `uv run --group analysis python -m signal_engine.scripts.watchlist_screen
--dry-run --force`.

---

### 2026-09-13 02:10 — Monthly screen automated: cron + Telegram digest, no TradingView write

**What changed**

The monthly technical-metrics screen (liquidity + ATR%, previous entry) is now a real,
scheduled job, not a script someone has to remember to run. New files:

- `signal_engine/scripts/watchlist_screen.py` — runs the screen, overwrites the repo's
  `intraday-stocks-watchlist-tradingview`, formats a digest, sends it.
- `signal_engine/scripts/watchlist_screen.sh` — cron wrapper (lock file, logging),
  matching `analysis/eod.sh`'s existing conventions exactly.
- `signal_engine/tests/test_watchlist_screen.py` — 8 tests, mainly that the digest
  message can never parse as a trade signal.

Cron entry installed on this host (`crontab -l` now shows it alongside the existing EOD
job): `0 7 1 * * .../watchlist_screen.sh >> .../watchlist_screen_cron.log 2>&1` — 07:00
IST, the 1st of every month.

**Entry** — Not affected for any strategy. This automates a maintenance task, not a
trading rule.

**Exit (SL)** — Not affected.

**Exit (TP)** — Not affected.

**Consideration — what this does and does not do.** It writes the repo's watchlist
file and sends a Telegram message. It does **not** touch TradingView — there is no
public API for a TradingView watchlist, so pasting the new list in stays a manual,
human step every month, exactly as asked. Treat the message as a prompt to go update
TradingView, not as confirmation that it is already updated.

**Why 07:00 IST specifically, not some other time or "right when the screen finishes":**
the Telegram send reuses `openalgoscheduler.send_telegram_notification()`, which owns
the SAME Telethon session file (`signal_engine/data/telegram`) the live listener
(`listener.py`) uses to receive trade signals. Two processes touching one Telethon
session concurrently is a documented corruption risk elsewhere in this codebase
(`analysis/breakingtrade/alerts.py`'s module docstring is why BreakingTrade uses a
separate bot token instead). 07:00 sits 1h50m before `openAlgoAutoStart`'s 08:50
listener startup, so the two can never overlap. Do not reschedule this job later in the
day without re-checking that gap - the safety here is entirely about timing, not code.

**Why the message goes to `notify_channel`, not a new dedicated channel:** it already
exists (`signal-engine-analyze` / `signal-engine-live`), it is already the admin/system
channel for exactly this kind of non-strategy-specific notice (day summaries, startup/
shutdown, risk halts), and reusing it needed no new Telegram setup from the user. The
send broadcasts to every configured phase (same as a startup notice), since a watchlist
update is not phase-specific.

**Verified end to end, not just written:** ran the script for real once during this
session (not the scheduled run) to confirm delivery actually works rather than trusting
an untested cron line. It sent successfully to both configured `notify_channel` entries
and rewrote the watchlist file with a live run (`ATHERENERG` - a newer listing - had
since entered the top of the screen, ahead of `PERSISTENT` from the prior manual run).

**NOTE:** Screen thresholds (`MIN_DAILY_VALUE_CR=100`, `MIN_ATR_PCT=0.15`, `TOP_N=24`,
`BLOCKED_SYMBOLS={YESBANK, BHEL}`) live as module constants in `watchlist_screen.py`,
matching the 2026-09-12 21:36 entry's methodology exactly. Changing a threshold without
a matching STRATEGY-LOG.md entry is exactly the kind of unauditable drift this whole
review was trying to eliminate - don't.

---

### 2026-09-13 01:36 — Sector rotation scoped to swing only; refresh cadence set; final ORB/BREAKOUT/EMA9VWAP list confirmed

**What changed**

Decision, not code: `sector-rotation-map` (the RRG dashboard investigated in the
previous entry) is adopted for SWING strategies only (`rsi-tp-mr` and similar,
weekly-bar positioning), not for ORB/BREAKOUT/EMA9VWAP. Reasoning, in order:

1. The app's calculation was verified correct (independent reimplementation matched
   its live output exactly, both at sector and per-stock level) - that was never in
   doubt after the last entry.
2. What was NOT verified is that "Leading/Improving this week" predicts anything
   useful on a 5-minute intraday chart. RRG is a weekly-bar, swing-horizon technique
   by construction (52-week rolling window, weekly resample) - applying its verdict to
   an intraday entry is a timeframe mismatch that was never tested, and per the
   validation-suite guidance now on file (`project_backtest_validation_suite.md`),
   a strategy verdict is not trusted here without that kind of check.
3. It fits the swing side cleanly instead: `rsi-tp-mr` already holds positions 2-7
   days and reasons on daily/weekly structure, the same horizon this tool is built
   for. Use it there if and when that strategy's stock selection is revisited.

Intraday stock selection stays exactly as the previous entry left it: liquidity and
volatility, no sector filter. This entry does not change which stocks are on the
list, only confirms what was already decided and answers the open cadence question.

**Entry** — Not affected.

**Exit (SL)** — Not affected.

**Exit (TP)** — Not affected.

**Consideration — static list vs periodic refresh.** Asked directly: a single
static watchlist is not recommended for ORB/BREAKOUT specifically, because the
thing they need (elevated ATR%, i.e. a stock currently more volatile than average)
is usually driven by a live catalyst - a listing, a corporate action, a sector
story, a news cycle - and those fade. A blue-chip's volatility is structurally
stable for years; a momentum name's is not, almost by definition. Freezing this
list once would reproduce the exact failure mode this whole review started from -
a stale ranking presented as current.

Recommend a MONTHLY refresh, not weekly and not static:
- Weekly is unnecessary churn: the same technical screen run twice, 2 weeks apart
  this session (2026-08-30 cache, then a forced 2026-09-11 refresh), returned
  materially the same top names with only minor value shuffling - the ranking is
  not noisy week to week.
- Monthly matches how every other blacklist/watchlist review on this platform
  already operates (ORB, EMA9, BREAKINGTRADE blacklists are all reviewed on a
  monthly cadence per `config.yaml`'s own comments) and is cheap to run: the same
  screen script, a few minutes, no manual grading.
- Rerun with `data.load(refresh=True)` every time - the first pass of the previous
  entry silently served a 13-day-old cache and had to be redone; that mistake is
  easy to repeat if the refresh flag is forgotten.

**NOTE:** Final candidate list for ORB/BREAKOUT/EMA9VWAP, unchanged from the
previous entry's screen (liquidity >= Rs 100cr, ATR% >= 0.15%, current 60-day
window, top 24 by ATR%): GVT&D, KALYANKJIL, SWIGGY, KAYNES, LODHA, PAYTM,
ADANIENSOL, PRESTIGE, MCX, MANAPPURAM, IDEA, ANGELONE, KEI, POWERINDIA, OFSS, SAIL,
GODREJPROP, ADANIGREEN, BDL, DELHIVERY, LTM, COFORGE, SONACOMS, PERSISTENT.

Two symbol-level notes from the RRG investigation, kept here since they were found
along the way and are otherwise easy to lose: GVT&D failed inside
`sector-rotation-map`'s own per-stock endpoint on an ampersand-encoding bug in that
app, but is confirmed directly tradeable via OpenAlgo (`client.quotes` returns a
live LTP) - no change needed on our side. LTM returned no RRG result (~7 months of
history, since 2026-02-27, likely short of that tool's 52-week window) but has 134
daily bars via OpenAlgo `history()` and is fine for a 60-day intraday screen.

---

### 2026-09-12 21:36 — Old reports removed; watchlist rebuilt from pure technical metrics

**What changed**

Per direct instruction: the dated performance reports this session had been leaning on
are removed from the repo, and the watchlist is rebuilt from each stock's own current
technical metrics instead of any strategy backtest grade - old or fresh. This entry
supersedes the stock-selection reasoning in the two entries above; those are left in
place as a historical record rather than edited, but their FEDERALBNK/BANKBARODA/
TATAPOWER/APOLLOTYRE calls should be read as overtaken by this one.

**Removed:** `trade-analysis/orb-performance-2026-Q1.md`, `orb-performance-2026-H1.md`,
`SIGNAL-PERFORMANCE-2026-Q1.md`, `orb-telegram-export-2026-Q1.json`, and two untracked
raw `result.json` telegram exports (already gitignored, so not a repo change). Trimmed
the "Signal Performance Summary (Q1 2026)" and "Stock Selection Methodology" sections
out of `orb/STRATEGY-ANALYSIS.md` - the grade-based monthly-review process described
there is the same thing being replaced here. `analyze_orb.py` (the tool that generates
this kind of report from a fresh export) is kept; it is reusable, not stale data.
`breakout.pine`'s changelog (`breakout.md`) was left alone - it is engineering history
(bugs found and fixed, checks performed), not a stock-performance report, and one of
its entries turned out to matter directly, below.

**Entry** — Not affected for any script. This is a watchlist change only: the shared
`intraday-stocks-watchlist-tradingview` file, read by ORB, BREAKOUT and EMA9VWAP alike.

**Exit (SL)** — Not affected.

**Exit (TP)** — Not affected.

**Consideration** — Before rebuilding the list, the ORB and BREAKOUT shipped-default
backtests were re-run on the CURRENT 60-day window (2026-06-22 to 2026-09-11) across
all 208 F&O names, because per-symbol backtest P&L was the obvious alternative
technical metric to liquidity and volatility, and it needed checking honestly. Both
came back net negative across the whole universe with shipped defaults - not a
few bad stocks, the whole thing. For BREAKOUT this is not new: `breakout.md`'s own
2026-08-30 entry had already found the key-level break event itself follows through
*less* often than a coin flip (30.9% vs a 33.3% no-edge baseline, t = -9.87, 49,677
events) - today's number matches that finding almost exactly. For ORB this is a softer
signal: a generic run with shipped defaults across every F&O name is not the same
system as the live, curated, tuned deployment that produced the positive record the
now-removed reports described, so it does not mean the live approach has stopped
working - it means a strategy backtest, run this broadly and this untuned, is too noisy
and too uniformly negative to tell a good stock from a bad one right now. That is the
actual argument for doing this the way it was asked to be done: liquidity and
volatility are properties of the stock, measured directly from price and volume, and
they do not carry that noise.

**Screen used:** current 60-day 5-minute bars, median daily traded value (`Close x
Volume`, summed per session) >= Rs 100 crore, median ATR% of price >= 0.15%. 141 of 208
F&O names clear both floors. Excluded YESBANK (global blacklist, manipulation risk) and
BHEL (ORB/BREAKOUT hard blacklist in `config.yaml`) - both still enforced live
regardless of what this file lists. Sorted by ATR% (most range to trade) and took the
top 24: GVT&D, KALYANKJIL, SWIGGY, KAYNES, LODHA, PAYTM, ADANIENSOL, PRESTIGE, MCX,
MANAPPURAM, IDEA, ANGELONE, KEI, POWERINDIA, OFSS, SAIL, GODREJPROP, ADANIGREEN, BDL,
DELHIVERY, LTM, COFORGE, SONACOMS, PERSISTENT.

This list looks nothing like the old PSU/metals-heavy one (SBIN, HINDALCO, TATASTEEL,
PFC, ...), and that is the point: it reflects which names currently have the volume
and range to be worth trading, not which names happened to trade well five to eight
months ago. Re-run the same screen periodically rather than hand-editing grades back
in - `data.load(refresh=True)` before rescreening, since the cache otherwise silently
serves a stale window (this session's first pass, before catching it, was reading an
2026-08-30 cache and reported the same numbers `breakout.md` had already recorded on
that date, not a new measurement).

**NOTE:** No per-symbol backtest P&L is used for ranking in the final list, only
liquidity and ATR% - reasoning above. `analyze_orb.py` remains available to regenerate
a fresh performance report from a new Telegram export whenever one is wanted; none is
checked in right now.

---

### 2026-09-12 21:13 — 3-min timeframe checked, watchlist corrected against ORB's own grade history

**What changed**

Follow-up to the same-day PDF review. Three things: whether switching from 5-minute to
3-minute bars gets a genuinely better entry, a correction to the watchlist edit above
(the first pass only checked liquidity, not the strategy's own track record on those
names), and a note that the ORB "15" in this codebase is the 15-minute opening-range
window, not a candle size — confirmed by reading `orb.pine` itself, and unrelated to
either of the timeframe checks above, so nothing needed changing there.

**Entry** — Tested 3-minute vs 5-minute head to head, on the same underlying 1-minute
ticks over the same 7-day window (Yahoo does not serve 3-minute NSE history directly,
and only keeps 1-minute history for 7 days — so this is a small, short sample, reported
as a direction, not a verdict). Result: 3-minute did not produce a better outcome. The
typical stop got tighter (as expected — a shorter candle covers less range), which
mechanically increases what costs take out of each trade, and that increase outweighed
the earlier, tighter entries even before accounting for the well-known problem that
finer bars also mean more trades and more total cost drag. Not adopted for now.

**Exit (SL)** — Not affected.

**Exit (TP)** — Not affected. No change; see the note on stop-width scaling above.

**Consideration** — The 3-minute chart is executable if it is ever wanted: the broker
side supports it already (Angel, Firstock, Shoonya, Fyers and others all serve
3-minute bars), so nothing in the platform blocks it. The question tested here is
narrower — does it improve THIS crossover's entry — and on the evidence gathered it
does not, for the same reason a tighter stop always costs more in relative terms
regardless of what is generating the signal. This finding is directional, from one
week of data; it is consistent with the mechanism (smaller bars, smaller stops, bigger
relative cost) and with a comment already recorded in `ema9-intraday.pine`'s own
research notes that costs get worse at smaller timeframes, but it has not been checked
against a full quarter and should be re-run once more history is available.

On the watchlist: the first pass this morning fixed one wrong ticker and removed two
liquidity outliers, but did not check the actual ORB grade history before deciding what
to keep or add — that was a real gap, corrected now. Cross-referencing
`orb-performance-2026-Q1.md` changed three decisions: FEDERALBNK and BANKBARODA both
fell from a good Q1 grade to a bad one (A to D, and B to D) and are dropped, and
TATAPOWER graded D in both quarters and is dropped too — none of that showed up in a
liquidity-only check. APOLLOTYRE, which the first pass removed for having the thinnest
liquidity of any F&O name checked, is restored: it graded A in both quarters for
ORB/BREAKOUT, a real and repeated result that a liquidity screen alone would have
thrown away. The new additions were also re-checked for volatility, not just traded
value — a breakout strategy needs price range to trade, and several of the most liquid
names in the whole market (HDFCBANK, RELIANCE, ICICIBANK, BHARTIARTL, ITC) move
proportionally less than the stocks already on this list, so they were left out rather
than added on liquidity alone; TCS and INFY matched both criteria and were added.

**NOTE:** Timeframe comparison, ORB-45 universe, same 1-minute source data resampled
to each timeframe, 7-day window, swing stop / 2R target, 10 bps cost:

| timeframe | median stop (ATR% of price) | plain-cross net_R | wick-confirm net_R |
| --- | --- | --- | --- |
| 5-minute | 0.177% (median ATR) | -0.212 | -0.170 |
| 3-minute | 0.128% (median ATR) | -0.220 | -0.262 |

ATR% ratio 3-min/5-min = 0.72, close to the sqrt(3/5) = 0.775 predicted by treating
intraday moves as roughly Brownian. n=764 (5m) / 905 (3m) plain-cross trades, n=318 (5m)
/ 291 (3m) wick-confirm trades — small samples, one week only, direction not magnitude.

Watchlist corrections against `orb-performance-2026-Q1.md`:

| symbol | Q1 grade | Q2 grade | decision |
| --- | --- | --- | --- |
| FEDERALBNK | A | D | removed (was kept in the first pass on liquidity alone) |
| BANKBARODA | B | D | removed (same) |
| TATAPOWER | D | D | removed (was kept in the first pass on liquidity alone) |
| APOLLOTYRE | A | A | restored (removed in the first pass for #209/209 liquidity) |
| SBIN, HINDALCO, BPCL | A/A/B | untracked by Q2 | kept, flagged as stale-grade / watch |
| VEDL, INDUSINDBK | never graded | never graded | kept - liquidity and ATR% both above the ORB-45 median |

ATR% of price (60d, 5-min bars), candidates vs the ORB-45 watchlist's own median
(0.195%): VEDL 0.228 (1.17x), INDUSINDBK 0.208 (1.07x), INFY 0.207 (1.06x), APOLLOTYRE
0.204 (1.05x), TCS 0.194 (1.00x) - all added or kept. HDFCBANK 0.144 (0.74x), RELIANCE
0.144 (0.74x), BHARTIARTL 0.151 (0.78x), ICICIBANK 0.153 (0.79x), ITC 0.136 (0.70x) -
all left out despite ranking in the top 6 by traded value across all 209 F&O names.
KOTAKBANK 0.171 (0.88x), AXISBANK 0.170 (0.87x), NTPC 0.168 (0.86x) - added as a
reasonable but not exceptional fit, close to the existing median.

Confirmed against `orb.pine` (`orb15Obj = ORBData.new(minutes=15)`, tooltip "ORB 15:
First 15 minutes - balanced signals (RECOMMENDED)"): the "15" in "ORB 15" is the
opening-range window length, independent of chart candle size. `or_minutes`/`orMinutes`
were already 15 in every place touched this session (`ema9_vwap.py`, `ema9-vwap.pine`);
no correction needed there.

---

### 2026-09-12 20:42 — PDF review (wick-confirmation is a real lead, PDF's exit is not), watchlist reviewed

**What changed**

Two inputs were checked against the strategy: a PDF describing a "VWAP + EMA Cross"
system, and a static TradingView watchlist the user had already built for ORB and
breakout, intended to also drive this strategy. Nothing in the script changed — this
entry records what was checked and what, if anything, is worth carrying forward.

**Entry** — One real, testable idea came out of the PDF: instead of firing on any
close-to-close sign flip between the 9 EMA and VWAP, require the signal candle to have
actually touched VWAP with its wick (not just its close). This was implemented and
measured, not just discussed. It is the first filter in this whole project to score
"helps in both windows" on the ORB watchlist — but it does not reproduce on the wider
208-stock F&O universe, and it stays a net loser after costs everywhere it was tested.
Full numbers below. Not adopted, but the closest thing to a lead this analysis has
produced, and worth re-testing once more forward data exists. The PDF's other
"confluence" idea — requiring the wick to touch both VWAP and the 9 EMA — turned out to
be numerically identical to the wick check alone: at the exact moment of a crossover,
the two lines are so close together that a wick reaching one always reaches the other.
No second filter there.

**Exit (SL)** — Not affected. The PDF's stop concept (none stated beyond the exit rule
below) was not adopted.

**Exit (TP)** — Not affected. The PDF has no take-profit concept at all — it holds
until the exit rule fires, which was tested and rejected (below).

**Consideration** — The PDF's actual stated exit — close the trade the moment a candle
closes back through the 9 EMA, no stop, no target — was measured and is worse than what
is already in the script: win rate collapses to about 24% because it exits on ordinary
noise long before a real move develops. Not adopted.

The document itself is a sales page for a US options day-trading product (SPX, SPY,
QQQ, IWM, 3-minute candles, calls and puts), not a research write-up: it states a
"70-80% win rate" with no trade count, no date range, no drawdown, and no way to check
the number. Several of its instructions do not transfer here regardless of that: the
3-minute timeframe and US-index-only universe are a different market structure, and its
"wait 2 minutes into the candle before acting" workaround solves a problem this script
does not have — it exists because their system must act before a 3-minute candle
closes, whereas this script only ever acts on a fully closed 5-minute candle, by design.
One genuinely fair point in the document, unrelated to its win-rate claims: VWAP is
weaker on thin volume. That point directly informed the watchlist review below.

On the watchlist: one entry was a wrong ticker (`FEDERALBANK`, which does not exist on
NSE — the real symbol is `FEDERALBNK`) and would have silently failed symbol lookup.
Two names were liquid enough but have a documented poor track record for this family of
momentum strategies — ADANIPOWER (Q1 grade D, dropped from the ORB watchlist by Q2) and
MANAPPURAM (named specifically as a drag worth removing in the ORB cost analysis) — both
removed rather than carried into a third strategy on the strength of liquidity alone.
APOLLOTYRE was dead last for liquidity out of all 209 F&O names checked (about 1/40th
the traded value of the top names), which is the exact failure mode the PDF itself warns
about, so it was dropped too. Ten of the most liquid F&O names not already on the list
were added (HDFCBANK, ICICIBANK, RELIANCE, BHARTIARTL, AXISBANK, INFY, KOTAKBANK, ITC,
NTPC, TCS) — all comfortably liquid and spread across banking, telecom, IT, energy and
FMCG rather than concentrated in one sector. BHEL was deliberately left out despite
ranking 27th on liquidity: it is hard-blacklisted for ORB and BREAKOUT in config.yaml
for a 0% win rate, and there is no reason to expect a fresh crossover strategy to fare
differently on a name that whipsaws that badly.

Worth being honest about the ceiling here: better symbol selection cannot fix a
strategy with no measured directional edge (the symmetric MFE/MAE finding from
2026-09-10 stands regardless of which stocks it runs on). What this review buys is
running on names where the signals are at least real signals rather than thin-volume
noise, and where a name's own track record with this family of strategy is not already
known to be bad.

**NOTE:** Ablation, ORB-45 universe, wick-confirmation vs the plain-cross base already
on file:

| variant | n IS | bps IS | n OOS | bps OOS | helps |
| --- | --- | --- | --- | --- | --- |
| base (plain cross) | 1,556 | -1.51 | 1,022 | 1.53 | - |
| wick confirmation | 637 | 0.65 | 427 | 4.95 | **BOTH** |
| confluence (wick both lines) | 637 | 0.65 | 427 | 4.95 | BOTH (identical to wick-confirm) |
| PDF's EMA-break exit | 2,152 | -0.47 | 1,402 | -1.83 | IS only |
| wick-confirm + EMA-break exit | 776 | 0.69 | 513 | -0.01 | IS only |

Wick-confirmation net_R, despite the gross improvement, stays significantly negative:
IS -0.185 (t=-3.82), OOS -0.203 (t=-3.44), full period n=1,064. Stop tightened alongside
the gross gain (stop_pct 0.455% vs base 0.622%), so cost drag rose (0.10/0.455 = 0.220R)
almost as fast as the gross edge did. Sweeping stop style on top of wick-confirm (swing,
ATR x1.0/1.5/2.0, all at TP 2R) found no combination with positive net_R; swing (the
existing default) remains the least-bad at -0.192 full period.

Wick-confirmation did NOT reproduce on the full 208-symbol F&O universe: OOS gross bps
collapsed from +4.95 (ORB-45) to +0.03 (F&O-208), n=2,052, net_R -0.259 (t=-9.87). Per
`by_symbol()`, results are carried by a handful of names (HYUNDAI +0.51R/trade,
MOTHERSON +0.44R, BANKINDIA +0.55R, n=14-24 each) against a long negative tail
(PIDILITIND -0.71R, ULTRACEMCO -0.72R, BAJAJ-AUTO -0.76R, n=26-32 each) — the ORB-45
result reads as noise from that 45-name slice rather than a reproducible edge.

VWAP source: PDF specifies OHLC/4 against the script's HLC3 (Pine's `hlc3`, the ta.vwap
default). Median absolute difference across 45 symbols is 0.0083% of price (p95
0.039%) — economically negligible on its own. But because a crossover happens exactly
where the EMA-VWAP gap is near zero, that tiny formula difference flips which single
5-minute bar registers as "the crossing bar" 67.9% of the time (4,287 of 6,311
crossings). Source choice does not move the level meaningfully but does reshuffle
signal timing almost every time — a sensitivity worth knowing, not a lever worth tuning.

Watchlist liquidity (median daily traded value, `Close x Volume` summed per session,
5-min bars, 60d), rank out of 209 F&O names: SBIN #6, HINDALCO #18, TATASTEEL #22,
VEDL #31, NATIONALUM #55, ASHOKLEY #60, BANKBARODA #62, PFC #66, FEDERALBNK #94 (once
corrected from the nonexistent FEDERALBANK), BPCL #96, RECLTD #98, TATAPOWER #109,
INDUSINDBK #114, JSWENERGY #147; removed ADANIPOWER (#21 liquidity, but Q1 grade D /
GONE by Q2 per `orb-performance-2026-Q1.md`), MANAPPURAM (#145, named explicitly in the
2026-03-13 cost analysis as a symbol worth removing), APOLLOTYRE (#209 of 209, last
place). Added, by liquidity rank: HDFCBANK #1, ICICIBANK #2, RELIANCE #3, BHARTIARTL
#5, AXISBANK #9, INFY #7, KOTAKBANK #17, ITC #32, NTPC #36, TCS #12.

Reproduce: `/tmp/.../scratchpad/pdf_concepts.py` and `liquidity.py` (session-scratch,
not checked in — rerun against fresh `data.load()` output to reproduce these numbers).

---

### 2026-09-10 03:05 — ORB watchlist: anticipate vs wait, and where stops/targets belong

**What changed**

Nothing in the code. This is a measurement entry, run on the 45-stock ORB watchlist
rather than the wider F&O list, to answer two questions: should entries be taken early
as the lines converge, or only once they actually cross; and where the stop and target
belong. Defaults were already set the way the evidence points, so none were altered.

**Entry** — No change. Waiting for the actual crossover remains the default, and the
measurement now supports that directly rather than by assumption. Entering early does
genuinely get a better price — that part of the idea is correct and was confirmed —
but the size of the prize is the problem, explained below.

**Exit (SL)** — No change. The stop stays at the recent swing extreme, which works out
around 0.57% of the share price. That width turns out to matter more than where exactly
it sits, for cost reasons described below.

**Exit (TP)** — No change. Twice the risk remains the default; targets between 1.5x and
3x behave almost identically, so there is nothing to gain from moving it.

**Consideration** — Three findings, in order of importance.

First, and most decisive: after a crossover, price travels almost exactly as far against
the trade as it does in its favour. Measured with no stop and no target at all, the
typical best-case run is 2.11 units of daily range and the typical worst-case run is
2.04 — a ratio of 1.03, where 1.00 means no directional information whatsoever. That
holds at every level checked, not just the average. This is why no arrangement of stop
and target can rescue the idea: you are placing brackets around a symmetric outcome, so
every choice returns the same nothing, minus costs.

Second, on entering early. The better fill is real and was measured: entering as the
lines converge gets a price roughly 0.19 to 0.39 units of daily range better than
waiting. Converted into risk terms, that is worth about 0.06R at the tight setting and
0.13R at the loose one. But one round trip in brokerage and slippage costs about 0.17R.
**The entire prize from anticipating is smaller than the cost of a single trade.** On
top of that, between 19% and 33% of these early signals never go on to cross at all,
so anticipating also buys a batch of trades the patient rule would simply never take.
Net result on the watchlist: essentially identical to waiting. The better price is real;
it is just too small to matter, and it is bought with worse signals.

Third, on stop width. Costs are a fixed percentage of the trade, so the narrower the
stop, the larger those costs loom relative to what is being risked. A 0.25% stop hands
over 0.40R per trade before anything else happens; a 0.90% stop only 0.11R. This creates
a trap visible in the results: one tight-stop setting is the only one that looks
positive in both halves of the data, which is normally the test that matters — but it
looks positive only before costs. After costs it is markedly worse than the wider swing
stop. Passing the both-windows test is necessary, not sufficient; the cost line has to
be cleared too, and here it is not, by a wide margin.

A note on the watchlist itself: on these 45 stocks the plain crossover reads negative in
the first half of the data and positive in the second, which is the exact opposite of
what the same rule did on the broader F&O list. A genuine edge does not change sign when
you change the stock list or the date range. This one does, twice.

**NOTE:** Universe 45 ORB watchlist names, 5-min, 2026-06-04 to 2026-08-28, 36 IS / 25
OOS, 10 bps.

Plain cross, n=2578: -1.51 bps IS / +1.53 OOS (-0.30 full), net_R -0.183. On the F&O 208
the same config was +1.36 IS / -0.41 OOS. Sign flips across both universe and window.

Anticipate (band 0.15), n=2298: -1.02 IS / -0.33 OOS, net_R -0.181. Statistically
indistinguishable from the cross on net_R despite the better fills.

Approach-event decomposition, HOR=6 bars (30 min), measuring every "EMA inside band and
narrowing" event:

| band | approaches | went on to cross | false start | median fill adv | in R |
| --- | --- | --- | --- | --- | --- |
| 0.15 ATR | 4,431 | 80.9% | 19.1% | 0.186 ATR | 0.063 R |
| 0.25 ATR | 5,749 | 74.2% | 25.8% | 0.293 ATR | 0.099 R |
| 0.35 ATR | 6,658 | 67.5% | 32.5% | 0.390 ATR | 0.132 R |

Conversion: median ATR 0.195% of price, median stop 0.574% of price, so 1R = 2.95 ATR.
Cost drag at 10 bps = 0.10/0.574 = **0.174 R/trade**, which exceeds the best-case fill
advantage of 0.132 R.

MFE/MAE from a next-bar-open fill, held to the 14:45 exit, n=5,322 cross entries:

| percentile | MFE (ATR) | MAE (ATR) |
| --- | --- | --- |
| p25 | 0.92 | 0.88 |
| p50 | 2.11 | 2.04 |
| p75 | 3.83 | 3.72 |
| p90 | 6.02 | 5.76 |

Median ratio 1.03. Symmetric at every percentile — the structural reason no stop/target
grid can find an edge.

Stop x target grid, OOS gross bps: swing and vwap stops are numerically identical
(min(vwap, swing) almost always selects swing, so the "vwap stop" option is effectively
dead code in practice — left in place because it is the option that matches the premise,
and its equivalence is itself the finding). Best cell in the entire grid is
`atr x1.0, TP 3R` at +2.55 bps OOS / +0.37 IS — still four times below the 10 bps cost
line, and its net_R is -0.303 because the 0.257% stop drags 0.39R. Swing stop nets
-0.173 to -0.191 across all targets. tp_r 1.5/2.0/3.0 are within noise of each other.

Cost drag reference: stop 0.25% -> 0.400 R; 0.40% -> 0.250 R; 0.62% -> 0.161 R;
0.90% -> 0.111 R; 1.20% -> 0.083 R.

---

### 2026-09-10 02:35 — Chart stripped back; IB/PW/PM added to the key-level filter

**What changed**

Two things. First, the chart is now bare by default: the 9 EMA, VWAP, and the small
triangles marking a crossover, and nothing else. Everything that used to draw itself
automatically — the coloured shading between the two lines, the previous-day and
opening-range levels, the session background tints, the entry/stop/target lines, the
trade labels and the dashboard — is still available but switched off, so it does not
fight with Initial Balance and Volume Profile layers added from other indicators.

Second, the optional "only trade near a key level" filter now recognises more levels.
It previously knew the previous day's high, low and close, plus the opening-range
edges. It now also knows the Initial Balance high and low (first 60 minutes), the
previous week's high and low, and the previous month's high and low. The filter is
still off by default.

**Entry** — Only when the key-level filter is switched on, which it is not by default.
With it on, a crossover is now accepted near a wider set of levels than before, so it
will permit somewhat more trades than it did yesterday. With the filter off, entries
are completely unchanged: still the plain crossover.

**Exit (SL)** — Not affected.

**Exit (TP)** — Not affected.

**Consideration** — A caution about the levels themselves, because widening the list
cuts both ways. Adding more levels makes the "near a key level" test easier to pass,
and a filter that passes almost everything is not filtering. Yesterday's measurement
already showed that requiring a key level made results worse in both halves of the
data, so this change makes an unhelpful filter slightly weaker rather than fixing it.
It is there so the idea can be looked at on a chart, not because the evidence supports
switching it on.

The bigger caution concerns entering early. Checking the actual TCS session this was
watched on, the whole day held exactly two crossovers — 10:55 and 12:30. The 12:30 one
matches the expectation of a long going into the 12:35 candle exactly. The 10:30/10:35
short does not exist: at 10:35 the 9 EMA was still meaningfully above VWAP and did not
reach it until 10:55. Getting the script to fire where the eye saw it requires
loosening the "approaching" setting to roughly double its default, and testing that
range shows the in-sample result improving steadily while the out-of-sample result
gets steadily worse. In plain terms: the earlier the entry, the better it looks on
days you have already seen and the worse it performs on days you have not. That is
worth sitting with, because it is the most seductive part of this whole idea.

Volume Profile levels (VAH/VAL/POC) are **not** included. They need a volume-profile
engine inside the script, which is a real piece of work rather than a few lines, and
was not built without asking first.

**NOTE:** Visual defaults changed only; no plot was deleted. New inputs `showFill`
and `showSkipped` split the EMA/VWAP shading and the rejected-cross markers out of
`showLines`/`showCross` so each can be silenced independently. `showLevels`,
`showKeyLevels`, `showZones`, `showLabels`, `showTable` all now default false.

Level set for `require_key_level` extended with `ibH`/`ibL` (rolling extremes over
`ibMinutes`, default 60, built the same way as the opening range), and `pwh`/`pwl`,
`pmh`/`pml` via `request.security(..., "W"/"M", high[1]/low[1],
lookahead=barmerge.lookahead_on)` — the confirmed-bar idiom, so no lookahead. Each
group is individually toggleable (`useIb`, `usePrevWeek`, `usePrevMonth`, all default
true *within* the filter, which itself is off).

Anticipation band sweep, `mode="anticipate"`, gross bps IS / OOS:

| band | bps IS | bps OOS |
| --- | --- | --- |
| 0.15 ATR | 0.64 | -1.47 |
| 0.25 ATR | 1.95 | -2.86 |
| 0.35 ATR | 2.33 | -2.86 |
| 0.50 ATR | 2.32 | -3.53 |

Monotonic in both directions and in opposite directions — the signature of a knob
that buys in-sample fit with out-of-sample performance.

TCS 2026-09-09 crossings, computed from 5-minute bars: 10:55 down (gap -0.07 from
+0.39), 12:30 up (gap +0.13 from -0.59). At 10:35 gap was +1.835 = 0.312 ATR
(session ATR ~5.9 on a ~2205 price). The backtest adapter is unchanged by this entry;
the Pine-side level additions are not mirrored there because the filter they feed was
already measured as unhelpful.

---

### 2026-09-10 01:59 — Built and measured. Verdict: do not trade.

**What changed**

A new strategy was written from an observation made while watching TCS on the
5-minute chart: that price "respects" the 9 EMA against VWAP, so the 9 EMA crossing
below VWAP is a short and crossing above it is a long. It was then measured before
being wired to money, and the measurement says the idea does not work. The script
exists as a paper-trading and measurement tool, not as a live strategy. Its defaults
are the plain crossover, and every proposed refinement ships switched off.

The short version of why: at the moment the two lines cross, price is usually
finishing a stretch rather than starting a run. Over 29,492 crossings the price went
the "right" way only about 44 times in 100 — worse than a coin toss. The eye
remembers the times it kept going, because those are the ones that look dramatic on
a chart. The times it immediately snapped back leave no memorable mark.

**Entry** — Yes, this defines a new one. A position opens when the 9 EMA crosses
session VWAP, on a closed 5-minute bar, with no candlestick confirmation required.
Two optional entry variations were tested and both ship OFF because they made results
worse: entering 1-2 candles early while the lines are still converging, and requiring
the cross to happen near a key level (previous day high/low/close, or the opening
range edges). Trading is restricted to the first 15 minutes onward, no new positions
after 14:00, and at most 3 per day.

**Exit (SL)** — Yes. The stop sits just beyond the recent 8-bar swing extreme, padded
by a tenth of the average daily range. Two alternatives are selectable: a fixed
volatility-based distance, or a stop parked on the far side of VWAP itself — that last
one matches the premise most closely, since if price stops respecting VWAP the reason
for being in the trade has gone. A stop closer than 0.20% of the share price is not
sent at all, because the engine would reject it anyway.

**Exit (TP)** — Yes. A single target at twice the risk, closing the whole position.
Targets of 1x, 1.5x, 2x and 3x the risk were all tested and all lost money; 2x lost the
least, which is not the same as making money. There is an optional exit when the 9 EMA
crosses back the other way, also off by default.

**Consideration** — The most important thing on this page: **this strategy lost money
in testing, before broker charges were even counted.** Across 208 stocks and roughly
three months of 5-minute data it produced 11,815 trades and earned essentially
nothing, then went slightly negative in the portion of the data that was not used for
tuning. Trading costs are around 10 basis points per round trip and the strategy earns
well under 1, so in practice every trade is a small donation to the broker.

TCS on its own looked mildly encouraging — but on only 51 trades, which is far too few
to tell skill from luck. Separating a genuine edge this small from noise takes roughly
1,600 trades. When the four other large IT names were added the picture went clearly
negative. TCS is not special; it was simply a small sample viewed with hindsight.

Two of the refinements — the early entry combined with a key-level filter, and the
long-term trend filter — looked like the *best* ideas on the first half of the data and
the *worst* on the second half. That pattern is the classic warning sign of a rule
tuned to noise, and it is worth recognising, because it is exactly how a losing
strategy gets talked into production.

Reversing the whole thing and fading the crossover was also tested, since the data says
the cross mean-reverts. The reversal is real but far too small to cover costs, so there
is no trade in that direction either.

If this is run forward, run it in sandbox or against a blacklist first, and compare the
signals it actually logs against the backtest above rather than against memory.

**NOTE:** Universe 208 NSE F&O names, 5-minute bars, 2026-06-04 to 2026-08-26, 59
sessions split 35 in-sample / 24 out-of-sample, costs 10 bps round trip, engine
`min_sl_pct` 0.002.

Base (plain cross, swing stop, TP 2R), n = 11,815: gross **+1.36 bps IS / -0.41 bps
OOS**, net_R -0.168, t = -16.76 full period. Cost sensitivity 6/8/10/12 bps: net_R
-0.097 / -0.133 / -0.168 / -0.204 — negative everywhere, no sign flip to find.

Model-free follow-through after the cross, no stop/target model, "does price travel
k x ATR in the cross direction before k x ATR against":

| k (ATR) | n | follow-through | t vs 50% |
| --- | --- | --- | --- |
| 0.5 | 29,492 | 44.54% | -18.76 |
| 1.0 | 29,179 | 48.31% | -5.78 |
| 1.5 | 28,287 | 49.10% | -3.01 |

Significantly below the 50% driftless baseline at every horizon, converging upward as
the horizon widens — the signature of short-horizon mean reversion. This is the core
finding; the trade-level results are downstream of it.

`ablation()`, only BOTH counts as evidence:

| variant | bps IS | bps OOS | helps |
| --- | --- | --- | --- |
| base | 1.36 | -0.41 | - |
| anticipate (1-2 bars early) | 0.64 | -1.47 | neither |
| near key level | 0.99 | -1.16 | neither |
| anticipate + key level | 3.14 | -4.04 | IS only |
| 200 EMA trend filter | 2.04 | -4.02 | IS only |
| stop beyond VWAP | 1.29 | -0.40 | OOS only |
| ATR stop | -0.62 | 0.36 | OOS only |
| exit on recross | 0.13 | -0.33 | OOS only |
| TP 1R | -0.38 | -0.70 | neither |
| TP 3R | 1.88 | -0.44 | IS only |

Nothing scores BOTH. `tp_r` sweep OOS: 1.0/-0.70, 1.5/-0.61, 2.0/-0.41, 3.0/-0.44 bps.

Single-name and sector detail: TCS n=51, gross +7.14 bps but t=-0.60, IS +13.87 vs
OOS -2.48. IT basket (TCS, INFY, HCLTECH, WIPRO, TECHM) n=242, OOS -8.97 bps, t=-3.15;
every one of the five is negative in total_R.

Fade (`invert=True`), same costs: ATR stop 0.5x with 0.75R/1R/1.5R targets gives
-1.62 / -0.44 / +0.65 bps full period; swing stop at 1R gives +0.18 bps. Best case
+0.65 bps against a 10 bps cost line, and net_R between -0.26 and -0.46 throughout.

Exit mix at defaults: TIME_EXIT 45.0%, SL 39.6%, TP 14.6%, SL_GAP 0.7%. Fewer than one
trade in seven reaches its target, and nearly half are still open at the time exit —
consistent with an entry that does not initiate movement.

Reproduce:
```
uv run --group analysis python -m signal_engine.backtest ema9_vwap --full
```
