# BREAKOUT strategy specification v1.0: FROZEN

Status: FROZEN on 2026-10-08 by owner approval (answers of 2026-10-07 and 2026-10-08). Moved into the
repository on 2026-10-08 (uncommitted until the owner asks). Any change to a rule after the freeze is a
new version.
Written 2026-10-07. Supersedes the draft `BREAKOUT-SPEC-DRAFT.md` (scratchpad only).
Authority: this specification is the authority for BREAKOUT. Pine (`breakout.pine`) is a migration
oracle only. A Pine difference is classified (implementation, data, universe, timing, intentional
revision); it is never a reason to change this text (owner decision 2026-10-07).

Every repository claim cites file:line as read on 2026-10-07. HYPOTHESIS marks an unverified claim.

Item status values (all items are FROZEN as of 2026-10-08; the older labels remain in sections for history):
- FROZEN: part of v1.0 as written.
- EXCLUDED: not part of v1.0 (gap filter, NR filter, afternoon window).

Path abbreviations: `PINE` = `signal_engine/strategy_library/intraday/orb/breakout.pine`;
`BMD` = `.../orb/breakout.md`; `CORE` = `signal_engine/strategy_core/breakout.py`;
`EXITS` = `signal_engine/strategy_core/exits.py`; `ENG` = `signal_engine/backtest/engine.py`;
`TYPES` = `signal_engine/backtest/types.py`; `BTAD` = `signal_engine/backtest/strategies/breakout.py`;
`SIM` = `signal_engine/pipeline/sim_sink.py`; `CYC` = `signal_engine/pipeline/cycle.py`;
`DRV` = `signal_engine/pipeline/exit_driver.py`; `MAIN` = `signal_engine/main.py`;
`RISK` = `signal_engine/risk.py`; `VAL` = `signal_engine/validator.py`; `TRK` = `signal_engine/tracker.py`;
`CFG` = `signal_engine/config.yaml`; `RSPEC` = `signal_engine/docs/PYTHON-RUNNER-SPEC.md`;
`ACC` = `signal_engine/docs/STRATEGY-ACCEPTANCE-CRITERIA.md`; `INV` = `signal_engine/docs/ACCOUNTING-INVARIANTS.md`.

## Status summary

| # | Item | Status |
|---|---|---|
| 0 | Versioning and onboarding | FROZEN (onboarding executed after the freeze) |
| 1 | Universe | FROZEN (deployment list version supplied by the owner at deployment) |
| 2 | Entry conditions | FROZEN, no gap gate |
| 3 | Signal timestamp | FROZEN |
| 4 | Level definitions | FROZEN (one implementation check, 5m frame and auction bars) |
| 5 | Retest and break | FROZEN, break stamps reset at each session start (5.1) |
| 6 | Day type | FROZEN |
| 7 | Counter-bias veto | FROZEN |
| 8 | Gap filter | EXCLUDED from v1.0; recorded as a known Pine defect (item 8) |
| 9 | NR filter | FROZEN as dropped (intentional revision) |
| 10 | RVOL, session volume, score | FROZEN |
| 11 | Stop loss | FROZEN |
| 12 | Exit plan | FROZEN |
| 13 | Fill timing and entry window | FROZEN, afternoon window off |
| 14 | Sizing | FROZEN |
| 15 | Re-entry | FROZEN, cap of 2 entries per symbol per direction per day |
| 16 | Daily and portfolio limits | FROZEN, per strategy book |
| 17 | Conflict handling | FROZEN, Pine dispatch stops on the BREAKOUT-PY universe during parity |
| 18 | Bad fill | FROZEN |
| 19 | Time exit | FROZEN, exact mapping in 19.1 |
| 20 | Costs and the two evaluation views | FROZEN, two-view wording approved |

v1.0 was frozen WITHOUT the gap filter. v1.0 is therefore not a test of the gap filter and must never
be described as one.

---

## 0. Versioning, single machine-readable form, research onboarding

Status: FROZEN. The onboarding plan below is executed after the freeze (steps recorded in the repository).

### 0.1 Rules

1. Spec id `BREAKOUT`, version `v1.0`. A change that can alter which trades exist, their size, or how
   they exit is a new version (`v1.1`, `v2.0`) with `parent`. A change that only makes code match this
   text keeps the version and is logged as an implementation fix.
2. Every version change gets: a `versions.yaml` entry; a trial in `trials.yaml`; a dated `BMD` entry
   with the five layman fields CLAUDE.md requires plus a NOTE block; a classification
   `intentional_revision` or `implementation_fix`.
3. One machine-readable form, read by all modes: the `strategy:` block in the idea's `strategy.yaml`
   (block in 0.3; it is not in `spec.yaml` because `research/intake/spec.py` rejects unknown top-level
   keys there, and `spec.yaml` carries the intake-schema intent of the same rules). The core, the backtest adapter, the runner, SimSink and the live gate read it. No
   mode repeats a value from it in code or in `CFG`; where `CFG` must hold a value (for example
   `time_exit`), start-up refuses a mismatch.
4. Each run artifact, each runner start-up log and each trades.db entry row carries
   `strategy_id`, `strategy_version` and the strategy fingerprint.
5. After owner approval, this document moves from the scratchpad into the repository (target
   `signal_engine/strategy_library/intraday/orb/BREAKOUT-SPEC.md`, linked from `idea.md` via
   `analysis_doc`). Not yet.

### 0.2 Onboarding plan (approved 2026-10-08; executed after the freeze)

Model: `research/ideas/idea-027-52-week-high-proximity/` (files `idea.md`, `spec.yaml`,
`assumptions.yaml`, `trials.yaml`, `versions.yaml`, `runs/`).

Steps, following `signal_engine/research/README.md` "Adding an idea" (lines 50-59):

1. Save the source unchanged: `signal_engine/research/ideas/_inbox/breakout-key-level.txt` (a pointer
   text naming `PINE`, `BMD`, this spec, and the date).
2. Copy `research/ideas/_template/` to `research/ideas/idea-031-breakout-key-level/` (031 is the next
   free id; REGISTRY.md lists 027-030).
3. Fill `idea.md` frontmatter like IDEA-027 (`research/ideas/idea-027-52-week-high-proximity/idea.md:1-17`):
   `id: IDEA-031`, `title: BREAKOUT key-level breakout and retest`, `stage: backtest`,
   `research_state: needs_more_evidence`, `strategy_category` (Momentum or the taxonomy's breakout
   category, see `research/taxonomy.yaml`), `horizon: intraday`,
   `backtest_module: backtest/strategies/breakout.py`, `analysis_doc` pointing to `BMD`,
   `revisit_when`, `summary`. Record the prior evidence (2026-09-30 shared-core run: net -0.48R) as
   history in `idea.md`; it does not count as a v1.0 run.
4. Write `spec.yaml` (schema v2, `research/ideas/_template/spec.yaml`): intent fields plus the
   `strategy:` block of 0.3. `profile: flattrade-nse-cash`
   (`research/profiles/flattrade-nse-cash.yaml`). Validation is `research/intake/spec.py`.
