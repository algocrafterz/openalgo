# Volume Profile Entry Model — Reference

**Source diagram:** [`volume-profile-scenarios.jpeg`](volume-profile-scenarios.jpeg) ("Volume Profile Entry Zone")
**Implementation:** [`volume-profile-decision-assist.pine`](volume-profile-decision-assist.pine)
**Analysis:** [`strategy-analysis.md`](strategy-analysis.md)

This is the authoritative reference for the model in the hand-drawn diagram: the levels, the 8 numbered entry scenarios, and the exact long/short entry conditions the indicator enforces.

---

## 1. The Map — Key Levels

All value-area levels are **previous-session** levels (frozen for the day) plus intraday structure:

| Level | Meaning | Source in tool |
|-------|---------|----------------|
| **VAH** | Value Area High — upper edge of the prev session's 70% volume zone | AUTO (1-min reconstruction) or MANUAL |
| **POC** | Point of Control — prev session's most-traded price (magnet) | AUTO or MANUAL |
| **VAL** | Value Area Low — lower edge of the 70% volume zone | AUTO or MANUAL |
| **PDH / PDL** | Previous Day High / Low | `request.security` daily |
| **ORH / ORL** | Opening Range (09:15–09:30 IST) high / low | tracked live |
| **IBH / IBL** | Initial Balance (09:15–10:15 IST) high / low | tracked live |
| **VWAP / EMA9** | Session VWAP, 9-EMA + slope (trend filter) | built-in |

The **value area** (VAH↔VAL) is where the market agreed on price yesterday. Trades happen at the **edges** (VAH, VAL) and just **outside** them — never in the middle.

---

## 2. The Diagram — 8 Entry Scenarios

The diagram enumerates 8 price-action scenarios around the three value-area edges, bounded by PDH/PDL. "Imbalance" is annotated at the bottom — orderflow imbalance at the edge is the confirming trigger (the one manual step, §7).

| # | Location | Price action | Auction meaning | Setup | Dir |
|---|----------|--------------|-----------------|-------|-----|
| ① | Above VAH → PDH | pullback then continuation up | Acceptance above value | VAH acceptance | **Long** |
| ② | Above VAH | V-dip then up | Acceptance / retest-hold above value | VAH acceptance / retest | **Long** |
| ⑧ | At VAH | bounce up off VAH | VAH reclaimed as support | VAH breakout-retest | **Long** |
| ③ | At VAH | push into VAH, reject down | Failed auction at value high | VAH rejection | **Short** |
| ⑦ | Inside, above VAL | bounce up off VAL | Rotation from value low → POC | VAL rejection | **Long** |
| ⑥ | At VAL | dip below then reclaim up | VAL reclaimed (failed breakdown) | VAL rejection | **Long** |
| ⑤ | Below VAL | continuation down | Acceptance below value | VAL breakdown-acceptance | **Short** |
| ④ | Below VAL → PDL | continuation down | Breakdown continuation | VAL breakdown-acceptance | **Short** |

---

## 3. Two Behavioural Families

Every scenario is one of two ideas. Classify which is in play **before** taking a trade:

- **Rejection / failed auction** (③, ⑥, ⑦, and the reclaim leg of ⑧) — price probes past a level and **snaps back**. Trade the reversion *toward POC / the opposite edge*. Confirmation = aggressive orders in the losing direction get **absorbed**.
- **Acceptance / continuation** (①, ②, ④, ⑤) — price **holds** beyond a level with volume. Trade the continuation *away from value*. Confirmation = delta **agrees**, no absorption at the extreme.

---

## 4. Long Entry Conditions

A long is valid only when **all** of these hold (as enforced in the indicator):

**A. Location (one of):**
- **VAL rejection** — bar's `low < VAL` **and** `close > VAL` (wick below value low, close back inside). *Covers ⑥, ⑦.*
- **VAH breakout-retest** — after a close breaks above VAH, price pulls back to within a confluence band of VAH and `close > VAH` holds, within the retest window. *Covers ⑧.*
- **VAH acceptance** — two consecutive closes above VAH. *Covers ①, ②.*

**B. Candle quality:** strong bullish close — `CLV ≥ 0.65` (close in the upper third of the bar).

**C. Participation:** `RVOL ≥ 1.5` (elevated volume vs SMA(20)).

**D. Trend filter (not a trigger):** price above VWAP and/or above rising EMA9. A high score with no level interaction cannot fire.

**E. Timing:** inside an entry window (09:15–11:00 or 13:00–14:45 IST).

**F. Score ≥ threshold** (default 7) — see §6.

