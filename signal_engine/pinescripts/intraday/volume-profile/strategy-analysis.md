# Volume Profile + Orderflow Strategy — Analysis & Automation Design

**Source requirements:** [`volume_profile_orderflow_strategy_requirements.md`](volume_profile_orderflow_strategy_requirements.md)
**Source diagram:** [`volume-profile-scenarios.jpeg`](volume-profile-scenarios.jpeg) ("Volume Profile Entry Zone")
**Target market:** Liquid NSE F&O stocks, 5-min execution / 15-min context
**Related infra:** ORB pipeline (PineScript → Telegram alert → `signal_engine` parser → executor → risk engine)

---

## 1. Guiding Principle (the brief, restated)

The requirements doc is deliberately conservative: it marks the entire GoCharting orderflow layer as "manual." The operator's directive is stronger and more precise:

> **Automate every objective, computable, and supporting input. Leave the human exactly ONE decision — the irreducibly discretionary one — and hand them a fully pre-computed decision packet so that single decision takes seconds, not analysis.**

So this document does two things the raw requirements do not:

1. **Re-classifies** each "manual" item into *truly discretionary* vs. *objectively computable but the requirements were cautious*. Several items the doc lists as manual (RVOL confirmation, candle outcome, level reclaim, next-candle confirmation, delta *proxy*) are fully automatable and should be automated.
2. **Defines the single critical manual decision** precisely, and designs the automation so everything the human needs to make *that* decision is pre-fetched, pre-scored, and pushed to them.

Result: a *semi-automated* system where the machine does ~90% of the work and the human does one high-value read.

---

## 2. Scenario Map — Diagram → Setups

The hand-drawn "Volume Profile Entry Zone" diagram enumerates 8 numbered price-action scenarios around the three value-area edges (VAH / VPOC / VAL) bounded by PDH/PDL. Mapping each to the requirements' setups A–E:

| # | Location in diagram | Price action | Auction meaning | Requirement setup | Direction |
|---|---------------------|--------------|-----------------|-------------------|-----------|
| ① | Above VAH (toward PDH) | pullback then continuation up | Acceptance above value | C (VAH breakout + acceptance) | **Long** |
| ② | Above VAH | V-shaped dip then up | Acceptance / retest-hold above value | C + E (retest) | **Long** |
| ⑧ | At/just below VAH | bounce up off VAH | VAH reclaimed as support | E (breakout + retest) | **Long** |
| ③ | At VAH | push into VAH, reject down | Failed auction at value high | A (VAH rejection) | **Short** |
| ⑦ | Inside value, above VAL | bounce up off VAL | Rotation from value low toward POC | B (VAL rejection) / inside-value rotation | **Long** |
| ⑥ | At VAL | dip below then reclaim up | VAL reclaimed (failed breakdown) | B + E (retest) | **Long** |
| ⑤ | Below VAL | continuation down | Acceptance below value | D (VAL breakdown + acceptance) | **Short** |
| ④ | Below VAL (toward PDL) | continuation down | Breakdown continuation | D | **Short** |

**Key reading of the diagram:** the tradeable events cluster at the *edges* (VAH, VAL) and just *outside* them (acceptance zones). "Imbalance" is annotated at the bottom — the diagram's author treats **orderflow imbalance at the edge** as the confirming trigger. This is exactly the human's single job (Section 4).

**Two behavioural families** across all 8:
- **Rejection / failed auction** (③, ⑥, ⑦, and the reclaim leg of ⑧): price probes past a level and snaps back → mean-revert toward POC/opposite edge.
- **Acceptance / continuation** (①, ②, ⑤, ④): price holds beyond a level with volume → trend continuation away from value.

The state machine (Section 5) must classify which family is in play before scoring.

---

## 3. Automation Decomposition — What Runs Where

This is the heart of the analysis. Every pipeline item, classified into three tiers, with a note where this **overrides** the requirements doc's more cautious classification.

### Tier 1 — Fully automated in PineScript (objective, deterministic)

