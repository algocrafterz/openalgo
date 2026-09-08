# BreakingTrade strategy — working log and current design

Running record of what we built, what we measured, and what we decided. Newest log entries at
the top. Every claim is tagged with how much evidence stands behind it, because a lot of what
passes for knowledge in trading is folklore nobody ever checked.

**Plain-language note:** jargon is expanded on first use. "We do X" means the code does X
today. "We think X" means it is a belief we have not proved.

---

## Current design (as of 2026-09-04)

### What the tool actually is

BreakingTrade is a subscription website that watches all ~220 NSE F&O stocks and describes,
for each, *how the day is unfolding* — using Market Profile, a way of reading a day by where
price spent its time rather than just where it closed. We scrape its two tables (Intraday and
Volume) every few minutes and turn them into candidate lists.

It is a **stock-picking aid**. It tells you *what is in play*, never *when to buy*.

### Selection — which stocks

**Score, don't gate.** Most interesting columns are blank most of the time (open type present
on only 19% of rows, single prints on 11%). Demanding all of them at once would leave about 1%
of the universe. So each piece of supporting evidence adds to a conviction score and we take
the top of the list, rather than insisting every box is ticked.

But missing data is *not* neutral (measured 2026-09-04, 3,059 rows over 14 sessions):

| Evidence present | that day's move | share that trend |
|---|---|---|
| Tail | 1.53% | 61.3% |
| No tail | 0.80% | 41.7% |
| Single print | 1.85% | — |
| No single print | 1.03% | — |

A "tail" means price pushed to a level, was firmly rejected, and left a visible mark — evidence
one side won a fight there. Names showing one moved nearly **twice as far**. So these columns
earn heavy weight in the score.

**Hard filters — few, each justified:**

- **Day type must be directional** — Trend, Double Distribution, or Normal Variation. The
  vendor's guide calls Neutral and Normal "fade days" (trade *against* the extremes) and
  Non-Trend "don't trade".
- **IB% ≤ 90.** "IB" is the Initial Balance, the first hour's range; IB% is how much of a
  typical day's range that hour already used. Above 90, only **23%** of names had a directional
  day — wild and choppy, not trending.
- **Volume in the day's top quartile**, measured against the day's own cross-section rather
  than an absolute multiple (see the 11:30 log entry for why absolute was wrong).
- **The move must not already be over** — no more than 5 TPOs beyond the day's extreme, and
  today's change not already in the top 20% of the day's moves.

### Entry, stop, target, booking

| Decision | Rule | Why |
|---|---|---|
| **Entry** | First 5-min candle that *closes* beyond the signal candle's extreme; market order | A close filters out the false break where price pokes through and snaps back. Market not limit: this book measured only 30–50% fill rates on limit orders into momentum, and missing the trades that ran is the expensive error |
| **Stop** | Beyond the structural level, plus 0.25 × ATR | ATR (Average True Range) measures normal movement. The buffer is the anti-stop-hunt device: a stop sitting exactly on the obvious level sits where everyone else's does |
| **Stop level** | The tail when there is one, else the broken IB extreme | Vendor guidance: "Buy/Sell Tail = defined-risk stop just beyond the tail". Tighter stop, so more size for the same rupee risk |
| **Targets** | 1× / 1.5× / 2× the IB range | The guide's own ladder |
| **Booking split** | Trend day 25/25/50 · otherwise 50/30/20 | The guide gives opposite instructions: a Trend day is "hold with a trailing stop, never fade"; a Normal Variation is "trade the breakout, take profit early". One split for both either caps runners or gives back the quick ones |
| **Timing** | Entries only 10:15–13:00 | Before 10:15 the day type is a provisional guess; after 13:00 the guide warns the range is spent |
| **Sizing** | Existing risk engine, 1% of day-start capital | Unchanged |

### BTST (buy today, sell tomorrow)

Long only, for a mechanical reason rather than a statistical one: you cannot short overnight in
the cash market because you cannot deliver shares you do not own. The short side belongs to the
morning scans.

Requires: closed green · directional day type · delivery in the day's top quartile · volume
busy into the close · not a "ghost rally" (price up on no participation) · **and the data the
judgement depends on must actually be present**.

"Delivery %" is the share of the day's trading that people took real ownership of rather than
buying and selling within the day. High delivery suggests real buyers, not day-traders.

---

## Confidence ledger

