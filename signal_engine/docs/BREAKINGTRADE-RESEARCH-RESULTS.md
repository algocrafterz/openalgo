# BreakingTrade Telegram research: consolidated results

Date: 2026-11-13
Status: COMPLETED AND VERIFIED (research record). Final verdict on every candidate: Reject.
Scope: research only. Nothing is connected to execution, signals, sandbox or live trading. No repository change. All inputs were read-only. This file lives in a scratch folder and quotes only symbol names, percentages and header-structure descriptions (Protected Content).

Sources: the phase 1 report, the phase 2 report (tracks A and B), the MFE/MAE summary, the backfill plan and verify output, and the two full-history reports (A and B). Every figure comes from those files. Where two sources disagree, the text says so (see "Source disagreements" at the end of section 7).

---

## 1. Plain-English summary

The channel posts market-move messages. We asked: can a trader use them to enter trades that make money after costs?

First, we tested 4 trading days. One rule looked good: buy the biggest pre-open losers at the open (A4 long fade losers, +40.9 bps net). Another rule looked flat: buy stocks in the Big Move Alert list (B1 long, +3.9 bps net). We marked both "Investigate further". We also said 4 days are too few.

Then we downloaded the whole channel history (12,580 messages, from May 2020; the tradable message types start in 2026). We froze the rules and re-tested them on far more days: 59 sessions for Pre-Open (Track A) and 152 sessions for Big Move Alerts (Track B).

The good result did not repeat. A4 long fade losers lost 12.3 bps per trade on the other 60 sessions. B1 long lost 14.8 bps per trade from March to October. We also ran a very large search (21.6 million rule variants for Track A, 3.2 million for Track B). The best rules on the training data all failed on the test data. The search could not tell its best rule from luck.

Result: all candidates are Reject. Earlier "Investigate further" labels are superseded. Nothing was changed in any trading system.

## 2. Bottom Line

- Impact on trading, signals, sandbox and live: none. Nothing was connected, deployed or changed. No order was sent.
- Main finding: the channel messages give no repeatable edge after realistic costs (13.55 bps round trip at 5 bps slippage per side). The earlier positive result came from 4 sessions that all closed up.
- Decisions needed from the owner:
  1. Stop and record this result, or keep ingesting for a later re-test.
  2. Whether to fix the missing symbols (needs a broker session; not done).
  3. Whether to create an IDEA-036 entry (derived numbers only; not created).
- Next steps if you continue: ingest at least 30 new sessions that include down days, then re-test the frozen rules once, without tuning.
- Risk: low. The risk is to act on the 4-session result (+40.9 bps). It does not replicate. Costs, auction fills and short availability were simulated, not observed.

---

## 3. Timeline of runs

| Run | What was tested | Data | Result |
|---|---|---|---|
| Phase 1 (5 sessions with messages; 4 with price bars) | 9 pre-registered hypotheses H1 to H9: continuation, fade, volume filter, repeats, Pre-Open with volume list, gap continuation and fade, Initial Balance bias | 79 messages, 6 to 13 Nov 2026; Historify 1m bars for 6, 9, 11, 12 Nov | No Paper-trading candidate. H1 long and H4 long and H8 fade-long and H7 labelled Investigate further. Rest Reject. |
| Phase 2 Track A | Pre-Open F&O Movers: A1 to A6 (long gainers, short losers, cuts, fade, magnitude, volume-list overlap), entry E1/E2/E3 | Same 4 sessions; 71 of 100 Pre-Open rows testable | Only A4 long fade losers positive (+40.9 bps net, top 10, E2). Fails multiple testing. 4 up days only. |
| Phase 2 Track B | Big Move volume multiple: B1 continuation, B2 reversal, B3 buckets, B4/B5 regressions, B6 repeats | Same 4 sessions; 65 B1-long trades | B1 long break-even (+3.9 bps net). B2 loses at all costs. Others not assessable. |
| MFE/MAE study | Excursion, time-to-peak and practical exit grids for the two best candidates | Per-observation CSVs plus Historify 1m recompute | Mean gross favourable excursion is large but the average adverse excursion is of similar size. Gains rest on a few trades. Max recompute difference 5.0e-05 bps. |
| History backfill | Fetch the full channel history into a separate research DB | 12,615 messages fetched, 12,580 stored, ids 30 to 13395, 3 min 30 s | Done. Adds 2020 to Nov 2026. Tradable templates exist from March 2026 (Big Move) and August 2026 (Pre-Open). |
| Full-history A (Pre-Open) | Part 1: frozen rules on 59 sessions. Part 2: search of 21,578,400 configurations, train/test split | History DB; Historify 1m incl. 13 Nov | Every frozen rule negative. Top 5 train winners all negative on test. Reality check p = 0.63. All Reject. |
| Full-history B (Big Move) | Part 1: frozen rules on 152 sessions. Part 2: search of 3,196,800 configurations | History DB; Historify 1m incl. 13 Nov | All four rules negative, every 90 percent interval below zero at 1.5x. Top 5 train winners fail on test. Reality check p = 0.93. All Reject. |

---

## 4. Data

### 4.1 Message databases

- Live DB: `signal_engine/data/breakingtrade_channel.db`. It held 79 rows in phase 1 (the TSV export held 78; the extra row is id 13386). The backfill verification found 87 live ids; all 87 are present in the history DB (0 missing). The live DB was not modified.
- Research DB: `signal_engine/data/breakingtrade_channel_history.db` (git-ignored by `signal_engine/data/`).
  - 12,580 rows. Message ids 30 to 13395. Stored send times 2020-05-01 13:26:53 UTC to 2026-11-13 10:35:00 UTC. Duplicate keys: 0.
  - Edits: 354 rows carry an `edit_date` (read-only query run for this record). Only the latest text is stored.
  - Id gaps: the id span is 13,366 and 12,580 are stored, so 786 ids are missing (computed; deleted or text-less). Worst months: 2025-11 (43 missing of a 105 span), 2025-12 (28), 2026-01 (18), 2026-03 (23), 2026-05 (22).
  - Known missing ids from phase 1: 13321 to 13324 and 13342. Cause unknown.
  - UNKNOWN (no template matched): 9,274 of 12,580 rows, 73.7 percent (computed from the monthly table in the verify output). UNKNOWN is 100 percent for May 2020 to July 2022. It falls to 47.6 percent (Apr 2026), 26.7 percent (Jun 2026), 13.7 percent (Aug 2026), 0.3 percent (Sep 2026), 1.7 percent (Oct 2026) and 0.0 percent (Nov 2026).
  - Parsed totals by type: Open volume list 1,380; Big Move Alert 1,365; Initial Balance 163; EOD report 118; Breaking News 88; Pre-Open 70; F&O Events 64; Closing Bell 58.
  - Session-day check: 1,636 IST dates with messages; 1,613 session days in range; 46 session days with no message; 69 message days that are not session days.
