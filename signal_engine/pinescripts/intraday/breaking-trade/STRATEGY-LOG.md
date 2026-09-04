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
