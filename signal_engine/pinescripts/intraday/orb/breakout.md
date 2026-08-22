# breakout.pine — Changelog

Key-level breakout strategy. Started as a byte-identical copy of `orb.pine`, then
extended so the Opening Range is one key level among several rather than the only one.

- **Source strategy**: `orb.pine` (unchanged, still the live strategy)
- **Merged from**: `../volume-profile/volume-profile-decision-assist.pine`
- **Model reference**: `../volume-profile/volume-profile-model.md`

---

## 2026-08-20 — Key-level merge (VP / PDH-PDL / IB)

Merged the volume-profile decision-assist logic into the ORB strategy. The premise: ORB
is a key-level breakout, and so are Value Area, Previous Day and Initial Balance breaks.
They share entry mechanics, so they should share one script.

### Design decisions

| Decision | Choice | Why |
|---|---|---|
| Merge mode | ORB **plus** new VA/PDH/IB triggers, each toggleable | The full key-level reading of the strategy; ORB logic untouched |
| Execution posture | **Alert-only by default** (`enableKeyLevelExecution = false`) | Observe and validate before risking capital on unproven setups |
| Session cap | Unchanged — **1 trade per session** | Preserves the existing risk profile and the signal_engine position budget |
| Trigger priority | ORB outranks all new families; within key levels retest > rejection > break | Never let an unvalidated setup pre-empt the proven signal |
| Level source | Auto-reconstruction ON, manual VAH/POC/VAL retained as escape hatch | Self-contained, with an exact fallback when accuracy matters |

### Inertness guarantee

The whole feature is additive. Only **8 lines** of `orb.pine` were modified:

| Change | Lines | Inert because |
|---|---|---|
| `buildEntryAlert()` gains a `srcTag` parameter | 1 sig + 1 body | `srcTag` is `""` for every ORB entry, and the `Trigger:` line is only emitted when non-empty |
| Two `buildEntryAlert()` call sites pass `klPendingSource` | 2 | `klPendingSource` is `""` unless a key-level setup armed the entry |
| Dashboard table grown 36 → 48 rows | 1 `table.new` + 3 `table.clear` | Capacity only; no cell contents change |

Everything else is new code gated behind `enableKeyLevels` / `enableKeyLevelExecution`.
With `enableKeyLevelExecution = false` (the default), `strategy.entry` is never called
for a key-level setup, so **the Strategy Tester report must match `orb.pine` exactly**.
That equality is the regression gate for this feature.

### What was added

**Level engine** (before `ORB LEVEL BUILDING`)
- Previous-session volume profile → VAH / POC / VAL, reconstructed from 1-min data via
  `request.security_lower_tf`, or typed in manually.
- Volume is spread evenly across every price row a bar spans, the same way TradingView's
  Session Volume Profile does. Assigning a bar's whole volume to one price was what put
  the POC ~26 points off TradingView's on TCS during the volume-profile work.
- Previous Day High / Low and daily ATR via `request.security(..., "D", x[1], lookahead_on)`.
- Initial Balance accumulator over a configurable window (default 09:15–10:15 IST).
- Derived context: auction state, opening position vs prior value, IB % of average daily
  range with a narrow/normal/wide day-type label.

**Setup + score engine** (after ORB breakout detection, so ORB wins the entry slot)
- Value Area: VAH/VAL rejection, acceptance, and breakout-retest.
- Previous Day: PDH/PDL break and break-retest.
- Initial Balance: IBH/IBL extension and extension-retest, active only once IB has closed.
- Unified confluence registry spanning VA + PD + IB **and** today's ORB levels — the
  source indicator had no access to ORB levels.
- Weighted score (confluence, retest structure, VWAP/EMA alignment, RVOL, candle close,
  delta proxy) with a configurable alert threshold.
- Level-based SL (ATR buffer beyond the defended level) and T1 = nearest structural level
  in the trade direction.

**Alerts**
- `buildKeyLevelAlert()` emits a decision packet in the existing Telegram envelope.
- Deliberately **not parseable as a trade signal**: `signal_engine/parser.py` keys off
  `Symbol:` / `Entry:` / `SL:` / `TP:` line prefixes, so the packet uses `Ref Entry`,
  `Ref SL`, `Ref T1` — the parser's `^(\w+)\s*:\s*(.+)$` cannot match across the space.
  An observation alert must never place an order by accident.
- When execution is enabled, the trade goes out through the unchanged `buildEntryAlert()`
  on the shared pending-entry path, so the executable format is identical to an ORB entry.

**Visuals & dashboard**
- Day-anchored dotted level lines with right-edge price tags, shaded value-area box,
  green/red signal triangles on setup change.
- Nine-row `── Key Levels` dashboard block with tooltips on every row.

### Fix — "The main body of the script is too long"

TradingView rejected the first version at compile time. Pine caps how much code may live
in the script's **global scope**, and `orb.pine` was already close to that ceiling before
anything was added: it carries ~2,226 global-scope statements, ~548 of them in the
DASHBOARD section alone. The key-level layer initially added ~440 more, inline.

Every stateful key-level block was moved into a function. Function *bodies* are separate
scopes and do not count toward the main-body limit, which is exactly what TradingView's
error message is pointing at.

| Block | Global statements before | After | How |
|---|---:|---:|---|
| Level construction | ~40 | 12 | `klProfile()`, `klInitialBalance()`, `klContext()` |
| Setup + score | ~230 | ~20 | `klCandle()`, `klBuildRegistry()`, `klTrackBreaks()`, `klResolveSetup()`, `klComputeScore()`, `klExecMap()`, `klFindT1()`, `klFireGate()` |
| Dashboard rows | 49 | 2 | `renderKeyLevels()`, mirroring the file's existing `renderFilterStatus()` |
| Visuals | ~35 | 6 | `klDrawLevels()`, `klDrawSignalLabel()` |

Net: the key-level layer's global-scope footprint dropped from **~440 statements to 104**
(`orb.pine` 2,226 → `breakout.pine` 2,330).

**The critical rule when wrapping state in functions**: a `var` declared inside a function
only advances on bars where the call is actually reached. So every stateful helper
(`klProfile`, `klInitialBalance`, `klTrackBreaks`, `klFireGate`, `klDrawLevels`) is called
**unconditionally**, with its enable flag passed in as a parameter and checked inside.
Guarding the *call* with `if enableKeyLevels` would silently corrupt the volume histogram
and the break-bar tracking.

Three things could not move into functions, because Pine forbids it:
- **`input.*` calls** (~28) must be in the global scope.
- **`plotshape()`** cannot appear inside a function, so the two signal-arrow plots and the
  three booleans feeding them stay in the main body.
- The **pending-entry assignment** stays inline: Pine functions may not assign to
  file-level variables, and `pendingLongEntry` / `pendingShortEntry` are file-level.

Also folded in the same pass: the three daily `request.security` calls became one tuple
call (the file already uses that form for the HTF bias), dropping `request.*` from 13 to
11. The three `request.security_lower_tf` calls were deliberately **left separate** —
merging them into a tuple would have saved two statements at the cost of relying on tuple
support in `security_lower_tf` that this codebase has never exercised. Not worth the
compile risk for two lines.

**If it still fails to compile**, the next levers, in order of cost: (1) set
`enableKeyLevels = false` to confirm the rest of the script is fine, (2) trim the verbose
`tooltip=` strings on the key-level inputs — they are large string literals sitting in the
global scope, (3) extract the read-only Range and Volatility sub-blocks of the ORB
dashboard into functions, the same way `renderFilterStatus` and `renderKeyLevels` already
are. Option 3 touches proven ORB code and was deliberately left undone.

### Naming — why everything is `kl`-prefixed

The source indicator's globals collided with existing identifiers in this file. Each was
renamed rather than shadowed, because Pine shadowing fails silently rather than loudly:

| Source identifier | Collision in `orb.pine` | Resolution |
|---|---|---|
| `atr` | global `atr = cachedATR` | reuse `cachedATR` |
| `vwapVal` | `isTrendUp` / `isTrendDown` parameters | new `sessionVWAP` global |
| `volMA` | `hasVolumeConfirmation` parameter | reuse `volumeMA` |
| `val` | local `string val` in `renderFilterStatus` | `klVAL` |
| `entry` | 110 occurrences | `klEntryRef` |
| `riskPct` | existing input | reuse |
| `capital` | — | reuse `accountSize` |