| Item                                                                                                      | Notes |
|-----------------------------------------------------------------------------------------------------------|-------|
| Session / time windows (OR 09:15–09:30, IB 09:15–10:15, entry 09:15–11:00 & 13:00–14:45, hard exit 14:55) | Trivial; reuse ORB session code |
| Instrument universe filter                                                                                | Config list; reuse Chartink watchlist workflow |
| ORH / ORM / ORL, IBH / IBM / IBL                                                                          | Rolling high/low over window |
| PDH / PDL                                                                                                 | `request.security` daily |
| VWAP, 9 EMA, EMA slope                                                                                    | Built-ins |
| RVOL (current 5-min vol / SMA(N)), absolute-volume floor                                                  | **Requirements marked "manual confirmation"; this is objective → automate.** |
| Candle quality (CLV, body %, close-in-extreme-zone)                                                       | **Requirements listed "candle outcome" as manual; it is fully objective → automate.** |
| Level proximity & confluence scoring (VAH+PDH, VAL+ORL, etc.)                                             | Distance in ATR/ticks; cluster detection |
| Auction state (inside value / above VAH / below VAL)                                                      | Deterministic from levels |
| Objective rejection definition (High>VAH & Close<VAH; Low<VAL & Close>VAL)                                | Deterministic |
| Objective acceptance definition (2 closes beyond edge, or breakout close + next-bar hold)                 | Deterministic |
| Breakout + retest detection (break → pullback → hold → trigger)                                           | State machine; reuse ORB retest logic |
| "Level reclaimed?" / "next candle confirms?"                                                              | **Requirements listed manual; both are objective bar comparisons → automate.** |
| Setup score (Section 7)                                                                                   | Weighted sum |
| Position sizing (1% risk / stop distance), margin adjustment                                              | Reuse `risk.py` + Margin API |
| Level-based SL, 50%/50% first-target + runner, 14:55 hard exit                                            | Deterministic exit engine |
| Alert emission (decision packet)                                                                          | `alert()` JSON to Telegram |
| Backtest of all objective rules                                                                           | Strategy mode |

### Tier 2 — Automated *with validation* (computable, but needs data/accuracy checks)

| Item | Why not Tier 1 yet | Recommended approach |
|------|--------------------|-----------------------|
| **Previous-session VAH / POC / VAL** | Pine has no native profile that provably matches GoCharting | Reconstruct via lower-timeframe histogram (Section 6). Validate against GoCharting for ~2 weeks before trusting scores that weight it. |
| Developing (intraday) profile VAH/POC/VAL | Compute-heavy, repaints intrabar | Compute on confirmed bars only; use as context, not trigger |
| **Delta proxy** (buy/sell pressure from candle) | True delta needs tick/footprint data Pine can't see | Compute a *proxy* (CLV-weighted volume, up/down volume split) and label it as a proxy, not ground truth. Feeds the packet, does **not** replace the human read. |
| Complex multi-level auction classification | Edge cases when clusters overlap | Rule-based first; refine after live data |

### Tier 3 — The ONLY genuinely manual step (see Section 4)

| Item | Why it stays manual |
|------|---------------------|
| **Footprint absorption / stacked-imbalance read at the edge** | Requires per-price bid/ask volume (footprint) that TradingView/Pine do not expose. Interpreting *whether aggressive orders were absorbed* is genuinely discretionary and is the strategy's actual edge. |

Note the requirements doc's "Low/discretionary" list (absorption, "institutional activity," meaningful-imbalance judgement, final orderflow confirmation) **all collapse into this single read.** They are not five separate manual steps — they are five lenses on one question, answered in one glance at the footprint.

---

## 4. The Single Critical Manual Decision

Everything above exists to bring the operator to exactly this one question, at exactly the right level, at exactly the right time:

> **"At this level, right now — did aggressive orders in the losing direction get ABSORBED, or did they get FILLED and follow through?"**

Concretely, on the GoCharting footprint the operator confirms **one** of two mutually exclusive readings:

- **Rejection setups (③⑥⑦, reclaim legs):** aggressive sellers (long) / buyers (short) hit the edge and were **absorbed** — price failed to extend, delta flipped against the aggressor → **confirm.**
- **Acceptance setups (①②④⑤):** delta agrees with the break, **no absorption** at the extreme, stacked imbalance in trade direction → **confirm.**

The human answers **YES / NO / SKIP** to a pre-staged checklist. Nothing else is asked of them. They do not compute levels, RVOL, scores, SL, or size — all of that is already in the packet.