5. Intake (commands from `research/intake/cli.py:433-490`):
   - `uv run python -m signal_engine.research.intake check IDEA-031`
   - `uv run python -m signal_engine.research.intake resolve IDEA-031`
   - `uv run python -m signal_engine.research.intake confirm IDEA-031 A-001 A-002 ...`
   - `uv run python -m signal_engine.research.intake classify IDEA-031 --category <cat> --tags intraday,key-level,breakout,retest`
6. Registry: `uv run python -m signal_engine.research.build_registry --check`, then
   `uv run python -m signal_engine.research.build_registry`.
7. Freeze: there is no separate freeze command. `record_run` freezes and registers the version
   (`signal_engine/backtest/record_run.py:1295-1296`, `:1440`, using `research/intake/freeze.py`).
   First v1.0 run: `uv run python -m signal_engine.backtest.record_run --idea IDEA-031 --trials <N>
   --note "v1.0 freeze"`, then `uv run python -m signal_engine.research.intake set-current IDEA-031 v1`.
   `--trials` counts every earlier BREAKOUT variant (13 exit variants and the stop-width and
   ablation runs in memory `project_breakout_shared_core_backtest.md`, plus the 2026-10-08 stamps-cleared
   check and every recorded variant in `BMD`), so the t hurdle is honest. Provenance rule (owner
   2026-10-08): earlier variants are listed in `trials.yaml` with `evidence_for_v1_0: false`; they
   raise the trial count and the hurdle, and they are never merged into, averaged with or used as
   evidence for the v1.0 run record. The v1.0 evidence is the first recorded v1.0 run only.
8. Prerequisite before step 7: the code changes S1-S9 in the shared-rule design that the spec requires
   (counter-bias veto, re-entry policy, bad-fill model, TP-at-close, time exit at the open). A run on
   today's code would describe a different strategy.

### 0.3 The machine-readable block (values are v1.0)

```yaml
strategy:
  id: BREAKOUT
  version: v1.0
  bar: 5m
  core:                                  # -> BreakoutParams
    va_triggers: true
    va_acceptance: false
    pd_triggers: true
    ib_triggers: true
    retest_max_bars: 6
    retest_includes_break_bar: true      # v1.0 quirk, frozen (item 5)
    break_stamp_carries_across_sessions: false  # reset at the first bar of each IST session (5.1); owner decision 2026-10-08
    cooldown_bars: 3
    score_threshold: 7
    conf_atr_mult: 0.25
    sl_buffer_mult: 0.35
    sl_min_atr_mult: 0.5
    sl_min_pct: 0.003
    t1_pad_mult: 0.15
    clv_long_min: 0.50
    clv_short_max: 0.50
    rvol_min: 1.2
    rvol_strong: 1.8
    min_session_vf: 0.8
    break_min_rvol: 1.5
    acc_min_rvol: 1.5
    max_chase_atr: 1.0
    min_headroom_r: 1.5
    max_tp_r: 2.0
    require_htf_close: true
    htf_minutes: 15
    entry_window: ["09:45", "11:45"]     # signal-bar open, half-open
    afternoon_window: null               # off in v1.0 (owner decision 2026-10-08)
    counter_bias_veto: {enabled: true, trend_ext_pct: 100, two_sided_ext_pct: 15}
    gap_filter: null                     # excluded from v1.0 (item 8); known Pine defect
    nr_filter: null                      # dropped (item 9)
  inputs:                                # -> build_breakout_inputs
    atr_len: 14
    ema_len: 9
    ib_minutes: 60
    orb_minutes: 15
    adr_days: 14
    volume_days: 14
    va_pct: 70.0
    rows_per_tick: 5
    continuous_only: true
  policy:                                # -> strategy_core.trade_policy (proposed)
    max_entries_per_direction_per_symbol_per_day: 2   # counted on accepted, filled entries (item 15)
    max_concurrent_positions_per_symbol: 1
    limits_scope: per_strategy_book         # item 16
    reentry_requires_flat: true
    reversal_requires_flat: true
    allow_longs: true
    allow_shorts: true
    max_entry_slippage_r: 0.5
    close_on_tp_overshoot: true
    bad_fill_consumes_entry: true
    time_exit: "14:45"
    product: MIS
  exits:
    plan: tp1_only
    exit_owner: ladder
    stop_before_target_same_bar: true
    tp_fill: touching_bar_close
    time_exit_fill: bar_open
  universe:
    research: current_fno_all
    deployment: {list_id: breakout-deploy, version: <owner supplies>}
  evaluation:
    economic_view: all_realized_events
    strategy_view: decided_exits_only
```

---

## 1. Universe

Status: FROZEN. The owner supplies the first deployment list version.

Today: TradingView watchlist (not in the repository; a generated 20-symbol list is at
`signal_engine/strategy_library/intraday/intraday-stocks-watchlist-tradingview`, written by
`signal_engine/scripts/watchlist_screen.py:109`); runner static 40 symbols (`CFG:533-572`); backtest
full F&O (about 209). The backtest engine applies neither the price band nor the blacklist
(`ENG:310-342`).

Canonical (owner decision 2026-10-07):
- `universe.deployment`: an explicit, versioned INPUT supplied by the owner, for paper and live. Not
  in strategy code. Proposed file: `signal_engine/strategy_library/intraday/orb/universe/breakout-deploy-<YYYYMMDD>-<NNN>.txt`,
  header with source, date, author, screen used. The runner reads the version named in config. A new
  list version is a universe change, not a strategy revision; it is stamped on every trade.
- `universe.research`: the full NSE F&O equity universe on the published dataset, for evidence
  backtests. A second report on the deployment list is optional, never the headline.
- Eligibility filters apply in every mode, backtest included: entry price band [300, 5000] on
  `signal_ref`; strategy blacklist (BHEL) and global blacklist (YESBANK) (`CFG:424-458`); broker MIS
  eligibility (live only, logged by name).

---

## 2. Entry conditions

Status: FROZEN. Condition 4 (gap gate) is excluded from v1.0.

An entry candidate exists for symbol s on signal bar i (closed bar, broker data, IST) when all hold:
1. A setup resolves with direction d (item 5).
2. `score >= 7` (item 10).
3. HTF gate, headroom gate (`>= 1.5R` to the ADR projection, na passes), TP-reach gate
   (`|T1 - signal_ref| / R <= 2.0`, na passes), chase gate (`|close - level| / ATR <= 1.0`, na passes),
   session-volume floor (`sess_vf >= 0.8`, na passes) (`CORE:262-289`; `PINE:3642-3661`).
4. Gap gate: EXCLUDED from v1.0 (item 8), a known Pine defect.
5. Counter-bias veto passes (item 7).
6. Signal-bar open time in [09:45, 11:45) (item 13).
7. Cooldown: more than 3 bars since the previous candidate fire on s that passed conditions 1-6.
   The cooldown is a signal rule and does not depend on execution state. (Pine couples it to the
   slot, `PINE:3700-3705`; that coupling is PINE-ONLY and not adopted.)
