# Strategy Acceptance Criteria for Live Trading

Applies to every signal_engine strategy (BREAKOUT, ORB, EMA9, and anything new) before it may
trade real capital. Written for our setting: NSE F&O-eligible cash equities (about 209 names),
intraday MIS, 5-minute bars, broker data as the source of truth, Indian charges, SEBI's retail
algo framework, and small real capital (tens of thousands of rupees; sandbox Rs 1 lakh).

Drafted 2026-09-30 from four research passes (statistics, robustness, deployment/regulation,
Indian market structure), a second pass that verified tick size, the closing auction and broker
square-off times against NSE and broker pages, and this repo's own cost schedule and data.

## How to use this document

- Gates run in order. A HARD item that fails stops the process: do not spend effort on later
  gates for a strategy that fails G1 or G2. Fix the strategy or retire it.
- Every threshold carries an evidence tier:
  - **[A]** peer-reviewed paper, regulator or exchange text.
  - **[P]** practitioner heuristic (books, platform docs, blogs). Common, not proven.
  - **[H]** house rule: our own choice, informed by the sources. Change it deliberately, in
    this file, with a dated reason.
- HARD = must pass. REPORT = must be computed and explained in the run record; a poor value is a
  finding to discuss, not an automatic rejection.
- Every backtest, ablation and parameter variant tried counts as a trial (see G2.5).
- Governing rules already in force: one shared code path for backtest, walk-forward, paper and
  live; broker data only; all timestamps IST; no silent exclusions (each skipped symbol or day
  is named in the log).
- Calibration principle (second pass, 2026-09-30): the gates should reject strategies that have
  no edge, not strategies that are merely imperfect. Thresholds were set so that a strategy with
  a modest but real edge after Indian costs, a decent number of trades and ordinary
  drawdowns can pass. Example that passes: gross +0.55R with a 0.35R cost (stops near 0.5%) gives
  net +0.20R, still positive with slippage doubled, and clears every economics and statistics gate.

## Market facts verified on 2026-09-30

- **Tick size (NSE/CMTR/67133, 13 Mar 2025; NSE/CMTR/62174, 24 May 2024):** cash-segment equities
  (series EQ, T0, BE, BZ, BO, RL, AF; ETFs excluded) tick by price: below Rs 250 Re 0.01; Rs 250 to
  1,000 Rs 0.05; above Rs 1,000 to 5,000 Rs 0.10; above 5,000 to 10,000 Rs 0.50; above 10,000 to
  20,000 Rs 1.00; above 20,000 Rs 5.00. The tier is set by a monthly review: the closing price on the
  last trading day of a month fixes the tick for the next month. Stock futures inherit the
  underlying's tick. The tier structure you supplied is confirmed. Our symbol master
  (`symtoken.tick_size`) still shows 0.05 for stocks above Rs 1,000, so it is stale for them.
- **Closing Auction Session (SEBI circular 16 Jan 2026; NSE/CMTR/73362, 18 Mar 2026; go-live
  3 Aug 2026 per NSE/CMTR/75479):** for F&O-eligible stocks (the first phase) continuous trading
  ends at 15:15 and the auction runs 15:15 to 15:35. The reference price is the VWAP of trades from
  15:00 to 15:15, with a plus or minus 3% band; only limit and market orders are allowed (no SL,
  SL-M or iceberg). The auction price becomes the official close and the F&O settlement price.
  Our data corroborates the date: all 10 names sampled lose volume from 15:16 to 15:20 starting
  Monday 2026-08-03, and from about 2026-09-04 one bar stamped 15:28 or 15:29 carries roughly half
  of the day's volume. Before August 2026 the last-bar volume share was about 0.4%, so the
  10-year history is essentially unaffected.
- **Broker MIS auto square-off (broker pages, read 2026-09-30):** CAS stocks are squared off
  earlier than other stocks. Fyers 15:05 (from 5 Jun 2026); Dhan 15:10; 5paisa 15:10 (from 3 Aug
  2026); Zerodha and ICICI Direct 15:12; Angel One 15:15; Upstox and Zerodha 15:25 for non-CAS
  stocks; Groww 15:20. Flattrade's own page says only "during last 15 minutes of market close"
  (no fixed time); the user has confirmed that Flattrade squares off MIS at 15:00 IST, which is
  the time this document and the backtest engine use. CNC (delivery) is not auto-squared by any
  broker checked. Charges are Rs 20
  to Rs 50 plus GST per order where stated.

