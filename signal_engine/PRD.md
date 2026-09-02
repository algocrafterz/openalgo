# Signal Engine — Reference

Autonomous trading pipeline: Telegram signals → OpenAlgo → Broker.
Runs alongside OpenAlgo as a separate process managed by `openalgoctl.sh`.

---

## Architecture

```
Telegram Channels
      │ raw text
      ▼
listener.py      — async Telethon client, stale guard, multi-channel
normalizer.py    — strip emojis, alias keys, handle TP HIT / SL HIT / pipe formats
parser.py        — text → Signal object
validator.py     — SL/TP direction, R:R, duplicate, stale, blacklist, price filter
      │ valid Signal
      ▼
main.py (_handle_entry / _handle_exit)
      ├─ risk.py        — check_exposure(), calculate_quantity()
      ├─ api_client.py  — fetch_available_capital(), fetch_margin(), fetch_positionbook()
      ├─ executor.py    — build_order(), send_order(), send_bracket_legs()
      ├─ tracker.py     — register position, poll SL fills, time exit, LTP monitoring
      ├─ notifier.py    — Telegram notifications
      └─ db.py          — persist trade audit to SQLite
```

---

## Pipeline

### Entry (LONG/SHORT)
1. `check_exposure()` — daily/weekly/monthly loss + portfolio heat + open positions
2. `fetch_available_capital()` → live capital; skip if below `min_capital_for_entry` floor
3. Day-start cached capital → `calculate_quantity()` → risk-based qty
4. `adjust_qty_for_margin()` [LIVE only]: NSE/BSE — binary reject if full-risk qty doesn't fit live capital (never scales down); derivatives — SpanCalc margin check, scales if needed
5. Apply `test_qty_cap` if set
6. `build_order()` + `send_order()` → `POST /api/v1/placeorder`
7. `send_bracket_legs()` → SL-M order only (see OCO constraint below)
8. `fetch_order_fill_price()` → confirm actual fill price
   - **10a. Fill overshoot check**: if fill ≥ TP (LONG) or fill ≤ TP (SHORT), cancel SL + send MARKET close + `record_close(0.0)` + return. Position never registered in tracker. Triggered by MARKET order execution lag on fast ORB breakouts where the entire TP range (e.g. 0.9%) is consumed before the order reaches the broker (VBL 2026-04-29: fill 533.95 vs TP 532.75).
9. `tracker.register()` → track for SL fill detection
10. `risk_engine.record_trade()` → persist counters
11. Notify + persist to DB

### Exit (TP HIT / SL HIT / EXIT signal)
1. Normalizer converts `"ORB TP1 HIT | SYMBOL"` or `"ORB SL HIT | SYMBOL"` → canonical EXIT signal
2. **Per-position asyncio.Lock acquired** — serializes concurrent TP signals for same position (TradingView fires all TP levels simultaneously at bar close; lock ensures second handler sees already-closed position)
3. **Phantom-exit guard** — if entry fill was never confirmed (`fill_price == 0`), query broker orderstatus before placing any exit. If status is `rejected/cancelled`, cancel orphaned SL, release slot, abort exit. Prevents naked short from TP signal on a non-existent position (EMAMILTD 2026-04-21 incident).
4. Resolve exit qty from `signal.exit_qty_pct` → `tp_levels` config → default 100%
5. **Cancel SL order first** — broker treats any SELL while SL SELL is active as new SHORT
6. `build_exit_order()` → MARKET SELL
7. `send_order()` with `tp_exit_retries` retries
8. Partial exit: reduce tracked qty, clear `sl_order_id`, re-place SL at TP1 − 0.3R (runner SL)
   - `tp1_runner_sl_buffer: 0.3` — 30% of R below TP1. Gives price room to wick-back and test TP1 before continuing to TP1.5. Old value 0.1R was too tight (TMPV 2026-04-13: ₹0.80 gap triggered by normal TP1 test wick). 0.3R scales correctly across ₹150–₹800 filter (9–72 ticks depending on SL%).
   - Runner SL is the **only automated protection** when TP1.5 signal is delayed or missing.
   - `compute_next_tp()` derives next TP level (TP1→TP1.5, TP1.5→TP2, TP2→TP3) for notification
   - Telegram notification includes: booked qty, remaining qty, new SL, next TP price
   - **TP1.5 exit fires only on TradingView TP1.5 HIT alert** — engine has no autonomous LTP monitoring for TP exits. If the alert is delayed/missing, runner holds until runner SL fires or time exit at 15:00.
   - `bracket.use_extended_runner_tiers` (default off, see 2026-09-02 changelog entry): when on, the runner SL ratchets to whichever TP level was just hit instead of always TP1, and never loosens — each further partial exit only tightens the stop.
9. Full exit: `tracker.unregister()` + `risk_engine.record_close()`

---

## Strategies in play

| Script | Alert tag | Status |
|---|---|---|
| `pinescripts/intraday/orb/orb.pine` | `ORB` | **Live.** No longer frozen: the time-exit alert was rebuilt 2026-08-29 (see that section). The channel is still running an older build — the R:R fix has not been redeployed. Documented in `HOW-IT-WORKS.md` |
| `pinescripts/intraday/orb/breakout.pine` | `BREAKOUT` | **Live as forward-data collection from 2026-08-30 — not as a proven edge.** Key-level engine (VA / PDH-PDL / IB); ORB entries off. The backtest premise is negative: key-level breaks follow through 30.9% against a 33.3% random-walk baseline (t = -9.87, 49,677 events), and no target, stop, bias or candle filter reverses it. Deployed deliberately to gather live trades for a first-week review. Changelog in `pinescripts/intraday/orb/breakout.md` (2026-09-02) |
| `pinescripts/intraday/ema9/ema9-intraday.pine` | `EMA9` | **Reference only — do not trade.** Backtest found no edge before costs (6577 trades, t = -9.90). Verdict block is in the file header. The source PDF's own six methods were tested separately and are all negative at zero cost — see its `STRATEGY-ANALYSIS.md` |
| `pinescripts/swing/momentum-rank/momentum-rank.pine` | `momentum-rank` | **Candidate, paper only.** 12-1 cross-sectional momentum, monthly rebalance. The only strategy tested so far with a positive out-of-sample edge that survives costs. Never traded |
| `pinescripts/swing/dividend-growth/dividend-growth.pine` | `swing-dividend-growth` | **Do not trade.** Entry has negative forward-return edge on the broad F&O universe; the Pine strategy block cannot book a loss. See its `STRATEGY-ANALYSIS.md` |
| `pinescripts/intraday/ib-extension/ib-extension.pine` | — | **Do not trade.** Zero gross expectancy at best (t = -0.71 at zero cost). See its `STRATEGY-ANALYSIS.md` |

`signal_engine` fully supports the `BREAKOUT` tag: `strategy_profiles.BREAKOUT` (product MIS,
`min_sl_pct` 0.002) and `blacklist.BREAKOUT` both exist, and the normalizer's strategy prefix
is generic, so no per-tag code path is needed. Both strategies are supported side by side;
nothing about `ORB` changes.

**Deployment posture set 2026-08-30.** `enableBreakout` OFF, `enableKeyLevelExecution` ON —
the key-level engine is the only thing in `breakout.pine` that trades. Three changes shipped
with it, all recorded in `breakout.md`:

- `structural_runner_sl` read `context["vah"]` while the alert emits `PVAH`, so every
  value-area runner silently fell back to the TP1-buffer stop. `_LEVEL_CONTEXT_KEYS` now maps
  family to wire key.
- `blacklist.BREAKOUT` added — the validator keys strictly on the tag, so `ORB`'s BHEL block
  was never applying.
- CLV gate relaxed 0.65/0.35 to 0.50/0.50. The 49,677-event study found the strict setting
  selects the worse half, and it also flattened the `CLV` column in the trade log to a
  constant, which made the gate untestable from live data.

---

## Signal field reference (BREAKOUT entry alerts)

Every line below `R:R` in a BREAKOUT entry alert is captured into `Signal.context` and stored
as JSON in `trades.db.context`. Measured at **arm time** — one bar before the fill, which is
the bar the decision was made on.

### Setup quality

| Field | Meaning | How to read it |
|---|---|---|
| `Trigger` | Which level fired, and how. `-RT` retest, `-REJ` rejection, `-BRK` break, `-ACC` acceptance | `VAH-RT` = price broke above Value Area High, came back to test it, held |
| `Score` | Composite setup score, 0–10. Threshold to fire is 7 | Did **not** separate winners on 2026-08-24. Don't over-trust it |
| `Conf` | Count of other levels within one ATR band, and their names | Confluence — 2+ levels stacked is a stronger wall |

### Participation (the strongest discriminator so far)

| Field | Meaning | How to read it |
|---|---|---|
| `RVOL` | This bar's volume ÷ average volume **at the same clock slot** over 14 prior sessions | >1.5 = real participation arriving. All 5 winners on 2026-08-24 had ≥1.5; both losers ≤1.0 |
| `VF` | Session-cumulative volume vs the same baseline | Whole-day measure. Slower than RVOL and did not separate winners |
| `CLV` | Close Location Value — where the bar closed in its own range. 1.0 = at the high, 0 = at the low | >0.7 on a long = buyers held the bar |
| `Delta` | buy / sell / neutral | **Derived from CLV** (>0.55 buy, <0.45 sell). Carries no extra information — not an independent variable |

### Position in the day

| Field | Meaning | How to read it |
|---|---|---|
| `Trend` | `vwap±` above/below session VWAP, `ema±` vs the 9-EMA, `slope±` EMA direction | All three aligned = trending with you |
| `Auction` | Where price sits vs the value area: Above VAH / Inside Value / Below VAL | Outside value = trending; inside = rotational |
| `OpenPos` | Where the session OPENED relative to value | Open outside value that stays outside is the strongest day type |
| `DayType` | Trend / Double Distribution / Normal / Neutral | Fading extremes is correct on Normal days, wrong on Trend days |
| `OpenType` | Open Drive / Open Test Drive / Open Rejection Reverse / Open Auction | Open Drive = one-way conviction from the bell |
| `AdrUsed` | % of the stock's average daily range already spent | High + continuation entry = late. TATAPOWER lost at 77% used |
| `Headroom` | R available before the next opposing level | <1.5R means the target is crowded |
| `Chase` | ATRs already travelled from the level | High = you are buying after the move, not at it |

### Volatility and geometry

| Field | Meaning | How to read it |
|---|---|---|
| `AtrPct` | The stock's ATR as % of price | The stock's natural noise. 0.5% is quiet, 3%+ is wild |
| `SlAtr` | Stop distance in ATRs | Near-constant (~0.5) **by construction** — the stop is defined in ATR terms. Low information |

### Raw level prices

`ORH` / `ORM` / `ORL` opening range high / mid / low · `IBH` / `IBM` / `IBL` initial balance
(first hour) high / mid / low · `PVAH` / `PPOC` / `PVAL` previous session value area high /
point of control / low · `PDH` / `PDL` previous day high / low.

The `P` prefix marks previous-session levels; bare names are today's. It exists because
TradingView's Session Volume Profile plots the *current* day's VAH/POC/VAL, so without it two
sets of lines carry the same three names at different prices. Setup codes are a separate
namespace and keep their original spelling (`VAH-ACC`, `VAL-REJ`).

`-` means the level does not exist today (no prior session loaded, IB still building, ORB
disabled). These let post-hoc analysis ask questions the categorical fields cannot: how far
was entry from PPOC, was IB unusually wide, did PDH cap the move.

---

## Platform upgrade — OpenAlgo 2.0.0.2 -> 2.0.2.1 (2026-08-25)

Merged 1146 upstream commits onto a branch off `feature/optimize-signal-engine`. Twelve conflicts; the ones that mattered:

- **`broker/flattrade/mapping/transform_data.py`** — upstream had independently implemented the same MPP fix, plus a fallback this branch lacked: a missing auth token, zero LTP or quote exception must never leave an SL-M going out as SL-MKT, which the Noren OMS rejects for API orders (order dead on arrival). Took upstream's fallback, kept this branch's trigger-based MPP base and zero-price normalisation. **This changes live SL-M behaviour on flattrade** — the price type is now always SL-LMT, derived from the trigger when no quote is available.
- **`broker/flattrade/mapping/order_data.py`** — upstream's `normalize_order_status()` plus this branch's `rejection_reason` field.
- **`blueprints/brlogin.py`** — union of the external-auth broker list (`iiflcapital` upstream + `flattrade` here).
- **`app.py`** — upstream's startup banner, keeping `allow_unsafe_werkzeug=True`.
- **`pyproject.toml`** — both dependency sets merged; the auto-merge had duplicated a `pythonpath` key, which made the file unparseable. `uv.lock` regenerated.

Operational notes:

- 23/23 migrations applied. `flow_workflows.api_key` verified present.
- 19 new keys merged into `.env` additively. **`FERNET_SALT` was deliberately not copied** — the sample value is a placeholder, and changing the salt would break decryption of stored broker tokens and the API key.
- `vectorbt` 0.28.4 -> 1.0.0 (major). Nothing currently imports it.
- Two stale tests corrected: `test_flattrade_transform.py::TestMppFallback` asserted the SL-MKT fall-through the release deliberately fixed, and `test_sizing_api.py` stubbed `database.auth_db` as a MagicMock while upstream's `settings_db` now does `from database.auth_db import PEPPER` at import time.

Pre-existing conditions confirmed *not* caused by the upgrade: `test_auto_login.py` unpacks 2 values from `auto_login()`, which returns 3; fourteen `test_backtest_*.py` files import a `backtest` package that does not exist on this branch; `eventlet` is absent from `uv.lock` although production runs `gunicorn --worker-class eventlet`.

---

## Recent Changes (2026-09-02)

### Extended runner tiers for BREAKOUT: TP1/TP2/TP3 profit booking, gated and revertible

`breakout.pine` previously offered only a 2-tier exit: `tp1ExitQtyPct` (default 50%) at TP1,
then either `tp1_5ExitQtyPct` (default 0%, display-only, held for TP2) or 100% at TP2 —
TP2 always closed whatever remained, so a trend day that ran to TP3 (RECLTD, FEDERALBNK,
2026-08-24) had nothing left to book there. `buildRunnerObsAlert` recorded that as an
unbooked move after the fact; it never captured any of it.

Added a new `useExtendedRunnerTiers` boolean input (default off) that, when on, overrides
the effective exit fractions to TP1=30%, TP1.5=0% (unchanged, still held for TP2), TP2=50%
of whatever remains, TP3=100% of whatever remains — a net 30/35/35 split. Off reproduces the
prior behavior exactly; this is the full revert path. A matching `config.yaml` boolean,
`bracket.use_extended_runner_tiers`, gates a companion change on the python side: the
runner-SL ratchet in `main.py`'s `_replace_runner_sl()` now anchors the post-partial-exit
stop to whichever TP level was just hit (TP1 → TP1.5 → TP2) instead of always TP1, and never
loosens the stop already in place. `compute_next_tp()` gained a TP2 → TP3 entry so partial-
exit notifications show the right next target once TP2 becomes a partial exit.

**Why 30/35/35 and not 40/30/30 or 50/25/25.** The split has to balance two things: not
missing the runner on a trend day, and still banking whatever the move gave even if TP2/TP3
never arrive. BREAKOUT's key-level engine has no TP2/TP3 hit-rate data of its own — it has
only been live since 2026-08-30 as forward-data collection against a *negative* backtest
(above: unconditional key-level breaks follow through 30.9% against a 33.3% random-walk
baseline). An initial 40/30/30 default leaned on ORB's own number instead (72.3% of TP1
trades also reach TP1.5 — `pinescripts/intraday/orb/STRATEGY-ANALYSIS.md`) as the closest
available evidence, which made it a conservative placeholder rather than a measured optimum
for this strategy.

The stock universe feeding BREAKOUT has since narrowed to trend-qualified names only, which
raises confidence in continuation without producing a BREAKOUT-specific number to calibrate
against. 30/35/35 shifts weight from the TP1 bucket into the TP2/TP3 buckets while holding
TP2's share of the remainder fixed at 50% (TP2 and TP3 land equal by construction, so the
only real knob is TP1's own percentage). 50/25/25 was rejected — it banks more at TP1 but
shrinks the TP3 runner leg, working against not missing the runner. 20/40/40 or lower was
also rejected — it leaves too little booked at TP1, the easiest target (R=1.0), for a
strategy with no proven edge yet, working against banking whatever the move gave.

The runner-SL ratchet is what makes 30% (rather than something higher) defensible for the
second goal: the 70% held past TP1 is not exposed and hoping for TP2 — its stop moves up to
near TP1's price the instant TP1 fires, so it is either taken out at a still-profitable
level or continues toward TP2/TP3. The fixed exit fractions decide how much becomes cash at
each checkpoint; the ratchet decides how much of the rest is protected regardless of whether
a further checkpoint is ever reached.

Tests: `test_main_partial_exit.py` (TP2→TP3 in `compute_next_tp`, ratchet on/off, ratchet
never loosens an existing SL), plus two pre-existing tests in `test_main_characterization.py`
and `test_main_helpers.py` updated from asserting `compute_next_tp(pos, "TP2") is None` to
the new TP2→TP3 contract.

### Runner-SL ratchet: buffer now scales with the anchor's own R-multiple

