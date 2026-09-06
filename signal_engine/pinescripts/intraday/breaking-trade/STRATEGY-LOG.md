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
