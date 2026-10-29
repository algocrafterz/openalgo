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

### 2026-10-29 15:51 — Hypothesis recorded, not acted on: trades paying at least 1:1 may do better
- **What changed:** Nothing in either strategy. On 2026-10-29 the runner refused all 22 signals
  because each paid less than 0.75 for every 1 risked. Replaying every signal since 2026-10-01
  as if there were no such rule showed no clear difference between the trades the rule lets in
  and the ones it blocks. One group stood out: trades paying at least 1 for every 1 risked
  averaged +0.27 of the amount risked per trade after costs, against roughly zero for the rest.
  That group is too thin to trust, so it is written down here as a question to test on new days.
  The minimum stays at 0.75 and no third paper version is started.
- **Entry:** Not affected. The minimum reward-to-risk of 0.75 stays as it is.
- **Exit (SL):** Not affected.
- **Exit (TP):** Not affected.
- **Consideration:** Of the 27 trades in that group, 10 came from one day (2026-10-28, a sharp
  recovery day) and made +0.70 each. The other 17 averaged +0.03, which is no different from
  zero. Raising the minimum to 1.0 would remove about three quarters of all signals, so a wrong
  guess costs a lot of trades. Do not change the rule on this evidence.

NOTE: Hypothesis H1, fixed before more data arrives: signals with signalled R:R >= 1.0 earn a
higher net R per trade than signals with R:R < 1.0. Only this one cut-off is tested; no other
bucket edges or side splits are to be tried on the same data, so the test counts as one trial.
Evidence so far (206 baseline signals, 18 days, 2026-10-01..10-29, 1m Historify bars, stop and
first target as signalled, stop wins a tie within a minute, open trades exit at the 14:45 bar
open, Rs 1,000 risk per trade, Flattrade charges + 4 bps slippage per side, no position caps):
buckets <0.50 n=48 +0.039R, 0.50-0.75 n=86 +0.056R, 0.75-1.00 n=45 +0.031R, >=1.00 n=27
+0.274R (t=1.8); passes-0.75 n=72 +0.122R, rejected n=134 +0.050R. Excluding 2026-10-28 the
>=1.0 group is n=17, +0.026R, t=0.2. Re-test on days from 2026-10-30 only, 2026-10-28
excluded from any pooled figure. Run it when at least 40 fresh signals at R:R >= 1.0 exist
across at least 15 days, or at the planned re-evaluation after about 20 paper sessions,
whichever is later. Pass: the >=1.0 group's net R per trade is above the < 1.0 group's, its
mean is positive, and its t-statistic is at least 2.0 with trades grouped by day. Fail or
inconclusive: drop H1, keep min_rr at 0.75. Only on a pass, raise `risk.min_rr` or add a
per-strategy gate (`validator.py` has one global `min_rr`, so a per-strategy value is a code
change). The replay script of 2026-10-29 lives only in the session scratchpad
(`rr_bucket_replay.py`) and is not in the repository; rebuild it from the rules above.

### 2026-10-29 12:45 — A stock first flagged after a quiet morning was being ignored: fixed
- **What changed:** The scanner remembers which stocks it has already flagged today, so it does
  not alert twice. It also skips alerts on the very first check of the day, because everything
  looks new then. It decided "first check of the day" by "nothing flagged yet today", not by "no
  check has run yet today". On 2026-10-29 nothing was flagged from 09:20 to 10:15, so the 10:20
  check was treated as the first of the day and its two stocks (PIDILITIND long, CHOLAFIN short)
  were silently dropped: no alert and no entry check. The scanner now counts any earlier check
  today, even an empty one, as the starting point.
- **Entry:** Changes which stocks reach the entry check. A stock first flagged after a quiet
  stretch is now alerted and can become a trade, as it always should have been. Nothing else
  about how an entry is confirmed changes.
- **Exit (SL):** Not affected.
- **Exit (TP):** Not affected.
- **Consideration:** The first check of the day is still silent, so there is no burst of alerts
  at startup. A pick that passes the entry check can still be refused by the runner's minimum
  reward-to-risk of 0.75 (`risk.min_rr`), which stays unchanged; 169 of 256 signals since
  2026-10-01 were refused that way, and all 16 on 2026-10-29.

NOTE: `store.record_hits(results, captured_at, baseline_exists=None)`; `__main__._watch` passes
`baseline_exists=store.has_snapshot_before(captured_at)`, true when any snapshot is stored
earlier that calendar day. `None` keeps the older rule (no hits earlier today) for the backfill.
Tests: `test_breakingtrade_store.py` (empty morning, cold start, same-day scoping). The poller
was restarted at 11:28:58 to load the fix; the two 10:20 stocks are not recoverable.

### 2026-10-28 17:40 — A closer stop for BREAKINGTRADE-RI tested on past data: rejected, no new version
- **What changed:** Nothing in either strategy, and no third paper version was started. The
  first paper day showed that RI's stop sits at the bottom of the first hour's range even though
  RI buys above its top, so every RI trade risks more than its first target pays. Two closer
  stops were replayed on RI's own entries: the middle of the first hour's range, and just under
  its top. Both looked better or equal on the recent weeks and clearly worse on the earlier
  weeks, which is the pattern of a result that will not hold up.
- **Entry:** Not affected.
- **Exit (SL):** Not affected. RI keeps the stop at the far side of the first hour's range.
- **Exit (TP):** Not affected.
- **Consideration:** A closer stop gives bigger positions and is hit more often. On past data
  that traded more without keeping more after costs. RI stays as it is; judge it against
  BREAKINGTRADE on several weeks of paper results.