**This preserves the requirements' core principle** ("orderflow is the final confirmation layer," "do not fully automate discretionary absorption interpretation initially") while satisfying the operator's directive (everything else automated).

---

## 5. Optimal Solution Architecture

Three PineScript modules (mirrors the requirements' recommended architecture) plus a decision-packet bridge into the existing signal engine.

```
┌─────────────────────── TradingView (PineScript v6) ───────────────────────┐
│                                                                            │
│  Module 1: Market Structure          Module 2: Setup Engine                │
│  • Prev-session VAH/POC/VAL (recon)  • Auction state classifier            │
│  • VWAP, EMA9 + slope                • Rejection / acceptance / retest FSM  │
│  • OR / IB / PDH / PDL               • RVOL + candle-quality gates          │
│  • Confluence clusters               • Confluence + trend alignment         │
│                                      • Delta PROXY (labelled)               │
│                                      • Setup score (0–10+)                  │
│                                                                            │
│                    Module 3: Execution / Backtest                          │
│                    • Level-based SL, 50% TP + runner, 14:55 exit            │
│                    • Position sizing (display), R:R                         │
│                    • Emits DECISION PACKET when score ≥ threshold           │
└────────────────────────────────┬───────────────────────────────────────────┘
                                  │  Telegram alert (JSON decision packet)
                                  ▼
              ┌──────────────────────────────────────────┐
              │  Operator (phone / desk)                  │
              │  Reads packet → opens GoCharting footprint │
              │  → ONE decision: absorption? YES/NO/SKIP  │
              └───────────────────┬──────────────────────┘
                                  │  YES → forward / reply to confirm
                                  ▼
      ┌────────────────────────────────────────────────────────────┐
      │  signal_engine: parser → validator → risk → executor        │
      │  (reuses ORB pipeline; VP alerts are a new signal source)   │
      └────────────────────────────────────────────────────────────┘
```

### Why this split is optimal

- **Pine is authoritative for everything objective** → one source of truth, backtestable, no operator arithmetic.
- **The operator is a gate, not a calculator** → their scarce attention is spent only on the footprint read, which is the actual alpha.
- **Reuses the proven ORB rails** → alert-to-execution path, risk engine, margin adjustment, loss limits, and hard-exit all already exist and are battle-tested (see `MEMORY.md`). VP is a new *signal source*, not a new *execution stack*.

### Two automation postures for the confirmation gate (operator chooses)

| Posture | How the human confirms | When to use |
|---------|------------------------|-------------|
| **A. Alert-only (Phase 1, recommended start)** | Packet arrives; operator manually places order in OpenAlgo after footprint YES. No auto-execution. | Until profile reconstruction + scoring are validated live |
| **B. Confirm-to-execute (Phase 2)** | Packet arrives; operator replies `CONFIRM <id>` in Telegram → signal engine executes with the pre-computed SL/size. `SKIP` / no-reply within N minutes → packet expires. | After the score is trusted and a reply-capture path exists |

Posture B is the truest expression of the brief: the *only* human input into the automated execution path is a single confirm token that encodes the footprint decision.

---

## 6. Volume Profile Reconstruction — The Critical Feasibility Risk

This is the one place the design can silently break, because **the scores lean heavily on VAH/POC/VAL and Pine's values may not match GoCharting's.**

### The problem
Pine has no built-in that reproduces a broker/GoCharting volume profile. Profiles differ by: bucket (row) size, volume basis (real vs. tick vs. up/down split), and whether the value area is 68%/70% and grown from POC outward.

### Recommended approach
1. **Reconstruct with a lower-timeframe histogram.** Pull 1-min (or finer) `high/low/volume` for the *previous session* via `request.security_lower_tf`, distribute each bar's volume across price buckets it spanned, then:
   - POC = bucket with max volume.
   - Value area = grow outward from POC (add the larger-volume adjacent bucket each step) until ≥ **70%** of session volume is enclosed → edges are VAH / VAL.
2. **Make bucket size and VA% configurable** (`ticks_per_row`, `value_area_pct`) so it can be tuned to match GoCharting.
3. **Validation gate (mandatory before trusting profile-weighted scores):** run for ~10 sessions, log Pine's VAH/POC/VAL next to GoCharting's for the 10-symbol watchlist, and require median absolute error within an acceptable band (e.g. ≤ 1 tick-row). Until then, **display the profile-based score component separately** and let the operator sanity-check it against their own chart.

### Fallback if reconstruction proves unreliable
Treat GoCharting VAH/POC/VAL as an **operator-supplied morning input**: the operator reads the three numbers per symbol from GoCharting at 09:10 and enters them once (config/Telegram command). Pine then uses *authoritative* levels and automates everything downstream. This is a pragmatic middle path — one small manual data entry buys exact levels and removes the reconstruction risk entirely. Recommended if the 10-session validation fails.

---

## 7. Setup Scoring — Refined Framework

Starting from the requirements' scoring table, with weights that reflect the strategy's own principle ("level location is primary; orderflow is final confirmation") and are **explicitly backtestable, no overfitting until data exists.**

| Component | Points | Automated? |
|-----------|-------:|:----------:|
| Major VAH/VAL/POC interaction | +2 | Yes (Tier 2 — profile) |
| Multi-level confluence (2+ levels clustered) | +2 | Yes |
| Breakout + retest structure present | +2 | Yes |
| PDH / PDL confluence | +1 | Yes |
| ORH / ORL confluence | +1 | Yes |
| IBH / IBL confluence | +1 | Yes |
| VWAP alignment | +1 | Yes |
| EMA9 alignment + slope | +1 | Yes |
| RVOL ≥ 1.5 | +1 | Yes |
| RVOL ≥ 2.0 | +2 (replaces the +1) | Yes |
| Strong candle outcome (CLV) | +1 | Yes |
| **Delta proxy agrees with direction** | +1 | Yes (Tier 2 — *proxy only*) |

**Bands (starting spec, to be tuned):**
- **Score ≥ 7** → emit decision packet (high-priority candidate).
- **Score 5–6** → watch-only alert (no packet / no confirm path).
- **< 5** → ignore.

**Design rules (from the requirements, enforced here):**
- Trend/VWAP/EMA is a **filter, not a trigger** — a high score with no level interaction cannot fire.
- Score **excludes** the footprint read entirely — the human read is a separate, final AND-gate, never a score contributor. This keeps the objective backtest clean (requirement 17: "backtest objective Pine rules separately from the discretionary filter").
- Freeze exact thresholds before backtesting to prevent rule drift.

---

## 8. Decision Packet — Alert Format

The packet is the whole point: it front-loads *everything* the operator needs so the footprint read is the only remaining work. Extends the requirements' example alert.

**Telegram-readable line (human):**
```
TCS LONG | VAH breakout-retest | Score 8/10 | RVOL 2.1
Level: VAH 3842 (+PDH 3840, +ORH 3838)  ← triple confluence
Trend: ✓ >VWAP ✓ >EMA9 ✓ slope+   Candle: ✓ CLV 0.78   Δ-proxy: ✓ buy
Entry ~3845  SL 3831 (below VAH)  Risk 14pt  Qty 22 (1%)  T1 3859 (POC/next level)
➡ CHECK FOOTPRINT: buyers stacked above VAH? sellers NOT absorbing?  → CONFIRM tcs-1013 / SKIP
```

**Machine JSON (execution path, Posture B):**
```json
{
  "source": "volume_profile",
  "id": "TCS-20260812-1013",
  "symbol": "NSE:TCS",
  "direction": "long",
  "setup": "vah_breakout_retest",
  "score": 8,
  "rvol": 2.1,
  "levels": {"vah": 3842, "poc": 3868, "val": 3810, "pdh": 3840, "orh": 3838},
  "confluence": ["VAH", "PDH", "ORH"],
  "context": {"vwap": true, "ema9": true, "ema_slope": true, "clv": 0.78, "delta_proxy": "buy"},
  "entry": 3845, "sl": 3831, "risk_points": 14, "t1": 3859, "runner": true,
  "expires_at": "2026-08-12T10:23:00+05:30"
}
```

Design choices:
- **Confluence is explicit and human-legible** — the operator instantly sees *why* this level matters.
- **The footprint prompt is in the message** — tells them exactly what to look for, tailored to setup family (rejection vs. acceptance).
- **`expires_at`** — packets are perishable; a stale one auto-voids (matches the 09:15–10:00 / 13:00–14:45 windows).
- **`source: "volume_profile"`** — lets `signal_engine` route/tag VP signals distinctly from ORB for separate performance tracking.

---

## 9. Entry / Risk / Exit (all automated)

Directly reuses the signal engine's existing mechanisms — no new risk code needed.

| Element | Rule | Reuses |
|---------|------|--------|
| Entry | Only after operator confirm (Posture A/B) | ORB alert→executor path |
| Risk per trade | 1% of day-start capital / stop distance | `risk.py`, `use_day_start_capital` |
| Position size | `floor(capital × 0.01 / stop_distance)`, then margin-adjusted | `adjust_qty_for_margin()` |
| SL | Level-based (below reclaimed VAH / above reclaimed VAL, or level ± buffer) — **not** ATR-fixed | new SL mode in Module 3 |
| First target | Book 50% at next major liquidity level (POC, opposite VA edge, PDH/PDL) | multi-TP partial-exit already supported |
| Runner | 50% held; manage off subsequent structural levels; runner SL to lock profit | `tp1_runner_sl_buffer` pattern |
| Hard exit | 14:55 forced close, no exceptions | time-exit engine |
| Loss limits | Daily/weekly/monthly + portfolio heat + max open positions | `risk.py` (unchanged) |
| SL-before-exit ordering | Cancel broker SL before any exit order | existing OCO fix (see `MEMORY.md`) |

**Level-based SL is the notable new piece.** Unlike ORB's ATR/%/swing modes, VP stops sit at the *invalidation level*: a VAH-reclaim long is wrong if price loses VAH, so SL = VAH − buffer. This must enforce `min_sl_pct` (reject sub-0.5% stops inside the spread) exactly as ORB does.

---

## 10. Integration with the Existing Signal Engine

VP is a **new signal source on existing rails**, minimizing new surface area:

1. **Parser** — add a `volume_profile` message shape (the JSON above). The parser already normalizes ORB alerts; add field mapping for `levels`, `confluence`, `setup`.
2. **Validator** — reuse price-band, min-SL, min-R:R checks. Add: reject if `expires_at` passed; reject if packet already confirmed (idempotency by `id`).
3. **Confirm gate (Posture B)** — new: capture a Telegram reply `CONFIRM <id>` / `SKIP <id>`, match to a pending packet, then hand to executor. This is the *only* new human-in-the-loop code.
4. **Risk / executor / tracker** — unchanged. VP trades count toward the same daily limits and portfolio heat as ORB (they share capital).
5. **Performance tracking** — tag trades `source=volume_profile` so VP and ORB expectancy are measured separately (mirror `SIGNAL-PERFORMANCE-2026-Q1.md`).

**Config additions** (fail-fast, per the project's config pattern — no in-code defaults):
```yaml
volume_profile:
  enabled: true
  watchlist: [TCS, ICICIBANK, SBIN, HDFCBANK, RELIANCE, AXISBANK, INFY, KOTAKBANK, BHARTIARTL, HCLTECH]
  ticks_per_row: <tuned>
  value_area_pct: 0.70
  score_threshold: 7
  rvol_min: 1.5
  clv_long_min: 0.65
  clv_short_max: 0.35
  entry_windows: ["09:15-10:00", "13:00-14:45"]
  hard_exit: "14:55"
  packet_ttl_minutes: 10
  require_operator_confirm: true   # Posture A/B switch
```

---

## 11. Phased Implementation Roadmap

Ordered so the riskiest assumption (profile accuracy) is validated before anything depends on it, and the operator gets value early.

| Phase | Deliverable | Exit criterion |
|-------|-------------|----------------|
| **0 — Freeze definitions** | Lock exact objective rules (rejection/acceptance/retest/score) in this doc | Signed off; no drift during build |
| **1 — Module 1 (structure)** | Levels + VWAP/EMA/OR/IB/PDH/PDL + confluence, on chart | Levels render correctly on all 10 symbols |
| **1b — Profile validation** | Prev-session VAH/POC/VAL reconstruction (Sec. 6) | ≤ 10-session error study passes, OR fall back to operator-supplied levels |
| **2 — Module 2 (setup engine)** | Auction FSM, RVOL/CLV gates, delta proxy, score | Score matches manual scoring on replayed days |
| **3 — Decision packet (Posture A)** | Alert-only packets to Telegram; operator trades manually | Packets fire only in windows, only ≥ threshold, no false repaints |
| **4 — Backtest** | Strategy-mode backtest of objective rules (no footprint) | Positive expectancy on objective rules alone establishes the floor |
| **5 — signal_engine parser + validator** | VP source ingested, validated, tracked separately | Paper/analyzer trades round-trip cleanly |
| **6 — Confirm-to-execute (Posture B)** | `CONFIRM <id>` reply path → auto-execution with pre-computed SL/size | Live small-size validation; loss limits enforced |
| **7 — Tune** | Weights, thresholds, level-SL buffers from live data | Documented like ORB Q1 analysis |

**Testing note (per project TDD rules):** the `signal_engine` additions (parser shape, validator rules, confirm-gate, level-SL sizing) are Python and get unit + integration tests to the 80% bar, same as the existing risk module (201 tests / 99% today). The PineScript objective rules are validated by replay + Strategy Tester, not pytest.

---

## 12. Decisions the Operator Must Make (open questions)

These are genuine forks the analysis cannot resolve alone — they change what gets built:

1. **Profile source** — trust Pine reconstruction (after validation), or supply VAH/POC/VAL from GoCharting each morning? (Section 6 fallback.)
2. **Automation posture** — start Posture A (alert-only) and graduate to B (confirm-to-execute), or stay alert-only permanently?
3. **Capital sharing** — do VP trades share the same daily-loss / open-position budget as ORB, or get a separate allocation?
4. **Delta proxy in score** — include the +1 proxy point, or keep the score 100% footprint-free and let the human's footprint read be the *only* orderflow input?
5. **Watchlist scope** — fixed 10-symbol list, or feed it from the Chartink workflow like ORB?

---

## 13. Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Pine profile ≠ GoCharting profile → wrong levels → wrong scores | **HIGH** | Mandatory 10-session validation gate; operator-supplied-levels fallback |
| Repainting of intrabar profile / VWAP → false triggers | HIGH | Confirmed-bar-only evaluation; `alert.freq_once_per_bar_close` |
| Operator over-trusts automated score, skips the footprint read | HIGH | Packet *requires* an explicit CONFIRM; score deliberately excludes orderflow |
| Delta *proxy* mistaken for real delta | MEDIUM | Always labelled "Δ-proxy"; never gates entry; footprint is authoritative |
| Perishable packets acted on late | MEDIUM | `expires_at` / TTL auto-void |
| VP + ORB double-exposure on same symbol/capital | MEDIUM | Shared risk budget + `max_positions_per_symbol: 1` |
| Overfitting the score on thin data | MEDIUM | Freeze rules; backtest objective layer separately; tune only after live sample |
| Low liquidity / circuit / lot-size issues | LOW | Restrict to the liquid F&O universe; reuse ORB instrument gates |

---

## 14. Summary

The requirements describe a semi-automated system and conservatively fence off the entire orderflow layer as manual. This analysis shows that **only one step is truly manual — the footprint absorption read at the level** — and everything the requirements listed as manual (RVOL, candle outcome, level reclaim, next-candle confirmation, delta *proxy*) is objectively computable and should be automated.

The optimal solution is therefore:

- **Three PineScript modules** compute all structure, all setups, all scores, all SL/size — and emit a **decision packet** that front-loads every input the operator needs.
- **The operator makes one decision** (absorption? YES/NO/SKIP), pre-staged with a setup-specific footprint checklist.
- **The existing signal-engine rails** (parser → validator → risk → executor, loss limits, margin adjust, hard exit) execute it — VP is a new *signal source*, not a new *stack*.

The single highest-risk dependency is **prior-session profile reconstruction accuracy**; it is gated by a mandatory validation study with a clean operator-supplied-levels fallback, so the rest of the system can proceed regardless of how that resolves.

This satisfies both constraints: the requirements' principle that *orderflow is the final human confirmation layer*, and the operator's directive that *everything else is automated and delivered as supporting information.*