| Finding | Evidence | Confidence |
|---|---|---|
| IB% > 90 means chop, not trend | 23% directional vs 63% in the 60–90 band, 3,059 rows | **Strong** |
| Narrow IB (<30) does NOT mean a bigger move is coming | Full-day move 0.68% vs 1.86% for wide IB; t = −8.93; negative on 14 of 14 days | **Strong** |
| Tails and single prints precede larger moves | 1.53% vs 0.80%; 1.85% vs 1.03% | **Strong** |
| Restricting BTST to directional day types helps | +0.44%/day excess, t = 1.85, 7 of 11 days | **Suggestive only** — would not survive correction for the variants tried |
| BTST has an edge overall | n=96; excess between −0.10% and +0.07% depending on benchmark; all abs(t) < 0.65 | **Unproven either way.** The test could only have detected an edge above ~0.5%, so it is close to uninformative |
| The intraday strategy has an edge | none | **Untested.** History is end-of-day only, so this cannot be answered from it at all |
| Sector alignment improves results | Longs in bullish sectors did *worse*, −0.58% vs −0.30%, t = −1.39 | **Weakly against** |

---

## Log

### 2026-09-08 13:52 — Found and fixed why not one real trade signal had ever fired

**What changed:** A bug in how price history was read meant the system could never actually
confirm a trade, no matter how good the setup looked — every single stock the scanner ever
flagged was structurally unable to become a real trade. That is now fixed.