`sessionVWAP` is separate from `trendVWAP` on purpose: `trendVWAP` is only assigned when
`enableTrendFilter` is on **and** `trendMode` contains "VWAP", so scoring off it would
silently drop the VWAP component based on an unrelated filter setting.

### Pine-specific notes

- `klEMA9` is computed unconditionally. A `ta.*` call reached on only some bars loses its
  internal series state, so gating an EMA behind an input is a correctness bug, not a saving.
- Signal labels use `if`/`else`, not a ternary: Pine's conditional operator can evaluate
  both branches, and `label.new` has a side effect — a ternary draws two labels per signal.
- `PDH`/`PDL` use `lookahead_on` **with** a `[1]` offset. That combination is the canonical
  non-repainting idiom: `[1]` guarantees a closed daily bar, `lookahead_on` makes it
  available from the session's first bar. The NR filter deliberately uses `lookahead_off`
  because it compares a *series* of daily ranges, where an extra day of lag is harmless.
- Level drawings are cleared and rebuilt on `barstate.islast` only, so line/label usage
  stays flat and the existing 500/500/300 resource caps still hold.
- `klPendingSource` is cleared on entry consumption, on pending-entry cancellation, and on
  session reset. Without the cancellation clear, a cancelled key-level pending would leave
  its trigger tag behind for a later ORB entry to inherit and mislabel.

### Static checks

```bash
cd signal_engine/pinescripts/intraday/orb

# Only the 8 intended lines differ from orb.pine
diff <(sed 's/\r$//' orb.pine) <(sed 's/\r$//' breakout.pine) | grep -c '^<'   # -> 8

# No duplicate top-level typed declarations
grep -oE '^(var +)?(float|int|bool|string|color|line|label|box|table) +[A-Za-z_][A-Za-z0-9_]*' breakout.pine \
  | awk '{print $NF}' | sort | uniq -d                                        # -> empty

grep -c 'request\.' breakout.pine        # -> 13, well under Pine's 40 cap
grep -c 'strategy.entry(' breakout.pine  # -> 2, unchanged
grep -cP '\t' breakout.pine              # -> 0
git status --porcelain orb.pine          # -> empty
```

### Not yet verified

Pine has no local compiler or unit framework, so the following still need a TradingView
session and are **not** claimed as done:

1. Compiles clean with no `max_bars_back` or resource warnings.
2. **Regression gate**: with `enableKeyLevels` off, the Strategy Tester report matches
   `orb.pine` exactly (net profit, trade count, win rate).
3. Auto VAH/POC/VAL match TradingView's native Session Volume Profile on 3+ symbols.
   Tune `klTicksPerRow` until they do. This is the known-risky one — POC accuracy already
   drifted once during the volume-profile work.
4. Key-level alerts fire, arrive in Telegram, and place **no** orders while
   `enableKeyLevelExecution` is off.
5. Dashboard fits inside 48 rows in its widest configuration.

### Not in scope

- No `signal_engine/` Python changes. The alert carries a `Trigger:` line and a
  `MODE: OBSERVE ONLY` marker, but nothing consumes them yet — that follows once
  `enableKeyLevelExecution` has been validated.
- `orb.pine` stays frozen as the live strategy.

---

## 2026-08-22 — Chart review: palette, label collisions, dashboard metrics

First rendered chart (`charts/TCS_2026-08-21_00-14-03_38896.png`) compiled and ran
correctly. Three presentation problems and one config mismatch came out of reviewing it.

### Colour palette standardised

The old defaults collided with the key-level palette on a dark background — ORB15 was
green (same as VAH), ORB5 yellow (same as IB), ORB60 purple (same as POC), and bullish
FVG green again.

| Element | Was | Now |
|---|---|---|
| ORB 5 / 15 / 30 / 60 | yellow / green / blue / purple | `#FFB74D` `#FF9800` `#F57C00` `#E65100` — one orange family, shaded so multi-stage users stay oriented |
| IB high/low | yellow | `#FFEB3B` |
| VAH | light green | `#90EE90` |
| POC | `color.purple` | `#BA68C8` — brighter; the stock purple reads almost black on a dark chart |
| VAL | `color.red` | `#EF5350` |
| PDH / PDL | gray | `#9E9E9E` — deliberately neutral, they are context not triggers |
| Bullish FVG | green | `#2E7D32` dark green |
| Bearish FVG | orange | `#C62828` red |

VAL red and bearish-FVG red are close by design (as requested). They stay distinguishable
because the forms differ: VAL is a thin dotted line, FVG is a 90%-transparent box.

### Label collisions fixed

Two distinct problems were visible on the chart:

1. **Key-level tags printed through ORB edge labels.** Both sat at `bar_index + 2`, so
   "IBH 2329.0" overprinted the "ORB15" badge whenever IB high equalled ORB high — which
   happens often, since the IB window (09:15-10:15) contains the ORB window (09:15-09:30).
   Key-level tags now start at `KL_TAG_OFFSET_BARS` (6), clear of `LABEL_OFFSET_BARS` (2).

2. **Key levels at similar prices overlapped each other.** `klDrawLevel` now keeps a
   `taken` array of every tag price placed this pass; a tag landing within a collision
   band (0.35 × ATR, so it scales with the symbol and the zoom) is pushed right by
   `KL_TAG_STEP_BARS` (11), repeatedly, until it finds clear air. Each level's line is
   extended to meet its own tag, so labels never float detached. POC is drawn first so it
   always wins the innermost slot — it is the level that matters most.

3. **Signal text stacked.** Around 11:00 three setups fired within a few bars (PDH-RT,
   VAH-ACC, PDH-RT) and their labels printed on top of each other. The arrow still prints
   for every signal; the TEXT is now throttled to one per `KL_LABEL_MIN_GAP` (6) bars.
   Vertical offset also widened from 0.7 to 0.9 ATR.

### Dashboard — three new rows

| Row | Shows | Why it earns its place |
|---|---|---|
| `IB H/L` | Initial Balance high / low | The IB levels were being drawn but their values were not readable anywhere |
| `ADR / used` | 14-day average daily range + % of it already travelled today | The best "is there room left?" read available. A breakout arriving after the stock has done its whole ADR is a materially worse trade than the same signal at 40% |
| `ATR / SL` | ATR(14) + the stop distance it implies at the configured ATR multiplier | Sanity-check for level-based stops: much tighter than this and the stop sits inside the noise; much wider and size has to come down |

`ADR / used` is colour-graded: green <50%, normal 50-80%, orange 80-100%, red >100%.

**ADR is `sma(high-low, 14)`, not the daily ATR.** ATR is *true* range, so it folds
overnight gaps into the number; for "how much intraday travel is left" the trader wants
high-low. Both are now carried — ATR sizes the stop, ADR sizes the remaining opportunity.
It rides in the existing daily `request.security` tuple, so no extra request call.

Session high/low needed for the ADR-consumed figure is tracked inside `klInitialBalance`
rather than via another security call. Dashboard table grown 48 → 54 rows.

### Input review

Checked every input against the documented strategy (`config.yaml`, `PRD.md`,
`SIGNAL-PERFORMANCE-2026-Q1.md`).

**Changed:** `riskPct` 2.0 → **1.0**, to match `config.yaml: risk_per_trade: 0.01`. The
panel was showing position sizes at double the real risk budget. Display-only — the
backtest sizes off `percent_of_equity`, so no Strategy Tester change.

**Correct as-is, verified against config:** entry cutoff 11:00, min entry time 09:45,
time exit 15:00, ORB15-only, `enableRetestEntry=false` (matches the MARKET-order finding),
volume 1.2×/1.8×, trend mode VWAP, gap filter 2.5%, ORB range filter 0.4-3.5%.

**`tp1ExitQtyPct = 50` is correct — do not "fix" it.** It initially looks like a bug,
because the backtest exits 100% at TP1 (`strategy.exit(limit=tp1)`) while the live alert
tells the engine to exit 50%. It is deliberate: the tooltip records "72.3% of TP1 trades
also reach TP1.5", `config.yaml` carries `tp1_runner_sl_buffer: 0.3` for the runner, and
`strategy_profiles.ORB` intentionally omits `tp_levels` so the script owns the fractions.
The consequence to be aware of: **Strategy Tester results understate the live strategy**,
because the backtest does not model the 50% runner to TP1.5.