NOTE: `experiments/2026-10-amt-replay/amt_replay.py` variants E17M (stop = IB midpoint -/+ 0.25
ATR) and E17H (stop = broken IB edge -/+ 0.25 ATR), both on E17's entries (E17 = BREAKINGTRADE-RI).
Expectancy in R per trade after Flattrade charges and 4 bps slippage per side (net4):

| Variant | 2026-09-24 onward: n, net4 R (t) | 2026-08-18 to 09-23: n, net4 R (t) |
|---|---|---|
| B (BREAKINGTRADE) | 110, +0.021 (0.47) | 55, -0.099 (-1.56) |
| E17 (RI, current) | 76, +0.013 (0.34) | 32, -0.026 (-0.39) |
| E17M (IB midpoint stop) | 144, 0.000 (0.00) | 70, -0.118 (-1.64) |
| E17H (IB edge stop) | 166, +0.089 (0.66) | 83, -0.345 (-3.77) |

Look-ahead check passed on every run, and the E17 rule audit passed on all three. E17H's recent
gain comes from shorts in a falling October (113 shorts, +0.148R) while its longs lost; in the
earlier period both sides lost (longs -0.473R, t -4.48). The earlier period was already used
as a holdout (2026-10-28 02:10), so it is a second sample here, not a clean out-of-sample
test. Nothing passes both periods, consistent with the 02:50 finding that selection, not
exits or stops, is the limit.

### 2026-10-28 17:17 — Two engine fixes from the first BREAKINGTRADE-RI paper day
- **What changed:** Two record-keeping and closing faults found in the 2026-10-28 paper review
  were fixed in the engine. Both apply to every strategy, not only BreakingTrade. (1) After a
  restart the engine took a trade's buy price from the broker's combined position, which other
  strategies on the same stock can move; it now uses the trade's own recorded fill. (2) The
  14:45 close sent one "close everything" request, which also closed other strategies' trades;
  it now sells only each strategy's own shares, one order per trade.
- **Entry:** Not affected.
- **Exit (SL):** A trade restored after a restart now measures its break-even stop from its own
  buy price. On 2026-10-28 the HDFCBANK runner stop sat about 0.5R too high because of fault (1).
- **Exit (TP):** Not affected. The 14:45 exit happens at the same time for the same trades; only
  the way the closing order is sent changed.
- **Consideration:** The engine's own P&L for 2026-10-28 is about Rs 450 too low for
  BREAKINGTRADE because of fault (1). The fill-backed Strategy P&L page was right all along.