8. The shared trade policy accepts it (items 15, 16, 17). The policy runs outside the core.

No gate is re-evaluated on the fill bar, except the engine halt and the trade policy.
Not in v1.0: ORB breakout entries (off in Pine, `PINE:176`), VA acceptance (off), NR filter (item 9),
afternoon window (Q5), the Pine requirement that the ORB has formed (`PINE:2792`, PINE-ONLY; the
ORB is always formed by 09:45 on complete data).

---

## 3. Signal timestamp convention

Status: FROZEN.

- `signal_time` = open time (IST) of the bar whose close produced the decision.
- `sig_id` = `{SYMBOL}-{YYYYMMDD}-{HHMM of signal_time}`; tags other than `BREAKOUT` are prefixed
  (`signal_engine/pipeline/signals.py:24-33`).
- Pine stamps the fill bar (`PINE:2844`, `PINE:2967`) and sends the alert one bar later. Ledger rows
  (2026-10-05 HDFCBANK and PFC) confirm a one-bar offset. Parity rule: Pine sig_id HHMM minus one
  bar = spec sig_id. This offset is classified "timing", never logic. The spec does not reproduce
  Pine's alert timing (owner decision 2026-10-07).

---

## 4. Level definitions

Status: FROZEN, with one implementation check.

All from broker data (Historify history, broker live bars), IST:
- `pdh`, `pdl`: high and low of the previous session, continuous market (bars stamped before 15:15).
- `ib_high`, `ib_low`: running extremes of bars with open in [09:15, 10:15); frozen after;
  `ib_done` true from the 10:15 bar.
- `orb_high`, `orb_low`: extremes of bars in [09:15, 09:30); used only in the confluence and target
  registry.
- `pvah`, `ppoc`, `pval`: previous session volume profile from 1-minute bars before 15:15, row =
  5 x broker tick, value area 70 percent.
- `htf_close`: close of the last 15-minute bucket (anchored 09:15) that completed before the bucket
  that contains the signal bar (`signal_engine/features/htf.py:22-34`).
- `adr`: mean (high - low) of the 14 sessions before today.
- `atr`: Wilder ATR(14) on 5-minute bars. `vwap`: session VWAP. `ema9`, `ema_slope = ema9 - ema9[1]`.
- Registry for confluence and targets: PPOC, PVAH, PVAL, PDH, PDL, IBH, IBL, ORBH, ORBL (`CORE:63`).

Implementation check before freeze (HYPOTHESIS): `signal_engine/pipeline/context.py:101` applies
`continuous_session` to the 1-minute frame; it is not verified that the 5-minute frame used for
`pdh`, `pdl`, `adr` and the first HTF bucket excludes 15:15-15:30 auction bars. If it does not, that
is an implementation fix (the definition above is the spec), not a revision.

---

## 5. Retest and break rules

Status: FROZEN (break stamps reset at each session start, 5.1).

- Break stamp: on a closed bar, close crosses the level (`close > L and close[1] <= L` for highs,
  mirrored for lows) for PVAH, PVAL, PDH, PDL, IBH, IBL (`CORE:127-135`; `PINE:3390-3410`).
- Retest: a break stamp within 6 bars INCLUDING the current bar, `low <= L + band` (long) and
  `close > L` and a bull candle (CLV >= 0.50); mirrored for short. Band = 0.25 x ATR.
- v1.0 freezes the current documented behaviour: the break bar itself can qualify as a retest, so it
  outranks `-BRK` and collects the +2 retest score (`BMD` 2026-08-28 "Found: the -BRK branch is
  unreachable"; `CORE:26-28`). Owner decision 2026-10-07. The corrected rule (break at least one bar
  before the retest) is revision v1.1, tested separately. It must not be changed silently.
- Resolution order (`CORE:47-62`): VAH-RT, VAL-RT, PDH-RT, PDL-RT, IBH-RT, IBL-RT, VAL-REJ, VAH-REJ,
  VAH-ACC, VAL-ACC (off), PDH-BRK, PDL-BRK, IBH-BRK, IBL-BRK. IB setups need `ib_done`.
### 5.1 Break-stamp state (FROZEN, owner decision 2026-10-08, Option A)

What is carried within a session. One integer per level (PVAH, PVAL, PDH, PDL, IBH, IBL): the bar
index of the latest break of that level (`_break_bars`, `CORE:107-109`). Nothing else.

Valid. A stamp is valid for the retest test while `bar_index - stamp_index <= 6`
(`retest_max_bars`), counted in 5-minute bars of the SAME session.

Expires. After 6 bars, when a newer break of the same level overwrites it, or at the first bar of the
next IST session, whichever comes first. All six stamps are cleared at the first bar of each IST
session (`break_stamp_carries_across_sessions: false`). A break in session D never provides a retest
in session D+1. On the first bars of a session there is no valid stamp until a break happens in that
session.

Next-session trigger. A level is retested only against a break made in the same session. A trigger on
the first bars of D+1 can come only from setups that need no stamp (REJ, ACC, BRK families) and only
inside the entry window.

Evidence for the freeze (run 2026-10-08, scratch only, nothing recorded): 202 eligible symbols of
the published dataset `nse-intraday-5m-20261006-001`, full history, engine path of
`gap/bt_run.py`, baseline versus stamps cleared. Result: 99,116 trades in each arm, ZERO trades
changed in any symbol (24,061 + 23,351 + 26,978 + 24,726). The per-bar setup resolution differed on
6,567 bars over 1,977 days and 202 symbols, every one between 09:15 and 09:40 (09:15: 4,515; 09:20:
1,041; 09:25: 522; 09:30: 287; 09:35: 146; 09:40: 56), before the 09:45 entry window opens. Those were
carried stamps retesting the new day's rolled levels (VAL-RT or VAH-RT in the baseline, no setup or
a different one in the patched arm). They are outside the entry window and produced no signal and no
trade. A signal in this specification includes the entry window (item 2, condition 6).

---

---

## 6. Day type

Status: FROZEN.

Port of `PINE:2468-2485`, computed causally on each closed bar of the session:
- Before `ib_done`, or when `ib_high <= ib_low`: day type `"-"` (unclassified).
- `ib_range = ib_high - ib_low`.
- From the 10:15 bar onward, running maxima over the session's post-IB bars, both starting at 0:
  `ext_up = max(ext_up, (High - ib_high) / ib_range x 100)`,
  `ext_dn = max(ext_dn, (ib_low - Low) / ib_range x 100)`. Reset at session start.
- `mx = max(ext_up, ext_dn)`; `two_sided = ext_up > 15 and ext_dn > 15`;
  `ib_adr_pct = ib_range / adr x 100`; `narrow = ib_adr_pct < 35`; `wide = ib_adr_pct > 60`
  (na `adr` makes both false).