## G0. Integrity (before any performance number is believed)

| ID | Requirement | Threshold | Tier | How we test |
|---|---|---|---|---|
| G0.1 | Same logic in every mode | Backtest, paper and live call the same `strategy_core` and `exits` code; adapters hold no rules | H | Code review; adapter tests such as `test_backtest_breakout_adapter.py` |
| G0.2 | Causality of every input | `f(df[:k]) == f(df).iloc[:k]` for each feature and the input builder | A (look-ahead bias) | Truncation tests in `tests/test_features_*`, `test_strategy_core_inputs.py` |
| G0.3 | Timestamps | All external times converted to IST at the boundary; bar timestamps are open-time | H | Unit test per data entry point |
| G0.4 | Data coverage | Every symbol-session used has at least 370 one-minute bars, except sessions from 2026-08-03 where the auction window has no continuous trades; gaps listed by name | H | Coverage report |
| G0.5 | No silent exclusion | Every excluded symbol, day or trade is logged by name with a reason; no catch-all around a batch | H | Lesson of 2026-09-30 (a batch of 11 valid equities was dropped silently) |
| G0.6 | Closing auction handled | Intraday level profiles (value area) use continuous-market bars only, i.e. stamped before 15:15, in every era; nothing else may depend on bars from 15:15 onward unless the strategy is explicitly designed for the auction | A (NSE CAS) / H | `features/session.continuous_session`; value area defaults to it |
| G0.7 | Test quality | At least 80% coverage on `features/` and `strategy_core/`; tests written before code | H (user rule) | `pytest --cov` |
| G0.8 | Corporate actions | Split and bonus sessions excluded or adjusted; ex-date handling verified | A (NSE adjustment rules) | `data.split_days` drop, spot-checked |

## G1. Economic viability (costs first)

| ID | Requirement | Threshold | Tier | Notes |
|---|---|---|---|---|
| G1.1 | Cost model | Statutory charges from `portfolio.costs.india_intraday` computed at the DEPLOYED notional, plus slippage (default 4 bps per side for market orders) | H | Table below |
| G1.2 | Net expectancy at 1.0x cost | At least +0.05R per trade, HARD | H | R = the trade's own stop distance |
| G1.3 | Slippage stress | Net expectancy above 0 with slippage doubled (8 bps per side), HARD. Statutory charges are exact, slippage is the uncertain part, so only slippage is stressed. Report total cost at 1.5x and 2.0x | P | Cost stress grid |
| G1.4 | Gross edge vs cost | REPORT: gross expectancy relative to round-trip cost; at least 1.5x is comfortable | A/H (Chan) | Compare in bps |
| G1.5 | Cost per trade in R | Median cost at most 0.40R, HARD; at most 0.30R is the target (REPORT) | H | Cost bps divided by stop bps |
| G1.6 | Stop floor | REPORT: if a fixed floor sets most stops, show results at other floors (BREAKOUT: 0.30% of price on 4 of 6 width buckets) | H | Stop-width table |

### Indian cost stack (current schedule in repo, matches Zerodha's published rates from 1 Oct 2024)

Per order brokerage is 0.03% or Rs 20, whichever is lower. STT is 0.025% on the sell leg only.
NSE transaction charge is 0.00297% of turnover. SEBI fee is Rs 10 per crore. Stamp duty is
0.003% on the buy leg. GST is 18% on brokerage, transaction charge and SEBI fee.

| Position notional | Statutory round trip | Plus 8 bps assumed slippage |
|---|---|---|
| Up to Rs 50,000 | 10.6 bps | 18.6 bps |
| Rs 1 lakh | 8.2 bps | 16.2 bps |
| Rs 2 lakh | 5.9 bps | 13.9 bps |
| Rs 3 lakh | 5.1 bps | 13.1 bps |

Re-verify the schedule after every Union Budget and SEBI or exchange circular, and confirm your
own broker's brokerage terms. The backtest default of 16 bps corresponds to Rs 1 lakh notional.

## G2. Statistical significance