Follow-up correction, same day. The ratchet above was first written with a flat buffer —
`tp1_runner_sl_buffer` (0.3R) subtracted from whichever level the stop had just moved to,
unchanged regardless of which level that was. That is fine at TP1, where it was already the
long-standing behaviour (this SL was never at breakeven — it sits at TP1 minus the buffer,
i.e. ~70% of the TP1 leg's profit locked in, and that predates this feature). It is a problem
once the anchor ratchets further out: a flat 0.3R buffer is 30% of TP1's own distance from
entry but only 15% of TP2's, so the stop sits proportionally closer to TP2 — a level that is
itself likely to draw other participants' stops and targets — than it ever did to TP1. That
is a real stop-hunt exposure the flat buffer did not account for.

Fixed by scaling the buffer by the anchor's own R-multiple (1.0 at TP1, 1.5 at TP1.5, 2.0 at
TP2): `buffer = tp1_runner_sl_buffer x risk_distance x anchor_multiplier`. This keeps the
same proportional headroom — 30% of that leg's own distance — at every level the ratchet
reaches, instead of a fixed absolute amount that shrinks in relative terms the further out it
goes. TP1's own behaviour is unaffected (multiplier 1.0, same result as always); only the
TP1.5/TP2 ratchet introduced by extended runner tiers changes. Test coverage extended with a
TP1.5 case verifying the 1.5x scaling directly.

Whether TP1's own long-standing ~70%-of-leg lock-in (as opposed to a stop nearer breakeven)
is itself worth revisiting is a separate, larger question — it is the default for every
strategy using the runner SL, not something introduced by extended runner tiers, and changing
it needs its own deliberate review rather than folding into this feature's scope.

## Recent Changes (2026-08-30)

### The key-level premise measured against a random walk, and it loses

`breakout.pine`'s key-level engine had been recorded on 2026-08-28 as "gross indistinguishable
from zero, killed by the stop floor". That was too generous, and the stop was the wrong culprit.

Every PDH/PDL/IBH/IBL break in the universe was collected — **49,677 events** over 208 F&O names
and 59 sessions — and walked forward to see whether it reached +1.0 ATR before giving back
0.5 ATR. For a driftless random walk that probability is exactly `b/(a+b)` = **33.3%**; observed
is **30.9%**, t = **-9.87**. Only **1 of 208** symbols sits significantly above the baseline where
5 are expected by chance, and 35 sit below. Key-level breaks follow through *less* often than a
coin flip.

That is upstream of every exit-side knob, which is why none of them helped: fixed 1:1, 1:2, 1:3,
a stop at the initiative drive candle, a wider stop floor and a daily HTF bias gate were all
tested and all remain negative. Full tables in `pinescripts/intraday/orb/breakout.md`.

### The CLV gate selects the worse half of breaks

`klClvLongMin = 0.65` demands a decisive break candle. Measured across the same events, candle
strength is *inversely* related to follow-through: marubozu 29.6% < strong close 30.3% <
unconditional 30.9%, and engulfing is 29.3% on 8,731 events. Nothing clears 33.3%. A wide,
full-bodied close through a level is closer to exhaustion than confirmation — the move already
happened inside that bar. This extends the 2026-08-29 candlestick finding to the key-level
trigger specifically: there is no confirming candle to wait for.

An intermediate ablation on the filtered trade set (n = 384) had shown engulfing helping in BOTH
windows and was nearly recorded as a finding. At zero cost it nets +0.050R at t = 0.76, and the
8,731-event measurement contradicts it. Small-sample noise; the large sample governs. Logged
because that is precisely the kind of number that ships by mistake.

### Per-symbol selection is not possible on this metric

Split-half correlation of per-symbol follow-through is **r = +0.095** (p = 0.17); 2 of the first
half's top 20 stay top 20 against 4 by chance. Observed cross-symbol sd is 3.3 points against a
typical per-symbol standard error of 3.0 — almost all visible spread is noise. TCS reads 38.8%
then 29.6%, M&M 40.0% then 28.0%: statistically identical, and M&M's first half beats TCS's
second. A per-symbol whitelist cannot be built this way; select on movement and liquidity, which
do persist, and not on edge.

### The reversion is real and still untradeable

Fading the break (risk 1.0 ATR to make 0.5 ATR, fair value zero) wins 69.1% of the time,
t = **+11.50**, worth **+0.83 bps**; on strong-close breaks 69.7%, t = +13.25, **+1.05 bps**.
Against an 8-10 bps round trip it is an order of magnitude short. Statistical significance and
economic significance are different tests, and this passes the first and fails the second.

### `backtest/strategies/ema9_pdf.py` — the 9-EMA article, all six methods

`791187576-9-EMA-Trading-Strategy.pdf` is an 11-page HowToTrade.com explainer describing five
crossover variants plus one worked method. All six were implemented as a **separate** adapter
from `ema9.py` — that one mirrors `ema9-intraday.pine`, the production artefact, and the two must
be able to disagree visibly.

Every variant is negative gross **at zero cost**, on 902 to 9,140 trades each, and no target
sweep lifts any above zero. Two defects in the source are worth recording: five of the six
methods specify no stop and no target at all, and the worked example on p9 ends by referencing
FVGs and a fixed 10-11 AM window that contradict the engulfing rule two paragraphs earlier — it
is spliced from another article. Verdict and tables in
`pinescripts/intraday/ema9/STRATEGY-ANALYSIS.md`.

### `key_level.py` adapter knobs

Four additions, all defaulting to previously shipped behaviour so the baseline reproduces to the
trade: `tp_mode` (`level` | `r` | `level_min_r`) with `tp_r`; `sl_mode` (`level` | `drive` |
`wider`); `pattern` (`""` | `engulf` | `engulf_or_clv` | `pin`); and `use_daily_bias` /
`daily_bias_len`.

## Recent Changes (2026-08-29)

### Candlestick patterns measured, and mostly switched off

`pinescripts/intraday/orderflow/candlestick-patterns.pine` (repo32) retuned. All 14
directional patterns were transcribed to numpy and measured over 764,573 five-minute bars:
mean forward return over 12 bars in ATR units, signed by the pattern's direction, **minus the
unconditional forward return of the same bars**.

That subtraction is the finding. Before it, the result looked decisive - every bearish
pattern positive, every bullish one negative. The baseline turned out to be -0.0393 ATR: the
sample window drifted down and the "edge" was the drift. After subtracting it, at a key level:

| pattern | n | excess ATR | t |
|---|---|---|---|
| bearHarami | 8,819 | +0.0345 | +1.43 |
| invHammer | 16,377 | +0.0232 | +1.32 |
| bullKick | 1,342 | +0.0192 | +0.30 |
| bearKick | 1,318 | +0.0184 | +0.31 |
| bullHarami | 9,458 | +0.0078 | +0.35 |
| hammer | 16,931 | -0.0420 | **-2.37** |
| bullEngulfing | 7,579 | -0.0525 | **-2.03** |
| bullBelt | 213 | -0.1414 | -0.70 |

**No pattern reaches |t| > 2 positive.** Two are significantly backwards - hammer and bullish
engulfing are followed by price falling relative to baseline, on 17,000 and 7,600 samples.
Neither is inverted into a short signal: with 14 patterns swept, one or two crossing |t| = 2
by chance is expected, and building a rule on the unluckiest of a swept set is how backtests
lie. Both are simply off.

The script now gates every pattern to a key level (ORB/IB/PDH/PDL/prior-day value/prior
week), requires above-average volume, skips the opening bars, and enables only the five with
non-negative measured behaviour. This is consistent with the DHB result, where a required
confirmation candle was completely inert.

### `market-structure.pine` retuned for NSE

Flux Charts' Market Structure Dashboard carried three FX defaults that are wrong on an Indian
equity: ICT sessions and killzones on a New York clock (so an NSE session reported "NY LUNCH"
or "OFF HOURS" all day - replaced with the six real NSE phases, OPEN AUCTION / IB BUILD /
MORNING / MIDDAY LULL / AFTERNOON / SQUARE-OFF); a timeframe ladder spanning 1-minute noise
and a 4-hour bar that does not divide a 6h15m session, now 5/15/60/D with Daily weighted
highest; and FVG as a bias input, which on a gapping equity fires on the overnight gap every
morning, now off by default.

---

### `signal_engine/analysis/` — signal-to-fill reconciliation

A Telegram channel cannot measure trade quality, and the ORB audit showed why: it never sees
the fill price, it cannot weight a partial exit correctly, and a signal that died in the
pipeline looks identical to one that traded fine. The new package joins three sources into a
round-trip ledger:

    trades.db          every order the engine placed, with order_id, quantity, status
    broker tradebook   the actual fills, joined on order_id
    (Telegram export)  optional, to find signals that never reached the engine at all

Each leg keeps all three layers separate — `signal_price` (what the alert advertised),
`order_qty` (what the risk engine sized), `fill_price` (what the broker did) — because each
gap has a different owner. `order_id` is the only join key used; symbol-and-time matching
would silently mis-pair two trades in the same name on the same day.

    PYTHONPATH=. uv run python -m signal_engine.analysis --snapshot   # every evening
    PYTHONPATH=. uv run python -m signal_engine.analysis --positions

**The broker tradebook is wiped daily and cannot be re-queried for a past session**, so
`--snapshot` must run after every close or that day's fills are gone permanently. As a
durable fallback, `TradeResult.fill_price` now carries the entry fill that
`_fetch_entry_fill` was already fetching and only logging, and `db.save` persists it to a new
`fill_price` column. Entry slippage is therefore measurable from `trades.db` alone from now on.

Reconciliation flags are the output that matters: `NO_FILL`, `QTY_MISMATCH`,
`UNMATCHED_FILL` (a broker fill the engine never sent — manual intervention or auto
square-off), `OPEN_AT_EOD`, `OVER_EXITED`, `NO_ENTRY`. Run against the current live
`trades.db` (221 events, 186 positions) it immediately reports **157 positions, 84%, where
the engine never sent an exit event at all** — the same leak the Telegram audit found,
confirmed independently on the engine's own records.

The package deliberately computes no strategy verdict. Live samples are self-selected — you
only ran the strategies you believed in — so treating this ledger as a backtest would be
survivorship analysis. It is for finding leaks, which are real at n=20. 15 tests in
`tests/test_analysis_ledger.py`, one per failure mode.

---

### ORB60 beats ORB15, but "beats" means less negative

Swept the opening-range window on the same 208 names x 59 sessions, everything else at the
current repo defaults (`tp1MinRR=1.5`, `useOrbStopFloor=false`). Gross expectancy improves
monotonically as the window lengthens, in both windows:

| ORB window | trades | gross bps | net R | median entry | median R:R |
|---|---|---|---|---|---|
| ORB5  | 2511 | -2.54 | -0.169 | - | - |
| ORB15 | 2422 | -3.21 | -0.177 | 10:10 | 1.50 |
| ORB30 | 1844 | -1.99 | -0.157 | - | - |
| ORB60 | 709  | -0.38 | -0.131 | 10:45 | 1.88 |

With the entry cutoff moved to 13:30 so ORB60 is not amputated by an 11:00 gate (it cannot
arm before 10:15), ORB60 is the only configuration with positive gross expectancy in BOTH
windows: IS +0.10 bps, OOS +2.08 bps, ALL +0.85 bps, against ORB15's -1.45.

So the switch to ORB60 was right, and the mechanism is not subtle: it takes 71% fewer trades
(709 vs 2422) at a better R:R (1.88 vs 1.50), because a 60-minute range is wide enough that
clearing it means something. But **net R is still negative at every window** (-0.131 at
ORB60). The stop is ~0.72% of price, so 10 bps of cost is ~0.15 R per trade, and gross
expectancy of roughly zero minus 0.15 R is the whole result. Longer windows do not fix that;
they only stop making it worse.

Two things follow that matter operationally:

- **ORB60 and `breakout.pine`'s IB key level are the same level.** The initial balance is
  the first 60 minutes. Running both scripts on the same symbol takes the same trade twice
  from two sources, doubling size on one idea while the risk engine counts two positions.
- **IBH is the most-faded level in the whole level study** (38.9% continuation, break edge
  -0.222 - the worst of the 18 measured). ORB60 breakouts are, by construction, trading the
  level with the strongest measured tendency to reject. That is consistent with gross
  expectancy sitting at zero however the window is tuned.

---

### The live ORB channel was audited against its own Telegram log

`pinescripts/telegram/intraday-orb-channel-result-from-oldest-till-28082026.json` —
588 entry signals, 2026-01-28 to 2026-08-28, 138 sessions. Reconstructed into trades
(one entry owns every exit message until the next entry on the same symbol/day) and then
re-resolved against real 5-minute bars for the window yfinance still covers.

**47% of entries never received an exit alert of any kind**, and the rate climbs with time:

| Month | Entries | No exit alert | Median stop, % of price | Median R:R |
|---|---|---|---|---|
| 2026-02 | 101 | 5% | 0.58 | 1.5 |
| 2026-03 | 73 | 5% | 0.71 | 1.5 |
| 2026-04 | 89 | 45% | 1.98 | 0.64 |
| 2026-05 | 121 | 72% | 2.13 | 1.0 |
| 2026-06 | 66 | 61% | 2.21 | 0.8 |
| 2026-07 | 78 | 72% | 1.88 | 0.8 |
| 2026-08 | 50 | 86% | 1.72 | 0.8 |

Two independent faults, both now understood:

1. **The deployed build predates the R:R fix.** It carries the newer TP1.5/TP2/TP3 and
   `ExitQtyPct` features but still runs `tp1MinRR = 0.8` and the ORB stop floor ON. That is
   the geometry the 2026-08-28 section already diagnosed, now confirmed in production data:
   a 3x wider stop paired with reward BELOW risk. Trades in that geometry rarely reach
   either level inside a session, which is *why* the exit alerts went quiet.
2. **The time exit that should have closed them was unreachable.** See below.

Resolving the 176 signals that fall inside the 5-minute bar window against actual prices —
counting the silent ones, and taking the stop when a bar spans both levels — the channel's
true record is **54.0% win rate, -0.004 R per trade gross, -0.057 R at 10 bps**. The
+0.747 R per trade the alert stream appears to show is survivorship: TP and SL announce
themselves, a trade that quietly drifts to the close does not.

### The ORB time-exit alert could never fire, and would not have parsed if it had (`orb.pine`)

The same defect `breakout.pine` was fixed for on 2026-08-27 was still live in `orb.pine`,
plus a second one on top:

- **It was gated on the wrong position.** `if isPastTimeExit and ... and strategy.position_size != 0`.
  `strategy.exit()` closes 100% at TP1 while the alert advertises `ExitQtyPct: 50` and the
  engine keeps holding half. The moment TP1 is touched the strategy is flat, so the residual
  position — the one that actually needs a time exit — can never be alerted on. Now gated on
  a dedicated `alertPosOpen` / `alertPosLong` pair, set by the entry alert and cleared only by
  an alert that exits the whole remainder (SL, TP1.5 at 100%, TP2, TP3) or by a new session.
- **The message was structurally unparseable.** `TIME EXIT | SYM` over `LONG | Entry: x` —
  the tag resolves to `TIME` and the compact line yields no `entry` field. Now
  `ORB EXIT | SYM` with `Reason:`, `Side:`, `Entry:`, `Exit:`, `SL:`, `TP:` each on their own
  line, matching `breakout.pine`.

Pinned by `tests/test_orb_alerts.py` — entry, TP (including that `ExitQtyPct` survives as
`exit_qty_pct`), SL, the new time exit, and a test that the old shape still fails.

### Which intraday level matters — measured, not asserted

208 NSE F&O names x 58 sessions of 5-minute bars, 229,697 level-touch events. For every
reference level, the first touch after 09:45 and what the next hour did.

**Every level has a negative break edge.** Continuation rate on first touch ranges 38.9%
(IBH) to 45.6% (day POC) — all below a coin flip — and median adverse excursion (1.5-1.9 ATR)
exceeds median favourable excursion (0.8-1.2 ATR) at every single level. On first contact
these are fade levels, not breakout levels.

Ranked by reaction size (median |1-hour move| in ATR): PMH 1.45, PWH 1.45, IBH 1.42,
PDH 1.40, ORH 1.38, PWL 1.36, PML 1.34, PVAH 1.33, PDL 1.32, PVAL 1.32, IBL 1.30,
DVAH 1.29, PPOC 1.27, ORM 1.24, ORL 1.24, IBM 1.20, DPOC 1.18, DVAL 1.17.

Two findings that contradict the usual playbook:

- **Confluence does not help.** Grouping by how many other levels sit within 0.25%:
  MFE/MAE is 0.56 / 0.58 / 0.54 / 0.57 for 0 / 1 / 2 / 3+ overlapping levels. Flat.
- **The retest is worse than the first touch.** Continuation 42.8% -> 39.5% -> 38.1% and
  MFE/MAE 0.57 -> 0.42 -> 0.39 across the 1st, 2nd and 3rd test of the same level.
  Repeated tests mean price is oscillating around the level, which is chop, not coiling.

### `backtest/strategies/dhb.py` — the TradeXPavan gainer-pullback setup

Adapter for `pinescripts/intraday/intraday-dhb.txt`. Result: a plausible but unproven edge.
Best honest configuration (top-5 cross-sectional gainers, 20-70% pullback of the leg, volume
gate, 2.5R target) gives 55 trades over 59 sessions at +0.202 R, consistent across both
windows — but the 95% bootstrap CI is [-0.076, +0.495] and dropping the three best trades
takes total R from +11.1 to +3.8. Ablation shows the pullback requirement and the volume
gate carry it; the confirmation candle is inert (identical results with it off) and the bare
"break the first day high" version loses money (-0.012 R). Not tradeable on this evidence.