- Classification, first match: two_sided -> Neutral; mx > 100 -> Trend if narrow else Double
  Distribution; mx > 50 -> Normal Variation; mx < 10 -> Non-Trend if narrow else Normal; wide ->
  Normal; else Normal Variation.
- Inputs exist in `features/`: `initial_balance` (`ib_high`, `ib_low`, `ib_done`), `adr_prev`, and
  bar High and Low (`signal_engine/strategy_core/inputs.py:76-98`).
- Day type is logged in each signal's context (`day_type`, `ext_up`, `ext_dn`). Its only trading use
  is item 7. Open type and the bias text (`PINE:2428-2502`) are display only, not part of the spec.

---

## 7. Counter-bias veto

Status: FROZEN. Included in v1.0 (owner decision 2026-10-07). Python implements it
explicitly inside the core (it must not stay an optional `bias_ok` input that defaults to True,
`CORE:290-292`).

Rule (`PINE:3646-3648`, `PINE:434`):
- `trend_day = day_type in {Trend, Double Distribution}` at the signal bar (item 6).
- `bias_long = ext_up > ext_dn`.
- Veto when `trend_day` and `(d == 1) != bias_long`. Otherwise pass. Unclassified days pass.

Equivalent reduced form (derived from item 6; both Trend and Double Distribution veto, so the IB
width and ADR do not change the veto): veto when `ib_done` and `ib_range > 0` and
`max(ext_up, ext_dn) > 100` and not `(ext_up > 15 and ext_dn > 15)` and the setup direction is
against `sign(ext_up - ext_dn)` (a tie counts as short bias, as in Pine). Implementations must give
the same result as the full classifier; a unit test asserts it.

Evidence status: documented intent (`BMD` 2026-08-22q), never measured. The v1.0 run with the veto
is the first measurement; a run without it is a separate trial.

---

## 8. Gap filter

Status: EXCLUDED from v1.0 (owner decision 2026-10-08). The evidence (scratchpad
`GAP-FILTER-EVIDENCE.md`) shows the Pine gap filter never acted. It is recorded here as a KNOWN PINE
DEFECT: an UNIMPLEMENTED rule. v1.0 was frozen without a gap gate, so v1.0 is NOT a test of the gap
filter and no evidence for v1.0 may be described as gap-filtered.

Known Pine defect record:
- The Pine input `enableGapFilter = true`, `maxGapPercent = 2.5` exists (`PINE:321-322`) but with the
  native session the gap block never runs after the first bar of history, so `gapFilterPassed` stays
  true and no signal was ever rejected by it. Every past Pine trade was produced without a gap gate.
- Fixing or removing the dead Pine code is a separate task, after a check in TradingView. It is not
  part of v1.0 and not a parity fix. If a gap gate is wanted later it is a new version (v1.1 or later)
  with its own trial, and the previous-close definition below is its first open question.

The remainder of this section is history and the draft definition if the filter is ever adopted.

Rule as Pine codes it:
- Inputs `enableGapFilter = true`, `maxGapPercent = 2.5` (`PINE:321-322`).
- On `isNewSession`: `todayOpen := open`; if `prevDayClose` is known and > 0,
  `gapPercent = abs((todayOpen - prevDayClose) / prevDayClose x 100)` and
  `gapFilterPassed = not enableGapFilter or gapPercent <= maxGapPercent`; else `gapFilterPassed = true`
  (`PINE:2161-2174`).
- `prevDayClose := close[1]` on the first bar where `not inSession and inSession[1]` (`PINE:2186-2188`).
- Gates key-level entries at arming (`PINE:2768`) and again at the fill bar (`PINE:2779-2780`).
- Hypothesis under separate measurement: with the native session (`PINE:539`, `PINE:1985`) the gap
  block never runs after the first bar of history, so the filter never acted. Result to be supplied.

Draft definition (proposed if adopted):
- `gap_pct = abs(session_open - prev_close) / prev_close x 100`; `session_open` = open of the 09:15
  bar; `prev_close` = OPEN QUESTION (last continuous bar close before 15:15, or the official closing
  auction price). Pass when `gap_pct <= 2.5`, or when `prev_close` is unknown. Evaluated once per
  session, for all setup families. Not re-checked at fill.

Decision rule: include in a version only if a measurement on the research universe supports it,
recorded as its own trial. If Pine never applied it, every past Pine trade was produced without it;
adding it is then an intentional revision relative to what traded, not a parity fix.

---

## 9. NR filter

Status: FROZEN: dropped from v1.0.

Classification: intentional strategy revision relative to the Pine input set, not a parity failure.
Rationale:
- In Pine it never affects trades: `enableNRFilter` is false (`PINE:351`) and the mode is the constant
  `"Prefer"`, under which the gate is always open (`PINE:2728`).
- No document states it as intended trading behaviour for BREAKOUT. `BMD` calls the NR gate
  "genuinely universal" only as a gate that stays wired if switched on (`BMD:2364-2368`).
- Independent evidence is against it: the standalone NR breakout study found the shipped NR7
  `require_nr` defaults "decisively negative"
  (`signal_engine/strategy_library/intraday/nr-breakout/STRATEGY-ANALYSIS.md:44`).
- Action: remove NR from the deferred list in `CORE:30-32` when the spec is frozen.

---

## 10. RVOL, session volume and score

Status: FROZEN.

- Per-slot baseline: for each 5-minute slot of the session, mean bar volume at that slot over up to
  13 prior sessions (14-session buffer, today excluded), at least 3 sessions, else unknown
  (`PINE:1098-1144`; `inputs.py:80`).
- `rvol` = bar volume / slot baseline; 0 when the baseline is unknown.
- `sess_vf` = cumulative session volume / mean cumulative volume at the same slot; unknown passes.
- Retest setups are scored on the highest `rvol` of the last 6 bars (`CORE:221`; `PINE:3608`).
- Score (`CORE:189-197`; `PINE:3516-3549`): 2 + min(conf, 3) + 2 if conf >= 2 + 2 if retest + 1 if close
  on the trade side of VWAP + 1 if close on the trade side of EMA9 with matching slope + (2 if
  `rvol >= 1.8`, else 1 if `rvol >= 1.2`) + 1 if the candle side matches (CLV) + 1 if `sess_vf >= 1.2`.
  `conf` = number of OTHER registry levels within 0.25 x ATR of the setup level, excluding the level
  itself (distance > tick / 2).
- Threshold 7. HTF gate passes when: HTF not required, `htf_close` or level unknown,
  `rvol >= 1.8`, a `-BRK` with `rvol >= 1.5`, or `htf_close` beyond the level in the trade direction.
- Thresholds were tuned on TradingView inputs; broker data may need re-validation (memory
  `feedback_prefer_broker_data.md`). That is research, not a spec change.

---

## 11. Stop loss

Status: FROZEN.

- `signal_ref` = close of the signal bar.
- `raw = level - d x 0.35 x ATR`; `min_dist = max(0.5 x ATR, 0.003 x signal_ref)`;
  `sl = min(raw, signal_ref - min_dist)` for long, `max(raw, signal_ref + min_dist)` for short
  (`CORE:244-250`; `PINE:3575-3590`).
