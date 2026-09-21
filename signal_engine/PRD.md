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
   - `bracket.use_extended_runner_tiers` (**on since 2026-09-06**, see that changelog entry): the runner SL ratchets to whichever TP level was just hit instead of always TP1, and never loosens — each further partial exit only tightens the stop. For a key-level trade the structural stop is the floor and the ratchet takes over once it is tighter.
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

## BreakingTrade scanner (2026-09-03 → 2026-09-06)

New subsystem: `signal_engine/analysis/breakingtrade/`. Scrapes breakingtrade.com's Market
Profile and Volume scanners (no API exists), turns them into candidate lists, stores every
snapshot, and measures whether the selection predicts anything.

**Full decision log and confidence ledger: `pinescripts/intraday/breaking-trade/STRATEGY-LOG.md`.**
That file is the source of truth for this strategy; only the summary lives here.

| Piece | What it does |
|---|---|
| `fetcher.py` | Headless Playwright; one browser and one login per day |
| `scans.py` | The vendor's 13 documented scans, directional ones only |
| `emerging.py` | Directional setups whose move has NOT already happened |
| `btst.py` | Closing-hour carry list, long only, decidable by 14:45 |
| `store.py` | Snapshot history + NEW-since-last-poll transitions |
| `paper.py` | BTST paper ledger (`--paper`) |
| `validate.py` | MFE/MAE forward test with a random control |
| `review.py` | Daily paper review (`--review`), mode check first |
| `flip_watch.py` | Alert-only warning when an open position's own setup reverses (2026-09-07) |
| `eod_summary.py` | Once-daily Telegram scorecard: watchlist calls vs. close, BTST settlements (2026-09-08) |
| `tp_watch.py` | Staged TP + runner-SL trailing, checked every ~20s (2026-09-08) |
| `poller.sh` / `breakingtradectl.ps1` | Lifecycle, PID-file tracked, Windows-task driven |

**Status: PAPER ONLY.** `intraday-breakingtrade` is enabled and signal_engine takes the signals
end to end, but OpenAlgo must be in ANALYZE mode so orders reach the sandbox. Strategy tag
`BREAKINGTRADE`; alerts use the standard alert shape so parser/validator/risk/executor need no
special case.

**Evidence so far is negative or absent, and that is the headline:**

- BTST paper ledger, 48 closed trades: gross **+0.005%**, **net −0.185%** after ~0.19% costs.
- BTST backtest, 96 trades over 14 sessions: excess between −0.10% and +0.07% depending on
  benchmark, all |t| < 0.65 — underpowered, not damning.
- Intraday: 5 scored signals, 2 correct. Nothing can be concluded.
- Disproved along the way: the "narrow IB = coiled spring" belief (t = −8.93, wrong on 14 of
  14 days), and sector alignment (weakly negative).

Three separate look-ahead traps were found and fixed during this work, including a BTST list
that could never have been traded because it needed the 15:15–15:30 session. Assume more exist.

## Recent Changes (2026-09-20)