- File-name correction: the file `telegram_ingest.db` does not exist. The live ingest DB is `breakingtrade_channel.db`; the research DB is `breakingtrade_channel_history.db`.

### 4.2 First appearance of each message type (month level, from the monthly count table)

| Type | First month seen |
|---|---|
| Open volume list | 2022-08 |
| Big Move Alert | 2026-03 (first alert 2026-03-20) |
| Initial Balance | 2026-03 |
| Breaking News | 2026-04 |
| Closing Bell | 2026-04 |
| EOD report | 2026-06 |
| F&O Market Events | 2026-08 |
| Pre-Open F&O Movers | 2026-08 (first parsed post 2026-08-06; ids 12190 to 13326) |

The volume line inside a Big Move Alert first appears on 2026-06-16 (217 of 475 June tradable lines have one; July onward almost all lines).

### 4.3 Backfill facts

- 12,615 messages fetched; 12,580 stored; the 35 not stored are, by the plan, text-less posts that the row builder skips (assumed from the plan, not checked line by line).
- Run time 3 min 30 s. Ids 30 to 13395.
- The run used a separate user-created session, not the live listener session. The live listener was not touched.
- Verification output (`verify.out`): 0 duplicate keys; 87 of 87 live ids present in history.

### 4.4 Historify 1m coverage

- Phase 1 and 2 loader: 212 symbols, 4 sessions (6, 9, 11, 12 Nov), each 375 bars per symbol. The 8 Nov Muhurat session (18,016 rows) is removed by the loader. 10 Nov is a holiday.
- Backfill plan: Historify range 2016-10-03 to 2026-11-12, 212 symbols.
- Full-history runs: Track A reports 214 symbols plus NIFTY; Track B reports 212 symbols. See disagreements.
- COCHINSHIP and GVT&D have no usable bars after 19 Oct.
- 2026-05-07 has no 1m bars for any symbol (12 Big Move lines lost). Track B also removed the Muhurat bars and 2 off-minute bars on 2026-05-11.

### 4.5 Missing symbols and share of lines affected

- Named absent from Historify (phase 1 and 2): COROMANDEL, CUB, DATAPATTNS, ENRIN, ICICILIFE, KARURVYSYA, NAVINFLUOR, SYRMA, UJJIVANSFB (9). Plus COCHINSHIP and GVT&D with no bars in the window (2).
- Full-history Track A adds DALBHARAT and ANANDRATHI to the absent names (10 symbols, 20 rows).
- Full-history Track B reports 16 absent symbols (85 lines); the names are in its data files, not listed in its report.

| Run | Lines or rows lost to missing data |
|---|---|
| Phase 1, Big Move | 24 of 123 tradable lines (19.5 percent); 10 of 100 Pre-Open rows |
| Phase 2 Track A | 71 of 100 Pre-Open rows testable (incl. the 20 rows of 13 Nov, which had no bars then) |
| Full A | 30 of 1,180 primary rows (2.5 percent): 20 absent symbols, 10 bar gaps |
| Full B | 85 absent-symbol lines of 3,140 tradable (2.7 percent, computed); 3,140 to 2,866 testable at +60 s (274 lost, 8.7 percent, computed) |

Missing names skew to large movers, so results are on a survivor-like subset.

### 4.6 The 13 Nov bar discrepancy

Phase 1 and phase 2 found no 1m bars for 13 Nov, so 13 Nov was untestable. Both full-history runs found 13 Nov bars (375 per symbol, full day; no bars for COCHINSHIP and GVT&D). The full runs used them. Full A treats 13 Nov as the least-checked data point; Full B lists "whether the 13 Nov bars are final" as unresolved. The backfill plan still lists Historify as ending 12 Nov.

---

## 5. Message taxonomy

| Type | Format (structure only) | Fields | First appears | Class |
|---|---|---|---|---|
| Big Move Alert | Header with IST time; up to 3 lines of arrow, symbol, signed percent; volume line per symbol (Nx normal plus activity label, from 2026-06-16); optional news line | time, symbol, percent, direction, volume multiple, activity label | 2026-03 | Tradable (signal candidate). No entry, stop or target. Threshold about 3 percent. |
| Pre-Open F&O Movers | Top 10 gainers and Top 10 losers lists around 09:11 | rank, symbol, signed percent, IEP | 2026-08 | Tradable (signal candidate). IEP is not a fill price. |
| Opening volume list | 3 F&O hashtag symbols, posted twice about 2 minutes apart | symbols only, no direction | 2022-08 | Filter only |
| Initial Balance | 10:20 post with NIFTY and BANKNIFTY IB range, day type, breadth, bias word | ranges, counts, bias | 2026-03 | Informational |
| Closing Bell | 15:35 close summary | index closes, breadth, movers, outlook | 2026-04 | Informational (after close) |
| EOD report | 16:05 summary | index return, breadth, sector table, top 5 gainers and losers, FII/DII | 2026-06 | Informational (after close) |
| F&O Market Events | 08:30 calendar lists (media posts) | results and dividend lists | 2026-08 | Informational |
| Breaking News | Ad hoc headline | headline, impact score, source | 2026-04 | Informational (no symbol field) |

---

## 6. Method

- Entry. Big Move: open of the first 1m bar at or after sent_at + latency (60 s primary; 0 s and 120 s sensitivity), rounded up to the next minute. Entry after 15:00 IST is refused. Pre-Open: E1 = 09:15 bar open, E2 = 09:16 (primary), E3 = 09:17. Gap-fade H8 in phase 1 used the 09:15 open.
- Latency and look-ahead. Only bars that end before the decision time feed the ATR.
- R. 1R = 1.5 x ATR(14) on 5-minute bars ending before the decision time.
- Exits. 30-minute time exit (primary), 1R stop checked on every 1m bar. Stop wins a same-bar tie. A stop gapped through after the entry bar fills at the bar open. Targets fill at the target price. No position is held past 15:15. Also tested: 5, 10, 15, 45, 60 minute horizons and a stop/target grid.
- Cost model. `CostModel`, product MIS, exchange NSE, broker flattrade (brokerage zero), notional Rs 1,00,000. Statutory part 3.55 bps (STT 2.50, exchange 0.61, GST 0.11, SEBI 0.02, stamp 0.30). Round trip: 13.55 bps at 5 bps per side (1.0x); 18.55 at 1.5x; 23.55 at 2.0x; 7.55 at 2 bps per side (optimistic). Costs are priced from the real entry and exit legs.
- Statistics. Day-clustered bootstrap, 90 percent interval, 10,000 resamples, seed 20261113 (phase 1 and 2). Max drawdown in R only where N is 30 or more. The day is the independent unit.
- Edit guard (full history). A post edited more than 5 minutes after sent_at is excluded from the primary set; it is kept in a sensitivity set.
- Splits (chronological, no shuffle, by session).
  - Track A: 59 primary sessions. TRAIN 41 sessions (2026-08-06 to 2026-10-15). TEST 18 sessions (2026-10-16 to 2026-11-13).
  - Track B: 152 sessions. TRAIN 106 sessions (2026-03-20 to 2026-09-04). TEST 46 sessions (2026-09-07 to 2026-11-13). 1,862 TRAIN and 956 TEST observations.
