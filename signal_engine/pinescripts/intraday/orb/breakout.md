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