**Flagged, not changed** (these move the edge and need backtest evidence first):

- `breakoutBuffer = 0.5%` is doing a lot of work. On TCS at 2329 that is **11.6 points**
  of required follow-through on a 38.4-point ORB — the breakout must clear the level by
  30% of the range. The input's own tooltip recommends 0.1%. This is the likeliest reason
  the reviewed session showed "Cross: ❌ None / price stayed within ORB range". Worth an
  A/B at 0.1-0.2%.
- `accountSize = 10000` is still the template default. With `risk_per_trade` at 1% every
  size and `Ref Qty` on the panel is computed off ₹10,000.
- `stopMode` defaults to `"ATR"`, but `HOW-IT-WORKS.md` documents the live stop as
  "Smart Adaptive". One of the two is stale.
- `klScoreThreshold = 7` let a VAH-ACC (plain acceptance — no retest, no rejection, the
  weakest family) fire at exactly 7/7. Consider 8 to require either confluence or retest
  structure before an alert.

---

## 2026-08-22 — Token-limit fix + confluence scoring bug

TradingView: *"Compiled code contains too many tokens: 100645. The limit is 100256."*
389 tokens over, i.e. a 0.39% cut needed.

### Comments are NOT the lever

Worth stating plainly, because it looks like an obvious place to save: **comments do not
count toward the compiled-token limit.** They are stripped by the lexer before tokens are
counted, so deleting them saves nothing. The verbose blocks in this file were condensed to
one-liners anyway (readability, and it was asked for), but every real saving below came
from code.

The same caution applies to the long `tooltip=` strings — a string literal is a *single*
token however long it is, so shortening tooltip prose is also not a lever.

### Where the tokens actually went

| Change | Effect |
|---|---|
| `klConfluence`: nine near-identical `if` blocks → one loop over parallel level/name arrays | biggest single win |
| `klBuildRegistry` now emits a parallel `string[]` of level names to feed that loop | small cost, enables the above |
| `klResolveSetup`: each of 14 branches set 6 variables; now sets 3 | `isRetest`/`rejectionFamily` derived once from the code suffix, `setupName` dropped entirely |
| `klSetupPhrase()` expands the code to readable text once, in the alert | replaces 14 long name literals |
| Dashboard: 13 rows → 10 | Open-pos merged into Auction, IB% into IB H/L, Score into Setup |
| Removed `klRvolMin` / `klRvolStrong` inputs | reuse `volumeMultiplier` / `strongVolumeMultiplier` from the VOLUME FILTER group — one definition of "strong volume" for the whole script instead of two |
| `buildKeyLevelAlert`: four chained `str.replace_all` → one `str.substring`; separator hoisted to a local | |

Estimated saving ~1,300 compiled tokens against the 389 required, so there should be real
headroom now.

### Context worth keeping in mind

A rough tokenizer puts the key-level layer at **~21% of the file's tokens**, which implies
`orb.pine` alone already consumes roughly **80,000 of the 100,256 limit**. The budget for
anything added to this script is about 20k tokens, and the key-level layer very nearly
spent all of it. Any future feature of comparable size will hit this wall again — at that
point the answer is splitting the key-level engine into a separate indicator rather than
shaving further.

### Bug found while refactoring: confluence counted the level against itself

`klConfluence` tested `math.abs(px - vah) <= band` for every level including the one that
triggered the setup. For a VAH setup that is `|vah - vah| = 0`, which always passes — so
**every VA, PD and IB setup scored a phantom +1**, and the rendered chart's
`Confluence: 1 VAH` on a `VAH-ACC` setup was the tell.

Worse, the phantom point could tip the `confCnt >= 2` multi-level bonus (+2 more) when only
one genuine level was nearby. Affected setups were scoring up to **3 points too high**.

Fixed by the loop rewrite: a level is skipped when its distance is under half a tick.

**This changes scoring behaviour.** Scores now read 1-3 points lower than before, so
`klScoreThreshold = 7` is effectively stricter than it was. Watch the alert rate for a
session or two before deciding whether 7 is still the right threshold — the previously
observed "VAH-ACC at 7/7" would now score 6 and not fire.

---

## 2026-08-22b — Chart legibility pass

### Price-scale badges removed

The `intraday-orb:ORB15 High / Mid / Low` labels obstructing the price panel came from
`display=display.all` on the twelve ORB `plot()` calls — TradingView renders a plot's last
value in the price scale. All twelve switched to `display=display.pane`: the line still
draws, the badge does not. The price information is not lost, it moves on-chart (below).

### Every level now carries its price on-chart

`klDrawLevels` tags VAH, POC, VAL, IB-H, **IB-M**, IB-L, PDH, PDL and now **ORB-H, ORB-M,
ORB-L** as well.

- ORB tags are drawn **tag-only** (`drawLine=false`) — the ORB lines already come from the
  `plot()` series, so drawing them again would double up.
- ORB is tagged **first**, so it claims the innermost anti-collision slots. It is the
  strategy's primary reference and should never be the one pushed out to the right.
- IB mid is derived inside the draw function from `(ibh + ibl) / 2` rather than carried
  through `klInitialBalance`'s return tuple — it is display-only.
- ORB tags take their colour from `orb.orbColor`, so they track whichever stage is active.

`klShowTags` now gates **only** the tags. In the first cut of this change it gated the
whole draw block, which would have hidden every key-level line when a user turned tags off
— a toggle labelled "Show level price tags" must not delete the levels.

### Dashboard

- Title `ORB DASHBOARD` → **`DASHBOARD`**, and the header tooltips likewise, since the
  script is no longer ORB-only.
- Text one step smaller across the board: Small→tiny, Normal→small, Large→normal, Auto→tiny.
  The panel carries ~30 rows now and the old sizing took over half the chart height.

### Breakout markers: triangle + short code + hover tooltip

`plotshape()` was replaced with label-based triangles. **`plotshape` cannot do what was
asked**: it takes no `tooltip`, and its `text` argument must be a compile-time constant, so
it could never name the setup that actually fired.

Each signal now draws:
- a `label.style_triangleup` / `triangledown` marker whose `tooltip` carries the full read —
  direction, setup phrase, score/threshold, level, confluence names, Ref SL, T1, RVOL, CLV,
  delta proxy;
- a very short code beside it (`VAH-RT`, `PDH-BRK`, …) at `size.tiny`.

The triangle prints on every signal; the text stays throttled to one per
`KL_LABEL_MIN_GAP` bars. The text label deliberately carries **no** tooltip of its own —
the triangle is the single hover target, as requested.

Side benefit: dropping the two `plotshape` calls returned two statements to the global
scope, which the main-body budget needed.

---

## 2026-08-22c — Ported upstream improvements (Luxy v5 latest)

Compared `orb-luxy-big-beautiful-dynamic-orb.pine` against **`orb.pine`** (our unmodified
ancestor) rather than against `breakout.pine`, so upstream's changes were isolated from ours.

### Ported

**GOD MODE** — upstream's breakout-quality layer. Rebuilt on state this file already has
(`cachedVolumeMA`, `cachedHTFBullish/Bearish`, `klATRDaily`, `todayOpen`, `prevDayClose`,
`sessionBreakouts*`, `klPDH/klPDL`) instead of re-deriving it, so it added **no new
`request.security` call** where upstream added one.

| Piece | What it does |
|---|---|
| **Adaptive buffer** | Effective buffer = `max(breakoutBuffer%, 10% of ATR)`. A **floor**, never a reduction |
| **Chop-Day Guard** | 2+ failed breaks in a session caps the quality score at 40 and warns on the dashboard |
| **Quality Score 0-100 / A+..D** | Volume, HTF alignment, ORB width vs daily ATR, gap direction, PDH/PDL confluence, prior-break penalty |
| Display | Score appended to breakout labels (`⚡ 87 (A+) 🏆`), plus `ORB Quality` and `Chop Day` dashboard rows |

The adaptive buffer directly addresses the `breakoutBuffer = 0.5%` concern raised earlier:
it is what makes a **low** fixed buffer safe. Set 0.1-0.2% and let ATR widen it per ticker,
instead of carrying one conservative number across every symbol.