- Multiple testing.
  - Phase 1: 27 tests; Bonferroni 0.0028 for the 18 hypothesis cells.
  - Phase 2: 264 logged p-values; 42 pre-registered (Bonferroni 0.0012); 0.00019 for all 264.
  - Full A: 21,578,400 configurations (99,900 filter/side combinations x 216 exit combinations). 1,243,944 had at least 40 TRAIN trades; 7,184 passed the selection rule. Bonferroni on the single-test p. Reality check: White-style day-clustered bootstrap of the maximum studentised mean, 500 resamples.
  - Full B: 3,196,800 configurations (core 1,468,800 plus one-at-a-time 1,728,000). 793,269 had at least 100 TRAIN trades; 315 passed. Bonferroni screen threshold 1.6e-8. Reality check: 1,000 day-block bootstrap draws.
  - The grid file (`config_grid.json`) was written before any outcome in both full runs (unit-checked).
- Controls. Random-stock control (Track A: 400 draws; Track B: 200 draws), NIFTY proxy or NIFTY-beta control, NIFTY-adjusted excess (beta 1), and a market-neutral pair variant for Track A.

---

## 7. Results

All figures are net bps per trade unless stated.

### 7.1 Phase 1 (4 sessions with bars; primary: 30 min, 1R stop; full set)

| Hypothesis | N | Days | Mean 1.0x | 1.5x | 2.0x | ExpR 1.0x | CI90 (1.0x) | Label then |
|---|---|---|---|---|---|---|---|---|
| H1 long up-movers | 65 | 4 | +3.9 | -1.1 | -6.1 | -0.042 | -1 / 8 | Investigate further |
| H2 short down-movers | 21 | 3 | -9.9 | -14.9 | -19.9 | -0.166 | -40 / -1 | Reject |
| H3 fade up (short) | 65 | 4 | -26.0 | -31.0 | -36.0 | -0.358 | -31 / -21 | Reject |
| H3 fade down (long) | 21 | 3 | -22.6 | -27.6 | -32.6 | -0.226 | -37 / 22 | Reject |
| H4 long (volume 3x or more) | 18 | 4 | +10.3 | +5.3 | +0.3 | -0.004 | -31 / 38 | Investigate further |
| H4 short | 9 | 2 | -24.1 | -29.1 | -34.1 | -0.307 | -41 / -22 | Reject |
| H5 repeat alert (pooled) | 7 | 1 | -30.2 | -35.2 | -40.2 | -0.364 | none | Reject (insufficient) |
| H6 larger percent (pooled) | 5 | 1 | -41.4 | -46.4 | -51.4 | -0.492 | none | Reject (insufficient) |
| H7 Pre-Open + volume list (pooled) | 5 | 3 | +2.1 | -2.9 | -7.9 | -0.184 | -81 / 72 | Investigate further |
| H8 gap continuation long | 38 | 4 | -14.3 | -19.3 | -24.3 | -0.198 | -40 / 23 | Reject |
| H8 gap continuation short | 33 | 4 | -23.4 | -28.4 | -33.4 | -0.600 | -40 / -8 | Reject |
| H8 gap fade long | 33 | 4 | +28.1 | +23.1 | +18.1 | +0.639 | 6 / 41 | Investigate further |
| H8 gap fade short | 38 | 4 | -20.6 | -25.6 | -30.6 | -0.639 | -34 / -6 | Reject |
| H8 gap fade pooled | 71 | 4 | +2.1 | -2.9 | -7.9 | -0.045 | -15 / 14 | Reject |
| H9 Initial Balance bias (NIFTY) | 4 days | | | | | | | Reject; 0 of 4 correct to 15:15 |

Other phase 1 facts: all 4 sessions closed up (NIFTY +2.01, +0.06, +0.84, +0.18 percent). H4 changed sign with a 60-second latency change (+4.1, -1.2, +8.3 at 0, 60, 120 s). Gross continuation of up-movers was +17 bps over 30 minutes, about the cost. The sign test p for H8 fade-long (0.014) does not pass 0.0028.

### 7.2 Phase 2 Track A (Pre-Open; E2, 1R stop, 30 min, 1.0x; 4 days)

| Rule | Cut | N | Mean | Median | ExpR | CI90 | Mean ex 9 Nov |
|---|---|---|---|---|---|---|---|
| A1 long gainers | top 10 | 38 | -3.8 | -39.6 | -0.35 | -27 / 22 | +2.1 |
| A2 short losers | top 10 | 33 | -21.2 | -41.9 | -0.50 | -38 / 4 | -15.5 |
| A4 fade gainers (short) | top 10 | 38 | -19.8 | -39.2 | -0.75 | -35 / -6 | -26.2 |
| A4 fade losers (long) | top 3 | 8 | +107.7 | +60.0 | +2.68 | none (3 days) | +107.7 |
| A4 fade losers (long) | top 5 | 15 | +86.5 | +15.3 | +1.89 | 43 / 144 | +72.7 |
| A4 fade losers (long) | top 10 | 33 | +40.9 | -34.3 | +1.00 | 8.6 / 75.2 | +29.2 |