| ID | Requirement | Threshold | Tier |
|---|---|---|---|
| G2.1 | Trade count | At least 200 net trades overall, and at least 30 in any subgroup that is reported or used | P (100 basic, 200+ confident; 30 bootstrap floor) |
| G2.2 | t-statistic | Day-clustered t-stat of net expectancy at least 2.5 when 10 or fewer variants were tried; with more trials use the Bonferroni level (p at most 0.05 divided by trials, about 2.9 at 20 trials, 3.0 at 30, 3.3 at 50). HARD | A (Harvey, Liu, Zhu 2016 argue 3.0 for factor mining; relaxed here because a strategy has thousands of trades and a short trial ledger) |
| G2.3 | Deflated Sharpe | DSR at least 0.90 using the true trial count | A method (Bailey, Lopez de Prado 2014); 0.90 is H |
| G2.4 | Probability of backtest overfitting | PBO (CSCV) at most 0.30 across tried variants; above 0.50 is worse than chance | A method (Bailey et al. 2016); 0.30 is H |
| G2.5 | Trials ledger | Every backtest and variant logged in `research/` with its count; DSR and t-hurdle use that number | H |
| G2.6 | Dependence | Sharpe and t computed on daily aggregated P&L with autocorrelation adjustment (Newey-West) | A |
| G2.7 | Track length | REPORT: sample length against the Minimum Track Record Length for the observed Sharpe at 95% | A method (Bailey, Lopez de Prado 2012) |

## G3. Out-of-sample discipline

| ID | Requirement | Threshold | Tier |
|---|---|---|---|
| G3.1 | Chronological hold-out | Last 30% to 40% of sessions untouched until the candidate is frozen | P (70/30, 60/40) |
| G3.2 | OOS result | OOS net expectancy above 0 and at least 40% of in-sample (walk-forward efficiency) | P (Pardo suggests 50%; relaxed) |
| G3.3 | Walk-forward | At least 4 rolling windows of at least 6 months; net above 0 in at least 60% of them | H |
| G3.4 | Symbol hold-out | At least 20% of the universe never used for tuning; must also be net positive | H |
| G3.5 | Leakage | No trade straddles the split; embargo of at least one full session | A concept (purging); length is H |

## G4. Robustness and bias

| ID | Requirement | Threshold | Tier |
|---|---|---|---|
| G4.1 | Parameter perturbation | Plus or minus 20% on each parameter: at least 70% of neighbours stay net positive and none loses more than 60% of the expectancy; choose the plateau, not the peak. HARD | P |
| G4.2 | Latency | REPORT: entry delayed by one 5-minute bar; flag if the sign flips (alert lag is seconds, so one bar is a severe case) | H |
| G4.3 | Bar resolution | REPORT: stop-first vs pessimistic same-bar reading of exits; flag if the sign flips | H |
| G4.4 | Sub-periods | Net above 0 in at least 60% of calendar years and worst year no worse than -0.25R; also report volatility terciles (India VIX). HARD | H (P for 2 to 4 year slices) |
| G4.5 | Dispersion | Top 10 symbols at most 40% of total net profit and at least 50% of symbols net positive, HARD; sector concentration is REPORT | H |
| G4.6 | Direction | REPORT: longs and shorts separately; a one-sided strategy must be declared one-sided | H |
| G4.7 | Survivorship | REPORT: the universe uses today's F&O list, which is survivor-biased; test on symbols as they existed and list names that later left | A concept; H action |
| G4.8 | Event days | REPORT: results excluding results days, monthly expiry, index rebalance (March and September) and budget or election days | H |

## G5. Risk and capital fit

| ID | Requirement | Threshold | Tier |
|---|---|---|---|
| G5.1 | Monte Carlo | At least 5,000 bootstrap paths, both trade-level and day-block; 95th percentile max drawdown at most 25% of capital; probability of ruin below 5% at the deployed risk per trade | P (under 15% manageable, over 25% hard; ruin under 5%) |
| G5.2 | Live caps applied | Backtest run with the live `max_open_positions`, `max_trades_per_day`, daily, weekly and monthly loss limits and the entry price band | H (EMA-20 was disqualified by a concurrency artifact) |
| G5.3 | Sizing | Risk per trade at most 1% of capital; never above 50% of estimated Kelly | P |
| G5.4 | Capacity | Order size at most 10% of the median 5-minute volume at entry time | P (participation caps of 1% to 10%) |
| G5.5 | Margin | Worst-case simultaneous MIS margin at most 90% of capital | H |