### `support-resistance.pine` retuned (`pinescripts/intraday/orb/`)

LonesomeTheBlue's SRv2, ported to v6 and retuned for 5-minute NSE work: zone width is now
ATR-relative rather than 10% of a 300-bar range (which made zones 1.5-2.5% of price and
swallowed every level into one), strength is weighted by recency and volume, pivots inside
the opening gap are discarded, alerts fire on the zone EDGE and gate on `barstate.isconfirmed`,
and the nearest level is exported as a plot. `alertMode` defaults to Reject rather than Break,
on the level-study evidence above. Repaint status is documented in the header: values never
change once drawn, but the level SET updates `prd` bars late, so it must not be backtested naively.

---

## Recent Changes (2026-08-28)

### Every live and candidate PineScript now has a backtest adapter

`backtest/strategies/` gained four adapters, so the strategies that were reasoned about on
charts are now measured on bars: `orb` (the live ORB path, shared by `orb.pine` and
`breakout.pine`), `key_level` (breakout.pine's PD and IB families), `value_zone`
(dividend-growth) and `ib_extension`. Registered in `__main__.py`:

    uv run --group analysis python -m signal_engine.backtest orb --full

### `backtest/portfolio.py` — cross-sectional engine

`harness.Backtest` runs one symbol at a time and cannot express "buy the strongest 30 names
this month". `portfolio.py` holds a price panel instead. Every result is reported against an
EQUAL-WEIGHT basket of the same universe over the same dates, because a long-only book in
NSE large/mid caps 2016-2026 makes money from the market rising — `alpha_pa` is the only
column attributable to the ranking, `cagr` on its own means almost nothing.

### The R:R regression in `orb.pine` and `breakout.pine`

`calculateTargets` floored TP1 at `risk * 0.8`. A floor below 1.0 can only push reward
BELOW risk, so the break-even win rate had risen to 55.6% while the strategy realised 43.8%.
The floor bound on 60% of trades, so it was not a safety net — it was the strategy's actual
reward-to-risk setting. Compounding it, `calculateStopLoss` forced the stop past the ORB edge
while entry already sat `breakoutBuffer` beyond it, making `risk >= buffer + ORB width + wick`
on every trade.

Fixed: `tp1MinRR` input (default 1.5, `minval=1.0`), ORB stop floor gated behind
`useOrbStopFloor` (default off), `atrMultiplier` 2.0 -> 3.0. Median R:R 0.80 -> 1.56, median
stop 1.76% -> 0.72%, gross expectancy -8.38 -> +7.51 bps, improving in BOTH windows.

`orb.pine` also gained `volBaselineMode` (default Time-of-Day): a flat 50-bar volume SMA at
10:20 has a denominator made mostly of the prior session's dead closing bars, so the filter
was near-inert in the window ORB trades. `breakout.pine` already had this via `klVolFactor`.
Default ORB stage moved to ORB60.

### ORB is break-even at realistic position limits — do not scale it

The +7.51 bps figure is the average across all 619 signals, which is ~11 concurrent positions
and unreachable: at a 0.73% stop and 1% risk, ONE position is 1.37x capital. Simulating real
slot limits (take signals as they fire, release a slot when a trade exits):

| slots | trades | win | profit factor | Rs/month on 1L at 1% risk |
|---|---|---|---|---|
| 1 | 73 | 45.2% | 1.35 | +5,130 |
| 2 | 142 | 42.3% | 1.09 | +2,757 |
| 3 | 202 | 41.1% | 0.93 | -3,159 |
| 5 | 322 | 42.9% | 1.01 | +686 |

Profit factor swings 0.86-1.35 on how many slots you run, and IS/OOS carry opposite signs
(IS PF 0.84, OOS PF 1.08). That instability IS the finding: expectancy is indistinguishable
from zero. The live Q1-2026 record remains the better evidence that a small edge exists; it
is thin enough that execution quality decides the sign.

### Higher win rate at lower R:R does not work

Tested directly (`tp_mode="r"`, fixed R multiples, 619 signals):

| target | win rate | break-even WR needed | verdict |
|---|---|---|---|
| 1:0.75 | 55.1% | 57.1% | below water |
| 1:1.0 | 50.6% | 50.0% | dead even |
| 1:1.25 | 46.8% | 44.4% | marginal |
| 1:1.5 | 45.7% | 40.0% | positive |
| 1:2.0 | 42.6% | 33.3% | best gross |
| 1:2.5 | 41.5% | 28.6% | best net |

Win rate DOES rise as the target comes in — but never fast enough. Going 2.5 -> 0.75 buys
13.6 points of win rate and costs 28.5 points of break-even threshold. Nearer targets get hit
more often, not proportionally more often. The gradient points at HIGHER R:R, not lower.

### The live Q1-2026 performance numbers were overstated 2.1x

`analyze_orb.py` sums every exit EVENT. But `orb.pine` executes TP1 only, full position
(`strategy.exit(... limit=tp1)`), and its own comment calls TP1.5/TP2/TP3 "display-only".
Those observation alerts were being summed as if each were a separate booked trade.

| | doc method | one exit per trade |
|---|---|---|
| cumulative | +101.5% / 270 events | **+33.3% / 186 trades** |
| per trade | +0.376% | **+0.179%** |
| win rate | 67.3% | **62.7%** |

Still positive. At the median 0.647% live stop it nets +0.079%/trade at 10 bps and turns
negative around 19 bps. Every optimisation decision taken against the old number was taken
against an inflated one.

### Costs, measured for Flattrade (zero brokerage)

| line | rate | bps round trip |
|---|---|---|
| STT (sell side) | 0.025% | 2.50 |
| Stamp duty (buy side) | 0.003% | 3.00 |
| NSE txn + SEBI + IPFT + GST | — | 0.75 |
| **total** | | **6.25** |

A Rs 20/order broker adds ~4.7 bps on a Rs 1L position. Worth having, but 5.5 of the
remaining 6.25 bps is statutory and unavoidable — brokerage was never the binding cost.
Slippage sits on top and is the term that decides ORB.

### `momentum-rank.pine` — the one strategy with a surviving edge

12-month return skipping the last month, top N of a 40-name universe, monthly rebalance,
long-only CNC, no stop (a name is sold when it leaves the top N). Over 201 F&O names,
2016-2026, vs an equal-weight basket of the same universe: **alpha +12.7%/yr, t 3.12,
IS +12.2 / OOS +13.4**. Robust to lookback (180-400d), rebalance cadence (21-126d) and
portfolio size, and still +8.2% at 150 bps — seven times realistic cost.

Why it survives where ORB does not: 21% monthly turnover means costs are paid roughly once
a quarter per name instead of twice a day. Per position — 54% win rate, average win +61.5%,
average loss -9.5%, profit factor 7.75, median hold 62 days.

Verified against `portfolio.py` by replaying the Pine's control flow: 105 of 105 rebalances
selected identical baskets, signal dates identical, per-period returns matching to 0.0000%.
That check found one defect — `rebalDays` counted CHART bars, so on a 5-minute chart it
would rebalance every 21 five-minute bars. Now guarded to daily charts.

Honest limits: alpha is lumpy (median year ~+6%, mean pulled up by 2021/2023/2024), it fell
HARDER than the market in Mar 2020 (-29.4% vs -27.6%), max drawdown 32.5%, and the universe
is today's F&O list walked backwards so survivorship bias is present.

**Neither Pine file has been compiled on TradingView.** Paper first.

---

## Recent Changes (2026-08-26 → 2026-08-27)

### Backtest framework (`backtest/`)

`signal_engine/backtest/` is now the core for testing any PineScript strategy, so a script
is measured before it is wired to a broker. Strategy-agnostic: an adapter answers only
"would this bar produce an entry, and with what stop and target", and the engine owns
sessions, trade caps, next-bar fills, stop/target resolution, costs and bookkeeping.

Pine execution semantics are reproduced deliberately — signals evaluated on a closed bar and
filled at the NEXT bar's open, the STOP taken when one bar spans both levels, and a bar that
opens past the stop filled at the open. Each is pinned by a test in
`tests/test_backtest_engine.py`.

Three anti-overfit rules are in the API rather than left to discipline: `confirm()` always
prints in-sample / out-of-sample / full, `ablation()` labels every filter BOTH / IS only /
neither, and every row carries a t-statistic.

    uv run --group analysis python -m signal_engine.backtest ema9 --full

The universe is the NSE F&O list (`backtest/data.NSE_FNO`, 209 names) read from this
instance's own symbol master via `refresh_fno()` — not a hand-picked basket. That choice
turned out to decide the answer; see below.

### A NaN stop could destroy a setup silently (found by the framework)

`risk <= 0` and `risk / price < min_sl_pct` are BOTH False when `risk` is NaN. During
indicator warmup a rolling `swing_hi` is NaN, so an unguarded engine armed a pending order
with a NaN stop, let the strategy consume its setup, then dropped the fill because
`NaN > 0` is also False. Real signals vanished with no error anywhere. Guarded explicitly
with `np.isnan`; regression test pins it. The same class of bug is possible in Pine — use
`na()`, never a bare comparison.

### Time exit alerts were being dropped (`breakout.pine`)

The alert header was `TIME EXIT | SYM` over `LONG | Entry: 440.00`. `parser.py` matches
`^(\w+)\s*:\s*(.+)$`, so the compact direction+entry line yields no `entry` field, and
unlike TP HIT / SL HIT there is no rewrite rule to synthesise the mandatory fields — every
one of these parsed to `None` and never reached the engine. Now `BREAKOUT EXIT | SYM` with
`Reason:`, `Side:`, `Entry:`, `SL:` and `TP:` each on their own line; it reconciles the way
an SL HIT does. Pinned by `tests/test_breakout_alerts.py`, including a test that the old
shape still fails so it cannot return.

Note the second trap in the old header: `TIME EXIT | SYM` also parses as a strategy named
`TIME`, because the pipe regex reads the word before `EXIT` as the tag. Always lead with the
real strategy tag.

### EMA9 strategy added, tested, and marked not tradeable (`pinescripts/intraday/ema9/`)

Implements the three variants from a HowToTrade 9 EMA video plus the filters its 257
comments suggested. Registered as `EMA9` (`strategies.py`, `strategy_profiles`, blacklist)
and alert-tested end to end, so the pipeline wiring is proven.

The strategy itself is not tradeable. Over 208 F&O names and 59 sessions it measures
**-0.122R per trade across 6577 trades (t = -9.90)**, with a gross edge of -0.12 bps against
a 10 bps cost line — no edge even before costs. All three modes lose in both windows; no
target between 0.5R and 3.0R changes it. At a 2R target the exits split 55% time-exit, 34%
stop, 10% target: the signal fires constantly and goes nowhere. Kept as a reference
implementation of the alert contract and of a properly measured negative result.

**The universe lesson is the durable part.** An earlier run over 29 hand-picked liquid large
caps showed +8.71 bps gross and read as "borderline, paper trade it". Testing the real F&O
universe removed the edge entirely and reversed the ranking of the video's three modes.
Define the population from a rule before looking at results, or the basket picks the answer.

### Skills

- `.claude/skills/pinescript-strategy/` — the alert/Telegram/engine contract, strategy
  registration, backtest-before-live, and Pine v6 pitfalls, with `alert-template.pine` and
  `adapter-template.py` to copy from.
- `.claude/skills/strategy-from-video/` — turning a video or article into a tested strategy:
  transcript and comment extraction, converting vague rules into testable ones, choosing
  timeframe and universe on evidence.

---

## Recent Changes (2026-08-24 → 2026-08-25)

Driven by the first live day of `BREAKOUT` alerts (2026-08-24, 7 entries, 5W/2L). Source
export: `pinescripts/intraday/orb/result.json`.

### One exit alert per bar (`breakout.pine`)

TP1/TP1.5/TP2/TP3 each fired their own `alert()`. A single 5-min bar that spans several
levels therefore sent several competing exit instructions — RECLTD and VEDL both sent three
at once, and since TP1.5/TP2/TP3 all carry `ExitQtyPct: 100`, whichever reached the engine
first decided the realised price. Arrival order is not guaranteed: RECLTD's TP2 landed
before its TP1, worth 0.75R on that trade alone.

The four level checks now *stage* the level into `tpFireLevel`/`tpFirePrice`/`tpFireQtyPct`
and the bar emits one alert, for the highest level reached. Chart labels still update per
level. Deterministic, and it books the better price.

### Exit alerts stop once the position is flat (`breakout.pine`)

New `orbExitAlerted` latch, set by any exit alert covering the whole remaining position (a
TP at `ExitQtyPct` 100, an SL, or the time exit). Later exit alerts on the same trade are
suppressed — RECLTD sent TP3 at 13:35, three hours after TP2 closed it, which drove the
engine into `_recover_position_from_broker` for a position that no longer existed. Reset
on every new entry.

### Levelled exits are deduplicated (`validator.py`)

`_check_exit_shortpath` returned VALID before `_check_duplicate` ran, so repeated TP/SL HIT
alerts all reached `_handle_exit` and relied on the per-symbol lock and `exit_pending` flag
to absorb them. An exit carrying a `tp_level` now goes through dedup, keyed on
`(symbol, direction, tp_level)` — PineScript emits each level once per trade, so a repeat is
a delivery artefact. `signal.entry` is the synthesized 0.0 for exits, hence the level
standing in as discriminator. A **bare** EXIT (no level) stays un-deduplicated: a manual or
safety close must remain retryable after a failed attempt.

### Entry criteria travel with the signal (`breakout.pine`, `parser.py`, `models.py`, `db.py`)

The KEYLEVEL packet held the reasoning (Score, RVOL, VF, CLV, auction state, ADR used,
headroom, chase, confluence, day type) but is deliberately unparseable, so none of it
reached the trade log — attribution over a large sample had nothing to group by.

- `breakout.pine` latches the key-level context at **arm** time into `klSnap*` vars and
  replays it into the entry alert as single-word `Key: value` lines. Arm time is the
  decision point; the fill is one bar later. Latched rather than read live because the
  key-level engine sits ~2900 lines below the pending-entry processor that fires the alert,
  and Pine resolves in source order.
- `parser.py` sweeps every unconsumed `Key: value` line into `Signal.context`, skipping the
  two lines that match the shape by accident (the `09:50 IST` footer, the chart URL).
  Adding a field to the alert now needs no engine change.
- `db.py` gains `raw_message` and `context` columns, filled in on an existing `trades.db`
  via `PRAGMA table_info` + `ALTER TABLE` — no manual migration.

### `Ref T1` matches the executed target (`breakout.pine`)

`klCalcTargets` floors the target distance at 1R, but the KEYLEVEL packet printed the raw
structural T1 — FEDERALBNK showed 0.36R there while the entry alert that followed carried
1.0R. The packet now applies the same floor.

### Per-strategy `min_sl_pct` (`config.py`, `config.yaml`, `validator.py`)

All 7 entries on 2026-08-24 carried stops of 0.247%–0.342% against `risk.min_sl_pct: 0.005`,
so **every one validated as IGNORED**. The floor was calibrated for ORB geometry; key-level
stops sit `klSlBufferMult` (0.35) × ATR beyond the level, several times tighter.

`strategy_profiles.<TAG>.min_sl_pct` now overrides the global floor. Absent inherits the
global; `0.0` disables the check for that strategy alone. Set `BREAKOUT: 0.002`.

0.20% sits ~19% below the tightest observed stop and is still several ticks wide on a ₹300
stock. **Do not raise to 0.25%** — that rejects VEDL at 0.2465%, the day's best trade (+4.57R).
All 7 now validate VALID.

### Time exit 15:00 → 14:45 (`config.yaml`, `breakout.pine`)

Wider buffer before the 15:10 broker auto square-off, and it clears the last-half-hour spread
widening. The engine and the PineScript each run their own clock, so both were moved.

### KEYLEVEL packets off by default, own channel when enabled (`breakout.pine`)

`enableKeyLevelAlerts` (default **false**) gates the KEYLEVEL `alert()`, and
`telegram_chat_id_keylevel` (empty = reuse the main ID) addresses the packet to a channel the
engine does not listen on. The trade channel now carries BREAKOUT only; the packet's context
is no longer needed there because the entry alert carries it inline.

### `min_sl_pct` is a leverage cap (`config.yaml` docs, `breakout.pine`)

Challenged 2026-08-25: does a fixed 0.20% make sense across a ₹150–5000 band? Verified — it
does, because under `fixed_fractional` sizing the notional exposure is
`risk_per_trade / (sl_pct × (1 + slippage_factor))`, in which **entry price cancels out**.
Measured across ₹150/300/800/2000/5000: exposure flat to within `floor()` rounding.
0.20% = 4.55x, 0.50% = 1.82x. The config comment now carries the derivation and a table.

Tick granularity does not bind inside the band either — 0.20% of ₹150 is ₹0.30, six ticks.

What a price-relative floor genuinely cannot express is **stop tightness relative to that
stock's volatility** (0.20% is 0.4 ATR on a quiet name, 0.07 ATR on a volatile one). That is
already enforced upstream by `klSlBufferMult × ATR`, and is now recorded per trade as
`AtrPct` / `SlAtr` in the entry-alert context so an ATR-relative floor can be set from
evidence rather than guessed.

### RUNNER observations (`breakout.pine`)

Suppressing post-exit TP alerts removed the only record of how far a trade ran after the
policy closed it — the evidence needed to judge whether booking 100% by TP1.5 is too early.
A level reached while flat now emits a non-actionable `RUNNER | SYMBOL` note carrying the
unbooked R, routed to the observation channel. Header carries no LONG/SHORT/EXIT token, so
`_parse_header` returns None and the pipeline drops it (verified). Toggle
`enableRunnerObs`, default on.

### Compiled-token ceiling (`breakout.pine`)

Hit 100292 against TradingView's 100256 limit. Trimmed ~150 lines, all of it provably dead:

- `calculateTPSplits` (66 lines), `cleanupBox`, `buildTestAlert` — never called. The test-alert
  path builds its own JSON inline; `cleanupBox` was the only reader of `orbBoxes` /
  `MAX_BOXES_TO_KEEP`, so those went too.
- `alertEntryTriggered` / `alertSLTriggered` / `alertTP1Triggered` / `alertTP2Triggered` —
  write-only. Their only readers were the inert `alertcondition()` hooks removed earlier.
- `bodyClosedAbove` / `bodyClosedBelow` / `priceRetestFromAbove` / `priceRetestFromBelow`, and
  `getBodyHigh` / `getBodyLow` once those four went.
- `klFindT1`, reduced to a pass-through when `klT1` was rewired onto `klNextLevel` /
  `klPadTarget`.

The eleven raw level prices moved from eleven scalars to a parallel name/value array pair, so
the alert emits them in a loop instead of eleven near-identical statements.

### VWAP and 9-EMA are already entry inputs

Confirmed in `klComputeScore`: +1 when price is on the correct side of session VWAP, +1 when
price is on the correct side of the 9-EMA *and* the slope agrees — 2 of the 10-point score.
Not added again. On 2026-08-24 they did not discriminate: JSWENERGY scored both points
(`vwap+ ema+ slope+`) and was the day's worst loss.

### Compiler warnings, second pass (`breakout.pine`)

The `idxSessionHigh` / `idxSessionLow` "declared in local scope" pair was a **real bug**, not a
style nit. Both were declared inside `else if indexFilterMethod == "ORB Direction"`, so the
series each produced had holes on every bar that branch did not run — and the very next line
read `idxSessionHigh[1]`, straight into a hole. All three index readings, the NR-filter
lookup, `checkHTFBias()`, `calcSuperTrend()` and `ta.ema(close, 12)` are now resolved at
global scope; the branches only choose which already-computed value to publish. Security call
count is unchanged at 13 of the 40 available.

`calculateStopLoss` lifted out of both entry ternaries — pure function, so computing it
unconditionally costs nothing and only the selection stays conditional.

Shadowed dashboard locals renamed (`entry`→`dashEntry` … `isBullish`→`dashIsBullish`, and the
closed-market panel's `isDarkTheme`/`bgColor`/`txtColor`/`headerColor`→`closed*`). Done with a
string- and comment-aware replacer so tooltip text was untouched; 48 lines changed.

Still unfixed, deliberately: the `calc_on_every_tick` notices. Enabling it would change
backtest semantics, and `barstate.islast` is being used correctly here — see repainting below.

### Repainting audit

Verdict: **low risk, the design is sound.** Checked every `request.security` and every alert
path.

- `lookahead_on` appears once (PDH/PDL/ADR, daily) and is paired with `[1]`. That is the
  canonical NON-repainting idiom — `[1]` guarantees a closed day, `lookahead_on` only makes it
  readable from the session's first bar. Every other security call uses `lookahead_off`.
- `klHTFClose` uses `close[1]` **and** `lookahead_off` — doubly safe.
- Entries arm on a confirmed bar (`klFireGate` requires `barstate.isconfirmed`) and fill at the
  NEXT bar's `open`. Both the arming decision and the fill price are fixed before the bar they
  act on completes, so neither can repaint.
- KEYLEVEL packets use `alert.freq_once_per_bar_close`.

The one caveat worth knowing: TP/SL detection runs under
`(barstate.isconfirmed or barstate.islast)`, so it evaluates on the forming bar and fires the
moment price trades through a level. That is intentional for live trading — waiting for bar
close would delay every exit by up to five minutes — but it means **backtest results will not
match live alert behaviour**, because `strategy.*` only evaluates on confirmed bars. Chart
labels also move while a bar forms. This is a property of the design, not a defect.

### Day summary capital trajectory was wrong (`notifier.py`)

`send_day_summary` passes `RiskEngine._last_known_capital`, which with
`use_day_start_capital: true` is the **day-start** capital — `get_sizing_capital()` caches the
first funds-API fetch of the day and `calculate_quantity()` stamps that into
`_last_known_capital`. The header treated it as the closing balance and derived the opening as
`capital - net_pnl`, shifting **both ends** of the trajectory down by the day's P&L: a ₹15,000
day that made ₹500 printed `₹14,500 → ₹15,000` instead of `₹15,000 → ₹15,500`. The delta
looked right, which is why it went unnoticed.

Verified correct and left alone: the return percentage (already measured against opening
capital) and the win rate (time exits deliberately excluded from W/L but shown as `T:`).

### Startup failures now alert; auth cooldown bounded; readiness probed

A broker-auth failure was silent and open-ended. On 2026-08-24 a single failure at 09:03 wrote a flat 24h cooldown that then blocked 80 start attempts through 15:27 -- the whole trading day -- with nothing sent anywhere, because `_run_startup()` called `sys.exit(1)` before reaching its notification step.

- `notify_failure()` alerts Telegram on every `_run_startup` failure path (configuration, auto-login, broker-auth) and never raises, so a dead notifier cannot mask the underlying error. New `openalgoscheduler notify` CLI lets `openalgoctl.sh` alert without duplicating Telegram wiring.
- Auth cooldown escalates 5m/15m/1h/3h (capped, counter resets on success) instead of a flat 24h. Worst case now stays inside one trading session.
- The supervisor loop re-probes the health URL every 60s and declares `app.py` wedged after 3 consecutive failures. `kill -0` only proved the PID existed, so an alive-but-unresponsive server read as healthy indefinitely.

**Files**: `scripts/openalgoscheduler.py`, `scripts/openalgoctl.sh`, `tests/test_openalgoscheduler.py`, `tests/test_openalgoctl.sh` (new).

### Initiative Drive Detector v6 -- rewrite

The script had never compiled. Three separate faults: a corrupted `ta.atr(` call, `label.style_labelup` / `label.style_labeldown` (the v6 constants carry underscores), and `location=` passed to `label.new`, which has no such parameter -- labels position with `yloc`.

Detection was also too loose to mean "initiative". Fixes, in order of impact:

- **Breakout is measured on the close, not the intrabar extreme.** `high > highest(high[1], N)` is satisfied by an upthrust that pokes through a level and gets sold back -- the textbook responsive/absorption bar this indicator exists to filter out. `close > highest(high[1], N)` requires the auction to hold the new territory into the close.
- **Direction is a prerequisite, not a score component.** Direction-neutral criteria were free points for both sides, so an upthrust inside an uptrend could score 6/8 as a BULL signal.
- **ADX regime gate, made directional.** An expansion bar inside balance is rotation, not initiative. ADX alone measures strength only, so it was another free point for both sides; +DI/-DI supplies direction. Neutral criteria are now down to two (ATR expansion, RVOL).
- **Dropped `range > avgRange`.** It measured the same property as `range >= ATR x mult`, so volatility scored twice while participation and structure scored once each.
- Body is direction-aware; bull and bear can no longer fire on the same bar; opening N bars are skipped (the auction volume spike inflates RVOL, so nearly every open looked like an initiative drive); warm-up guard; cooldown `>` corrected to `>=`; stopped shadowing the built-in `vwap`; `nz()` around alert interpolations so DMI warm-up cannot emit `NaN` into the JSON.

Marking is now unobtrusive: tiny `plotshape` triangles off the bar plus a faint background tint. Candle recolouring and score labels are **off** by default -- recolouring overwrites the real candle colour, which is still needed when reading the bar against the footprint.

**Files**: `pinescripts/intraday/orderflow/initiative_drive_detector_v6.pine`.

### Key-level candle verdicts, and two Pine compile faults

`plotshape`'s `size` argument is a const string, so the `input.string` marker-size control was rejected outright: *Cannot call "plotshape" with argument "size"*. Sizes driven by an input have to be drawn with `label.new`, whose properties accept series values. Both orderflow scripts hit this.

`keylevel-candles.pine` was firing almost nothing but FAIL on a live TCS 5-min chart. Two causes:

- **Proximity was the wrong trigger.** "Within 0.25 x ATR of the nearest level" is true on nearly every bar once eleven levels are on the chart. The bar now has to actually trade *through* a level, and of the levels it pierced, the one its close settles nearest is the one under test.
- **The level reference was unstable.** `close[1]` was compared against whichever level was nearest on *this* bar, frequently a different level from the one the previous bar closed against. Every level in the table is fixed once formed, so the comparison is now made against that one level.

Also: OR/IB levels were live during their own formation window, so every bar inside the first 15 / 60 minutes "touched" them. They are now published only once the window closes.

The output is one of three verdicts rather than five pattern names, each carrying a tooltip with the candle type, level, body/wick geometry, volume and the reading -- `BREAK` (conviction close through, on volume, no wick into the level), `FAKE` (rejection wick, or a close back inside after the previous bar closed beyond), `WEAK` (inconclusive, off by default).

`initiative_drive_detector_v6.pine` markers are now small green/red `ID` labels instead of cyan/amber diamonds.

**Files**: `pinescripts/intraday/orderflow/keylevel-candles.pine`, `pinescripts/intraday/orderflow/initiative_drive_detector_v6.pine`, `pinescripts/README.md`.

### Initiative drive: expansion is a gate, not a score point

Live TCS 5-min was marking visibly small candles as drives. Cause: expansion was one of eight score criteria firing at 5/8, and `bodyPct` is measured against the bar's OWN range -- a doji-sized bar with a 90% body passed it. A small candle could reach 5/8 on rvol + body% + close + ema + vwap without ever being an expansion candle, which is the opposite of what "initiative drive" means.

Restructured into gates and ranking. The four properties that *define* a drive are now hard gates, all required:

1. **Body expansion** -- `body >= median(body, N) x 1.5`. The absolute-size test, and the one that actually stops small candles qualifying.
2. **Range expansion** -- `range >= median(range, N) x 1.15`.
3. **Directional body** -- closes in the signal direction, body a majority of range.
4. **Close at the extreme** -- little or no wick against the direction.

The remaining five (RVOL, close beyond the N-bar level, EMA, VWAP, ADX with +DI/-DI) only rank context and can no longer create a signal. Score floor is 3/5, applied after the gates. Since entries are mapped off these candles, a drive must be a drive before context is even consulted.

### Key-level marks: one horizontal row, plain-English tooltips

Marks were scattered across the price pane at each event's own price and the tooltips read like a manual. All marks now sit on a single row anchored below the session low (`rowY`, offset in ATRs, only ever pushed lower), with short text (`BRK U` / `FAKE D`) and a three-line tooltip: what the level was, what the candle did, what it means. No jargon.

**Files**: `pinescripts/intraday/orderflow/initiative_drive_detector_v6.pine`, `pinescripts/intraday/orderflow/keylevel-candles.pine`.

### Key-level marks: shorter, and level coverage made visible

Marks read `BRK U` / `FAKE D`, which collided with each other whenever two events landed on adjacent bars. Now two characters (`B▲` `B▼` `F▲` `F▼` `W`), tiny by default.

Separately: on a live chart only PDL and ORL appeared to produce verdicts. The selection loop tests all eleven levels identically, so that was not a bias — TCS on 2026-08-25 traded 2262-2313 and spent the session between PDL 2279.5 and ORL 2268.9, while PDH 2321.6 was never reached. But it exposed a genuine trap: **VAH/POC/VAL are off by default and default to 0.0**, so they are silently never tested, which on the chart is indistinguishable from "the script does not handle those levels".

Added a coverage table (bottom-right): every level in play, its price, and today's BREAK / FAKE counts, with an explicit `VAH/POC/VAL — off` row when the volume-profile levels are disabled. A level producing nothing is now visibly distinct from a level that was never armed.

**Files**: `pinescripts/intraday/orderflow/keylevel-candles.pine`.

### No unlabelled key-level lines

`klDrawLevel` dropped the TAG for any level further than `klTagMaxATR` (6) x ATR from close but kept its LINE, so the chart carried horizontal lines the trader could not name. A line you cannot identify is worse than no line — it reads as a level without saying which.

Suppression is now symmetric: a distant level whose line we draw ourselves is dropped entirely instead of being left anonymous. Levels whose line comes from elsewhere (`drawLine=false`, the ORB plots) are always tagged, because that line cannot be removed and must not be orphaned.

`keylevel-candles.pine` was compounding this — it re-plotted ORH/ORL/IBH/IBL/PDH/PDL with `plot()`, which carries no on-chart tag, duplicating lines breakout.pine already draws AND labels. `Plot Levels` now defaults off.

Also dampened its FAKE spam: `VAL` alone produced 15 verdicts in one session because a close one tick beyond a level counted as a break, making every oscillation a failed break. Added an acceptance margin (prior close must clear the level by 0.15 x ATR) and a per-level cooldown (3 bars).

**Files**: `pinescripts/intraday/orb/breakout.pine`, `pinescripts/intraday/orderflow/keylevel-candles.pine`.

### Every level drawn and tagged; previous-day VP levels get a P prefix

Making tag suppression symmetric fixed anonymous lines but created a worse problem: PDH, 33 points from close on a mid-range day, was hidden entirely — and PDH is exactly the level a trend day runs at. `klTagMaxATR` is now 0 (no distance limit): every level is drawn AND tagged, and the existing collision stagger keeps stacked tags readable.

`VAH/POC/VAL` renamed to `PVAH/PPOC/PVAL`. These are the PREVIOUS session's value area, but TradingView's Session Volume Profile plots the CURRENT day's VAH/POC/VAL — two sets of lines carrying the same three names at different prices, both on the chart at once. The P prefix also makes the naming self-consistent: `PVAH/PPOC/PVAL/PDH/PDL` are previous-session, bare `ORH/ORM/ORL/IBH/IBM/IBL` are today's. Display only — `klLvlNames` feeds a human-readable KEYLEVEL packet that goes to a channel the signal engine does not listen on, and `parser.py` never reads level names, so nothing in the trade pipeline is affected.

`keylevel-candles.pine`: acceptance margin and per-level cooldown both back to 0, so every candle interacting with a level is judged again — a run of F-down marks along one level *is* the conviction signal, and thinning it out removed the evidence. Each of the four verdicts now has its own colour (B up green, B down red, F up amber, F down blue) because at tiny label size the arrow alone was not readable.

**Files**: `pinescripts/intraday/orb/breakout.pine`, `pinescripts/intraday/orderflow/keylevel-candles.pine`.

### Every level interaction gets a verdict

Marks thinned out because quality was GATING the verdict: an interacting candle without conviction produced no mark at all, so the level test simply vanished from the chart. Quality is now an attribute, not an entry condition.

The taxonomy is positional and therefore exhaustive — where the previous close sat versus where this one closed:

| from | to | verdict | reading |
| --- | --- | --- | --- |
| below | above | `B▲` | broke up |
| above | below | `B▼` | broke down |
| above | above | `F▼` | dipped below intrabar, level held as support |
| below | below | `F▲` | poked above intrabar, level held as resistance |

The last two necessarily involve an intrabar probe, since the bar had to pierce the level to be considered at all. Conviction (volume, body, wick) now shows as label opacity and is named in the tooltip; it never suppresses a mark. `WEAK` is gone — with an exhaustive taxonomy there is nothing left for it to catch.

Note the other half of the drop was configuration, not code: the 01:46 chart had the volume-profile levels armed (VAL 2272.8 sat in the middle of the day's range and produced 15 marks by itself), and the 02:18 chart had them off. The coverage table's `PVAH/PPOC/PVAL — off` row is there to make exactly that visible.

Default label size raised from tiny to normal.

**Files**: `pinescripts/intraday/orderflow/keylevel-candles.pine`.

### Cluster fan-out for key-level marks

With every interaction now judged, a level being ground along puts a verdict on consecutive bars — `PDL 2279.5` produced 18 in one session — and at one fixed y they overlapped into an unreadable clump. Label size is back to tiny, and marks landing within two bars of the previous one step down through four slots; isolated marks stay on the line, clusters fan out.

The first attempt put slot resolution in a helper function, which would not have compiled: **Pine rejects assignment to a global variable from inside a user function.** It is resolved at global scope instead, which is legal, and the verdicts being mutually exclusive means one slot per bar is sufficient. A scan of both orderflow scripts confirms no other function assigns to a global.

**Files**: `pinescripts/intraday/orderflow/keylevel-candles.pine`.

### pinescripts/ structure

- Added `pinescripts/README.md` -- there was no map of the 10 `.pine` files, their Pine titles, or which are live vs third-party reference.
- Removed three vestigial `__init__.py` files. Nothing imports `signal_engine.pinescripts`, and `trade-analysis` cannot be a module anyway (hyphen).
- `STRATEGY-ANALYSIS.md` is now the single spelling; it previously existed in three casings, which made it ungreppable.
- Performance reports consolidated under `orb/trade-analysis/`.
- Strategy-Tester exports (`result.json`) and `charts/` are gitignored -- regenerated per run, not source.

**Files**: `pinescripts/README.md` (new), `.gitignore`, `PRD.md`.

### Open

- `accountSize` in `breakout.pine` is still the 10000 template default; the engine sizes from
  the live funds API, so this only skews the `Ref Qty` shown in the KEYLEVEL packet.
- RVOL ≥ 1.5 as an entry gate is unvalidated beyond n=7. Test against the Q1 export first.

---

## Recent Changes (2026-08-22, config snapshot)

<!-- AUTO-GENERATED: breakout.pine input defaults. Regenerate from source, do not hand-edit. -->
### `breakout.pine` — current configuration

| Setting | Value | Setting | Value |
|---|---|---|---|
| `enableKeyLevels` | true | `enableBreakout` (ORB entries) | **false** |
| `enableKeyLevelExecution` | **true** | `enableVATriggers` | true |
| `enableVAAcceptance` | **true** | `enablePDTriggers` / `enableIBTriggers` | true / true |
| `klScoreThreshold` | 7 | `klRequireHTFClose` / `klConfirmTF` | true / 15 |
| `klMinSessionVF` | 0.8 | `klMinHeadroomR` | 1.5 |
| `klMaxChaseATR` | 1.0 | `klBreakMinRvol` | 1.5 |
| `klAccMinRvol` | 1.5 | `klBlockCounterBias` | true |
| `klSlBufferMult` | 0.35 | `klT1PadMult` | 0.15 |
| `enableAfternoonWindow` | **false** | PM window (if enabled) | 13:00–14:30, `pmMinSessionVF` 0.8 |
| Entry window | 09:45 – **11:45** | Time exit | 15:00 |
| `volBaseDays` | 14 | `volumeMultiplier` / `strong` | 1.2 / 1.8 |
| `riskPct` | 1.0 | `accountSize` | **10000 — still the template default** |
<!-- END AUTO-GENERATED -->

### Corrections to the entries below

Statements in the older sections of this document that are **no longer true**. Left in place as
history; superseded here:

| Older claim | Current truth |
|---|---|
| "`%IB` (vs average IB) types the day" | **`%ADR` types the day**, with the model doc's <35 / >60 bands. IB-vs-average-IB is the second reading |
| "Afternoon window 13:45–14:15, **ON**, requires VF ≥ Min Volume ×" | **13:00–14:30 and OFF by default**, gated by `pmMinSessionVF` 0.8 |
| "`enableVAAcceptance` default **false**" | **true**, gated instead by `klAccMinRvol` 1.5 — the model doc makes acceptance four of its eight scenarios |
| "`canTakeKeyLevelEntry` fixes the ORB-width gate" | **Was incomplete.** Only arming used it; fill and cancel still used `canTakeEntry`. Fixed with `canFillLong`/`canFillShort` |
| "NOT compiled since ~15 structural changes" | **Compiles and runs.** Verified on TCS, FEDERALBNK, ABCAPITAL charts |

### What changed since the entries below

**Bugs fixed** (each was live behaviour, not cosmetic):

- **Key-level longs rendered as SHORT.** `isBullish` read `everHadBreakUp`, an ORB-only flag that
  never sets now ORB is demoted. Execution was unaffected (`orbTradeDirection` drives TP/SL).
- **Stop stayed armed after a target was booked**, firing spurious `SL HIT` alerts into the
  pipeline. Gated on `anyTPBooked`. The close-reason chain also reported a booked-then-stopped
  trade as a flat −1R.
- **Afternoon window could never open** — two causes: a per-bar volume threshold applied to a
  cumulative session measure, and both windows sharing one entry slot.
- **ORB width still killed key-level fills** (above).
- **HTF gate made `-BRK` setups unfireable.** A break triggers on the first bar closing beyond a
  level, while the gate demanded a closed HTF bar *already* beyond it — near mutually exclusive.
  Breaks now qualify on break-bar volume instead.
- **Alerts fired that could not trade**, putting a SHORT packet in Telegram while a LONG was open.

**New gates:** session participation floor (`klMinSessionVF`), headroom (`klMinHeadroomR`),
chase limit (`klMaxChaseATR`), counter-bias veto on trend days, break-bar volume.

**Other:** renamed `intraday-orb` → `intraday-breakout`; all three alert types carry the
`BREAKOUT` tag (the SL alert previously carried none and fell back to `ORB`); ORB demoted from
trigger to level; day/open type and bias rows added, open type now carries direction.

### Validation status — read before enabling live

- **Zero Strategy Tester runs.** Every change is argued from first principles or read off charts.
- **~6 trades observed** across 12 charts. Not statistically meaningful.
- **Eight interacting gates, never validated in combination.** Two destructive interactions were
  found and fixed in one session; there is no basis for assuming a third does not exist.
- **Signal count unknown.** One trade/day/symbol, morning only, through eight filters.
- **`signal_engine` config for the `BREAKOUT` tag does not exist.**

---

## Recent Changes (2026-08-22)

### Key-level execution enabled (`breakout.pine`)

- **Blocker 2 fixed.** Key-level entries were executed with ORB-derived SL and ORB-width targets
  while the alert reported level-based `Ref SL`/`Ref T1`. `klSLLevel`/`klT1` are now latched into
  `klArmedSL`/`klArmedT1` at arming; the pending processor branches on `klPendingSource != ""` and
  uses `klCalcTargets()` (same 1/1.5/2/3x shape, anchored on the next structural level, 1.5R
  fallback). Executed geometry now matches the signal.
- **IB% carries both denominators** — `%IB` (vs average IB, Market Profile bands) types the day;
  `%ADR` says how much of a normal day's range the first hour consumed. Daily ATR was indeed
  wrong: true range folds the overnight gap in and makes the IB read falsely narrow on gap days.
- **Afternoon window** `enableAfternoonWindow` 13:45–14:15, ON. Requires session volume factor ≥
  Min Volume × (the morning window does not) — the PM wave is volume-driven. Ends 14:15 so an
  entry still has 45 min to the 15:00 time exit.
- **`enableKeyLevelExecution` now true.** `enableVAAcceptance` new, default false — suppresses the
  weakest family (VAH-ACC/VAL-ACC).
- **Entry stays breakout, not retest.** `enableRetestEntry` (wait-for-pullback) remains off —
  already rejected for adverse selection. Retest *quality* comes from `-RT` setups, which detect a
  completed break-and-retest and enter on the confirming bar without waiting.

**⚠ NOT compiled since ~15 structural changes.** Before live use: compile clean; Strategy Tester
with execution on, verifying key-level SL/TP match the alert; confirm `parser.py` handles a
key-level entry alert (never fired before); observe the PM window opening/closing correctly.

Inputs 60 → 66.

---

### Key-level execution blockers + headroom gate (`breakout.pine`)

- **Fixed:** `canTakeEntry` folded in `orbRangeFilterPassed`, so IB/VA setups were rejected on
  ORB width. Split out `canTakeKeyLevelEntry` (cutoff + slot + gap + minTime + NR, no ORB width).
- **⚠ NOT fixed — must be before `enableKeyLevelExecution = true`:** a key-level entry hands off
  to the shared pending processor, which derives SL from **ORB levels** and TP from **ORB width**.
  The observation alert reports level-based `Ref SL`/`Ref T1` from `klExecMap()`. **The executed
  trade would not match the alert.** Fix: latch `klSLLevel`/`klT1` at arming and prefer them when
  `klPendingSource != ""`.
- **Headroom gate** `klMinHeadroomR` (1.0R default): projects the day's extension to a full ADR,
  measures entry-to-there in units of the trade's own risk, refuses setups with no room. Verdict
  shows `⛔ NO ROOM`. Objective form of "don't buy a breakout after the move is done".
- **IB% denominator corrected** — was `IB / daily ATR` (no literature threshold), now
  `IB / average IB` over 14 sessions with Market Profile bands (<80% narrow, >120% wide).
- **Entry cutoff 11:00 -> 11:45.** IB triggers cannot arm before 10:15 and the HTF gate adds up
  to 15 min, leaving the primary family ~30 usable minutes. 11:45 gives 90 and stops where the
  cheat sheet puts the lunch trap.

**Static vs live:** IB% is **static after 10:15** (day-type classifier). ADR is static; `% used`
is live and only rises. `Room (R)` is live and directional — the metric that answers "steam left
for this trade".

Net session token change **-1,790 proxy (~-7,770 compiled)**; est. ~89,500 / 100,256.

---

### Input diet + verdict dashboard (`breakout.pine`)

Reframed around the fact that signals reach the broker via webhook -> signal_engine -> trade
bridge. The panel is not a decision aid for a human clicking buy; it reports what the system
is doing and why.

- **Inputs 141 -> 59.** Deleted 13 dead (FVG subsystem — its filter was never called; pullback
  filter; currency conversion, which also freed a `request.security` slot, now 13 total).
  Froze 67 to constants (colours, display toggles, legacy retest machinery, position-sizing
  config since signal_engine owns sizing, ADX, HTF/index/trend sub-params, VP resolution).
  Freezing keeps value and consumers identical, so behaviour is provably unchanged.
- **`renderVerdict()`** — one line plus one reason at the top: `⛔ NO TRADE` (naming the first
  failing filter) / `⏰ TOO EARLY` / `🏁 CUTOFF` / `🏁 DONE` / `🟡 ARMED` / `⏳ WAITING` (HTF) /
  `🟢🔴 SIGNAL` / `✅ IN TRADE`.
- **`dashMode`** Focus (default) / Full. Focus = verdict + Vol Factor, Auction, IB, ADR, Setup,
  KL Mode. Confluence folded into Setup as `+2lvl`.
- **Signal tooltip rebuilt** with labelled columns, plain language, and volume (session factor,
  break-bar ratio, bar close quality). Chart text beside the triangle carries the factor too.

Net token change **-2,201 proxy (~-9,550 compiled)** — the file is now well clear of the ceiling.

**Known defect, not yet fixed:** `canTakeEntry` includes `orbRangeFilterPassed`, so an IB or VA
setup can be rejected because the *Opening Range* width was out of band. Latent while
`enableKeyLevelExecution = false`; **must be fixed before execution is enabled.**

**Files**: `pinescripts/intraday/orb/breakout.pine`, `breakout.md`, `PRD.md`. `orb.pine` untouched.

---

### Volume Factor replaces the rolling volume MA (`breakout.pine`)

`volumeMA = ta.sma(volume, 50)` was **time-of-day blind** — on a 5-min chart the denominator
at 09:20 was mostly the previous session's dead close bars while the numerator ran 5-10x
normal, making the 1.2x test near-inert in the window this strategy trades most. Replaced by
`klVolFactor()`, which compares each bar against **the same slot of the session on prior days**.

| Read | Question | Consumer |
|---|---|---|
| per-slot mean bar volume | Does this break carry participation? | `volBaseline` — every existing `volume / volBaseline` ratio is now normalised |
| cumulative session factor | Is this stock in play today? | New `Vol Factor` dashboard row, +1 score component, alert |

At slot 0 this is the Zarattini/Barbon/Aziz (SFI 2024) relative-volume measure, which that
study found did almost all the work in ORB selection across 7,000+ US stocks.

**Removed**: `volumeMaLength` input, the `ta.sma` cache, the 3-bar `max()` numerator (let a
spike two bars *before* a weak breaking candle pass the filter), and the **GOD MODE quality
score** (`godScore`/`godGrade`/`ORB Quality` row) — grep-verified to appear only in label text
and table cells, never in a breakout or entry condition. Its removal funded the volume engine:
net session cost **+15 proxy ≈ +65 compiled**. GOD MODE's adaptive buffer, chop guard and
both-pending fix are kept.

`volumeMA` → `volBaseline` (renamed, not silently redefined — it is no longer a moving average).

**⚠ Two consequences:**
1. **The `orb.pine` regression gate no longer holds — deliberately.** `volBaseline` feeds
   `volumeOK` in ORB breakout detection, so ORB trade selection changes. Strategy Tester will
   differ from `orb.pine`; that is now expected.
2. **`volumeMultiplier` / `strongVolumeMultiplier` (1.2 / 1.8) are no longer calibrated** — they
   were tuned against an inflated-at-open denominator. Normalised, morning ratios read lower, so
   the filter is now **stricter in the morning**. Expect fewer signals initially. Observe before
   re-tuning; change one at a time.

**Files**: `pinescripts/intraday/orb/breakout.pine`, `breakout.md`, `PRD.md`. `orb.pine` untouched.

---

### Key-level signal quality batch (`breakout.pine`)

Three changes to key-level signal generation. **No Python changes** — key levels remain
`enableKeyLevelExecution = false` (alert-only), and the decision packet stays deliberately
unparseable by `parser.py` (`Ref Entry` / `Ref SL` / `Ref T1`).

| Change | Effect |
|---|---|
| **Removed `deltaProxy` from `klComputeScore()`** | It was derived from CLV, which was already scored — one strong close collected +2 of the 7-point threshold for a single input. Max score 15 -> 14 |
| **`klT1PadMult` (0.15 ATR)** | T1 now sits in front of the next structural level instead of on it, where the crowd's resting limits sit. Reports `-` when the pad leaves no workable target |
| **`klRequireHTFClose` + `klConfirmTF` (15m, default ON)** | A setup needs a *closed* HTF bar on the correct side of the level. Direction-uniform, so rejections are not inverted. Non-repainting (`close[1]` + `lookahead_off`); costs up to one HTF bar of lag. RVOL >= `strongVolumeMultiplier` bypasses the wait |

Dashboard `Setup:` row shows `⏳HTF` while a scoring setup waits on confirmation.

**Threshold caution:** combined with the earlier confluence fix, scores now read up to 4
points lower than the build that chose `klScoreThreshold = 7`. Observe alert rate before
re-tuning.

**Evidence base** (outside sources, not the legacy ORB log — that measures a different
strategy): Zarattini/Barbon/Aziz (SFI 2024) found plain ORB weak across 7,000+ US stocks
2016-2023, with **opening relative volume doing almost all the work** — motivating the
Volume Factor work still outstanding, since `volume / sma(volume, 50)` is not time-of-day
normalised. Breakout literature supports close-beyond-level over wick, and higher-timeframe
confirmation over lower. SMC order blocks (~50-55% raw WR, high discretion) were reviewed
and deliberately not ported.

**Files**: `pinescripts/intraday/orb/breakout.pine`, `pinescripts/intraday/orb/breakout.md`,
`PRD.md`. `orb.pine` untouched. Validation is TradingView compile + Strategy-Tester diff.
**Token cost**: +201 proxy ≈ +872 compiled.

---

## Recent Changes (2026-08-20)

### Key-level breakout strategy (`breakout.pine`, new)

New PineScript alongside `orb.pine` — a copy of it, extended so the Opening Range becomes
one key level among several. Merges the volume-profile decision-assist logic
(`pinescripts/intraday/volume-profile/volume-profile-decision-assist.pine`) into the ORB
strategy, since ORB, Value Area, Previous Day and Initial Balance breaks are all the same
key-level breakout with shared entry mechanics.

Added trigger families, each toggleable and all running through the existing
entry/SL/TP/alert pipeline:
- **VA** — VAH/VAL rejection, acceptance, breakout-retest (previous-session volume profile,
  auto-reconstructed from 1-min data or typed in manually)
- **PD** — PDH/PDL break and break-retest
- **IB** — IBH/IBL extension and extension-retest (09:15–10:15 window, active after IB closes)

Plus a unified confluence registry spanning VA + PD + IB **and** today's ORB levels, a
weighted setup score with alert threshold, level-based SL/T1, day-anchored level drawings,
and a 9-row dashboard block.

**Safety posture — alert-only.** `enableKeyLevelExecution` defaults to **false**: key-level
setups draw arrows, score, and fire a decision-packet alert, but never call `strategy.entry`.
The one-trade-per-session cap is unchanged, and ORB outranks every new family for that slot.

**Validation contract**: only 8 lines of `orb.pine` were modified (a `srcTag` parameter on
`buildEntryAlert`, its two call sites, and the dashboard table grown 36→48 rows) — all inert
at default settings. With `enableKeyLevelExecution=false` the Strategy Tester report must
match `orb.pine` exactly. TradingView compile + Strategy-Tester diff + POC-vs-native-profile
check on 3+ symbols are **still outstanding**.

**Engine impact**: none yet. The decision packet deliberately avoids the `Symbol:`/`Entry:`/
`SL:`/`TP:` line prefixes `parser.py` keys off (it uses `Ref Entry`/`Ref SL`/`Ref T1`), so it
cannot be mis-parsed into an order. Executed key-level entries reuse the unchanged
`buildEntryAlert` format, and the new `Trigger:` line is ignored by the parser.

**Files**: `pinescripts/intraday/orb/breakout.pine` (new), `pinescripts/intraday/orb/breakout.md`
(new — full changelog), `PRD.md`. `orb.pine` untouched. Pine has no unit framework —
validation is Strategy-Tester diff.

---

## Recent Changes (2026-05-12)

### ORB PineScript TP fix (`calculateTargets()` rewrite)

The April 30 "pure 1R" fix overcorrected: with ATR×2.0 as default SL, TP1 was set 2–3× the ORB width — unreachable intraday (e.g. ₹8 TP on a ₹2 ORB). Q1 used `min(orbTP, riskTP)` which had the opposite problem (R:R collapse when SL floor expanded past orbWidth). New formula resolves both:

```pine
float tp1_dist = math.max(orbWidth, risk * 0.8)
```

Primary anchor is the ORB measured move (`orbWidth` = actH − actL). Floor at 0.8R prevents R:R collapse on wide-SL days. Removed `riskAdjustment` multiplier (was 1.0/0.8/0.6 by price tier — always 1.0 with max_entry_price=800, dead code).

### Exposure reduction: `max_open_positions` 7→4 / `max_trades_per_day` 16→10

Q1 data: signals 1–2 have 90% WR, signals 3+ drop to 57% WR. On chop days (May 5, 7, 12) all 7 slots filled with correlated losers — more positions amplified losses without edge. 4 slots capture the best morning breakouts while limiting exposure when market isn't following through. `max_trades_per_day` proportionally reduced (4 concurrent + 6 recycles).

### Loss-cut gate (no-progress sub-gate)

New exit gate fires before the 90-min main gate when progress drops past a deep negative threshold:
- Config: `loss_cut_enabled: true`, `loss_cut_min_age_minutes: 20`, `loss_cut_progress_threshold: -0.80`
- At progress < −80% (price has moved 80% of the way to SL against the trade), position is market-exited immediately — no waiting for the 90-min check or broker SL
- `min_age_minutes: 20` skips opening-candle noise
- Trigger: BHEL 2026-05-12 (−121% progress at 90min; loss-cut would have exited much earlier)

**Files**: `config.py` (+3 settings), `config.yaml`, `tracker.py` (new gate in `_check_no_progress`)

### `day_start_capital` persisted in RiskStore (engine-restart safe)

Previously, a signal-engine restart mid-day (e.g. crash/watchdog) lost the cached day-start capital — the first post-restart trade re-fetched live capital, which was lower (open positions consuming margin) — producing inconsistently smaller qty for the same risk %. 

**Fix**: `RiskStore.save()` / `load()` now include `day_start_capital` column. `RiskEngine._restore_state()` restores the cached value directly from DB and logs it on startup. Schema migration adds the column via `ALTER TABLE ... IF NOT EXISTS` guard.

**Files**: `risk_store.py`, `risk.py`

### `early_check_enabled` disabled (2026-05-07)

The early gate (45min / 5%) was force-exiting trades at small adverse drift before the broker SL had a chance to fire — adding MARKET-order slippage on top of an unrealised loss. On 2026-05-07, 4/4 trades hit the early gate; TATASTEEL slipped −1.5R past SL on a market-exit at −8%. Disabled pending rework. Main gate (90min / 20%) and loss-cut gate remain active.

### Config state as of 2026-05-12

| Key | Value | Change |
|-----|-------|--------|
| `risk_per_trade` | 0.01 | Reverted from 0.015 (capital at ₹35K now) |
| `max_open_positions` | 4 | Was 7 |
| `max_trades_per_day` | 10 | Was 16 |
| `min_rr` | 0.75 | Clarified: safety net only; ORB PineScript guarantees ≥0.8R |
| `early_check_enabled` | false | Disabled 2026-05-07 |
| `chop_tightener_enabled` | false | Disabled (redundant with max_open_positions=4) |
| `loss_cut_enabled` | true | New |
| Blacklist ORB hard | BHEL only | HUDCO / NATIONALUM removed (position closed) |

**Files**: `orb.pine`, `config.yaml`, `config.py`, `risk.py`, `risk_store.py`, `tracker.py`, `tests/test_risk.py`, `tests/test_tracker.py`, `tests/test_validator.py`.

---

## Recent Changes (2026-04-29)

### Bug Fix: Fill-above-TP auto-close (main.py)

MARKET entry orders on fast ORB breakouts can fill past the TP price due to execution lag (Telegram delivery + API round-trip, typically 2–5s). When the TP range is narrow (e.g. 0.9%), the entire target can be consumed before the broker executes. VBL 2026-04-29: fill 533.95 vs TP 532.75 (range 4.75pts), SL at 519.34 — holding the position would mean full downside with no upside.

**Fix:** Post-fill overshoot check added after `fetch_order_fill_price()` (pipeline step 10a). If fill ≥ TP (LONG) or fill ≤ TP (SHORT): SL cancelled, MARKET close sent, `record_close(0.0)` called, function returns. Position is never registered in tracker.

### Bug Fix: Rejected trades un-counted from daily trade limit (risk.py)

When the broker rejects a MIS order (T2T/BE series stocks, margin issues), `record_rejection()` released the open-position slot but left `trades_today` incremented. On active days with repeated rejections (e.g. 3 MIS-restricted stocks), the daily trade limit could be exhausted by phantom trades, blocking valid signals for the rest of the day.

**Fix:** `record_rejection()` now decrements both `open_positions` and `trades_today` (with floor at 0). Only `open_positions` and `trades_today` are touched — loss counters are unchanged (the trade never existed at the broker).

### Bug Fix: Guard 2 orphan false-positive on filled positions (tracker.py)

Guard 2 relied solely on `orderstatus` API to determine fill status. When the broker API returns HTTP 500 or empty strings (as seen in the Apr-22 incident), Guard 2 times out and incorrectly treats a genuinely filled position as an orphaned rejection — cancelling the SL and releasing the slot on a live position.

**Fix:** `TrackedPosition` gains `ever_seen_nonzero_qty: bool`. Set to `True` in the poll loop whenever positionbook reports qty > 0. Guard 2 timeout now checks this flag: if set, processes as real close (calls `_process_close`) rather than orphan cleanup — because positionbook qty > 0 is broker-side proof of fill, independent of the orderstatus API.

### Config Updates (config.yaml)

- `risk_per_trade: 0.015` (was 0.01) — temporary 1.5% until capital raised to ₹35K
- `blacklist.ORB.hard` — added HUDCO (0% WR, 4 trades, -₹194 Apr 2026, erratic ORB)
- `blacklist.ORB.soft` — added NATIONALUM (mixed Apr 2026: 3 break-even/small-loss, 1 good TP, erratic follow-through)

**Files**: `main.py`, `risk.py`, `tracker.py`, `config.yaml`, `tests/test_main.py`, `tests/test_risk.py`, `tests/test_tracker.py`.

---

## Recent Changes (2026-04-25)

### ORB strategy improvements — Phase 1 (config + scanners)

**No-progress thresholds loosened.** `check_after_minutes` 60→90, `min_progress_pct` 0.33→0.20. Apr 13–24 logs showed the old gates cut JSWENERGY/EXIDEIND/NATIONALUM at 31.x%-progress, 1–2% short of TP1. New `ab_test_disable: false` flag short-circuits the entire check without changing thresholds — for clean A/B measurement.

**Tiered blacklist.** `blacklist.ORB` now splits into `hard` (full block, validator) and `soft` (qty reduction by `soft_multiplier`, risk engine — Phase 2 wires this). CANBK, FEDERALBNK, WIPRO, BANKBARODA moved from hard to soft (Q1→Q2 grade flips that may recover). Backward compat: flat list under a strategy key still parses as hard-only.

**ORB-STRATEGY-ANALYSIS.md scanners updated.** Setup Scanner widened `{nifty200}`→`{nifty500}`. Two new scanners added: pre-market gap scanner (Scanner 3, 09:10 AM) and NR7 end-of-day pre-filter (Scanner 4, 15:25). Workflow table updated. Sector-rotation overlay documented (Chartink has no native sector filter — manual sector-index check).

**Files**: `config.yaml`, `config.py`, `tracker.py`, `tests/test_config.py`, `pinescripts/intraday/orb/STRATEGY-ANALYSIS.md`.
**Next phases**: Phase 2 (risk.py soft-blacklist sizing) and Phase 3 (PineScript NR filter input). Tracking via `pre-orb-improvements-2026-04-25` git tag.

### ORB strategy improvements — Phase 2 (risk engine soft sizing)

`RiskEngine` now accepts `soft_blacklist` (per-strategy frozenset map) and `soft_blacklist_multipliers` (per-strategy float map). New private method `_apply_soft_scaling(signal, qty)` is invoked between baseline mode dispatch and the `qty <= 0` skip. When the signal's symbol is in `soft_blacklist[signal.strategy]`, qty is multiplied by `soft_blacklist_multipliers.get(strategy, 0.5)` and the floor is taken. Result is logged as `[soft-sized] SYMBOL (STRATEGY): qty A -> B (multiplier xN)`.

Risk-shape preservation: scaling is applied to final qty, NOT to `risk_per_share`. So actual rupee-risk is exactly `multiplier × baseline_risk` — half-size = half-risk. The 1% per-trade guarantee is unaffected for non-soft symbols.

`main.py` passes `settings.soft_blacklist` and `settings.soft_blacklist_multipliers` into the global engine. CANBK, FEDERALBNK, WIPRO, BANKBARODA now trade at 50% qty for ORB instead of being fully blocked.

**Tests**: 8 new cases in `TestSoftBlacklist` covering halve-default, non-soft passthrough, strategy scoping, multiplier=1 noop, multiplier=0 → skip, slippage interaction, missing-config noop, missing-multiplier-key fallback.

**Files**: `risk.py`, `main.py`, `tests/test_risk.py`, `PRD.md`. Bandit clean.

### ORB strategy improvements — Phase 3 (PineScript NR filter)

`orb.pine` adds a Crabel narrow-range pre-filter as a regime gate:
- New input group `🔻 NR FILTER (Crabel)`
- `enableNRFilter` (default false), `nrLookback` (default 7, options 4/7/10/20), `nrMode` (default "Prefer", options Prefer/Require)
- Daily-timeframe security call computes `cachedNRPreferHit` = whether yesterday's range ≤ lowest of last `nrLookback` daily ranges. CRITICAL: `lookahead=barmerge.lookahead_off` to prevent repainting.
- `nrRequireGate` ANDed into `canTakeEntry` — only blocks in Require mode. In Prefer mode (default on enable), zero behaviour change vs baseline.
- Validation contract: `enableNRFilter=false` produces an identical backtest to before. Manual TradingView Strategy Tester check required before flipping to Require.

**Files**: `pinescripts/intraday/orb/orb.pine`, `PRD.md`. Pine has no unit framework — validation is Strategy-Tester diff.

---

## Recent Changes (2026-04-22)

### Bug Fix

**`orderstatus` field name mismatch (api_client.py)**  
`fetch_order_fill_price` and `fetch_order_status` looked for `"orderstatus"` but the OpenAlgo orderstatus API returns `"order_status"`. This caused entry fill price to always be `None`, which set `pos.fill_price = 0.0` for every trade. The cascading effect:
1. Guard 2 (orphan check) fired on every position close after 60 min — treating legitimate no-progress exits as orphaned rejections.
2. P&L was not tracked for those positions (slot released as rejection), so the untracked broker P&L bled into the next ORB position's `pnl_delta` via the `m2mrealized` delta.

Fix: both functions now check `order_status` first with `orderstatus` as fallback.

### Notification Reduction

Granular per-trade Telegram messages removed. Five functions converted to log-only (signal_engine.log only, no Telegram):
- `notify_entry_filled` — "LIVE | fill confirmed, SL active"
- `notify_partial_exit` — TP1/TP1.5 partial exit with runner qty
- `notify_position_closed` — full close (TP win / SL hit)
- `notify_be_stop_applied` — SL moved to break-even
- `notify_no_progress_exit` — no-progress market exit fired

Signal-engine channel now receives only: entry placement, rejections, SL failures, orphaned orders, time exits, risk halts, day summary, engine lifecycle.

---

## Recent Changes (2026-04-17)

### New Features

**No-Progress Detection (Rate-Based Exit)**  
Detects stuck trades and takes action based on a rate projection — not a fixed timer. At `check_after_minutes` (60min), if progress < `min_progress_pct` (33%):
- Compute rate: `progress / age_minutes`
- Project: `minutes_needed = (1 - progress) / rate`
- If `minutes_needed > minutes_to_exit` → **market exit now** (won't reach TP1 before 15:00)
- Otherwise → **break-even SL** (slow but still on track — let it run)

60min grace aligns with ORB momentum window. Late entries (11:00 AM) are held to a higher standard — they need ≥25% progress vs ≥19% for early entries (9:45 AM), because they have less runway. Sends `⚠️ STOP → BREAK-EVEN` or `🚪 NO-PROGRESS EXIT` notification.

**Orphaned Position Detection**  
New `record_rejection()` in RiskEngine releases position slots for phantom/unfilled orders without counting a trade or touching loss counters. When the tracker detects an order was never filled (broker rejection, slow fill, or zero-PnL orphan), the function is called to free the slot. Sends Telegram notification: `⚠️ ORDER NOT FILLED`.

**T2T (BE Series) MIS Filter**  
Rejects MIS orders for T2T (BE series) stocks — brokers do not allow intraday MIS trading on these symbols. Logs warning and skips order, freeing the slot. Recommendation: add to `blacklist.ORB` to suppress repeated attempts.

### Configuration Updates

**`tracking.min_position_age_seconds` (New)**  
Minimum age before a position can be detected as closed (default: 30s). Protects against ghost-closes when order rejection causes brief qty=0 in positionbook before broker processes fill. Covers worst-case propagation lag while catching real SL hits.

### Notification Changes

**Redesigned Telegram Notifications** — trader-friendly format added:
- Entry: `📤 ENTRY SENT`, `🚫 ENTRY REJECTED`, `🚨 SL NOT PLACED`, `⚠️ ORDER NOT FILLED`
- Exit: `⏰ TIME EXIT`, `❌ EXIT FAILED`
- Risk/system: `🛑 TRADING HALTED`, `🟢/🔴 Engine started/stopped`
- Daily summary: trades, win rate, net P&L, capital, per-trade table

Note: as of 2026-04-22, `💰 LIVE`, `🎯 TP1 HIT`, `✅ TP WIN`, `❌ SL HIT`, `⚠️ STOP → BREAK-EVEN` were removed from Telegram (converted to log-only). See 2026-04-22 changes.

---

## Module Map

| File | Responsibility |
|------|---------------|
| `main.py` | Pipeline orchestration: entry/exit routing, T2T (BE series) filter, message dispatch. Decomposed 2026-08-23 — `_handle_entry` and `_handle_exit_locked` are now thin orchestrators over named stage functions (`_entry_rejected_by_symbol_rules`, `_resolve_entry_quantity`, `_establish_position`, `_recover_position_from_broker`, `_reconcile_sl_hit`, `_book_exit_result`, `_finalize_full_exit`, `_finalize_partial_exit`, ...). No function exceeds 50 lines. |
| `startup.py` | Process startup and engine lifecycle (added 2026-08-23, extracted from `main.py`): CLI flags (`--smoke-test`, `--dry-run`, `--test`), startup health checks, broker position reconciliation and tracker restore, event loop and graceful shutdown. Collaborators are passed in — no global state. |
| `listener.py` | Async Telegram channel listener (Telethon) |
| `normalizer.py` | Raw message preprocessing → canonical format; handles TP HIT, SL HIT, pipe-delimited, emoji stripping |
| `parser.py` | Canonical text → `Signal` model |
| `validator.py` | Signal validation (SL, R:R, duplicates, blacklist) |
| `risk.py` | Position sizing (`RiskEngine`), exposure limits, portfolio heat; `record_rejection()` for phantom orders |
| `risk_store.py` | SQLite persistence for risk counters (restart-safe). Path: `RISK_DB_PATH` |
| `executor.py` | Order construction + OpenAlgo API calls |
| `tracker.py` | Position lifecycle: register, poll SL fills, time exit, no-progress detection; `TradeRecord` dataclass, `_compute_r()` R-multiple helper; three-guard close detection (Guard 1: min age, Guard 2: orderstatus with rejection/timeout-orphan, Guard 3: zero-PnL orphan); `ever_seen_nonzero_qty` flag — set when positionbook shows qty>0 at any poll; Guard 2 timeout treats position as real close (not orphan) when flag is set; all orphan paths cancel associated SL order; race guard (`_positions.get(key) is not pos`) under `_pnl_lock` prevents double-close; per-(key,kind) debug throttling via `_should_log_debug()` |
| `api_client.py` | All async OpenAlgo API calls |
| `notifier.py` | Telegram notification dispatch (entry, exit, risk, lifecycle, daily summary); `format_day_context()` / `format_slot_context()` helpers for running day telemetry lines |
| `config.py` | Fail-fast config loader (`Settings` dataclass singleton); no_progress, tracking sections |
| `models.py` | `Signal`, `Order`, `TradeResult`, `ValidationResult` |
| `runtime.py` | Composition root (added 2026-08-23): `build_risk_engine()` wires `settings` -> `RiskEngine`. Used by both `main.py` and `smoke_test.py` — previously three separate constructions had drifted by three sizing parameters, so the dry run did not size like production. |
| `timeutils.py` | Shared `IST` timezone and `now_ist()` (added 2026-08-23). Previously redefined independently in five modules. |
| `strategies.py` | Strategy name constants |
| `db.py` | SQLite trade audit trail. Path: `_DB_PATH` |
| `logger_setup.py` | Loguru daily rotation; file sink defaults to INFO (set `SIGNAL_ENGINE_LOG_LEVEL=DEBUG` for verbose mode) |
| `smoke_test.py` | Pre-session health checks + dry run |

### Known defects (documented, not fixed)

| Defect | Location | Notes |
|---|---|---|
| `notify_be_stop_applied(original_sl=...)` is always `None` | `tracker.py` `_no_progress_break_even` | `pos.sl` is assigned `be_price` immediately before the notification is built, so the `pos.sl != be_price` guard can never be true. The break-even alert therefore never shows the stop it replaced. Preserved as-is by the 2026-08-23 refactor (behaviour-preserving); fixing it changes the notification payload. |

### Key Functions & Methods (2026-04-17 additions)

| Function | Module | Purpose |
|----------|--------|---------|
| `record_rejection()` | `risk.py` | Release position slot AND un-count `trades_today` for rejected/phantom orders (position never existed at broker) |
| `_is_be_series()` | `main.py` | Check if symbol is T2T (BE series) — MIS trading rejected |
| `notify_orphaned_position()` | `notifier.py` | Telegram alert for order never filled |
| `notify_be_stop_applied()` | `notifier.py` | Log-only: SL moved to break-even (no Telegram) |
| `notify_partial_exit()` | `notifier.py` | Log-only: partial TP exit (no Telegram) |
| `notify_position_closed()` | `notifier.py` | Log-only: full position close (no Telegram) |
| `notify_no_progress_exit()` | `notifier.py` | Log-only: no-progress market exit fired (no Telegram) |
| `notify_entry_filled()` | `notifier.py` | Log-only: entry fill confirmed (no Telegram) |
| `notify_day_summary()` | `notifier.py` | EOD Telegram summary: trades, win rate, per-trade table, capital trajectory |
| `_poll_positions()` | `tracker.py` | Detects closed positions with min_position_age_seconds guard |
| `_check_no_progress()` | `tracker.py` | Detects stuck trades and moves SL to break-even |

### Scripts (`scripts/`)

| File | Purpose |
|------|---------|
| `openalgoscheduler.py` | Startup: auto-login (TOTP), verify auth, start signal engine |
| `openalgoctl.sh` | Service controller: start/run/stop/restart/status, log rotation |
| `openalgoctl.ps1` | Windows: launches `openalgoctl.sh` in minimized cmd |
| `createTaskOpenAlgoScheduler.ps1` | Windows Task Scheduler: 3 tasks — 8:50 AM start, 3:30 PM stop, watchdog every 5 min 9 AM-3:25 PM |

---

## Signal Format

### Entry
```
STRATEGY DIRECTION
Symbol: SYMBOL
Entry: 250.50
SL: 246.00
TP: 262.00
Exchange: NSE        (optional — defaults to broker.exchange)
Product: MIS         (optional — defaults to broker.product)
```

Pipe format also supported: `ORB LONG | WIPRO` on first line.  
`Target:` is aliased to `TP:` for backward compatibility.

### TP HIT / Exit
```
ORB TP1 HIT | WIPRO
```
Normalizer converts this to canonical EXIT. `TP1` matched against `strategy_profiles.tp_levels`.

---

## Position Sizing Calculator (Webapp)

A standalone calculator is available at `/sizing` in the OpenAlgo webapp.
- **API**: `POST /api/v1/sizing` — stateless, JSON-only, works from curl/Android too
- **Modes**: `fixed_fractional` (risk %) and `pct_of_capital` (flat allocation)
- **Live capital**: leave `capital` blank to auto-fetch from broker funds
- **Outputs**: qty, risk ₹, R:R ratio, position value, skip reason (if qty=0)
- **Tests**: `test/test_sizing_service.py` (47 unit), `test/test_sizing_api.py` (24 integration)

---

## Position Sizing

### fixed_fractional (default)
```
qty = floor(capital × risk_per_trade / (|entry - sl| × (1 + slippage_factor)))
```
`slippage_factor: 0.10` — widens the denominator by 10%, producing conservatively fewer shares.
If `qty = 0`, trade is skipped (stock too expensive for the risk budget).

**Actual risk per trade (Q2 2026 validated):**
The 10% slippage buffer means actual risk = `qty × sl_dist ≈ capital × risk_per_trade / 1.10`.
At 1% risk and ₹15K capital: actual risk ≈ ₹138 (0.91%), not ₹152 (1.0%) — the ~9% haircut
absorbs real fill slippage on MARKET entry and TP exit orders. This is by design.

Validated 2026-04-13: JSWENERGY 0.81%, EMAMILTD 0.89%, EXIDEIND 0.90%, TMPV 0.88%.

**Expected profit per trade (₹15K capital, avg stock ₹350, avg SL 1.5%):**
```
Full exit at R:R 1:1:     profit ≈ actual_risk ≈ ₹138  (not ₹150 — slippage buffer accounts for ~9%)
TP1 50% exit at R:R 0.7:  profit ≈ ₹138 × 0.7 × 0.5 ≈ ₹48  (partial, runner continues)
TP1 50% exit at R:R 0.5:  profit ≈ ₹138 × 0.5 × 0.5 ≈ ₹35  (partial, runner continues)
Blended (50-50 trail, TP1.5 hit 27.6% of trades): ≈₹61/winning trade
```
ORB signals typically have R:R 0.5–0.7 at TP1. Validated: TMPV ₹45, JSWENERGY ₹45 at TP1.
Full round-trip (TP1+runner) validated: TMPV ₹84.60, JSWENERGY ₹103.20.

### Slippage Factor vs Broker MPP

These serve different purposes and are NOT redundant:

- **MPP (Market Price Protection)** — Flattrade hard fill cap per leg: entry MARKET → LMT at LTP+1% (₹100–500 stocks). SL-M → SL-LMT at trigger+1%. Prevents catastrophic fills in illiquid spikes. Pure safety net.
- **`slippage_factor: 0.10`** — sizing buffer: widens denominator by 10% to account for entry filling above signal price on momentum breakout (typical: 0.3–0.8% above). Also covers TP exit slipping below TP price on MARKET order. Without this buffer, actual risk regularly runs 10–15% over 1%.

MPP caps the worst-case single fill. `slippage_factor` accounts for the typical gap that widens effective risk across both legs. Keep at 0.10 — validated Q2 2026 (actual risk 0.81–0.90% = ~9% haircut from the 10% buffer).

**`max_sl_pct_for_sizing` (default 0.0 = disabled):** Enabling this cap would inflate qty beyond
what the SL distance justifies, producing actual losses of 2–3× risk_per_trade when SL fires.
Keep at 0 to maintain strict ~0.91% risk per trade. Wide-SL days will have lower capital
utilisation — this is the mathematically correct consequence of consistent 1% risk management.

### pct_of_capital
```
qty = floor(capital × pct_of_capital / entry_price)
```

### Day-start capital (`use_day_start_capital: true`)
First capital fetch of each day is cached. All subsequent trades size off this value — equal risk per trade regardless of intraday P&L.

### Minimum capital floor (`min_capital_for_entry`)
Before sizing, live capital is checked against `sizing.min_capital_for_entry`. If below the floor, the entry is skipped cleanly — no order is sent to the broker. Prevents dwarf positions when open positions have consumed most of the margin.

### Margin check (Live only)
After sizing, `adjust_qty_for_margin()` checks whether the full-risk qty fits in live capital:

- **NSE/BSE equity** (SpanCalc API is derivatives-only): estimates margin as `qty × entry × mis_margin_pct`. **Binary reject** — if estimated margin > live capital, returns 0 (trade skipped). Never scales qty down; a scaled trade risks less than 1% and produces dwarf profits not worth commission cost.
- **Derivatives (NFO etc.)**: `fetch_margin()` → SpanCalc API for exact margin. If `actual_margin > live_capital`: `qty = floor(raw_qty × live_capital / actual_margin)`.

### Capital vs Concurrent Slots

**Q2 2026 observed margin per trade** (NSE MIS 20% on actual position values):
- Avg margin per trade: 6–8% of capital (wide-SL stocks like JSWENERGY: 4%, tight-SL: 9%)
- 5 concurrent slots = 30–40% margin utilisation — well within ₹15K+ available capital
- Original Q1 estimate of 31% margin per trade was based on avg_sl=0.64%; actual SLs vary 0.5–4%

`margin_per_trade ≈ qty × entry × 20%`  where `qty = floor(capital × 1% / (sl_distance × 1.10))`

**Sizing and profitability by capital level** (assumptions: avg stock ₹350, avg SL 1.5%, slippage_factor=0.10, 50-50 trail-to-TP1 strategy, ~3 trades/day, 70% win rate from Q1):

| Capital | Risk/trade | Actual risk (~91%) | Qty (avg) | Margin/trade (~9%) | Slots | TP1 partial profit | Blended $/win | Est. monthly net |
|---------|-----------|-------------------|-----------|-------------------|-------|--------------------|---------------|------------------|
| ₹15K    | ₹150      | ₹136              | ~25       | ₹788              | 5     | ₹44                | ₹61           | ₹3,200           |
| ₹25K    | ₹250      | ₹227              | ~43       | ₹1,330            | 5     | ₹74                | ₹102          | ₹5,400           |
| ₹35K    | ₹350      | ₹318              | ~60       | ₹1,860            | 5     | ₹103               | ₹142          | ₹7,500           |
| ₹50K    | ₹500      | ₹454              | ~86       | ₹2,660            | 5–6   | ₹148               | ₹204          | ₹10,800          |

Return % stays constant at ~21%/month — profit scales linearly with capital. The practical benefit of larger capital is fewer rejections from `min_capital_for_entry` floor and room to raise `max_open_positions` to 6–7 at ₹35K+.

### Multi-TP vs TP1-only: Strategy Decision

Q1 2026 net PnL comparison (196 trades, 31 days, ₹35K capital, Flattrade percentage fees):

| Strategy | Net PnL | Notes |
|---|---|---|
| TP1-only (100% exit) | +30.3% | Leaves 54 TP1.5 trades on the table |
| 50-50 trail to BE | +28.2% | BE stop-outs eat into gains |
| **50-50 trail to TP1** | **+42.7%** | **Strictly optimal** |

50-50 trail-to-TP1 dominates: identical outcome on SL trades and TP1-only trades, strictly better on the 27.6% of trades that reach TP1.5 (54 trades in Q1). Percentage fees (Flattrade) make partial exits free — no per-order cost penalty.

Note: earlier analysis showed "TP1-only +69R better" — that was under mStock's per-order brokerage model. Under percentage fees, 50-50 trail is always at least as good. **Current implementation is optimal.**

---

## Risk Management

All limits checked before each order. Counters persist across restarts via `risk_store.py`.  
On startup, `open_positions` is reconciled against actual broker positionbook.

| Limit | Config Key | Notes |
|-------|-----------|-------|
| Daily loss | `risk.daily_loss_limit` | Realised + unrealised loss |
| Weekly loss | `risk.weekly_loss_limit` | Mon–Sun |
| Monthly loss | `risk.monthly_loss_limit` | Calendar month |
| Max open positions | `risk.max_open_positions` | Slot-based (slots recycle) |
| Max trades/day | `risk.max_trades_per_day` | Total order count |
| Portfolio heat | `risk.max_portfolio_heat` | Sum of open risk % |
| Price filter | `sizing.min/max_entry_price` | Skip outside band |
| Min SL % | `risk.min_sl_pct` | Reject SL tighter than x% of entry |
| Min R:R | `risk.min_rr` | Skip signals below threshold |
| Symbol concentration | `risk.max_positions_per_symbol` | Per-symbol cap |
| Duplicate window | `risk.duplicate_window_seconds` | Dedup identical signals |
| Stale signal | `risk.stale_signal_seconds` | Reject old signals |
| Hard blacklist | `blacklist._global` / `blacklist.STRATEGY.hard` (or flat list) | Validator rejects with IGNORED status — no order placed |
| Soft blacklist | `blacklist.STRATEGY.soft` + `soft_multiplier` | Risk engine scales qty by `soft_multiplier` (default 0.5) via `_apply_soft_scaling`. Use for regime-flipped stocks where full block discards optionality. Logged as `[soft-sized]` in signal_engine.log. |

---

## Indian Broker OCO Constraint

**Problem:** Broker treats any SELL while a SL SELL is active as a new SHORT → `FUND LIMIT INSUFFICIENT`.

**Solution:** Only the SL-M order is placed at the broker. TP exit is driven by TradingView TP HIT signals → `_handle_exit`. No simultaneous SL + TP broker orders.

**Critical:** Before ANY exit order (full or partial) the SL must be cancelled first via `cancel_order()`.

### Flattrade MPP (Market Price Protection)
Flattrade blocks raw MARKET and SL-MKT order types via API.
- `MARKET` → `LMT` with price = LTP ± MPP% (slab: 2%/<100, 1%/100-500, 0.5%/>500)
- `SL-M` → `SL-LMT` with limit = trigger_price ± MPP% (keeps trigger untouched)
- Handled in `broker/flattrade/mapping/transform_data.py`

---

## Telegram Notifications

All notifications sent via `notifier.py` to `telegram.notify_channel`. Format is trader-friendly with timestamps, direction arrows (▲/▼), hold duration, and risk-adjusted metrics.

### Entry

| Message | Trigger | Example |
|---------|---------|---------|
| `📤 ENTRY SENT` | Order placed to broker | `📤 ENTRY SENT \| SBIN LONG ▲ \| ORB \| 10:15 IST`<br>`Signal: 800.50 \| SL: 793.00 \| TP: 815.00 \| R:R 1:1.9` |
| `🚫 ENTRY REJECTED` | Risk check failed or order rejected | `🚫 ENTRY REJECTED \| SBIN \| ORB \| 10:15 IST`<br>`No trade taken. Reason: max_open_positions exceeded` |
| `🚨 SL NOT PLACED` | Entry filled but SL order failed | `🚨 SL NOT PLACED \| SBIN \| ORB \| 10:15 IST`<br>`Position UNPROTECTED. Reason: broker rejected`<br>`Place SL manually or close position.` |
| `⚠️ ORDER NOT FILLED` | Entry order rejected or unconfirmed after timeout | `⚠️ ORDER NOT FILLED \| SBIN LONG ▲ \| ORB \| 10:15 IST`<br>`No position taken. Entry order was rejected by the broker.`<br>`Check broker terminal: order 260422XXXXXX` |

### Exit

| Message | Trigger | Example |
|---------|---------|---------|
| `⏰ TIME EXIT` | Forced close at `time_exit.hour:minute` | `⏰ TIME EXIT \| SBIN LONG ▲ \| ORB \| held 4h 45m`<br>`800.50 → — \| +₹243 (+0.6R)` |
| `❌ EXIT FAILED` | Exit order placement failed | `❌ EXIT FAILED \| SBIN \| ORB \| 10:45 IST`<br>`Reason: API timeout` |

### Risk & System

| Message | Trigger |
|---------|---------|
| `🛑 TRADING HALTED` | Daily/weekly/monthly loss limit exceeded |
| `🟢 Engine started` | Startup complete, capital initialised |
| `🔴 Engine stopped` | Engine shutdown |
| `🟢 READY` / `🔴 STARTUP FAILED` | Pre-market health check result |

**Log-only (not sent to Telegram):** fill confirmation, partial TP exits, position close (TP win / SL hit), break-even SL move, no-progress exit. All appear in signal_engine.log at INFO level. Set `SIGNAL_ENGINE_LOG_LEVEL=DEBUG` for poll-level traces.

### Risk and System Events

| Message | Trigger |
|---------|---------|
| `🛑 TRADING HALTED` | Daily/weekly/monthly loss limit exceeded |
| `⚠️ ORDER NOT FILLED` | Orphaned position detected (phantom/slow-fill order, slot released) |
| `🟢 Engine started` | Startup complete, capital initialized |
| `🔴 Engine stopped` | Engine shutdown |
| `🟢 READY` / `🔴 STARTUP FAILED` | Pre-market health check result |

### EOD Day Summary

```
📊 DAY SUMMARY | 22-Apr-2026
Trades: 3 | W: 1  L: 0  T: 2 | Win Rate: 100%
Net: +₹102 (+0.7%) | Avg R: +0.8R
Capital: ₹14,465 → ₹14,567
────────────────────────────────────────
▲ EXIDEIND     339.80→347.90   +₹102    (+0.8R)  TP1+TP1.5
▲ NATIONALUM   432.85→431.00   -₹17     (-0.1R)  TIME
▲ JSWENERGY    554.35→554.50   +₹1      (+0.0R)  TIME
```

**Summary components:**
- `W`/`L`: decided trades (TP or SL outcome). WR = W / (W+L), excludes time exits
- Capital line: opening → closing values
- Per-trade table: direction arrow, symbol, entry→exit, total P&L, total R (risk-adjusted), exit types (TP1, TP1.5, SL, TIME_EXIT)
- `⚠️` flag: orphaned trades (0 PnL + entry ≈ exit price) indicate unfilled orders
- Avg R: mean risk-multiple across decided trades (numeric summary of expected value)

---

## Configuration (`config.yaml`)

All values required — `ConfigError` raised on any missing key.

### `telegram`
```yaml
telegram:
  channels:
    - name: "channel-label"
      id: -100XXXXXXXXX
  notify_channel:           # system alerts (startup/shutdown/errors)
    name: "admin"
    id: -100XXXXXXXXX
```

### `sizing`
| Key | Description |
|-----|-------------|
| `mode` | `fixed_fractional` or `pct_of_capital` |
| `risk_per_trade` | Fraction of capital to risk (fixed_fractional) |
| `pct_of_capital` | Fraction of capital per position (pct_of_capital) |
| `min_entry_price` | Skip stocks below this price |
| `max_entry_price` | Skip stocks above this price |
| `slippage_factor` | Widens SL distance before sizing |
| `max_sl_pct_for_sizing` | SL cap for qty calc (0=off). Wide-SL stock qty computed as if SL = entry × cap. Real SL order unchanged. |
| `sandbox_capital` | Capital override in analyze mode |
| `use_day_start_capital` | Cache first fetch of day for equal risk per trade |
| `test_qty_cap` | Max qty per order in `--test` mode (0 = disabled) |
| `min_capital_for_entry` | Skip new entries if live capital below this floor (INR) |

### `risk`
| Key | Description |
|-----|-------------|
| `daily/weekly/monthly_loss_limit` | Loss lockout thresholds (fraction of capital) |
| `max_portfolio_heat` | Max open risk fraction |
| `max_open_positions` | Concurrent slot cap |
| `max_trades_per_day` | Daily order cap |
| `min_rr` | Min reward:risk ratio |
| `duplicate_window_seconds` | Dedup window |
| `stale_signal_seconds` | Max signal age |
| `min_sl_pct` | Min SL distance (0 = disabled) |
| `max_positions_per_symbol` | Per-symbol cap (0 = disabled) |
| `max_positions_per_sector` | Per-sector cap (0 = disabled) |

### `bracket`
| Key | Description |
|-----|-------------|
| `enabled` | Place SL-M after entry fill |
| `cnc_sl_enabled` | false = skip bracket for CNC (NSE cancels overnight) |
| `sl_order_type` | `SL-M` (default) |
| `max_sl_retries` | Retry count for SL placement |
| `retry_delay` | Seconds between retries |
| `tp_exit_retries` | Retry count for TP MARKET exit |

### `strategy_profiles`
```yaml
strategy_profiles:
  ORB:
    product: MIS
    # tp_levels absent — ORB uses ExitQtyPct from signal directly
  RSI-TP-MR:
    product: MIS
    tp_levels:
      TP1: 1.0          # Exit 100% at TP1
```

### `no_progress` (new — 2026-04-17, rate-based — 2026-04-20, loosened — 2026-04-25, chop tightener — 2026-05-05, loss-cut — 2026-05-12)
```yaml
no_progress:
  enabled: true
  check_after_minutes: 90        # Was 60. Grace period — most ORB winners need >60min to develop
  min_progress_pct: 0.20         # Was 0.33. 20% qualifies as "real progress"
  profit_lock_ratio: 0.0         # 0.0 = strict break-even SL (recommended)
  ab_test_disable: false         # Master kill-switch for A/B comparison; skips entire check when true
  early_check_enabled: false     # Disabled 2026-05-07: was adding slippage on top of SL loss
  early_check_after_minutes: 45
  early_min_progress_pct: 0.05
  # Adaptive chop tightener — disabled 2026-05-12 (max_open_positions=4 handles chop exposure)
  chop_tightener_enabled: false
  chop_tightener_trigger_count: 2
  chop_tightener_early_check_after_minutes: 30
  # Loss-cut gate (2026-05-12) — exits when trade is deeply against you, regardless of age
  loss_cut_enabled: true
  loss_cut_min_age_minutes: 20        # skip first N min (opening-candle noise)
  loss_cut_progress_threshold: -0.80  # fire when progress < -80% (price 80% to SL)
```
At 90min, if progress < 20%: project `minutes_needed = (1 - progress) / rate` and compare to minutes remaining until time exit. If the trade cannot reach TP1 before 15:00 at its current pace → market exit. Otherwise → break-even SL.

**2026-04-25 loosen rationale:** Apr 13–24 logs showed ~17 firings/week with JSWENERGY (146min/31.2%), EXIDEIND (162min/31.7%), NATIONALUM (84min/31.1%) cut at 1–2% short of the 33% threshold AND 1–2% short of TP1. The 60min/33% gates were trimming would-be winners. New 90min/20% lets genuinely-stuck trades resolve.

**`ab_test_disable`:** When true, the entire no-progress check is skipped without altering thresholds — used to compare 10 days with the feature off vs on for clean PnL attribution.

**Chop tightener (2026-05-05):** After `chop_tightener_trigger_count` (default 2) no-progress firings in the current session, the **early gate** age threshold drops from `early_check_after_minutes` (45) to `chop_tightener_early_check_after_minutes` (30). Main gate is intentionally untouched — it protects slow-developing winners (e.g., 2026-05-05 PETRONET reached recovery at 127min on the 90/20% main gate). Counter resets at session reset / time exit alongside other day counters.

Empirical basis (Apr 20–May 5 logs): 4/10 days had ≥2 no-progress exits; on 2026-05-05, 7/8 filled positions hit no-progress and the 1st two fired at 10:45/10:50 — the tightener engages here, force-exiting subsequent stalled trades ~15min sooner and recycling capital. Trades on a real path clear the 5% progress floor well before 30min, so winners are not affected.

**Decision thresholds by entry time** (entry window 9:45–11:00 AM, time exit 15:00):

| Entry | Check at | Time to exit | Break-even SL if progress ≥ | Market exit if progress < |
|---|---|---|---|---|
| 9:45 AM | 11:15 AM | 225 min | 21% | 21% |
| 10:00 AM | 11:30 AM | 210 min | 22% | 22% |
| 10:30 AM | 12:00 PM | 180 min | 25% | 25% |
| 11:00 AM | 12:30 PM | 150 min | 27% | 27% |

Progress ≥ 20% → no action (on track). Between threshold and 20% → break-even SL. Below threshold → market exit. Formula: `threshold = check_after_minutes / (check_after_minutes + minutes_to_exit)`.

### `time_exit`
```yaml
time_exit:
  enabled: true
  hour: 15            # IST 24h (15:00 = 10min buffer before broker square-off at 15:10)
  minute: 0
```

### `tracking`
```yaml
tracking:
  poll_interval: 5                 # seconds between position polls
  min_position_age_seconds: 30     # minimum age before detecting a position as closed
  guard2_timeout_minutes: 30       # max wait for ambiguous entry order status
```
**`min_position_age_seconds`:** Protects against ghost-closes. When an order is rejected/slow-to-fill, positionbook briefly shows qty=0. 30s covers worst-case propagation.

**`guard2_timeout_minutes` (updated — 2026-04-21):** Guard 2 waits for entry fill confirmation before recording a close. If fill price is still unconfirmed after 30min and orderstatus remains ambiguous (e.g. persistent 500 errors from broker API), Guard 2 times out and treats the position as an orphaned rejection — cancels any associated SL order and releases the slot. Previously this path "assumed complete" which left rejected-order phantom positions alive for hours (EMAMILTD 2026-04-21 incident: rejected entry tracked for 4.5h, TP1 exit placed on non-existent position creating naked short risk).

### `broker`
```yaml
broker:
  exchange: NSE
  product: MIS                   # MIS or CNC
  order_type: MARKET
  allow_off_hours_testing: false  # MIS→CNC override in analyze mode (testing only)
  mis_margin_pct: 0.20           # Must match broker's actual NSE equity MIS margin %
```

### `api`
```yaml
api:
  timeout: 5.0
  margin_retries: 3
```

---

## Environment Variables (`signal_engine/.env`)

| Variable | Required | Notes |
|----------|----------|-------|
| `TELEGRAM_API_ID` | Yes | From my.telegram.org |
| `TELEGRAM_API_HASH` | Yes | From my.telegram.org |
| `TELEGRAM_PHONE` | Yes | Phone number for Telegram session |
| `OPENALGO_API_KEY` | Yes | From OpenAlgo `/apikey` page |
| `OPENALGO_BASE_URL` | Yes | Default: `http://127.0.0.1:5000` |
| `REDIRECT_URL` | Yes | Broker auto-detected from path (e.g. `/flattrade/callback`) |
| `BROKER_PASSWORD` | TOTP brokers | Plaintext, hashed before sending |
| `BROKER_TOTP_SECRET` | TOTP brokers | Base32 seed from authenticator app |
| `BROKER_API_KEY` | Yes | `CLIENT_ID:::API_KEY` (flattrade) or client ID (mstock) |
| `BROKER_API_SECRET` | Most brokers | Not required for mstock |
| `BROKER_NAME` | No | Overrides `REDIRECT_URL` auto-detection |

---

## Broker Setup

### Broker Detection Priority
1. `BROKER_NAME` env var (explicit)
2. First path segment of `REDIRECT_URL` (auto-detect)

### Flattrade (Headless TOTP)
```ini
REDIRECT_URL=http://127.0.0.1:5000/flattrade/callback
BROKER_API_KEY=FZ40074:::your_api_key    # CLIENT_ID:::API_KEY
BROKER_API_SECRET=your_api_secret
BROKER_PASSWORD=your_password
BROKER_TOTP_SECRET=BASE32SECRET
```
Auth flow: `POST /auth/session` → `POST /ftauth` (SHA256 pwd + TOTP) → `POST /trade/apitoken`

### mstock (TOTP)
```ini
REDIRECT_URL=http://127.0.0.1:5000/mstock/callback
BROKER_API_KEY=MA6718246                 # client code only
BROKER_PASSWORD=your_password
BROKER_TOTP_SECRET=BASE32SECRET
# BROKER_API_SECRET not required
```

### OAuth Brokers (e.g. Zerodha)
No programmatic login. Log in via OpenAlgo web UI first; scheduler reuses stored DB token.

### Switching Brokers
Update `REDIRECT_URL` + broker credentials in `.env`, then restart. TOTP brokers auto-replace token. OAuth brokers require a browser login first.

**Troubleshooting:**

| Error | Fix |
|-------|-----|
| `Cannot determine broker name` | Set `REDIRECT_URL` in `.env` |
| `ftauth HTTP 4xx` | Verify `BROKER_API_KEY=CLIENT_ID:::API_KEY` format |
| `Broker changed from X to Y` | TOTP: just restart. OAuth: browser login first |
| `stat=Not_Ok` / `emsg=...` | Wrong password or TOTP secret |

---

## Operating Modes

### Live
- Capital: broker funds API → `/api/v1/funds`
- Margin: validated via `/api/v1/margin` before each trade
- Orders: real broker

### Analyze (Sandbox)
- Capital: `sandbox_capital` from config
- Margin check: skipped
- Orders: OpenAlgo sandbox (virtual ₹1Cr capital)
- `allow_off_hours_testing: true` → MIS overridden to CNC for after-hours pipeline tests

---

## Startup & Shutdown

```bash
# Linux/WSL — full start (OpenAlgo + broker auth + signal engine)
./signal_engine/scripts/openalgoctl.sh run       # foreground
./signal_engine/scripts/openalgoctl.sh start     # background
./signal_engine/scripts/openalgoctl.sh stop
./signal_engine/scripts/openalgoctl.sh status

# Windows
.\signal_engine\scripts\openalgoctl.ps1 start

# Windows Task Scheduler (one-time setup, run as Administrator)
.\signal_engine\scripts\createTaskOpenAlgoScheduler.ps1
# Creates 3 tasks under Anand user:
#   openAlgoAutoStart  -- 8:50 AM weekdays, long-running (blocks all day)
#   openAlgoAutoStop   -- 3:30 PM weekdays, graceful shutdown
#   openAlgoWatchdog   -- every 5 min, 9:00 AM-3:25 PM weekdays, crash recovery
```

### Windows Task Scheduler -- How the 3 Tasks Work Together

| Time | Task | Action |
|------|------|--------|
| 8:50 AM | `openAlgoAutoStart` | Calls `openalgoctl.ps1 run` -- starts app.py + signal engine, **stays running all day** |
| 9:00 AM-3:25 PM | `openAlgoWatchdog` | Calls `openalgoctl.ps1 start` every 5 min -- no-op if healthy, relaunches if crashed |
| 3:30 PM | `openAlgoAutoStop` | Calls `openalgoctl.ps1 stop` -- sends Telegram notification, kills both services |

The watchdog uses `start` (idempotent): polls `http://127.0.0.1:5000/`, skips if healthy, restarts the full stack if dead. Maximum recovery time after a crash: **5 minutes**.

### Failure alerting and cooldown (2026-08-25)

A failed startup used to be silent and open-ended. `_run_startup()` called `sys.exit(1)` on every failure path *before* reaching its notification step, and `openalgoctl.sh` then wrote a flat 24-hour cooldown. On 2026-08-24 a single 09:03 failure blocked 80 start attempts through 15:27 — the whole trading day — with nothing sent anywhere.

- `notify_failure()` alerts Telegram on each `_run_startup` failure (configuration, auto-login, broker-auth). It never raises, so a dead notifier cannot mask the underlying error.
- `openalgoscheduler notify <stage> <detail>` lets `openalgoctl.sh` raise an alert without duplicating Telegram wiring.
- The auth cooldown escalates **5m / 15m / 1h / 3h**, capped, with the counter reset on success. Worst case now stays inside one trading session.
- The supervisor loop re-probes the health URL every 60s and declares `app.py` wedged after 3 consecutive failures. `kill -0` only proved the PID existed, so an alive-but-unresponsive server read as healthy indefinitely.
- Alerts also fire on `app.py` crash and on signal-engine crash-loop giveup.

Covered by `tests/test_openalgoscheduler.py` and `tests/test_openalgoctl.sh` (13 shell assertions on the cooldown state machine).

### WSL Stability -- ~/.wslconfig (Windows user home)

```ini
[wsl2]
vmIdleTimeout=-1      # prevent Windows from auto-terminating the WSL2 VM
memory=8GB            # explicit cap prevents OOM-kill from Windows memory pressure
swap=2GB
pageReporting=false   # stop Windows reclaiming WSL memory pages aggressively
```

Apply with: `wsl --shutdown` then restart WSL.

### Signal Engine Self-Restart (openalgoctl.sh)

If only the signal engine crashes (app.py still healthy), `openalgoctl.sh run` automatically restarts it -- up to 5 times within a 5-minute window. Restart counter resets after 5 minutes of stability. If app.py itself dies, the watchdog task handles full recovery.

Shutdown reason in Telegram notification accurately reflects cause:
- `scheduled` -- normal INT/TERM signal (AutoStop task)
- `signal_engine_crash` -- signal engine exited, app.py still alive
- `app_crash` -- app.py exited unexpectedly
- `signal_engine_crash_loop` -- exceeded 5 restarts in 5 min
- `crash` -- both processes exited simultaneously

### Startup Sequence
```
openalgoctl.sh run
  1. Start OpenAlgo (uv run app.py) -- wait for HTTP readiness
  2. openalgoscheduler.py startup
     a. Auto-login to broker (TOTP or OAuth reuse)
     b. Verify token via broker funds API
  3. python -m signal_engine.main
     a. Run startup health checks (7 checks, ~5s) -- abort if critical fail
     b. Reconcile open_positions against actual broker positions
     c. Restore risk counters from DB
     d. Start TimeExitScheduler + PositionTracker
     e. Send Telegram startup notification
     f. Start Telegram listener (blocking)
```

---

## Testing

```bash
# Full test suite (458 tests)
PYTHONPATH=. uv run pytest signal_engine/tests/ -v

# With coverage
PYTHONPATH=. uv run pytest signal_engine/tests/ --cov=signal_engine --cov-report=term-missing

# Pre-session health check (run before market open)
PYTHONPATH=. uv run python -m signal_engine.main --smoke-test

# Full dry run (smoke + synthetic order without placing)
PYTHONPATH=. uv run python -m signal_engine.main --dry-run

# Inject a test signal (live market, uses test_qty_cap)
PYTHONPATH=. uv run python -m signal_engine.main --test "ORB LONG
Symbol: SBIN
Entry: 800.00
SL: 793.00
TP: 815.00"

# From file
PYTHONPATH=. uv run python -m signal_engine.main --test --test-file signal_engine/tests/test_signal.txt
```

---

## Databases

| DB | Path | Table | Contents |
|----|------|-------|----------|
| Risk store | `signal_engine/data/risk.db` | `risk_counters` | Daily counters: trades, loss, open_positions, portfolio_heat — keyed (mode, date) |
| Trade audit | `signal_engine/data/trades.db` | `trades` | Every order: symbol, qty, entry/sl/tp, order_id, status, timestamps |

### Key SQL Commands

```bash
# Today's risk state
sqlite3 signal_engine/data/risk.db \
  "SELECT mode, trade_date, trades_today, daily_loss, open_positions, portfolio_heat, day_start_capital FROM risk_counters WHERE trade_date=date('now');"

# Last 7 trading days
sqlite3 signal_engine/data/risk.db \
  "SELECT trade_date, trades_today, printf('%.2f',daily_loss) loss, open_positions, printf('%.2f',portfolio_heat) heat FROM risk_counters WHERE mode='live' ORDER BY trade_date DESC LIMIT 7;"

# Fix stale state after broker auto-squareoff
sqlite3 signal_engine/data/risk.db \
  "UPDATE risk_counters SET open_positions=0, portfolio_heat=0.0, daily_loss=0.0 WHERE mode='live' AND trade_date=date('now');"

# Today's trades
sqlite3 signal_engine/data/trades.db \
  "SELECT id, direction, symbol, quantity, order_id, status, substr(executed_at,12,8) time FROM trades WHERE executed_at >= date('now') ORDER BY executed_at;"

# Trades per day (last 14 days)
sqlite3 signal_engine/data/trades.db \
  "SELECT substr(executed_at,1,10) date, count(*) total, sum(status='SUCCESS') ok, sum(status='REJECTED') rej FROM trades WHERE executed_at >= date('now','-14 days') GROUP BY 1 ORDER BY 1 DESC;"
```