- A4 long fade losers, top 10, by entry: E1 +28.1, E2 +40.9, E3 +30.8. By cost: +46.9 (2 bps), +40.9, +35.9 (1.5x), +30.9 (2.0x).
- By day (E2): 6 Nov +58.5, 9 Nov +93.2, 11 Nov -35.8, 12 Nov +64.4. Without the 3 best trades (GLENMARK, NAM-INDIA, SIEMENS) the mean is +15.0. The median is negative because 55 percent of trades stop out.
- About 43 percent of the gross return is NIFTY rising on the 4 up days. Excess gross vs NIFTY +25.6 bps.
- Pre-open percent versus IEP: median gap of IEP to the 09:15 open is 29.5 bps (90th percentile 86.8). The post cannot support a price-limited order at IEP.
- A5 (does the size of the move predict continuation): Spearman about 0 (both sets -0.002). No relation.
- A6 (Pre-Open name also in the opening volume list): N = 5 on 3 days. No inference.
- Exit grid: 124 cells; 16 percent positive. Best cells are all in the long-losers fade. Exploratory.

### 7.3 Phase 2 Track B (Big Move; +60 s, 1R stop, 30 min, 1.0x)

| Rule | N (dedup) | Days | Mean | Median | ExpR | CI90 | 1.5x | 2.0x |
|---|---|---|---|---|---|---|---|---|
| B1 long | 65 (63) | 4 | +3.9 | -10.3 | -0.04 | -0.7 / 8.5 | -1.1 | -6.1 |
| B1 short | 21 (16) | 3 | -9.9 | -2.0 | -0.17 | none | -14.9 | -19.9 |
| B2 long | 21 (16) | 3 | -22.6 | -25.1 | -0.23 | none | -27.6 | -32.6 |
| B2 short | 65 (63) | 4 | -26.0 | -23.0 | -0.36 | -31 / -20 | -31.0 | -36.0 |

- B3 volume buckets (long, continuation): below 2x -9.1 (N 32); 2-3x +24.0 (15); 3-5x +31.2 (10); 5x or more -15.7 (8). Not monotone.
- B4/B5: Spearman of log multiple with return +0.12 (p 0.28); no reliable effect.
- B6 repeats: 7 observations on one day. Not assessable.
- Reconciliation: H1, H2, H3, H4 and the four H8 cells reproduce phase 1 exactly (0.000000 bps).
- Phase 2 multiple testing: no cell can pass; with 4 days the smallest possible two-sided day-level sign-test p is 0.125.

### 7.4 MFE/MAE (gross, direction-adjusted bps; 30-minute window)

Scenario 1: A4 fade losers, top 10, E2 (N 33, 4 days; median 1R 29.1 bps)

| Measure | Value |
|---|---|
| Mean MFE (30 min) | 118.4 |
| Mean MAE (30 min) | -56.0 |
| Mean gross exit pnl, 30-min time exit | 59.2 |
| MFE capture, gross / net | 0.50 / 0.39 |
| Net mean, 30-min time exit | 45.6 |
| Net mean, 1R stop, 30 min | 40.9 (median -34.3) |
| Median time to MFE / MAE | 21 min / 1 min |
| Net mean without the 3 best trades (1R stop) | +15.0 |

Top 5 variant (N 15): mean MFE 162.6, mean MAE -53.6, net 30-min time exit +78.7, 1R stop +86.5.

Scenario 2: B1 long, all, +60 s (N 65, 4 days; median 1R 57.6 bps)

| Measure | Value |
|---|---|
| Mean MFE (30 min) | 63.1 |
| Mean MAE (30 min) | -43.9 |
| Mean gross exit pnl | 17.1 |
| MFE capture, gross / net | 0.27 / 0.06 |
| Net mean, 30-min time exit | 3.6 |
| Net mean, 1R stop | 3.9 (median -10.3) |
| Median time to MFE / MAE | 16 min / 9 min |
| Net mean without the 3 best trades (1R stop) | -6.4 |

B1 long with volume 3x or more (N 18): mean MFE 83.9, MAE -57.5, net 30-min time exit +18.6, 1R stop +10.3.

Exit grids: 12 rules per cell (3 stops x 4 targets). With a 0.5R or 1R stop plus a target, the A4 top 10 mean is between -13.5 and +1.8; the best means come from rules with no target. 144 rule-cells in total; exploratory, inside noise.

### 7.5 Full-history Part 1: Track A (59 sessions, 1,180 rows; E2, 1R stop, 30 min)

| Rule | Cut | N | Mean 1.0x | Mean 1.5x | Mean 2.0x | Opt 2 bps | ExpR 1.5x | CI90 1.5x | Sessions positive | Ex best day | Ex best 3 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A1 long gainers | 3 | 172 | +1.1 | -3.9 | -8.9 | +7.1 | -0.12 | -17.2 / +10.5 | 37% | -7.8 | -9.2 |
| A1 | 5 | 286 | -2.8 | -7.8 | -12.8 | +3.2 | -0.33 | -18.0 / +3.6 | 37% | -12.3 | -11.3 |
| A1 | 10 | 576 | -4.0 | -9.0 | -14.0 | +2.0 | -0.33 | -17.1 / -0.1 | 32% | -12.2 | -11.0 |
| A2 short losers | 3 | 174 | -15.6 | -20.6 | -25.6 | -9.6 | -0.53 | -30.8 / -9.6 | 29% | -24.8 | -25.7 |
| A2 | 5 | 289 | -12.9 | -17.9 | -22.9 | -6.9 | -0.48 | -25.4 / -10.1 | 27% | -20.2 | -21.9 |
| A2 | 10 | 574 | -9.1 | -14.1 | -19.1 | -3.1 | -0.44 | -20.8 / -6.8 | 25% | -16.4 | -16.3 |
| A4 long fade losers | 3 | 174 | -8.7 | -13.7 | -18.7 | -2.7 | -0.49 | -29.4 / +3.8 | 29% | -19.3 | -23.8 |
| A4 long fade losers | 5 | 289 | -7.1 | -12.1 | -17.1 | -1.1 | -0.43 | -23.8 / +0.9 | 29% | -16.1 | -18.2 |
| A4 long fade losers | 10 | 574 | -9.5 | -14.5 | -19.5 | -3.5 | -0.50 | -22.1 / -6.3 | 27% | -16.7 | -17.5 |
| A4 short fade gainers | 3 | 172 | -8.7 | -13.7 | -18.7 | -2.7 | -0.45 | -24.2 / -2.5 | 36% | -17.5 | -19.7 |
| A4 short fade gainers | 5 | 286 | -8.5 | -13.5 | -18.5 | -2.5 | -0.48 | -22.3 / -4.5 | 31% | -15.9 | -17.9 |
| A4 short fade gainers | 10 | 576 | -9.0 | -14.0 | -19.0 | -3.0 | -0.53 | -20.7 / -6.8 | 27% | -16.3 | -16.4 |