Note it is a floor only — on TCS (ATR ≈ 12, price ≈ 2300) the ATR component is ~0.05%, so
with `breakoutBuffer` still at 0.5% the adaptive path changes nothing. **It only starts
working once the fixed buffer is lowered.**

**Both-pending-entries-due fix** — upstream merged the separate long/short pending blocks
into one with `entryIsLong = pendingLongDue and not pendingShortDue`, commented "both due →
short wins". In our file the two blocks are still separate and ~250 lines each, so rather
than restructure them the same resolution was applied as a guard on the long block. Without
it, if both pendings came due on one bar the long block would fire `strategy.entry("Long")`
and set up its lines, then the short block would overwrite the shared `orb*` state — leaving
strategy position and on-chart trade lines describing different trades.

### Not ported (deliberately)

| Upstream feature | Why not |
|---|---|
| `STAGE & COLOR MANAGEMENT` stage-complete alerts | Indicator-style `sendAlert()`; our alert model is Telegram JSON consumed by signal_engine, and it is proven |
| `INDIVIDUAL ALERTS` / `TRADE MANAGEMENT ALERTS` | Same reason — upstream is an `indicator`, we are a `strategy` with a different, working alert contract |
| Premarket high/low tracking (`godPmHigh/Low`) | Upstream feeds it from `not inSession` bars. NSE cash has no usable extended-hours data, so it would be `na` all day. PDH/PDL confluence, which we do have, carries that part of the score |

### Bug found while porting

My own heredoc insertions had written **54 lines with bare LF** into a CRLF file. Pine
tolerates it, but it made line-based tooling misread the file — which is how it surfaced.
Whole file normalized to CRLF, and the check is now part of the validation sweep.

Also caught before it shipped: `godGrade` was being called from `renderGodRow` (line ~1451)
but defined at ~2379. Pine resolves identifiers in source order, so that would not compile.
`godGrade` is dependency-free, so it was hoisted above `renderGodRow`. `godScore` could not
move with it — it depends on `klATRDaily`, which is not defined until ~2023.

### ⚠ Token budget

GOD MODE costs roughly **+665 proxy tokens (~2,000 compiled)**. This build was already
within a few hundred tokens of the 100,256 ceiling, so **it may exceed the limit again.**

If it does, drop in this order — the cheap items carry most of the value:

1. **`renderGodRow`** (the two dashboard rows, ~60 proxy tokens). The score still prints on
   the breakout label.
2. **The quality score itself** (`godScore` / `godGrade` / label append, ~250 proxy). Keep
   the adaptive buffer and chop guard — together they are ~15 tokens and are the two pieces
   that actually change trade selection.
3. The `tooltip=` strings across the file total ~41,000 characters. Whether that counts
   toward the limit depends on how TradingView tokenises string literals — unknown from
   outside — but it is the largest single reservoir if it does.

The adaptive buffer, chop guard and the pending-entry fix are all near-free and should be
the last things removed.

---

## 2026-08-22d — Token diet: single-stage ORB, NSE-only session

`Compiled code contains too many tokens: 102402. The limit is 100256` — 2,146 over.

That error gave the first hard calibration: the net source change since the 100,645 build
was **+405 proxy tokens → +1,757 compiled**, i.e. **~4.34 compiled tokens per proxy token**.
Target was therefore ~494 proxy tokens.

### Removed — ORB stages 5 / 30 / 60 (-931 proxy, ~4,040 compiled)

Only ORB15 is traded, and 5/30/60 were already disabled by default. Removed their inputs,
colours, `ORBData` objects, `plotORB*` series, and their `plot()`/`fill()` pairs.

Safe because every consumer iterates `allORBs` — the array size drives all the loops, so
`array.from(orb15Obj)` reconfigures the engine without touching its logic. `cachedNextORBs`
resized 4 → 1 to match.

`orb15Obj` keeps `isFirstORB = false` in `getORBPlotValues`. With 5/30/60 disabled,
`getPreviousEnabledORB(orb15Obj)` already returned `na`, so leaving the flag alone
reproduces exactly what was running before rather than silently changing the building-phase
plot window.

### Removed — non-NSE session handling (-244 proxy, ~1,060 compiled)

The Auto-Detect asset-type tree resolved to `tradingSession := ""` on **every** Indian-equity
path, so 25 lines of futures/crypto/forex/ETF branching collapsed to one assignment. Also
removed `enableExtendedHours`, `extendedPreMarket`, `extendedAfterHours`,
`futuresTradingHours`, and forced `is24_7Market` to `false`.

The three sites that consumed `enableExtendedHours` were resolved rather than stubbed: the
regular-hours branch keeps only the path NSE actually took, and two now-unreachable
pre-market dashboard branches were dropped.

Custom session mode was **kept** — it is 2 inputs and preserves the ability to override hours.

### Total: -1,175 proxy ≈ -5,100 compiled, against 2,146 required

### What the upstream port was actually worth

Asked directly, and the honest answer is that most of GOD MODE is decoration:

| Ported | Changes which trades are taken? | Cost |
|---|---|---|
| **Both-pending-due fix** | **Yes — correctness.** Prevents strategy state and trade lines describing different trades | ~10 proxy |
| **Adaptive buffer** | **Yes — but dormant.** It is a floor; at `breakoutBuffer = 0.5%` the ATR term is ~0.05% on TCS, so it does nothing until the fixed buffer is lowered to 0.1-0.2% | ~15 proxy |
| Chop-Day Guard | No. Caps the displayed score and prints a warning. Does not block entries | ~20 proxy |
| Quality Score + grade + dashboard rows | No. Pure display | ~600 proxy |

Verified by inspection: `godLastScore` and `godChopDay` appear only in label text and
`table.cell` calls — neither is referenced in any breakout or entry condition.

So **~90% of GOD MODE's token cost buys zero change in trade selection.** It is a
discretionary read, not a filter. If the budget is ever tight again, the score and its
dashboard rows are the obvious first cut; the pending-entry fix and adaptive buffer are
near-free and should be the last things removed.

The score would only start earning its cost if it were wired into `canTakeEntry` as a
minimum-grade gate — which is a strategy change to validate on the Strategy Tester first,
not something to switch on quietly.

---

## 2026-08-22e — Chart review 2

Chart: `charts/TCS_2026-08-22_13-10-08_b1cdd.png`. Colours, anti-collision tags and the
tooltip triangles all render as intended.

### Bug: dashboard title printed twice

The dashboard has three sibling branches:

```
if   not showFullDashboard and (dashDataChanged or forceUpdate)   -> clears, compact status
else if showFullDashboard                                          -> clears, full panel
else if not na(dashTable)                                          -> NO CLEAR, header + message
```

The third branch never cleared the table. It runs on bars where the full panel did **not**
redraw, so its two closed-market rows printed on top of the previous full panel and its
header landed under the stale one — two `DASHBOARD` rows. Added the `table.clear` the other
two branches already had.

### Font

Reverted the previous over-shrink one step: Small→small, Normal→normal, Large→large,
Auto→small (was tiny/small/normal/tiny).

### Redundant `ORB15` badge

`showEdgeLabels` now defaults **off**. The orange `ORB15` badge sat directly beside
`ORB-H 2292.0` / `ORB-L 2266.1` and carried strictly less information. Kept as an input
rather than deleted, so it can be switched back on.

### Added: level tags fade out with distance

`klTagMaxATR` (default 6.0) — only levels within N ATRs of price get a price tag. The
**line is always drawn**; only the tag is suppressed. With ORB, VA, IB and PD all plotted
there are up to nine tags competing for the right edge, and levels far from price are
context rather than decisions. `0` disables the filter.

### Remaining visual observations (not changed — your call)

1. **ORB fill and the value-area box overlap into a muddy olive** on the right of the
   chart. Both are wide translucent regions covering the same price band. Options: raise
   the VA box transparency past 94, or drop the VA box entirely — VAH and VAL already
   bound that zone with lines, so the shading is arguably redundant.
2. **The panel sits over price and the volume pane** at Bottom Left. Middle-left is empty
   on most sessions; `dashPos` already exists as an input.
3. **`ORB Quality` reads `-`** in this capture because no ORB breakout registered today —
   the volume filter rejected the cross, so `sessionBreakoutsUp` stayed 0 and no score was
   ever computed. Expected, not a fault. Should populate on the next clean break; worth
   confirming on a session that actually breaks out.
