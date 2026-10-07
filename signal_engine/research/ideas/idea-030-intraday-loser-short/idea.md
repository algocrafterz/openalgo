---
id: IDEA-030
title: Intraday loser short
stage: inbox
research_state: active
strategy_category: Statistical / Quantitative
strategy_tags: [cross-sectional, ranked-portfolio, intraday-return-persistence, tug-of-war, short-only, intraday]
horizon: intraday
source:
  type: self
  author: owner
  ref: _inbox/intraday-loser-short.txt
  captured: 2026-10-07
summary: Each session, short at the 09:20 open the 10 eligible F&O stocks with the lowest mean 09:20-open-to-14:45-close return over their previous 60 valid sessions; stop at 1.5 x that return's standard deviation above the reference price; exit at the close of the 14:45 bar.
revisit_when: After the owner resolves the open decisions D1 to D16 below and baseline_cross_sectional is implemented and validated. Then freeze v1 and run once.
last_reviewed: 2026-10-07
---

# IDEA-030 - Intraday loser short

Status: UNFROZEN. Owner decisions D1 to D16 approved 2026-10-07 as recommended (decision log).
Nothing was backtested. No return or outcome data for this hypothesis was read. The only data
queries were coverage counts (listed under "Data contract"). The freeze waits for
`baseline_cross_sectional` and its known-answer check (D15).

## Idea

The owner's approved hypothesis (candidate A) and the owner's added requirements are in
`_inbox/intraday-loser-short.txt`. The rules are in `spec.yaml`.

### Three hypotheses, kept separate

- **H1**: persistent intraday losers continue to lose intraday.
- **H0-drift**: any positive result is only the unconditional NSE intraday short drift. Any
  short of any eligible stock from 09:20 to 14:45 would earn it.
- **H-sel**: selecting the worst 60-session performers adds value beyond that drift.

The basket result alone cannot tell H1 from H0-drift. The comparison with
`baseline_cross_sectional` (below) is what separates them. H-sel is the claim this idea tests.

### Economic mechanism: why 60-session intraday losers should continue to lose

Tug-of-war clientele (Lou, Polk, Skouras 2019; Heston, Korajczyk, Sadka 2010):

1. Two groups trade a stock at different times of day. Attention-driven and retail investors buy
   at or before the open (news, overnight attention, gap chasing). Through the day, long-biased
   MIS books are unwound before the square-off, and institutions supply the other side.
2. A stock's 09:20-to-14:45 return measures the balance between the opening buyers and the
   intraday sellers. A stock with a negative mean intraday return over 60 sessions is one where
   the opening demand is repeatedly reversed during the day.
3. The clientele of a stock (who trades it, and when) changes slowly, over months, not days. Lou,
   Polk and Skouras report that the intraday return component of a stock persists in the cross
   section for months. Heston, Korajczyk and Sadka report that returns at the same time of day
   persist across days. So the imbalance measured over the last 60 sessions should continue into
   the next session.
4. The universe's negative intraday drift is the same effect at market level. The open question
   is whether it is concentrated in identifiable stocks.