Across the 1,116 primary cells at 1.5x, 1 is positive (A1 top 3, E1, no stop, 45 min: +1.3 bps, CI lower bound -24.5). None has a CI lower bound above 0. The sensitivity set with edited posts (64 sessions) gives the same picture.

### 7.6 Full-history Part 1: Track B (152 sessions; +60 s, 1R stop, 30 min; primary set after edit guard)

| Rule | N | Days | Win % | Mean 1.0x | Median 1.0x | Mean 1.5x | Mean 2.0x | Opt 2 bps | ExpR 1.5x | CI90 1.5x | Sessions positive |
|---|---|---|---|---|---|---|---|---|---|---|---|
| B1 long | 1,674 | 147 | 35.1 | -13.4 | -18.5 | -18.4 | -23.4 | -7.4 | -0.33 | -21.2 to -15.6 | 19% |
| B1 short | 1,136 | 143 | 36.8 | -12.8 | -16.2 | -17.8 | -22.8 | -6.8 | -0.35 | -21.2 to -14.5 | 19% |
| B2 long | 1,136 | 143 | 34.9 | -14.3 | -19.5 | -19.3 | -24.3 | -8.3 | -0.36 | -22.6 to -15.6 | 27% |
| B2 short | 1,674 | 147 | 38.8 | -15.6 | -14.2 | -20.6 | -25.6 | -9.6 | -0.35 | -23.1 to -18.1 | 18% |
| B1 all | 2,810 | 152 | 35.8 | -13.2 | -17.5 | -18.2 | -23.2 | -7.2 | -0.34 | -20.3 to -16.0 | 15% |
| B2 all | 2,810 | 152 | 37.3 | -15.1 | -16.3 | -20.1 | -25.1 | -9.1 | -0.35 | -22.1 to -18.0 | 15% |

- No rule beats cost in any calendar month with N of 100 or more (best: B1 long, Nov, -6.5 at 1.5x, N 175). B1 long is negative in all 9 months.
- Volume buckets (continuation, 1.0x): below 2x -16.2 (763); 2-3x -11.0 (340); 3-5x -9.0 (371); 5x or more -9.0 (558); no volume line -16.1 (778). Best cell B1 short 3-5x: -3.6 (N 137). Nothing is positive.
- B4: Spearman of log multiple with the 30-minute continuation return 0.057 (p 0.010, N 2,032); day-clustered t 1.3. Weak and not significant after clustering.
- B6: repeats -14.9 versus first alerts -13.0.
- Every Part 1 interval at 1.5x is below zero. The optimistic 2 bps cost is also negative for every rule.

### 7.7 Replication of the earlier 4-session finding

| Finding | 4 earlier sessions | Other sessions | Source |
|---|---|---|---|
| A4 long fade losers, E2, top 10, 1R stop, 30 min, 1.0x | N 33, +40.9 bps, win 45%, CI +8.6 / +75.2 | 60 other sessions: N 587, -12.3 bps, win 25%, CI -19.4 / -4.5 | Full A |
| Same, all 64 sensitivity sessions | | N 620, -9.5, win 26%, CI -16.8 / -1.6 | Full A |
| B1 long, +60 s, 1R stop, 30 min, 1.0x | N 65, +3.9 | March to October: N 1,499, 138 days, -14.8 (CI at 1.5x -22.7 to -16.9) | Full B |
| B1 long, 6 to 13 Nov only (5 sessions) | +6.9 (CI at 1.5x -4.5 to +10.4) | November, 9 sessions incl. 13 Nov: -1.5 | Full B |

The 4-session results reproduce exactly on their own sessions (Track A N 33, +40.9; Track B 172 observations, difference 0.0 bps). They do not replicate elsewhere. The sign flips. They were small-sample results from a rising tape.

### 7.8 Full-history Part 2: Track A search (train versus test)

Top 5 by TRAIN CI lower bound (all: long listed gainers, top 3, entry 09:20, no stop, 100 bps target, first-minute spike filter; they differ in a max-percent cap and in the horizon). Net bps at 1.5x.

| # | Config | TRAIN N / days | TRAIN mean | TRAIN CI90 | TRAIN pos. sessions | TEST N / days | TEST mean | TEST CI90 | TEST 1.0x | TEST win | TEST ExpR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | top3, 45 min | 55 / 34 | +37.2 | +21.2 / +52.3 | 74% | 26 / 16 | -4.6 | -32.4 / +23.8 | +0.4 | 58% | -0.24 |
| 2 | same, cap 6 | 53 / 34 | +35.5 | +19.4 / +51.0 | 74% | 24 / 15 | -7.1 | -36.4 / +24.2 | -2.1 | 58% | -0.33 |
| 3 | top3, 30 min | 55 / 34 | +33.0 | +18.0 / +47.3 | 71% | 26 / 16 | -1.8 | -25.8 / +23.3 | +3.2 | 62% | -0.11 |
| 4 | as 3, cap 6 | 53 / 34 | +31.2 | +16.2 / +45.7 | 71% | 24 / 15 | -3.7 | -29.0 / +23.6 | +1.3 | 62% | -0.17 |
| 5 | top3, 60 min | 55 / 34 | +35.9 | +16.0 / +53.3 | 76% | 26 / 16 | -2.9 | -32.0 / +26.1 | +2.1 | 58% | -0.25 |