## G6. Indian market tradability

| ID | Requirement | Threshold | Tier |
|---|---|---|---|
| G6.1 | Session and square-off | Entries only inside the strategy window (BREAKOUT: 09:45 to 11:45). Flattrade squares off MIS at 15:00 IST (confirmed by the user), so live positions are always closed by 15:00 and the standing time exit is 14:45, a 15-minute buffer. If another broker is added, use the earliest confirmed square-off among the brokers used | P (broker pages, Sep 2026); Flattrade time is user-confirmed |
| G6.9 | Backtest respects the square-off | HARD: no backtest trade may be open at or after 15:00. The exit bar must close at least 5 minutes before the broker's square-off (latest 5-minute exit-bar stamp 14:50, close 14:55). Enforced by the engine (`RunConfig.mis_squareoff_min`, `squareoff_margin_min`), which caps any larger `time_exit_min`; a test asserts it. Any new intraday strategy must show its latest exit time in the run record | H |
| G6.2 | Daily eligibility | Skip names under ASM, GSM or T2T, names your broker blocks for MIS, and names without a valid tick; log each by name | A (NSE surveillance), H |
| G6.3 | Circuits | REPORT: entries whose stop or target lies beyond the day's price band | A, H |
| G6.4 | Tick size | HARD before live: every order price and stop trigger must be a multiple of the tick that applies to that stock this month (tier by price, set by the previous month's last close). `executor.py` used a hard-coded 0.05 (TCS trigger 2079.75 is not a valid 0.10 tick); fixed 2026-09-30: it now rounds to the coarsest tick the month could carry (`features.instruments.safe_tick`, valid under every finer tier). Still owed before any live run: place one small real order per tier (below Rs 250, up to Rs 1,000, above Rs 1,000) with the broker and confirm the stop triggers are accepted | A (NSE/CMTR/67133) |
| G6.5 | Gap-through stops | Backtest fills stops at the open when gapped; live stop type (SL-M vs SL) tested for rejection behaviour | H |
| G6.6 | SEBI retail algo framework | Broker-registered API access, static IP (one primary, one backup), 2FA, daily session logout, order tagging, at most 10 orders per second without exchange registration. Mandatory from 1 April 2026 | A (SEBI circular 4 Feb 2025 and extension; NSE INVG67858) |
| G6.7 | Charges current | Cost schedule re-verified within the last quarter | H |
| G6.8 | Closing auction awareness | No SL or SL-M orders exist in the auction (only limit and market), so any position that could still be open at 15:15 needs a plan. The intended plan is to be flat by the time exit and never hold into the auction | A (NSE CAS) |

## G7. Paper (analyze) incubation and live tracking

| ID | Requirement | Threshold | Tier |
|---|---|---|---|
| G7.1 | Duration | Paper mode on the same core for at least 20 sessions AND at least 30 closed trades. If the strategy makes fewer than one trade per session, 40 sessions replace the trade count and judgement rests on signal parity and execution quality | P (2 to 4 weeks, 30 to 50 trades) |
| G7.2 | Signal parity | Replaying the paper period through the backtest reproduces at least 95% of paper signals; any gap is investigated because the code is shared | H |
| G7.3 | Execution quality | Median entry slippage at most 2x the assumed 4 bps; 95th percentile adverse slippage below `max_entry_slippage_r`; every fill protected by a stop | H |
| G7.4 | Tracking | Paper expectancy inside the 5th to 95th percentile Monte Carlo band; degradation versus backtest of 10% to 20% is expected, worse needs a written explanation | P |
| G7.5 | Capital ramp | 25% of intended size, then 50%, then 100%, at least 10 sessions per stage after the initial paper run, repeating G7.2 to G7.4 and G8 | P |

## G8. Operational readiness

| ID | Requirement | Threshold | Tier |
|---|---|---|---|
| G8.1 | Kill switch | Cancel-all and flatten-all tested end to end and complete within 10 seconds; daily loss lock-out tested | A (SEBI, MiFID II RTS 6, FINRA 15-09); 10 s is H |
| G8.2 | Pre-trade controls | Max order quantity and value, price collar against last price, duplicate-order guard, stale-data guard | A (SEC 15c3-5, RTS 6) |
| G8.3 | Recovery | On restart, reconcile positions and orders with the broker and confirm every open position has a stop | H |
| G8.4 | Clock | NTP synchronised, drift under 1 second (also needed for TOTP login) | H (WSL2 drift already bit us) |
| G8.5 | Broker sessions | Daily token expiry (about 03:00 IST) handled with retried auto-login and an alert on failure | H |
| G8.6 | Audit trail | Structured logs with signal ID, order IDs, strategy version and config hash per trade; retained at least 5 years | A (SEBI tagging; SEC 3 to 6 years) |
| G8.7 | Change control | Version tag and config hash on every trade, code and security review before deployment, post-deploy verification, tested rollback | A/P (RTS 6, FINRA 15-09, Knight Capital) |
| G8.8 | Alert escalation | Repeated rejects, unprotected positions and drift page a human; warnings in logs are not enough | P (Knight: 97 ignored alerts) |

## G9. Monitoring and retirement (after go-live)

| ID | Requirement | Threshold | Tier |
|---|---|---|---|
| G9.1 | Decay watch | Rolling 60-trade net expectancy below the Monte Carlo 5th percentile for two consecutive windows pauses the strategy | H |
| G9.2 | Drawdown watch | Drawdown beyond the backtest 95th percentile pauses trading and triggers review | H |
| G9.3 | Re-validation | Any code, parameter, cost or broker change reruns G1 to G5 and repeats the paper stage; full re-validation at least annually | A (SR 11-7 ongoing monitoring; RTS 6 annual assessment) |
| G9.4 | Retirement | Two consecutive quarterly reviews failing G1 or G2 retires the strategy | H |

## Scorecard: BREAKOUT (shared core, TP1-only baseline)

Backtest of 2026-09-30: 209 equities, 2016-10 to 2026-09, broker data, Rs 1 lakh notional, 16 bps.
Scored against the calibrated thresholds above.

| Gate | Result | Verdict |
|---|---|---|
| G1.2 net expectancy at 1.0x cost | -0.477R per trade (needs +0.05R) | FAIL |
| G1.3 slippage doubled | worse than -0.477R | FAIL |
| G1.5 median cost in R | 0.46R against a limit of 0.40R | FAIL |
| G2.2 clustered t-stat | about -101 | FAIL |
| G3.2 OOS | -0.483R (in-sample -0.474R) | FAIL |
| G4.5 dispersion | 71 of 200 symbols positive on gross R (36% against 50%) | FAIL |

BREAKOUT still fails at the relaxed thresholds, and by wide margins on the ones that matter
(expectancy and significance). The failure is the missing edge, not strictness. The 13 exit-ladder
variants on the same entries all stayed between -0.43R and -0.53R net, so no exit design reaches
G1. Later gates are not evaluated: a strategy failing G1 and G2 stops there.

## Claims from the research passes that were NOT adopted

- The India research example of "Rs 190 to 220 per Rs 1 lakh round trip". GST was mis-computed;
  the correct figure is about Rs 82, matching the repo schedule.
- "E[SR_oos] is roughly SR_is divided by the square root of N", the PBO cut-offs of 0.05 to 0.10,
  and an embargo of twice the holding period: not traceable to the cited papers.
- "F&O ban blocks cash-segment MIS orders": still unverified; the ban is for derivatives
  positions. We use the broker's own MIS eligibility instead (G6.2).
- The first India pass's closing-session description (auction 15:00 to 15:15, no 15:40 end) was
  wrong; the second pass found the actual structure from NSE and SEBI circulars (see Market facts).
- "Six months of silent failure" for the BreakingTrade timestamp bug: duration not established.
- Several blog-sourced numbers were labelled verified by the research agents; they are [P].

## Sources

Peer-reviewed, exchange and regulatory [A]:
- Harvey, Liu, Zhu (2016), "...and the Cross-Section of Expected Returns", Review of Financial Studies 29(1): https://academic.oup.com/rfs/article/29/1/5/1843824
- Bailey, Lopez de Prado (2014), "The Deflated Sharpe Ratio": https://www.pm-research.com/content/iijpormgmt/40/5/94
- Bailey, Borwein, Lopez de Prado, Zhu (2016), "The Probability of Backtest Overfitting": https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf
- Bailey, Lopez de Prado (2012), "The Sharpe Ratio Efficient Frontier": https://www.davidhbailey.com/dhbpapers/sharpe-frontier.pdf
- Bailey et al. (2014), "Pseudo-Mathematics and Financial Charlatanism", Notices of the AMS: https://www.ams.org/notices/201405/rnoti-p458.pdf
- White (2000), Reality Check for Data Snooping; Hansen (2005), Test for Superior Predictive Ability
- Lopez de Prado (2018), Advances in Financial Machine Learning (purged and embargoed validation)
- Newey, West (1987), HAC covariance estimator, Econometrica
- SEBI circular 4 Feb 2025 on retail algorithmic trading: https://www.cse-india.com/upload/upload/Feb_042025.pdf ; timeline extension (Sep 2025): https://www.sebi.gov.in/legal/circulars/sep-2025/extension-of-timeline-for-implementation-of-sebi-circular-dated-february-04-2025-on-safer-participation-of-retail-investors-in-algorithmic-trading-_96979.html
- NSE implementation standards INVG67858: https://nsearchives.nseindia.com/content/circulars/INVG67858.pdf
- NSE tick size circulars: https://nsearchives.nseindia.com/content/circulars/CMTR67133.pdf (13 Mar 2025); NSE/CMTR/62174 (24 May 2024)
- SEBI circular on the Closing Auction Session (16 Jan 2026): https://www.sebi.gov.in/legal/circulars/jan-2026/introduction-of-closing-auction-session-cas-in-the-equity-cash-segment-and-certain-modifications-in-the-pre-open-auction-session_99122.html
- NSE CAS circulars: https://nsearchives.nseindia.com/content/circulars/CMTR73362.pdf (18 Mar 2026); https://nsearchives.nseindia.com/content/circulars/CMTR75479.pdf (30 Jul 2026, go-live); NSE CAS page: https://www.nseindia.com/static/products-services/closing-auction-session
- Commission Delegated Regulation (EU) 2017/589, MiFID II RTS 6: https://ec.europa.eu/finance/securities/docs/isd/mifid/rts/160719-rts-6_en.pdf
- FINRA Regulatory Notice 15-09: https://www.finra.org/sites/default/files/notice_doc_file_ref/Notice_Regulatory_15-09.pdf
- SEC Rule 15c3-5 (market access); Federal Reserve SR 11-7 (model risk management)

Practitioner [P]:
- Pardo (2008), The Evaluation and Optimization of Trading Strategies (walk-forward efficiency)
- Aronson (2007), Evidence-Based Technical Analysis (data-mining bias)
- Chan, Quantitative Trading (2009) and Algorithmic Trading (2013) (cost versus edge)
- Carver (2015), Systematic Trading (parameter simplicity, plateau selection)
- Kissell, market-impact models; Knight Capital case studies (PRMIA, CIO Magazine)
- Broker MIS square-off pages: Flattrade https://flattrade.in/support/knowledge-base/what-is-mis/ ; Zerodha https://support.zerodha.com/category/trading-and-markets/trading-faqs/market-sessions/articles/intraday-auto-square-off-timings ; Upstox https://upstox.com/announcements/risk-management/update-on-auto-square-off-timings-3/ ; Fyers https://support.fyers.in/portal/en/kb/articles/what-is-the-intraday-square-off-timings-at-fyers ; 5paisa https://forum.5paisa.com/portal/en/kb/articles/what-are-the-auto-square-off-timings-for-open-intraday-positions ; Angel One https://www.angelone.in/support/your-orders/square-off ; Dhan https://dhan.co/support/general/market-session-status-and-timing/what-are-the-dhan-intraday-auto-square-off-timings/ ; ICICI Direct https://www.icicidirect.com/faqs/stocks/what-time-does-the-intraday-day-position-get-squared-off ; Groww https://groww.in/help/stocks,-f&o,-ipo-&-mtf/order/what-will-happen-if-i-fail-to-square-off-a-position-1
- Zerodha charges page: https://zerodha.com/charges/
- Backtesting guides used for drawdown, profit-factor and paper-trading norms (TradeZella, QuantConnect docs). Treat their numbers as heuristics.