- `R = |signal_ref - sl|`. The stop is fixed at the signal; it is never re-derived from the fill.
- Validator floor `min_sl_pct = 0.002` of `signal_ref` (`CFG:928`, `CFG:934`; `VAL:118-129`).
- Broker order: SL-M with the trigger on a valid tick (`executor.round_to_tick`).

---

## 12. Exit plan

Status: FROZEN.

Owner decision 2026-10-07: `tp1_only` in v1.0; the Ladder owns the stop state; no tracker no-progress,
loss-cut or profit-lock for BREAKOUT-PY; other exit variants are later revisions.

- Targets from `signal_ref` (`CORE:344-354`): `t1` = nearest registry level beyond `signal_ref` in
  direction d, padded 0.15 x ATR short of it (NaN if the pad leaves no room);
  `dist = 1.5R` if no `t1`, else `max(|t1 - signal_ref|, R)`; `tp1 = signal_ref + d x dist`.
  TP2 and TP3 are computed and logged, not traded in v1.0.
- Plan `tp1_only` (`DRV:33`): the whole position exits at TP1 or the stop. No stop move.
- Exit owner `ladder` (`CFG:584`; `TRK:84-91`): the tracker does not apply no-progress, loss-cut or
  profit-lock. Forced exits: time exit (item 19), bad-fill close (item 18), engine halt and manual.
- Bar processing order on each closed bar: (1) stop: if the bar opens beyond the stop, exit at the
  open (`SL_GAP`); else if the bar's range reaches the stop, exit at the stop (`SL`); (2) only if the
  stop did not trigger: if the range reaches TP1, exit at the CLOSE of that bar (owner decision
  2026-10-07). Live: the stop is the broker SL-M (intrabar); TP1 is a MARKET exit sent after the
  touching bar closes (`MAIN:1597-1605`; `DRV:178-196`).
- Differences with today's code (implementation changes required): Ladder fills TP at the target price
  (`EXITS:115`); SimSink uses the Ladder price (`SIM:232`); BREAKOUT backtest default is the legacy
  single TP/SL path (`TYPES:45`, `ENG:242-272`). Pine BREAKOUT live uses a 30/35/35 ladder with
  `MAIN._replace_runner_sl` and tracker no-progress; that is Pine-path behaviour, not v1.0.

---

## 13. Fill timing and entry window

Status: FROZEN. Afternoon window off.

Owner decision 2026-10-07: canonical execution model; Pine alert timing is not reproduced.
- Decision at the close of the signal bar. MARKET entry order sent at once.
- Backtest and SimSink: entry fill = open of the next bar (`ENG:155-159`; `SIM:114-116`).
- Live and paper: the actual fill; its distance from `signal_ref` is execution cost (`ACC` G7.3) and
  feeds item 18.
- Sizing, R, targets, the slippage guard and the validator use `signal_ref`.
- Entry window: signal-bar open in [09:45, 11:45). The last signal bar is 11:40; its fill at the 11:45
  open is allowed. (Pine cancels an 11:40 arming at fill, `PINE:2744`, `PINE:2749`, `PINE:2779`; PINE-ONLY.)
- Afternoon window: off in v1.0 (owner decision 2026-10-08). Value `null`.

---

## 14. Position sizing

Status: FROZEN (deployment rule referenced by the spec).

- `qty = floor(capital x 0.01 / (R x 1.10))`, capital = the strategy's day-start capital
  (`RISK:554-582`; `CFG:200`, `CFG:229`; `use_day_start_capital`). Skip when `qty = 0`.
- Price band on `signal_ref` (item 1). Soft-blacklist scaling (`RISK:521-552`). Margin scaling
  `MAIN.adjust_qty_for_margin` (`MAIN:126`).
- The backtest cost model sizes notional with the same formula, including the 1.10 factor (today it
  omits it, `signal_engine/backtest/costs.py:180-181`; implementation fix).

---

## 15. Re-entry rule

Status: FROZEN. Cap N = 2 (owner decision 2026-10-08).

Owner decision 2026-10-07: one entry per symbol per direction per trading day; a direction may be
re-entered after the previous position is flat; a reversal only after the existing position is flat;
no simultaneous entries in the same symbol and direction; identical in backtest, SimSink/paper and
live. Earlier owner text: "LONG, exit, LONG again is allowed ... unlimited re-entries never".

The two sentences conflict if "one entry per direction per day" is read as a daily count of one: that
count forbids "LONG, exit, LONG again". The draft made that mistake. This candidate reads "one entry"
as one OPEN position per symbol per direction, and needs a numeric daily cap to satisfy
"unlimited re-entries never".

Canonical rule (frozen parts):
- Key: (strategy tag, symbol, IST trading day, direction). Direction = LONG or SHORT of the entry.
- At most one open position per (strategy tag, symbol). No pyramiding, no simultaneous long and
  short. A signal while a position or an unfilled entry order exists is declined (`POSITION_OPEN`).
- Same-direction re-entry: allowed only after the previous position is fully flat.
- Reversal: allowed only after the existing position is fully flat.
- Daily counter per direction moves when an entry FILLS (any quantity). A declined, rejected,
  unfilled or skipped signal does not move it. A bad-fill close DOES move it (owner decision 2026-10-07).
- Counters reset at the first bar of each IST session. Live and paper rebuild them at start-up from
  trades.db entry rows with a fill for that tag, symbol and day.
- The same function decides in every mode (shared-rule design).

Frozen: `max_entries_per_direction_per_symbol_per_day` = 2 (the first entry plus one re-entry after
flat). The day is bounded at 4 entries per symbol (2 long, 2 short). A sequence LONG, exit, LONG is
allowed and is the second entry of that direction.

The counter counts ACTUAL ACCEPTED ENTRIES, not generated signals (owner 2026-10-08): it moves when an
entry order is accepted and fills (any quantity), including an entry that a bad-fill guard closes at
once. A signal that is generated and then declined by the policy, risk engine or validator, an order
the broker rejects, an order that does not fill, and a skipped signal do NOT move it.
Ledger fact for scale: BREAKOUT-PY took INFY LONG three times on 2026-10-01 (scratchpad copy of
trades.db), so today's paper path already exceeds N = 2.

---

## 16. Daily and portfolio limits

Status: FROZEN. Limits apply per strategy book (owner decision 2026-10-08); no same-side cap.

Owner decision 2026-10-07: paper uses the SAME trading limits as live.
- Limits are deployment-profile rules, referenced by the spec, applied identically in paper and live:
  daily loss 4 percent (realised plus unrealised), weekly 8, monthly 15, `max_trades_per_day` 10,
  `max_open_positions` dynamic from capital (-1), `max_positions_per_symbol` 1, sector cap off
  (`CFG` `mode_profiles.live`, `risk`; `RISK:384-428`, `RISK:591-632`).