- The five are one family of near-duplicates. TEST N is 24 to 26, below 30.
- Multiple testing: best TRAIN studentised t 3.95; single-test p 4e-5; Bonferroni over 21,578,400 configurations gives 1.0; reality check p = 0.63 for the best (0.75, 0.76, 0.85, 0.96 for #2 to #5). The median maximum t under the null (4.18) is above the observed 3.95.
- Descriptive check after the freeze: of the top 200 TRAIN configurations, 44 have 30 or more TEST trades; 9 percent of those are positive on TEST; median TEST mean -60 bps; Spearman of TRAIN bound with TEST mean -0.16.
- Best TRAIN configuration per side, TEST mean: short losers -66.0 (N 37); pair -30.7 (N 20); short fade gainers -53.0 (N 23); long fade losers -23.8 (N 17). All negative.
- Unfiltered baseline (E2, top 5, 1R stop, 30 min, 1.5x), TRAIN / TEST: G long -13.1 / +5.3 (N 82); L short -16.4 / -21.5; L long fade -20.3 / +7.4 (N 85); G short fade -14.4 / -11.4; pair -17.4 / -6.7 (N 156). TEST net intervals span zero and the same rules lost on TRAIN.

### 7.9 Full-history Part 2: Track B search (train versus test)

Net at 1.5x. All five winners are continuation rules with a 60-minute horizon and a wide fixed stop (100 or 60 bps).

| # | Config (short form) | TRAIN N / days | TRAIN mean | TEST N / days | TEST 1.0x | TEST 1.5x | TEST 2.0x | TEST ExpR | TEST CI90 | TEST pos. sessions | Change |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | shorts, 3% or more, vol 2x or more, +120 s, NIFTY up pre-decision | 105 / 29 | +24.1 | 82 / 24 | +6.5 | +1.5 | -3.5 | -0.07 | -23.1 / +29.9 | 38% | -22.7 |
| 2 | shorts, 4% or more, vol 2x, +60 s, stop 60 bps, target 1.5R, max 2 concurrent | 101 / 36 | +9.3 | 99 / 33 | -19.3 | -24.3 | -29.3 | -0.29 | -37.3 / -13.7 | 21% | -33.6 |
| 3 | both sides, 3% or more, vol 3x, 3rd name in post, target 0.5R | 112 / 42 | +1.0 | 111 / 36 | -6.1 | -11.1 | -16.1 | -0.28 | -17.8 / -4.3 | 44% | -12.2 |
| 4 | shorts, 3% or more, vol 5x, first alert of symbol-day, target 1.5R | 110 / 41 | +5.3 | 105 / 35 | -10.6 | -15.6 | -20.6 | -0.18 | -29.4 / -2.3 | 43% | -20.8 |
| 5 | both sides, 4% or more, vol 3x, +120 s, target 1R, NIFTY up pre-decision | 156 / 31 | +0.6 | 118 / 27 | -14.6 | -19.6 | -24.6 | -0.32 | -31.7 / -8.3 | 30% | -20.2 |

- TRAIN CI lower bound (10,000 resamples): candidate 1 +2.9; candidates 2 to 5: -7.0, -6.8, -7.3, -8.0.
- Multiple testing: Bonferroni screen 1.6e-8; best TRAIN one-sided cluster p 0.05; TEST stage threshold 0.01 and the TEST p-values are 0.53, 1.00, 0.89, 0.99, 0.87. Reality check p = 0.93. Best observed TRAIN mean among eligible configurations +26.4 bps; null maximum median +37.2, 95th percentile +58.6. Deflated benchmark sqrt(2 ln K) = 5.5; best selected TRAIN cluster t = 1.6.

### 7.10 Controls

Track A (1.5x):

| Top config | Part | Strategy | Random-stock mean (p5 / p95) | Share of draws at or above | NIFTY proxy (gross) |
|---|---|---|---|---|---|
| #1 | TRAIN | +37.2 | -17.2 (-30.3 / -4.4) | 0.00 | -16.4 (+2.1) |
| #1 | TEST | -4.6 | -9.3 (-30.8 / +12.4) | 0.36 | -8.6 (+9.9) |
| #3 | TEST | -1.8 | -7.7 (-28.3 / +12.8) | 0.30 | -6.2 (+12.4) |
| #5 | TEST | -2.9 | -7.8 (-30.2 / +14.9) | 0.34 | -8.6 (+10.0) |

On TRAIN the rule beats random by 50 bps or more (selection effect). On TEST it sits inside the random band. Part 1 NIFTY excess is negative for all four rules (-8 to -19 bps at 1.5x).

Track B (TEST, 1.5x): actual versus random-stock control, share of control draws at or above actual: #1 +1.5 vs -18.1 (0.00); #2 -24.3 vs -21.8 (0.70); #3 -11.1 vs -17.4 (0.02); #4 -15.6 vs -20.6 (0.20); #5 -19.6 vs -16.5 (0.76). NIFTY-beta control excess on TEST: +19.6, -3.1, +4.4, -1.3, -2.1 bps. Candidate 1 NIFTY-adjusted (beta 1) +3.4. A random stock loses about the cost of the trade (-17 to -22 bps).

### 7.11 Down-index days

- Track A, Part 1 (E2, 1R stop, 30 min, top 10, 1.5x): gap-down sessions (31) versus gap-up (28): A1 -6.2 vs -12.1; A2 -15.6 vs -12.3; A4 long fade losers -20.3 vs -7.8; A4 short fade gainers -17.6 vs -10.0. Ex-post down days: A4 long fade losers -27.1 (6% of sessions positive) versus +0.1 on ex-post up days. No rule is positive on down-index days.
- Track A, Part 2 top 5: gap-down TEST (9 days, N 12) +16 to +23 with intervals about -26 / +54; gap-up TEST (6 to 7 days, N 12 to 14) -18 to -30. N too small to judge.
- Track B: 76 ex-post down days and 75 up days (151 with NIFTY data). B1 long -18.5 on down days (N 550) versus -10.9 on up days (N 1,124). Pre-decision regime, 1.0x: B1 long -18.5 down / -11.5 up; B1 short -17.5 / -3.0; B2 long -10.3 / -22.6; B2 short -13.3 / -16.5. No rule is positive on down-index days. Part 2 down-day TEST 1.5x: candidate 2 -33.2 (N 62), 3 -10.6 (54), 4 -27.6 (65); candidates 1 and 5 trade only on up days by design.

### 7.12 Source disagreements

| Item | Source A | Source B | Note |
|---|---|---|---|
| 13 Nov 1m bars | Phase 1/2: none; Historify ends 12 Nov | Full A/B: 375 bars per symbol exist | Full runs are later. 13 Nov bars treated as least checked. |
| Historify symbol count | Phase 1/2 and Full B: 212 | Full A: 214 plus NIFTY | Not reconciled. |
| Absent symbols | Phase 1/2: 9 (+2 with no bars) | Full A: 10 symbols (adds DALBHARAT, ANANDRATHI); Full B: 16 symbols | More history exposes more absent names. |
| Mean of A4 long fade losers without 9 Nov | Phase 2: +29.2 (top 10, E2) | Full A: +29.3 (3 sessions, N 27) | Rounding or N; unresolved. |
| B1 long 4-session CI | Phase 1: -1 / 8 | Phase 2: -0.7 / 8.5 | Same point estimate +3.9; different bootstrap rounding. |
| Live DB rows | Phase 1: 79 | Backfill verify: 87 live ids | Live DB grew; no conflict in history DB. |
| 9 Nov NIFTY day return | Channel EOD: +0.23 | Bars: +0.06 | Unresolved since phase 1. |
| Phase 1 session count | "5 sessions" (messages: 6, 9, 11, 12, 13 Nov) | 4 sessions with bars | 13 Nov had no bars then. |

---

## 8. Verification record

| Run | Unit checks | Hand-checked observations | Maximum difference | Other |
|---|---|---|---|---|
| Phase 1 | All pass (count not printed in output) | 5 random (seed 7) | 0.01 bps | Examples: UNIONBANK 9 Nov, MOTHERSON 6 Nov, KEI 12 Nov. ATR hand check GLENMARK 28.0500 vs 28.0500. |
| Phase 2 | 51 pass | 10 (5 Track A, 5 Track B) | 0.01 bps or better | H1, H2, H3, H4 and 4 H8 cells reproduce phase 1: 0.000000 bps. |
| MFE/MAE | n/a | 6 (3 per scenario) in separate DuckDB code: ALL MATCH | 5.0e-05 bps (recompute vs CSV) | Net 1R stop recomputed 40.87 vs CSV 40.87; B1 3.93 vs 3.93. |
| History backfill | Two backfill/parser test files exist | n/a | n/a | 0 duplicate keys; 87 of 87 live ids present in history. |
| Full A | 57 pass | 8 random | 0.000 (gross, net, R) | Config count declared 21,578,400 = counted 21,578,400. +40.9 / N 33 reproduces exactly. |
| Full B | 36 pass | 14 random | 4e-05 bps | Config count 3,196,800 = file count. 172 old observations match to 0.0 bps. Cost fit error vs `CostModel` 2.5e-14 bps. |

Verified: parsing, percent base (Muhurat close on 9 Nov), entry rounding, ATR exclusion of bars at or after the decision time, 15:15 cap, short sign, stop-first order, costs (13.548 / 18.548 / 23.548 / 7.548 bps), reconciliation with earlier runs, grid file written before outcomes.

Assumed: Rs 1,00,000 notional; flattrade brokerage zero; 5 bps slippage per side; fill at the entry bar open with no queue or partial fill; MIS short availability for all names; price bands ignored; target fills at the target price; NIFTY beta of 1 for the adjusted excess.

Unresolved: original 09:11 text of message 13326 (edited, original lost); why ids 13321 to 13324 and 13342 are missing; whether absent symbols are absent by design; live fill quality of a delayed market order in mid-caps and of an order after the opening auction; whether a post was altered within 5 minutes of posting; whether 13 Nov bars are final; why the Big Move volume line starts on 2026-06-16; the two header-delay outliers (ids 11500, 11501).

---

## 9. Limitations and data gaps

- Short history. Tradable templates run from March 2026 (Big Move, 152 sessions) and August 2026 (Pre-Open, 59 primary sessions). One regime, about 3.5 to 8 months.
- Simulated fills. Entry is the bar open. The 09:15 open is an opening-auction print, not an executable price for a retail MIS order. No queue, partial fill, spread, price band or circuit model. Opening-minute slippage may exceed 5 bps for the biggest movers.
- Short availability is assumed for all names.
- Missing symbols (section 4.5). They skew to large movers.
- Edits. 354 rows in the history DB have an edit date. Only the latest text is stored. Edited posts are excluded from the primary sets (more than 5 minutes after sending). An edit inside 5 minutes is undetectable.
- 13 Nov. Bars appeared late; treat that day as least checked.
- Missing or deleted ids: 786 in the history DB, including 13321 to 13324 and 13342. Text-less messages (35 fetched but not stored) are skipped.
- The channel text carries no entry, stop or target. Every exit here is invented by this study.
- Sessions in one day share the market factor, so effective sample size is the number of days.
- Few down-index days in phase 1 and 2 (none). The full-history runs have 31 gap-down sessions (Track A) and 76 down days (Track B), but no rule is positive on them.
- Search limits: the Track B grid has no two-way interaction among the one-at-a-time filters; the Track A reality check used 500 resamples; Bonferroni is very conservative because configurations overlap.
- Scratch folders hold channel text (msgs.txt, msgs.pkl, posts.pkl, DB-derived pickles). They must stay out of the repository.

---

## 10. Recommendation per candidate

Labels allowed: Reject / Investigate further / Paper-trading candidate.

Minimum criteria for Paper-trading candidate (all required): at least 30 independent sessions; at least 100 symbol-days; mean net above +5 bps at 1.5x slippage; ExpR above +0.10; day-clustered CI lower bound above 0 at 1.5x; positive in at least 60 percent of sessions; positive on down-index days; survives dropping the best day and the 3 best trades; same sign on TEST.

The earlier phase 1 and phase 2 "Investigate further" labels (H1 long, H4 long, H7, H8 gap-fade long = A4 long fade losers at E1, B1 long, A3 fade-losers cuts, A6, B3, B6) are superseded by the full-history result (Reject). Reason: they rested on 4 sessions that all closed up; the same rules, tested on 59 and 152 sessions, are negative at every cost level; and the search over millions of variants found nothing that survives test data or the reality check.

| Candidate | Recommendation | Basis |
|---|---|---|
| A1 long top gainers | Reject | Mean -4 to -9 bps at 1.5x (cuts 3/5/10 in section 7.5 show -3.9, -7.8, -9.0); 32 to 37 percent of sessions positive |
| A2 short top losers | Reject | Mean -14 to -21 at 1.5x; CI below 0 |
| A4 long fade losers | Reject | +40.9 does not replicate (-12.3 on 60 new sessions) |
| A4 short fade gainers | Reject | -13.5 to -14.0 at 1.5x; CI below 0 |
| A3 cuts (top 3/5/10) | Reject | All cuts negative or CI spans zero at 1.5x |
| A5 magnitude | Reject | No relation; no mean positive after costs |
| A6 and H7 (volume-list overlap) | Reject | N = 5 on 3 days; not re-run on full history; parent Pre-Open rules are Reject (judgement, not a separate test) |
| Track A top-5 search family | Reject | TEST -1.8 to -7.1 at 1.5x; reality check p 0.63; N below 30 |
| Pair (market-neutral) variants | Reject | No TRAIN bound above 0; unfiltered pair loses |
| B1 long | Reject | -18.4 at 1.5x; CI -21.2 to -15.6; negative in all 9 months |
| B1 short | Reject | -17.8 at 1.5x; watch item only: after NIFTY up pre-decision, -3.0 at 1.0x (N 369), still negative |
| B2 reversal (long and short) | Reject | -19.3 and -20.6 at 1.5x |
| B3 volume buckets | Reject | No bucket positive on full history |
| B4/B5 volume and magnitude | Reject | rho 0.057; not significant after clustering |
| B6 repeats | Reject | Repeats with a higher multiple: -5.6 (N 52), CI -24.1 to +2.8; no edge |
| Track B search candidates 1 to 5 | Reject | TEST at 1.5x: +1.5, -24.3, -11.1, -15.6, -19.6; reality check p 0.93 |
| H9 Initial Balance bias | Reject (as timing signal) | 0 of 4 correct |
| EOD, Closing Bell, F&O Events, Breaking News | Informational (not signals) | After session, or no direction or price |

No candidate meets the Paper-trading criteria. The best configuration of each search is a research finding of that search, not a trade recommendation.

---

## 11. Next steps and options

1. Stop and record. Keep this file as the result. No further work is needed.
2. Keep ingesting and re-test later. Wait for at least 30 new sessions that include down days. Re-test the frozen rules once, with no tuning. Do not tune on new data.
3. Missing symbols. Fixing them needs a logged-in broker session (existing historify download tooling). It was not done. Check symbol mapping (for example GVT&D, ENRIN, SYRMA) before any download.
4. IDEA-036. A research entry could hold derived numbers only (counts, bps, p-values; no channel text). It was not created. Start at `signal_engine/research/REGISTRY.md` if the owner wants it.
5. Keep the scratch folders out of the repository. They contain channel text.

---

## 12. File index

Base: `/home/anand/.hermes/cache/scratch/`

Phase 1 (`bt_research/`): report.md; observations.csv; results_by_hypothesis.csv; results_pooled.csv; pct_check.csv; h9.csv; bm_parsed.csv; stats_out.txt; phase1b_out.txt; pc9_out.txt; unit_checks_out.txt; verify_hand_out.txt; scripts parse.py, analysis.py, stats.py, phase1.py, phase1b.py, pc9.py, verify_hand.py, unit_checks.py, loadbars.py, export.py; caches msgs.pkl, msgs.txt, bars1m.pkl, obs.pkl, skips.pkl.

Phase 2 (`bt_research_next/`): report.md; track_a_observations.csv; track_a_grid_observations.csv; track_b_observations.csv; a_results_cells.csv; a_results_horizons.csv; a_results_grid.csv; a_results_a5.csv; a_results_a6_rows.csv; a_gap_by_day.csv; a_gap_rows.csv; a_registry.csv; b_results_b1b2.csv; b_results_b3.csv; b_results_b4.csv; b_results_b5.csv; b_results_b6.csv; b_results_horizons.csv; b_results_tod.csv; b_registry.csv; nifty_days.csv; a_out.txt; b_out.txt; extra_out.txt; a_robust.txt; coverage_out.txt; unit_checks_out.txt; verify_hand_out.txt; scripts lib.py, build_a.py, analysis_a.py, build_b.py, analysis_b.py, export.py, parse.py, unit_checks.py, verify_hand.py.

MFE/MAE (`bt_research_next/mfe_mae/`): summary.md; summary_body.md; obs_mfe_mae.csv; horizon_tables_all.csv; exit_grid_all.csv; hand_out.txt; maxdiff.txt; scripts calc.py, hand.py, report.py.

Backfill (`bt_backfill_design/`): plan.md; scripts bt_backfill.py, login_once.py, parse_robust.py, tg_creds.py, verify_history.py; tests test_bt_backfill.py, test_parse_robust.py. Verify output: `verify.out` (scratch root, `/home/anand/.hermes/cache/scratch/verify.out`).

Full A (`bt_full_a/`): report.md; config_grid.json; split.json; frozen_top5.json; posts_guard.csv; coverage_by_rank.csv; coverage_by_day.csv; nifty_days.csv; sessions_all.csv; obs_inputs.csv; part1_results.csv; part1_a5.csv; part1_replication.csv; part1_observations.csv.gz; part2_top5_train_test.csv; part2_top1_observations.csv to part2_top5_observations.csv; part2_controls.csv; part2_downup.csv; part2_top200_diag.csv; part2_extras.csv; verify_direct.csv; verify_out.txt; unit_out.txt; search_out.txt; part2_out.txt; scripts prep.py, build_obs.py, make_grid.py, core.py, search_lib.py, run_search.py, part1.py, part2_eval.py, extras.py, verify_direct.py, unit_checks_full.py, parse_robust.py.

Full B (`bt_full_b/`): report.md; config_grid.json; grid_def.py; split.json; p2_selected.json; per_observation.csv; results_part1.csv; results_B4.csv; results_B5.csv; results_part2_top5.csv; p2_cand1_train_trades.csv to p2_cand5_test_trades.csv; nifty_daily.csv; part1_out.txt; part2_run_out.txt; part2_test_out.txt; unit_checks_out.txt; verify_hand_out.txt; scripts load_data.py, parse_lines.py, parse_robust.py, build_obs.py, lib.py, stats_lib.py, part1_build.py, part1_analysis.py, grid_lib.py, part2_data.py, part2_select.py, part2_run.py, part2_test.py, unit_checks.py, verify_hand.py.

Research DBs (not in scratch; git-ignored): `/home/anand/github/openalgo/signal_engine/data/breakingtrade_channel.db` (live), `/home/anand/github/openalgo/signal_engine/data/breakingtrade_channel_history.db` (research).

---

## 13. Reproduction commands

All commands are read-only. Run from `/home/anand/github/openalgo`. Scratch scripts hold paths to the scratch folders, so run them from their own folder where they expect it.

```
cd /home/anand/github/openalgo
# Backfill verification queries (read-only on the history DB)
PYTHONPATH=. uv run python /home/anand/.hermes/cache/scratch/bt_backfill_design/verify_history.py

# Full-history Track A: unit checks and direct hand check
PYTHONPATH=. uv run python /home/anand/.hermes/cache/scratch/bt_full_a/unit_checks_full.py
PYTHONPATH=. uv run python /home/anand/.hermes/cache/scratch/bt_full_a/verify_direct.py

# Full-history Track B: unit checks and hand check
PYTHONPATH=. uv run python /home/anand/.hermes/cache/scratch/bt_full_b/unit_checks.py
PYTHONPATH=. uv run python /home/anand/.hermes/cache/scratch/bt_full_b/verify_hand.py

# Phase 1 and 2 checks
PYTHONPATH=. uv run python /home/anand/.hermes/cache/scratch/bt_research/unit_checks.py
PYTHONPATH=. uv run python /home/anand/.hermes/cache/scratch/bt_research_next/unit_checks.py
```

These commands were not re-run for this record. The search scripts (`run_search.py`, `part2_run.py`) take minutes and write only into their own scratch folders; run them only if a full re-computation is needed.

Operational impact of this record: none.