Limits of the evidence, stated now: the cited papers use US data. That the NSE universe has a
negative intraday drift is an earlier internal measurement (project memory, "NSE intraday drift
is -23%/yr"). It was not measured again for this idea and is not in a repo doc.

### Why 60 sessions and 10 stocks (implementation choices, not optimal values)

No alternative was tested and none will be. One frozen specification, then one baseline run.

- **60 sessions.** About three calendar months. The mean of 60 daily returns has a standard error
  of sd / sqrt(60), about 0.13 sd, so one results day or news day cannot decide the rank by
  itself. 60 sessions is short enough to follow a clientele that changes over quarters (the
  papers use formation windows of months). It also equals the platform's default listing-age
  rule (`execution.eligibility_min_prior_sessions` 60, `research/intake/execution.py`), so it adds
  no second history rule.
- **10 stocks.** The eligible universe is 140 (2017 median) to 212 (2026 median) symbols per
  session (coverage count below), so 10 is the most extreme 5% to 7%, the tail where the
  hypothesis says the effect concentrates. 10 positions also give per-session diversification for
  the day-clustered statistics, keep gross exposure at Rs 10,00,000, and match the platform's
  default 10-slot capacity scoring (`record_run --caps`). The full bottom decile is in the
  diagnostics (decile monotonicity), so the choice of 10 does not hide a different shape.

## Rules, made explicit

Each item states the rule and the reason. The spec carries the same rule in its own words.

| Item | Rule | Reason |
| --- | --- | --- |
| Intraday performance | r(s,d) = Close of the 14:45 bar / Open of the 09:20 bar - 1, both 5m bars of session d (open-time stamped, IST). Open and close prices only; no high, low or volume | It is exactly the trade's own price window (09:20 open fill, 14:45 bar close exit), so the signal measures what the trade earns. Both bars carry the same price factor, so back-adjustment does not change r |
| 60 sessions, not days | The 60 most recent valid prior sessions, counted as official NSE trading sessions with a valid observation; never calendar days | Calendar days would give a different number of observations around holidays. A fixed observation count gives every stock the same estimation precision |
| Session time window | Ranking at 14:50 IST of the previous session; only fill at the 09:20 open; exit at the 14:45 bar close (14:50 IST); the stop works from the 09:20 bar to the 14:45 bar | 09:20 skips the first 5 minutes (opening auction print and first-bar noise) and is the engine's first fill after a completed bar. 14:45 is the Flattrade profile's standing time exit, 15 minutes before the 15:00 force-flat |
| Eligibility before selection | At least 60 valid prior sessions; session D is not an ex-date of a price-factor corporate action; NSE raw close of the last valid prior session at least Rs 10. All known before 09:15 on D | The owner's rule is "no current-session data in the ranking". Ex-dates are announced before the session. The platform price floor normally reads the same-day close, so here it reads the previous close (D10) |
| Insufficient history | A symbol with fewer than 60 valid prior sessions is not ranked | A shorter window would make its m60 noisier than the others and would bias selection toward new or patchy names |
| Ties | Exact tie in m60: symbol name ascending, A to Z | The owner's rule. With float means of 60 returns an exact tie is near impossible; the run counts ties (D11) |
| Selection timestamp and information set | Fixed from bars up to and including the 14:45 bar of the previous official session; no bar of session D; not revised during D | The ranking must not know anything about the session it trades |
| Costs in selection and execution | The ranking uses gross r, with no cost. The basket, `baseline_cross_sectional` and the universe short all use the same cost model: Flattrade statutory per-trade MIS model at the fixed Rs 1,00,000 notional plus the house slippage (4 bps per side), the same product and the same direction | At equal notional the cost per trade is the same for every symbol, so costs cannot change the rank order, and they cancel in the excess (excess is the same gross or net). The one inconsistency is that slippage is modelled flat for all names, while the selected names may cost more to trade; the Outcome 3 threshold covers it |
| Fewer than 10 eligible | Trade all eligible symbols; no substitute; unused capital is not reallocated | Substitution would change the hypothesis (a less extreme loser). Coverage: after the first 60 sessions no session has 1 to 9 symbols with 60 prior price-valid sessions; sessions with 0 are the empty sessions (halts, special session) |
| Selected symbol excluded on D | Slot stays empty; the 11th symbol is not substituted; counted by reason | The exclusion reads same-day data (contract, same-day volume and close). Substitution would let that data choose a stock |
| Tiny dispersion of m60 | Nothing changes. No dispersion threshold, no filter. Dispersion is reported as a diagnostic | No economic reason was found for a threshold that is not itself a new tuned parameter. A narrow spread only means the selection has little to work with; the excess test measures that directly |
| Falsification | Section "Falsification" below | Decided before any run |

## Comparison baseline: `baseline_cross_sectional` (pending implementation)

A recorded, reusable same-session random-stock short baseline, designed separately as a generic
platform feature. It is not implemented yet. This idea cannot be evaluated against falsification
item 1 until it exists. Required for this idea:

- Same eligible universe on each session (the pre-selection eligibility above), drawing only from
  eligible stocks that are not in that session's selected 10.
- Same session, same entry time (09:20 open), same direction (short), same exit rules (14:45 time
  exit and the same 1.5 x sd60 stop rule on the drawn stock's own sd60), same execution
  assumptions, holding constraints and cost model.
- Same number of positions per session as the basket actually traded.
- Seeded and recorded in the run, with the excess per session (basket mean minus random mean) and
  its day-clustered t.

The universe short (H0-drift reference (b)) is the mean short return of all eligible stocks on the
session, same rules and costs.

## Pre-declared reading of each outcome

Decided before any baseline runs. E = cross-sectional excess per trade of the basket over
`baseline_cross_sectional` (bps, same session); t(E) = its day-clustered t. Drift = net per trade
of `baseline_cross_sectional`. The outcomes are checked in the order 3, 2, 1.

| Outcome | Condition | Reading | Verdict | Next action |
| --- | --- | --- | --- | --- |
| 3. Selection adds meaningful value | t(E) at least 2.0 and E at least +12 bps per trade | H-sel supported | Set by the gates: EDGE only if every HARD gate passes (G1.2, G1.3, G2.2 t 2.5, G3.2, G4.4, G4.5); MARGINAL if positive net evidence with a gate failed (name it); NO_EDGE if net fails | Independent Opus review of the run. If EDGE or MARGINAL, run G5 with the live caps (G5.2) before any paper stage. No parameter change |
| 2. Drift exists, selection adds nothing | Not Outcome 3, and Drift net above 0 | H0-drift explains the result. Includes t(E) at least 2.0 with E below 12 bps: statistically present, not tradeable | NO_EDGE for IDEA-030, recorded as "drift harvesting, selection adds nothing", even if the basket's own net passes G1 to G4 | Reject IDEA-030 (rejection_basis to be chosen at the time). The drift, if wanted, becomes a separate pre-specified idea with its own trial. 60 and 10 are not tuned |
| 1. No short drift | Not Outcome 3, and Drift net at or below 0 | No harvestable intraday short drift net of cost at Rs 1,00,000 in 2017 to 2026, and selection does not create one | NO_EDGE | Reject IDEA-030. Record the baseline result as evidence against short-drift ideas on this universe and cost basis |

**The 12 bps threshold for Outcome 3.** The primary round-trip cost at Rs 1,00,000 is 11.55 bps
(Flattrade statutory 3.55 bps, zero brokerage, plus 8 bps slippage; IDEA-029 decision log
2026-10-07, finding D5). An excess of at least 12 bps means the selection alone pays the full
cost of a trade with zero market drift. It is also above the 8 bps that doubling the slippage adds
(G1.3 stress), so the excess survives the plausible extra slippage of a basket of more volatile
names. A weaker alternative is 8 bps (the cost-stress difference only); D14 asks the owner to
choose.

## Falsification (any one kills it)

1. E at or below 0 bps per trade, or t(E) below 2.0 (needs `baseline_cross_sectional`). A pass of
   2 to 6 with a fail of 1 is recorded as drift harvesting, not an edge.
2. Net fails G1.2 (+0.05R per trade), or the day-clustered net t is below 2.5 (G2.2 at 10 or
   fewer trials).
3. Net at or below 0 with slippage doubled (G1.3).
4. Out-of-sample (from 2023-11-01) net at or below 0, or below 40% of in-sample (G3.2).
5. Net positive in fewer than 6 of the 10 calendar years 2017 to 2026, or the worst year below
   -0.25R (G4.4). 2017 starts after the 60-session history (first eligible session 2017-03-30) and
   2026 ends 2026-10-01; both are partial years.
6. The top 10 symbols carry more than 40% of net profit, or fewer than 50% of traded symbols are
   net positive (G4.5).

## Pre-declared diagnostics (report only, never a gate, never a reason to change v1)

- Decile monotonicity: next-session r by m60 decile, all eligible stocks.
- The mirror top-10 long leg (the 10 highest m60, long), same rules.
- Entry delayed one bar (09:25 open), G4.2.
- `baseline_cross_sectional` by year.
- Cross-sectional dispersion of m60 by year: its standard deviation, and the gap between the
  10th-lowest m60 and the median.
- The span in official sessions that the 60 valid observations cover (staleness), by year.
- Counts by reason of selected symbols not traded (contract, same-day eligibility, STOP_TOO_TIGHT)
  and of sessions with fewer than 10 positions.
- Count of exact m60 ties at the 10th place.

## Gate arithmetic (no data used)

R is the stop distance: R = 1.5 x sd60 of the entry price. With the owner's illustrative
sd60 = 2% (not measured; sd60 is a statistic of the outcome variable r, so it was not looked at):

- R = 1.5 x 2% = 3.0% = 300 bps.
- G1.2 needs net at least 0.05R = 15 bps per trade. Gross must be at least 15 + 11.55 = 26.55 bps,
  so about 27 bps (verified). With the generic discount-broker cost (16.27 bps) it is 31.3 bps.
- G1.3 needs net above 0 at 2x slippage: gross above 11.55 + 8 = 19.55 bps.
- G2.2 needs a day-clustered net t of at least 2.5. Assumption, not measured: if the daily mean of
  10 shorts has a standard deviation of 100 bps, over 2,345 sessions its standard error is
  100 / sqrt(2345) = 2.1 bps, so t 2.5 needs net of 5.2 bps (gross of 17 bps).
- G1.5 (median cost at most 0.40R): 11.55 / 300 = 0.04R. Not binding.

So G1.2 is probably the binding economic gate. Because it is in R, a wider stop raises the bar
roughly in proportion and a tighter stop lowers it. The expectancy in R is the mean of each
trade's net / R, so trades on low-sd60 stocks weigh more than trades on high-sd60 stocks. Under
a driftless random walk, the chance that the intraday maximum passes 1.5 sd of the close-to-close
return is about 2 x P(Z > 1.5) = 13%; that is theory, not data.

## Data contract (proposed, D7)

```yaml
data:
  frequency: 5m
  field_families: [price]
  min_n_minutes: 1
  complete_bins: {first: "09:15", last: "14:45"}
```

- **Price** (required). The strategy reads the 09:15 bar (engine signal bar and, under D2, the
  stop reference), the 09:20 open, the highs and opens of 09:20 to 14:45 (stop), and the 14:45
  close.
- **Volume** (not required, not read by the strategy). A price-valid session with invalid volume
  is served with volume withheld as NaN (`intraday_access.py`). The platform eligibility rule
  then reads it as zero volume (`eligibility._daily` uses `nan_to_num`), so the session is
  excluded as ZERO_VOLUME on the same day and is not a valid prior session. Coverage: 4,897 of
  432,773 price-valid symbol-sessions (1.1%) have invalid volume, concentrated in 2018 to 2021.
- **final_bar** (not required): nothing after 14:45 is read, so the closing auction (from
  2026-08-03) never enters a signal or an exit.
- **Bin completeness 09:15 to 14:45** (67 bins). A missing bin would leave a stop check unread or
  the entry or exit price undefined.
- **min_n_minutes 1**: availability threshold only (IDEA-029 precedent). G0.4's "at least 370
  one-minute bars" wording is not applied as a filter here, as in IDEA-029; D7 asks the owner to
  confirm.
- **When the contract fails** on a prior session: no observation. On session D: the selected
  symbol is not traded and the slot stays empty.

Coverage counts (quality table of `nse-intraday-5m-20261006-001`; no prices or returns read):
2,408 official in-scope sessions (2017-01-02 to 2026-10-01), 213 symbols, 441,275 symbol-sessions,
432,773 price-valid. Symbols with at least 60 prior price-valid sessions: 2,345 sessions with 10
or more, first 2017-03-30; median per session 140 (2017) rising to 212 (2026). Sessions from
2023-11-01: 722 of 2,408 (30%).

## Engine path (D8, no code written)

- `backtest/record_run.py` lines 895 to 899 call `strategy.prepare_panel(layer.frames, params,
  layer.calendar)` for any strategy that defines it, intraday or swing (verified). The look-ahead
  truncation check then keeps every frame and re-runs the panel through `derive_calendar(fr)`
  (line 911).
- The existing panel adapters (`strategies/low_volatility.py`, `strategies/high_proximity.py`)
  are swing (`SwingStrategy`, daily bars). The panel helpers in `backtest/panel.py`
  (`close_panel`, `top_n_schedule`, `attach_schedule`) are built for one close per session.
  `top_n_schedule` breaks ties by a hash of symbol and date, not alphabetically.
- Needed: a new intraday adapter in `backtest/strategies/` (a `Strategy` from
  `strategies/base.py`) with
  - `prepare_panel`: compute r per symbol-session from the 5m frames, the valid-prior-session
    mask, m60 and sd60, the pre-selection eligibility and the 10-name schedule per session (rank
    first, then apply same-day exclusions without substitution), and attach per-bar columns
    (selected flag, sd60);
  - `entry()`: at the 09:15 bar of a selected session, a short `EntrySignal` with
    `sl = Close[09:15] x (1 + 1.5 x sd60)` and a target that is never reached;
  - `run_overrides`: `skip_open_minutes 0`, `entry_cutoff_min` 09:20 (only the 09:15 bar can
    signal), `max_trades_per_day 1`, `allow_longs False`, `min_sl_pct 0.005`, time exit 14:45
    from the profile.
  - Registry: add it to `REGISTRY` in `backtest/__main__.py` with kind `"intraday"`.
- Engine facts that shape the rules: the stop is an absolute price set at the signal bar, and risk
  (R) is measured from the signal close, not the fill (`engine.py`, Position.risk). A fill at or
  beyond the stop is taken and closed as SL_GAP_ENTRY. The engine checks the stop on the fill bar
  itself.
- Not verified: that `derive_calendar` works on 5m frames for the truncation re-run; that the
  data layer removes contract-failed sessions from `layer.frames` before `prepare_panel` (the
  adapter must rank from prior information whether or not session D is present); and how the
  intraday baseline code in `record_run.py` (around line 937, a same-symbol random-entry sample)
  will coexist with `baseline_cross_sectional`.

## Owner decisions (approved 2026-10-07)

Each has a recommendation and a reason. The owner approved every recommendation as written; the
spec carries each as "APPROVED - owner decision Dn".

- **D1. Stop multiple (1.5 x sd60).** Recommend keep 1.5. The arithmetic above holds: with
  sd60 = 2%, R = 300 bps, G1.2 needs 15 bps net, 27 bps gross, and G1.2 is probably the binding
  gate. Do not choose the multiple to make a gate easier: that would be a hidden trial. A stop in
  sd units scales R with each stock's own volatility. No stop at all would make R undefined, so
  G1.2 could not be evaluated, and the live pipeline places a stop-loss order on every entry (project memory, Indian broker OCO fix).
- **D2. Stop reference price.** The engine sets the stop at the signal bar and measures R from the
  signal close. Recommend P_ref = close of the 09:15 bar of D (engine-native, known before the
  09:20 fill, used only for the stop, never for selection). The alternative, the 09:20 open fill
  as the hypothesis wrote it, needs a generic engine change (stops relative to the fill). The two
  differ by one 5-minute gap.
- **D3. Entry at the 09:20 open versus pre-open.** Recommend the 09:20 open. Whether Flattrade
  accepts MIS orders in the NSE pre-open session is not verified (no repo doc covers it). The
  dataset's 09:15 open is not a guaranteed fill price for a pre-open order. The 09:20 open is the
  engine's first fill after a completed bar and the start of r. Consequence: if the opening buying
  is concentrated in the first 5 minutes, this design measures only the part after 09:20.
- **D4. MIS shortability throughout the history.** Assumption, High materiality: every F&O name
  was MIS-shortable on every eligible session. Not in the data: the F&O ban list, ASM and GSM
  stages, and Flattrade's own MIS withdrawals (`docs/FLATTRADE-RESTRICTIONS.md`: during an F&O
  ban, cash MIS "gets restricted at most brokers"; the config reject list is empty as of
  2026-05-04). Bias risk: names in a ban period have heavy positioning and are not a random
  subset, so they may be over-represented among extreme losers. Recommend proceed with the
  assumption, disclosed in the run. G6.2 needs a daily ban and surveillance list before any paper
  stage.
- **D5. OOS boundary.** Recommend 2023-11-01, as IDEA-029. It gives 722 of 2,408 sessions (30%),
  inside G3.1's 30% to 40%. Trades are intraday, so none straddles the split; the G3.5 embargo of
  one session applies. The m60 of early OOS sessions reads in-sample returns; that is not leakage,
  because nothing was fitted on the in-sample.
- **D6. Which sessions count in the 60-session window.** Recommend: the 60 most recent valid prior
  sessions, where valid means the data contract passes, the session is not a price-factor ex-date,
  and the symbol was eligible that session under the platform rules. An excluded session gives no
  observation and does not break the window (IDEA-029 decision 11). No staleness cap; the span is
  reported. Alternative for the owner: also require the 60 observations to lie within the last N
  official sessions (for example 80). That is a new rule, so it is not proposed.
- **D7. Data contract.** Recommend price only, min_n_minutes 1, complete bins 09:15 to 14:45, as
  above. Volume is not used. Confirm the ZERO_VOLUME relabelling of the 4,897 volume-invalid
  sessions is acceptable, and that G0.4's 370-minute wording is not applied (IDEA-029 precedent).
- **D8. Engine path.** Confirm the adapter plan above: an intraday `Strategy` with
  `prepare_panel`, a new panel helper for per-session intraday returns, and a `REGISTRY` entry.
  It is new code and needs tests (information boundary, truncation, ranking, no substitution)
  before the freeze.
- **D9. Portfolio cap and fewer than 10.** Recommend max 10 concurrent, max 10 trades per day,
  Rs 1,00,000 each, Rs 10,00,000 gross exposure; with fewer than 10 eligible, trade all, no
  substitute, no reallocation. The cap never binds because the basket is at most 10 by
  construction. Conflict to resolve: G5.2 requires the backtest to use the live
  `max_open_positions` and loss limits, and the live account is small (tens of thousands of
  rupees; 2 slots per project memory). Recommend the research run use 10 x Rs 1,00,000 to test the
  hypothesis, and G5.2 be run as a separate evaluation before any paper stage.
- **D10. Same-day information in eligibility.** The platform eligibility (`backtest/eligibility.py`)
  reads the same session's volume and last-bar close (price floor, ZERO_VOLUME). The data
  contract also reads the whole session. Using either before the ranking would let session-D data
  choose stocks. Recommend: the price floor for ranking reads the previous valid session's raw
  close; the same-day rules and the contract act only after ranking and remove without
  substitution. This needs adapter support; the platform's same-day exclusion still applies as an
  execution fact.
- **D11. Ties.** Recommend alphabetical as the owner stated, and count ties. Note that the
  platform convention (`panel.top_n_schedule`) is a hash of symbol and date; alphabetical order
  favours early letters if ties ever occur.
- **D12. Disclosure.** Recommend `chart_peeking: true`. The short direction was chosen knowing the
  universe's negative intraday drift on this same history. This raises the validation bar and does
  not stop the run. `prior_variants: 0`: the owner chose candidate A among drafted candidates, but
  no variant was backtested. Confirm.
- **D13. Universe.** "Published F&O universe per date" does not exist point-in-time; the repo has
  only today's F&O list (`eligibility.MEMBERSHIP_SOURCE`). Recommend `current_fno` with the
  survivorship disclosure. Direction of the bias is unclear: names that collapsed and left F&O
  (possible good shorts) are absent, and names added after strong runs are present.
- **D14. Outcome 3 economic threshold.** Recommend 12 bps per trade (selection alone pays the full
  11.55 bps primary round trip). Alternative: 8 bps (the slippage-stress difference).
- **D15. Dependency on `baseline_cross_sectional`.** Recommend not to freeze or run until it is
  implemented and checked on a known-answer case (for example, a basket drawn at random must give
  E near 0 with t(E) below 2.0). Running without it would leave falsification item 1 unevaluated,
  and the result could not separate H-sel from H0-drift.
- **D16. Corporate-action ex-dates.** The rule excludes an ex-date session as a trade day and as an
  observation (G0.8). Within-session r is not changed by back-adjustment, so this is a conservative
  choice (abnormal flows on ex-dates), not a correctness need. Confirm how the adapter finds
  ex-dates in the published intraday path: by a change in the session price factor
  (`intraday_access.session_price_factors`) or from the corporate-action table. The legacy
  `split_days` drop runs only in the Historify loader.

Other items checked and not ambiguous: holding is same session, no overnight; the time exit price
is the same price as r's end; the cost model charges both legs at the entry notional for a short,
the same as for a long (`backtest/costs.py`); the profile's MIS standing exit is 14:45 and the
latest exit bar closes by 14:55 (`research/profiles/flattrade-nse-cash.yaml`).

## Decision log

- 2026-10-07: drafted from the owner's approved hypothesis (candidate A) and the owner's added
  requirements (three hypotheses, outcome readings, `baseline_cross_sectional`, explicit rules,
  mechanism and parameter reasons). Unfrozen. No backtest, no outcome data, coverage counts only.
  `spec.yaml` passes `validate_spec` with the proposed defaults; decisions D1 to D16 are open.
- 2026-10-07: owner approved D1 to D16 as recommended, the full eligible-pool `baseline_cross_sectional` design (report-only, not a gate), D10 (ranking and the price floor use prior-session information only; same-day checks may remove a selected stock, never substitute one) and D14 (Outcome 3 needs a cross-sectional excess of at least 12 bps per trade, about the Flattrade round trip). No parameter sweeps; one frozen specification, then one baseline.
- 2026-10-07: limitations kept prominent, not blocking. (a) Universe: today's F&O list applied to the past, so survivorship bias of unknown direction. (b) Shortability: an operational assumption, not historically verified, that every selected name was MIS-shortable on every session (the F&O ban list and ASM/GSM stages are not in the data). Neither is production-ready evidence; historical F&O membership and ban-list verification are prerequisites before paper or live. Research question as interpreted: given that prior research observed negative intraday drift (chart_peeking true), does selecting persistent intraday losers add incremental value?