- Realised losses from bad-fill closes and time exits reach the loss limits (`INV` I4).
- Backtest: per-trade evidence is uncapped; the run record adds a portfolio replay under the same
  limits (first come, first served, `signal_engine/backtest/concurrency.py`), reported beside it.
- Contradiction in the repository today: `CFG` `mode_profiles.analyze` sets every cap off
  (`max_open_positions: 0`, `max_trades_per_day: 0`, loss limits 1.0).
- Scope (frozen): the limits are applied PER STRATEGY BOOK. Paper applies the live numbers to
  BREAKOUT-PY's own book. In live the risk engine pools slots and capital across strategies in one
  account (`RISK:203-215`, `_key` pooled in live); that pooling is a live-account effect that the
  per-strategy paper cannot reproduce, and reports must say so.
- Same-side concurrency cap (`BMD` 2026-09-06 Finding 4): NOT in v1.0.

---

## 17. Conflict handling

Status: FROZEN. Pine BREAKOUT order dispatch stops on the BREAKOUT-PY universe during parity (owner decision 2026-10-08).

Frozen parts:
- One strategy tag never holds two positions in one symbol and never opposite directions at once
  (item 15).
- Cross-strategy conflicts are an account rule, not a strategy rule. At cutover exactly one tag
  trades BREAKOUT (`signal_engine/pipeline/single_source.py:61-90` enforces one signal source per tag).
- Ledger invariant `INV` I2: one close produces at most one ledger close event and one risk-counter
  update. The KOTAKBANK 2026-10-05 double booking (rows 948 and 949) was a real I2 violation, now
  fixed in the working tree and under review (coordinator, 2026-10-07; `INV` "Known gaps").

Frozen Q10: during parity, OpenAlgo's sandbox nets positions per symbol across tags (`INV` "Netted
sandbox positions"; `RSPEC` "Update 2026-10-01"). Pine BREAKOUT order dispatch STOPS on the
BREAKOUT-PY universe, so Python is the sole execution source and duplicate paper trades cannot
contaminate the comparison. Pine alerts are kept for the shadow comparison only. The dispatch change
is an operational switch (not a strategy rule) and is made when parity starts.

---

## 18. Bad-fill policy

Status: FROZEN.

Owner decision 2026-10-07: actual entry fill observed; on a violation close at the fill-bar open; ZERO
gross R for the strategy event; record the actual realised financial loss; the entry counts as used
for that direction; the actual loss goes to the risk engine.

Condition (one shared function):
- Inputs: direction d, `signal_ref`, `sl`, `tp1`, `fill`. `R = |signal_ref - sl|`.
- `slippage_r = d x (fill - signal_ref) / R`.
- Violation when `slippage_r > max_entry_slippage_r` (0.5, spec value), or `d x (fill - tp1) >= 0`
  (TP overshoot), or `d x (fill - sl) <= 0` (STOP_BREACHED: a gap through the stop).
- ERRATUM, owner decision 2026-10-08 (Option A). The first text of this item said a fill through the stop
  is "slippage of at least 1R". That is false: a long that gaps down fills at a LOWER price than the
  signal, so its slippage is negative. STOP_BREACHED is its own bad-fill event, exit type
  `ENTRY_REJECTED_STOP_BREACHED`, class `BAD_FILL_CLOSE`, in every mode. `BAD_FILL_CLOSE` does not mean a
  fixed loss: the R of the close comes from the actual fill against the entry and the risk.
  `SL_GAP_ENTRY` does not exist for this strategy (the bad-fill model replaces it). This changes
  no frozen rule other than naming and classifying that case; the economics (close at the fill-bar
  open, zero gross R, real cost) are as before.
- Source today: `MAIN.entry_slippage_r` (`MAIN:1643-1655`), `MAIN._auto_close_on_bad_fill`
  (`MAIN:1676-1755`), global `bracket.max_entry_slippage_r: 0.5` (`CFG:797`).

Action and record (live, as implemented):
- Cancel the SL-M, market close the full quantity, check the close went through; if the broker still
  holds the position, restore the stop, write no close row, move no counter (`INV` I4, I5).
- On a confirmed close: `risk_engine.record_close(realized_pnl, strategy, symbol)`; ledger row with
  `exit_class = BAD_FILL_CLOSE`, `exit_type` `ENTRY_REJECTED_SLIPPAGE` or `ENTRY_REJECTED_TP_OVERSHOT`
  (`signal_engine/db.py:370-426`; `MAIN:1823-1846`).
- Required fields: `sig_id`, strategy, symbol, direction, quantity, `signal_ref` (`signal_entry`),
  `sl`, `tp1`, `R`, `max_entry_slippage_r`, `entry_order_id`, `entry_fill`, `slippage_r`,
  `slippage_rs` (adverse rupees per share x quantity), `exit_order_id`, `exit_fill`, `pnl` (realised
  rupees), `exit_class`, `exit_type`, `reason`, `strategy_version`.

Backtest and SimSink model:
- `fill` = open of bar i+1. On a violation the position closes at that same open.
- Strategy view: `r_gross = 0` (zero gross R for the strategy event).
- Economic view: realised loss = the round-trip cost of both legs under the primary cost model at the
  deployed notional (item 20); `r_net = -cost / R`. The entry counter for that direction moves.
- This replaces today's BREAKOUT backtest behaviour (fill through the stop closed as `SL_GAP_ENTRY`,
  `ENG:205-219`; no 0.5R guard; skip-at-fill off, `TYPES:64`, `BTAD:60`).
- Known limit: the backtest cannot see intrabar fill drift; in live the same event has a non-zero
  adverse fill-to-close move, which the economic view carries as the real loss.

---

## 19. Time exit

Status: FROZEN. Exact mapping in 19.1 (owner approval 2026-10-08).

Owner decision 2026-10-07: time exit at the 14:45 OPEN.
- `time_exit = 14:45` IST, MIS. Live: market close of all open positions at 14:45:00 wall clock
  (`TRK:1866-1905`, `TRK:1573-1613`). Backtest and SimSink: after the 14:40 bar is processed (stop,
  then TP at close), any open position exits at the OPEN of the first bar whose open is at or after
  14:45. Today both exit at that bar's close (`ENG:234`, `ENG:265`; `SIM:128-131`): implementation change.
- Ledger: `exit_class = TIME_EXIT` row per position (now written in live, coordinator 2026-10-07);
  the time exit holds `_time_exit_active` and skips positions already booked (`INV` I2).
- Start-up refuses a mismatch between `policy.time_exit` and `CFG time_exit` (`CFG` `time_exit: 14:45`).
- Must stay inside the profile's square-off rule: latest exit-bar close 14:55, force-flat 15:00
  (`research/profiles/flattrade-nse-cash.yaml`).

### 19.1 Time-exit mapping (FROZEN, owner approval 2026-10-08)

Trigger. The trigger is the clock reaching 14:45:00 IST. In bar terms it is the 5-minute bar that
opens at 14:45 (it covers 14:45 to 14:50).