**Stop:** just **below** the level in play (VAL or reclaimed VAH) − ATR buffer.
**Target 1:** nearest structural level above (POC → VAH → PDH / ORH / IBH).

---

## 5. Short Entry Conditions

Mirror image. All must hold:

**A. Location (one of):**
- **VAH rejection** — bar's `high > VAH` **and** `close < VAH` (wick above value high, close back inside). *Covers ③.*
- **VAL breakdown-retest** — after a close breaks below VAL, price pulls back to within a band of VAL and `close < VAL` holds, within the retest window.
- **VAL breakdown-acceptance** — two consecutive closes below VAL. *Covers ④, ⑤.*

**B. Candle quality:** strong bearish close — `CLV ≤ 0.35` (close in the lower third of the bar).

**C. Participation:** `RVOL ≥ 1.5`.

**D. Trend filter:** price below VWAP and/or below falling EMA9.

**E. Timing:** inside an entry window.

**F. Score ≥ threshold** (default 7).

**Stop:** just **above** the level in play (VAH or reclaimed VAL) + ATR buffer.
**Target 1:** nearest structural level below (POC → VAL → PDL / ORL / IBL).

---

## 6. Setup Catalog (indicator codes)

The 8 scenarios collapse into 6 objective setups the indicator detects and labels on the chart (▲ long / ▼ short arrow + code):

| Code | Scenarios | Trigger | Dir | Family |
|------|-----------|---------|-----|--------|
| `VAL-REJ` | ⑥ ⑦ | low < VAL, close > VAL, bull candle | Long | Rejection |
| `VAH-RT` | ⑧ | break above VAH → retest holds | Long | Rejection→continuation |
| `VAH-ACC` | ① ② | 2 closes above VAH | Long | Acceptance |
| `VAH-REJ` | ③ | high > VAH, close < VAH, bear candle | Short | Rejection |
| `VAL-RT` | (mirror of ⑧) | break below VAL → retest holds | Short | Rejection→continuation |
| `VAL-ACC` | ④ ⑤ | 2 closes below VAL | Short | Acceptance |

**Priority when several qualify:** retest → rejection → acceptance.

### Scoring (default threshold 7)

| Component | Points |
|-----------|-------:|
| Value-area level interaction (VAH/VAL/POC) | +2 |
| Structural confluence (PDH/PDL, ORH/ORL, IBH/IBL near the level) | +1 each (cap +3) |
| Multi-level confluence (2+ clustered) | +2 |
| Breakout + retest structure | +2 |
| VWAP alignment | +1 |
| EMA9 alignment + slope | +1 |
| RVOL ≥ 1.5 / ≥ 2.0 | +1 / +2 |
| Strong candle (CLV) | +1 |
| Delta **proxy** agrees | +1 |

Score **excludes** the footprint read — that is a separate, final manual gate (§7), never a score contributor.

---

## 7. The Manual Gate — Footprint Confirmation

The score gets you to the level with everything pre-computed. The **only** discretionary decision remains:

> At this level, right now — did aggressive orders in the losing direction get **ABSORBED** (rejection setups) or **FILLED with follow-through** (acceptance setups)?

Confirm on the GoCharting footprint:
- **Rejection (VAL-REJ / VAH-REJ / retest):** sellers (long) or buyers (short) hit the edge and were **absorbed**; delta flips against the aggressor → **confirm**.
- **Acceptance (VAH-ACC / VAL-ACC):** delta **agrees** with the break, stacked imbalance in trade direction, no absorption at the extreme → **confirm**.

No footprint confirmation → **no trade**, regardless of score.

---

## 8. Day-Type Context (bias the whole day)

Read once in the morning to set expectations (shown on the dashboard):

- **Open pos** — where price *opened* vs prev value area. Open **above VAH** or **below VAL** favours a **trend day** (lean on acceptance/continuation setups). Open **inside VA** favours a **rotation day** (lean on rejection setups fading the edges).
- **IB %** — Initial Balance range ÷ average daily range. **< 35% (narrow)** → trend-day potential (breakouts extend). **> 60% (wide)** → range day (edges hold, fade them).

---

## 9. Non-Negotiable Rules

1. Trades happen at the **edges** (VAH/VAL) and just outside — never mid–value-area.
2. **Trend/VWAP/EMA is a filter, not a trigger** — no level interaction, no trade.
3. **Footprint is the final AND-gate** — score is necessary, not sufficient.
4. **One position per symbol**, level-based stop, hard exit 14:55 IST.
5. On a fresh chart, AUTO profile needs **one completed prior session** before VAH/POC/VAL populate.