4. **PDH tag collides with TradingView's own `High` price marker** when the previous day's
   high equals the visible high. Nothing in the script can move the native marker; the
   distance filter will usually hide the PDH tag anyway once price moves away.

---

## 2026-08-22f — Signal-quality batch: CLV double-count, T1 placement, HTF confirmation

Three changes to key-level signal generation, chosen against outside evidence rather than
the legacy ORB trade log (which measures a different strategy: ORB-only, 5-min, no key levels).

### Bug: CLV was scored twice

`klCandle()` returns both `clv >= klClvLongMin` **and** `deltaProxy`, which is itself derived
from the same number (`clv > 0.55 ? "buy"`). `klComputeScore()` awarded **+1 for each**, so a
single strong close collected **2 of the 7-point threshold** for one piece of information —
and the weakest piece in the model, since Pine has no orderflow data and CLV is only a proxy
for it. Removed the `deltaProxy` component; `deltaProxy` is still carried in the alert text
and the signal tooltip, where it is a read rather than a weight.

Max score 15 -> 14. Combined with the earlier confluence fix, scores now read up to 4 points
lower than the build that set `klScoreThreshold = 7`. **Watch the alert rate before
re-tuning the threshold** — 7 is now a materially stricter gate than when it was chosen.

### T1 is placed short of the level, not on it

`klFindT1()` returned the nearest structural level exactly. That is where every other
trader's resting limit orders sit, so price routinely stalls a few ticks short and rolls
over. T1 now sits `klT1PadMult` ATRs **in front of** the level (default 0.15).

If the pad would leave T1 inside the noise band around entry, T1 reports `-` rather than a
target that cannot be worked — that setup has no room to a structural target, which is
itself information.

Symmetry worth stating: stops go **beyond** the crowd, targets go **in front of** it. Both
follow from the same fact about where liquidity rests.

### HTF close confirmation (`klRequireHTFClose`, default ON, `klConfirmTF` = 15)

A key-level setup now needs a **closed** higher-timeframe bar on the correct side of the
level. The test follows trade direction, so one expression serves every family:

| Setup | Requirement |
|---|---|
| long (`-RT`, `-BRK`, `-ACC`, `VAL-REJ`) | HTF close **above** the level |
| short (`-RT`, `-BRK`, `-ACC`, `VAH-REJ`) | HTF close **below** the level |

Rejections are not inverted by this — a `VAH-REJ` short wants the HTF close back *under*
VAH, which is exactly the rejection thesis.

`request.security(..., close[1], lookahead_off)` is the non-repainting pair. Requesting
plain `close` would return current price between HTF boundaries, making the test
meaningless rather than strict. **Cost: up to one HTF bar of entry lag.** Setups printing
RVOL at or above `strongVolumeMultiplier` bypass the wait — volume that size is order flow
arriving, not a stop run.

Dashboard `Setup:` row now shows `⏳HTF` in orange when a setup clears the score threshold
but is still waiting on confirmation, so the wait is visible rather than silent.

**Known limitation:** if `klConfirmTF` is set at or below the chart timeframe the gate
degrades toward permissive (fails open, reverting to previous behaviour) rather than
blocking. No runtime guard was added — it would cost tokens for a misconfiguration the
tooltip already warns about.

### Evidence base

- Zarattini, Barbon & Aziz (Swiss Finance Institute, 2024) — across 7,000+ US stocks
  2016-2023, plain ORB was weak; **selecting by opening relative volume did almost all the
  work.** Their measure is first-5-min volume / mean first-5-min volume over 14 prior days —
  time-of-day normalised, which the current `volume / sma(volume, 50)` is not. This is the
  case for the Volume Factor work, not yet built.
- Breakout-confirmation literature is consistent that lower-timeframe breaks carry a higher
  false-break rate, and that a **close** beyond the level (not a wick) is the discriminator.
- Liquidity-sweep anatomy: wick through the level, close back inside, with the break bar
  under ~1.5-2x average volume. Sweeps cluster at equal highs/lows.
- SMC order blocks backtest at roughly 50-55% raw win rate with heavy discretion and real
  alpha-decay exposure — consistent with the decision not to port them. The one SMC
  construct with support is sweep + close-back-inside + LTF confirmation.

### Token cost

+201 proxy ≈ **+872 compiled**. Estimated headroom before this batch was ~2,950 compiled,
so roughly 2,080 should remain. Not verified against TradingView.

### Not verified

Pine has no local compiler. Still needs a TradingView session:

1. Compiles clean.
2. Regression gate: `enableKeyLevels = false` -> Strategy Tester still matches `orb.pine`.
   (Should hold — every change is inside the key-level path.)
3. `⏳HTF` actually appears and clears on a live break.
4. Alert rate after the scoring change is still workable at threshold 7.

---

## 2026-08-22g — Volume Factor replaces the rolling MA; GOD MODE score removed to pay for it

### The old denominator was the weak link

`volumeMA = ta.sma(volume, 50)` was **time-of-day blind**. On a 5-min chart a 50-bar average
spans ~4 hours, so at 09:20 the denominator was mostly the previous session's dead close bars
while the numerator was an opening bar running 5-10x normal. The 1.2x test was therefore
**near-inert in exactly the window this strategy trades most**, and over-strict around lunch.

Two artefacts in the code were compensating for it rather than fixing it:
- `max(volume, volume[1], volume[2])` in `hasVolumeConfirmation()`
- the earlier 20 -> 50 MA change, recorded in `SIGNAL-PERFORMANCE-2026-Q1.md:183`

Both are now gone.

### What replaced it

`klVolFactor()` compares each bar against **the same slot of the session on previous days**.
One pass over the history matrix yields two different reads:

| Read | Question it answers | Used by |
|---|---|---|
| `base` — mean bar volume at this slot | Does THIS break carry participation? | `volBaseline`, so every existing `volume / volBaseline` ratio is now time-of-day normalised |
| `vf` — cumulative session volume vs the same point of day | Is this STOCK in play today? | New `Vol Factor` dashboard row, +1 score component, alert line |

At slot 0 the `vf` read **is** the Zarattini/Barbon/Aziz relative-volume measure — first-5-min
volume over the mean first-5-min volume of N prior days. That study (SFI 2024, 7,000+ US
stocks, 2016-2023) found plain ORB was weak and **selection by opening relative volume did
almost all the work**, which is why this moved ahead of everything else on the list.

Bands on the dashboard row are the BreakingTrade cheat sheet's, including its rule that
**>=3.0x is a WARNING** (block deal / news), not a green light.

### Implementation notes

- **Stateful, so called unconditionally** — the file's existing rule. Its accumulator advances
  on **confirmed bars only**; the forming bar is added for the live read without mutating
  state, so a realtime bar cannot freeze a partial volume into history.
- **Matrix allocated at input maxima** (30 x 400) rather than at the configured size. Pine can
  reject a series/simple int as a matrix dimension and 12,000 floats is cheap — not worth the
  compile risk to save the allocation.
- **Local `row` renamed `vRow`** — this file renames rather than shadows, because Pine
  shadowing fails silently.
- **Minimum 3 prior sessions** before the reading is trusted. With one prior session the
  denominator is just yesterday, so a single heavy day would suppress the factor all through
  the next. Below the minimum it reports `na` and the volume filter **fails open**.
- `volSlotCount` derives from the chart timeframe (390 min / TF + 2), so 1/5/15-min all work.

### Removed

| Removed | Why safe |
|---|---|
| `volumeMaLength` input + its validation | No remaining consumer |
| `ta.sma(volume, ...)` cache | Replaced by the per-slot baseline |
| 3-bar `max()` in `hasVolumeConfirmation` and at 2 label sites | Let a spike two bars BEFORE a weak breaking candle satisfy the filter. Research is consistent that what matters is expansion AT the level, in the candle doing the breaking — volume arriving after is the crowd |
| **GOD MODE quality score** — `godScore`, `godGrade`, `godNear`, `godConfUp/Dn`, `godLastScore/Up`, label appends, `ORB Quality` row | Referenced **only** in label text and table cells, never in any breakout or entry condition. Verified by grep before removal |

`volumeMA` -> **`volBaseline`**, `cachedVolumeMA` -> `cachedVolBaseline`. Renamed rather than
silently redefined: it is no longer a moving average, and leaving a misleading name in trading
code is how the next bug gets made.