Last bar that manages the position normally. The bar that opens at 14:40 (covers 14:40 to 14:45).
On it the order is: stop first, then TP at the bar close (items 15 and 16). A position still open
after that bar is closed at the next event.

Exit price.
- Backtest and SimSink: the OPEN of the 14:45 bar, with the profile's market-exit slippage and
  costs applied as for any other MARKET exit. The 14:45 bar is not evaluated for stop or TP: the
  position is already closed at its open.
- Live and paper: a MARKET close order is sent at 14:45:00 wall clock. The fill price is the
  broker fill. The stop order is cancelled before the close order (OCO rule, so the broker does not
  read the close as a new short).
- If no bar opens exactly at 14:45: the first bar whose open is at or after 14:45 is used (this is
  still the normal rule and the exit is a normal `TIME_EXIT`). If NO bar at or after 14:45 exists
  before 15:00, the position is closed at the last known close as an EXCEPTION: the close is
  explicitly flagged `DATA_GAP`. A `DATA_GAP` close is NOT a normal `TIME_EXIT` observation: it is
  excluded from parity counts and from v1.0 evidence for that run/date, reported separately with
  its date and symbol, and never silently substituted into the normal evidence. The money still counts
  in net P&L and risk counters (it was a real or modelled close), so no loss disappears.

What happens at the boundary, case by case.
- Position open with stop and TP untouched at 14:40 bar end: closed at the 14:45 open. Class
  `TIME_EXIT`.
- Stop or TP hit inside the 14:40 bar: closed by that rule before the boundary. Not a time exit.
- Stop level would have been hit inside the 14:45 bar: ignored in backtest (closed at open). Live
  sees the same result because the close is sent at 14:45:00.
- Entry signal: none can exist. The last entry signal bar is 11:40 (item 13), so no entry is pending
  at 14:45. A position opened by a bad fill is already handled by item 18.
- Position already booked by another path (stop fill found by the poll, signal exit): skipped, never
  booked twice (`INV` I2).
- Time-exit close fails and the broker still holds the position: no ledger row, no risk-counter
  change (`INV` I4, I5). The tracker keeps it open and logs an error; the broker's own square-off
  is the last backstop.

Accounting. One `TIME_EXIT` ledger row per successfully booked position (`exit_class = TIME_EXIT`). It counts in net P&L,
risk counters and reconciliation. It is excluded from win rate and average R, and listed with its
count in the strategy view (`INV` I3).

Config check. Start-up refuses a mismatch between `policy.time_exit` and `CFG time_exit`
(`14:45`). The close must stay inside the profile square-off rule (force-flat 15:00).

Today's difference (an implementation change, behavior-changing, step S7). Backtest and SimSink exit
at the close of the 14:45 bar. The frozen rule is the open.

Closed (Q19): the live 14:45:00 wall-clock close is the event the backtest's 14:45-bar open models.
The runner stops at 14:45 and hands open positions to the tracker (`pipeline/service.py:62-64`, `:341`).

---

## 20. Costs and the two evaluation views

Status: FROZEN. The two-view wording for `INV` I3 and `ACC` G1.2 below is APPROVED (owner 2026-10-08).

Costs:
- Primary: deployment profile broker statutory per-trade model at the deployed notional (Flattrade,
  zero brokerage) plus 4 bps slippage per side (`signal_engine/backtest/record_run.py:125`, `:215-253`;
  `signal_engine/backtest/costs.py:189-191`; `ACC` G1.1). Stress at 2x slippage (G1.3). The flat 16 bps
  and a generic broker are comparison scenarios only.
- Paper and live reports restate each trade under the same primary model, so the three modes show one
  cost line. Sandbox charges are not assumed (HYPOTHESIS: the sandbox books no statutory charges).

Two views (owner decision 2026-10-07), reported side by side in every run record, paper report and
live report:
- Economic view: ALL realised trades and events, including `BAD_FILL_CLOSE`, `TIME_EXIT` and other
  execution-protection closes. Total and net P&L, net expectancy per trade, drawdown. This view
  drives the economic viability gates (G1). No loss ever leaves net P&L or expectancy.
- Strategy view: normal strategy decisions and exits only (`SL`, `SL_GAP`, `TP`), with
  `BAD_FILL_CLOSE`, `TIME_EXIT` and other protection events listed separately with their counts and
  P&L. Used to diagnose whether the logic has edge (win rate, average R of decided trades, setup
  breakdown). Not used to pass G1.
- Classification source: `exit_class` on every ledger row, `db.NOT_DECIDED_EXIT_TYPES`
  (`signal_engine/db.py:378`). The backtest must emit the same classes.

Data caveat: ledger exit P&L for 2026-09-21 to 2026-10-01 is an estimate
(`docs/ACCOUNTING-DEFECT-2026-09-ESTIMATED-EXIT-PNL.md`) and must not be used as strategy evidence.

Approved wording changes (approved 2026-10-08; applied to `INV` and `ACC` in the repository after the move):

`INV` I3, replace the second bullet with:
> - What does it say about the strategy? A close the clock (`TIME_EXIT`) or the execution guard
>   (`BAD_FILL_CLOSE`) caused is a trade event, not a decided win or loss. Reports show two views.
>   The economic view includes every realized event in net P&L, expectancy and drawdown, and it
>   decides economic viability. The strategy view excludes these events from win rate and average R
>   and lists them separately. No realized loss is ever left out of the economic view.

`ACC` G1.2, replace the row with:
> | G1.2 | Net expectancy at 1.0x cost | At least +0.05R per trade, HARD, on the ECONOMIC view: all
> realized trades and events, including bad-fill closes (`BAD_FILL_CLOSE`) and time exits
> (`TIME_EXIT`). The strategy view (decided exits only, protection events listed separately) is
> REPORTED beside it for diagnosis and never passes this gate | H | R = the trade's own stop
> distance; a bad-fill close has zero gross R and its realized cost |

And add to `ACC` "Definitions": "Economic view" and "Strategy view" as defined above.

---

# Shared-rule design (updated for the owner decisions; PROPOSAL, nothing implemented)

## S.1 Differences between the modes today (unchanged facts, updated targets)

| Rule | Backtest engine | SimSink | Paper runner | Live path | v1.0 target |
|---|---|---|---|---|---|
| Re-entry | 2 per symbol per day, one per direction (`ENG:294-318`) | same at arm (`SIM:152-173`) | none (`CYC:206-240`) | per-strategy daily count across symbols; none per direction | item 15, one function |
| Concurrency | one position (`ENG:283`) | `SIM:157-158` | none before dispatch | 1 per tag (`RISK:702-711`) | 1 per tag, also pending orders |
| Limits | none in run | none | analyze: all off | live profile | live numbers in paper (item 16) |
| Bad fill | `SL_GAP_ENTRY` (`ENG:205-219`) | `SL_GAP_ENTRY` (`SIM:212-214`) | via `MAIN` | `MAIN:1676-1755` | item 18 in every mode (a gap through the stop is STOP_BREACHED, class BAD_FILL_CLOSE) |
| Counter-bias | not supplied | not supplied | not supplied | Pine only | inside the core (item 7) |
| TP fill | target price (`EXITS:115`) | Ladder price (`SIM:232`) | market after bar close | market after bar close | close of the touching bar |
| Time exit | 14:45 bar close | same | hands over at 14:45 | 14:45 wall clock | 14:45 bar open |
| Exit owner | Ladder or legacy | Ladder | Ladder | Pine: tracker plus no-progress | Ladder only |
| Evaluation views | one R list | one R list | ledger `exit_class` | ledger `exit_class` | two views everywhere |