NOTE: (1) `startup._restore_tracker_positions` now sets `fill_price` from the trades.db entry row
(`db.fetch_last_entry_trade` returns `fill_price`), falling back to the positionbook
`average_price` only when no fill was recorded. HDFCBANK was restored at 678.6 (EVWAP-EARLY-PY's
stop fill) instead of 675.15, booked 913.05 against 1,365.0; with APOLLOHOSP's 2.5 split this is
the whole 449.4 engine-vs-page gap. Tests: `test_startup_reconciliation_fixes.py`
`TestRestoredLegKeepsItsOwnFill`. (2) `tracker._close_strategy` replaces `close_all_positions`
(OpenAlgo `/closeposition`, account-wide). Each leg gets a MARKET order for
`min(tracked qty, net - other tracked legs not yet closed)`; nothing is sent when that is 0 or
less (a stop already filled, or another strategy's opposite position), with a CRITICAL alert.
The closing order id is kept for verification and booking, so a symbol another strategy still
holds no longer reads as "not closed". Tests: `test_time_exit_own_legs.py`. Reviews:
`signal_engine/docs/paper-monitoring/2026-10-28-BREAKINGTRADE*.md`.

### 2026-10-28 15:10 — Narrow or wide opening hour and older reference levels tested as entry filters: none holds up
- **What changed:** Nothing in the strategy. Four ideas were written down first and then tested:
  a narrow first hour (Initial Balance, 09:15-10:15) leads to better breakouts; an open outside
  yesterday's value area helps; an untested earlier POC (the busiest price of a past day) ahead
  of the trade pulls price toward it; entering away from yesterday's and the 5-day POC helps.
- **Entry:** Not affected.
- **Exit (SL):** Not affected.
- **Exit (TP):** Not affected.
- **Consideration:** On 10 years of balance-breakout trades and 44,000 ordinary breakouts, no
  filter kept the same sign before and after November 2023, and none made money after costs in
  both periods. The website's own IB% looked best in its narrow third for BREAKINGTRADE in both
  months, but on only 19 and 14 trades, which is too few to act on.

NOTE: `experiments/2026-10-ib-reference-levels/` (hypotheses pre-registered in its README).
Narrow / wide = bottom / top third of IB range / ATR14, cut on in-sample only; split 2023-11-08;
net after Flattrade charges + 4 bps slippage per side (11.55 bps round trip); t clustered by day.
- IDEA-034 narrow IB: IS +8.0 net (366) / OOS -4.7 (127); wide IB: -13.8 / -3.6.
- Open outside prior value in the trade direction: IS -6.0 / OOS -4.7, worse than inside (-1.8 / -2.5).
- Naked POC on the target side: no magnet in the 44,121-trade placebo (-11.3 / -5.6 vs -10.3 / -7.0).
- Entry vs prior / composite POC: untestable on breakouts (all but 15 placebo trades are beyond both).
- BREAKINGTRADE site ib_pct narrow third: P1 -8.6 (19) / P2 +17.6 (14); wide: -15.5 / -10.1.
  ib_pct rises during the IB, so it is likely IB as a percent of an average range (inferred, not
  confirmed). Look-ahead check: 0 mismatches on 60 + 25 recomputed trades.

### 2026-10-28 04:30 — Acting the moment a label appears gains nothing; the guide's extra scans tested
- **What changed:** Nothing in the strategy. Each label (tails, failed highs/lows, poor
  highs/lows, open drive, the live single-print count) and each ready-made scan from the
  vendor's guide that we do not run was tested from the FIRST poll at which it appeared, the
  earliest we can act, and again 10 and 20 minutes later.
- **Entry:** Not affected.
- **Exit (SL):** Not affected.
- **Exit (TP):** Not affected.
- **Consideration:** By the time any label first shows, the stock has already moved 0.4% to
  1.6% in that direction, and over the next 15 to 60 minutes it does no better than the average
  stock. Entering 10 or 20 minutes later gives the same result, so polling every 2 minutes (the
  site's own refresh) instead of every 5 to 15 would not gain points. None of the extra scans
  beat costs in both periods.

NOTE: `experiments/2026-10-mp-state-study/event_study.py` (direction per the guide,
https://breakingtrade.com/scanner-guide, fetched 2026-10-28; site refresh every 2 min, TPO
period 30 min). Signed excess vs the equal-weight universe; t clustered by day; P1 / P2:
- Move already made at first appearance: tails +37 to +52 bps, open drive +66 to +69, live print
  3-5 TPO +75 to +107, 6+ TPO +95 to +157.
- After it, +15/+30/+60 min: within about +-3 bps for almost every label; to 14:45 within +-10.
- Live print 6+ TPO, which the guide calls late: not worse than 3-5 (P1 +1.1 / P2 +9.3 up).
- Scan 10 live print + Trend: P1 -25.8 / P2 +31.5 bps (26 / 33 events) - regime-flipping.
- Scan 6 mirror (buy tail inside value on a Normal Variation up day, long): +7.2 (t 0.9) /
  +7.5 (t 2.2) - the only consistent one, below a round trip of cost. Scan 6 itself (short):
  +1.7 / +5.1.
- Failed high (short, the guide's reading): P2 -6.1 (t -2.1), i.e. the price kept rising.
- Scans 8 (poor high magnet) and 11 (range-day edge fade): too few events or within +-7 bps.
  Scan 9 (single print refill) has no direction in the data and was not tested; scan 13 (sector
  sweep) is a discretionary momentum/laggard choice and was not tested.

### 2026-10-28 03:40 — Every Market Profile state in the snapshots tested early in the day: no usable edge
- **What changed:** Nothing in the strategy. Every state the website reports (opening type,
  open drive, tails, single prints, poor highs and lows, day type, position against value and
  the prior day's range, volume pace) was tested on all ~230 F&O stocks at 09:35, 10:20 and
  11:01, against how the stock moved for the rest of the day beyond the average stock.
- **Entry:** Not affected.
- **Exit (SL):** Not affected.
- **Exit (TP):** Not affected.
- **Consideration:** No state moved stocks by more than a round trip costs, in both periods.
  The data describes the auction so far; it does not add information that the price has not
  already used. A better edge needs our own multi-day profile levels, not more filters on these
  columns. Three weak leads are listed in the note for a forward test, none proven.

NOTE: `experiments/2026-10-mp-state-study/` (`state_study.py`, `volume_study.py`,
`picks_by_volume.py`). Direction for each state fixed before running from auction theory; signed
excess vs the equal-weight universe from the next minute's open; t clustered by day; P1
2026-08-18..09-23 and P2 2026-09-24..10-27 (30-34 days with bars). Findings, to 14:45:
- Open Test Drive in its direction, read at 10:20: P1 +12.9 bps (t 3.90), P2 +7.7 (t 0.91). The
  only state positive and sizeable in both periods; still about one round trip of cost.
- Initiative openings fade after the IB: Open Drive in its direction at 10:20 -14.1 / -5.3 bps;
  opening above prior value -8.2 / -2.4; gap down -9.9 / -1.9 (t all under 1.6). Weak, but the
  same sign in both periods: the textbook continuation reading does not hold here after 10:20.
- Universe: a move since the open on LOW volume pace continued (10:20: P1 +4.5, t 1.64; P2 +11.3,
  t 3.55); a move on HIGH pace did not (+0.4 / +3.0). Among the scanner's own picks the same split
  showed nothing (all t under 1).
- Day type, tails, poor highs/lows, single prints and position against value: signs flip between
  periods or stay within about 10 bps.

### 2026-10-28 02:50 — The scanner's picks arrive after the move, not before it
- **What changed:** Nothing in the strategy. A check of the stock selection alone, with no
  entry, stop or target rule, over 680 picks on 35 days (2026-08-18 to 2026-10-27). By the time
  a stock is picked it has already moved about 1.2% in the picked direction since the open.
  From the pick onward it moves no further than the average F&O stock over the same minutes.
- **Entry:** Not affected.
- **Exit (SL):** Not affected.
- **Exit (TP):** Not affected.
- **Consideration:** This is why no entry or exit setting has turned the strategy profitable:
  the picks carry about 0 to 4 basis points of extra move, and a round trip costs about 10 to 12.
  The short trades' gains in October were the market falling, not the selection. Improving the
  strategy needs a selection that is early, not a better exit.

NOTE: `experiments/2026-10-amt-replay/selection_check.py`. Signed return in the pick's direction
from the first scanner poll after the pick (1m open), minus the same-direction mean return of
every NSE stock in Historify over the same minutes ("excess"):

| Horizon | All 680: raw | All: excess (t) | Long 331: excess | Short 349: excess |
|---|---|---|---|---|
| +30 min | +1.3 bps | +0.1 (0.08) | -2.0 | +2.2 |
| +60 min | +1.6 bps | +0.1 (0.08) | -2.6 | +2.8 |
| +120 min | +4.6 bps | -0.7 (-0.27) | 0.0 | -1.4 |
| to 14:45 | +13.1 bps | +4.2 (1.35) | +5.1 | +3.3 |

Move already made from the day's open to the pick: +124.5 bps (long +102.7, short +145.2).
Raw short returns to 14:45 were +28.2 bps (t 6.30) but only +3.3 beyond the market. "Live Print
in Formation Up" (56 picks) went the other way after the pick: -13.0 bps excess at 30 minutes
(t -2.47). The vendor's columns describe the day so far (day type, tails, position against
value); they are filled in by the move itself, which is why the picks come late. One regime,
equal-weighted benchmark, Historify as of 2026-10-27.

### 2026-10-28 02:31 — Second version BREAKINGTRADE-RI paper traded beside BREAKINGTRADE
- **What changed:** A second version runs on paper from today next to the current one, on the
  same scanner picks, so the two can be compared on live paper data. BREAKINGTRADE itself is
  unchanged. Every signal now also records which scan picked the stock and whether it would
  pass the range-integrity rule, so results can be split both ways.
- **Entry:** For BREAKINGTRADE-RI only: it enters only after 10:15, and only when a 5-minute
  bar closes outside the first hour's range (above it for a buy, below it for a sell) as well
  as beyond the signal bar. BREAKINGTRADE still enters on the first close beyond the signal bar.
- **Exit (SL):** Not affected. Both versions use the same stop rule.
- **Exit (TP):** Not affected. Both versions use the same targets, staged exits, stalled-trade
  check and 14:45 time exit.
- **Consideration:** RI often trades a stock BREAKINGTRADE already holds, a little later, so
  the paper account carries a larger position in those names. The replay says RI is better than
  today's rules but not profitable after costs; judge it on several weeks of paper results, not
  on a few days. It uses the retired watchlist Telegram channels, renamed.

NOTE: Tag `BREAKINGTRADE-RI`; channel `intraday-breakingtrade-ri-{analyze,live}` in config.yaml
(the retired watchlist chat ids; analyze enabled, live disabled). Code: `trigger.py`
`range_integrity_trigger` / `plan_trade_range_integrity` (reuse `stop_level`, `target_levels`,
`target_split`); `alerts.py` kind `trade_signal_ri`, `ri_enabled()`; `__main__._emit_trade_signals`
and `entry_watch.check` (per-version "already confirmed" state, one bars fetch per symbol per
poll); `tp_watch._WATCHED_STRATEGIES`; `strategies.REGISTRY`; `strategy_cards.CARDS`; config
`strategy_profiles` and `blacklist` copied from BREAKINGTRADE; `tracker._dedupe_breakingtrade_overlap`
drops the RI leg from the day summary on a symbol BREAKINGTRADE also traded, because the broker
nets the two into one position (trades.db keeps both). Tracking: trade_signal rows now carry the
real `scan`, `strategy`, `range_integrity` (`TradePlan.range_integrity`) and `entry_vs_ib`. Compare
with `uv run python -m signal_engine.analysis.breakingtrade.shadow --since 2026-10-28`. The replay
rule E17 is the same rule; the live trigger also sees the bar still forming at a poll, as
BREAKINGTRADE's always has. Tests: `test_breakingtrade_ri.py`, `test_breakingtrade_shadow_tracking.py`.

### 2026-10-28 02:10 — Range-integrity candidate (E1+E7) validated on past data; still not adopted
- **What changed:** Nothing in the strategy. The candidate from the entry below was tested as one
  unit: take a trade only after 10:15, once the first hour's range is complete, and only when
  the confirming 5-minute bar closes outside that range. Checks confirmed that the replay applies
  both rules to every trade and never uses a price from after the moment of the decision.
- **Entry:** Not affected yet. The candidate would skip about 3 in 10 of today's entries.
- **Exit (SL):** Not affected.
- **Exit (TP):** Not affected.
- **Consideration:** The candidate loses less than today's rules in both periods tested, but it
  is not profitable after realistic costs: about breakeven in the test month and a small loss in
  the earlier month. The earlier month was not used to choose the rule, but the strategy itself
  was built during it. Any further check has to come from new data, on paper.

NOTE: Code and commands: `experiments/2026-10-amt-replay/` (`amt_replay.py`, variants B, E1, E7,
E17). E1 alone measures the IB from bars before the trigger bar opened, so before 10:15 it uses
the IB still forming; E7 alone skips trigger bars starting before 10:15; E17 is both. Checks run on
every variant: each accepted decision is recomputed from bars strictly before the order minute
and must match exactly (poll, signal price, stop, TP, trigger bar); for E17 every trade is also
asserted to have a trigger bar starting at or after 10:15, a close beyond the complete IB edge in
its direction, and an order poll at or after that bar's close. All passed. The rebuilt E17 is
identical, trade for trade, to the "E1 as run" rows below.

After Flattrade charges and 4 bps slippage per side (8 bps in brackets); R = planned risk:

| Period | Variant | Trades | Exp. R | t | PF | Max DD | Long R (n) | Short R (n) |
|---|---|---|---|---|---|---|---|---|
| Test 09-24..10-27 | B | 96 | -0.047 (-0.095) | -1.21 | 0.71 | -Rs 3,025 | -0.141 (56) | +0.083 (40) |
| | E1 | 67 | +0.015 (-0.024) | 0.37 | 1.13 | -Rs 1,413 | -0.021 (32) | +0.048 (35) |
| | E7 | 94 | -0.042 (-0.090) | -1.06 | 0.73 | -Rs 2,320 | -0.135 (54) | +0.083 (40) |
| | **E17** | 67 | **+0.004 (-0.035)** | 0.10 | 1.03 | -Rs 1,413 | -0.044 (32) | +0.048 (35) |
| Holdout 08-18..09-23 | B | 55 | -0.099 (-0.172) | -1.56 | 0.58 | -Rs 4,264 | -0.088 (46) | -0.160 (9) |
| | E1 | 34 | -0.021 (-0.075) | -0.35 | 0.78 | -Rs 1,758 | +0.012 (28) | -0.172 (6) |
| | E7 | 54 | -0.052 (-0.124) | -0.72 | 0.77 | -Rs 4,032 | -0.056 (46) | -0.033 (8) |
| | **E17** | 32 | **-0.026 (-0.078)** | -0.39 | 0.76 | -Rs 1,837 | +0.014 (27) | -0.238 (5) |

Both periods together: B 151 trades at about -0.066R, E17 99 trades at about -0.006R. By NIFTY
direction at entry, E17 against B: test up -0.001 vs -0.058, down +0.009 vs -0.036; holdout up
+0.032 vs -0.066, down -0.135 vs -0.142. Rupee figures are not comparable across the two periods:
the holdout has no executed trades to estimate day capital from, so it sizes from the median.

Is the holdout clean?
- For choosing E1 and E7, yes. Both rules came from the test month (the entry-location analysis
  of the executed trades). No variant was run on 2026-08-18..2026-09-23 before the choice, and
  only B, E1, E7 and E17 were run there afterwards. Nothing was changed after seeing those results.
- For the strategy as a whole, no. The scanner's scans, its filters and the 2-hour confirmation
  window were designed and tuned on data from late August and early September, inside this
  period, so the baseline's own parameters are partly fitted to it.
- The scan list and vendor columns may also have differed in those weeks; picks are used exactly
  as the scanner recorded them. 16 holdout picks and 32 test picks had no 1m bars.
- The holdout has now been looked at. It cannot confirm any further change; only new data can.

Same assumptions across variants: every variant reads the same picks, the same Historify bars
(as of 2026-10-27 20:31 IST), the same poll schedule, the same exit simulator, the same sizing
rule and the same cost model. A variant changes only its own rule; where that rule moves the stop
or target (E3, E4, E5), position size follows the new stop as live sizing would.

### 2026-10-28 01:28 — Market Profile entry rules tested on past data; one candidate, nothing adopted
- **What changed:** Nothing in the strategy. Seven Market Profile (auction market theory) changes
  to how a trade is entered, stopped or targeted were tested one at a time on past scanner picks.
  Only one passed the rule set before testing: take a breakout only once price is actually
  outside the first hour's range. It also helped on an earlier month kept aside as a check.
- **Entry:** Not affected yet. The candidate would skip about 3 in 10 of today's entries: those
  where the "breakout" was bought while price was still inside the first hour's range.
- **Exit (SL):** Not affected. Moving the stop to just inside the broken range made results no
  better per unit of risk and the worst losing run five times deeper.
- **Exit (TP):** Not affected. Targets at the nearest prior-day or range-extension level left so
  little room that most trades were refused, and the rest lost.
- **Consideration:** Even with the candidate, the strategy roughly breaks even after realistic
  costs on the test month (+0.004 of risk per trade) and still loses slightly on the earlier
  month (-0.026). It is an improvement, not a profitable strategy. The Performance page shows a
  profit because sandbox fills include no slippage; about 2.7 bps of slippage per side erases it.

NOTE: Replay rebuilt from `scan_hits` (picks), not from executed trades, so a change of entry
rule changes which trades exist. It reproduces the live path: first capture per (symbol, scan);
signal bar = last 5m bar stamped at or before it; trigger = first completed 5m close beyond that
bar's extreme, seen at a scanner poll (`__main__.POLL_WINDOWS`) within 2 h; order at that poll's
1m open; IB, ATR (14 x 5m) and gates from what the poll could see. Exits, sizing and costs as in
the entry below (staged ladder, 90 min gate, Flattrade charges, 4 bps per side). Calibration on
2026-09-24 to 2026-10-27: 79 of 83 paper entries reproduced with the same side, stop and target,
90% within 5 minutes of the real order; the 17 extra trades are signals the engine refused for
reasons outside the strategy (T2T symbols, a capital fetch failure, order timeouts).

Decision rule fixed before running: against the baseline after costs, expectancy up by at least
0.03R, profit factor up, drawdown no worse, at least 30 trades, and the gain holding on both
sides or in both NIFTY directions at entry.

Test month, 2026-09-24 to 2026-10-27, after charges and 4 bps per side:

| Variant | Trades | Net | Exp. R | t | PF | Max DD | Long R | Short R | Rule |
|---|---|---|---|---|---|---|---|---|---|
| B: today's rules, rebuilt | 96 | -Rs 3,925 | -0.047 | -1.21 | 0.71 | -Rs 3,025 | -0.141 | +0.083 | |
| E1 as run (included E7's 10:15 rule, so E1+E7) | 67 | Rs 240 | +0.004 | 0.10 | 1.03 | -Rs 1,413 | -0.044 | +0.048 | pass |
| E2: 30-min period close beyond the IB edge | 100 | -Rs 3,845 | -0.041 | -1.12 | 0.72 | -Rs 4,156 | -0.114 | +0.023 | fail |
| E3: stop just inside the broken IB edge | 212 | Rs 531 | -0.042 | -0.60 | 1.01 | -Rs 14,060 | -0.367 | +0.176 | fail |
| E4: TP1 at the nearest AMT reference | 22 | -Rs 5,415 | -0.293 | -2.92 | 0.14 | -Rs 4,009 | -0.375 | +0.079 | fail |
| E5: Gap-Down Rescue as a gap-fill trade | 61 | -Rs 678 | -0.013 | -0.28 | 0.91 | -Rs 701 | -0.197 | +0.083 | fail |
| E6: scan time windows enforced | 57 | -Rs 6,359 | -0.131 | -2.48 | 0.38 | -Rs 4,363 | -0.158 | +0.012 | fail |
| E7: no trigger before the IB completes | 94 | -Rs 3,453 | -0.042 | -1.06 | 0.73 | -Rs 2,320 | -0.135 | +0.083 | fail |

Holdout, 2026-08-18 to 2026-09-23 (picks not used to form any rule):

| Variant | Trades | Net | Exp. R | t | PF | Max DD | Long R | Short R |
|---|---|---|---|---|---|---|---|---|
| B | 55 | -Rs 4,138 | -0.099 | -1.56 | 0.58 | -Rs 4,264 | -0.088 | -0.160 |
| E1 as run (E1+E7) | 32 | -Rs 809 | -0.026 | -0.39 | 0.76 | -Rs 1,837 | +0.014 | -0.238 (5) |

The first E1 run skipped any trigger bar starting before 10:15, which is E7's rule, so the rows
above labelled E1 are E1 and E7 together. E1 alone, E7 alone and the pair are separated in the
2026-10-28 02:10 entry.

- E1. The gain is the removal of entries still inside the IB (29 trades at -0.203R in the test
  month); on the trades both keep, E1 is no better. It beat B in 5 of 6 weeks and in both NIFTY
  directions at entry, and in the holdout it improved by +0.073R with half the drawdown. The idea
  came from looking at the test month, which is why the holdout exists; 32 holdout trades is thin.
- E2. Waiting for a 30-minute close beyond the IB adds trades and does not improve them.
- E3. More trades pass the R:R gate with a tighter stop and size grows; the net in rupees is
  positive only through size, the expectancy is unchanged and the worst run is -Rs 14,060.
- E4. Prior-day value-area edges, POC, PDH/PDL and 1.5x/2x IB extensions usually sit close to
  entry, so R:R falls under 0.75 and 249 signals are refused; the 22 left lost.
- E5. Treating Gap-Down Rescue as a gap-fill trade (stop under the rescue low, target the prior
  close) made its longs worse (-0.197R); the small overall gain is only from skipping 23 picks
  whose gap had already filled.
- E6. "Neutral Day Resolution" is defined for 14:00 onward, but the scanner does not poll from
  13:00 to 14:50 and the time exit is 14:45, so enforcing its window removes it entirely. It was
  the best scan only while firing outside its definition. The other scans' windows also hurt.
- E7. Only 2 trades change; neutral. It is still the correct rule: an entry before 10:15 sizes
  from an IB that has not finished.

### 2026-10-27 23:58 — Watchlist variant retired; replay of three proposed changes (none adopted)
- **What changed:** The watchlist variant (enter at the scan price, no confirming close) is
  switched off: it no longer trades, sends messages or records alerts. Its code, tag, tests and
  past trades are kept. Over 20 paper days it lost Rs 976 before costs on 168 trades, while the
  confirmed variant made Rs 3,195 on 83. Three possible improvements to the confirmed variant
  were tested on past data only, and none is adopted.
- **Entry:** Not affected for the confirmed variant. The watchlist variant no longer enters.
- **Exit (SL):** Not affected. A tighter stop at the inner edge of the tail made the same trades
  worse; a stop just beyond the tail turned out to be the stop already in use.
- **Exit (TP):** Not affected. Checking stalled trades after 60 minutes instead of 90 made
  results worse, so 90 stays.
- **Consideration:** These results are not proof of an edge. After realistic charges and
  slippage the confirmed variant loses about 0.05 of its risk per trade in the replay, all long
  trades together lose, and the short trades' gains came in a falling month. The "Gap-Down
  Rescue" scan accounts for most long losses; it is flagged for a separate test, not removed.

NOTE: Retirement. `config.yaml` `intraday-breakingtrade-watchlist-analyze` is `enabled: false`,
and `__main__._emit_trade_signals` builds the watchlist plan only while
`alerts.watchlist_enabled()` is true (that channel enabled for the current mode). Tested by
`tests/test_breakingtrade_watchlist_retired.py`.

Paper comparison, 2026-09-24 to 2026-10-27 (sandbox, trades.db, before charges). The final EXIT
row of a partly exited position already includes its earlier legs (`db.py`, `includes_leg_pnl`),
so a trade's P&L is its last row, not the sum of its rows. Summing rows overstated the confirmed
variant at Rs 5,715 and the watchlist at Rs 4,288.

| | Confirmed | Watchlist |
|---|---|---|
| Trades (symbol-days) | 83 | 168 |
| P&L before charges | Rs 3,195 | -Rs 976 |
| Win rate / profit factor | 54% / 1.38 | 49% / 0.96 |
| Largest drawdown | -Rs 1,380 | -Rs 4,811 |
| Green days | 12 of 20 | 9 of 21 |

On the 76 calls both took, the watchlist's earlier entry did better, but the 92 calls that never
confirmed lost money: the confirming close works as a filter.

Replay method (isolated; read-only on trades.db, breakingtrade.db and Historify 1m bars; the
script and CSVs were in the session scratchpad and are not part of the repo). Each executed paper
signal is re-entered at its order time and fill price, then exited as the engine does: SL-M at
the planned stop; staged targets 50/30/20 at 1.0/1.5/2.0 x the TP1 distance (`tp_watch`), with
the runner stop moved to each level minus 0.3 x its R (`main._replace_runner_sl`, extended tiers
on); loss cut at 20 min below -80% progress; no-progress gate below 20% progress (market exit if
the pace cannot reach TP by 14:45, else stop to the fill); time exit 14:45. Gates are evaluated
at a 1m bar close and acted on at the next open; a stop and a target in the same bar count as the
stop. Sizing is 1% of an estimated day capital over the stop distance with the 0.10 slippage
factor. Costs: Flattrade statutory charges (`portfolio.costs.india_intraday`) plus 4 bps slippage
per side (8 bps as stress). Calibration against the paper results on 81 matched symbol-days:
correlation 0.92, same sign 91%, median gap Rs 21; the replay is about Rs 8 per trade more
conservative than the paper fills. 5 trades had no 1m bars.

Results after charges and 4 bps per side (expectancy in R of planned risk):

| Variant | Trades | Net | Exp. R | t | PF | Max DD |
|---|---|---|---|---|---|---|
| Baseline (90 min gate, IB stop) | 81 | -Rs 3,060 | -0.045 | -1.14 | 0.72 | -Rs 2,593 |
| No-progress gate at 60 min | 81 | -Rs 3,454 | -0.050 | -1.49 | 0.63 | -Rs 2,874 |
| Tail stop, beyond the tail extreme | 81 | identical to baseline | | | | |
| Tail stop, at the tail's inner edge | 123 | -Rs 4,285 | -0.043 | -0.95 | 0.80 | -Rs 3,680 |
| - same 81 trades only | 81 | -Rs 5,898 | -0.086 | -1.46 | 0.66 | -Rs 3,648 |
| - 42 signals it newly admits | 42 | Rs 1,613 | +0.042 | 0.66 | 1.35 | -Rs 2,228 |

Before charges the baseline is +0.020R per trade (Rs 1,613). Baseline by side: long 46 trades,
-0.129R (t -2.37); short 35 trades, +0.064R (t 1.17). By NIFTY direction, from open to the entry
minute: down -0.033R (41), up -0.058R (40). By NIFTY's full-day close (known only afterwards):
down -0.042R (58), up -0.053R (23). NIFTY fell 3.0% over the window, with 13 down days of 21.

- Tail stop. The scanner stores only a tail flag, never a price, so the tail is rebuilt from bars
  before the decision: the stretch at the day's extreme traded by only one completed 30-minute
  period, used only when the vendor's flag at or before the decision agrees with the trade's
  side. In all 55 trades with such a tail, the extreme was the IB extreme, so "just beyond the
  tail" is the stop `stop_level()` already places. The design note's claim that a tail stop "sits
  closer than the IB extreme" holds only for the inner edge, and that version made the same trades
  worse (-0.086R against -0.045R). Its total looks better only because 42 refused signals now pass
  the 0.75 R:R gate, 33 of them shorts in a falling month.
- No-progress 60 min. Worse on every measure; 90 stays.
- Gap-Down Rescue. 40 of the 46 long trades came from this scan: -0.147R (t -2.66, PF 0.30),
  negative whether NIFTY was rising or falling at entry. The other 6 longs were flat. All other
  scans: +0.054R (t 1.00, PF 1.58). One month, one regime, five tests on the same 81 trades: a
  candidate for a forward test, not a rule. Scan names are matched by the stock's first scan hit
  of the day, because signal rows do not store their scan.

Known limits. One-minute bars, against the engine's ~20 s polling. Same-day capital is estimated
from the day's own executed trades (affects rupees, not R). Newly admitted signals are entered at
the signal price at decision time, a fill the paper never had. No variant approaches the t >= 3.0
acceptance gate; at the baseline's spread that needs roughly 500 trades.

### 2026-10-25 13:38 — Exit safety: no selling into a fired stop, safer timeouts and time exit
- **What changed:** Before it sells, the engine now checks with the broker whether the protective
  stop has already sold some or all of the shares, and sells only what is still held. When a
  network timeout hides an order's confirmation, the engine now matches it only to that exact
  order, never to an earlier order of the same size. At the 14:45 time exit it first proves from
  the broker's order list that the stops are cancelled, and only then closes.
- **Entry:** Not affected.
- **Exit (SL):** A stop is never left working beside an exit. If the time exit cannot prove a
  stop is cancelled, it does not close that position: the stop stays in place and an urgent alert
  asks for a manual close before the broker's own 15:00 square-off. If a "no-progress" exit (the
  early close of a trade that is going nowhere) is rejected, the stop is put back at once.
- **Exit (TP):** A target exit that arrives after the stop already fired sends nothing, or only
  the shares still held. An exit whose confirmation timed out is never sent a second time.
- **Consideration:** A skipped time exit can leave a position open after 14:45 until it is closed
  by hand or by the broker (broker charges apply). Watch for "TIME EXIT SKIPPED" alerts. Profit
  and loss is no longer distorted by a failed balance read.

NOTE: `main._cancel_sl_before_exit` reads the stop's status when its cancel fails and flags a fill;
the exit then uses the broker's own quantity (flat: book the stop, partly filled: exit the rest).
`order_reconcile.parse_broker_stamp` reads sandbox and Flattrade (day-first) stamps, and every
reconcile passes the strategy and today's booked order ids (`main._reconcile_scope`).
`tracker._close_after_confirmed_cancel` gates each time-exit close on `_clear_working_orders`; a
skipped strategy is neither swept nor booked and stays tracked. The no-progress market exit sends
through `main._send_exit_with_retries` and restores the stop on a definite failure. A failed
`fetch_realised_pnl` returns None and keeps the realised baseline. Built on the stop-lifecycle and
exit-watch branches; their slot reservation enforces the open-position cap.


### 2026-09-18 02:02 — Fixed a duplicate watchlist call when a stock flickers off and back on a scan

**What changed:** A stock could get flagged as a fresh watchlist call twice in one day if it
briefly stopped matching a scan for one check and then matched again later (price/volume
sitting right on the edge of the scan's threshold). On 2026-09-17 this happened to BHARATFORG,
which fired the watchlist trade signal at 11:31 and again at 12:31 for the same scan, same day.
The system now remembers everything it has already flagged earlier THAT DAY, not just what it
saw on the previous check, so a symbol that flickers off and back on no longer counts as new.

**Entry:** Yes — this is specifically an entry-trigger fix. A stock that already triggered a
watchlist entry once today can no longer trigger a second, duplicate one from the same scan.
The reset still happens at midnight, so a genuinely new day starts clean.

**Exit (SL):** Not affected.

**Exit (TP):** Not affected.

**Consideration:** This reduces the number of watchlist trades taken on choppy days where a
stock sits right at a scan's threshold and flickers in and out — expect slightly fewer, but
never duplicate, watchlist entries per symbol per day going forward. Only 1 of roughly 13-14
new-hit events on 2026-09-17 was affected; this was not a systemic problem on that day, but the
code had no protection against a noisier day making it one.

NOTE: Technical detail in `signal_engine/analysis/breakingtrade/store.py`'s `previous_hits()`
docstring and `signal_engine/docs/PRD.md`'s 2026-09-17 review-fixes entry.

### 2026-09-10 18:37 — Now also trading the watchlist call itself, as a second, separately-tracked outcome

**What changed:** Until today, a scanner call only ever became a real trade after price PROVED
it right - closing beyond the level the scanner flagged. The watchlist notice itself ("this
name just matched a scan") was explicitly informational, never a trade. Starting today, the
system ALSO takes a trade on the watchlist call itself, the moment it fires - a second, fully
separate strategy running side by side with the original one, on its own Telegram channel and
its own slice of paper capital, so the two can be compared honestly rather than blended into
one number. Neither can affect the other's results.

**Entry:** Two entry styles now run in parallel. The original ("confirmed") still waits for
price to prove the call right first. The new one ("watchlist") enters immediately, at whatever
price the stock was trading when the scanner flagged it - no waiting, no confirmation. This
tests the belief that the scanner's own selection is already good enough on its own, without
needing price to agree first.

**Exit (SL):** Not affected - same stop-loss placement logic as the confirmed strategy already
used (just beyond the level that would prove the setup wrong).

**Exit (TP):** Not affected - same profit targets as the confirmed strategy already used.

**Consideration:** This new watchlist strategy has ZERO track record yet - it starts today with
no history, same as the confirmed strategy started with none in its first days. Both currently
run in PAPER mode only (simulated money, no real capital at risk). Going live on either one
later still requires reviewing its own results first, the same discipline already applied
before any other strategy here went live - and only one of the two should ever run with real
money at a time, not both together.

NOTE: Technical detail in `signal_engine/analysis/breakingtrade/trigger.py`'s
`plan_trade_watchlist()` docstring and `signal_engine/docs/PRD.md`'s 2026-09-10 18:37 Recent
Changes entry.

### 2026-09-09 16:10 — Found the real reason almost no trade signal had ever fired: the system only ever checked once, at the wrong moment

**What changed:** The scanner flags a stock, then waits for price to actually follow through
before calling it a real trade — that part was always correct. The bug was in *when* the system
looked: it only checked once, at the exact instant a stock was first flagged, when price
essentially never has had time to follow through yet. It then never looked again. The system now
keeps checking a flagged stock on every scan for up to 2 hours, the same way a person watching a
chart would keep watching rather than glancing once and looking away.

**Entry:** Real entries can now happen far more often. Replayed today's actual scanner output
(31 stocks flagged) with this fix applied and found 22 of them would have genuinely confirmed a
real trade if the system had just kept watching — versus the single real signal this scanner has
ever produced across its entire history before today. The bar for calling something a real trade
is unchanged: price still has to genuinely follow through, closing beyond the level the scanner
flagged, not just touch it. Nothing about what counts as "confirmed" was loosened — the system
is just now allowed to see confirmations that happen a few minutes to a couple of hours later
instead of only the ones that happen in the same instant.

**Exit (SL):** Not affected.

**Exit (TP):** Not affected.

**Consideration:** A flagged stock now has a 2-hour window to confirm before the system gives up
on it — chosen from today's data, where the slowest real confirmation took 94 minutes. A stock
that takes longer than that to follow through is treated as a different, staler setup not worth
chasing, not as a missed trade. This does not guarantee signals every day — it only removes a
mechanical bug that was silently preventing almost all of them; some days the market genuinely
won't offer a qualifying setup.

NOTE: Technical detail in `signal_engine/analysis/breakingtrade/entry_watch.py`'s module
docstring and `signal_engine/docs/PRD.md`'s 2026-09-09 Recent Changes.

### 2026-09-09 15:20 — Yesterday's fix was live all day, but still zero real signals — traced to a dead broker connection, not the scanner

**What changed:** Nothing in this scanner's own logic. Yesterday's fix (below, 13:52 entry) was
confirmed working, but the broker connection everything runs through was down for most of today,
so the fix never got a fair test until late afternoon.

**Entry:** Not affected — the entry-confirmation logic itself was not touched today.

**Exit (SL):** Not affected.

**Exit (TP):** Not affected.

**Consideration:** The system that connects this platform to the stock exchange lost its
connection sometime before this morning's market open and did not reconnect on its own — nobody
was told, so it silently affected every part of today's trading for about six hours, until the
whole system was restarted by hand mid-afternoon. During that window this scanner could not pull
the price history it needs to confirm any trade, so every stock it flagged stayed a watchlist
entry with nothing wrong in the flagging itself. Checked right after the connection came back:
pulling price history worked correctly and returned properly time-stamped data, which confirms
yesterday's fix is genuinely working — today's silence was the broken connection, not the fix
failing. The connection issue itself now has an automatic check that runs every 15 minutes and
reconnects on its own the next time this happens, so tomorrow should not repeat this.

NOTE: See `signal_engine/strategy_library/intraday/orb/breakout.md`'s 2026-09-09 15:20 entry for the
full technical postmortem (this outage also affected BREAKOUT/sandbox order execution) and
`signal_engine/docs/PRD.md`'s 2026-09-09 Recent Changes for the fix summary across both.

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