**Kept from GOD MODE**: adaptive buffer, chop-day guard, both-pending-due fix — the three
pieces that actually affect trade selection, all near-free. The chop row survives; only the
score it used to sit beside is gone.

### ⚠ Two consequences to watch

**1. The regression gate against `orb.pine` no longer holds — deliberately.** `volBaseline`
feeds `volumeOK` in ORB breakout detection, so ORB trade selection changes. This was the
explicit instruction (replace, not opt-in). The Strategy Tester will differ from `orb.pine`
and that is now expected, not a fault.

**2. `volumeMultiplier` / `strongVolumeMultiplier` are no longer calibrated.** 1.2 / 1.8 were
tuned against an inflated-at-open denominator. Properly normalised, morning ratios read
**lower** — so the filter is now materially **stricter in the morning**, where most trades
happen. Expect fewer signals at first. The cheat sheet's own bands (1.2-1.5 elevated,
1.5-3.0 strong) suggest 1.2/1.8 remain sensible, but **observe before re-tuning**, and change
one at a time.

Score max returns to 15 (the new session-VF component offsets the removed delta proxy), so
`klScoreThreshold = 7` sits roughly where 2026-08-22f left it.

### Token accounting

| | proxy | ~compiled |
|---|---:|---:|
| 22f batch (CLV / T1 / HTF) | +201 | +872 |
| Volume factor engine | +485 | +2,105 |
| GOD MODE score removal | -671 | -2,912 |
| **Net for the session** | **+15** | **+65** |

Headroom should be roughly where it started (~2,885 compiled). Not verified against TradingView.

### Not verified

1. Compiles clean — `matrix.new<float>` dimensions and the `var` matrix inside a function are
   the two spots most likely to be rejected.
2. Volume Factor reads sanely: ~1.0x on an ordinary day, and slot 0 should roughly reproduce a
   manual first-bar-volume / 14-day-average calculation.
3. Signal count after the recalibration is still workable.
4. Chop row still renders now that the score above it is gone.

---

## 2026-08-22h — Chart review 3 (TCS 16:30): two fixes

Chart `charts/TCS_2026-08-22_16-30-07_78c48.png`. **The build compiles and runs** — the
`Vol Factor` row renders, `ORB Quality` is gone, and three key-level signals fired across the
three visible sessions (VAL-RT Aug 19, PDH-RT Aug 20, IBH-RT Aug 21). That clears the compile,
matrix-allocation and dashboard items from 22g's unverified list.

### Bug: `"#.00"` drops the leading zero — a 100x misread

`Vol Factor` printed **`70x thin`**. The value was **0.70**; Pine's `#` means "digit, omitted
if zero", so `str.tostring(0.70, "#.00")` yields `.70`. On a volume gauge that reads as
**seventy times** normal when it is seven tenths — the opposite conclusion. The band label
(`thin`) was the tell that the number was misrendering.

Fixed to `"0.00"` here and on CLV/RVOL, which had the same exposure. `"#.#"` elsewhere in the
file renders `0.4x` correctly, so those sites were left alone.

### Setup / Confluence rows were blank almost always

Both showed only the CURRENT bar, and a setup exists for exactly one bar — so the rows read
`-` on ~99% of bars, which is indistinguishable from "this engine produces nothing". The chart
made the cost obvious: **IB-H 2292.0 and ORB-H 2292.0 were identical** — two levels stacked,
the strongest tell in the model — and `Confluence:` showed `-`.

Now latched: the last fired setup persists, dimmed and stamped `HH:mm`. Bright = firing on
this bar, dim = history, so the two can never be confused.

### Found, not fixed: ORB width filter gates key-level entries

`smartFiltersPass = gapFilterPassed and orbRangeFilterPassed and isAfterMinTime`, and
`canTakeEntry` consumes it. So an **IB or VA setup can be rejected because the Opening Range
was too narrow or too wide** — a quantity with no bearing on whether an IB extension is valid.

Latent while `enableKeyLevelExecution = false`, so nothing is being lost today. **Must be
resolved before execution is switched on.** Gap, volume, trend, HTF and index are all
genuinely universal; ORB width and ORB cross are the two that are ORB-only.

### Still open from the chart

- **POC 2291.25 sits 1.0 point above VAL 2290.25.** Possible with a bottom-heavy distribution,
  but POC accuracy already drifted ~26 points once on TCS. Verify against TradingView's native
  Session Volume Profile before trusting VA-family setups.
- ORB fill and the value-area box still overlap into muddy olive on the right of the chart.
- Volume filter blocked the session at 0.4x against a 1.2x threshold, with session VF 0.70x.
  This is the 22g recalibration behaving as predicted, on a genuinely quiet day — not evidence
  of a fault, but the signal-count question stays open until an active day is observed.

---

## 2026-08-22i — Input diet (141 -> 59), verdict-first dashboard, readable signal tooltip

Reframed around one fact: **signals are consumed by webhook -> signal_engine -> trade bridge.**
Nobody is reading this panel to decide whether to click buy. The panel's job is to answer
"what is the system doing, and why" — which is a completely different design brief.

### Inputs: 141 -> 59

| Action | Count | What |
|---|---:|---|
| **Deleted — dead code** | 13 | FVG subsystem (7), pullback filter (3), currency conversion (3) |
| **Frozen to constants** | 67 | Values that should never be touched mid-strategy: the 8 chart colours, label/TP display toggles, the legacy retest/cycle machinery, position-sizing config (signal_engine owns sizing), ADX/adaptive-RR, HTF/index/trend sub-parameters, NR internals, VP resolution, CLV thresholds, session mode |
| **Added** | 1 | `dashMode` (Focus / Full) |

**FVG was entirely decorative** — `hasValidFVGNearLevel()` was defined but never called, so
`enableFVGFilter` did nothing. It drew boxes and affected no signal. Removed with its arrays,
four functions and drawing block.

**Currency conversion was dead weight** on an NSE-only script — 3 inputs, ~35 lines, and a
whole `request.security` slot for an FX rate. `request.*` is now **13**.

Freezing rather than deleting is deliberate: the value and every consumer stay identical, so
behaviour is provably unchanged and anything can be re-exposed by putting `input.x(` back.

### Dashboard: verdict first

New `renderVerdict()` prints **one line plus one reason**, ahead of everything else:

| Verdict | Meaning |
|---|---|
| `⛔ NO TRADE` | A filter blocks. The reason line names **which**, first-failure-wins |
| `⏰ TOO EARLY` / `🏁 CUTOFF` / `🏁 DONE` | Outside the window, or the session slot is spent |
| `🟡 ARMED` | Filters pass, waiting for a setup (flags chop day / thin volume) |
| `⏳ WAITING` | Setup scored, HTF close has not confirmed |
| `🟢/🔴 SIGNAL` | Alert fires this bar |
| `✅ IN TRADE` | Position open |

`dashMode = "Focus"` (default) shows the verdict plus Vol Factor, Auction, IB, ADR, Setup and
KL Mode. `"Full"` restores every legacy ORB row for auditing. The seven `show*` flags were
repointed at `dashMode == "Full"`, so one selector drives the whole panel. Confluence folded
into the Setup row as `+2lvl`.

### Signal tooltip rebuilt

The old one-liner used bare abbreviations (`d-proxy`, `CLV`, `RVOL`) that meant nothing
without reading the source. Now labelled columns, plain language, and **volume is included**:

```
LONG  |  IBH breakout-retest
Score 9/7  - fires

LEVEL       2292.00
STACKED     1 more: ORBH

Entry ref   2294.50
Stop ref    2288.20   (risk 6.30)
Target 1    2304.10

VOLUME
  Session   0.70x  thin
  This bar  1.8x   strong
  Bar close 0.82   closed strong into the break
```

The on-chart text beside the triangle now carries the session factor too (`IBH-RT  0.7x`).

### Two over-deletions, caught and repaired

Recorded because the technique caused them and the check is what saved it. Both cuts used a
start marker plus a **section-header end marker**, and both swallowed everything in between:

1. Cutting the pullback inputs to the `TARGETS & RISK` header removed the **volume and trend
   input blocks** that sat between them.
2. Cutting the FVG arrays to `var bool orbNavigationCached` removed **~100 unrelated variable
   declarations** — nearly every ORB trade-state var in the file.