**value_area_fade selectivity investigation concludes: gap to real cost narrowed from ~16x to
~2.3-2.7x across six rounds, then sample size — not cost-viability — ended the line of inquiry.**
Pushed `min_wick_depth_atr` to 4.5/5.0/5.5/6.0 (direct continuation of 2026-09-17/18's v1-v5;
full tables in `STRATEGY-ANALYSIS.md`, v6 section). The trend from prior rounds continued cleanly
through depth 5.0 — best full-universe t-stat of the entire investigation (-3.15, vs v5's -5.34),
best dispersion (89/201 = 44.3% of symbols net-positive, vs v5's 33.8%), gross edge ~8.2-8.8
bps/trade (~10x v3's original 0.81) — then reversed sharply at depth 5.5, where IS trade count
(215) fell below the 300-500-trade floor this investigation has used throughout: gross edge
collapsed to 2.36 bps and the 0bps result flipped from solidly positive to negative. The reversal
lines up exactly with the sample-size floor, not with any change in the underlying pattern —
noise, not signal, confirmed by depth 6.0 (n=132) being worse still. Breakeven cost at the best
depths (4.5-5.0) is ~6-7bps against the real 16bps model — the smallest gap found across all six
rounds, but the lever that closed most of it (deeper selectivity) ran out of usable sample before
closing the rest. Recommended config if revisited: `min_wick_depth_atr=5.0` or `4.5`. Verdict:
this specific lever (wick-depth selectivity) is exhausted; further progress on value-area
rejection fades would need a different lever (lower-cost execution, multi-day confluence, or real
orderflow/footprint confirmation), not another depth sweep.

**Pre-live message clarity pass, the night before the first live day: a phase tag on every
Telegram send, four wording fixes, and the messaging system's standardization documented.**
Prompted by reviewing one real example of every message type the engine sends (24 messages,
across every channel) and asking "would this confuse a trader" with real money starting
tomorrow. Five changes, all in `notifier.py` / `analysis/breakingtrade/{alerts,eod_summary}.py`:

- **`[PAPER]` / `[LIVE]` tag on every outgoing message.** Until now, whether a message
  described real or paper money depended entirely on which physical Telegram channel it
  landed in — nothing in the text itself said so (except the startup banner). Added at a
  single new low-level chokepoint, `notifier._deliver()`, that all four of the module's
  independent send paths (`notify()`, `_mirror_to_strategy()`, `_send_and_pin_day_summary()`,
  `_send_oneshot()`) now funnel through — a new notify_* function or send path gets the tag
  automatically, it never has to know it exists. `alerts.py`'s `send()` (the BreakingTrade bot
  path) carries the same tag via its own small copy of the same convention.
- **`NO-PROGRESS EXIT (stalled)` / `TIME EXIT (session cutoff)`.** Both are forced closes with
  no SL/TP hit; without the qualifier a trader can't tell "price never moved" apart from "the
  clock ran out" at a glance.
- **`orphaned_position`'s wording no longer implies a fill might still arrive.** "ORDER NOT
  FILLED" read as pending; it is terminal. Now: "NO POSITION TAKEN | ... Final - order did not
  fill ... No further action from the engine."
- **`be_stop_applied` promoted from `normal` to `quiet` tier.** The stop moving to break-even
  changes the trader's actual risk exposure, unlike the purely mechanical `order_placed`/
  `sl_placed` steps it used to share a level with — a quiet channel shouldn't hide it.
- **BreakingTrade's watchlist scorecard retitled** `BT WATCHLIST SCORECARD` (was `BT EOD
  SUMMARY`) — it scores direction-only calls that were mostly never real trades, a different
  thing from `notifier.py`'s real-P&L day summary and the BTST basket summary; the shared
  "EOD SUMMARY" title let a trader conflate the three.

**Messaging standardization, documented rather than rebuilt.** Investigated whether to unify
message formatting for future strategies; found the main pipeline (any strategy wired through
`main.py`) is *already* fully standardized — every `notify_*` function is strategy-agnostic, so
adding a new strategy needs zero new message code, just a `strategies.REGISTRY` entry and its
`config.yaml` channels (now spelled out explicitly in `notifier.py`'s and `strategies.py`'s
module docstrings). The one real gap was `strategies/examples/momentum_rank_strategy.py`'s
Telegram-sending code being untested — it can't import `signal_engine.notifier`/`alerts`
(single-file constraint: OpenAlgo's `/python` Strategy Host takes one uploaded `.py` file), so
the fix there is a documented copy-paste reference (`_send_telegram()`'s own docstring, cross-
referenced from `alerts.py`), not a shared import. 3 new regression tests for `_send_telegram()`
close the coverage gap.

23 new tests (16 in `signal_engine/tests/`, 3 in `strategies/examples/tests/`, plus 4 existing
assertions updated for the new tag); both suites green (1608 and 69 respectively — run
separately, per each file's own documented command; a combined run hits a pre-existing
`openalgo` package-name collision unrelated to this change).

## Recent Changes (2026-09-18)

**Automated daily EOD regression check, posted to Telegram** (`signal_engine/analysis/eod_review.py`,
new). Every manual EOD review to date (EOD-ANALYSIS-2026-09-11.md, the 2026-09-17 review fixes
above) was a human sitting down and cross-checking trades.db against the broker, grepping logs
for notifier failures, and reading the Telegram channels back by hand — the same three checks,
run once someone remembered to ask. This automates exactly that, in three categories:

- **TRADE** — the day's ledger holds together (no unfilled entry orders, no unmatched broker
  fills, nothing still open at EOD — reusing `analysis/ledger.py`'s existing flags) and
  `reconcile.py`'s engine-vs-broker P&L canary agrees.
- **SIGNAL** — did messages this system claims to send actually leave: notifier warnings
  (queue full, flush failure, mirror failure) grepped from today's log; BreakingTrade's own
  delivered/undelivered count from its `alerts` table; and a read-only check that the Telegram
  session is still authorized and the admin channel OpenAlgo is currently routing to is
  reachable (a COPY of `telegram.session`, never the original — same pattern as
  `tests/test_telegram_integration.py`).
- **SYSTEM** — config sanity and risk-DB reachability (reusing `smoke_test.py`'s own checks),
  ERROR+ lines in `errors_DAY.jsonl`, ERROR+ lines in OpenAlgo's own `log/errors.jsonl` for
  today, and FD/DB-lock signatures in the day's logs.

The verdict posts to `settings.notify_channel` — the SAME admin channel
(signal-engine-analyze/-live) the live engine's own day summary already uses, picked by
OpenAlgo's CURRENT analyze/live mode — via the Telegram Bot HTTP API (the shared
`BREAKINGTRADE_BOT_TOKEN`), not the engine's own Telethon client: this runs as a standalone
process, and `alerts.py`'s docstring already established why two processes must never share
one Telethon session file. Falls back to whichever phase's channel IS configured, same rule
as `notifier.py`'s `_channel_for_phase()`. Wired into `analysis/eod.sh` as a new section (the
cron job that already runs at 15:25 IST weekdays needs no separate scheduling). Run by hand
with `PYTHONPATH=. uv run python -m signal_engine.analysis.eod_review [DAY] [--dry-run]`.
26 new tests, full suite 1592 green.

**value_area_fade selectivity sweep continued deeper (2.75/3.0/3.5/4.0 ATR wick depth) — the
improving trend from 2026-09-17 keeps going, still hasn't crossed into viable.** Direct
continuation of the previous day's investigation (see 2026-09-17 below for v1-v4). Full-universe
16bps t-stat improves at every depth tested: -15.20 (depth 2.5) → -12.31 → -10.76 → -7.74 →
**-5.34 at depth 4.0**, the least-extreme result of any version tried. Gross edge rose from 3.48
to 5.77 bps/trade; dispersion improved from 18/210 to 71/210 net-positive symbols. More
importantly, breakeven cost itself moved: depth 2.5 flipped negative by 4bps, depth 4.0 doesn't
clearly flip negative until somewhere between 4-8bps — closing part of the gap to the real 16bps
cost (now ~3-4x short, down from ~16x at depth 2.5), though not closing it. Sample size is NOT
yet the binding constraint (978 IS / 968 OOS trades at depth 4.0, both above the 300-500-trade
noise floor this investigation uses) — the trend was still improving when the tested range ended,
not because it plateaued but because that was the specified range. Whether continuing to depth
5.0+ is worth a further ~65-minute full-universe sweep, chasing a narrowing but still-real gap
against an increasingly thin sample, is a judgement call for next time. Full tables in
`STRATEGY-ANALYSIS.md`, v5 section.

## Recent Changes (2026-09-17)

**9:15 Opening-Candle Scalp ("Open Drive") backtested and it loses — t = -8.04, before-cost edge
is already flat.** New Python-native adapter (`signal_engine/backtest/strategies/open_drive.py`,
no PineScript) testing the "open above/below previous Value Area + hold-confirmation + break of
the opening candle" hypothesis from two independent LLM design passes, across all 212 F&O
symbols Historify holds 1-minute bars for, 2023-01-02 to 2026-09-13 (IS/OOS split at
2025-03-19). Result: net R -0.128/trade, t = -8.04 on 1,149 trades — high win rate (73.3%) but a
backwards payoff (avg TP +0.094R vs avg SL -1.112R), because the stop is anchored to an
already-oversized opening candle while the target is a flat 1x ATR. The ablation's honest
surprise: both LLM passes' central filter (open above/below the prior Value Area) HURTS results
in both the in-sample and out-of-sample windows here — removing it roughly doubles trade count
and improves gross bps in both. Hold-confirmation, the open≈extreme wick check, and the volume
filter all show the opposite (in-sample-only improvement when removed = overfitting signature),
so those stay. Full tables, the 5-minute run's invalidating config bugs, and next-step
suggestions in `pinescripts/intraday/open-drive/STRATEGY-ANALYSIS.md`.

Building this also surfaced two real bugs, now fixed with regression tests in
`signal_engine/tests/test_volume_profile.py`: (1) the previous-session Value Area shift logic
in the new `volume_profile.py` module silently mis-mapped the day after a too-short/skipped
session; (2) the third-party `MarketProfile` library crashes on a real, non-rare data pattern —
a legitimately zero-volume price bucket between the POC and one edge, which its bare-truthiness
"no buckets left" check cannot distinguish from a genuinely empty side.

**Open Drive v2, rebuilt against this repo's own documented volume-profile model, is dramatically
worse than v1 — t = -81.67, 0 of 210 symbols net-positive.** Rebuilt the adapter to implement the
actual 6 named setups from `pinescripts/intraday/volume-profile/volume-profile-model.md`
(VAH-ACC/VAL-ACC acceptance, VAH-RT/VAL-RT retest, VAH-REJ/VAL-REJ rejection) with the model's own
stop rule (level in play, ATR-buffered) and target rule (nearest structural level: POC/VAH/VAL/
PDH/PDL/IBH/IBL), instead of v1's invented "break of the opening candle" trigger. Same universe
and period as v1. Result: 185,153 trades, net R -0.334/trade, t = -81.67, **every single symbol
net-negative** — worse than v1 on every axis, including a real negative edge even at 0 bps cost
(v1's was merely flat before cost). Mechanism: the "nearest structural level" target is, on
1-minute bars, almost always a few paise away (especially the still-forming Initial Balance
high/low), so 70.5% of trades touch TP for essentially zero R while the 24.5% that hit SL lose a
full R+ — the model's own text is explicit that its footprint/orderflow confirmation is "the
ONLY discretionary decision... no footprint confirmation, no trade, regardless of score", and
automating everything except that human gate does not produce a weaker edge, it produces a
negative one. One lead survived two independent ablation cuts: **rejection alone** (fading a
failed break, not betting on continuation) is the least-bad setup in both IS and OOS — still net
negative, but the only result in this exercise that improved in both windows twice. Full tables
and the two realistic paths forward (discretionary alert tool with human confirmation, or a
fresh rejection-only design) in `pinescripts/intraday/open-drive/STRATEGY-ANALYSIS.md`.

**The rejection-only lead, rebuilt as its own strategy with a purpose-built stop/target
(`signal_engine/backtest/strategies/value_area_fade.py`) — first version with a real signal
before cost, still not viable at realistic cost.** Target is now POC specifically (floored at a
minimum R so a close POC can't reproduce v2's "trivially cheap target" bug), no continuation-style
VWAP filter by default, both of the model's entry windows (09:15-11:00 and 13:00-14:45). At 0 bps
cost: t = -1.30, total R +423 — statistically indistinguishable from zero, the first result in
this whole exercise that isn't confidently wrong before cost. But a mere 4 bps (a quarter of the
16 bps shipped assumption) flips it to t = -18.64, and by 16 bps: t = -68.98, 0 of 210 symbols
net-positive. Diagnosis is now entirely about frequency vs edge size, not direction: 78,040 trades
over 906 days (~1 every 2-3 sessions per symbol) against a raw edge of ~1 gross bp/trade — roughly
an order of magnitude too thin to survive real NSE intraday cost, not a filter-tuning gap. Best
ablation combo found (VWAP filter on + wider target floor) only reaches ~2 gross bps. Full tables
in the same `STRATEGY-ANALYSIS.md`, v3 section.

**Made the fade trigger more selective (`min_wick_depth_atr`, requiring the wick to clear a
minimum ATR-multiple past the level) — best gross edge of all four versions, first genuinely
positive raw signal in the investigation, still not viable at real cost.** Full-universe result
at `min_wick_depth_atr=2.5` + VWAP filter on: 8,003 trades (vs v3's 78,040), gross 3.48 bps/trade
(vs v3's 0.81), t=-15.20 at 16 bps (vs v3's -68.98) — meaningfully better on every axis. At 0 bps:
t=+1.15, the first *positively* significant result of any version (v3 was merely flat at t=-1.30).
Breakeven cost is ~1-2 bps against the real 16 bps this repo's cost model uses — an order of
magnitude short, not a rounding gap. Dispersion improved to 18/210 symbols net-positive (from
v3's 0/210), still a small minority. Along the way, a 3-symbol sample sweep found a
different-looking "best config" (looser volume filter, wider stop) that a full-universe ablation
then showed was noise — reverting both to v3's original defaults scored better at scale. Exactly
the overfitting trap `harness.ablation()`'s "helps BOTH windows" rule exists to catch.
Full tables and the overfitting-trap detail in `STRATEGY-ANALYSIS.md`, v4 section.

**Full system check confirmed all fixes from 2026-09-16 held through the first live cycle.**
8:50 AM `AutoStart` fired automatically and succeeded end-to-end (network check, NTP wait,
app.py health, broker auto-login via existing session reuse, signal engine start) — the
first fully automated, unattended boot since the executable-bit fix. `Watchdog` and
`HeartbeatCheck` both ran clean with nothing to do. Three non-fatal warnings logged during
boot (a transient empty-funds-data auth check that retried successfully, a SocketIO
`NoneType.emit` error during master contract refresh, and unrelated Yahoo Finance lookup
failures for a delisted symbol) — none blocked startup.

**Schedule change (requested) — squareoff moved 3:02 PM -> 2:55 PM, autostop moved
3:30 PM -> 4:00 PM** (the later stop gives the momentum-rank EOD scan runway after market
close). Confirmed live: all 5 tasks' triggers now read `AutoStart` 8:50 AM, `Watchdog`
9:00 AM-4:00 PM, `HeartbeatCheck` 9:05 AM-4:00 PM, `SquareOff` 2:55 PM, `AutoStop` 4:00 PM.
The 2026-09-16 note about `AutoStop`/`Watchdog` being blocked by a Windows permission error
no longer applies — a subsequent elevated re-run of `createTaskOpenAlgoScheduler.ps1`
applied both. Squareoff firing before (not after) the engine's own 3:00 PM exit is still a
live behavior change — see the flagged note under Startup & Shutdown.

**Found and fixed the same executable-bit bug in the separate BreakingTrade automation.**
While checking `breakingTradeAutoStart`/`AutoStop`/`Watchdog` (a parallel Task Scheduler
setup driven by `breakingtradectl.ps1`, controlling the standalone
`signal_engine.analysis.breakingtrade --watch` process — distinct from the `openalgoctl`
stack), `poller.sh` and `scan.sh` were found tracked in git as mode `100644`
(non-executable), identical to what caused the 2026-09-15/16 outage. They were only still
working because the on-disk copies happened to still be `+x`; the next checkout or edit
would have silently broken all three breakingtrade tasks the same way, with the same
false read that "it just isn't running" and no alert. Fixed: executable bit restored and
`breakingtradectl.ps1` now invokes both as `bash <script>` instead of `./<script>`.

## Recent Changes (2026-09-16)

**Startup/watchdog stack hardened after a silent 20+ hour outage.** `openalgoctl.sh` lost
its executable bit on 2026-09-15 (git tracked it as non-executable); every scheduled
start/watchdog attempt since then failed instantly with `Permission Denied`, before any
log line or Telegram alert could fire — the stack was down through the first ~3 hours of
today's session with zero notification. Fixed the immediate cause and five related gaps
found while tracing it: the invocation no longer depends on the executable bit surviving a
checkout; a new `heartbeat.txt` file plus an independent `openAlgoHeartbeatCheck` Task
Scheduler task (no WSL/bash/Python dependency) now catches this failure class directly;
the recurring Windows "Open File - Security Warning" popup (helper `.bat` launched from
the `\\wsl.localhost\...` path) is gone — it now runs from `$env:TEMP`; the daily 3:30 PM
scheduled stop no longer misreports itself as an `app_crash`; the missing `openAlgoSquareOff`
3:02 PM failsafe task was re-registered; and `kill_from_pidfile`'s process match was
narrowed so a routine restart can no longer collaterally kill an unrelated
`signal_engine.*` tool (e.g. the BreakingTrade watcher). Full detail in the "Dead-man's
switch and the 2026-09-15/16 silent outage" section under Startup & Shutdown below.
Deferred: migrating the bash+PowerShell+Task Scheduler supervision to WSL2 systemd
services, which needs a WSL restart.

## Recent Changes (2026-09-15)

**BREAKINGTRADE-WATCHLIST's P&L is no longer double-counted against BREAKINGTRADE on a
symbol both fired on the same day.** BREAKINGTRADE and BREAKINGTRADE-WATCHLIST collide by
design (same scanner, same daily-changing universe — see `config.yaml`'s
`duplicate_window_seconds` comment), and on 2026-09-15 both fired on LTM, SUPREMEIND and
HDFCBANK within minutes of each other. The engine tracked these as two independent
per-strategy P&L legs, but the broker/sandbox nets positions by (symbol, exchange, product)
alone — `sandbox.position_manager.get_position_for_symbol()` has no strategy argument — so
it was really ONE blended broker position the whole time. That mismatch between per-strategy
engine accounting and per-symbol broker accounting produced the day's first-ever
reconciliation-canary trigger (`reconcile.py`, built after the 2026-09-11 incident):
engine -1,573.43 vs broker -1,127.04, a -446.39 gap. Fix: `tracker._dedupe_breakingtrade_overlap()`,
applied in `_day_from_db()`, drops BREAKINGTRADE-WATCHLIST's leg for any symbol BREAKINGTRADE
also traded that day before the day summary / per-strategy Telegram reports are built — a
non-overlapping symbol is unaffected either way. Reporting-layer only: `reconcile.py`
deliberately still reads the full, undeduped `trades.db` so the integrity canary keeps seeing
every real close, and no order-placement behavior changed — the underlying broker-side netting
(the deeper fix, which would change what orders get placed) is unresolved and matters before
enabling the `-live` counterparts of both strategies together. Tests: `test_breakingtrade_overlap_dedup.py`.

**Startup watchdog no longer kills and relaunches its own in-progress boot.** Postmortem for
this morning's 5 failed OpenAlgo launches (09:50-10:20 IST, "operation was canceled by the
user", engine didn't start until 10:27 — missing ORB's opening-range window entirely for the
day): the Windows Task Scheduler watchdog fires every 5 minutes and used to unconditionally
`taskkill` any old service window and run `openalgoctl.sh stop` before every relaunch, even
when the previous attempt was merely still mid-boot (NTP wait + broker login legitimately
takes 1-3 minutes) rather than dead — a self-inflicted kill/relaunch livelock. Fixed with a
`flock`-based single-instance guard (`acquire_lock()` in `openalgoctl.sh`), now the sole
authority on "is a start already in progress or running" for every entry point (watchdog,
manual `openalgoctl.ps1` run, direct WSL invocation); `openalgoctl.ps1` no longer pre-emptively
kills anything and detects an early window exit as a likely duplicate-start refusal instead of
waiting out the full health-check timeout; `createTaskOpenAlgoScheduler.ps1`'s
`ExecutionTimeLimit` raised 3m -> 10m so Task Scheduler itself doesn't hard-kill a legitimately
slow boot (`MultipleInstancesPolicy=IgnoreNew` was already in place as a second layer). One gap
closed in the same fix: the lock originally used `flock -n` (instant refusal) — `cmd_restart`
kills the old run's app/signal PIDs and comes straight back to `acquire_lock`, but the OLD run
process only notices its children died on its next 5s poll, then can spend up to another 10s in
`cleanup()`'s shutdown-notification timeout before it actually exits and releases the flock. An
instant refusal there would have made a plain `restart` fail as a spurious "duplicate" most of
the time. Changed to a bounded `flock -w 20` (overridable via `LOCK_WAIT_SECS` for tests) —
still refuses a genuine duplicate, just tolerates the old process's teardown window. Tests:
`test_openalgoctl.sh`'s `acquire_lock` section (free / brief-hold / long-hold cases).

**Backtest roundup across all 10 candidate strategies — final promising/not-promising call.**
Closing out the "go through the remaining 9" backtest pass (plus `breakout.pine`'s own
standalone adapter, added the same session) with a single verdict table and a fine-tuning plan
for what survived, rather than leaving the result scattered across each strategy's own
`STRATEGY-ANALYSIS.md` / changelog:

| Strategy | Verdict | Why |
|---|---|---|
| `swing/momentum-rank/momentum-rank.pine` | **Promising — top priority** | 36.9% CAGR over 105 simulated months, +10.7%/yr alpha over an equal-weight benchmark, Sharpe 1.43. Survives 7x realistic costs (+8.2%/yr floor) and every lookback/rebalance/portfolio-size sweep. Never traded live. |
| `intraday/orb/orb.pine` | **Promising — marginal, execution-bound** | Two real bugs fixed (R:R had collapsed below 1.0; volume filter was inert). Honest live number is +33% over 186 trades, 62.7% win rate, but the edge flips negative past ~19 bps slippage against an ~11 bps break-even from statutory costs alone — slippage, not the signal, decides the sign. Only holds up at 1-2 concurrent slots; 3+ goes negative in testing. Fixes not yet compiled on TradingView. |
| `intraday/orb/breakout.pine` (`BREAKOUT` tag) | Do not trade | Standalone adapter, full 212-symbol universe, 2016-2026: **zero of 212 symbols net profitable**, both IS and OOS negative (t = -62.84 / -49.62), and still a significant loser at zero trading cost (t = -5.69). Extends the 2026-08-30 finding (key-level breaks follow through 30.9% vs. a 33.3% random-walk baseline) across the full live filter stack. Currently live only as deliberate forward-data collection, not an expected edge. |
| `intraday/ema9/ema9-intraday.pine` | Do not trade | All 6 tested variants (shipped script + 5 from the source PDF) lose money before costs. |
| `intraday/ib-extension/ib-extension.pine` | Do not trade | Only 66/200 symbols profitable; the 1x-range target is mechanically unreachable most days given the stop size — a geometry problem, not a tunable parameter. |
| `swing/dividend-growth/dividend-growth.pine` | Do not trade | The 76% win rate is manufactured — the strategy block only closes profitable positions, so real losses sit as unrealized "open trades" and never count against it. |
| `swing/gap-rsi` ("1:10 R:R" video) | Not shipped, no edge | Short side robustly negative (0 of 11 years positive). Long side is a coin flip, statistically indistinguishable from zero (95% CI crosses zero). No `.pine` file was written. |
| `swing/ema-pullback` | Not yet tested | Script exists; needs 10 years of split-adjusted Historify data pulled before `backtest ema_pullback --full` can run. |
| `intraday/volume-profile` | Not yet tested | Still in design/validation phase — footprint-reconstruction accuracy study has to land first; no `.pine` strategy file yet. |

Fine-tuning plan for the two promising ones:
1. **`momentum-rank`** — compile on TradingView (untested there), re-run the robustness sweep
   specifically on the shipped 8-of-40 config (the strong numbers above are mostly from the
   larger 30-of-201 test), then paper-trade one full rebalance cycle before funding. Open risk
   to decide on explicitly: no crash protection (-29.4% in Feb-Mar 2020, in line with the
   universe) — decide whether a market-regime override belongs in v1 before scaling size.
   **Update 2026-09-15**: fine-tuning sweep re-run against Historify (OpenAlgo's own
   broker-verified daily store), 300 configs on the full 197-name F&O universe plus 180
   configs on the actual deployable 40-name Pine universe — see the dated entry in
   `swing/momentum-rank/STRATEGY-ANALYSIS.md`. Two findings: (a) Historify's daily history
   for this universe only reaches back to 2019-12, not 2016, so the 36.9% CAGR / +10.7%/yr
   headline above is **not yet reproducible against the platform's own broker data** and
   should be treated as unconfirmed until it is; on the shorter window the shipped config's
   OOS alpha is a statistically insignificant +4.55%/yr (t=0.71). (b) The one robust,
   cross-validated improvement found was slowing the rebalance cadence from 21 to 30
   sessions (~monthly to ~six-weekly) — shipped into the `.pine` default — which cut max
   drawdown 19.3% -> 14.3% and raised OOS alpha to +7.50%/yr, consistent across both
   universes tested. Still candidate/paper-only; the rest of this plan (TradingView compile,
   paper-trade a full cycle, decide on a crash-regime override) is unchanged and still open.
   **Update 2026-09-15 (follow-up)**: the 40-symbol-restricted framing above was
   superseded — backtests must use the full F&O universe, not a subset chosen for a
   deployment constraint (see `feedback_backtest_full_universe.md` in memory). Re-read
   with the full 197-name universe as authoritative: top 12 of 197, lookback 300, rebal
   30 gives +11.0%/yr OOS alpha (t=1.38) and +25.7%/yr all-period alpha (t=3.59) at 15.5%
   max drawdown, vs. the 40-name Pine cap's +0.06%/yr OOS / +12.3%/yr all-period / 32.7%
   drawdown. Since Pine cannot hold more than 40 names, closing this gap means moving off
   Pine — user chose to migrate. **Phase 1 shipped**: `strategies/examples/
   momentum_rank_strategy.py` (single self-contained file — the `/python` host only
   accepts one uploaded `.py` — unit-tested including numerical
   parity with `portfolio.py`'s `build_factor`) ranks the full universe via OpenAlgo's
   own `history` API and posts a human-readable digest to its own Telegram bot/chat —
   deliberately NOT the machine-parseable alert format, so the trader still places CNC
   orders manually (auto-execution would today route through `signal_engine`'s global
   sizing mode against a fake SL, since unregistered strategy tags silently fall through
   to defaults rather than erroring — `main.py:208`). Phase 2 (auto-execution: a new
   per-strategy sizing-mode override in `risk.py`, `strategy_profiles.MOMENTUM-RANK`
   registration) is explicitly deferred, not started. See the STRATEGY-ANALYSIS.md
   follow-up entry for full detail.
2. **`orb`** — compile the R:R/volume-filter fixes on TradingView, then attack slippage
   directly (limit vs. market fills on entry/exit) using the same method as the
   `intraday-slippage-analysis` learned pattern. Keep position sizing capped at 1-2 concurrent
   slots until live data confirms the backtest edge past that limit.

## Recent Changes (2026-09-14)

**Backtest OOM fix: the full 11-strategy registry now runs to completion; a standalone
BREAKOUT adapter shipped alongside it.** Prior full-universe runs crashed with OOM on this
7.8GB box regardless of which strategy — root cause was `data.from_historify()`'s 1m->5m
resample using a per-day `groupby().apply()` (~535,000 tiny pandas calls across 212 symbols x
~2,500 trading days), which fragments the process heap badly enough that RSS ratchets upward
for the life of the run independent of live data size. Fixed with a single vectorized
`.resample(..., origin=<09:15>)` call per symbol (verified bit-for-bit identical output,
36-106x faster), float32 downcasting of both raw OHLCV and every strategy's own derived
indicator columns (`Ctx` in `signal_engine/backtest/types.py`), and a new `release_raw` flag
on `Backtest` (`harness.py`) that drops each symbol's raw frame once its indicators are
prepared. None of this changed any strategy's reported numbers — verified bit-identical on
`orb` before/after. Peak memory across all 12 strategies now ranges 4-6.6GB, no crashes.

Full-universe (212 F&O symbols, 2016-2026, 16 bps cost) results, OOS row:

| Strategy | OOS t | OOS net_R | Profitable symbols | Read |
|---|---|---|---|---|
| `breakout` (live BREAKOUT tag) | -49.62 | -0.493 | 0/212 | Negative even at 0 cost — see below |
| `key_level` | -33.67 | -0.450 | 1/212 | Cost-fragile weak gross edge |
| `ema9_pdf` | -30.42 | -0.344 | 0/212 | No edge before cost |
| `ema9_vwap` | -31.22 | -0.204 | 0/211 | Real gross edge (t=9.23 at 0 cost), erased by volume of trades |
| `ema9` | -22.12 | -0.151 | 1/211 | No edge before cost |
| `orb` | -17.27 | -0.156 | 6/211 | Small real edge, erased by cost |
| `dhb` | -4.80 | -0.085 | 67/210 | Mildest loser, consistent gross edge, worth revisiting on cost |
| `gap_rsi` | -1.84 | -0.168 | 59/195 | Indistinguishable from zero (t below 1.97 bar) |
| `phoenix` | +1.88 | +0.094 | 119/168 | Promising, just under significance, small OOS n |
| `value_zone` | +3.24 | +0.021 | 161/196 | **Strongest positive result** — consistent both windows, 82% symbols profitable, low cost sensitivity |
| `ema_pullback` | -5.49 | -0.158 | 102/195 | **Overfit**: IS gross +262bps flips to OOS loss |
| `ib_extension` | -8.15 | -0.089 | 30/211 | Was reporting 0 trades — bug, now fixed (see 2026-09-14 entry below). Real result: significant loss even at 0 cost (t=-3.49) |

**BREAKOUT got its first-ever standalone backtest adapter** (`signal_engine/backtest/strategies/breakout.py`),
separate from the pre-existing `key_level.py` research tool because cross-checking the latter's
claimed defaults against the current `breakout.pine` source found two drifted values (CLV gate
0.65/0.35 vs the live 0.50/0.50; missing `klMaxTpR` reachability gate added 2026-09-06). Full
finding and methodology: `signal_engine/pinescripts/intraday/orb/breakout.md`'s 2026-09-14 entry.

Also: 10 non-strategy `.pine` files (indicator/overlay/dashboard scripts with no trading logic
of their own) renamed with category prefixes (`indicator-`, `concept-`, `report-`) for clarity;
all cross-references updated. No strategy or backtest code path touches these files.

**`ib_extension`'s zero-trades result (2026-09-13 entry below) was a bug, not a finding.**
`entry()`'s regime gate (`p.use_regime and c["regime"][i] != direction`) rejected every signal
in both directions because `self.regime` was never populated — `load_regime()` (Nifty-50 vs its
50 EMA) existed in the file but nothing ever called it, so `prepare()`'s fallback filled the
`regime` column with 0.0 for every bar, and `0.0 != 1` and `0.0 != -1` are both always true. No
error, no crash, nothing in a log — the same silent-failure shape as the IST bug this project has
already been burned by once (see root CLAUDE.md). A second, compounding bug: even with regime
loaded, `load_regime()` only fetched `period="2y"` of daily Nifty data against a ~10-year
Historify backtest, and the gate did not special-case the documented `0 = unknown regime` value
from unmapped dates as a pass — it would have kept blocking ~8 of 10 years even after the wiring
was fixed. Fixed both: `IbExtension.__init__` now loads regime automatically (`period="max"`,
covers 2007 onward), and the gate now reads `c["regime"][i] not in (0.0, direction)` so unknown
regime passes through instead of blocking. Real OOS result: n=3,775, net_R=-0.089, t=-8.15 —
significantly negative, and still significantly negative at zero trading cost (t=-3.49, payoff
0.74-0.79) — consistent with the strategy's own docstring warning that its stop/target geometry
gives a payoff below 1:1 even at the best-case entry. 30/211 symbols profitable.

## Recent Changes (2026-09-13)

**Intraday watchlist (ORB/BREAKOUT/EMA9VWAP) rebuilt from live technical metrics, not
performance grades; sector-rotation-map scoped to swing strategies only.** The dated
Q1/H1 ORB performance reports were removed from the repo (stale, and the user did not
want future selection biased by old market conditions) — `analyze_orb.py`, the tool
that regenerates such a report from a fresh Telegram export, was kept. The shared
`signal_engine/pinescripts/intraday/intraday-stocks-watchlist-tradingview` file is now
built by a repeatable screen (median daily traded value >= Rs 100cr, median ATR% of
price >= 0.15%, current 60-day 5-min bars via `data.load(refresh=True)`, top 24 by
ATR%) rather than hand-graded. Recommended cadence: monthly, matching the existing
blacklist review cadence elsewhere in `config.yaml` — not static, and not weekly (the
ranking was stable across a 2-week re-check this session).

Separately, `sector-rotation-map` (an external RRG dashboard, OpenAlgo-powered) was
evaluated as a possible stock filter. Its calculation was verified correct by an
independent reimplementation, but its verdict is scoped to swing strategies only
(`rsi-tp-mr`), not intraday: RRG is inherently a weekly-bar technique (52-week rolling
window) and its value for a 5-minute entry was never tested. Full reasoning and the
current candidate list in `signal_engine/pinescripts/intraday/ema9-vwap/STRATEGY-LOG.md`.

**The watchlist screen above is now a scheduled job, not a manual script — triggered
by startup, not a fixed cron hour.** `signal_engine/scripts/watchlist_screen.py`'s
`maybe_run_monthly_screen()` is called from `openalgoscheduler._run_startup()` as its
LAST step (a fixed clock time assumes the machine is on then, which a laptop is not
guaranteed to be), gated by `signal_engine/data/watchlist_screen_state.json` so it
only does real work once per calendar month regardless of how many times startup runs.
It posts a digest to `notify_channel` — sent, not just formatted; verified with real
runs during this session — and rewrites the repo's watchlist file, but does **not**
touch TradingView (no public API for that); updating TradingView from the Telegram
message stays a manual step. Running as startup's last step also means the Telegram
send is sequential with (never races) the live listener for their shared Telethon
session file.

**Screen criteria expanded from two factors to four, researched against public
practice and NSE-specific rules, not assumed.** Liquidity (traded value, not market
cap) and ATR% alone missed two real gaps: no check for whether a stock's activity is
CURRENTLY elevated (added relative volume, 5-day vs 30-day baseline — 15 of the prior
month's 24 picks failed this), and no NSE-specific tradeability gate (added a live
check against NSE's own ASM/GSM surveillance endpoints — some stages carry 100% margin
or block intraday trading outright, independent of how well a stock otherwise
screens). Also added beta vs NIFTY (1.0–2.0 band) as a distinct measure from ATR%: one
prior pick scored highest on ATR% and 0.60 on beta, meaning its volatility was almost
entirely uncorrelated with the market. Full sources and the funnel counts in
`ema9-vwap/STRATEGY-LOG.md`'s 2026-09-13 03:30 entry.

**Cadence moved from monthly to weekly, with conviction tracking added — measured,
not assumed.** Recomputing the liquidity/ATR%/RVOL pool at different points in the
same 60-day window: day-to-day overlap ~74%, but week-to-week overlap only **~39%**
— more than half the pool turns over within a week, mostly RVOL's own 5-day window
reacting to single high-volume days. A monthly screen took one such noisy snapshot
with no way to tell a real trend from a one-day fluke. Now weekly (ISO week), and
every run's symbols are appended to a rolling 4-run history
(`signal_engine/data/watchlist_screen_state.json`) so each candidate is annotated
`(N/4)` — how many recent runs it appeared in — in both the Telegram digest and the
watchlist file. ASM/GSM exclusion confirmed (not changed) as watchlist-level: the
broker already refuses orders on those names, so keeping them out of the candidate
list upstream is the right layer rather than relying on the broker's rejection
downstream. Full churn data in `ema9-vwap/STRATEGY-LOG.md`'s 2026-09-13 03:51 entry.

**Split further into compute-daily/decide-weekly** after feedback that bundling all
four factors onto one cadence was itself the mistake — cost only applies to beta
(a full year of per-symbol daily bars), not to liquidity/ATR%/RVOL, which are nearly
free on top of the 60-day pull already needed. `run_daily_scan()` now runs on every
startup and only updates a rolling 10-day history; `run_weekly_screen()` runs on top
of that once a week and is the only part that fetches beta, writes the watchlist
file, and sends Telegram. Conviction resolution improved from 4 weekly points to 10
daily ones. Also fixed a ranking bug this surfaced: sorting by conviction alone made
every symbol tie on the first run and silently fall back to alphabetical order —
today's (ATR%, RVOL) is now the explicit tie-break. Full reasoning (with a
plain-language explanation of why the four factors don't share one clock) in
`ema9-vwap/STRATEGY-LOG.md`'s 2026-09-13 15:34 entry.

## Recent Changes (2026-09-12)

**`max_open_positions` is dynamic in LIVE mode — no more manual edits as capital grows.**
`mode_profiles.live.max_open_positions` is now `-1` ("dynamic") instead of a fixed `2`.
`RiskEngine._dynamic_max_open_positions()` (risk.py) computes the real ceiling on every
exposure check from: live capital (already fetched from the broker each day),
`sizing.min_capital_for_entry` (reused as an always-uncommitted safety buffer, not a new
config key), `broker.mis_margin_pct`, `sizing.risk_per_trade`/`slippage_factor`, and the
TIGHTEST `strategy_profiles.<TAG>.min_sl_pct` among every `strategies.REGISTRY`
`tradeable=True` strategy (`runtime._worst_case_live_sl_pct()`).

Key finding behind this change: risk-based sizing (qty = capital × risk_pct / SL-distance)
means the margin ONE position needs, as a % of capital, does not depend on capital OR stock
price at all — only on risk_per_trade, mis_margin_pct and the SL%. So a capital→position-count
bracket table (25K→2, 50K→3, ...) would have been arbitrary, not derived. With today's config
(BREAKOUT/BREAKINGTRADE/BREAKINGTRADE-WATCHLIST at a 0.20% min_sl_pct floor), the dynamic
ceiling resolves to 1 at every capital level from ₹25K to ₹10L+ — two such tight-stop
positions can never fit simultaneously (2 × ~91% margin > 100%), at any account size. That is
the correct, safe answer: a bigger account takes a proportionally bigger position too. The
ceiling only rises above 1 once a wider-stop strategy (e.g. ORB, currently not live) is the
one actually promoted, or `margin_reserve_buffer`'s relative weight shrinks enough at very
high capital — both handled automatically, no config edit required either way.

0 still means "unlimited" (used by `analyze`). -1 is a new, distinct sentinel so a config typo
of 0 in LIVE can never silently mean unlimited. `RiskEngine.effective_max_open_positions_for()`
is the new public accessor every caller outside risk.py (notifications, smoke test) must use
instead of reading `.max_open_positions` directly, since that raw attribute can now be -1.

## Recent Changes (2026-09-11)

**Concentration caps are PER STRATEGY, in both modes.** A strategy may not re-enter a name it
already holds - that is averaging into a position its own rules already sized once, doubling
that strategy's exposure with no second decision behind it. A DIFFERENT strategy may take the
same name: the two reached it by different logic, each with its own stop and target, and each
is a position a trader would genuinely have taken. Deliberately identical in LIVE, unlike the
capital and slot counters which pool there - "has THIS strategy already got a position in this
name" is a question about the strategy either way. The exposure it creates is real and
`max_positions_per_sector` is the control for it (currently off).

**Outcome mirroring covers every channelled strategy.** It is driven by `strategies.REGISTRY`
rather than a hardcoded list, so ORB, BREAKOUT, BREAKINGTRADE and BREAKINGTRADE-WATCHLIST all
get their outcomes in their own channel automatically; EMA9, EMA9VWAP and RSI-TP-MR have no
channel by design. All nine mirrored events pass the strategy tag through.

---

**One position per stock, globally, in both modes.** The symbol/sector caps were briefly made
per-strategy in ANALYZE so the BREAKINGTRADE vs BREAKINGTRADE-WATCHLIST comparison could run -
the two are built to call the same names. Reverted: a trader holds ONE position in a stock,
and analyze has to mirror live or it is not evidence. Two strategies both long the same name
is one position with two labels; counting it twice overstates the sample, doubles the real
exposure and produces a paper result live could never reproduce. The cost is stated rather
than hidden - confirmed-vs-watchlist has to be answered from the DECLINED rows (both signals
are recorded either way) or by running the two in separate phases.

**BTST is allocated ONE capital, split across the day's names.** It was a fixed notional per
stock, so the deployed figure grew with the size of the watchlist - six names meant Rs 6,00,000
at work, which is not how an account behaves. `btst.capital` in config.yaml is now the
strategy's pot, divided equally: ten names means smaller positions, not more money.

**Trade outcomes are mirrored into the strategy's own channel.** For the PineScript strategies
the engine acts on the ENTRY alert alone - it sizes, places the SL-M and the TP, and manages
from there - and IGNORES the script's own "SL HIT"/"TP1 HIT" alerts, which describe what the
script thinks happened on its chart rather than what the engine did with the order. So a
trader watching intraday-orb-analyze saw the entry, then messages the engine ignored, and
nothing about the real outcome: it went to the admin channel, and the only way to learn
whether the stop or the target actually filled was to open the broker app. Outcomes
(entry_filled, partial_exit, position_closed, time_exit, no_progress_exit, and the failure
events) now post to the strategy's channel as well. Intermediate steps deliberately do not -
the channel already carries the alert that triggered the order.

**Duplication audit across every strategy.** trades.db has never held two entries in the same
stock on the same day, and BTST's paper book is clean after the de-duplication. The three
alert-dedupe bugs (tp_hit 5x AXISBANK, structure_flip 4x ADANIENSOL and 4x ADANIENT) all
traced to the one timestamp-format comparison and are fixed. The remaining repeats are
GENUINE: MFSL's two "intraday_transition" alerts for the same scan an hour apart correspond to
it leaving the scan and re-entering, which scan_hits confirms.

---

**Daily reconciliation canary.** New `signal_engine/reconcile.py` runs straight after the day
summary and compares the engine's day against the broker's: the sum of closed-trade P&L in
trades.db against OpenAlgo's own realised P&L. A gap beyond Rs 1 is CRITICAL and always
reaches Telegram, unfiltered by `notify_level`, because it means every other number that day
is suspect. Deliberately dumb - it does not care WHY they differ. On 2026-09-11's figures it
would have fired: engine +986.41, broker +119.31, difference +867.10.

**The same timestamp bug, in three places.** trades.db writes `datetime.isoformat()` with a
'T'; breakingtrade.db writes a space. At index 10 ' ' (0x20) sorts BELOW 'T' (0x54), so a
LATER alert always compared as SMALLER and every raw-string `created_at >= ?` filter matched
nothing. Three "have I already done this?" checks therefore always answered no:
`tp_watch._last_level_hit` (5x TP1 for AXISBANK), `flip_watch._already_warned` (4x ADANIENSOL,
4x ADANIENT) and `entry_watch._already_confirmed`. All three now share
`alerts.SINCE_CLAUSE`, which parses both sides.

**Two gaps the user found in the strategy-by-strategy review.** An EXIT matching no position
left no row anywhere - both of the day's BREAKOUT signals were exactly this (an SL HIT for
BPCL, a TP1 HIT for INDUSINDBK, for positions the engine never held), so a review built from
trades.db reported BREAKOUT as having produced zero signals. `_decline()` records EXITs now.
And the watchlist strategy's 10 delivered signals never reached the engine because the
Sep-10-started process was not subscribed to that channel; it is now.

**Notifications raised before Telegram connects are queued, not dropped.** Startup
reconciliation runs before `listener.set_client()`, so all four of the day's closes went
straight to the floor - which is why nothing appeared in the admin channel. Queued (bounded
at 50) and flushed on connect.

**BTST reports a basket result, not a column of percentages.** The EOD summary listed each
stock's +/-% and nothing else, so "did this make or lose money" had to be totalled in the
reader's head across ten rows. BTST has no position size of its own (it is a manual call), so
the report now states the framing a trader would actually use - an EQUAL-WEIGHT BASKET, one
position per stock, at a notional set in `config.yaml`'s new `btst:` block (reporting only;
nothing traded depends on it):

    6 positions settled | 1 won, 5 lost | 17% hit rate
    NET  -0.52% per position  =  Rs -3,147 on Rs 100,000 each (Rs 600,000 deployed)
    Best  AXISBANK +0.36%   |   Worst  GRASIM -1.04%
    Avg winner +0.36%  |  Avg loser -0.70%  |  Payoff 0.51

Payoff (average winner over average loser) is the figure a percentage list hides completely -
a 60% hit rate at payoff 0.5 loses money. Note what de-duplication did to the same day: "10
settled | 3 winners, 7 losers" became 6 positions, 1 winner. The duplicates were inflating
the winner count.

**One BTST watchlist message per day.** The closing scan runs at 14:50 and again at 15:10 and
each run sent its own copy, so the channel showed the same list twice. The paper book now
keeps only the first recommendation of the day, so the second copy is noise. A later run
whose SELECTION genuinely differs is still sent, marked REVISED.

**trades.db indexed.** The table carried none, which was survivable while it was read twice a
day at startup - not now that `fetch_day_trades()` backs both the EOD summary and the
day-context line on every close notification, on a table that is deliberately never pruned.
Two expression indexes (the queries filter on `date(executed_at)` and `upper(symbol)`, which
a plain column index cannot serve) take both hot queries from `SCAN trades` to
`SEARCH trades USING INDEX`. `fetch_day_trades` also ran one sub-query per exit row; it now
fetches the day's entries once.

---

**EOD review of the first full day, and ten defects it exposed.** Full write-up:
[`EOD-ANALYSIS-2026-09-11.md`](EOD-ANALYSIS-2026-09-11.md). The engine that ran today started
on 10 Sep and never picked up the day's commits, so everything below was the pre-fix system.

Reported day P&L was +Rs 986.41. The broker's own per-symbol figures make it about -Rs 78.

*P&L came from a portfolio-level delta.* `_book_broker_close()` used
`fetch_realised_pnl() - _last_realised_pnl`, the whole ACCOUNT's realised P&L, so two closes in
one poll cycle meant the first absorbed everything: ADANIENSOL booked +897.40 and ADANIENT
+0.00 in the same second, when the broker's figure for ADANIENSOL was -197.20. The positionbook
has always carried the per-symbol number - reconciliation reads it. `_index_positionbook()` now
returns a `BookEntry(quantity, ltp, realised)` and the close path uses it.

*Tracker-detected closes never reached trades.db.* `book_close()` filed an in-memory record,
moved the day counters and sent Telegram, but wrote no EXIT row - so the audit trail every
report reads had nothing, and the 15:05 restart's reconciliation booked all four closes a
second time with different numbers. New `db.save_tracker_exit()`.

*The TP ladder could never advance and re-fired TP1 forever.* `_last_level_hit()` compared
`created_at >= executed_at` as strings across two databases that format timestamps differently
- trades.db uses isoformat() with 'T', breakingtrade.db uses a space, and at index 10 ' '
(0x20) sorts below 'T' (0x54). A later alert always compared as smaller, so the filter matched
nothing and `_next_level(None)` was always "TP1". AXISBANK got five TP1 alerts in two minutes;
each carries ExitQtyPct 50, so against a live position that is half the remainder exited five
times over. Now `datetime()` on both sides.

*Exits were all labelled "SL".* None was: all four were no-progress market exits. The label
reaches the day summary and the trade record.

*Two channels got no EOD summary at all.* The per-strategy send iterated only strategies with a
CLOSED trade, so intraday-orb and intraday-breakout stayed silent - indistinguishable from "the
engine was down". Every enabled channel for the phase now files a report, including "No trades
taken today."

*BTST counted the same stock twice.* The closing scan runs at 14:50 and 15:10 and each run
opened a separate paper position, so AXISBANK settled twice into the winners list and
"10 settled | 3 winners, 7 losers" described about six distinct stocks. A unique index on
`(strategy, symbol, date(entry_at))` now makes a same-day duplicate impossible and the FIRST
recommendation of the day wins - the price a trader acting on the first alert would have had.
Existing duplicates collapse on first connect (verified against a copy of the live database:
78 rows to 69, idempotent).

Defects 1, 2, 3 and 6 were not analyze-only - they would have behaved identically with real
money. The remaining irreducible difference between analyze and live is slippage: the sandbox
fills at the requested price.

**Two things must happen before the next session:** top the sandbox up (at Rs 3,073 it cannot
fund a single position, so tomorrow repeats today) and restart the stack, since the running
engine predates every fix.

---

**Three defects found in today's own logs, after the review.**

*Every BreakingTrade alert overstated its R:R by exactly 2.00x - all 25 of them.* The message
sent `TP: targets[0]` (the 1.0x IB target) while the `R:R:` line beneath it was computed from
`targets[-1]` (2.0x). The staged ladder is computed but not wired through - BreakingTrade
exits 100% at `targets[0]` - so the advertised number described an exit the engine never
performs. UNIONBANK is the sharp case: the channel showed 1:1.2 and the engine then IGNORED
the same signal for falling under `min_rr: 0.75`, at its real 1:0.60. `reward_risk` now
measures the TP actually sent; the ladder is reported separately as `Runner target (not traded
yet)`. `build_trade_signal_message()` was split out so the text is testable at all - the bug
survived because the only route to it also wrote to the database and called Telegram.

*14 of 26 validated signals were rejected for sandbox margin and logged as broker errors.* The
sandbox drained to Rs 3,073 while the engine sized every trade off the Rs 1,00,000
`sandbox_capital` override. All three guards read the override rather than the real balance,
so `min_capital_for_entry` compared Rs 1,00,000 against its Rs 5,000 floor and passed, and the
margin check is skipped in analyze mode anyway. Every order was sent and bounced, landing in
trades.db as a broker REJECTION - a capacity limit recorded as a broker problem, in the one
profile whose slot caps were removed specifically so declines would say something about the
strategy. New `fetch_funds_available()` reads the real balance and `_sandbox_can_fund()`
declines with `stage="sandbox_margin"`. An unreadable balance is "unknown", never "broke".

*Two dependencies were failing silently.* `/api/v1/orderstatus` answered 404 to 564 of 568
calls today and `fetch_order_fill_price()` swallowed all of it at DEBUG, so the engine has
been falling back to the signal's entry price - quietly disabling
`no_progress.use_fill_price_for_progress` and making every R-multiple a quote rather than a
fill. Separately a 403 burst caused 16 consecutive positionbook failures (11:50-11:52 IST):
the tracker was blind for 2.5 minutes with no close detection, no no-progress gate and no
time-exit trigger, and said nothing beyond one WARNING per cycle. Both now report.

**P2/P3 review items closed.** Daily rollover checked wherever a counter is first touched
(M2); reconciliation honours per-strategy `product` overrides (M4) and attributes restored
positions by broker quantity and side rather than dict order (M5); idle strategies' phantom
slots are cleared (M6); the consolidated day summary labels its pooled capital figure and
drops the meaningless percentage (M7); `strategies.REGISTRY` replaces six hand-maintained maps
and `config.validate_channels()` reports duplicate names/ids, missing phase twins and
both-phases-enabled at startup (M10); the BreakingTrade alert warning is keyed per destination
(M12).

**Resources.** trades.db moved to one connection per THREAD with the schema built once - one
shared connection was tried first and silently lost 3-7 of 8 concurrent writes, because two
threads racing CREATE TABLE made one lose to "database is locked", which `save()` catches and
logs (L1). Shared `httpx.AsyncClient` (L3), `RiskStore` lifecycle (L2), and all three released
on shutdown. New `--prune` for breakingtrade.db and the 125 MB Chromium cache, leaving the
login session intact (L4). The fd-audit then found two more: the Telethon client was never
disconnected when the listener gave up - harmless until degraded mode made the process outlive
the failure - and `_exit_locks` grew one lock per symbol forever.

**Housekeeping.** `signal_engine/` is ruff-clean and gated in CI with no `continue-on-error`
(L5); icons out of Telegram text and the JSON error sink (L6); the Telegram integration tests
use a private copy of the session file instead of fighting the running engine for it (L7). The
sizing line now shows implied leverage and, when margin scaling moved the quantity, the actual
risk taken against the intended 1% (T1/T2). Suite 1095 -> 1305 tests; coverage 77% -> 80%.

---

**End-to-end review, and every P0/P1 finding fixed.** Full findings and the remaining plan:
[`CODE-REVIEW-2026-09-11.md`](CODE-REVIEW-2026-09-11.md). Suite 1095 -> 1172 tests.

**The two-gate channel rule now actually exists (C1).** `config.yaml` has claimed since the
ANALYZE/LIVE split that a channel trades only when BOTH `enabled: true` AND OpenAlgo is in
that phase's mode. Gate 2 was never implemented — `listener._split_by_enabled()` filtered on
`enabled` alone, and nothing anywhere compared a channel's `-analyze`/`-live` suffix to the
running mode. Flipping OpenAlgo to LIVE (exactly what promoting BREAKOUT requires) would have
put all four enabled `-analyze` channels onto real money, silently, pooled into 2 slots.
`listener._phase_mismatch()` now refuses a mismatched message PER MESSAGE (subscriptions are
fixed at connect, so a subscribe-time check would miss a mid-session flip), logs CRITICAL,
records a DECLINED row and alerts once per channel. Unsuffixed channels stay phase-agnostic.

**A mid-session mode flip halts new entries (C2).** The mode was resolved ONCE, in
`startup._run_engine()`, and fanned out to `apply_trade_mode` / `db.set_trade_mode` /
`logger_setup.set_mode` with nothing ever re-checking — while `notifier` and the BreakingTrade
poller both re-checked every 60s. A flip left the engine placing real orders under the analyze
profile (`max_open_positions: 0` = unlimited, all three loss limits at `1.0` = off) and
stamping `trades.db`, `risk.db` and the log file "analyze". New `mode_guard.py` owns one
process-wide phase cache (replacing `notifier`'s duplicate), is armed at startup, is polled by
the tracker each cycle, and halts new entries on a change. `main._entry_halted()` enforces it
first in the entry pipeline. Exits are never halted — a halt stops NEW risk, it must not
strand an open position.

**Loss limits were off for the first signal after every restart (H1).** `check_exposure()`
gated all three loss checks on `last_known_capital`, which is stamped only inside
`calculate_quantity()` and never persisted — so 0.0 on a fresh process, and `_handle_entry`
runs the risk gates BEFORE resolving capital. A restart after a day that already hit the 4%
daily limit let the next signal straight through. `_limit_capital()` now falls back to the
persisted `day_start_capital`.

**Weekly and monthly limits measure NET drawdown (H2).** `record_close()` only accumulated
`abs(pnl)` on the losing branch, and the weekly/monthly gates summed that gross column — so
they were trade counters wearing a drawdown percentage. At Rs 350 risk/trade on Rs 35k that
was 8 losing trades for the week and 15 for the month: roughly two and four sessions at 10
trades/day, on a net-profitable account. The daily gate stays gross on purpose (four stop-outs
is a signal about the day whatever the winners did). `risk.db` gains `daily_net_pnl` via an
idempotent in-place migration backfilled from each row's own `-daily_loss`.

**Duplicate suppression keyed without the strategy (H3).** The key was
`(symbol, direction, tp_level or entry)`, and an EXIT synthesizes entry `0.0` — so it reduced
to `(SYMBOL, "EXIT", "TP1")`, shared by every strategy holding that name. Two strategies
hitting TP1 on the same stock within 60s meant the second alert was dropped and THAT POSITION
NEVER EXITED; it rode to the 14:45 time exit. BREAKINGTRADE / BREAKINGTRADE-WATCHLIST collide
by design.

**Concentration caps follow the LIVE-pools / ANALYZE-isolates split (H4).**
`max_positions_per_symbol: 1` was global in both modes, which quietly broke the BreakingTrade
two-outcome experiment: the watchlist fires on the scan-hit poll, before `plan_trade()`'s
confirming close exists, so it always took the symbol slot and the confirmed signal was
declined "symbol concentration limit". BREAKINGTRADE showing near-zero trades would have read
as "the confirmation filter is too strict".

**The restart path reintroduced the broker OCO rejection (H5).**
`_restore_tracker_positions()` registered positions with `sl_order_id=""`, and
`_cancel_sl_before_exit()` returns early when that is falsy — so after any restart the next
TP/EXIT placed a SELL while the broker SL-M was still working, which an Indian broker reads as
a new SHORT and rejects with FUND LIMIT INSUFFICIENT. New `api_client.fetch_orderbook()` and
`startup._match_open_sl_orders()` recover the live stop id per symbol.

**Telegram flood waits killed the engine, not just the listener (H6).** Confirmed incident,
`logs/errors_2026-09-08.jsonl`, 11:15:50 -> 11:39:53 IST: 96 records, 64 of them
`GetUsersRequest` flood waits generated by `_keepalive()`'s own `get_me()` — the keepalive
triggered the throttle it exists to detect. The retry loop then backed off 2/4/8/16/32s
against a stated 349-second wait, so every retry landed inside the ban, and after five
`start_listener()` returned — which `_serve_until_shutdown()` treated as completion, stopping
the tracker and the time-exit scheduler with it. Sixteen times in twenty-four minutes of
market hours, each leaving open positions with no close detection, no no-progress gate and no
square-off. Keepalive is now a passive `is_connected()` read; `FloodWaitError` is slept for
its own stated seconds plus jitter against a separate bounded budget; and a dead listener
enters DEGRADED MODE (positions stay managed, new entries halted, operator alerted) instead of
ending the session.

**The unrealised-drawdown gate is finally wired (M1).** `update_unrealised()` had no
production caller at all, so `check_exposure()`'s `daily_realised_loss + unrealised_loss` was
only ever the realised half — while this document described the combined gate as live.
`tracker._push_unrealised()` now runs each poll off the same positionbook snapshot. Losses
only: a position in profit reports `0.0`, because the limit ADDS this to realised losses and
letting a winner credit against it would let one open runner mask a day of stop-outs.

Also fixed: `_restore()` maps stored keys through `_key()` so LIVE no longer loads
per-strategy rows it can never read but still sums (M3); stale signals are logged at WARNING
and written as DECLINED rows instead of vanishing at DEBUG (M8); and an unreachable OpenAlgo
returns the LAST KNOWN phase rather than always `"analyze"`, so an API blip no longer routes a
live-money alert into the paper channel (M9, partial).

---

**EOD summary split per strategy, plus a consolidated cross-strategy comparison.**

`notifier.notify_day_summary()` used to pool every strategy's closed trades into ONE blended
report (`tracker.py`'s `_day_trades`/`_day_wins`/`_day_pnl` are, and remain, single global
counters) — a strategy's individual win rate, avg R and capital trajectory were not
recoverable from it, and `TradeRecord` did not even carry a `strategy` field to make that
possible. This mattered specifically because each strategy already sizes off its OWN cached
day-start capital (per-strategy risk isolation, 2026-09-10) — a single pooled capital-
trajectory line doesn't correspond to any real account once more than one strategy is
blended into it, and a blended win-rate hides one strategy's losses behind another's wins.

`TradeRecord` (`tracker.py`) now carries `strategy` (from `pos.strategy`, already available at
both places a trade closes — `book_close()` and `_book_one_time_exit()`). `notify_day_summary()`
groups `trade_records` by strategy and sends THREE things every day trades happen:

1. One summary per strategy, to that strategy's OWN `-analyze`/`-live` channel (new
   `_channel_for_strategy()`, matching alerts.py's identical per-strategy channel lookup) —
   trades, W/L, win rate, net ₹, avg R, capital trajectory (per-strategy, via
   `RiskEngine.last_known_capital_for()`), a `Best: X (+1.8R) | Worst: Y (-0.6R)` callout, and
   the usual per-trade table.
2. ONE consolidated summary to `notify_channel`, unchanged in its pooled top-line stats, but
   — when more than one strategy traded — followed by a comparison table, one row per
   strategy, ranked **best to worst by avg R, not net ₹**: avg R is agnostic to how much
   notional/risk-% a strategy happened to be sized with, so it is the fair basis for comparing
   ORB against BREAKINGTRADE-WATCHLIST against BREAKOUT, which raw ₹ P&L is not. A single
   strategy trading alone gets no comparison table — nothing to compare yet.
3. Both are best-effort and independent: an unconfigured per-strategy channel, or one
   strategy's send failing, never blocks another strategy's summary or the consolidated one,
   and `notify_day_summary()`'s return value (which gates tracker.py's "day is done" marker)
   reflects only the consolidated send.

**Obsolete config removed.** `.env`'s `BREAKINGTRADE_CHAT_ID_BTST`/`_INTRADAY`/`_WATCHLIST` —
dead since alerts.py moved to reading every channel id from `config.yaml` — deleted; only
`BREAKINGTRADE_BOT_TOKEN` remains in `.env`. Repo-wide sweep (code, tests, scripts, docs) found
no other leftover reference to the pre-split single-channel names or config shapes.

**BreakingTrade poller: one redundant OpenAlgo API call removed at every startup.**
`__main__.py`'s `_watch()` already fetches OpenAlgo's mode once at startup for its own log line
and the `alert_started()` banner; `alert_started()`'s first Telegram send used to immediately
repeat the identical `/api/v1/analyzer` call from `alerts.py`'s cold mode cache to answer the
same question a few lines later. New `alerts.prime_mode_cache(mode, is_analyze)`, called right
after `_watch()`'s own check, seeds the cache so the redundant call never happens — and closes
a theoretical race where the banner text and the channel it's actually delivered to could
disagree if OpenAlgo's mode flipped in the gap between two independent checks. Also fixed a
stale docstring reference to the renamed `_CHANNEL_GROUP_BY_KIND` dict.

**Every Telegram channel split ANALYZE/LIVE — config.yaml is now the single source of truth
for every channel id in the engine.**

Until today every strategy had ONE Telegram channel carrying both paper-phase and (once
promoted) real-money signals, and BreakingTrade's chat ids lived in `.env` while everything
else lived in `config.yaml` — two places to keep in sync, with no enforcement that they
agreed. Both problems compound the same failure mode: a channel's own scrollback mixes
unrelated phases, making after-the-fact performance review unreliable, and a config split
across two files invites drift.

`config.yaml`'s `telegram.channels` list now carries an `-analyze`/`-live` pair per strategy —
`intraday-orb`, `intraday-breakout`, `intraday-breakingtrade`,
`intraday-breakingtrade-watchlist` — each pair sharing the SAME two-gate safety rule the
single-channel design already had: a channel only ever trades when both (1) `enabled: true`
here AND (2) OpenAlgo is actually in that phase's mode. A `-live` entry ships `enabled: false`
until a strategy is deliberately promoted. For ORB/BREAKOUT (PineScript, TradingView-driven)
the existing channel became `-analyze` unchanged (same id, so paper history isn't lost) and a
new `-live` channel was created; both ran a brief real stretch before 2026-09-07 mixed into
what is now paper history — cosmetic only, since the actual trade record is `trades.db`, never
the Telegram channel itself. BreakingTrade/watchlist have been paper-only since inception, so
their split carries no such caveat.

BTST (`signal_engine/analysis/breakingtrade`'s overnight carry call) got its own
`telegram.breakingtrade_btst_channels.{analyze,live}` mapping instead of a `telegram.channels`
entry — it is a manual daily decision with no engine-listener counterpart, so it was never
part of the execution-safety surface and doesn't belong in the list that gates trading.

`alerts.py` (the BreakingTrade family's own outbound bot) no longer reads ANY chat id from
`.env` — `chat_id_for()` now resolves the destination by name from
`settings.telegram_channels` (or `settings.breakingtrade_btst_channels` for BTST), picking
`-analyze` or `-live` from OpenAlgo's live `/api/v1/analyzer` state via a new `_current_phase()`
(60s cache, defaults to `analyze` if OpenAlgo is unreachable — never assume live). Only
`BREAKINGTRADE_BOT_TOKEN`, an actual secret, remains in `.env`.

`notify_channel` (the admin/system channel — startup, shutdown, risk halts, order lifecycle,
day summary) got the identical split: `config.yaml`'s `telegram.notify_channel` is now
`{analyze: {...}, live: {...}}`, and `notifier.py` picks the phase the same way `alerts.py`
does (its own `_current_phase()`/`_channel_for_phase()`, 60s-cached via
`api_client.fetch_trading_mode()`). One difference from the strategy channels: since an admin
alert can be safety-critical (a failed SL, a risk halt, a startup failure), `_channel_for_phase`
falls back to whichever phase IS configured rather than silently dropping the message if the
current phase's channel isn't set up yet — a mislabeled alert beats a missing one.
`openalgoscheduler.py`'s broker-login notice (mode-independent, unrelated to trading mode) now
broadcasts to every configured phase instead of picking one.

Real regression caught in the same change: `strategy_cards.py`'s pinned per-channel reference
card was keyed by the OLD unsuffixed channel name (`"intraday-orb"`); after the rename every
channel would have silently stopped getting its pinned card. Fixed by stripping the
`-analyze`/`-live` suffix before the `CARDS` lookup, and each card now states inline whether
it's pinned in the paper or live channel. Added a card for BTST too, pinned via the same
mechanism (`listener.py`'s `_connect()` now also pins `breakingtrade_btst_channels`, which
sits outside the engine's normal subscription list).

**Smoke-test startup message trimmed to one line on a routine green restart.**

`notifier.build_startup_summary_message()` used to print the full OpenAlgo/Signal-Engine
checklist on EVERY restart, including a fully-passing one — the engine restarts daily (and
after every code change), so an unchanging seven-line checklist just trained the reader to
stop looking at it. Now: all-passed gets one line (`READY | mode | capital | "All N checks
passed."`); the full section-by-section breakdown returns the moment there is something to
troubleshoot (a warning-level check failed — a critical failure never reaches this function at
all, see `run_startup_health_checks()`'s docstring).

**Day summary is now pinned, replacing the previous day's pin.**

New `notifier._send_and_pin_day_summary()` sends the day summary exactly as before, then pins
it in the notify_channel and unpins whatever was pinned there the day before (state persisted
in `signal_engine/data/day_summary_pin_state.json`, same JSON-state idiom as
`strategy_cards.py`). "How did today go" is now one tap away without scrolling, while every
day's summary still lands in the channel's ordinary history too — pinning is additive, never a
replacement for delivery, and a pin/unpin failure (bot lost admin rights, etc.) is logged and
swallowed rather than reported as a failed send.

**`notifier.py` emoji removed — project convention (CLAUDE.md: no icons/emoji anywhere) was
being violated in every trade-lifecycle message** (📤💰🛡️🚨✅❌⏰🛑⚠️🚪📊🔴), while
`alerts.py`/`strategy_cards.py` already correctly avoided them. Also renamed the fill
confirmation's "💰 LIVE" label to "FILLED" — now that ANALYZE/LIVE means channel phase, a
message literally saying "LIVE" inside the paper channel was actively misleading.

## Recent Changes (2026-09-10)

**18:37 IST — BreakingTrade now trades its WATCHLIST call directly, as a second outcome
separate from the existing CONFIRMED signal.**

Until now the scanner only ever traded a CONFIRMED entry: `trigger.plan_trade()` waits for
`entry_trigger()` to see a later 5-minute bar CLOSE beyond the signal bar's extreme before it
will emit anything, and the BT WATCHLIST notice it sends the moment a symbol first matches a
scan is explicitly "no action, not a trade signal" (`alerts.alert_transitions()`). That
confirmation requirement is why real signals stayed rare even after 2026-09-09's entry_watch.py
fix (see that date's entry below) — a genuine belief that the scanner's own selection is
already decent quality, independent of whether price goes on to confirm it, was never testable.

New `trigger.plan_trade_watchlist()` builds the same stop/target plan (`stop_level`,
`target_levels`, `target_split` all unchanged) but enters at the SCAN-HIT PRICE itself, with no
confirming-close wait. Wired into `__main__.py`'s `_emit_trade_signals()` alongside the existing
confirmed path (one shared bars/ATR fetch per symbol, not two separate OpenAlgo history calls),
and emitted under a wholly separate strategy tag/channel — `BREAKINGTRADE-WATCHLIST` on
`intraday-breakingtrade-watchlist` — rather than folded into `BREAKINGTRADE`. Deliberately kept
apart in `config.yaml` (own `strategy_profiles`/`blacklist` blocks, own risk slots since
`RiskEngine` isolates per-strategy in analyze mode, see 14:55 entry below) so a losing streak on
one outcome can never throttle the other via a shared limit, and so paper P&L answers "which
entry style (if either) is worth taking live" as a clean two-way comparison rather than a
blended number. Both `BREAKINGTRADE` and `BREAKINGTRADE-WATCHLIST` channels currently `enabled:
true` under OpenAlgo ANALYZE (paper) mode only — going live on either follows the same
one-strategy-at-a-time discipline BREAKOUT already established (disable the other before
flipping OpenAlgo to live), since `mode_profiles.live`'s risk numbers are a POOLED total across
whatever is enabled live, not per-strategy.

Also fixed in the same change: `alert_trade_signal()` gained a `kind` parameter so a caller can
route to either channel; `alerts.py`'s `_env()`/`_CHANNEL_BY_KIND` and `.env` gained
`BREAKINGTRADE_CHAT_ID_WATCHLIST`; the informal "BT WATCHLIST" header was spelled out to
"BREAKINGTRADE WATCHLIST" (project convention: no abbreviations in Telegram text, so the three
message shapes - `BREAKINGTRADE WATCHLIST` notice, `BREAKINGTRADE LONG/SHORT` confirmed trade,
`BREAKINGTRADE-WATCHLIST LONG/SHORT` watchlist trade - stay visually distinct at a glance).
`.env`'s `BREAKINGTRADE_CHAT_ID_INTRADAY` also had a stray `lets` suffix corrupting the chat id
(`-1004379472313lets`), found and fixed while wiring this up - it would have silently broken
delivery to the confirmed-trade channel.

Separately, `strategy_cards.py`'s per-channel pinned reference card used to be re-sent and
re-pinned on every listener startup (daily, and after every code change) - harmless in intent
but it meant a fresh pinned message, and channel clutter Telegram never fully cleans up, on
every restart that changed nothing. `send_and_pin_cards()` now hashes each rendered card and
persists the hash of whatever it last pinned per channel
(`signal_engine/data/strategy_card_state.json`); a channel is only touched again when that hash
differs (first-ever pin, or the CARDS entry was actually edited), and the stale previous pin is
unpinned at the same time. Applies to every strategy's card, not just BreakingTrade's two.

See `signal_engine/pinescripts/intraday/breaking-trade/STRATEGY-LOG.md`'s 2026-09-10 entry for
the layman version.

**14:55 IST — Per-strategy capital isolation; analyze-mode trade/position caps removed.**

`RiskEngine` (`risk.py`) previously tracked open_positions/trades_today/day-start capital/
realised loss in ONE shared set of counters across all three sandbox strategies (ORB, BREAKOUT,
BreakingTrade) — so all three sized every trade off the SAME cached capital figure and competed
for the same `max_open_positions`/`max_trades_per_day` slots. A busy strategy could starve the
other two of slots regardless of signal quality (surfaced while investigating a 909-qty PFC
trade — the sizing itself was correct, `capital=100,000 x risk 1% / SL Rs1.10 = 909`, but it
exposed the shared-pool design underneath it).

New `_StrategyState` dataclass gives each strategy its own `open_positions`, `trades_today`,
`daily/weekly/monthly_realised_loss`, `unrealised_loss`, `day_start_capital`,
`last_known_capital`, keyed lazily by strategy tag (`RiskEngine._state(strategy)`). Example: ORB
and BREAKOUT both call `get_sizing_capital(100_000, strategy)` the same morning — each caches its
OWN Rs100,000 independently; ORB opening 3 positions does not touch BREAKOUT's counters at all.
Symbol/sector concentration limits (`max_positions_per_symbol/sector`) stay GLOBAL on purpose —
correlated risk from two strategies piling into the same stock is real regardless of which one
triggered it.

`RiskStore`'s `risk_counters` table migrated `(mode, trade_date)` -> `(strategy, mode,
trade_date)` primary key (`risk_store.py`); pre-migration rows preserved under a `_LEGACY`
strategy sentinel, migration runs transparently in `RiskStore.__init__` (renames old table to
`risk_counters_pre_strategy_split`, copies rows forward — no manual step needed). New
`RiskStore.strategies_for(mode, date)` lets `RiskEngine._restore()` reload every strategy active
today after a restart without needing to know the strategy set up front (strategy names are free
text parsed from the Telegram alert header, not a fixed enum).

**Same session: `mode_profiles.analyze.max_open_positions`/`max_trades_per_day` removed
entirely** (0 = unlimited — `check_exposure()` now treats 0 the same way
`max_positions_per_symbol`/`sector` already did; previously a shared 9/36 across all three
strategies, see the "Mode profiles" table below, now superseded). Paper trading records every
signal a strategy's own entry rules pass instead of discarding some to an artificial slot
ceiling — more data for comparing strategies fairly.

**Trade-off worth knowing, not fixed today:** `adjust_qty_for_margin` was already skipped in
analyze mode (sandbox has no real margin to check), and every trade sizes off the same cached
day-start capital regardless of how many others are open — so a busy day can now show more
simultaneous "at risk" exposure across concurrent trades than a real Rs1L account could ever
actually fund at once. Each individual trade's own numbers (entry/SL/qty/result) stay honest;
the SUM across many concurrent trades on a busy day is not something a real capital-constrained
account could reproduce. Deliberate trade-off for the data-maximizing goal of this paper phase —
revisit before ever running more than one LIVE strategy at a time: the `live` mode_profile keeps
its real slot caps (2/10, calibrated to actual Rs35k MIS margin — see "BREAKOUT paper week"
below), but per-strategy isolation means nothing currently caps TOTAL exposure across multiple
concurrently-enabled LIVE strategies. Not an active risk today (BREAKOUT is the only strategy
enabled in live mode), but a gap to close first if a second one ever is.

19 files changed, 13 new tests (`test_risk_strategy_isolation.py`) plus signature fixes across
the risk/tracker/startup test suites. Full suite green (1011/1011, excluding 4 pre-existing
live-network telegram tests). Commit `23fb0c608`.

**15:48 IST — LIVE mode pools every strategy's counters; closes the gap flagged above.**

The 14:55 entry above deliberately left LIVE mode using the same per-strategy isolation as
ANALYZE, flagging it as a gap to close before ever enabling a second live strategy — a real
account has exactly one pot of money, so two live strategies each believing they have the FULL
Rs35,000 independently would double-count it, relying entirely on the broker's margin rejection
to catch the overcount after the fact (confusing, and exactly what the original 2026-09-06
2-slot design was built to prevent).

New `RiskEngine.isolates_per_strategy` property (`self._trade_mode == "analyze"`) — `_key()`
now routes every strategy to one shared bucket (`_LIVE_POOLED_KEY = "PORTFOLIO"`) whenever this
is False, i.e. in LIVE mode and any other/unrecognised mode (real money defaults to the SAFER,
shared behaviour rather than guessing). This is a one-line branch inside `_key()`, so every
existing call site (`main.py`, `tracker.py`) needed NO changes — they still pass
`signal.strategy`/`pos.strategy` exactly as before, unaware of which mode they're resolving
into. Pooling applies uniformly: sizing capital, `open_positions`, `trades_today`, and
daily/weekly/monthly realised loss ALL pool in LIVE — not just capital. Example: if ORB already
holds a position and BREAKOUT's next signal checks in, it sees ORB's real remaining
capital/slot/loss numbers, not a second imaginary full account. Symbol/sector concentration
limits were already global in both modes and are unaffected.

`startup.py`'s broker-position reconciliation branches on `isolates_per_strategy`: ANALYZE
keeps correcting each strategy's own counter independently (attributed via the matching local
position's `strategy` field, as before); LIVE now corrects the ONE shared counter against the
broker's TOTAL open count in a single call — looping per known strategy name would have been
wrong here (each call would overwrite the shared total with that strategy's own partial count,
last one winning).

`mode_profiles.live`'s five numbers (`max_open_positions: 2`, `max_trades_per_day: 10`,
daily/weekly/monthly loss 4%/8%/15%) are consequently a POOLED total across every enabled live
strategy, same as they always implicitly meant before per-strategy isolation existed — this
restores that, explicitly, rather than by accident of only one strategy being live. Moot today
(BREAKOUT is still the only live strategy) but no longer a landmine for whenever a second one is
turned on.

11 new tests (`test_risk_live_pooling.py`) plus 5 existing restart-safety tests in
`test_risk_counters.py` switched from an incidental `trade_mode="live"` to `"analyze"` (their
actual purpose — restart-safe persistence keyed by one named strategy — was never about live
pooling; `risk_fixtures._engine()`'s default `trade_mode` is now `"analyze"` for the same
reason, since that was always the implicit assumption of the tests that don't set it
explicitly). Full suite green (1022/1022, excluding 4 pre-existing live-network telegram tests).

**11:27 IST — Fixed silent startup notification, consolidated into one Telegram message.**
`notify_startup_result(True, ...)` and `notify_engine_started()` both fired before
`start_listener()` ever calls `notifier.set_client()` — `notify()`'s module-level `_client`
was still `None`, so a passing startup silently dropped both messages (only a WARNING/DEBUG
log recorded it; on failure it worked, since that path already used a one-shot client).
Replaced both with a single `notifier.notify_startup_summary()`, sent via the same one-shot
Telegram client pattern `notify_startup_result` already used for failures, so it doesn't
depend on the listener being connected yet. `run_startup_health_checks()` now returns the
`SmokeTestReport` (or `None` on critical failure, having already sent the failure alert)
instead of sending its own success message. The one message groups checks into `-- OpenAlgo
--` (reachability, broker auth/capital, quote API) and `-- Signal Engine --` (config load,
signal pipeline, risk engine state, DB) sections, plus broker name, mode, capital, config
summary, and channel list — verified live via `openalgoctl.sh restart`, message confirmed
received. 7 new tests in `test_startup_notification.py`; full suite green except the 2
pre-existing unrelated `test_main_entry.py::TestBracketOrderFlow` failures.

**09:52 IST — Pre-session smoke test.** Ran `PYTHONPATH=. uv run python -m signal_engine.main
--smoke-test`. All 7/7 checks passed: config load, OpenAlgo reachable (HTTP 200), broker auth
(funds API, available capital 80,313.62 INR), quote API (SBIN LTP=1009.0), signal pipeline
(normalize/parse/validate), risk engine state (open=0/2, trades_today=0/10, daily_loss=0.00),
risk-store DB round-trip. No dry-run order placed. Engine clear to start the session.

**Pre-market-open readiness check for all 3 paper-trading strategies (ORB, BREAKOUT,
BreakingTrade), plus a genuine IST/UTC bug found along the way.**

- **Capital mismatch fixed.** `sizing.sandbox_capital` was 35000, stale against OpenAlgo's
  actual sandbox account (`db/sandbox.db`'s `sandbox_config.starting_capital = 100000`, set
  since account creation 2025-12-29). Every paper trade had been sized as if capital were 35k
  while the real dummy account held ~80-100k. Raised to 100000 to match.
- **Log files split by mode.** New `logger_setup.set_mode()`, called from `startup.py`
  alongside `db.set_trade_mode()`. Two file sinks (`signal_engine_live_*.log`,
  `signal_engine_analyze_*.log`) each gated by a loguru `filter` checking the current mode at
  log time, plus `signal_engine_unknown_*.log` for the pre-mode-resolution startup window.
  Verified live: analyze-mode lines correctly routed, live file stayed empty.
- **Found and fixed a real IST/UTC bug** while investigating why a test only failed after
  midnight IST: `TradeResult.timestamp`/`Signal.received_at` defaulted to
  `datetime.now(timezone.utc)`, and `db.fetch_all_open_positions()` /
  `fetch_last_entry_trade()` filter `date(executed_at) = <today, IST>`. SQLite's `date()`
  converts ANY offset-bearing timestamp to UTC before extracting the date - true of a raw UTC
  value and of a timezone-AWARE IST one alike (confirmed: `date('...+05:30')` returns the
  PREVIOUS day). Only a naive-IST timestamp (tzinfo stripped) round-trips correctly, matching
  the rest of the codebase's established "naive datetime means IST" convention. Fixed at the
  source in `models.py`, plus the same anti-pattern in three `db.py` call sites
  (`save_declined`, `save_reconciled_exit`, `set_strategy_version`). Same bug class as
  `fetch_bars()`'s 2026-09-08 incident (see breaking-trade/STRATEGY-LOG.md) - invisible during
  actual market hours (09:15-15:30 IST = 03:45-10:00 UTC, both zones agree), only bites a
  restart landing in the ~5.5 hour post-midnight window. This session's own restarts tonight
  were in exactly that window, which is how it surfaced.
- Prerequisites verified clean before market open: broker session healthcheck confirmed firing
  reliably every 15 minutes overnight (should catch the ~03:00 IST token rollover well before
  09:15), zero stuck sandbox orders, risk state clean, all 4 Telegram channels watching,
  strategy cards re-pinned.
- **Trade storage, for the record:** every real order (all 3 strategies) lands in
  `signal_engine/data/trades.db`'s `trades` table via `db.save()`/`save_declined()` - one
  shared table, `strategy` column distinguishes them. BTST paper positions are separate
  (`signal_engine/data/breakingtrade.db`'s `paper_trades` table - a simulated overnight hold,
  never a real OpenAlgo order). Access: `sqlite3 signal_engine/data/trades.db` directly, or
  `db.fetch_clean_trades(strategy, since)` from Python for analysis-ready rows.
- Noted but not fixed (pre-existing, unrelated to today's changes): `openalgoctl.sh`'s
  supervisor mislabels its own deliberate `cmd_stop` as `app_crash`/"exited unexpectedly" in
  the log, and Telethon's session sqlite occasionally throws "database is locked" on a rapid
  back-to-back restart (its own session file, not trades.db) - cosmetic, not correctness bugs.
- **Security finding, unrelated to today's work, surfaced by reading full startup output**:
  `.env`'s `API_KEY_PEPPER` is still the public `.sample.env` placeholder - every broker
  token/API key/TOTP secret in `db/openalgo.db` is encrypted with a publicly-known value.
  OpenAlgo's own startup check prints full remediation steps
  (`uv run python upgrade/init_db.py` to see which applies). Not touched here - rotating it
  destroys the existing password hash/tokens if done wrong, an explicit user decision.

**Follow-up same day: data-model review + repo cleanup.**

- `trades.db` schema reviewed for analysis-depth: adequate (entry/SL/TP, fill price, full
  signal context as JSON, trade_mode, data_quality). Deliberately NOT adding a stored
  P&L/R-multiple column - `analysis/ledger.py` computes those on read, which is correct
  (a cached column would go stale the moment sizing/exit logic changes). Found 221 historical
  trades (2026-03-09 - 2026-08-23, all before the trade_mode column existed) still untagged -
  left alone per the existing "NULL beats a guess" design decision rather than backfilled
  unilaterally.
- Cost calculator (per-trade brokerage/STT, intraday vs delivery) considered and deliberately
  deferred - it would only produce a meaningful number once a strategy clears the 5-day/
  one-of-each-exit-path minimum window from the 2026-09-09 policy above; applying it to today's
  first day of the now-fixed pipeline would just add false precision to noise. Revisit once
  that window is cleared.
- Repo cleanup: removed 3 untracked, superseded rotated `log/openalgo_*.log.*` files (~30MB,
  already fully diagnosed and documented above/in breakout.md), this session's own scratch
  `openalgoctl_manual_restart.log`, and `breaking-trade/claude_chrome_extension_session_summary.md`
  (a superseded one-off manual research session from 2026-09-03; the "periodic export" action
  item it describes as blocked was resolved differently by the automated fetcher.py/store.py
  pipeline built the next day, and the actual scan logic in scans.py doesn't follow this
  document's ad-hoc classification method). Checked and kept: `db/*-test.db` (referenced by
  `test/conftest.py`), `breaking-trade/excel/*.xlsx` (referenced by
  `test_breakingtrade_extractor.py`/`test_breakingtrade_scans.py`), `db/health.db`/`logs.db`
  (OpenAlgo's own live databases, out of scope).

Tests: 4 new (`test_logger_setup.py` mode-routing, `test_db.py` midnight-boundary regression).
Full suite green except 3 pre-existing unrelated failures (`test_flattrade_transform.py`,
`test_main_entry.py::TestBracketOrderFlow` - fixture gaps, not touched by any change this
session) and one flaky live-network integration test.

## Recent Changes (2026-09-09)

**Root cause found for a full day of broken paper trading: the Flattrade broker session was
dead from before market open until 15:05 IST, and nothing was checking.** `openalgoctl.sh`'s
`run()` supervisor logs in exactly once, at process start (`bootstrap()`); it only watches for
app.py/signal_engine *crashing*, never for the daily ~03:00 IST token expiry. A stack started
before that rollover (the normal case — started once, kept alive for days) runs the rest of the
day on a dead session with every quote call failing silently. Confirmed via
`log/openalgoctl.log`: 3,122 `"Session Expired : Invalid Session Key"` errors between 09:15 and
15:05 IST today, none after. Two paper-trading symptoms traced to this one cause:

- **Orders rejected with "unable to fetch current price"** (SBIN, NATIONALUM) — the sandbox's
  synchronous quote check at order-placement time failed, correctly refused to guess, and
  rejected. Working as designed; nothing to fix here.
- **HINDALCO "phantom fill"** — entry + two partial exits placed 10:50-11:10 IST all stayed
  `open` (no quote to fill against) while `signal_engine`'s 30-min orphan-timeout gave up on the
  position at 11:20 and released the risk slot. The underlying sandbox orders sat unfilled
  regardless, then all filled in one instant at 15:05:28 IST once the session recovered — at
  1021.1, the first live tick in 4+ hours — producing a paper trade with zero relationship to
  the TP1/TP2/TP3 hits the strategy had already announced. The leftover 38-qty position (SL
  cancelled at 11:20, never replaced, `signal_engine` no longer tracking it) was later
  force-settled at pnl=0 by `catch_up_processor.py`'s stale-MIS sweep.
- **BreakingTrade scanner emitted zero trade_signals all day, despite yesterday's fetch_bars fix
  being live** — `validate.fetch_bars()` calls the same `/api/v1/history` endpoint, so every
  confirmation check failed for the same 6 hours. Verified post-recovery: `fetch_bars()` now
  returns correctly-shifted IST bars (checked live at 15:2x IST), so the 2026-09-08 fix is
  confirmed working — today's watchlist-only outcome was the broker outage, not a residual bug.

**Fixes** (see `sandbox/execution_engine.py`, `sandbox/order_manager.py`,
`signal_engine/scripts/openalgoscheduler.py`, `signal_engine/scripts/openalgoctl.sh`, and
`signal_engine/pinescripts/intraday/orb/breakout.md` for the full writeup):

1. **Broker session self-heals now.** New `openalgoscheduler.py healthcheck` subcommand:
   cheap when the session is valid (one funds-API call via the existing `verify_broker_auth()`),
   performs a real re-login when it isn't. `openalgoctl.sh`'s `run()` loop calls it every 15
   minutes, reusing the existing auth-cooldown mechanism so a genuinely broken broker doesn't
   get hammered. Alerts to Telegram only on a state change (session found dead + recovered, or
   re-login failing) — not every cycle.
2. **Stuck MARKET orders auto-cancel instead of filling hours later at a stale price.**
   `ExecutionEngine.check_and_execute_pending_orders()` now cancels (via `OrderManager.
   cancel_order()`, which already handles margin release correctly) any `MARKET` order still
   `open` after `SANDBOX_MAX_MARKET_ORDER_AGE_SECONDS` (default 300s) with no valid quote.
   `LIMIT`/`SL`/`SL-M` orders are exempt — resting for hours on their trigger price is normal for
   them. Defense-in-depth: this protects against *any* future data-feed outage, not just today's.
   4 new tests: `test/sandbox/test_stale_market_order_cancel.py`.
3. `OrderManager.cancel_order()` gained an optional `reason` param, recorded on the order and
   passed through the existing order-update event, so a stale-cancel is diagnosable from the
   orderbook itself.

Tests: `signal_engine/tests/test_openalgoscheduler.py` (5 new, `TestHealthcheck`),
`test/sandbox/test_stale_market_order_cancel.py` (4 new). Full sandbox suite (137 tests) and
full openalgoscheduler suite (53 tests) green.

**Second finding the same day: BreakingTrade's real signal rate was never actually about signal
quality — `_emit_trade_signals()` only checks a scan pick on the single poll it first appears
on, and `trigger.entry_trigger()` needs a LATER bar to close beyond the signal bar's level, which
essentially never exists yet at that exact moment.** Once a symbol drops out of `new_by_scan` on
the next poll it is never re-examined, confirmed or not. Replayed the day's real scan output
(31 symbols flagged) against the full day's bars: 22 would have genuinely confirmed if simply
rechecked on a later poll (delay 3-94 min, median ~14). **New `entry_watch.py`**: re-checks every
still-pending scan pick (read from `store.py`'s existing `scan_hits` table, cross-checked against
`alerts` for `kind='trade_signal'` to avoid re-emitting) on every regular poll, not just the poll
it was born on, bounded by `CONFIRMATION_WINDOW` (2h, sized off the 94-minute slowest genuine
confirmation in the sample) so a stale pick eventually stops being retried. Wired into
`__main__.py`'s `_watch()` — runs unconditionally on every non-BTST poll, independent of whether
that poll had new hits. No change to `trigger.py`'s actual confirmation rule (still requires a
real bar CLOSE beyond the signal-bar level) — this gives that existing rule the multiple chances
across time it needed, it does not loosen it. 12 new tests: `signal_engine/tests/
test_breakingtrade_entry_watch.py`. Full breakingtrade suite (195 tests) green.

**Third round: BTST retrospective (K+L+M) comparison list, message-format unification, pinned
strategy cards.**

- `btst.candidates()` gained `full_session: bool` — skips straight to the K+L+M
  (`closing_ramp_full`, new `volume_shapes.SHAPES` entry) read. `btst.retrospective_candidates()`
  wraps it: never actionable same-day (M doesn't trade until the 15:15-15:30 auction, after the
  BTST order deadline), exists purely to measure whether the fuller read beats the live one once
  enough day-pairs accumulate — the prior K-vs-K+L+M backtest (11 day-pairs, both t<1.1) was too
  thin to trust either way. Both the live and retrospective lists are now saved daily to a new
  `btst_candidates` table (`store.save_btst_candidates()` / `btst_candidates_for()`) for that
  future comparison — no scoring built yet, intentionally (not enough data to make it worth
  reading; see `retrospective_candidates()`'s docstring).
- `alerts.alert_trade_signal()` gained an `R:R: 1:N` line (matches ORB/BREAKOUT's own alerts;
  not a parser.py-consumed field, so no effect on execution). `alert_transitions()` (watchlist)
  and `alert_btst()` headers now share one shape — `LABEL timestamp | N noun | context` — instead
  of two different orderings of the same three pieces of information.
- New `signal_engine/strategy_cards.py`: a short what/when/how-to-act reference card per
  strategy channel (ORB, BREAKOUT, BreakingTrade), sent and re-pinned to that channel on every
  engine startup via `listener.py`'s `_connect()`. Update a strategy's `CARDS` entry in the same
  change that alters what it does/when it runs/how to react — this is the Telegram-facing
  counterpart to the STRATEGY-LOG.md discipline CLAUDE.md already requires.
- Scope note: ORB and BREAKOUT's raw entry/exit alerts are authored directly in
  `breakout.pine`/`orb.pine` and posted straight from TradingView to Telegram — nothing on the
  Python side can reformat them. Left untouched this round by explicit choice; matching them to
  the same layout needs a separate PineScript edit.
- **Performance-analysis policy adopted:** trades from before a strategy's most recent
  STRATEGY-LOG.md-dated change must not be pooled with trades after it — same reasoning already
  applied to live-vs-analyze mode in `trades.db`'s `trade_mode` column (a blended average is true
  of neither period). Minimum window before drawing even a preliminary "does this look broken"
  conclusion: **5 trading days, or until every exit path (TP hit, SL hit, time exit) has been
  observed at least once, whichever is later** — sized for catching execution/plumbing bugs fast
  (the actual goal of the daily EOD review), not for validating edge, which needs far more data
  (see the BTST 11-day-pair result above, t<1.1 on both readings).

  **Now implemented**: `db.flag_data_quality(order_id, reason)` marks a trades.db row
  execution-corrupted (auto-called from `tracker.py`'s `_release_orphan()`) without deleting
  it; `db.fetch_clean_trades(strategy, since)` is the read side analysis should use instead of
  querying `trades` directly. `db.set_strategy_version(strategy, effective_from, reason)` /
  `get_strategy_version()` record the machine-readable half of a STRATEGY-LOG.md cutover — bump
  it in the same change as the log entry. Backfilled today's HINDALCO phantom-fill rows; set
  BREAKINGTRADE's cutover to 2026-09-09 (entry_watch.py changed what counts as a confirmed
  signal). ORB/BREAKOUT untouched, no strategy-logic change today.

Tests: 27 new (`test_breakingtrade_store.py` BTST persistence, `test_breakingtrade_btst.py`
retrospective/fallback cases, `test_strategy_cards.py`, `test_breakingtrade_alerts.py` R:R).
Full breakingtrade suite (205 tests) and full sandbox+openalgoscheduler suites still green.

## Recent Changes (2026-09-08)

**Critical: `fetch_bars()` UTC/IST bug fixed — this is why zero BreakingTrade trade signals had
ever fired** (`validate.py`). OpenAlgo's history endpoint returns UTC epoch seconds; `fetch_bars`
converted to a naive `Timestamp` without shifting to IST, while every consumer
(`trigger.initial_balance()`'s 09:15-10:15 IB window, `entry_trigger()`'s `signal_time`
comparison) assumes naive-IST input. `initial_balance()` returned `(None, None)` for every
symbol every day; `plan_trade()` returned `None` before reaching entry confirmation. Verified
against live data: 16/19 of today's watchlist symbols now produce a valid plan, versus 0 before.
No prior test coverage of `fetch_bars()` itself (other tests bypass it with hand-built IST bars)
— added regression tests mocking the HTTP response with real epoch seconds. **The running
poller process needs restarting to pick up this fix.**

**Tabular Telegram messages render correctly now; EOD summaries grouped by outcome**
(`alerts.py`, `eod_summary.py`). `send()` gained `monospace: bool`, wrapping the message in a
Markdown code block — every column-padded message (watchlist digest, BTST list, both EOD
summaries) was previously plain text, which Telegram renders in a proportional font, so the
alignment never actually rendered. EOD summaries restructured into RIGHT/WRONG
(intraday) and WINNERS/LOSERS (BTST) sections instead of one list sorted by return.

Full plain-language writeups with entry/SL/TP/consideration summaries are in
`STRATEGY-LOG.md`; this is the technical index.

**BTST paper ledger auto-settlement fixed** (`paper.py`). `settle_open_trades()` required a
snapshot stamped exactly `15:30:00`, which only `--backfill` ever writes — `--watch`'s schedule
stops at 15:10 and never produces one, so positions opened while `--watch` was the only data
source sat `status='open'` forever. Now settles against the latest snapshot of the first later
trading day, and is called automatically from the daily 14:45+ BTST block.

**Poller hard self-stop added** (`__main__.py`). `AUTO_STOP_TIME = 15:20`: the watch loop
self-terminates through the same clean shutdown path as SIGTERM/Ctrl-C, rather than depending on
an external cron or a person to stop it.

**Per-strategy SL floor and price band for BREAKINGTRADE** (`config.yaml`, `risk.py`,
`config.py`, `runtime.py`). Added `strategy_profiles.BREAKINGTRADE` (`min_sl_pct: 0.002`,
matching BREAKOUT/EMA9's evidence for the same tail+ATR-buffer stop style). Built genuine
per-strategy price-band support in `RiskEngine` (didn't exist before — `min_entry_price`/
`max_entry_price` were fixed at construction) and disabled BREAKINGTRADE's band entirely
(`min/max = 0`) — the global 300-5000 band is calibrated for ORB's fixed universe and was
silently declining real BreakingTrade candidates outside that range.

**Plain-English scan reasons** (`alerts.py`). `_SCAN_REASON` gives every scan a one-line
description (e.g. "gapped down but buyers stepped in and bought it back") instead of a cryptic
tag like `GapDnRescue`. Threaded into both the watchlist digest and the confirmed trade signal
(a non-mandatory `Reason:` field `parser.py` already tolerates).

**Automated EOD Telegram summaries** (`eod_summary.py`, new). Once per day, from the existing
14:45+ block: scores every symbol first flagged that day against its closing price (live
OpenAlgo quote), and separately summarizes whatever BTST positions settled that day straight
from the paper ledger. Both informational only, deduped to one send per day.

**Staged TP + runner-SL trailing for BREAKINGTRADE** (`tp_watch.py`, new). Wires
`trigger.py`'s already-computed 3-level target ladder (1.0R/1.5R/2.0R) and split through to the
engine's existing multi-TP mechanism (`ExitQtyPct`, TP1/TP1.5/TP2 ratcheting) — the same one
ORB/BREAKOUT use, not a new one. Split is a fixed default (trigger.DEFAULT_SPLIT, 50/30/20) —
day-type awareness deferred, needs cross-process state correlation. Dedup is gated on confirmed
Telegram delivery, not the attempt, so an undelivered TP-hit alert retries next poll instead of
permanently sticking a position.

**TP checks moved to the poller's ~20s loop, not the 5-15 min scan schedule**
(`__main__.py`). `tp_watch.check()` needs nothing from a scan poll — only an open position and
a live OpenAlgo quote — so tying it to the scan cadence left real accuracy on the table. Now
runs on `_watch()`'s own outer loop (already iterates every ~20s), gated to market hours
(09:15-15:30).

## Recent Changes (2026-09-07)

**BreakingTrade heartbeat false alarm fixed.** The poller's "no successful poll for N minutes"
watchdog compared elapsed time against blanket market hours (09:20-15:15), not against
`POLL_WINDOWS` — which has a deliberate ~2h gap between the 10:30-13:00 and 14:50-15:10 windows
(lunch, nothing to poll for). Result: a false alarm every trading day at ~13:21 and again at
~14:21. `_within_poll_hours()` now gates the watchdog on the actual scheduled windows (plus a
5-minute trailing buffer) instead. See `STRATEGY-LOG.md` 2026-09-07 for the full writeup.

**Alert-only structure-flip watch added** (`flip_watch.py`). Compares each currently-open
BREAKINGTRADE position's latest poll reading (`open_type_dir`) against the direction it was
entered on; sends one Telegram warning the first time it reverses, never auto-exits. Found via a
live example: TCS's Gap-Down Rescue LONG (09:55) had its own `buy_tail`/`rejection up` structure
flip to `sell_tail`/`rejection down` by 10:25.

**Telegram alerts now carry a link back to the message that started them.** `alerts.send()`
captures Telegram's own `message_id`; `alerts.telegram_link()` builds a `t.me/c/.../<id>` deep
link. `alerts.find_last_alert()` looks up a symbol's originating alert. Used by the flip warning
above to link back to the entry signal.

**Watchlist digest no longer looks like a trade signal.** `alert_transitions()`'s header now
reads "BT WATCHLIST ... -- no action, not a trade signal" and its per-row side tags are
lower-case (`long`/`short`); upper-case LONG/SHORT is now reserved for `alert_trade_signal()`,
the only message type the engine ever acts on.

## Recent Changes (2026-09-06)

### trades.db records the trade mode; per-strategy scorecard

`risk.db` has always keyed counters on `(mode, date)`; `trades.db` — the audit trail the ledger
and every report read — had no such column, so a paper week would land in the same table as the
221 real ORB trades with only the date to separate them. `db.set_trade_mode()` is called by
startup after `fetch_trading_mode()`. Rows written before the mode is known say `unknown`, never
`live`. The 221 pre-existing rows are deliberately not backfilled — the engine has an analyze
mode and an off-hours testing switch, so nothing in the data distinguishes them, and a NULL
analysis can see beats a guess it cannot. Mode is part of the ledger's position identity, so an
entry in one mode and an exit after a switch never merge.

`python -m signal_engine.analysis` gained a `BY STRATEGY` block split by mode, showing trades,
declines, win% and R per strategy. Never pooled across modes (a paper fill has no slippage in
it), and declines are shown beside trades because a strategy that fired 20 signals and traded 4
is a different thing from one that fired 4.

Where each strategy's data lives: `trades.db` (orders sent + signals declined, per strategy and
per mode), `risk.db` (counters, per mode), `breakingtrade.db` (scanner claims + BTST paper
ledger), tradebook snapshots (broker fills, joined on `order_id`), and the Telegram channels
(claims only — neither can see a fill). `trades.db` joined to the snapshots is the only
combination holding signal, order and fill together.

---

### PineScript token limit; day-summary one-shot guard

`breakout.pine` hit TradingView's compile limit (101,359 vs 100,256). This session's additions
were +339 raw tokens against an overrun needing -386, so the file was already ~50 tokens from
the ceiling. Comments and tooltips do not count — a string literal is one token however long.

Cuts: `sigId()` returns the finished alert line so each of five call sites is `msg += sigId()`
(-85 raw); `klTpReachOK` folded into the existing `roomOK` argument rather than a new gate
parameter (-8); duplicated direction ternary removed (-10); and **`buildKeyLevelAlert()` plus
`klFootprintCue()` removed (-456 raw, ~1,300 compiled)**. The packet's input defaulted to false,
its tooltip already said the entry alert carries the same context inline, and the engine ignores
KEYLEVEL packets — every field it carried is still emitted by `buildEntryAlert`. Headroom is now
~650 compiled tokens; the documented next move is splitting the key-level engine into its own
indicator.

**Day summary** was missing on 2026-09-04 only because the engine was not running (last log
2026-08-25). The delivery chain is sound. But `time_exit_all()` sent the summary then called
`_reset_day_counters()`, which cleared `_day_summary_sent` — killing the one-shot guard for the
rest of the day, so any later empty-book moment, or the watchdog's post-14:45 restart, sent a
false "No trades taken today." over the real summary. The guard is now the date sent, held in
process and in a `data/day_summary` marker so it survives a restart; the reset no longer clears
it, and an unreadable marker answers "not sent" rather than silencing the summary indefinitely.

---

### Mode profiles, notify_level, and automated EOD review

**`mode_profiles`** — `live` and `analyze` each state their own risk limits; the engine layers
the profile matching whatever OpenAlgo reports at startup over the base `risk:`/`sizing:`
values. Both modes are listed explicitly (a profile for only one mode is the confusing state
this removes), and an unknown mode falls through to base rather than guessing.

| | live | analyze |
|---|---|---|
| max_open_positions | 2 | 0 (unlimited) |
| max_trades_per_day | 10 | 0 (unlimited) |
| daily / weekly / monthly loss | 4% / 8% / 15% | off |

**Updated 2026-09-10, twice the same day — see that date's entries above for the full story.**
This table originally read 2/10 vs 6/24 (a SHARED total across the two live-in-sandbox
strategies at the time), then per-strategy isolation briefly made it 2/10 vs 3/12 (a real
per-strategy cap, no longer shared), then the analyze side was removed entirely (0 = unlimited)
once `max_open_positions`/`max_trades_per_day` stopped needing to protect anything real — see
`sizing.sandbox_capital`'s per-strategy note in `config.yaml` for why more concurrent paper
trades doesn't need more capital. `live` keeps its real 2/10, calibrated to actual Rs35k MIS
margin and still the only thing protecting real money.

Analyze is deliberately looser: with `save_declined` recording refusals, loose paper limits mean
every signal is taken, so the outcome of a signal live would have refused is observable and the
live limits can be replayed offline from the ledger. Tight paper limits never generate those
outcomes. Per-strategy isolation (2026-09-10) means `intraday-breakout` and
`intraday-breakingtrade` no longer compete for slots in the shared sandbox at all — each has its
own counters now, not just a bigger shared number.

Fixes a pre-existing bug: `risk_engine` is built at import with `trade_mode="live"` and was
never corrected, so paper losses would have been written into the LIVE row of `risk_store` —
the mixing that store keys on `(mode, date)` to prevent. `runtime.apply_trade_mode()` sets the
mode and reloads counters at startup, before any signal. Overridable keys are a fixed allowlist,
so a profile typo cannot set an attribute nothing reads.

**`telegram.notify_level`** (`quiet` | `normal` | `verbose`, set to `quiet`) — one entry sent
three messages and one exit two or more, ~120/day across two strategies at 24 trades. At `quiet`
six routine events are dropped and 13 kept. No failure event is in the level table at all
(rejections, SL failures, risk lockouts, orphans, engine start/stop always send), and an
unclassified event is delivered rather than dropped. `partial_exit` is `quiet`-level because it
books money — under extended runner tiers most trades end as a sequence of partials and never
send `position_closed`.

**`signal_engine/analysis/eod.sh`** — cron at 15:25 IST weekdays, writing
`analysis/reports/eod-YYYY-MM-DD.md`: position ledger, declined signals by gate, BreakingTrade
scanner-vs-engine review, and the day's `errors_{date}.jsonl`. 15:25 is bounded by broker
square-off finishing ~15:20 and `openAlgoAutoStop` at 15:30 — the tradebook snapshot is the only
source of fill prices and needs OpenAlgo up. A failed snapshot writes a warning banner into the
report and exits non-zero rather than silently producing a fill-less report. Weekday guard,
`flock` against overlap, accepts a date argument to re-run a past session. Reports and
`signal_engine/logs/` are gitignored as regenerable output.

---

### BREAKOUT paper week (ANALYZE mode); ORB stood down; SigID threads the trade

**Superseded same day:** live trading was prepared and then deliberately deferred. From
2026-09-07 BREAKOUT runs **one week of paper trading with OpenAlgo in ANALYZE mode**, reviewed
EOD daily. The mode is read at runtime from `/api/v1/analyzer` — it is not set in `config.yaml`.
`sandbox_capital` (Rs35,000) replaces the broker funds call and `risk_store` keys counters on
`(mode, date)` so paper and live totals never mix. Every limit below is kept at its intended
LIVE value on purpose: a paper run with limits the live account cannot honour produces numbers
that do not transfer.

`intraday-breakout` is `enabled: true`;
`intraday-orb` is `enabled: false` — not a retirement, an attribution decision. Two live
strategies sharing two slots on Rs35k means whichever fires first takes the margin and the
other's rejections read like signal quality.

**SigID** — `SYMBOL-YYYYMMDD-HHmm`, built from the entry BAR's timestamp and repeated on all five
alert types (entry, TP, SL, RUNNER, time exit). `ledger.py` joins on `order_id`, which exists
only once an order has been sent, so a rejected or unfilled signal had no key and fell back to
`(strategy, symbol, day)` plus arrival order — which mis-pairs two round trips in one name on one
day. New `Signal.sig_id` (first-class, not swept into `context`), a `sig_id` column added through
the existing additive `_ADDED_COLUMNS` path (migrated the live 221-row trades.db in place), and
`build_ledger` keyed on it when present. Keyless events reconcile exactly as before.

The normalizer's `_rewrite_tp_hit` / `_rewrite_sl_hit` build the canonical EXIT message from
scratch, so SigID was being dropped on exactly the messages that need it; `_sig_id_line()` now
carries it through.


**Declined signals are now persisted.** Every early return in `_handle_entry` (blacklist, T2T,
exposure, symbol/sector concentration, capital floor, `qty=0`, margin) and the validator gate in
`handle_message` (price filter, min_rr, min_sl_pct, duplicate, stale) exit before `save()`, so a
declined signal existed only as a log line. `db.save_declined()` writes a row carrying the
signal's prices, `context` and `sig_id`, so a refused signal can be scored later against the same
chart data as a taken one.

- Status `DECLINED` is distinct from `REJECTED` — the latter means the *broker* refused an order
  that was sent; DECLINED means none ever left the engine.
- `order_id` is the sentinel `-DECLINED-`, not empty: the ledger reads a falsy `order_id` as
  "sent but unmatched by any fill", a reconciliation error a decline is not.
- Excluded from the position ledger (`build_ledger`), read by `load_declined()`, and reported by
  `python -m signal_engine.analysis` grouped by the gate that stopped them (`--declined` lists
  each). Mixing them in would flag every decline as `NO_FILL` and bury the real unfilled orders.
- Never raises; EXIT signals are excluded.

**Test isolation, added after this bug bit:** wiring `save_declined()` into main wrote six rows
into the live `trades.db` on an ordinary `pytest` run, because the existing entry tests patch
`save` and knew nothing about a second writer. Rows deleted, 221 real ones untouched. An autouse
fixture in `tests/conftest.py` now repoints `db._DB_PATH` at a throwaway file for the whole
suite, so a future writer is covered before anyone remembers it exists.

**What the paper week cannot measure:** slippage (a sandbox fills at the requested price) and
margin (`_resolve_entry_quantity` skips `adjust_qty_for_margin` in analyze mode). Any expectancy
from it is an upper bound.

**Three settings corrected, and kept for the paper week:**

- `max_open_positions` **4 -> 2**. At 1% risk with a ~0.30% stop, notional is ~3x capital, so at
  20% MIS margin one position needs ~Rs21k of Rs35k (measured median Rs21,123, 60% of capital).
  Slots 3 and 4 could never be funded; leaving them produced broker rejections that looked like
  signal quality.
- **Loss limits re-armed** — daily 4%, weekly 8%, monthly 15%, from `1.0` (disabled) under a
  `# TESTING` comment. At `max_trades_per_day: 10` and 1% risk, off means a 10% day has no floor.
  4% is four full stops and would have fired on 03 Sep, which took exactly four.
- `min_entry_price` **150 -> 300**, for slippage not edge. NSE tick is Rs0.05 on every sampled
  name (verified against `symtoken`), so one tick is 0.143R on IEX at Rs118 and 0.063R on ITC at
  Rs265, against a `slippage_factor` budget of 0.10R for a two-leg round trip. `max_entry_price`
  deliberately left at 5000 — high-priced names execute cheapest, but widening the universe on
  day one adds variance with no evidence; revisit after 2-3 live weeks.

**Logging for the EOD debrief:** `backtrace=True` on the file sink (`diagnose` stays off — it
renders local variable values, and the locals around an order call hold the API key and broker
token), plus a second `logs/errors_{date}.jsonl` sink at ERROR, serialised, 90-day retention.
Mirrors the root CLAUDE.md `log/errors.jsonl` convention.

Tests: `test_signal_id.py`, `test_logger_setup.py`, `test_listener_channels.py`. 798 passed.

**Still true and worth knowing:** `smidestn` (test channel) remains enabled and can inject
signals into what is now a live-money engine.

---

### Per-channel enable/disable in `telegram.channels`

`telegram.channels` entries take an optional `enabled` key (default `true`, so existing configs
are unchanged). It gates **subscription**: a disabled channel is not listened to, so no trade is
ever taken from it, but it is still parsed and still carried in settings so startup logs it as
deliberately off. All-channels-disabled is reported as a distinct, loud failure from an empty
list. `enabled: "false"` in quotes is handled explicitly — YAML makes that a truthy string.

Prefer `enabled: false` over deleting a block. BREAKOUT's paper phase was expressed as an
absence from this list, which is indistinguishable from an oversight and cost a database query
to establish; `intraday-breakout` (`-1004450500772`) is now listed with `enabled: false` and a
comment saying why.

This is not a shadow/dry-run mode — the engine does not read, size, validate and then decline to
trade. See `analysis/ledger.py` for why a Telegram channel cannot be the proof of trade quality
in the first place.

---

### Week-1 BREAKOUT forward review, and Findings 1-3 implemented

First forward review of the key-level BREAKOUT engine. **PineScript-only paper phase by design
— the engine is not subscribed to `intraday-breakout` and `trades.db` holds zero BREAKOUT rows,
ever.** Every figure below is chart simulation: theoretical fills, no broker, no slippage, no
rejections. Sample: 31 Aug - 04 Sep, 20 entries, 18 resolved, **+4.31R, 8 wins / 10 losses
(44%)**. Pooled with 26-28 Aug:
30 resolved, +6.27R. The full measurement, the filters that were tested and rejected, and the
confidence ledger live in `pinescripts/intraday/orb/breakout.md` — that file is the source of
truth for this strategy's analysis and is not duplicated here.

The result does **not** overturn the 2026-08-30 negative backtest (30.9% follow-through vs a
33.3% random-walk baseline). 18 trades is inside chance for a process with a fixed -1R downside
and ~1.8R winners. It does show the asymmetric exit structure can carry a sub-50% hit rate, so
collection continues.

Three changes shipped, plus one defect the work uncovered:

**1. Every trade now emits a terminal event.** 4 of 20 trades had no recorded ending, which
silently corrupts every number the review produced. Root cause for two of them: `strategy.exit()`
carries no `qty_percent`, so the strategy model closes 100% at TP1 while the engine keeps the
runner — after which the SL alert is (correctly) suppressed by `anyTPBooked` and the 14:45 time
exit was gated on `strategy.position_size != 0`, already zero. New `orbOpenQtyPct` state variable
tracks the **engine's** remainder independently; the time exit and a new end-of-session backstop
both run off it. The other two went silent for a reason still unidentified, so both gates were
placed at the top level of the script, outside the `not orbLinesFrozen` block that governs
per-bar TP/SL detection.

**2. Extended runner tiers enabled** — `bracket.use_extended_runner_tiers` and breakout.pine's
`useExtendedRunnerTiers` are both now `true`. Booking is 30/35/35 across TP1/TP2/TP3 instead of
50/50 closing at TP2. The week produced two `RUNNER` observations at 4.50R unbooked each
(RADICO, HAVELLS); re-scoring the printed alert prices under 30/35/35 gives +6.62R against the
+4.31R actually booked, with no change to which trades were taken or risk per trade.

**3. TP1 reachability gate** — new `klMaxTpR` input (default 2.0R, 0 disables) in breakout.pine,
threaded through `klFireGate`. TP1 is the nearest structural level floored at 1R with no ceiling,
and the whole ladder scales off it, so a far level draws a first checkpoint no session can reach.
Pooled, 4 setups drew TP1 beyond 2R; none booked anything and two of them are among the four with
no recorded ending. The setup is **skipped**, not rescaled — rescaling would re-introduce the
arbitrary R-multiple exit the key-level engine exists to replace.

**Defect found while implementing 2:** `_replace_runner_sl()` returned as soon as
`structural_runner_sl()` gave it a price, and that function returns a price for **every**
key-level trigger, floored at break-even. So every BREAKOUT runner's stop was pinned at
break-even for the life of the trade and the extended-tier ratchet added on 2026-09-02 was
reachable only for plain ORB breakouts. Harmless while TP2 closed the position outright; a real
leak once 35% is held past TP2 behind a break-even stop after price has run 2R — **flipping the
toggle without this fix would have made the strategy worse.** The structural stop is now a floor
rather than the answer: both candidates are computed and the tighter wins, and a stop still never
loosens. A second bug in the same function surfaced with it — `anchor_level` was only bound in
the `elif`/`else` branches, so the `logger.info` after a successful SL placement raised
`UnboundLocalError` on every key-level runner SL.

Tests: `test_main_partial_exit.py::TestKeyLevelRunnerRatchet` — TP2 ratchet above the structural
floor (long and short), TP1 keeping a tighter structural stop, flag-off reverting exactly, and
the gap-through case (one 5-min bar spanning TP1 to TP2, so the engine sees TP2 with no preceding
TP1 and `pos.sl` still at the original loss-side stop). 772 passed.

**Known limitation:** a runner stopped out broker-side after a partial now records a `TIME_EXIT`
event priced at the 14:45 close rather than the actual stop price, so its R will be wrong —
wrong and visible, against absent before. Emitting the engine's own terminal event is the next
iteration.

**What to watch:** `BREAKOUT EXIT / Reason: TIME_EXIT` messages appearing at all; `TP2 HIT`
reading `ExitQtyPct: 50` rather than 100; `TP3 HIT` on trades that previously produced only a
`RUNNER` note; and fewer entries per day from the reachability gate.

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

### Telegram alert audit: exit notifications restored, two day-summary bugs fixed

Prompted by a direct question: does the channel actually receive alerts for entry, SL, and
multi-TP exits? Audited every `notify_*` function against its live body (not the docs, which
had drifted) and found the 2026-04-22 "Notification Reduction" entry below still describes
the current code exactly — `notify_entry_filled`, `notify_sl_placed`, `notify_partial_exit`,
`notify_position_closed`, `notify_be_stop_applied`, and `notify_no_progress_exit` are all
still log-only. Concretely: **no Telegram alert fires for a multi-TP partial exit, or for a
position closing via SL or final TP** — only entry placement, failures, time exits, risk
halts, and the day summary reach the channel. Given the point of the last two sessions' work
was fine-grained TP1/TP2/TP3 and runner-SL behaviour, that is a real visibility gap, not a
documentation gap.

Restored all six to real Telegram sends (kept the existing log line in each; added a
`notify()` call using the same emoji-header format as every other message in this file), and
fixed a defect that restoring `notify_be_stop_applied` to a *live* channel made worth fixing
rather than leaving as documented-but-broken: `original_sl` was always `None`, because
`_no_progress_break_even` read `pos.sl` for the notification *after* already overwriting it
with `be_price`. Now captured before the overwrite. New test file
`test_notifier_restored_alerts.py` pins all six functions calling `notify()`, and the
`original_sl` fix specifically.

Two independent bugs found while re-reading the day-summary path for accuracy, both fixed:

- **Time-exit P&L never reached the risk engine's loss counters.**
  `tracker.py`'s `_book_one_time_exit` called `risk_engine.record_close(pnl=0.0, ...)`
  unconditionally — a leftover from the function's original form (commit `157ae0f02`), before
  per-position P&L was computed at all, never updated when it was added. The Telegram
  day-summary total was already correct (`_day_pnl` is accumulated separately from the same
  broker delta); `daily_realised_loss` / `weekly_realised_loss` / `monthly_realised_loss` were
  not, so a real loss on a time-exited position was invisible to the loss-limit circuit
  breaker. Fixed to pass the real `total_pnl`.
- **A mid-session close could permanently truncate the day summary.** The three signal-driven
  full-close paths in `main.py` (`_reconcile_sl_hit`, the full-exit branch of
  `_book_exit_result`, `_finalize_invalid_partial`) sent the day summary the instant
  `tracker._positions` went empty, with no time-of-day check. `check_positions`' own
  `_maybe_send_day_summary` already had this guard (deferred to within 30 min of the
  scheduled time exit, to avoid a poll-detected ghost-close sending a premature summary) but
  the signal-driven paths never used it. Since `send_day_summary()` is one-shot
  (`_day_summary_sent`), a genuine SL/TP exit that happened to empty the book at, say, 10:30
  AM — while the entry window and `max_trades_per_day` still had room — would send an
  incomplete tally and then never send the real one, even though later trades kept updating
  the in-memory counters correctly. Unified behind one new `PositionTracker.maybe_send_day_summary()`,
  used by all four close paths; `_maybe_send_day_summary` (the polling one) now delegates to
  it. New tests in `test_tracker_close_detection.py`
  (`TestMaybeSendDaySummarySharedGate`) exercise the shared gate directly: still-open
  no-op, mid-morning skip, near-EOD send.
- Removed `PositionTracker.projected_day_context()` — zero callers repo-wide, dead since the
  close-path unification documented in `REFACTOR-LOG.md`.

Confirmed, not changed: P&L is sourced from real broker data throughout (not estimated);
win/loss/time-exit classification is deliberate and clearly labelled (`W: x L: y T: z`, win
rate computed over decided trades only). Known, accepted limitation left as-is: when multiple
MIS positions time-exit in the same batch, P&L is split equally across them
(`_book_time_exit_trades`) — the aggregate day total is exact, individual per-symbol
R-multiples in that scenario are approximate. Making it exact needs serial per-position broker
closes instead of the current bulk `close_all_positions`, a larger change than this pass.

Tests: `test_notifier_restored_alerts.py` (new), `test_tracker_close_detection.py`
(`TestMaybeSendDaySummarySharedGate`, new), `test_tracker_time_exit.py`
(`test_time_exit_loss_is_recorded_on_risk_engine`, new). 653 tests green.

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

`pinescripts/intraday/orderflow/indicator-candlestick-patterns.pine` (repo32) retuned. All 14
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

### `report-market-structure.pine` retuned for NSE

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

### `indicator-support-resistance.pine` retuned (`pinescripts/intraday/orb/`)

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

## Remaining Strategies on the Rebuilt Dataset (2026-09-13)

Swing strategies (`gap_rsi`, `phoenix`, `value_zone`) run on the yfinance 10-year
adjusted daily panel, 201 F&O names, 2016-2026, IS/OOS split at 2022-09-09. Intraday
ones queued behind the F&O backfill (see below) because Historify's DuckDB file takes
an exclusive lock while being written and cannot be read concurrently.

| Strategy | OOS trades | OOS net R | OOS clustered t | Verdict |
| --- | --- | --- | --- | --- |
| **gap_rsi** (extreme-RSI breakaway gap) | 3,716 | -0.231 | -2.18 | Negative, clears the hurdle in the wrong direction. Even the isolated LONGS-only book is a coin flip (t=-2.31 despite +0.039 mean R — see note below). SHORTS alone: -0.491R, t=-6.31. Do not trade. |
| **phoenix** | 156 | +0.085 | 1.85 | Promising but NOT proven — OOS \|t\| (1.85) is below the single-trial hurdle (1.97), let alone the 10-trial hurdle (2.83) this session has now run up. All trades are LONGS (shorts fire zero times); 82.5% OOS win rate on few trades. Needs more sessions before a verdict, not a trade. |
| **value_zone** | 1,305 | +0.033 | **7.54** | The strongest result in this session, OOS and clustered. 76% win rate, 3,151 total trades, gross ≈3.1% per trade before cost. **But it trades with NO real stop-loss by design** (`stop_mode="none"` reproduces the source Pine, which has none — `sl` is parked 99% away and MAX_HOLD (120 sessions) is the only floor). MAX_HOLD exits are 14.1% of trades at -20.7% average loss. Position sizing must assume a name can be held for six months and give back a fifth of its value before this strategy's own logic closes it. |

**Why gap_rsi's "longs only" row shows a negative t with a positive mean R:** this is
the clustering correction catching something real, not a bug. `net_R` weights every
trade equally; the clustered `t` weights every SESSION equally. A few sessions with a
pile of simultaneous long entries are dragging the per-trade average positive while
most individual sessions lose. On a session picked at random, the longs book is more
likely to be a loser than net_R alone suggests - which is exactly the failure mode
`t_naive` vs `t` exists to expose (see [[project-backtest-validation-suite]]).

**None of these three would pass the 10-trial hurdle (\|t\| > 2.83) except value_zone.**
That is the correct number of trials to hold them to: this session alone has now run
ORB, EMA9, gap_rsi, phoenix and value_zone at the shipped defaults plus each one's
cost-sensitivity sweep.

## Broker Choice for Deeper History, and the yfinance Policy (2026-09-13)

### Angel One over mStock - evidence, not a guess

Commit `48807197` (`fix(angel): pace rate limits to real caps, eliminate history gaps,
speed up streaming`), already in this repo, states: **"Validated: 2-year 1-minute
history is gapless across NSE, NSE_INDEX, BSE, BSE_INDEX, NFO, BFO and MCX."** That is
a tested claim about Angel's own plugin, not a marketing number - and it already beats
Flattrade's measured ~12-13 months with a mid-window gap. No equivalent claim exists
for mStock anywhere in this repo's history. Recommendation: **Angel One** is the
broker to set up in `.secondary_broker.env` first.

This is still a claim about the PLUGIN CODE's tested behaviour, not this specific
account's live data. `broker_login.py --probe` (now checks 5 symbols, not one, since
Flattrade's own depth turned out to vary by symbol) is what turns "should be ~2 years"
into a verified number before a multi-hour backfill is run against it.

### yfinance: cross-check only, never a backtest data source

Confirmed policy, now the actual default (`__main__.py` and `data.py`): Historify is
the ONLY source a reported backtest result may be built on. yfinance is kept for
exactly one purpose - `dataquality.cross_source()` compares Historify's daily closes
against Yahoo's as an INDEPENDENT check that a broker price is not systematically
wrong, which two draws from the SAME source could never catch. It is not a fallback
for missing history: yfinance's own 60-day intraday cap and 49.2% session-completeness
(measured 2026-09-12) make it strictly worse than Historify everywhere Historify has
any coverage, and it carries a per-symbol rate limit Historify does not. `--source
yahoo` remains for the one legitimate case - a symbol Historify has not been
backfilled for yet - and should be treated as informational, never final.

### Provenance: Historify's schema has no broker/source column

`market_data`'s primary key is `(symbol, exchange, interval, timestamp)` - there is
nowhere to record WHICH broker a given historical bar came from. Backfilling the same
symbol/date range from two different brokers would silently make that unanswerable
later if one is ever found to disagree with the other.

Design choice made here rather than a schema migration: **keep the ranges
non-overlapping**. Flattrade continues to own the recent window (its own daily catch-up
keeps it current for live trading); a second broker is used ONLY to reach further back
than Flattrade goes, into dates Flattrade has already returned "no data" for. Under
that split, provenance is recoverable from the date alone without touching the live
Historify schema. A `source` column is possible if full per-bar provenance is ever
needed, but that changes a table the live Historify feature also writes to and needs
a proper migration (see CLAUDE.md's Schema Changes rule) - not done speculatively.

## Angel Backfill Relaunched After Results Were Captured (2026-09-13, 16:07 IST)

Killed the first attempt seconds after starting it (nothing lost) to run the 8
strategies above FIRST, since a concurrent Historify read fails immediately on
DuckDB's file lock - there is no safe interleaving. Relaunched now that those results
are captured in this file. Expected runtime ~4 hours (211 symbols x ~9.7 years x
30-day chunks, paced by Angel's own internal rate limit). Once complete: re-run the
5 intraday strategies above - they should finally use the full universe instead of
the ~48-symbol subset that clears `min_sessions=250` today.

## Remaining Strategies Backtested (2026-09-13)

All on Historify (no yfinance), `--trials 15` (this session's actual configuration
count across every strategy and sweep run so far).

### Intraday (5-minute, cost 16 bps) - still the pre-Angel-backfill universe

These ran on the ~48 symbols with 250+ SESSIONS of history (`min_sessions=250`
default) - the 164 newly-covered symbols have ~250 calendar days of 1m data each,
which lands right at that threshold and mostly didn't clear it. **These results will
change, likely for the better on sample size, once the Angel backfill (below)
extends everyone comfortably past it.**

| Strategy | OOS trades | OOS net R | OOS clustered t | Verdict |
| --- | --- | --- | --- | --- |
| **dhb** | 220 | -0.197 | -2.36 | Negative at shipped defaults; the earlier "not tradeable" finding (from a smaller yfinance sample) holds up on more data. |
| **key_level** | 1,647 | -0.496 | -13.96 | Clearly negative - matches the standing PRD finding that every reference level has a negative break edge (fade, not breakout). |
| **ema9_vwap** | 14,417 | -0.257 | -16.93 | Clearly negative. Introduced `SL_GAP_ENTRY` (2 trades) - the tag added this session for a fill that gaps through its own stop, per the live validator's actual behaviour. |
| **ema9_pdf** | 2,224 | -0.500 | -14.97 | Clearly negative. DOJI exit (13.3% of trades, +0.912R average) is the one bright spot in the exit mix - worth isolating as its own hypothesis later. |
| **ib_extension** | 0 | - | - | **CORRECTED 2026-09-14: this WAS a bug, not a real finding** - see the 2026-09-14 entry below. `use_regime=True` (shipped default) silently rejected every signal in both directions because `load_regime()` was never called anywhere in the codebase; the double-breakout condition itself was never actually tested by this run. |

### Swing (daily, cost 24 bps, Historify's OWN split-adjusted daily bars - not yfinance)

197 symbols now (was 201 on yfinance), 2019-12-03 to 2026-09-13, IS/OOS cut
2023-12-28. Confirms the yfinance-based verdicts from 2026-09-12 transfer to
broker-sourced daily data:

| Strategy | OOS trades | OOS net R | OOS clustered t | Verdict |
| --- | --- | --- | --- | --- |
| **gap_rsi** | 2,696 | -0.168 | -1.84 | Negative, confirming the 2026-09-12 yfinance-based verdict on independent broker data. Same "longs only" clustering artifact as before (t=-0.33 despite net_R=+0.136 - a few days concentrate many simultaneous long entries). |
| **phoenix** | 131 | +0.094 | 1.88 | Still promising, still NOT proven - OOS \|t\| (1.88) clears the single-trial hurdle (1.97) barely below it, well short of this session's actual 15-trial hurdle (2.96 - see `hurdle_t(15)`). More sessions needed, not a trade yet. |
| **value_zone** | 994 | +0.021 | **3.24** | Holds up on independent broker data: still the strongest result in the portfolio. Clears the 15-trial hurdle (2.96). 68.3% OOS win rate. The no-stop-loss caveat from 2026-09-12 stands unchanged - MAX_HOLD exits are 11.6% of trades at -18.7% average loss, and this is still by design (`stop_mode="none"` reproduces the source Pine, which has none). |

### Two bugs found and fixed while running these

1. **`data.from_historify_daily()` crashed on every call.** `.dt.tz_convert(...).normalize()`
   is missing a second `.dt` - each `.dt.method()` call returns a plain Series, not
   another datetime accessor, so `.normalize()` needs its own `.dt` prefix. This
   crashed `gap_rsi`, `phoenix` and `value_zone` on their first attempt. Fixed; pinned
   by an end-to-end test against a real temporary DuckDB file
   (`TestFromHistorifyDaily`), not just the pure adjustment math already covered by
   `TestSplitAdjustedDaily`.
2. **`metrics.by_symbol([])` crashed instead of reporting zero trades.** Building a
   DataFrame from an empty list of dicts produces a frame with NO columns, so the
   subsequent `.sort_values("total_R", ...)` raised `KeyError: 'total_R'` - which is
   exactly what `ib_extension` hit, since it never fired a signal at all. A strategy
   that fires zero times over the window is a real, reportable finding and must not
   crash the `--full` report. Fixed with the same empty-guard pattern `by_reason()`
   already used; pinned by `TestBySymbolEmptyTrades`.

### A sandbox restart happened mid-run - what survived and what didn't

Historify's data (persistent volume) and every code/PRD/memory change survived
intact. Every `/tmp` log and in-flight background process did not - the 8 strategy
backtests above had to be re-run from scratch, and this section itself is written
promptly rather than held until a longer pipeline finishes, per the lesson from the
previous entry.

## Logging Bug Found Mid-Run (2026-09-13, does not affect data correctness)

`utils/logging.py`'s console formatter falls back to `record.msg` (the raw %-style
template) instead of `record.getMessage()` (the substituted string) whenever
`super().format(record)` raises - reproduced directly, not just observed once. Every
`logger.warning`/`logger.info` call in `backfill.py` and `broker_login.py` using
%-style lazy args (`logger.warning("%s", x)`) showed the literal `%s` on console
instead of the value. Fixed by switching those calls to f-strings (already the
dominant convention elsewhere in this codebase) - not a fix to the shared formatter
itself, which is cross-cutting production logging infrastructure out of scope here.

**Does not affect any data written to Historify** - only the human-readable progress
log during the run. The Angel re-fill launched before this fix carries the bug in its
already-running process (Python does not hot-reload); its FINAL summary numbers
(compared/mismatches/worst) are computed directly from the returned `SymbolResult`
objects and printed via f-string, not parsed from the log, so they are correct
regardless. Only the interim per-chunk diagnostic lines in `angel_full_refill.log`
are garbled for this one run.

## Full Angel Re-Fill, Consistency Check, and PineScript Inventory (2026-09-13)

Three follow-up requests, addressed in order.

### 1. Angel as the authoritative source, not just a gap-filler

Original plan kept Flattrade and Angel non-overlapping by date to avoid needing a
comparison step. Superseded: `backfill()` gained `compare_existing=True` (now
`broker_login.py`'s default). Before every chunk write, it reads whatever Historify
already has for that exact (symbol, timestamp range), compares CLOSE within
`COMPARE_TOLERANCE_PCT` (0.5%), logs a warning naming the symbol/range/worst
disagreement if any bar disagrees beyond that, and then writes the new (Angel) value
over it regardless - Angel is authoritative for the whole range, agreement or not.
This is a full re-fill: **2016-01-01 through today**, all 211 F&O symbols, superseding
the earlier `--end 2025-09-17` non-overlap design. Running now (started 17:24 IST);
see `angel_full_refill.log` for the live run, or the final printed summary for total
bars compared / disagreed / worst diff.

### 2. How to confirm the backfill reached everything Angel actually has

Three checks, in order of how much they tell you:

    # A. Per-symbol coverage: first date, last date, staleness
    uv run python -m signal_engine.backtest.backfill --status

Read the `first` column: it should cluster around 2016-2019 for most symbols (Angel's
own ceiling, per the probe, sits somewhere between 7 and 10 years back - not a single
clean date, so some spread across symbols is expected and correct, not a bug).
A symbol still showing a 2025-09-18 `first` date after the full run finishes did not
gain from Angel - worth investigating THAT symbol specifically (some F&O names are
newer listings and genuinely have less history at any broker).

    # B. Whether Angel and Flattrade agreed where they overlapped
    tail -100 angel_full_refill.log | grep -A5 "agreement with the existing store"

Reports total overlapping bars checked, how many disagreed beyond 0.5%, and the worst
single disagreement. Zero or near-zero mismatches is the expected, reassuring
outcome - it means Flattrade was already accurate and Angel confirms it, not that the
comparison did nothing. A high mismatch rate would be the actual red flag, worth
individually inspecting via `dataquality.py --cross-check`.

    # C. Data quality on the result (impossible bars, session completeness, gaps)
    uv run python -m signal_engine.backtest.dataquality --source historify --interval 5m

This is the same audit that found yfinance's 49.2% session completeness back on
2026-09-12 - re-run it against the refilled store to confirm Angel's bars are as
clean as Flattrade's were, not just deeper.

### 3. PineScript inventory - what still needs backtesting

Every file under `signal_engine/pinescripts/`, classified by whether it declares
`strategy()` or `indicator()` in Pine, and whether it fires the structured
Entry/SL/TP/Direction alert `signal_engine/parser.py` actually executes on (not just
a generic notification):

| File | Type | Backtest status |
| --- | --- | --- |
| `orb/orb.pine` | strategy | `orb` adapter - run |
| `orb/breakout.pine` | strategy | `key_level` adapter - run (PD/IB families only; PVAH/PPOC/PVAL sub-family explicitly out of scope, needs 1-min volume-profile reconstruction - see `key_level.py`'s own docstring) |
| `ema9/ema9-intraday.pine` | strategy | `ema9` adapter - run |
| `ema9-vwap/ema9-vwap.pine` | strategy | `ema9_vwap` adapter - run |
| `ib-extension/ib-extension.pine` | strategy | `ib_extension` adapter - run (0 trades - the double-breakout condition never fired) |
| `swing/dividend-growth/dividend-growth.pine` | strategy | `value_zone` adapter - run (strongest result) |
| `intraday/intraday-dhb.txt` | discretionary write-up, no compiled pine | `dhb` adapter - run |
| `swing/momentum-rank/momentum-rank.pine` | indicator (alert-only, full Entry/SL/TP/Direction contract) | **This IS the live deployment of the 12-1 cross-sectional momentum result already validated in `portfolio.py`** (the gold-standard +11.25%/yr alpha finding, 2026-09-12). Its own header cites the same backtest (+12.7%/yr alpha, t=3.12) run against 201 F&O names 2016-2026 - a later, slightly different parameter sweep than this session's, both independently positive. Re-running via `portfolio.py` against Historify daily (once the Angel re-fill lands) will give the final, broker-verified number to compare against the header's claim. |
| `orderflow/indicator-candlestick-patterns.pine` | indicator, no alert | Not a strategy - pattern overlay only, no entry/exit/stop of its own |
| `orderflow/concept-initiative_drive_detector_v6.pine` | indicator, alert fires direction+score | Context alert, not a complete trade signal - no SL/TP/entry price, meant to inform a discretionary decision |
| `orderflow/indicator-keylevel-candles.pine` | indicator, alert fires break/fake verdict | Same - context, not a complete signal |
| `orderflow/report-market-structure.pine` | indicator, generic alert | Visual structure marking only |
| `orb/indicator-support-resistance.pine` | indicator, no alert | Visual zone marking only (already discussed earlier in this file - retuned, `alertMode` defaults to Reject per the level-study evidence) |
| `orb/indicator-orb-luxy-big-beautiful-dynamic-orb.pine` | indicator, generic alert | Visual dynamic-ORB overlay, no embedded stop/target |
| `volume-profile/concept-smart-money-concepts-luxalgo.pine` | indicator, no alert | Third-party (LuxAlgo) order-block/liquidity visualization only |
| `volume-profile/indicator-volume-heatmap.pine` | indicator, no alert | Visualization only |
| `volume-profile/indicator-volume-suite.pine` | indicator, no alert | Visualization only |
| `volume-profile/report-volume-profile-decision-assist.pine` | indicator, alert fires a formatted message | Decision-support alert, not a complete trade signal |

**Conclusion: every actual STRATEGY in this repo (a script with defined entry, stop
and target logic) already has a Python backtest adapter and has been run this
session or the previous one.** The 9 remaining files are indicators or context alerts
- they inform a discretionary trader but do not themselves specify a complete,
backtestable trade (no stop, no target). Backtesting one of them would mean
INVENTING a strategy around its signal (e.g. "buy when initiative-drive score exceeds
X, with a Y% stop") rather than converting an existing, specified one - a new
strategy-design task, not a re-run, so none were attempted without being asked.

## Angel One Probe Result: ~7 Years, Confirmed (2026-09-13)

User connected Angel One via `broker_login.py` (credentials in
`.secondary_broker.env`, never touching `auth_db`). `--probe` result across 5 symbols
(RELIANCE, TCS, SBIN, TATASTEEL, HDFCBANK), 1-minute interval:

    days_back   week_of      bars
       30       2026-08-14   1805
       90       2026-06-15   1875
      180       2026-03-17   1875
      365       2025-09-13   1875
      545       2025-03-17   1875
      730       2024-09-13   1875
     1095       2023-09-14   1500
     1460       2022-09-14   1875
     1825       2021-09-14   1875
     2555       2019-09-15   1875
     3650       2016-09-15      0

**Gapless real data at every offset up to 2555 days (~7.0 years) on all 5 symbols,
consistent with commit `48807197`'s own "2-year gapless" claim being a floor, not a
ceiling. Zero at 3650 days (~10 years) - the true boundary sits somewhere in between.**
This is ~7x Flattrade's measured ~1 year, and does not share Flattrade's mid-window
gap (150-240 days back).

### Backfill plan: non-overlapping by design, not by schema

Angel backfill covers **2016-01-01 to 2025-09-17** (one day before the shallow
164-symbol group's Flattrade coverage begins) for the full 211-symbol F&O universe -
including the 47 symbols that already have Flattrade data back to 2020, since Angel
plausibly reaches further still and the ranges do not overlap (Angel: pre-2020,
Flattrade: 2020-onward for those 47). This preserves the provenance-by-date design
from the earlier entry without touching `market_data`'s schema.

`--pause 0.1` (down from Flattrade's 0.4): Angel's own plugin already rate-limits
itself internally (`HISTORY_MIN_INTERVAL = 0.5s`, module-level, shared across calls in
this process) - added external pacing on top only slows the run down.

**Must run to completion before any read of Historify is attempted** - `upsert_market_data`
opens and closes a connection per chunk, but a concurrent `read_only=True` connection
still fails immediately on DuckDB's file lock rather than waiting; there is no safe
window to interleave a strategy read with a live backfill.

### A genuine sandbox restart happened mid-session

The background pipeline from the previous entry (steps 1-3) completed successfully
before the sandbox recycled - Historify's data survived (persistent volume) but every
`/tmp` log and the pipeline's own progress markers did not. Steps 4-5 (the 8 remaining
strategy backtests) had to be re-run from scratch; no data was lost, only computed
results that hadn't been captured into this file yet. Lesson: write findings into
PRD.md as they land, not only at the end of a long background run.

### Coverage gaps: 3 of 211, and why

`GVT&D` and `COCHINSHIP` each have TWO `symtoken` rows for the same (symbol, exchange)
- a `-BE` (trade-for-trade) and a `-EQ` (normal) series with different tokens.
`database.token_db_enhanced.get_token_dbquery` does
`SymToken.query.filter_by(symbol=symbol, exchange=exchange).first()` with no
disambiguation, so it can silently resolve to the illiquid BE token, which the broker
then returns no history for. `M&M` is missing only from the `D` interval for the same
underlying reason. `NIFTYFPI` has zero rows in `symtoken` for exchange='NSE' at all -
it is not a valid, currently-listed symbol on this instance, matching the
"possibly delisted" error yfinance gave it earlier.

Not fixed here: `get_token_dbquery` is a core, widely-used lookup the whole live
platform depends on, not a backtest-only function - changing its disambiguation rule
needs its own review, not a byproduct of a backtest data pipeline. 208/211 (98.6%)
F&O coverage is high enough to proceed; these three are noted, not chased further.

## Full F&O Universe Backfill and Historify-Only Data Pipeline (2026-09-13)

Triggered by: "is data available for all stocks in nse exchange or only for specific
stocks" + "I would prefer not to use yfinance due to rate limits ... use openalgo
historify instead ... fetch all available information available for all fno stocks
universe".

### The F&O list itself was stale

`data.NSE_FNO` (hardcoded) had 209 names; the live `symtoken` table has 211 - three
new listings (`ATHERENERG`, `MAHABANK`, `SAGILITY`) were missing and one delisted name
(`DALBHARAT`) was still in the list. Synced via `data.refresh_fno()`.

### Coverage before this session: 44 of 211 F&O names had ANY Historify data

167 names - spanning nearly every sector, not a handful of niche tickers - had zero
rows. `backfill.py --symbols <167 names> --start 2025-09-08 --interval 1m` is
backfilling all of them to Flattrade's practical ceiling (see below).

### Flattrade's real intraday depth ceiling, re-measured

Bisected against three fresh symbols (ADANIPORTS, COALINDIA, HINDALCO), not just the
one used on 2026-09-12: data is present at 120 days back, ABSENT from 150-240 days
back (a real gap in the broker's own retention, not a clean boundary), and PRESENT
again intermittently from 270-360 days back. There is no clean "12 months and no
further" line - it is uneven. A background pass (`backfill_1m_deeper.log`) pushes the
167 newly-covered names all the way to 2020-01-01 to check exhaustively rather than
assume, at the cost of mostly-empty requests beyond ~14 months.

### Historify replaces yfinance for swing/daily work too

`data.from_historify_daily()` (new) pulls Historify's `D`-interval bars and back-adjusts
them for splits/bonuses with `_split_adjust_daily()` - the same guarantee
`auto_adjust=True` got from yfinance, now sourced from the broker feed already in this
instance instead of a rate-limited external API. Historify's daily retention reaches
further back than its 1-minute retention (small daily candles cost the broker far less
to keep), so this is usually the DEEPEST panel available here. Pinned by 5 tests in
`test_backtest_integrity.py::TestSplitAdjustedDaily` (single split, two compounding
splits, high/low scaled with close, volume deliberately left unadjusted).

`__main__.py`'s swing path now defaults to `--source historify`; `--source yahoo`
remains as an explicit fallback for a symbol Historify has not been backfilled for.

### Full pipeline running this session (`pipeline.sh`, background)

1. Finish the 167-symbol, ~1-year 1m backfill already running.
2. Push those same 167 symbols back to 2020-01-01 (exhaustive depth check).
3. Backfill `D`-interval bars for the full 211-symbol F&O universe (one request per
   symbol - daily's chunk size is 4000 days, so this step is minutes, not hours).
4. Re-run the remaining intraday strategies (`dhb`, `ib_extension`, `key_level`,
   `ema9_vwap`, `ema9_pdf`) against the now-current, now-complete Historify universe.
5. Re-run the three swing strategies (`gap_rsi`, `phoenix`, `value_zone`) against
   Historify's own split-adjusted daily bars instead of yfinance, and diff the results.

### Second-broker path, for going deeper than Flattrade can

`signal_engine/backtest/broker_login.py` (new): authenticates directly against Angel
One or mStock - both already OpenAlgo plugins - using credentials from a SEPARATE,
git-ignored `signal_engine/backtest/.secondary_broker.env`, and NEVER calls
`database.auth_db.upsert_auth`. This matters because `upsert_auth` is keyed by
username alone, not (username, broker): logging a second broker in through OpenAlgo's
normal web flow would silently replace the live Flattrade session this instance trades
on. `--probe` authenticates and reports how far back 1-minute data actually goes,
without writing anything, so the decision to run a real backfill is made on evidence.
Not run this session - needs the user's own broker credentials, supplied via that file,
never through chat.

## Backtest Trust Review (2026-09-12)

Triggered by the right question: almost every strategy tested came back negative, and
there was no way to tell a real verdict from a broken harness.

### The harness was not the problem. The dataset was.

Four checks now run as `backtest/validation.py`, each with an answer established
outside this repo, so a disagreement is a bug HERE:

| Check | Expected | Result |
| --- | --- | --- |
| A. Overnight vs intraday return | overnight carries the premium | PASS — median overnight **+54.0%/yr** vs intraday **-22.8%/yr**, overnight wins in **100%** of 48 symbols |
| B. Engine differential — panel arithmetic vs the simulator | identical | PASS — **0.0 bps** apart over **48,815** trades |
| C. Cross-sectional momentum 12-1 | positive alpha | PASS — **+11.25%/yr** over an equal-weight benchmark, t = 2.86, Sharpe 1.43, 201 names |
| D. Short-term 1-month reversal | negative alpha | PASS — **-1.12%/yr** (weak, t = -0.10) |

C and D are run as a pair on purpose. The classic backtest bug — filling on the signal
bar instead of the next one — makes a ranking strategy read its own outcome and would
print BOTH as strongly positive. The sign flip is the evidence the timing is clean.

**Finding A is the answer to "why is everything negative".** The NSE intraday session
carries a structural drift of about **-23%/yr**, while the entire equity premium accrues
overnight. A long-biased intraday book is fighting a headwind before it selects a single
stock. This is not a defect and not bad luck; it is documented across essentially every
equity market studied.

### What was actually wrong

1. **59 sessions of data.** yfinance caps 5-minute history at 60 days, and roughly half
   of those sessions are missing their closing bars. Every intraday verdict on record
   was drawn from ~59 sessions of half-complete data.
2. **`db/historify.duckdb` already held 16.9M one-minute bars back to 2020 and nothing
   used them.** `data.from_historify()` now resamples them: **48 symbols, median 1,043
   sessions, 99.5% session completeness, zero impossible bars** — against yfinance's 59
   sessions at 49.2%.
3. **Costs were understated by ~60%.** `RunConfig.cost_bps` was 10; the statutory NSE
   intraday charges alone are 8.2 bps at a Rs 1 lakh position, leaving under 1 bp per
   side for slippage on a market-order breakout. Now 16, computed by
   `types.india_intraday_bps()` from `portfolio.costs.india_intraday` so the backtest
   and the Portfolio Backtester cannot hold different views of what a trade costs.
4. **The t-statistic counted one market move as forty observations.** Now clustered by
   session, with the uncorrected figure kept alongside as `t_naive` so the gap is
   visible. On ORB it shrank |t| from 14.17 to 10.86.
5. **Drawdown was measured over the wrong ordering.** The harness collects trades symbol
   by symbol, so `max_dd_R` described a curve that ran A's whole history and then B's.
   Now sorted by exit time.
6. **No allowance for how many things had been tried.** `metrics.hurdle_t(n_trials)`
   gives the |t| a result must clear: 1.97 at one configuration, 2.83 at ten, 3.53 at
   a hundred.
7. **Historify downloads in ONE un-chunked request.** Flattrade answers a single 1-minute
   history call with at most ~58,000 bars and truncates silently. Ask the UI for five
   years and it reports success and stores seven months. `backtest/backfill.py` chunks
   by month, is resumable, and reports per-symbol coverage.

### What this does to the verdicts

ORB, re-run on 1,404 sessions instead of 59:

| Source | Sessions | Trades | Gross bps | Net R @ cost | Clustered t |
| --- | --- | --- | --- | --- | --- |
| yfinance | 59 | 2,422 | **-3.21** | -0.177 @ 10 bps | -5.82 |
| Historify | 1,404 | 9,307 | **+1.08** | -0.155 @ 16 bps | -10.86 |

EMA9 on the same store, 25,254 trades over 1,499 sessions: gross **-1.65 bps**, and at
ZERO cost the expectancy is **-0.001R per trade at t = -1.87**. That is a coin flip
measured to three decimal places - which is a far more useful statement than the
previous "t = -9.90 on 6,577 trades", because it says the strategy is not backwards,
it is empty.

The old sample said ORB loses money *before costs*. It does not — it has a small
positive gross edge of about 1 basis point per trade. That edge is real and it is also
useless, because the round trip costs sixteen. At zero cost the whole thing is
+0.023R/trade at t = 0.09, which is indistinguishable from zero. **The conclusion did
not change, but the reason did**, and the reason is the part you can act on: this
strategy family does not fail on direction, it fails on transaction cost.

### Data now, and the ceiling

| | Before | After |
| --- | --- | --- |
| 1-minute bars in Historify | 16.89M | **18.45M** |
| Symbols current to the last session | 0 / 48 | **47 / 48** |
| 5-minute sessions per symbol (median) | 59 (yfinance) | **1,043** |
| Session completeness | 49.2% | **99.5%** |

Measured against Flattrade on 2026-09-12: **1-minute history reaches back about twelve
months and no further; daily reaches about December 2019.** The 2020-2026 one-minute
bars already in the store therefore CANNOT be rebuilt if lost — they are an asset to
back up, not a cache to clear. A newly added symbol starts with roughly one year of
intraday history.

    uv run python -m signal_engine.backtest.backfill --status
    uv run python -m signal_engine.backtest.backfill --interval 1m          # extend all
    uv run python -m signal_engine.backtest.dataquality --source historify
    uv run --group analysis python -m signal_engine.backtest.validation
    uv run --group analysis python -m signal_engine.backtest orb --source historify --trials 10

NIFTY is the one symbol still stale: it is an index and needs `NSE_INDEX`, not `NSE`.

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

**Files**: `pinescripts/intraday/orderflow/concept-initiative_drive_detector_v6.pine`.

### Key-level candle verdicts, and two Pine compile faults

`plotshape`'s `size` argument is a const string, so the `input.string` marker-size control was rejected outright: *Cannot call "plotshape" with argument "size"*. Sizes driven by an input have to be drawn with `label.new`, whose properties accept series values. Both orderflow scripts hit this.

`indicator-keylevel-candles.pine` was firing almost nothing but FAIL on a live TCS 5-min chart. Two causes:

- **Proximity was the wrong trigger.** "Within 0.25 x ATR of the nearest level" is true on nearly every bar once eleven levels are on the chart. The bar now has to actually trade *through* a level, and of the levels it pierced, the one its close settles nearest is the one under test.
- **The level reference was unstable.** `close[1]` was compared against whichever level was nearest on *this* bar, frequently a different level from the one the previous bar closed against. Every level in the table is fixed once formed, so the comparison is now made against that one level.

Also: OR/IB levels were live during their own formation window, so every bar inside the first 15 / 60 minutes "touched" them. They are now published only once the window closes.

The output is one of three verdicts rather than five pattern names, each carrying a tooltip with the candle type, level, body/wick geometry, volume and the reading -- `BREAK` (conviction close through, on volume, no wick into the level), `FAKE` (rejection wick, or a close back inside after the previous bar closed beyond), `WEAK` (inconclusive, off by default).

`concept-initiative_drive_detector_v6.pine` markers are now small green/red `ID` labels instead of cyan/amber diamonds.

**Files**: `pinescripts/intraday/orderflow/indicator-keylevel-candles.pine`, `pinescripts/intraday/orderflow/concept-initiative_drive_detector_v6.pine`, `pinescripts/README.md`.

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

**Files**: `pinescripts/intraday/orderflow/concept-initiative_drive_detector_v6.pine`, `pinescripts/intraday/orderflow/indicator-keylevel-candles.pine`.

### Key-level marks: shorter, and level coverage made visible

Marks read `BRK U` / `FAKE D`, which collided with each other whenever two events landed on adjacent bars. Now two characters (`B▲` `B▼` `F▲` `F▼` `W`), tiny by default.

Separately: on a live chart only PDL and ORL appeared to produce verdicts. The selection loop tests all eleven levels identically, so that was not a bias — TCS on 2026-08-25 traded 2262-2313 and spent the session between PDL 2279.5 and ORL 2268.9, while PDH 2321.6 was never reached. But it exposed a genuine trap: **VAH/POC/VAL are off by default and default to 0.0**, so they are silently never tested, which on the chart is indistinguishable from "the script does not handle those levels".

Added a coverage table (bottom-right): every level in play, its price, and today's BREAK / FAKE counts, with an explicit `VAH/POC/VAL — off` row when the volume-profile levels are disabled. A level producing nothing is now visibly distinct from a level that was never armed.

**Files**: `pinescripts/intraday/orderflow/indicator-keylevel-candles.pine`.

### No unlabelled key-level lines

`klDrawLevel` dropped the TAG for any level further than `klTagMaxATR` (6) x ATR from close but kept its LINE, so the chart carried horizontal lines the trader could not name. A line you cannot identify is worse than no line — it reads as a level without saying which.

Suppression is now symmetric: a distant level whose line we draw ourselves is dropped entirely instead of being left anonymous. Levels whose line comes from elsewhere (`drawLine=false`, the ORB plots) are always tagged, because that line cannot be removed and must not be orphaned.

`indicator-keylevel-candles.pine` was compounding this — it re-plotted ORH/ORL/IBH/IBL/PDH/PDL with `plot()`, which carries no on-chart tag, duplicating lines breakout.pine already draws AND labels. `Plot Levels` now defaults off.

Also dampened its FAKE spam: `VAL` alone produced 15 verdicts in one session because a close one tick beyond a level counted as a break, making every oscillation a failed break. Added an acceptance margin (prior close must clear the level by 0.15 x ATR) and a per-level cooldown (3 bars).

**Files**: `pinescripts/intraday/orb/breakout.pine`, `pinescripts/intraday/orderflow/indicator-keylevel-candles.pine`.

### Every level drawn and tagged; previous-day VP levels get a P prefix

Making tag suppression symmetric fixed anonymous lines but created a worse problem: PDH, 33 points from close on a mid-range day, was hidden entirely — and PDH is exactly the level a trend day runs at. `klTagMaxATR` is now 0 (no distance limit): every level is drawn AND tagged, and the existing collision stagger keeps stacked tags readable.

`VAH/POC/VAL` renamed to `PVAH/PPOC/PVAL`. These are the PREVIOUS session's value area, but TradingView's Session Volume Profile plots the CURRENT day's VAH/POC/VAL — two sets of lines carrying the same three names at different prices, both on the chart at once. The P prefix also makes the naming self-consistent: `PVAH/PPOC/PVAL/PDH/PDL` are previous-session, bare `ORH/ORM/ORL/IBH/IBM/IBL` are today's. Display only — `klLvlNames` feeds a human-readable KEYLEVEL packet that goes to a channel the signal engine does not listen on, and `parser.py` never reads level names, so nothing in the trade pipeline is affected.

`indicator-keylevel-candles.pine`: acceptance margin and per-level cooldown both back to 0, so every candle interacting with a level is judged again — a run of F-down marks along one level *is* the conviction signal, and thinning it out removed the evidence. Each of the four verdicts now has its own colour (B up green, B down red, F up amber, F down blue) because at tiny label size the arrow alone was not readable.

**Files**: `pinescripts/intraday/orb/breakout.pine`, `pinescripts/intraday/orderflow/indicator-keylevel-candles.pine`.

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

**Files**: `pinescripts/intraday/orderflow/indicator-keylevel-candles.pine`.

### Cluster fan-out for key-level marks

With every interaction now judged, a level being ground along puts a verdict on consecutive bars — `PDL 2279.5` produced 18 in one session — and at one fixed y they overlapped into an unreadable clump. Label size is back to tiny, and marks landing within two bars of the previous one step down through four slots; isolated marks stay on the line, clusters fan out.

The first attempt put slot resolution in a helper function, which would not have compiled: **Pine rejects assignment to a global variable from inside a user function.** It is resolved at global scope instead, which is legal, and the verdicts being mutually exclusive means one slot per bar is sufficient. A scan of both orderflow scripts confirms no other function assigns to a global.

**Files**: `pinescripts/intraday/orderflow/indicator-keylevel-candles.pine`.

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
(`pinescripts/intraday/volume-profile/report-volume-profile-decision-assist.pine`) into the ORB
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

### Key Functions & Methods (2026-04-17 additions)

| Function | Module | Purpose |
|----------|--------|---------|
| `record_rejection()` | `risk.py` | Release position slot AND un-count `trades_today` for rejected/phantom orders (position never existed at broker) |
| `_is_be_series()` | `main.py` | Check if symbol is T2T (BE series) — MIS trading rejected |
| `notify_orphaned_position()` | `notifier.py` | Telegram alert for order never filled |
| `notify_be_stop_applied()` | `notifier.py` | Telegram: SL moved to break-even. Log-only 2026-04-22 to 2026-09-01; restored, see 2026-09-02 changelog |
| `notify_partial_exit()` | `notifier.py` | Telegram: partial TP exit (TP1/TP1.5/TP2). Log-only 2026-04-22 to 2026-09-01; restored |
| `notify_position_closed()` | `notifier.py` | Telegram: full position close (TP or SL). Log-only 2026-04-22 to 2026-09-01; restored |
| `notify_no_progress_exit()` | `notifier.py` | Telegram: no-progress market exit fired. Log-only 2026-04-22 to 2026-09-01; restored |
| `notify_entry_filled()` | `notifier.py` | Telegram: entry fill confirmed. Log-only 2026-04-22 to 2026-09-01; restored |
| `notify_day_summary()` | `notifier.py` | EOD Telegram summary: trades, win rate, per-trade table, capital trajectory |
| `_poll_positions()` | `tracker.py` | Detects closed positions with min_position_age_seconds guard |
| `_check_no_progress()` | `tracker.py` | Detects stuck trades and moves SL to break-even |

### Scripts (`scripts/`)

| File | Purpose |
|------|---------|
| `openalgoscheduler.py` | Startup: auto-login (TOTP), verify auth, start signal engine |
| `openalgoctl.sh` | Service controller: start/run/stop/restart/status, log rotation |
| `openalgoctl.ps1` | Windows: launches `openalgoctl.sh` in minimized cmd |
| `createTaskOpenAlgoScheduler.ps1` | Windows Task Scheduler: 5 tasks — 8:50 AM start, 2:55 PM squareoff, 4:00 PM stop, watchdog every 5 min 9 AM-4 PM, heartbeat check every 10 min 9:05 AM-4 PM |

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

Return % stays constant at ~21%/month — profit scales linearly with capital. The practical benefit of larger capital is fewer rejections from `min_capital_for_entry` floor. `max_open_positions` no longer needs raising by hand as capital grows (2026-09-12) — see below.

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
| `sandbox_capital` | Capital override in analyze mode. Per-strategy since 2026-09-10 — each strategy caches this SAME value independently, not a total split between them |
| `use_day_start_capital` | Cache first fetch of day for equal risk per trade. Cached per-strategy since 2026-09-10 — see `RiskEngine._StrategyState` |
| `test_qty_cap` | Max qty per order in `--test` mode (0 = disabled) |
| `min_capital_for_entry` | Skip new entries if live capital below this floor (INR) |

### `risk`
| Key | Description |
|-----|-------------|
| `daily/weekly/monthly_loss_limit` | Loss lockout thresholds (fraction of capital) |
| `max_portfolio_heat` | Max open risk fraction |
| `max_open_positions` | Concurrent slot cap. 0 = unlimited (2026-09-10 convention, matching `max_positions_per_symbol`/`sector`). Enforced per-strategy since 2026-09-10 — each strategy gets its own counter, not a shared total. **-1 = dynamic (2026-09-12)**: computed fresh from live capital every check instead of a fixed number — see `mode_profiles.live` and "Recent Changes (2026-09-12)" below |
| `max_trades_per_day` | Daily order cap. 0 = unlimited. Per-strategy since 2026-09-10, same as above |
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
# Creates 5 tasks under Anand user:
#   openAlgoAutoStart      -- 8:50 AM weekdays, long-running (blocks all day)
#   openAlgoAutoStop       -- 4:00 PM weekdays, graceful shutdown (moved from 3:30 PM on
#                             2026-09-17 to give the momentum-rank EOD scan runway after close)
#   openAlgoWatchdog       -- every 5 min, 9:00 AM-4:00 PM weekdays, crash recovery
#   openAlgoSquareOff      -- 2:55 PM weekdays, MIS failsafe close (WakeToRun) -- moved
#                             from 3:02 PM on 2026-09-17; now fires BEFORE the engine's
#                             own 3:00 PM exit, not after (see note below)
#   openAlgoHeartbeatCheck -- every 10 min, 9:05 AM-4:00 PM weekdays, dead-man's switch
```

### Windows Task Scheduler -- How the 5 Tasks Work Together

| Time | Task | Action |
|------|------|--------|
| 8:50 AM | `openAlgoAutoStart` | Calls `openalgoctl.ps1 run` -- starts app.py + signal engine, **stays running all day** |
| 9:00 AM-4:00 PM | `openAlgoWatchdog` | Calls `openalgoctl.ps1 start` every 5 min -- no-op if healthy, relaunches if crashed |
| 9:05 AM-4:00 PM | `openAlgoHeartbeatCheck` | Runs `heartbeat_check.ps1` every 10 min -- alerts if `signal_engine/logs/heartbeat.txt` is stale, independent of WSL/bash/Python |
| 2:55 PM | `openAlgoSquareOff` | Calls `openalgoctl.ps1 squareoff` -- failsafe MIS close (WakeToRun). **Now fires BEFORE the engine's own 3:00 PM exit** (moved from 3:02 PM on 2026-09-17), so it unconditionally force-closes MIS positions every day rather than only when the engine failed to — confirm this is the intended behavior |
| 4:00 PM | `openAlgoAutoStop` | Calls `openalgoctl.ps1 stop` -- sends Telegram notification, kills both services. Moved from 3:30 PM on 2026-09-17 to give the momentum-rank EOD scan (runs after market close) more runway |

The watchdog uses `start` (idempotent): polls `http://127.0.0.1:5000/`, skips if healthy, restarts the full stack if dead. Maximum recovery time after a crash: **5 minutes**.

**2026-09-17 schedule change — resolved.** `openAlgoSquareOff` (2:55 PM) and `openAlgoHeartbeatCheck`'s window applied cleanly on the first attempt. `openAlgoAutoStop` and `openAlgoWatchdog`'s window initially failed to update (`Register-ScheduledTask` returned "Access is denied" — the same restriction hit on 2026-09-16 for tasks that predate this fix, since they're not owned by the current user's non-elevated context). A subsequent elevated ("Run as Administrator") re-run of `createTaskOpenAlgoScheduler.ps1` applied both. Confirmed live the same day: all 5 tasks' triggers read `AutoStop` 4:00 PM and `Watchdog` 9:00 AM-4:00 PM.

### Failure alerting and cooldown (2026-08-25)

A failed startup used to be silent and open-ended. `_run_startup()` called `sys.exit(1)` on every failure path *before* reaching its notification step, and `openalgoctl.sh` then wrote a flat 24-hour cooldown. On 2026-08-24 a single 09:03 failure blocked 80 start attempts through 15:27 — the whole trading day — with nothing sent anywhere.

- `notify_failure()` alerts Telegram on each `_run_startup` failure (configuration, auto-login, broker-auth). It never raises, so a dead notifier cannot mask the underlying error.
- `openalgoscheduler notify <stage> <detail>` lets `openalgoctl.sh` raise an alert without duplicating Telegram wiring.
- The auth cooldown escalates **5m / 15m / 1h / 3h**, capped, with the counter reset on success. Worst case now stays inside one trading session.
- The supervisor loop re-probes the health URL every 60s and declares `app.py` wedged after 3 consecutive failures. `kill -0` only proved the PID existed, so an alive-but-unresponsive server read as healthy indefinitely.
- Alerts also fire on `app.py` crash and on signal-engine crash-loop giveup.

Covered by `tests/test_openalgoscheduler.py` and `tests/test_openalgoctl.sh` (13 shell assertions on the cooldown state machine).

### Dead-man's switch and the 2026-09-15/16 silent outage

On 2026-09-15, `openalgoctl.sh` lost its executable bit (git tracked it as mode `100644`). Every subsequent `AutoStart`/`Watchdog` invocation (`openalgoctl.ps1` runs `./signal_engine/scripts/openalgoctl.sh <cmd>`) failed instantly with `Permission Denied` — **before** the script could write a log line or send a Telegram alert. The stack was down from 15:00 that day through 11:30+ the next morning with zero notification anywhere; every alerting path in this doc lives inside the process that couldn't start.

Fixes:
- `$ctlScript` in `openalgoctl.ps1` now invokes `bash ./signal_engine/scripts/openalgoctl.sh <cmd>` instead of relying on `./<script>` and the executable bit surviving every checkout.
- `openalgoctl.sh`'s run loop now writes `signal_engine/logs/heartbeat.txt` (a Unix timestamp) every ~5s while genuinely healthy. A new, independent Task Scheduler task, `openAlgoHeartbeatCheck` (`heartbeat_check.ps1`), checks that file's staleness every 10 minutes during market hours and raises a blocking Windows alert if it's missing or stale — with **no dependency on WSL, bash, or Python succeeding**, so it survives the exact failure class above.
- Separately, `Invoke-Start` in `openalgoctl.ps1` writes its helper `openalgo-run.bat` to `$env:TEMP` instead of `$PSScriptRoot` (which resolves to the `\\wsl.localhost\...` UNC path). Windows flags batch files launched via `Start-Process` from that path with a "This file is in a location outside your local network" security prompt on every single launch, which silently blocks unattended recovery waiting for a click that never comes.
- `cmd_stop()` now writes `signal_engine/logs/stop_requested.flag` before killing anything. `AutoStop` runs as a separate process from the long-running `AutoStart`/`Watchdog` loop, and that loop previously had no way to distinguish "I was told to stop" from "I died" — it misreported every routine 3:30 PM shutdown as `app_crash` / "Stack is DOWN". The sentinel fixes the misattribution and also prevents a duplicate shutdown notification (the `stop` command already sends one).
- `kill_from_pidfile()`'s process-matching narrowed from `-m signal_engine` to `-m signal_engine.main` — the old pattern also matched unrelated standalone tools under the same package (e.g. `signal_engine.analysis.breakingtrade`), risking collateral kills on routine restarts.
- `wait_for_network()` no longer depends on a single external test service (`httpbin.org`); it now accepts any of three well-known endpoints.
- A successful startup that followed one or more auth-cooldown failures now sends a "recovered after N failed attempts" notification instead of resolving silently.
- The missing `openAlgoSquareOff` task (documented as one of the scheduled tasks but absent from the live Task Scheduler — cause unknown) was re-registered.

Not done as part of this fix, flagged as a larger follow-up: replacing the bash+PowerShell+Task Scheduler supervision stack with WSL2 systemd services (`Restart=always`), which would remove most of the hand-rolled flock/restart-budget/health-probe logic. Deferred because it requires enabling systemd in `/etc/wsl.conf` and restarting the WSL2 VM.

### 2026-09-17 review fixes: BREAKOUT audit gap, day-summary premature send, watchlist/BTST duplicates

A full review of 2026-09-17's trades and Telegram reporting turned up five separate bugs, all now fixed:

- **BREAKOUT reconciliation gap.** `db.save_tracker_exit()` (the write path used when the tracker's own polling detects a close -- e.g. a broker SL-M fill beating the TradingView alert here -- rather than a signal-driven exit) never persisted `sig_id`, so `analysis/ledger.py`'s sig_id-keyed reconciliation couldn't pair that close back to its entry. It reported the real entry as falsely still-open (`OPEN_AT_EOD`/`NO_EXIT_EVENT`) and the close as a phantom orphan (`NO_ENTRY`) -- every BREAKOUT position that closed via tracker detection on 09-17 (PFC, HINDALCO, RECLTD) showed up broken in the EOD report even though the trades themselves, P&L, and Telegram notifications were all correct. `TrackedPosition` now carries `sig_id` from entry through to every exit path (`book_close()`, startup's position-restore and reconciliation backfill), threaded through `save_tracker_exit()`/`save_reconciled_exit()`/`fetch_all_open_positions()`/`fetch_last_entry_trade()`.
- **Day summary sent prematurely, then permanently suppressed the real one.** The on-disk "already sent today" marker got written mid-morning (11:14) with only a handful of the day's trades reflected, and the genuine 14:45 EOD send never fired afterward -- `send_day_summary()`'s one-shot guard had no way to tell "sent, but incomplete" apart from "sent, and correct." The marker now also records the trade count it reported; a later call whose trade count has grown sends a corrected summary instead of staying silent for the rest of the day. Also hardened `tests/conftest.py`'s autouse DB-isolation fixture to redirect `_DAY_SUMMARY_MARKER` too (it previously only protected `trades.db`), closing off test-pollution as a possible cause of a stray real-file write.
- **Notifier pin-state ordering.** `_send_and_pin_day_summary()` split the pin attempt and the pin-state save into separate try/except blocks, so a state-save failure is never misreported as "could not pin" when the pin itself actually succeeded.
- **BreakingTrade watchlist duplicate alerts.** `store.previous_hits()` only compared a scan's matches against the single immediately-preceding poll, so a symbol that dropped out of a scan for one poll and reappeared later re-qualified as "new" and re-alerted -- BHARATFORG fired duplicate watchlist alerts at 11:31 and 12:31 on 09-17. Dedup is now scoped to the whole trading day (still resets at midnight).
- **BTST "one message per day" dedup was structurally broken.** `alerts.record()` always stamped `created_at` with real wall-clock `datetime.now()`, ignoring `alert_btst()`'s caller-supplied `captured_at` (the scan's logical timestamp). `_btst_names_sent_today()` then filtered by `captured_at`'s date, which only coincidentally matched the real insert date -- on any day they disagreed (confirmed via two pre-existing failing tests in `test_btst_metrics.py`, unrelated to this review but caught by the same pass), every run sent a fresh duplicate instead of detecting the earlier one. `record()` now accepts an optional `when` override that `alert_btst()` passes through.

All fixes covered by tests; full `signal_engine/tests/` suite (1566 tests) green.

### 2026-09-21 weekly paper-trading review: tradebook snapshot never worked, added weekly_review.py

Reviewing the paper week of 2026-09-14 to 2026-09-18 end to end turned up one bug that had been silent since the EOD pipeline's creation, and one gap in tooling:

- **Broker tradebook snapshot has never actually captured a broker fill.** `analysis/__main__.py`'s `_fetch_tradebook()` called `_post_tolerant("tradebook", {})` with an empty payload -- no `apikey` -- so `restx_api/tradebook.py`'s schema validation rejected every call with HTTP 400, every single day since the snapshot step existed. On top of that, the function then treated `_post_tolerant`'s raw `httpx.Response` as if it were already-parsed JSON (every other `_post_tolerant` caller in `api_client.py` calls `.json()` first), so even a 200 response would have failed the same way. Because `ledger.py`'s `_leg_from_event()` falls back to the fill price the engine saved at order time when no broker snapshot match exists, every daily EOD report since 2026-09-06 has been reading like it has real fill/slippage data -- it does not; it is the engine's own recorded intent, never cross-checked against the broker. Fixed in `_fetch_tradebook()` to send `_auth()` and parse the response correctly. The broker's tradebook is wiped daily, so this cannot recover last week's real fills -- going forward only.
- **No standing way to review a week without re-reading five daily reports by hand.** Added `analysis/weekly_review.py` (+ `analysis/weekly.sh`, suggested Saturday cron), which aggregates `ledger.py`/`__main__.py`'s same data over a date range, keeps a day's P&L out of the "verified" total whenever that day's tradebook snapshot file is missing, and ends with an auto-generated plain-language paragraph -- so a weekly check no longer requires an LLM to read reports and eyeball numbers.

All fixes covered by tests (`test_analysis_main_snapshot.py`, `test_weekly_review.py`); full `signal_engine/tests/` suite (1624 tests) green.

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
| Risk store | `signal_engine/data/risk.db` | `risk_counters` | Daily counters: trades, loss, open_positions, day_start_capital — keyed (strategy, mode, date) since 2026-09-10 (was (mode, date); pre-migration rows carry strategy=`_LEGACY`) |
| Trade audit | `signal_engine/data/trades.db` | `trades` | Every order: symbol, qty, entry/sl/tp, order_id, status, timestamps |

### Key SQL Commands

```bash
# Today's risk state, every strategy (one row per strategy since 2026-09-10)
sqlite3 signal_engine/data/risk.db \
  "SELECT strategy, mode, trade_date, trades_today, daily_loss, open_positions, day_start_capital FROM risk_counters WHERE trade_date=date('now');"

# Last 7 trading days for one strategy
sqlite3 signal_engine/data/risk.db \
  "SELECT trade_date, trades_today, printf('%.2f',daily_loss) loss, open_positions FROM risk_counters WHERE strategy='BREAKOUT' AND mode='live' ORDER BY trade_date DESC LIMIT 7;"

# Fix stale state for one strategy after broker auto-squareoff
sqlite3 signal_engine/data/risk.db \
  "UPDATE risk_counters SET open_positions=0, daily_loss=0.0 WHERE strategy='BREAKOUT' AND mode='live' AND trade_date=date('now');"

# Today's trades
sqlite3 signal_engine/data/trades.db \
  "SELECT id, direction, symbol, quantity, order_id, status, substr(executed_at,12,8) time FROM trades WHERE executed_at >= date('now') ORDER BY executed_at;"

# Trades per day (last 14 days)
sqlite3 signal_engine/data/trades.db \
  "SELECT substr(executed_at,1,10) date, count(*) total, sum(status='SUCCESS') ok, sum(status='REJECTED') rej FROM trades WHERE executed_at >= date('now','-14 days') GROUP BY 1 ORDER BY 1 DESC;"
```

## Open-Drive / Value-Area-Fade Backtest: Investigation Concluded (2026-09-21)

Backtest-only research (never went live) into an intraday value-area rejection fade, across the
full 212-symbol F&O universe, 2023-01-02 to 2026-09-13. Seven rounds (v1-v7, full detail in
`signal_engine/pinescripts/intraday/open-drive/STRATEGY-ANALYSIS.md`) progressively narrowed the
gap between the strategy's gross edge and real NSE intraday trading cost (~16 bps round-trip) from
~16x down to ~2.1x, but never closed it:

- v1-v3 established the base setup and its cost problem (raw signal too weak or fires far too
  often to survive real cost).
- v4-v6 tuned single-session wick-depth selectivity, narrowing the gap to ~2.3-2.7x before running
  out of sample (trade count fell below this investigation's own 300-500-trade noise floor at
  deeper thresholds, and the result visibly reversed sign there - a hard stop, not a judgement call).
- v7 (final round) added three Market Profile / HTF-derived filters, all computable from OHLCV
  alone (no orderflow/footprint data is available for backtesting here - Historify's `market_data`
  table stores only open/high/low/close/volume/oi, and no Indian broker's historical API replays
  depth or footprint history): a day-type filter (Normal/balance vs Trend day), multi-day
  value-area confluence (does the faded level agree with the session two days back, not just one),
  and an HTF trend filter. Day-type and confluence were rejected outright (clean IS-only/OOS-only
  overfitting signatures at every threshold). HTF trend was the only filter that passed, and
  stacked on the best base it narrowed the gap further to ~2.1x with the best symbol dispersion of
  any round (44.7-45.2% net-positive) - but its t-stat at real cost was still worse than the
  single best config already found in v6.

**Conclusion: no cost-viable edge exists in OHLCV-only data for this setup at this cost level.**
Every lever derivable from price/volume bars (selectivity, multi-day confluence, day-type, HTF
trend) has now been tried without closing the gap. The one untested, structurally unavailable
ingredient is real orderflow/footprint confirmation - not available from any Indian broker's
historical API. ICT-style liquidity-sweep/fair-value-gap concepts were deliberately not built as
entry triggers here: independent published backtests found only weak-to-null edges for them
(t~0.9-1.1) even on lower-cost, higher-liquidity markets than NSE intraday.

This investigation is closed - not recommended to revisit with another threshold sweep absent a
genuinely new data source (real orderflow) or a materially lower-cost execution model.

## EOD Reconciliation Mismatch: Root Cause and Fix (2026-09-21)

The 2026-09-21 EOD run flagged a CRITICAL reconciliation mismatch: engine (`trades.db`) reported
+2,092.95 over the day's trades, the broker's own realised P&L (`m2mrealized`) reported +547.18,
a difference of +1,545.77 - "the day's numbers are NOT trustworthy" per `reconcile.py`.

Root cause, confirmed against `trades.db` and the broker's tradebook for the day: `book_close()`
in `tracker.py` (the path every tracker-detected close - broker SL-M fill, no-progress market
exit - goes through) wrote every closing EXIT row with `order_id=pos.entry_order_id` and
`quantity=pos.original_quantity`, regardless of how much was actually still open or which order
closed it. For a position with no prior partial exits this is harmless (remaining qty == original
qty). For a position that had already partially exited (BHARTIARTL: TP1 then TP2 before the final
leg; RELIANCE: TP1 before the final leg), the final leg's row claimed to close the FULL original
quantity under the ENTRY's own order_id - a row indistinguishable from a fabricated duplicate of
the entry. Two downstream effects:

- The ledger's order_id-keyed reconciliation could never find the real broker fill that closed
  the position (a different order_id - the trailing SL or the no-progress market exit), and
  reported it as "a broker fill the engine never sent" for all 5 of the day's closed positions.
- The final leg's exit price came from `pnl_delta / pos.quantity` using the REMAINING quantity at
  the time (correct), but with the wrong quantity/order recorded downstream, cross-checking the
  leg against the real broker fill (e.g. BHARTIARTL's true closing fill was 63 @ 1833.80 via the
  trailing SL, not the synthetic weighted-average price implied by treating it as an 180-share
  close) was impossible from `trades.db` alone.

Fix: `book_close()` now writes `quantity=pos.quantity` (the remaining size at close, already
decremented on every partial exit by `main.py`) and `order_id=pos.sl_order_id` (the order that is
actually live against the position - the trailing stop, or the no-progress market exit, which now
also stores its id there via `_no_progress_market_exit`), falling back to `db.py`'s existing
`"-TRACKER-"` marker when truly unknown rather than silently misattributing to the entry. Two
regression tests added to `test_close_accounting.py`. Full suite (1,630 tests) passes.

**Correction, same day:** the paragraph originally here speculated the remaining gap was
brokerage/STT/exchange charges the engine doesn't model. That's wrong and is superseded by the
next section below - `sandbox/execution_engine.py` charges nothing at all (no brokerage/commission
computation exists in it), and the true story turned out to be two separate double-counting bugs,
one on each side of the reconciliation, not a costs gap.

## EOD P&L: Three Numbers For One Day, Only One Correct (2026-09-21, same day)

Follow-up the same evening: the OpenAlgo dashboard showed Realised P&L = 547.18, the Telegram DAY
SUMMARY showed Net +2,093 (Trades: 8, W:7 L:1, 88% win rate), and neither matched the other, let
alone the truth. Ground truth is `db/sandbox.db`'s `sandbox_trades` table - every actual fill,
disputable by nobody:

```
BHARTIARTL  SHORT 180 @ 1842.10  ->  BUY 54 @ 1837.50, BUY 63 @ 1832.40, BUY 63 @ 1833.80
RELIANCE    SHORT 245 @ 1237.10  ->  BUY 73 @ 1233.30, BUY 172 @ 1234.80
PFC / NTPC / HDFCBANK: single-leg, one exit fill each
```

Summed by hand from those fills: **true day P&L = +1,619.65.** Both displayed numbers were wrong,
for two independent reasons:

1. **Telegram's +2,093 (signal_engine, `trades.db`):** `_book_broker_close()` in `tracker.py`
   treated the broker's per-symbol "realised" figure as the FINAL leg's own delta and added it on
   top of `pos.realized_pnl` (the correctly-tracked sum of prior partial legs). For BHARTIARTL:
   859.50 (TP1 248.40 + TP2 611.10, both correct - confirmed against the fills above) + 718.80
   (broker "realised", not actually that leg's delta) = 1,578.30 booked, against 1,382.40 true.
   Fixed: once `pos.realized_pnl != 0` (partial-exit history exists), the final leg's delta is now
   derived from price - `ltp` (which the broker sets to its own execution price on a full close,
   confirmed against `sandbox/execution_engine.py`'s "Position closed completely" branch) against
   the position's entry price and remaining quantity - instead of trusting the broker's realised
   figure at all. Verified against every position that closed that day: all five now reproduce the
   true per-fill P&L exactly. Two regression tests added
   (`TestFinalLegDoesNotDoubleCountPriorPartialExits`); full suite (1,632 tests) passes.

2. **Dashboard's 547.18 (OpenAlgo sandbox, `sandbox_funds.today_realized_pnl`):** confirmed via
   `sandbox_positions` that the sandbox's OWN per-position `today_realized_pnl` also undercounts
   for BHARTIARTL (718.80) and RELIANCE (673.00) - in both cases landing on neither the correct
   cumulative total nor any single leg's own value, while the three single-leg positions (PFC,
   NTPC, HDFCBANK) are exactly correct. `execution_engine.py`'s partial-close and full-close
   branches both look correctly additive (`position.today_realized_pnl = (... or 0) + realized_pnl`
   in both branches, using the right per-order `old_quantity`/`avg_price`/`close_quantity`), so
   the loss is happening somewhere else in the sandbox's state handling - `position_manager.py`'s
   catch-up/session-boundary reset (`_index_positionbook`-adjacent code that zeroes
   `today_realized_pnl` for positions "last updated before today's session boundary") is the most
   likely suspect but was not confirmed line-by-line. **Not fixed** - `sandbox/`,
   `database/sandbox_db.py` and the `/dashboard` P&L widget are shared OpenAlgo infrastructure
   used by every broker/user of the platform, not scoped to signal_engine's paper trading, and
   this needs its own investigation before touching it. Until it's fixed, the OpenAlgo dashboard's
   Realised P&L will keep undercounting on any day with a multi-leg (partial-exit) close.

With fix (1) in place, the Telegram DAY SUMMARY is the one number that is now independently
correct - computed entirely from signal_engine's own tracked fills and no longer dependent on the
sandbox's buggy bookkeeping. The dashboard widget remains wrong until the sandbox-side bug above
is separately investigated and fixed.

**Decision, same day:** the user does not want the OpenAlgo sandbox bug (item 2 above) fixed -
`sandbox/`, `database/`, `blueprints/`, etc. are OpenAlgo core, maintained by the OpenAlgo project,
out of scope here (see memory `feedback_no_openalgo_core_changes`). Three more fixes followed,
entirely within `signal_engine/`, to make trades.db's own number the one reliable P&L without
touching OpenAlgo at all:

- **`main.py`'s `_handle_exit_locked` wrote a SECOND EXIT row for every signal-driven full exit.**
  `book_close()` already writes the authoritative row (via `save_tracker_exit()`); the unconditional
  `save(signal, exit_order, trade_result)` right after it wrote a duplicate with the raw signal's
  empty context. Didn't manifest 2026-09-21 (every close that day was broker-detected, not
  signal-driven) but is a live latent bug for the common case. Fixed: `save()` is now skipped after
  a successful full exit; still called for partial exits (the only row they get).
- **`db.fetch_day_trades()` counted every partial-exit leg as its own "trade" at 0 P&L**, since a
  partial leg's row has no `pnl` in its context and silently defaulted to 0.0 - inflating the
  Telegram DAY SUMMARY's "Trades: N" (8 instead of 5) and, since 0.0 >= 0 reads as a win, its win
  rate too (reported 88%; the real number across 5 closed positions was 80%, 4 winners). Fixed:
  rows with no `pnl`/`realized_pnl` key in context are now skipped - the partial leg's own P&L was
  never lost, it's already folded into the final row's cumulative total by `book_close()`.
- **`reconcile.py` is now mode-aware.** A mismatch in analyze mode is now recognized as the
  known OpenAlgo sandbox limitation above (`Reconciliation.is_known_sandbox_limitation`) - logged,
  but no longer escalated to Telegram as a CRITICAL "day's numbers are NOT trustworthy" claim,
  and no longer a `[FAIL]` in the EOD report (`eod_review.check_reconciliation()`). A live-mode
  mismatch is unchanged: a real broker's own realised P&L is trustworthy, so it's still CRITICAL.

All three: regression tests added (`test_main_exit.py`, `test_day_summary_from_db.py`,
`test_reconcile.py`), full suite (1,634 tests) passes.

**Data correction, same day:** the two `trades.db` rows written before the `book_close()` fix
above (BHARTIARTL id=421, RELIANCE id=415) were hand-corrected to what the fixed code would have
written, verified against `sandbox_trades`' real fills: `quantity` 180→63 / 245→172 (remaining at
close, not original), `order_id` corrected to the actual closing order (BHARTIARTL
`26092117577881`, the trailing SL that filled at 1833.80; RELIANCE `26092174202960`, the TP1-buffer
SL that filled at 1234.80), `fill_price` and `context.pnl` corrected to 1382.40 / 673.00. Backed up
first to `signal_engine/data/trades.db.bak-2026-09-21-correction`. This was a one-off manual
correction of two known-wrong historical rows, not a migration — the code fix above prevents the
same error going forward; nothing else needed backfilling (PFC/NTPC/HDFCBANK were single-leg and
already correct).

**2026-09-21 corrected final results** (`db.fetch_day_trades("analyze", "2026-09-21")`, verified
exactly against every fill in `sandbox_trades`):

| Symbol | Qty | Entry | Exit | P&L | Result | Exit path |
|---|---|---|---|---|---|---|
| BHARTIARTL | 180 | 1844.40 | 1833.80 | +1,382.40 | WIN | TP1 + TP2 + trailing SL |
| RELIANCE | 245 | 1237.20 | 1234.80 | +673.00 | WIN | TP1 + trailing SL |
| NTPC | 1095 | 326.70 | 326.40 | +438.00 | WIN | no-progress exit |
| PFC | 874 | 346.30 | 345.75 | +87.40 | WIN | no-progress exit |
| HDFCBANK | 409 | 739.00 | 737.15 | -961.15 | LOSS | no-progress exit |

**5 trades, 4 wins / 1 loss (80% win rate), net +₹1,619.65.** Plus 1 declined signal (VEDL,
correctly rejected by sizing — entry too expensive for the risk budget) and 5 BreakingTrade
scanner signals that stayed informational-only (0 became trades, by design).

Before vs. after, for the record:

| | Before fix | After fix (true) |
|---|---|---|
| Telegram Day Summary | +₹2,093 / 8 trades / 88% win rate | +₹1,619.65 / 5 trades / 80% win rate |
| OpenAlgo dashboard Realised P&L | ₹547.18 | still wrong (known OpenAlgo sandbox limitation, out of scope — see `feedback_no_openalgo_core_changes` memory) |
| Ground truth (`sandbox_trades`, hand-summed) | +₹1,619.65 | +₹1,619.65 (unchanged — it was always right, nothing read directly from it before today) |

**Same day, follow-up:** the other 3 positions (PFC, NTPC, HDFCBANK) had the identical order_id bug
as BHARTIARTL/RELIANCE (final EXIT row reused the entry's order_id) — their `total_pnl` was already
correct (single-leg, so `quantity` numerically matched), but `ledger.py`'s own separate order_id-
keyed join (used for the EOD report's EXECUTION QUALITY/POSITIONS/BY-STRATEGY tables, and by
`weekly_review.py`) couldn't match their real closing fill either, silently zeroing their
R-multiple and excluding their P&L from "gross P&L". Backfilled their `order_id` to the real
closing order too (PFC `26092177983763`, NTPC `26092114047537`, HDFCBANK `26092113551803`). With
all 5 corrected, `ledger.py`'s own reconstruction now agrees exactly with `db.fetch_day_trades()`:
5/5 positions matched, 0 "broker fills the engine never sent", win 80.0%, gross P&L +1,620,
`BY STRATEGY: BREAKOUT 5 trades 80% win +1.79R`.

**Bigger finding, same day:** `weekly_review.py --since 2026-09-04 --until 2026-09-21` (per-strategy
comparison across the recent paper-trading window — the actual purpose of running strategies in
analyze mode is to compare them before promoting one to live) shows only **1 of the last 12
calendar days (2026-09-21) as `verified`** against the broker tradebook; every trading day from
2026-09-07 through 2026-09-18 is `unverified` (signal-side estimate only, per `ledger.py`'s own
verified/unverified discipline). Root cause found in `signal_engine/logs/eod_cron.log`: it is
**not** an OpenAlgo/API problem — 09-16, 09-17, and 09-18's snapshot attempts all failed
immediately with `FATAL: another EOD run is in progress (.eod.lock)`, `eod.sh`'s own overlap guard
(a `flock` on `signal_engine/logs/.eod.lock`). Since `flock` releases when the holding process
exits, something held that lock across multiple consecutive days without ever releasing it (a hung
process, not a stale pidfile) until it cleared on its own by 2026-09-21. The exact process/cause is
not recoverable after the fact (nothing was still holding it or logged when it let go).

**Fixed, same day:** `eod.sh` now checks the lock file's mtime before trying to acquire it -
anything older than `STALE_LOCK_SECONDS` (2 hours; a normal run takes seconds to a couple of
minutes, so nothing legitimate should ever be anywhere near that) is removed as abandoned rather
than treated as a live run. Verified both branches (a synthetic 3-hour-old lock file gets removed;
a fresh one is left alone) and a live end-to-end run. The 7 already-unverified days' broker-side
numbers are still very likely unrecoverable (the tradebook API is same-day only) — any live-
promotion decision needs to wait for enough NEW verified days going forward, not lean on the
unverified week behind it.

## Unverified History Purged From trades.db (2026-09-21, same day)

Checking `signal_engine/data/tradebook/` against every day that had rows in `trades.db` found the
unverified-data problem above was far bigger than the recent week: **only 2026-09-21 has ever had
a broker tradebook snapshot, in the database's entire history back to 2026-03-09** (410 rows total,
14 of them 2026-09-21's). Every other day's P&L/win-loss/R numbers were the engine's own unconfirmed
estimate, never checked against a real broker fill - a much larger fraction of the audit trail was
"not trustworthy" than the reconciliation-mismatch investigation alone suggested.

User's explicit instruction: remove all records that aren't trustworthy, so `trades.db` can't cause
confusion by mixing unverified history with the now-correct, strategy-attributable, broker-verified
data - and treat 2026-09-21 as the baseline going forward. Confirmed the exact scope (396 unverified
rows, 2026-03-09 through 2026-09-18) before acting, given the irreversible size of this relative to
what the conversation had been discussing (the recent week, not six months). Executed:

1. Backed up first: `signal_engine/data/trades.db.bak-pre-unverified-purge-20260921-200236`
   (alongside the earlier `trades.db.bak-2026-09-21-correction` from the same-day P&L fix).
2. `DELETE FROM trades WHERE date(executed_at) < '2026-09-21'` — 396 rows removed.
3. `PRAGMA integrity_check` (ok) and `VACUUM` (233KB → 45KB).
4. Verified via `db.fetch_day_trades()`, `weekly_review.py`, and the full test suite (1,634 tests,
   all use isolated tmp databases so unaffected either way) that the remaining 14 rows (2026-09-21,
   5 closed BREAKOUT positions + 1 decline) are intact and correctly attributed.

`trades.db` now contains only 2026-09-21 onward - every row in it is broker-verified and
strategy-tagged. Future `weekly_review.py`/EOD comparisons build cleanly from here with no
unverified history mixed in.

## Unattended Daily /eod Review (2026-09-21, same day)

The user asked for `eod.sh` (the 15:25 IST cron) to do what invoking `/eod` interactively does,
so they don't have to run it by hand every trading day. `eod.sh` itself can't: it's a
deterministic script (fixed PASS/FAIL thresholds), and root-causing a FAILED check - deciding
whether it's a real bug, known noise, or an OpenAlgo-side limitation, and what the fix would be -
needs an LLM reasoning over `trades.db`/the tradebook/the logs, the way `/eod`'s skill does
interactively. A cron job cannot do that.

What was built instead: `signal_engine/analysis/eod_claude_review.sh`, a new cron entry at 15:40
IST weekdays (15 min after `eod.sh`, matching its own "before openAlgoAutoStop at 15:30" timing
note - though this script doesn't actually need OpenAlgo live, since the `/eod` skill's own
methodology only reads local files: `trades.db`, the tradebook JSON `eod.sh` already snapshotted,
and the log files - never a live API call). It invokes the `claude` CLI headlessly
(`-p`/`--dangerously-skip-permissions`, required for a genuinely unattended run) with a prompt
telling it to follow `.claude/skills/eod/SKILL.md`'s steps 1-2 (get the report, triage every
FAILED check) and step 4 (technical summary + layman recap), explicitly skipping step 3 (fix).

User chose **report-only** (not autonomous fixing) when asked. That's enforced three ways, not
just by asking nicely: `--permission-mode plan` (Claude Code's structurally read-only mode - the
response IS the investigation, nothing executes), `--disallowedTools Edit,Write,NotebookEdit`
(the mutating tools are not registered at all), and the prompt itself forbidding edits/commits/DB
mutation as a third layer. If a real bug is found, the report describes the fix needed for a
human to apply later via `/eod` - same review-with-tests process every fix in this file's
2026-09-21 entries went through.

Output: `signal_engine/analysis/reports/eod-<date>-claude-review.md` per day (the investigation
itself), `signal_engine/logs/eod_claude_cron.log` (the wrapper's own operational log). Verified
with a live end-to-end run before trusting it in cron, not just a syntax check.

## Correction: The Real Cause of the Snapshot Gap Was an Auth Bug, Not (Only) the Lock

Committing `signal_engine/analysis/__main__.py` surfaced a fix that had been sitting uncommitted
in the working tree since before this session started (author/date unknown - it predates
2026-09-21's work). It matters enough to correct the record above: **`_fetch_tradebook()` was
calling OpenAlgo's `/tradebook` endpoint with an empty payload (`{}`), never sending the API key
at all** - every call was guaranteed a `400 Bad Request` (missing `apikey`) the moment it reached
OpenAlgo, regardless of the stale-lock issue. Fixed (already, in this working-tree state) to send
`_auth()` and to surface the actual status code and response body in the error, not just the
Response object's repr.

This means the "broker tradebook snapshot" section of the "Unverified History Purged" entry above
over-credited the stale-lock theory. Re-reading `eod_cron.log` more carefully: 2026-09-16/17/18's
failures show `RuntimeError: tradebook call failed: <Response [400 BAD REQUEST]>` - this auth bug,
not (only) the lock - with a `FATAL: another EOD run is in progress` line interleaved from a
separate, concurrent invocation (log writes from two processes sharing one file, not necessarily
evidence of a multi-day stuck lock). Earlier days (09-07 through 09-15) show
`httpx.ConnectError: All connection attempts failed` - genuine OpenAlgo-unreachable-at-cron-time,
unrelated to either bug. **This auth fix, silently present in the working tree, is the actual
reason 2026-09-21 was the first snapshot to ever succeed** - working-tree edits execute regardless
of commit status. The lock-staleness fix (`eod.sh`) remains valid, independent hardening; it just
was not, on its own, what was blocking 09-16-18 specifically. Verified with
`test_analysis_main_snapshot.py` (3 tests: success sends `apikey`, a 400 raises with the real
status/body, a non-JSON error body doesn't crash) and the full suite.