## S.2 Module

`signal_engine/strategy_core/trade_policy.py`, pure (no I/O, no clock, no config reads), beside
`exits.py`.

```python
@dataclass(frozen=True)
class TradePolicy:                       # from spec.policy
    max_entries_per_direction_per_day: int    # frozen: 2
    max_concurrent_per_symbol: int            # 1
    allow_longs: bool
    allow_shorts: bool
    entry_window_min: tuple[int, int]         # (585, 705)
    time_exit_min: int                        # 885
    max_entry_slippage_r: float               # 0.5
    close_on_tp_overshoot: bool               # True

@dataclass(frozen=True)
class SymbolDay:                         # immutable value
    day: date
    long_fills: int = 0
    short_fills: int = 0
    open_direction: int = 0              # +1, -1, 0 flat
    pending: bool = False

def roll(s: SymbolDay | None, day: date) -> SymbolDay
def can_arm(p, s, direction, signal_bar_min) -> Verdict      # codes reuse backtest ledgers CODE_INDEX
def on_arm(s) -> SymbolDay
def on_fill(s, direction) -> SymbolDay                       # moves the direction counter
def on_unfilled(s) -> SymbolDay
def on_flat(s) -> SymbolDay
def bad_fill(p, direction, signal_ref, sl, tp1, fill) -> BadFill  # (violated, slippage_r, exit_type)
def time_exit_due(p, bar_open_min) -> bool
def rebuild(day, fills_and_open) -> SymbolDay                # restart from trades.db
def classify_exit(reason) -> str                             # STRATEGY_EXIT | TIME_EXIT | BAD_FILL_CLOSE
```

`can_arm` enforces item 15 in this order: direction allowed; window; no open position or pending
order (`POSITION_OPEN`); direction count below the cap (`DIRECTION_ALREADY_TAKEN`). Reversal and
same-direction re-entry need `open_direction == 0`.

Callers keep the state value: engine local per symbol; SimSink `dict[symbol, SymbolDay]`; runner a
`PolicyStateStore` in `runner_state.db` keyed (mode, day, strategy, symbol), rebuilt from trades.db
at start-up; live `MAIN._handle_entry` rebuilds from trades.db and the tracker before risk gates.
`MAIN._auto_close_on_bad_fill` calls `bad_fill` for the condition and keeps the order actions. The
ledger and the backtest both use `classify_exit`, so the two views come from one mapping.

Counter-bias and day type live in the core (items 6, 7), not in the policy: they are signal rules.

## S.3 Migration steps

| Step | Change | Test that fails today | Class |
|---|---|---|---|
| S0 | Approve spec; execute the onboarding plan (0.2); fix stale docs: `CORE:30-32`, `BTAD:12-18`, `RSPEC:31`, `BMD:525`, `BMD:1162` | `build_registry --check` with IDEA-031 | docs |
| S1 | `trade_policy.py` reproducing today's engine rules; engine and SimSink call it | bit-identical engine and P3 trade-parity tests | pure refactor |
| S2 | Cross-mode parity test: one scripted sequence (LONG fill, stop, LONG, LONG while open, SHORT after flat, third LONG) through engine, SimSink, runner cycle with fake dispatch, `MAIN._handle_entry` with fakes | runner and live accept what the engine declines | test only |
| S3 | Policy set to item 15; wired into `CYC._dispatch` and `MAIN._handle_entry` for BREAKOUT-PY | S2 green | behaviour (approved, N = 2) |
| S4a | `bad_fill` extracted; `MAIN` calls it | 20 fixed cases agree with today's live guard | pure refactor |
| S4b | Engine and SimSink: `BAD_FILL_CLOSE` at the fill-bar open, r_gross 0, counter used | a 0.6R adverse fill gives `BAD_FILL_CLOSE`; today the trade stays open | behaviour (approved) |
| S5 | Day type and counter-bias veto in the core; `bias_ok` input removed | trend-day counter-direction setup vetoed; reduced form equals full classifier | behaviour (approved) |
| S5b | Core resets break stamps at each IST session start (`break_stamp_carries_across_sessions: false`) | stamps from session D give no retest in D+1 (unit test); engine trades identical to today on the 202-symbol history (checked 2026-10-08) | behaviour in code, zero trade change |
| S6 | Ladder and SimSink: TP fill at the touching bar's close | Ladder test expects the bar close | behaviour (approved) |
| S7 | Time exit at the 14:45 open in engine and SimSink | engine test expects the 14:45 open | behaviour (approved; Q19) |
| S8 | `CFG mode_profiles.analyze` limits = live for BREAKOUT-PY (per strategy book) | 11th entry in a paper day declined | behaviour (approved) |
| S9 | Two-view reporting in `record_run`/tearsheet and the paper/live reports; cost model sizing with the 1.10 factor | run record lacks `economic` and `strategy` blocks | reporting |
| S10 | Spec loader: `BreakoutParams.from_spec`, version stamps, start-up mismatch refusal | config value different from spec refuses start | pure refactor if values equal |
| S11 | First v1.0 recorded run (onboarding step 7) | n/a | evidence |
| later | Gap gate (if ever wanted), v1.1 retest fix, afternoon window | separate trials | revisions |

Pure refactors: S1, S2, S4a, S10. Behaviour changes already approved by the owner decisions: S3 (cap
N pending), S4b, S5, S6, S7, S8. Each still needs the `BMD` layman entry and a version record.

---

# Owner decisions closed on 2026-10-08

- Q-NEW-1: cap 2 per symbol per direction per day, on accepted filled entries.
- Q4b: break stamps reset at each IST session start; stamps-cleared check passed (zero trade changes).
- Q5: afternoon window off.
- Q10: Pine BREAKOUT dispatch stops on the BREAKOUT-PY universe during parity.
- Q19: time-exit mapping approved, with DATA_GAP as an explicit non-evidence exception.
- Q-NEW-2: limits per strategy book. Q-NEW-3: no same-side cap in v1.0.
- Q-NEW-4: count every earlier variant in `--trials`; keep them out of the v1.0 evidence.
- Gap filter: excluded; known Pine defect recorded (item 8).
- I3 and G1.2 two-view wording approved.

Still open for later revisions, not blocking v1.0: gap rule previous-close definition (only if a gap
gate is ever adopted), the v1.1 retest fix, an implementation check that the 5m frame excludes
15:15-15:30 auction bars (item 4), the deployment universe list version.