**Entry:** Real entries can now happen. The underlying check ("did price actually break out and
hold, not just look like it might") had been comparing today's price history against the wrong
clock — off by exactly 5 hours 30 minutes, India's offset from world clock time — so the
morning-session check that opening range breakouts rely on could never find any data to look at,
and the whole process quietly gave up before it ever got to the "has price confirmed the move"
step. Checked this against today's real scanner picks: 16 of 19 stocks flagged today would now
correctly produce a trade plan, versus zero before the fix.

**Exit (SL):** Not affected directly, but follows from the fix — a stop-loss can only be set once
a trade actually opens, and trades were never opening.

**Exit (TP):** Same as above — not affected directly, but profit targets can only be set once a
trade opens.

**Consideration:** This has been broken since this scanner started emitting entries — every past
"why no signal" observation this week is now explained by this one bug, not by the market simply
not offering good setups. The live scanning process needs to be restarted to pick up this fix;
until it is, it is still running on the old, broken logic.

NOTE: `validate.fetch_bars()` parses OpenAlgo's history endpoint, which returns UNIX epoch
seconds (a UTC instant), into a naive pandas `Timestamp` — but never shifted it to IST, while
every consumer (`trigger.initial_balance()`'s `IB_START=09:15`/`IB_END=10:15` window,
`entry_trigger()`'s `signal_time` comparison) is written in naive IST on the assumption the bars
already were too. A bar genuinely stamped 09:15 IST arrived as 03:45, so the Initial Balance
window never matched a single row, `initial_balance()` returned `(None, None)` for every symbol
every day, and `plan_trade()` returned `None` before it ever reached the entry-confirmation
check. `fetch_bars()` had no direct test coverage — every other test in the suite builds
synthetic bars with already-correct IST timestamps by hand, bypassing this function entirely,
which is how the bug stayed invisible. Fixed with a single `+ 5:30` shift in `fetch_bars()`
itself, and added regression tests (mocking the HTTP response with real epoch seconds) plus an
end-to-end proof against `trigger.initial_balance()`.

### 2026-09-08 09:17 — Telegram messages were rendering misaligned; end-of-day summaries now grouped by outcome

**What changed:** Every table-style Telegram message (the watchlist digest, the BTST list, both
end-of-day summaries) was being sent as plain text, which Telegram displays in a font where
columns don't line up — so the neat-looking spacing in the code never actually looked neat on a
phone. These now render in a fixed-width font so the columns actually align. The two end-of-day
summaries were also reorganized to group stocks by whether the call was right or wrong, instead
of one mixed list, so the day's hit rate is visible at a glance.

**Entry:** Not affected.

**Exit (SL):** Not affected.

**Exit (TP):** Not affected.

**Consideration:** Purely a readability fix — no numbers, prices, or decisions changed, only how
the messages display.

NOTE: `alerts.send()` gained a `monospace: bool` parameter that wraps the message in a Markdown
code block and sets `parse_mode: "Markdown"`; applied to `alert_transitions`, `alert_btst`, and
both `eod_summary.py` functions. The EOD summaries now build `RIGHT`/`WRONG` (intraday) and
`WINNERS`/`LOSERS` (BTST) sections instead of a single list sorted by return.

### 2026-09-08 01:47 — TP checks now happen every ~20 seconds, not every 5-15 minutes

**What changed:** The system now checks stock prices against profit targets much more often —
roughly every 20 seconds all day, instead of only when the scanner does its regular 5-to-15-minute
check.

**Entry:** No change to how or when a trade is entered.

**Exit (SL):** No change — the stop-loss is always placed directly with the broker and triggers
instantly regardless of this fix.

**Exit (TP):** Profit-taking now reacts much faster to price moves, since it is checked roughly
every 20 seconds instead of waiting for the next scheduled scan.

**Consideration:** Still not instant like a person watching a live chart tick by tick — a
20-second gap remains — but it is a big step up from the old 5-15 minute gap.

NOTE: `tp_watch.check()` moved from inside the scanner poll (`_fetch_and_report`, gated on
`POLL_WINDOWS`) into the poller's own outer `while True` loop in `_watch()`, which already
iterates every `sleep(20)` seconds regardless of scan timing. It needs only `datetime.now()` and
OpenAlgo's own `/api/v1/quotes` endpoint (never the scanner's scraped price), so no scan-cycle
dependency existed in the first place — this was a wiring fix, not a new capability.

### 2026-09-08 00:45 — Profit-taking now happens in stages, with the stop-loss moving up as price runs

**What changed:** Previously every trade closed 100% at the first profit target. It now exits
gradually — a portion at each of three price levels — the same approach already used by the
other automated strategies (ORB, Breakout).

**Entry:** No change to how a trade is entered.

**Exit (SL):** Once part of the position is booked at a profit level, the stop-loss is walked up
to that level, so the remaining part of the trade only risks money already gained, never more of
the original capital.

**Exit (TP):** Instead of selling everything at the first target, the system now sells roughly
half at the first target, more at the second, and closes the rest at the third — letting winners
run further while still locking in some profit early.

**Consideration:** This mechanism is brand new and has not been exercised by a single real signal
yet — it should be watched closely the first few times it actually fires. The exact split (how
much is sold at each stage) is currently a fixed default rather than adjusted for how strongly
the stock is trending.

NOTE: Wired `trigger.py`'s already-computed 3-level target ladder (1.0R/1.5R/2.0R) and split
through to the engine's existing `ExitQtyPct`/TP1-TP1.5-TP2 staged-exit and runner-SL-ratchet
mechanism (`main.py`'s `_resolve_exit_qty`/`compute_next_tp`), via a new `tp_watch.py` module
playing the same role as ORB/Breakout's PineScript "TP HIT" alert. The split is a fixed default
(50/30/20) rather than the day-type-aware version `trigger.py` computes, since preserving that
would need correlating state across two separate processes (the poller and the trading engine) —
a deferred follow-up, not a limitation of this change itself. Also fixed a real bug found while
testing: an undelivered Telegram alert would have permanently stuck a position at its last level;
delivery is now required before a level counts as "done," so a failed send retries next poll.

### 2026-09-08 00:33 — Alerts now explain themselves in plain English; a daily report shows how the day's calls actually did

**What changed:** Two readability improvements. Every stock alert now includes a one-line, plain
reason (e.g. "gapped down but buyers stepped in and bought it back") instead of just a code name
like "GapDnRescue." And a new automatic end-of-day message shows, for every stock flagged that
day, whether it actually moved the way the alert expected.

**Entry:** No change to how or when a trade is entered — this only makes existing alerts easier to
read and adds a scorecard afterwards.

**Exit (SL):** Not affected.

**Exit (TP):** Not affected.

**Consideration:** The end-of-day report is purely informational — it never buys or sells
anything, it just reports afterwards how the day's calls would have done if every one had been
acted on.

NOTE: Added `alerts._SCAN_REASON` (a one-line description per scan, with a test asserting every
scan in `scans.py` has one) and threaded it into both the watchlist digest and the confirmed
trade signal, as a non-mandatory `Reason:` field `parser.py` already tolerates safely. Added
`eod_summary.py`: scores every symbol first flagged that day against its closing price (fetched
live via OpenAlgo), and separately summarizes whatever BTST positions settled that day straight
from the paper ledger. Both send once per day from the existing 14:45+ block.

### 2026-09-08 00:21 — Fixed two settings that could have silently blocked real trades

**What changed:** Two configuration values borrowed from a different, older strategy (ORB) were
quietly applying to this strategy too, without anyone deciding that was wanted. Both are now
specific to this strategy.

**Entry:** The scanner can now consider a stock of any price — it previously silently ignored
anything priced above roughly Rs 5,000 or below roughly Rs 300, and it has already flagged
several stocks outside that range (Maruti at ~Rs 12,700, for one).

**Exit (SL):** The minimum allowed stop-loss distance was loosened to match how tight this
strategy's stops actually are (0.20% instead of 0.50%) — the old, wider minimum could have
rejected a real trade signal outright before it ever placed an order, exactly as happened once
before to the Breakout strategy.

**Exit (TP):** Not affected by this change.

**Consideration:** Both settings had never actually been tested against a real trade signal, so
this is a preventative fix based on a strong analogy to a problem already found and fixed
elsewhere, not a bug confirmed in the act.

NOTE: Added `strategy_profiles.BREAKINGTRADE` to `config.yaml` (`min_sl_pct: 0.002`,
`min_entry_price: 0`, `max_entry_price: 0`), and built genuine per-strategy price-band support in
`RiskEngine` (previously the price filter was fixed at construction with no override mechanism
at all) using the same override pattern `validator.py` already uses for `min_sl_pct`.

### 2026-09-07 16:05 — Fixed the paper-trading scoreboard that was stuck, and made sure the robot always turns off after market close

**What changed:** Two independent fixes. The overnight paper-trading ledger (BTST) was never
actually closing out old positions and recording their results — it now does, automatically,
every day. And a hard safety switch was added so the data-collection robot always shuts itself
off after market hours, rather than depending on someone remembering to stop it.

**Entry:** Not affected — this is about how paper trades get closed and scored, and about the
robot's own on/off behavior, not about opening new trades.

**Exit (SL):** Not affected.

**Exit (TP):** Not affected — the fix is specifically about the BTST paper ledger's own overnight
close-out step, which has no stop-loss or profit target (see the earlier BTST entries).

**Consideration:** Before this fix, days of paper-trading results were silently sitting
unfinished, so nobody could actually see how the strategy was doing — that is now visible again
every day.

NOTE: `paper.settle_open_trades()` required a snapshot stamped exactly `15:30:00`, which only the
manual `--backfill` command ever produced; the live `--watch` poller's schedule stops at 15:10 and
never wrote one, so a position opened while `--watch` was the only data source could never
settle. It now settles against the latest snapshot of the first later trading day, and is called
automatically from the daily BTST block rather than only from the manual `--paper` command. Also
added `AUTO_STOP_TIME` (15:20): the poller now self-terminates through the same clean shutdown
path as SIGTERM/Ctrl-C, rather than relying on an external process to stop it.

### 2026-09-07 13:30 — Heartbeat false alarm fixed; structure-flip watch added; watchlist alerts now unmistakable

**The "no successful poll for 35 minutes" alarm from today was a false alarm, not a dead
poller.** The schedule has a deliberate ~2-hour gap between the 10:30-13:00 window and the
14:50-15:10 BTST window (nothing worth polling for over lunch), but the heartbeat check only
knew about "market hours 09:20-15:15" - so it fired every single trading day at ~13:21 and again
an hour later at ~14:21, for as long as that gap lasted. Confirmed identical on 2026-09-06 (two
false alarms) and today. Fixed: the heartbeat now only watches inside an actual scheduled poll
window, not blanket market hours. In plain terms - the watchdog no longer cries wolf during
lunch, so it stays trustworthy for the day it needs to catch a real failure.

**New: alert-only warning when an open BREAKINGTRADE position's own setup reverses.** A scan
match (e.g. Gap-Down Rescue) is a read of the tape at one instant; nothing previously re-checked
whether that read was still true after the trade was taken. Investigating a live example (TCS,
09:55 LONG) showed its own auction structure had genuinely flipped by 10:25 - the same
`buy_tail`/`rejection up` combination that justified the LONG had become `sell_tail`/`rejection
down`. `flip_watch.py` now compares every currently-open position's latest poll reading against
the direction it was entered on, and sends ONE Telegram warning the first time it reverses
(never repeats for the same position). Deliberately alert-only, not an auto-exit: this is Day 1
of the paper week, there is no evidence yet that exiting on a flip beats the existing SL (which
is already built from the same tail level), and closing early would just as easily cut a
position that recovers.

**Telegram alerts sent by `alerts.py` now carry a link back to the message that started it.**
Every send captures Telegram's own message id and stores it; `telegram_link()` turns that into a
`t.me/c/.../<id>` link a reader can tap to jump straight to the original alert. The flip warning
above uses this to link back to the original entry signal, so nobody has to scroll the channel
looking for it.

**The watchlist digest ("BT ... | N new") was visually indistinguishable from a real trade
signal.** Both used upper-case LONG/SHORT, and the digest's per-row shape ("LONG PNBHOUSING
1,166.0 BreakPDH") reads exactly like an actionable call on a phone notification preview. Fixed:
the digest now opens with "BT WATCHLIST ... -- no action, not a trade signal" and its side tags
are lower-case (`long`/`short`); an upper-case LONG/SHORT now only ever appears in a message the
engine will actually act on. Verified the direction LABELING logic itself was already correct
before this change - every scan's canonical `direction` in `scans.py`'s `ScanDef` matched the
heuristic used to color the digest, in all 12 cases, including Gap-Down Rescue (whose whole
thesis - "Gap Down + Rejection up + Buy Tail" - is genuinely bullish, not a naming trick).

### 2026-09-06 17:30 — Lifecycle notifications; analyze mode switchable from the CLI

The poller only ever spoke on FAILURE, so silence was ambiguous - a healthy poller and a dead
one looked identical, which is exactly how 2026-09-04 went unnoticed for an hour. It now sends:

- **START** - led by the trading mode, plus the schedule and any polls already stored today
- **STOP** - with the day's tally, so an empty day is distinguishable from a dead process
- heartbeat alarm (pre-existing) if no poll succeeds for 35 minutes in market hours

`poller.sh stop` sends SIGTERM, which by default kills the process without running the shutdown
path, so a SIGTERM handler was added - otherwise the channel never learns the poller went away.

**The mode banner has THREE states, not two.** The first version reported "unknown" as LIVE,
which fires the alarm on every ordinary weekend start when OpenAlgo is not running. An alarm
that cries wolf gets ignored, which would defeat the one guard protecting an enabled channel.
Now: PAPER / UNKNOWN-verify-before-the-open / LIVE.

Also added `--set-analyze on|off`, which toggles paper mode over OpenAlgo's REST API. It
deliberately does NOT call `database.settings_db.set_analyze_mode()` directly: the flag is a
column in settings.db but the running app caches it in process, so writing the row from another
process leaves the live app acting on the stale value while orders keep going wherever they were
going - the worst possible failure for a paper phase.

### 2026-09-06 17:00 — Intraday paper phase ENABLED (analyze mode), one week

`intraday-breakingtrade` is now `enabled: true`, so signal_engine takes every signal end to end -
sizing, entry, SL-M placement, staged TPs, time exit. **OpenAlgo must be in ANALYZE mode**, which
routes those orders to the sandbox rather than the broker.

**The safety of this entire phase rests on one setting.** Enabled channel + live mode = real
orders on a strategy whose only scored sample is 5 signals, 2 correct. Two guards added:

- the poller checks the mode at startup, logs it, and raises a Telegram health alert if it is
  anything but analyze;
- `--review` prints the mode as its **first line**, every day, and shouts if it is not analyze.

A live order and a sandbox order look identical in the logs until the money is gone, so the mode
is never assumed.

**Daily routine for the week:**

    ./signal_engine/analysis/breakingtrade/scan.sh --review    # intraday, end of day
    ./signal_engine/analysis/breakingtrade/scan.sh --paper     # BTST ledger
    ./signal_engine/analysis/breakingtrade/scan.sh --audit     # was collection complete?

`--review` reads TWO databases on purpose: `breakingtrade.db` holds what the scanner *claimed*,
`trades.db` holds what the engine *did*. They diverge whenever a signal is rejected for risk,
margin, a blacklist or a duplicate - and that gap is usually the most informative line in the
report. Reading only one would present intentions as outcomes.

**What this week is actually testing:** whether the plumbing works end to end - signals emitted,
parsed, sized, filled, exited, recorded. It is NOT testing whether the strategy makes money; a
week of intraday signals is nowhere near enough for that, and the evidence so far points the
other way.

### 2026-09-06 16:20 — Wired to signal_engine; BTST on paper, and the paper says no

**signal_engine integration complete.** The scanner now emits signals in the engine's own alert
shape, so no special case is needed anywhere in the pipeline:

    BREAKINGTRADE LONG
    Symbol: VOLTAS
    Entry: 1186.5
    SL: 1178.2
    TP: 1203.0

Verified end to end - `parser.parse()` returns a valid Signal from it. The symbol comes from the
scan; entry, stop and targets come from `trigger.py` using OpenAlgo bars. **If the bars are
unavailable or the entry trigger has not fired, no signal is emitted** - a selection without
levels is not a trade, and inventing levels to fill the gap would be worse than silence.

Config added: `BREAKINGTRADE` tag in strategies.py, a blacklist section, and the channel
`intraday-breakingtrade` in config.yaml with **`enabled: false`** - the same paper-phase pattern
`intraday-breakout` already uses. Everything is parsed, logged and scoreable; one word turns it
into real orders.

**BTST paper ledger built and seeded from the 13 stored sessions. The result is not encouraging:**

| | |
|---|---|
| Closed trades | 48 |
| Mean gross return | **+0.005%** |
| Mean NET return | **-0.185%** (after ~0.19% round-trip cost) |
| Win rate | 48% |
| Best / worst | +3.64% / -4.24% |

Gross is indistinguishable from zero, and after realistic costs the strategy **loses money**.
That is the clearest read yet, and it points the same way as every earlier measurement. The
ledger simulates exactly what the backtest did - buy the list at the 14:50 snapshot price, sell
at the next session's close, no stop, no target - so paper and backtest remain poolable.

Settlement is automatic: a position closes as soon as the next session's snapshot is captured.

### 2026-09-06 15:30 — Alerts split onto their own two channels

The alerts had been going to `intraday-breakout`, the channel `breakout.pine` already publishes
to - mixing two unrelated strategies' signals in one feed. Now routed per strategy:

| Alert kind | Channel | .env key |
|---|---|---|
| BTST list | btst-breakingtrade | `BREAKINGTRADE_CHAT_ID_BTST` |
| Intraday transitions, poller health | intraday-breakingtrade | `BREAKINGTRADE_CHAT_ID_INTRADAY` |

Two channels rather than one, and the reason is **attention, not tidiness**: the BTST message is
a single actionable alert per day with a hard 15:15 deadline, while the intraday feed is
exploratory research with no established edge. Sharing a channel means the message that must be
acted on within 25 minutes competes with a scroll of "here is something interesting", which is
exactly how a deadline gets missed. Separating the *analysis* never needed channels - the `kind`
column already does that.

**There is deliberately no fallback to a generic chat id.** Falling back on a missing key would
silently resume delivering into breakout.pine's channel, repeating the very problem. An
unconfigured channel now means "record it, do not deliver it", and a test pins that.

### 2026-09-06 15:10 — Weekend data corruption found and purged

The new `--audit` command immediately earned itself: it reported **21 polls collected on a
Sunday**. `is_due()` only ever checked the time of day, never the day of week, so the poller ran
all weekend — and the vendor keeps serving the last session's table when the market is shut. The
result was **6,944 rows of Friday's closing data stamped with Saturday and Sunday timestamps**
(360ONE at 1137.0 on all three days).

That is worse than useless. Anything computing a "next session" return would have seen a
fabricated 0% day between Friday and Monday, quietly diluting every forward-return measurement
taken from here on.

Two guards added, and the bad rows deleted (6,944 snapshots, 360 scan hits; 15 clean sessions
remain):
- **Trading-day check** — Mon-Fri only, applied in `is_due()` and `_due_marks()`.
- **Duplicate guard** — a snapshot byte-identical to the previous stored one is not saved. A
  weekday exchange holiday looks exactly like a trading day to a clock; only the data can tell
  you nothing happened.

Worth noting the shape of this failure: the audit tool built to catch *missing* data is what
caught *fabricated* data. Both are silent by nature.

### 2026-09-06 14:40 — Telegram live; autostart installed

**Telegram alerts are delivering.** Two defects found and fixed getting there:
1. The bot token had been pasted *with* the `bot` prefix (`bot8123...` instead of `8123...`).
   The API path is `/bot<TOKEN>/`, so this produced `/botbot8123.../` and a bare `404 Not Found`
   that is indistinguishable from an invalid token. The loader now strips a leading `bot`,
   surrounding quotes and stray CR from a CRLF `.env`.
2. `send()` returned a bare `False` on any non-200, hiding Telegram's own explanation. A 404
   means a bad token, 400 "chat not found" means the bot was never added to the channel, 403
   means it cannot post - completely different fixes. The description is now printed.
3. **The BTST alert fired six identical messages** - one per symbol - because delivery was
   coupled to recording. One message now covers the whole list, while a row is still stored per
   symbol so alerts can be scored per name later.

**Autostart moved to Windows Task Scheduler**, mirroring the existing openAlgo tasks rather
than inventing a second mechanism. `breakingtradectl.ps1` (start/stop/status/audit) reaches into
WSL exactly as `openalgoctl.ps1` does, and `createTaskBreakingTradePoller.ps1` registers three
tasks: AutoStart 9:10, Watchdog every 5 min 09:15-15:15, AutoStop 15:35.

The in-WSL cron entries added earlier were **removed** - two mechanisms doing the same job is a
debugging trap. The Windows task is strictly better here because it holds the WSL VM alive;
cron inside WSL cannot, since WSL shuts down shortly after its last process exits.

**Why a watchdog and not just an autostart:** on 2026-09-04 the poller died *mid-session*, which
a boot-time entry would never have caught. `start` is idempotent, so a five-minute watchdog is
safe and bounds worst-case loss to a single poll - which matters because intraday data cannot be
back-filled.

**Friday audit: 25 of 27 scheduled polls missed (93%).** New `--audit [date]` command reports
scheduled-vs-collected for any day, so this can never again be discovered days later by accident.

Collector hardening:
- Every attempt logged to `signal_engine/logs/breakingtrade_poller.log` (rotating weekly, kept 8
  weeks) with duration, row counts and full tracebacks.
- **Catch-up on restart**: a scheduled mark is still taken up to 4 minutes late if nothing was
  stored for it, so a bounced process resumes its slot instead of skipping it. On startup the
  poller reports how many polls it already has for the day.
- **Browser rebuilt on failure** - a dead session used to poison every later poll.
- **Heartbeat**: no successful poll for 35 minutes during market hours raises an alert instead of
  failing silently, which is exactly how Friday was lost.
- Nothing in the loop can be fatal; failures are logged and the next mark is still attempted.

Alerts (`alerts.py`): intraday transitions (only names ENTERING a scan) and the BTST list, sent
via the Telegram **Bot HTTP API** rather than signal_engine's Telethon client - the poller is a
separate process and sharing a Telethon session file risks corrupting it. **Every alert is
written to an `alerts` table whether or not delivery succeeds**, because the record is the
experiment and delivery is only a convenience. Needs `BREAKINGTRADE_BOT_TOKEN` and
`BREAKINGTRADE_CHAT_ID` in `.env`; without them alerts are still recorded.

**Intraday backfill is impossible - confirmed, not assumed.** MP Replay steps through TPO periods
for ONE symbol on an HTML `<canvas>`; it is a chart-reading trainer, not the cross-sectional
scanner table, and canvas pixels cannot be scraped. Day navigation yields exactly one snapshot
per day, at the close. **Intraday data is use-it-or-lose-it**, which is what makes collector
reliability the single most important component. End-of-day data remains fully backfillable
(`--backfill N`), so a future outage costs the intraday sample but never the EOD history.

### 2026-09-05 23:55 — Close report for Friday 04-Sep. The poller died and status lied.

**Collection failed.** Only three polls survived (10:57, 11:01, 11:16). The 14:50 and 15:05 BTST
reads never happened, so Friday produced no live BTST list.

Two compounding faults, both operational rather than strategic:
1. `pkill -f "breakingtrade --watch"` matches the shell running that very command, so the
   restart killed itself before starting the replacement (it exited 144, which was noticed and
   dismissed).
2. `pgrep -f "breakingtrade --watch"` matches its own command line too, so every status check
   afterwards reported "ALIVE" while nothing was running.

Fixed with `poller.sh` — start/stop/status tracked by PID file, verified against
`/proc/<pid>/cmdline`, detached with `setsid` so it survives the launching shell. Status was
confirmed to report NOT RUNNING when nothing runs before being trusted.

**The honest intraday sample: 5 signals, 2 correct.**

| First seen | Scan | Dir | Symbol | Move to close | Right? |
|---|---|---|---|---|---|
| 10:57 | Gap-Down Rescue | up | KEI | -0.80% | no |
| 10:57 | Live Print Down | down | UNITDSPR | +0.05% | no |
| 11:01 | Live Print Up | up | VOLTAS | -1.01% | no |
| 11:16 | Live Print Down | down | INDHOTEL | -0.17% | **yes** |
| 11:16 | Neutral Day Resolution Up | up | HYUNDAI | +0.96% | **yes** |

n=5 proves nothing either way. Market context: 90 of 220 names green, mean -0.11%.

A trap worth recording: scan_hits also holds rows stamped 15:30 from the EOD backfill, and
scoring those against the 15:30 close gives a flattering 17-of-25. That number is **circular** -
those signals were derived from the closing state and then measured to that same close. Only
signals detected *before* the move can be scored. This is the third look-ahead trap found in
this project; assume more exist.

### 2026-09-05 23:40 — Both BTST lists confirmed working; the M version is not tradeable
A list using TPO through the M session *can* be produced (`executable=False`) but only exists
after 15:30, by which time F&O continuous trading has been shut for 15 minutes. Friday's two
lists, from the same snapshot:

| | Names |
|---|---|
| Executable (K only, decide 14:45) | SWIGGY, BAJFINANCE, AXISBANK, ULTRACEMCO, GRASIM, GMRAIRPORT |
| Full TPO (K+L+M, after 15:30) | SWIGGY, PFC, NTPC, AXISBANK, GRASIM |

Three names overlap. The executable version *adds* BAJFINANCE, ULTRACEMCO and GMRAIRPORT - names
the full version silently drops because they carry no L/M data at all. So the tradeable list is
not a degraded copy of the full one; it is differently composed, and covers more of the universe.

### 2026-09-04 13:15 — BTST was not executable; rebuilt around the 15:15 cutoff
**The most important correction so far.** The list needed the L (14:45–15:15) and M (15:15–15:30)
volume sessions, which only exist at or after the close — so the decision could never be acted
on. The backtest bought at the close using data that only existed at the close: look-ahead bias.

Worse than assumed, because of a rule change we had not accounted for. **From 2026-08-03 the NSE
runs a Closing Auction Session, and for F&O stocks — our whole universe — continuous trading
ends at 15:15.** M *is* the auction. Sources:
[Zerodha bulletin](https://zerodha.com/marketintel/bulletin/249809/latest-intraday-leverages-mis-bo-co),
[CAS explainer](https://www.sahi.com/blogs/closing-auction-session-cas-explained-nse-bse-closing-price-rules-2026).

Rebuilt on the **K session (14:15–14:45)**, complete at 14:45, leaving ~25 minutes to place a
CNC order before 15:15. Measured cost of becoming executable — almost nothing:

| Version | Excess vs market | t | Tradeable? |
|---|---|---|---|
| K+L+M | +0.188% | +1.08 | **No** — look-ahead |
| K+L | +0.144% | +0.67 | Marginal, needs 15:15 exactly |
| **K only** | **+0.134%** | **+0.65** | **Yes, decide by 14:45** |
| K only, no delivery filter | +0.033% | +0.28 | Yes, but edge mostly gone |

Two side findings: dropping the delivery filter collapses the edge (+0.134% to +0.033%) while
quadrupling trades, so delivery is doing real work; and K is reported for **100%** of names
against **74%** for L/M, so the old rule silently discarded about **53 names per session**.
Poll schedule moved from 15:05/15:20 to **14:50/15:05** — the 15:20 poll produced a list that
could no longer be traded that day.

Still not statistically significant (t = 0.65). Executable and honest, not proven.

### 2026-09-04 13:10 — Poller made resilient
Each fetch now retries up to 3 times with 5s then 15s backoff, and refuses to retry a login
failure (credentials do not fix themselves). Backoff is deliberately slow: at ~27 polls a day
there is no need to hurry, and hammering a subscription site after a failure is how accounts get
blocked. A lost intraday poll cannot be recovered — the scanner keeps no intraday history.

### 2026-09-04 12:45 — Swing holding (1–15 days) tested: the signal does not survive the first day
Held the BTST selection for 1, 2, 3 and 5 sessions. Excess return over the market **decays and
then reverses**: +0.17% at one day, +0.15% at two, −0.24% at three, −0.42% at five (no result
statistically meaningful, all abs(t) < 0.6). Mechanically sensible — the selection is built from
one day's closing auction, and that is information with a roughly one-session shelf life.

Also tested whether names *repeating* on the list day after day (a multi-day accumulation read,
which would have a longer shelf life) do better. There is no population to test: the list
averages 4.3 names and only **0.7** of them carry over to the next day. The selection is almost
entirely fresh each session, so there is nothing persistent to hold.

Conclusion: this data does not support swing trading as it stands. A 1–15 day hold would need a
different input with a longer half-life — multi-day delivery trends rather than one day's close.
Note the sample only spans 14 sessions, so holds beyond 5 days cannot be tested at all yet.

### 2026-09-04 12:35 — Poller was silently failing; stale-page bug fixed
Polls succeeded at 11:01 and 11:16 then failed every 15 minutes until 12:35. Cause: `goto()` to a
URL differing only by its hash does not reload a single-page app, so the page drifted from a
clean boot and the grid eventually rendered a single "no records" row. Now every poll forces an
explicit reload, and an empty grid reports itself as such instead of as a malformed table.

### 2026-09-04 11:35 — Missing data is informative; scoring beats gating
Measured completeness of every column (open type 18.8%, tail 44.6%, single print 11.4%) and
tested whether absence carries information. It does, strongly — see the table above. Concluded
sparse columns should raise conviction when present rather than act as gates, and that any test
must compare like with like (never a filtered subset against the whole universe — an error made
and corrected earlier today).

### 2026-09-04 11:30 — Two over-filters found and removed
Requiring an open type discarded 182 of 220 names, and the volume gate compared the vendor's
per-session "elevated" threshold (1.2×) against a *cumulative* measure running near 0.3
intraday. Together they cut candidates to 1, and that one was an index. Open type became a
scoring input; the volume bar became a percentile of the day's own distribution.

### 2026-09-04 11:20 — The "coiled spring" belief fails at both horizons
Re-tested at the correct same-day horizon after the first test used the wrong one. A narrow
first hour precedes *smaller* moves, consistently. Noted a trap: Day Type "Trend" never appears
above IB 60, which looks like confirmation until you notice the vendor *defines* a Trend day as
having a narrow IB — circular, and proves nothing.

### 2026-09-04 11:00 — First live intraday collection
Poller started. Fixed a bug where the two scanners were stamped a minute apart, so the two
halves of one poll could never be paired in stored data.

### 2026-09-04 10:00 — BTST measured: no edge detected
14 sessions backfilled (the vendor's entire retained history). 96 simulated trades: picks
tracked the market almost exactly, and the sign flips depending on the benchmark. Also found
the historical view only reports closing sessions for the ~75% of names that were busy.

### 2026-09-04 02:30 — We had been scraping the free view all along
The scanners render for logged-out visitors with *different, reduced* data. Everything measured
before this point was the public view. Login is now verified by looking for the sign-in control
rather than by "did the table load".

### 2026-09-03 — Tooling built
Extractor, the vendor's 13 documented scans, volume row-shapes, BTST list, headless fetcher,
snapshot database, MFE/MAE validation harness.

---

## Open questions

1. **Does the intraday strategy work?** Unanswerable from history. Live collection began
   2026-09-04 and needs roughly 50 sessions before the answer means anything.
2. **Does a live 15:20 read match the settled historical read of the same day?** If not, any
   backtest built on historical snapshots uses information a live trader would not have had.
3. **Is the delivery threshold right?** The vendor's 70% bar matched one name in 213; we use
   the day's top quartile instead. Arbitrary, and untested.