Both restored from a pre-cut backup. **The check that caught them**, worth keeping:

```bash
# nothing should disappear except what you intended
diff <(grep -oE '^var [a-z<>]+ +[A-Za-z_][A-Za-z0-9_]*' OLD | awk '{print $NF}' | sort) \
     <(grep -oE '^var [a-z<>]+ +[A-Za-z_][A-Za-z0-9_]*' NEW | awk '{print $NF}' | sort)
diff <(grep -oE '^[a-zA-Z_][a-zA-Z0-9_]*\(' OLD | sort -u) \
     <(grep -oE '^[a-zA-Z_][a-zA-Z0-9_]*\(' NEW | sort -u)
```

**Lesson: never bound a deletion with a section header.** Bound it with the last line that
belongs to the block being removed.

### Token accounting

Net for the session: **-2,201 proxy (~-9,550 compiled)**. The file is now far under the
ceiling — roughly 12,500 compiled of headroom against the 100,256 limit.

### Not verified

1. Compiles. The freeze pass rewrote 67 declarations; a malformed default would surface here.
2. Focus mode renders and Full still shows everything.
3. Verdict reports the correct blocking reason on a day where more than one filter fails.

---

## 2026-08-22j — ORB-width blocker fixed, headroom gate, IB% denominator corrected, cutoff 11:45

### Blocker 1 fixed: ORB width no longer gates non-ORB setups

`canTakeEntry` folded in `orbRangeFilterPassed`, so an IB extension or value-area setup could
be refused because the **Opening Range** was too narrow or too wide — a property of the ORB
and of nothing else. Split:

```pine
canTakeEntry         = cutoff + slot + gap + ORB width + minTime + NR   // ORB path
canTakeKeyLevelEntry = cutoff + slot + gap +             minTime + NR   // key-level path
```

Gap, min-entry-time and the NR gate are genuinely universal and are kept on both. ORB width
and ORB cross are the only two that are ORB-specific.

### ⚠ Blocker 2 found, NOT fixed: executed SL/TP would not match the alert

A key-level setup that fires with execution enabled sets `pendingLongEntry` and hands off to
the **shared** pending processor, which computes:

```pine
sl = calculateStopLoss(entry, activeHigh, activeLow, ...)   // ORB levels
[tp1..] = calculateTargets(entry, sl, ..., activeHigh, activeLow)  // ORB width
```

So an `IBH-RT` entry would receive a stop derived from the **ORB low** and a target anchored to
**ORB width**, while the observation alert reported `Ref SL` / `Ref T1` from `klExecMap()` —
level-based, measured from the level that actually triggered. **The two disagree.**

Harmless while `enableKeyLevelExecution = false`. **Must be fixed before execution is enabled.**
Design: latch `klSLLevel` / `klT1` when the pending is armed, and have the processor prefer them
whenever `klPendingSource != ""`, falling back to the ORB calculation for ORB entries.

### Headroom gate — "is there anything left to take?"

`klHeadroomR()` projects where the day tops out if its range expands to a normal ADR
(`dayLow + ADR` for a long), measures entry-to-there, and divides by the trade's own risk.
`klMinHeadroomR` (default 1.0R) refuses setups below the floor; the verdict reports
`⛔ NO ROOM` with the figure.

This is the objective form of "do not buy a breakout after the move is done". ADR is an
average and a real trend day exceeds it, which is why the default is permissive rather than
strict. **Live** — recomputed every bar from the running session high/low.

### IB% denominator corrected

Was `ibRange / klATRDaily` — "IB is 47% of a daily ATR", a number with no threshold anywhere in
the literature. Now `ibRange / average IB` over `volBaseDays` sessions, with Market Profile's
own bands: **<80% narrow** (coiled, trend-day candidate), **>120% wide** (rotational, IB
extremes tend to hold). `klIBAverage()` keeps the rolling history; stateful, called
unconditionally, banks the previous day's final IB range on rollover.

`klATRDaily` is now unused (it also lost its `godScore` consumer). Left in the daily tuple —
removing a tuple element for one unused warning is not worth the risk.

### Entry cutoff 11:00 -> 11:45

IB triggers cannot arm until the IB window closes at 10:15, and the HTF gate adds up to 15
minutes on top. The old 11:00 cutoff left the family now rated **primary** with roughly 30
usable minutes. 11:45 gives it 90, and stops exactly where the cheat sheet's session map puts
the lunch trap (F-G, 11:45-12:45), so no lunch-window block is needed.

### Static vs live — worth stating plainly

| Reading | Denominator | Numerator | Behaviour |
|---|---|---|---|
| `IB H/L` + `%` | avg IB, **static** for the day | IB range, **frozen at 10:15** | **Static after 10:15.** A day-type classifier, not a live gauge |
| `ADR / used` | ADR, **static** (prev 14 completed days) | session high-low, **live** | **Live, monotonically rising** — range only grows |
| `Room (R)` | trade risk, live | ADR projection vs price, live | **Live**, and directional — unlike `% used` |

`% used` is non-directional: a choppy day burns 100% of it going nowhere. `Room` is the metric
that actually answers "is there steam left **for this trade**".

### Token accounting

Net for the session: **-1,790 proxy (~-7,770 compiled)**. Estimated ~89,500 / 100,256.

### Not verified

1. Compiles.
2. `klIBAverage` reads sensibly — the first `volBaseDays` sessions warm up, and IB% should sit
   near 100% on an ordinary day.
3. `⛔ NO ROOM` fires on a late-session setup and not before.

---

## 2026-08-22k — Blocker 2 fixed, PM window, execution ON

### Blocker 2 fixed: executed SL/TP now match the alert

A key-level entry handed off to the shared pending processor, which derived the stop from
**ORB levels** and the target from **ORB width** — so an `IBH-RT` trade was executed with
geometry belonging to a completely different setup, while the alert reported `Ref SL` / `Ref T1`
measured from the level that actually triggered.

`klSLLevel` and `klT1` are now latched into `klArmedSL` / `klArmedT1` when the pending is armed.
The processor branches on `klPendingSource != ""`:

```pine
sl  = fromKL ? klArmedSL : calculateStopLoss(..., activeHigh, activeLow, ...)
tps = fromKL ? klCalcTargets(entry, sl, dir, klArmedT1) : calculateTargets(..., activeHigh, activeLow)
```

`klCalcTargets()` keeps the same 1x / 1.5x / 2x / 3x shape as the ORB version so the alert and TP
tracking downstream need no changes, but anchors on the **next structural level** instead of ORB
width, falling back to 1.5R when nothing lies ahead. Latches clear on consumption, cancellation
and session reset, alongside the existing `klPendingSource` clears.

### IB%: both denominators, and why ATR was wrong

Confirmed — **daily ATR was the wrong denominator.** ATR is *true* range, so it folds the
overnight gap into the number: on a gap day the denominator inflates and the IB reads falsely
narrow, exactly when the reading matters most.

But "ADR instead of ATR" only answers one of two different questions, so the row now carries both:

| Reading | Denominator | Question |
|---|---|---|
| `%IB` | average IB over N sessions | **Is this IB narrow or wide?** Market Profile's own bands, <80 / >120. This is what types the day |
| `%ADR` | ADR (high-low) | **How much of a normal day's range did the first hour eat?** A room question |

Row now reads e.g. `2292/2263.3  103%IB/49%ADR normal`.

### Afternoon entry window

`enableAfternoonWindow` (default ON), 13:45-14:15. The cheat sheet's session map puts a real
pickup at J-K (13:45-14:45) as the European open feeds through.

Two deliberate constraints:
- **Ends 14:15, not 14:45.** With the 15:00 time exit, a 14:15 entry has 45 minutes to work; a
  14:45 entry has 15 and is a coin flip.
- **Requires session volume factor >= Min Volume ×**, which the morning window does not. The PM
  thesis is entirely volume-driven; without the wave it is a thin tape with less runway.

### Production switches

- `enableKeyLevelExecution` default **true**.
- `enableVAAcceptance` (new, default **false**) suppresses plain VAH-ACC / VAL-ACC — the weakest
  family, no retest and no rejection structure, and the one that was firing at exactly 7/7 before
  the confluence double-count was fixed. VA rejection and VA retest are unaffected.

### Retest vs breakout — the distinction that matters

Two different things were being conflated:

| | What it is | Verdict |
|---|---|---|
| `enableRetestEntry` | An **order-placement** strategy: after a break, WAIT for a pullback to the level | **Stays OFF.** Already tested and rejected — adverse selection, 30-50% fill rate. The pullback often never comes, and when it does it is frequently because the break failed |
| `-RT` setups | A **pattern**: the engine detects a break-and-retest that has **already completed** and enters on the bar confirming the level held | **This is the retest quality worth having.** Nothing is waited for |

So: **breakout mechanically, retest quality via setup selection.** The engine already ranks
retest > rejection > break and awards +2 for retest structure. Raising `klScoreThreshold` biases
toward retests without touching the entry mechanism — a plain break with no confluence tops out
around 7-8; a retest with confluence clears 9-11.

### ⚠ Execution is ON but the file has NOT been compiled

Roughly fifteen structural changes have landed since the last successful TradingView compile
(the 16:30 chart). **Do not run this live until:**

1. It compiles clean.
2. Strategy Tester runs with `enableKeyLevelExecution = true` and the trade list is inspected —
   specifically that a key-level entry's SL/TP match its alert's `Ref SL` / `Ref T1`.
3. A key-level entry alert is confirmed parseable by `signal_engine/parser.py` (it goes out
   through `buildEntryAlert`, which is the proven format, but this path has never fired).
4. The PM window is observed opening and closing at the right times, and staying shut on a
   low-volume afternoon.

Inputs 60 -> 66.

---

## 2026-08-22l — Compile fix: Pine cannot return a tuple from a ternary

First compile after the execution changes. Six errors, all one root cause at the two pending
processors:

```
Cannot assign a variable to a tuple. The right side must be a function call or
structure ("if", "switch", "for", "while") returning a tuple with the same number of elements.
```

`[tp1, tp1_5, tp2, tp3] = fromKL ? klCalcTargets(...) : calculateTargets(...)` is invalid —
`?:` operates on values, and a tuple is not a value in Pine. Rewritten as the structure form the
error message itself names:

```pine
[tp1, tp1_5, tp2, tp3] = if fromKL
    klCalcTargets(entry, sl, true, klArmedT1)
else
    calculateTargets(entry, sl, true, activeHigh, activeLow)
```

The `sl` line above it is fine — that ternary returns a scalar.

**Rule for this file: a ternary may select a value, never a tuple.** Tuple destructuring needs a
function call or an `if`/`switch`/`for`/`while`.

### Checks added to the validation sweep

Two greps that would have caught this before the round trip, and a third that catches the other
common cause of a failed compile after bulk edits:

```bash
# tuple destructuring whose RHS is a ternary
grep -nE '^\s*\[[^]]+\]\s*=\s*[^=].*\?' breakout.pine | grep -v '= if'

# frozen-constant conversions that came out malformed
awk '/^[a-zA-Z_][a-zA-Z0-9_]*[ ]*=[ ]*[^=]/ && !/input\./ {q=gsub(/"/,"\""); if (q%2!=0) print NR": ODD QUOTES"; if ($0 ~ /,[ \t]*\r?$/) print NR": TRAILING COMMA"}' breakout.pine
```

Plus an arity checker comparing each function definition's argument count against every call
site — run across all 15 changed functions, all match. (`renderKeyLevels()` inside a comment
reads as a 1-arg call; that one is a false positive.)

---

## 2026-08-22m — Key-level-only mode; corrections from the model doc

Chart `charts/TCS_2026-08-22_18-23-51_99355.png`. **A real key-level trade was placed** —
`LONG IBH-RT 8/7 +1lvl 10:50`, entry 2296, stop 2291.23, TP1 2303.98, 20 shares, risk ₹95 (1%).

Validated by that chart:
- Compiles and runs with execution on.
- **Blocker 2 fix works.** Stop 2291.23 sits just under IBH 2292.0 — level-based. The ORB-derived
  stop would have been near ORB-L ≈ 2266. TP1 2303.98 sits just under VAH 2304.8: the structural
  target with its pad.
- Leading-zero fix works (`0.70x`, previously rendered `70x`).
- Setup row latches with time and folded confluence; IB row carries both denominators.
- Session VF is live: the 10:50 signal label reads `0.9x`, the end-of-day panel `0.70x`.

### I was wrong about VA acceptance

`volume-profile-model.md` makes acceptance **four of its eight scenarios** (①②④⑤), co-equal with
rejection — not a weak afterthought. Suppressing it was wrong and it is back on.

But the doc also requires **RVOL >= 1.5 on every entry** (§4C, §5C), and the implementation's
acceptance had *no volume condition of its own* — volume reached it only through the score, worth
+1 or +2 out of 7. That gap, not the family, was the real problem. New `klAccMinRvol` (1.5) gates
acceptance specifically. Rejection and retest are untouched: their thesis is absorption, which
does not require expansion.

### ORB demoted from trigger to level

`enableBreakout` now defaults **false**. `canDetectBreakout` and the breakout block are the only
consumers, so **ORB level building is untouched** — ORBH/ORBL still populate `klBuildRegistry`
and still earn confluence points. ORB stops competing for the session's single entry slot.

That answers "what if ORB breaks first, then IB?": previously first-come-first-served, so a 10:00
ORB break consumed the slot and the 10:50 IBH-RT never fired. Now the slot is always the key-level
engine's, and ORB contributes what it is actually good for — a level other levels can stack on.

### Day type now named by the doc's denominator

The doc (§8) defines IB% as **IB / average daily range**, `<35%` narrow, `>60%` wide. That is now
what names the day. The IB-vs-average-IB figure stays as a second reading — "is this IB unusual
for this stock" and "how much of a normal day's range did the first hour eat" are different
questions. Row reads `82%IB/49%ADR normal`.

### Key-level stops had no minimum distance

The live trade printed a **0.2% stop** (4.77 points at 2296). `calculateStopLoss()` floors ORB
stops at `max(0.5-1.5x ATR, 0.3% of price)`; `klExecMap()` bypassed that entirely and returned
`level ∓ 0.15 x ATR` raw. A 0.2% stop on a 5-min chart is inside the noise band — precisely the
sweep bait this whole effort is meant to avoid.

`klExecMap` now applies `minDist = max(0.5 x ATR, 0.3% of entry)`, and the stop may only move
**further** from entry, never nearer, so the level still governs whenever it is already wide
enough. `klSlBufferMult` raised 0.15 -> 0.35.

**Be aware of the trade-off.** On that TCS trade the floor would have widened risk from 4.77 to
~6.9 points. The structural target does not move, so R:R falls from ~1.67 to ~1.16 and position
size drops from 20 shares to ~13. That is the correct trade: 1.16R that survives beats 1.67R that
gets wicked out. The alternative — **skipping** setups whose level-based stop lands inside the
noise floor — is arguably cleaner and is the obvious next lever if signal quality matters more
than count.

### PM window widened to the doc's hours

Doc §4E gives `09:15-11:00 or 13:00-14:45`. PM window moved 13:45-14:15 -> **13:00-14:30**. Not
14:45: the time exit is 15:00, and an entry with 15 minutes left is a coin flip. Raise the time
exit if the full doc window is wanted.

### Why the entry was 10:50 and not 10:20

Structural, not a miss. Two independent gates made 10:20 impossible:

1. **IB does not close until 10:15.** `ibDone` requires `not klInIB`, so no IB setup can exist
   before then.
2. **The HTF gate reads the last CLOSED 15-min bar.** At 10:20 that is the **10:00-10:15** bar —
   a bar wholly inside the IB window, whose close is by construction `<= IBH`. `klHTFClose > IBH`
   cannot be true. The earliest 15-min bar that *can* close above IBH is 10:15-10:30, which only
   becomes readable from ~10:35.

From ~10:35 the `-RT` pattern still has to complete: break bar, pullback into the band, close back
above on a bull candle. **10:50 is the first bar where all of it was true.**

### Still not automatable: the doc's §7 footprint gate

The model doc is explicit — *"No footprint confirmation → no trade, regardless of score"* — and
that read (absorption vs follow-through on a GoCharting footprint) has no Pine equivalent. A fully
automated pipeline **drops that gate**. What stands in for it here is CLV, RVOL, the session volume
factor and the HTF close. Weaker than a footprint, and worth knowing you are trading without it.
