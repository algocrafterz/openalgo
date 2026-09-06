# breakout.pine — Changelog

Key-level breakout strategy. Started as a byte-identical copy of `orb.pine`, then
extended so the Opening Range is one key level among several rather than the only one.

- **Source strategy**: `orb.pine` (unchanged, still the live strategy)
- **Merged from**: `../volume-profile/volume-profile-decision-assist.pine`
- **Model reference**: `../volume-profile/volume-profile-model.md`

---

## 2026-09-06 16:00 IST — Paper week instead of live: ANALYZE mode, and declined signals now leave a record

Live trading is off. From 2026-09-07 BREAKOUT runs one week of paper trading with OpenAlgo in
**ANALYZE mode**, reviewed EOD daily, before any real money is committed.

### What changes, and what deliberately does not

**Nothing in `config.yaml` sets the mode.** The engine reads it at runtime from OpenAlgo's
`/api/v1/analyzer` endpoint (`api_client.fetch_trading_mode`) and adapts: `sandbox_capital`
(Rs35,000, already matching intended live capital) replaces the broker funds call, and
`risk_store` keys its counters on `(mode, date)` so paper and live totals can never mix. The
mode is switched in OpenAlgo itself, not here.

**The limits stay at their live values on purpose.** `max_open_positions: 2`, daily loss limit
4%, price filter 300-5000 — a sandbox could afford any number of positions, but a paper run with
limits the live account cannot honour produces numbers that do not transfer. The week is meant
to answer "what would the live configuration have done".

**This is a far better measurement than the fortnight before it.** That was `breakout.pine`
talking to itself. Now the full pipeline is real: validation, sizing against Rs35k, risk gates,
slot limits, SL-M placement, staged TP exits, the tracker's 10s position poll, the 14:45 time
exit. Only the fill is simulated.

### The gap that had to be closed first: declines left no record

Every early return in `_handle_entry` — blacklist, T2T, exposure limit, symbol/sector
concentration, capital floor, `qty=0`, margin — and the validator gate in `handle_message`
(which is where the price filter, min_rr, min_sl_pct, duplicate and stale checks live) exit
**before** `save()`. A declined signal existed only as a log line and a Telegram message;
`trades.db` recorded the trades that happened and nothing about the ones that did not.

Tolerable when the question is "did my fills work". Not tolerable for this week, whose entire
question is "what would this strategy have done": at `max_open_positions: 2` on Rs35k a real
share of signals will never become an order, and a review that cannot see them would read a
capital constraint as a signal-quality result.

`db.save_declined()` now writes a row for each, carrying the signal's prices, `context` (the
full entry criteria) and `sig_id`, so a declined signal can be scored after the fact against the
same chart data as a taken one — which is what lets the review ask whether the refused ones
would have been the winners.

- **Status `DECLINED`, distinct from `REJECTED`.** REJECTED means the *broker* refused an order
  that was actually sent. DECLINED means no order ever left the engine. Collapsing them would
  make an engine policy decision look like a broker failure.
- **`order_id` is the sentinel `-DECLINED-`, not empty.** The ledger reads a falsy `order_id` as
  "sent but matched by no fill", which is a reconciliation error. A decline is not an error.
- **Excluded from the position ledger, read separately by `load_declined()`.** Otherwise every
  decline becomes a Position with an unfilled entry leg — a `NO_FILL` flag that is not one, and
  it would bury the genuine unfilled orders.
- **Never raises.** Persistence is bookkeeping and must not be able to break signal handling.
- EXIT signals are excluded: an EXIT that fails validation is a reconciliation problem, not a
  trade that did not happen, and counting it would corrupt the number the review depends on.

`python -m signal_engine.analysis` now prints declines grouped by the gate that stopped them,
under the ledger rather than inside it (`--declined` lists every one). The pair of numbers is the
point: a thin week reads completely differently depending on whether the strategy found nothing
or the risk limits refused what it found.

### A bug this work introduced, and the guard against its whole class

Adding `save_declined()` to main's decline paths wrote **six rows into the live
`signal_engine/data/trades.db`** on an ordinary `pytest` run — the existing entry-pipeline tests
patch `save` but knew nothing about a second writer. The rows were deleted; the 221 real ones
were untouched.

Patching each new writer at each call site is a rule nobody can be relied on to follow, so the
fix is structural: an autouse fixture in `tests/conftest.py` repoints `db._DB_PATH` at a
throwaway file for every test in the suite. A future writer is covered before anyone remembers
it exists.

### What this week can and cannot measure

**Can:** how many signals survive the gates and how many are declined and by which gate; whether
`max_open_positions: 2` is the binding constraint; whether the new `min_entry_price: 300` filter
removes the signals it was meant to; whether every trade now emits a terminal event (the
2026-09-06 14:38 fix, still unverified against live alerts); whether extended runner tiers
actually book TP2/TP3; whether the reachability gate thins entries as expected.

**Cannot: slippage.** A sandbox fills at the requested price. Slippage is the single largest
controllable cost in the system — `slippage_factor: 0.10` budgets ~0.19R per round trip, roughly
half of gross on the ORB numbers — and it stays unmeasured until real orders reach a real broker.
Any expectancy from this week is therefore an **upper bound**, and should be read as one.

**Also not measured: margin.** `_resolve_entry_quantity` skips `adjust_qty_for_margin` entirely
in analyze mode, because the sandbox has fixed virtual capital and the broker margin API is not
available. So the "can I actually fund a second position on Rs35k" question — the one that
motivated dropping `max_open_positions` to 2 — is the one thing this week is structurally unable
to answer.

### Open item, deliberately not changed

`intraday-breakingtrade` was switched to `enabled: true` outside this work. It shares the same
two slots and the same daily loss limit, so some BREAKOUT signals will be declined because
BreakingTrade took the slot first — the exact contamination `intraday-orb` was stood down to
avoid. It is at least now visible rather than silent: those declines are recorded with
`max_open_positions reached` as the reason. Left as set, flagged here as a decision to confirm.

`smidestn` also remains enabled and can inject signals into the pipeline.

---

## 2026-09-06 15:44 IST — Going live: SigID, ORB stood down, and three settings that were wrong for real money

BREAKOUT trades real capital from the next session. Paper phase ends here; everything in this
file dated before today is PineScript chart simulation.

### SigID — the key that survives a rejected order

`ledger.py` joins signal to fill on `order_id`, which exists only once an order has been SENT.
A signal the validator rejected, one the broker refused for margin, one that never filled — none
of them have a key, and the ledger falls back to grouping by `(strategy, symbol, day)` and
pairing entries to exits by arrival order. Its own docstring warns that this "will silently
mis-pair two trades in the same name on the same day". `breakout.pine` permits exactly that: a
re-entry after a stop.

`sigId()` emits `SYMBOL-YYYYMMDD-HHmm` built from the **entry bar's** timestamp — frozen at fill
in `orbEntrySignalTime`, so a TP firing three hours later produces the identical string. It goes
out on all five alert types: entry, TP, SL, RUNNER, time exit.

Engine side: `Signal.sig_id`, parsed as a first-class field rather than swept into `context`
(one copy, not two); a `sig_id` column added to `trades.db` through the existing additive
`_ADDED_COLUMNS` mechanism, which migrated the live 221-row database in place; and
`build_ledger` now keys its grouping on it when present. Events without one keep the old
positional rule exactly, so every historical row reconciles as before.

The normalizer needed a change that is easy to miss: `_rewrite_tp_hit` and `_rewrite_sl_hit`
build the canonical EXIT message **from scratch** rather than editing the original, so anything
not explicitly carried is dropped. SigID was being lost on precisely the messages that most need
it. `_sig_id_line()` now carries it through both.

Blank is treated as absent (`_nonblank_or_none`) — an empty-string key would group every keyless
leg in a session into one position, which is worse than having no key at all.

Tests: `test_signal_id.py` — parse from all three alert shapes, absent and blank forms, no
duplication into context, and the two ledger cases that motivated it (two round trips in one
name on one day; an exit arriving after the next entry opened).

### ORB stood down

`intraday-orb` set `enabled: false`. Nothing about ORB changed and it is not being retired —
this is about attribution. Two live strategies sharing two slots on Rs35k means whichever fires
first takes the margin, and the other's rejections then look like signal quality. Running
BREAKOUT alone for the first weeks makes its numbers its own.

### Three settings that were fine for paper and wrong for money

**`max_open_positions` 4 -> 2.** Arithmetic, not preference. At 1% risk with a ~0.30% stop the
notional per position is `risk / (sl_pct x (1 + slippage))` — about 3x capital — so at 20% MIS
margin ONE position needs roughly Rs21k of a Rs35k account. Measured across the 31 Aug - 04 Sep
signals: median Rs21,123 per position, 60% of capital. Two concurrent already needs 1.2x capital
and the Margin API scales the second one down. Slots 3 and 4 could never have been funded;
leaving the cap at 4 would have produced broker rejections that read like signal quality.

**Loss limits re-armed: daily 4%, weekly 8%, monthly 15%.** All three sat at `1.0` — disabled —
under a `# TESTING` comment. With `max_trades_per_day: 10` at 1% risk, going live with them off
means a bad day has no floor: ten full stops is a 10% account loss with nothing stopping the
eleventh. 4% daily is four full stops, and it **would have fired on 03 Sep**, which took exactly
four -1R hits. That is the intent, not a flaw.

**`min_entry_price` 150 -> 300.** For slippage, not edge. NSE tick is Rs0.05 on every name in
the sample — verified against `symtoken`, including IEX at Rs118, so there is no fine-tick
regime to soften this. With a ~0.30% stop the risk per share IS price x 0.003, so one tick costs:

| | price | risk/share | 1 tick |
|---|---|---|---|
| IEX | 118.70 | 0.35 | **0.143R** |
| ITC | 264.65 | 0.79 | 0.063R |
| SBIN | 1035.70 | 3.11 | 0.016R |
| MARUTI | 12884.00 | 41.64 | 0.001R |

A MARKET entry plus a MARKET TP exit is two legs, so sub-Rs300 names spend 0.13-0.29R on ticks
alone against a `slippage_factor` budget of 0.10R. The calibration is simply untrue for them.

**`max_entry_price` deliberately LEFT at 5000.** The same arithmetic says high-priced names are
the *cheapest* to execute and there is no affordability barrier at Rs35k, so 5000 excludes the
best-executing part of the universe for no reason. It is not raised today because widening the
universe on day one adds variance with nothing behind it — and the three names above 5000 in the
sample (DIVISLAB, MARUTI, BAJAJHLDNG) were all losers, which is n=3 and proves nothing in either
direction. Revisit after 2-3 live weeks.

### Logging for the EOD debrief

Two additions, since the first read after a bad session is the log rather than the code:

- `backtrace=True` on the file sink — the frames that produced an error, not just the raising
  line. `diagnose` stays **off** deliberately: it renders local variable values, and the locals
  around an order call hold the API key and the broker session token.
- A second sink, `logs/errors_{date}.jsonl`, ERROR and above, one JSON object per line, 90-day
  retention. The main log is a whole trading day of polls and fills; after a bad session the
  question is "what broke", and that should be a short file. Mirrors the root CLAUDE.md
  convention where `log/errors.jsonl` is the documented first place to look.

Tests: `test_logger_setup.py` — errors serialise with symbol and traceback, INFO/WARNING stay out
of the error sink, the full log still receives everything.

### Unchanged and worth knowing

`smidestn` (the test channel) is still enabled and can inject signals into a live engine. That is
fine while it is only used deliberately, but it is now a live-money path rather than a paper one.

`intraday-breakingtrade` is configured `enabled: false` — added outside this work, inert.

---

## 2026-09-06 15:16 IST — Per-channel enable/disable: the paper phase is now stated, not implied

BREAKOUT is deliberately PineScript-only and is not meant to reach a broker yet. That decision
was correct and is unchanged by this entry. What was wrong is that **nothing in the repo said
so** — the strategy's paper status was expressed as an *absence*: `breakout.pine` posts to
`intraday-breakout` (`-1004450500772`), `telegram.channels` in `config.yaml` listed only
`smidestn` and `intraday-orb`, and a missing line looks identical to a line nobody remembered
to add. It took a database query to establish the difference (`trades.db`: zero BREAKOUT rows,
ever, against 221 ORB rows), and in the meantime the 13:28 review below was written describing
chart simulation as though it were executed trades.

### The change

`telegram.channels` entries take an optional `enabled` key, default `true`:

```yaml
    - name: "intraday-breakout"
      id: -1004450500772
      enabled: false
```

- **Default true** — every pre-existing config keeps working with no edit.
- **A disabled channel is still parsed and still carried in settings.** It is not dropped at
  load. That is the entire point: startup now logs `Channel DISABLED, no trades will be taken
  from it: intraday-breakout (-1004450500772)`, so the paper phase announces itself on every
  boot instead of being invisible.
- **All channels disabled is a distinct, loud failure** from an empty list — the engine says
  which channels it found, that every one is off, and that it will trade nothing.
- `_channel_names()` still covers disabled channels, so a stray message from one logs by name
  rather than as a bare chat id.
- **Prefer `enabled: false` over deleting or commenting out a block.** A commented-out channel
  carries no intent; a disabled one does.

`enabled: "false"` in quotes is handled explicitly. YAML parses that as a non-empty string,
which is truthy — the one way to write this key and get the exact opposite of what it says.

### Scope, deliberately

This gates **subscription**, not execution: a disabled channel is not subscribed at all, so its
messages never enter the pipeline. It is not a shadow or dry-run mode — the engine does not
read, size, validate and then decline to trade. That is a different feature and a larger one;
see the note below on what a paper phase actually needs.

Tests: `test_config.py::TestChannelEnableDisable` (default, explicit false, string forms,
disabled channels surviving into settings) and a new `test_listener_channels.py` (subscription
split, all-disabled, name resolution for disabled channels). 785 passed.

---

## 2026-09-06 14:38 IST — Findings 1-3 implemented; a fourth defect found while doing it

Follow-up to the review directly below, which is left intact as the record of what was measured.
This entry is what was actually changed, plus one bug the work uncovered that the review had not
seen. Full suite green: 772 passed (767 before, 5 new).

### Finding 2 — extended runner tiers switched on, and the ratchet they depend on repaired

`breakout.pine`'s `useExtendedRunnerTiers` and `config.yaml`'s `use_extended_runner_tiers` are
both now `true`. Booking becomes 30% at TP1, 35% at TP2, 35% at TP3 instead of 50/50 closing the
position at TP2.

**The defect this exposed, which changes the case for the flip.** The review's counterfactual
assumed the runner's stop ratchets up as further levels are banked — that is what makes holding
35% past TP2 safe. It does not, and for BREAKOUT trades it never has:

`_replace_runner_sl()` asked `structural_runner_sl()` first and returned as soon as it got a
price. That function returns a price for **every** key-level trigger, floored at break-even. So
for any BREAKOUT trade — all of which carry a trigger — the runner stop was pinned at the entry
level or break-even and stayed there for the life of the trade, and the extended-tier ratchet
added on 2026-09-02 was reachable only through the `elif` branch, i.e. only for plain ORB
breakouts, the one strategy it was not written for.

That is harmless while TP2 closes the position outright: the runner never survives past TP2, so
there is nothing to ratchet. It becomes a real leak the moment 35% is held past TP2 — that
tranche would have sat behind a break-even stop after price had already run 2R, so any reversal
hands the whole move back. **Flipping the toggle without this fix would have made the strategy
worse, not better.**

Fixed: the structural stop is now treated as a floor rather than the answer. Both candidates are
computed and the **tighter** wins — structural early, ratcheted once a further level is banked —
and a stop still never loosens against the one already in place.

The same test run surfaced a second, unrelated bug in that function: `anchor_level` was only
assigned inside the `elif`/`else` branches, so the `logger.info` after a successful SL placement
raised `UnboundLocalError` on **every** key-level runner SL — after the order was placed and the
tracker updated, but before the caller finished the exit. It had gone unnoticed because no test
exercised the structural path end-to-end. `anchor_level` is now always bound.

New tests in `test_main_partial_exit.py::TestKeyLevelRunnerRatchet` cover: TP2 ratcheting above
the structural floor (long and short), TP1 keeping a structural stop that is tighter, the flag
off restoring the old behaviour exactly, and the gap-through case — one 5-minute bar spanning
TP1 to TP2, where the engine sees a TP2 alert with no preceding TP1 and `pos.sl` still at the
original loss-side stop. That last one was flagged in the review as needing verification before
the flip, because 2 of the week's 8 winners (RADICO, LICHSGFIN) gapped through a level.

### Finding 3 — TP1 reachability gate

New `klMaxTpR` input (default **2.0 R**, `0` disables) in the scoring group, threaded through
`klFireGate` alongside `klRoomOK`. A setup whose nearest structural level sits more than that
many R from entry no longer fires — it is **skipped, not rescaled**, for the reason argued in
the review: rescaling TP1 to a flat 2R would re-introduce the arbitrary R-multiple exit the
key-level engine exists to replace, and would not have saved either of the two known losers.

`na` passes the gate: no level ahead at all is a case `klCalcTargets` already handles by falling
back to a reachable 1.5R.

Applied to the pooled sample this removes 4 setups — TATAPOWER (6.47R), DIVISLAB (3.15R),
IEX (2.74R), PIDILITIND (8.73R) — which between them booked nothing and left two of the four
unresolved trades.

**Not done:** the dashboard's verdict row does not yet show this gate, so a setup skipped for
reachability is currently invisible on the chart. `renderVerdict` already carries fourteen
arguments and expanding it is a separate change.

### Finding 1 — every trade now emits an ending

New `orbOpenQtyPct` state variable: the percentage of the original position the **live engine**
still holds, tracked separately from `strategy.position_size`. Set to 100 at fill, compounded
down by each alert's `ExitQtyPct` (30% then 50% of the remainder leaves 35%, not 20%), zeroed on
any 100% exit, SL alert, or time exit.

Two gates now run off it instead of the strategy position:

- **14:45 time exit** — `(strategy.position_size != 0 or orbOpenQtyPct > 0)`. This is the fix for
  cause (a): a post-TP1 runner is invisible to `strategy.position_size`, which is why APLAPOLLO
  and ICICIPRULI each sent one `TP1 HIT | ExitQtyPct: 50` and then nothing. The direction now
  comes from `orbTradeDirection`, since the strategy position is already flat in exactly the case
  this fires for. `strategy.position_size` stays in the OR so a plain ORB trade behaves as before.
- **End of session** — a second backstop that emits the terminal event if the 14:45 clock never
  ran at all.

Because the root cause of (b) is still unknown, the fix is deliberately positioned rather than
targeted: both gates sit at the top level of the script, outside the `not orbLinesFrozen` block
that governs per-bar TP/SL detection, so whatever silences that block cannot silence them.

**Known limitation, worth being explicit about.** If a runner is stopped out broker-side after a
partial (the SL alert is correctly suppressed once any TP has booked, so the engine is not told
to exit at the original stop), the record will now carry a `TIME_EXIT` event priced at the 14:45
close rather than at the price the runner actually stopped at. The R attributed to that trade
will be wrong — but wrong and visible beats absent, which is where the last two weeks left it.
Reporting the runner's true exit price needs the engine to emit its own terminal event, which is
a python-side change and a candidate for the next iteration.

`strategy.exit()` was deliberately **not** given a `qty_percent`. Making the strategy model book
partials would fix cause (a) at the source and keep `strategy.position_size` meaningful, but it
changes every Strategy Tester number this file has ever produced. The tracking variable gets the
alert stream right without touching the backtest model.

### What to watch over the next week

1. A `BREAKOUT EXIT / Reason: TIME_EXIT` message should now appear for any trade still holding a
   runner at 14:45. Zero of them across the next week means the fix did not take.
2. `TP2 HIT` alerts should read `ExitQtyPct: 50`, not 100, and `TP3 HIT` should start appearing
   on trades that previously produced a `RUNNER` note instead.
3. Fewer entries per day, from the reachability gate. Roughly one in five setups in the pooled
   sample drew a TP1 beyond 2R.

---

## 2026-09-06 13:28 IST — Week-1 forward review (31 Aug - 04 Sep): the sample is net positive, but 4 of 20 trades have no recorded ending

> **Correction added 2026-09-06 15:02 IST — read this before the numbers below.** This entry
> was written as if the sample were live executed trades. It is not, **by design** — BREAKOUT is
> deliberately in a PineScript-only paper phase and was never meant to reach a broker yet. The
> mistake was mine in the framing, not a wiring fault. `breakout.pine` posts to
> `intraday-breakout` (`-1004450500772`), which the engine does not subscribe to; `trades.db`
> holds **zero BREAKOUT rows, ever** (221 ORB rows, last one 2026-08-23), and the engine's own
> logs stop on 2026-08-25.
>
> So every price, P&L and R figure below is **PineScript's own chart simulation**: theoretical
> fills on TradingView's feed, no broker, no slippage, no rejections, no margin. Read "booked"
> throughout as "the chart would have booked". The findings themselves survive unchanged —
> Findings 1 and 3 are defects in the alert stream and the target ladder, which are real either
> way, and Finding 2's runner-tier gap is real in the same sense. What does not survive is any
> claim about live edge, which was already marked unproven and is now not even a live sample.
>
> The one thing this improves: the ratchet defect found while implementing Finding 2 (runner
> stop pinned at break-even for every key-level trade) was **latent, not costly** — no BREAKOUT
> trade ever reached the broker for it to damage.
>
> Channel topology and what to do about it: see the entry above this one.

The review promised by the 2026-08-30b entry ("a live sample to review after the first week").
Source: `signal_engine/pinescripts/telegram/intraday-breakout-channel-result-31082026-to-04092026.json`
(45 Telegram messages, 20 entry alerts, 5 sessions). Cross-checked against the earlier
`...-26082026-to-28082026.json` (12 entries) wherever a claim could be tested twice — that
earlier week ran the pre-08-30b configuration, so it is used only to falsify week-2 patterns,
never to confirm them.

Nothing in the strategy was changed by this entry. It is the measurement plus a ranked list of
what to change next, so that when a change is made later there is a record of what it was made
against.

### The result

18 of 20 entries reached a recorded ending. Booked outcome across those 18: **+4.31R, 8 wins /
10 losses (44% win rate, +0.24R per trade)**. R here means "multiples of the money risked on
that trade" — +1R is a win the size of the stop, -1R is a full stop-out.

| Session | Entries | Long/Short | Booked R | Detail |
|---|---|---|---|---|
| 08-31 | 4 | 1/3 | +0.01 | INDHOTEL -1.00, ITC +1.51, APLAPOLLO +0.50, DIVISLAB -1.00 |
| 09-01 | 2 | 1/1 | +2.25 | PIDILITIND no ending, RADICO +2.25 |
| 09-02 | 6 | 0/6 | +2.62 | AXISBANK -1.00, HAVELLS +2.25, LTF -1.00, ICICIPRULI +0.50, CONCOR +1.88, IEX no ending |
| 09-03 | 5 | 5/0 | -1.58 | UNOMINDA -1.00, PFC -1.00, SBIN -1.00, MARUTI -1.00, INDIGO +2.42 |
| 09-04 | 3 | 3/0 | +1.01 | DELHIVERY -1.00, BAJAJHLDNG -1.00, LICHSGFIN +3.01 |

Pooled with the earlier week: 30 resolved trades, +6.27R.

**What this does and does not say about the negative backtest.** The entry two below measured
key-level breaks following through 30.9% of the time against a 33.3% random-walk baseline, and
concluded the premise is negative. This week does not overturn that and is not offered as
overturning it. 18 trades is far too few — a run of +4R from 18 samples with an average win
around +1.8R and losses fixed at -1R is well inside what chance produces from a coin-flip
process. What it does establish is narrower and still worth having: the asymmetric exit
structure (fixed -1R stop, winners allowed to reach 2R-4.5R) can carry a sub-50% hit rate to a
positive number, so the strategy is not obviously bleeding and the collection period should
continue. Treat the sign as unproven and the process as worth another month.

### Finding 1 (highest priority, mechanical, not statistical) — 4 of 20 trades never emit an ending

This is a data-integrity defect, not a trading one, and it is ranked first because it silently
corrupts every number above and every number the next review will produce.

**Two separate causes, both confirmed in code.**

*(a) The half of a position held past TP1 has no terminal alert.* `strategy.exit("TP1_L",
"Long", limit=tp1, stop=sl)` (`breakout.pine:2828`, and `:2949` for shorts) carries no
`qty_percent`, so in the Pine strategy model TP1 closes the whole position. But the alert sent
to the engine says `ExitQtyPct: 50`, so the live engine keeps half. From that moment the two
models disagree, and neither path can report the remainder's fate:

- the SL alert is blocked by `bool anyTPBooked = orbTP1Hit or ...` (`:4037`) — deliberately, so
  the engine is not told to exit at the original stop after the runner stop has moved up;
- the time-exit alert at `:4175`-`:4178` requires `strategy.position_size != 0`, which is already 0.

APLAPOLLO and ICICIPRULI both show a lone `TP1 HIT | ExitQtyPct: 50` and then silence. Their
+0.50R each is the booked half only; the other half's result is simply unknown. Two of eight
winners are therefore recorded at a floor, not a value.

*(b) Two trades went silent for a reason not yet identified.* PIDILITIND (09-01 09:55) and IEX
(09-02 11:40) produced an entry alert and then no message of any kind.

**Correction, same day:** this entry first attributed that to the `bar_index > orbEntryBar`
guard on the SL check (`:4038` long, `:4143` short) suppressing a stop tagged on the entry bar.
That is wrong, and the mistake is left visible rather than edited away. `orbEntryBar` is set to
the **signal** bar (`:2799` long, `:2921` short), while `strategy.entry` fills at the next bar's open — so the fill
bar already satisfies `bar_index > orbEntryBar` and a first-bar stop is announced normally. The
guard is doing its actual job, which is to stop the signal bar's own range from counting.

What is established: whatever silenced them, the entire per-bar TP/SL detection block sits
behind `if not orbLinesFrozen and ...` (`:3971`), so any path that freezes the trade lines also
takes the alerts with it, and the 14:45 time exit — the one place that should have caught the
fallout — was itself unreachable for the same trades because of (a). The remaining
circumstantial detail is that these two carry the tightest stops in the sample (0.215% and
0.295% of price, against a 0.30% median). **Root cause still unknown**; the fix below is
therefore deliberately cause-agnostic rather than a patch to a specific guard.

Corroboration that the emitter itself is not simply broken: the ORB export
(`trade-analysis/orb-telegram-export-2026-Q1.json`) contains time-exit alerts; both breakout
exports contain zero across 32 entries.

**Why it matters beyond bookkeeping.** Under (b) the signal engine believes it holds a position
that the strategy considers closed, and carries that belief until its own 14:45 clock. Under (a)
the forward test cannot answer the one question it was deployed to answer — whether holding past
TP1 pays — because the held portion is exactly the part with no recorded outcome.

**Proposed fix (not yet made):** give the exit path a terminal event that does not depend on
`strategy.position_size`. Track the live-model remainder in a Pine variable of its own
(`klOpenQtyPct`, set to 100 at fill and decremented by each `tpFireQtyPct`), fire the time exit
on `klOpenQtyPct > 0` rather than on the strategy position, and allow an SL alert on the entry
bar when the trigger and the stop are not on the same bar's extremes. Each of these is small in
isolation; together they make the next review's numbers trustworthy.

### Finding 2 (highest value, already built, just switched off) — extended runner tiers never took effect

Every `TP2 HIT` alert this week reads `ExitQtyPct: 100`, and `config.yaml`'s `bracket.use_extended_runner_tiers` read
`false`. The TP1/TP2/TP3 tiering shipped on 2026-09-02 (entry below)
has therefore not been exercised by a single live trade, while the sample produced two explicit
`RUNNER` observations — RADICO and HAVELLS, **4.50R unbooked each** — which are precisely the
events that feature exists to capture.

Re-scoring the week under 30/35/35, using the prices actually printed in the alerts and assuming
the final tranche exits at the ratchet stop (level minus 0.3R x the level's own R-multiple) when
no further target was reached:

| Trade | Reached | Booked as run | Under 30/35/35 | Delta |
|---|---|---|---|---|
| HAVELLS | TP3 (runner) | +2.25 | +3.08 | +0.83 |
| RADICO | TP3 (runner) | +2.25 | +3.04 | +0.79 |
| LICHSGFIN | TP3 | +3.01 | +3.61 | +0.60 |
| INDIGO | TP2 | +2.42 | +2.59 | +0.17 |
| ITC | TP2 | +1.51 | +1.50 | -0.01 |
| CONCOR | TP2 | +1.88 | +1.82 | -0.07 |
| | | | **net** | **+2.31** |

The week would have been roughly **+6.6R instead of +4.31R, a 54% improvement, with no change to
which trades were taken and no change to risk per trade** — the stop is unchanged, only the
distribution of what gets booked where. The two trades that lose a little (ITC, CONCOR) lose it
because 35% is held past TP2 into a ratchet stop instead of being banked at TP2; that is the
cost the feature was designed to pay, and it is an order of magnitude smaller than what the
runners return.

This is the cheapest available improvement in the whole review: the mechanism is written, tested
(`test_main_partial_exit.py`), and revertible by one boolean on each side. Flip
`config.yaml`'s `use_extended_runner_tiers` and `breakout.pine`'s `useExtendedRunnerTiers`
together, as that entry instructs.

One wrinkle to verify before flipping: RADICO gapped straight through TP1 to TP2 in a single
bar, and LICHSGFIN gapped through TP2 to TP3. The Pine only reports the furthest level reached
in a bar, so the engine never sees the skipped level. Under the current 50/100 split that is
harmless. Under tiering the engine must close the skipped tranche at the reported price rather
than wait for a level that has already passed — worth a test in `compute_next_tp` /
`_replace_runner_sl` before this goes live, because 2 of 8 winners gapped.

### Finding 3 (mechanical argument, thin sample, replicated) — TP1 has a floor but no ceiling

`klCalcTargets` (`breakout.pine:1148`) sets `d = math.max(|t1 - entry|, risk * 1.0)`: the first
target is the nearest structural level, floored at 1R so R:R never collapses. There is no
corresponding cap, and the whole ladder scales off `d` (TP3 = 3d), so when the nearest level
happens to be far away the trade is left with no reachable checkpoint at all.

PIDILITIND drew TP1 at **8.73R** (TP3 therefore at 26R) with its stop 0.215% away. DIVISLAB drew
3.15R, IEX 2.74R.

Pooled across both weeks, trades whose TP1 landed beyond 2R: **4 of 4 failed to book anything**
— TATAPOWER (6.47R, week 1) and DIVISLAB stopped out, PIDILITIND and IEX are two of the four
with no recorded ending at all. Every one of the 8 winners in week 2 had TP1 at exactly 1.00R or
1.50R. The mechanism explains the outcome without needing the statistics: a trade whose first
profit-taking checkpoint is unreachable degenerates into all-or-nothing on a distant level
behind a 0.3% stop, which is the exact shape the negative backtest condemned.

**Proposed change:** add `klMaxTpR` (default 2.0) and **skip the setup** when the nearest level
in the trade's direction sits beyond it, rather than rescaling TP1 to an arbitrary 2R. Skipping
is the option consistent with this file's own stated position ("if a level is worth entering on,
it is worth exiting on", `:1142`-`:1143`) — rescaling would re-introduce the R-multiple exit the
key-level engine was built to replace. Rescaling also has no support in the data: capping TP1 at
2R would not have saved DIVISLAB or TATAPOWER, both of which stopped out; skipping them saves
+2R and removes two of the four unresolvable trades.

n=4 is thin, so this belongs behind a default-on input that can be turned off in one click, not
hardcoded.

### Finding 4 (risk shape, not expectancy) — four same-side positions on one impulse is one bet, not four

09-03 took five entries, all long, all "Above VAH" continuations. PFC and SBIN fired at
**10:30:12 and 10:30:17** — the same PDH-RT setup on two names five seconds apart — and both
stopped at 10:35. UNOMINDA, already open from 10:10, stopped at 10:40. Three full stop-outs
inside ten minutes.

09-02 was the mirror image: six entries, all short, three winners.

With `max_open_positions: 4` and no side constraint, the engine is free to put its entire
allowance on one direction responding to one index-wide move. On 09-03 that was **-3% of capital
in ten minutes** from positions the risk engine had counted as three independent 1% risks. The
week's total was still positive, so this is not an expectancy problem; it is a statement about
the drawdown shape the current caps permit, and it will matter more as the sample grows.

**Proposed tuning:** a same-side concurrency cap (max 2 concurrent long or short) or a
short-window entry cap (max 2 entries in any 15 minutes). Applied to this sample the former
blocks SBIN and turns 09-03 from -1.58R to -0.58R — a small effect on R, which is the point:
the change buys drawdown shape, not return, and should be judged on that.

### Finding 5 (guard against a tempting wrong move) — do not raise the score threshold

The instinct after a 4-loss session is to demand a higher confluence score. The data says the
opposite, in both weeks independently:

| | Score >= 9 | Score < 9 |
|---|---|---|
| Week 2 (18 resolved) | 5/13 wins, +3.06R | 3/5 wins, +1.25R |
| Week 1 (12 resolved) | 3/7 wins, +0.68R | 3/5 wins, +1.28R |
| Pooled (30) | 8/20 wins (40%), +0.19R/trade | 6/10 wins (60%), +0.25R/trade |

Three of week 2's eight winners sat at the minimum score of 7 (APLAPOLLO, RADICO, ICICIPRULI,
worth +3.25R between them) and would have been filtered out by moving the threshold to 8. The
single highest-scoring trade of the week, DELHIVERY at 11, was a full stop-out.

The score does not discriminate and mildly inverts. This is consistent with "Corollary 2 —
per-symbol selection does not work" in the 2026-08-30 entry. **Leave the threshold at 7.**
Recorded here so the next losing day does not produce this change by reflex.

### Finding 6 (looks strong, does not replicate — do not act on it) — volume-factor and RVOL splits

Week 2 alone produces the most attractive-looking filter in the review:

| Week-2 filter | In | Out |
|---|---|---|
| VF >= 1.0 | 8/15 wins, +7.31R | 0/3 wins, -3.00R |
| RVOL >= 2.5 | 6/11 wins, +8.31R | 2/7 wins, -4.00R |
| VF >= 1.0 AND RVOL >= 2.5 | 6/10 wins, +9.31R (+0.93R/trade) | 2/8 wins, -5.00R |

A filter that turns +4.31R into +9.31R and removes every losing trade below 1.0 volume factor is
exactly the kind of result that should be distrusted, so it was checked against week 1 — where
it inverts:

| Week-1 filter | In | Out |
|---|---|---|
| VF >= 1.0 | 3/7 wins, +0.28R | 3/5 wins, +1.68R |
| RVOL >= 2.5 | 1/3 wins, -0.50R | 5/9 wins, +2.46R |

The pooled numbers still favour the filter, but only because week 2 supplies most of the
evidence and all of the effect. Three of the three VF < 1.0 losers are a single week's worth of
coincidence until a second week agrees. **No filter added.** What should happen instead: keep VF
and RVOL in the entry alert (they already are) and re-test this split at the next review with
roughly double the sample. If it holds twice, it becomes a candidate gate; if it inverts again,
it was noise and the record shows it was never acted on.

Same treatment for the direction split — shorts beat longs in both weeks (pooled 9/15 and +6.99R
short against 5/15 and -0.72R long) — which is at least directionally consistent, but 09-03 and
09-04 were two long-side sessions in a market that fell, and two weeks cannot separate a
strategy property from a market regime. Watch it; do not gate on it.

### What was deliberately not changed

- **Score threshold** — see Finding 5; the data argues against the change most likely to be made.
- **CLV gate (0.50/0.50)** — direction-normalised CLV averages 0.85 for winners and 0.74 for
  losers, a gap far too small to act on. The gate was relaxed on 08-30b specifically so this
  regression would be possible; it now has 18 more samples and still says nothing.
- **Entry window (09:45-11:45)** — week 2 shows the 11:00-11:45 slot going 3 for 3 at +6.55R and
  the 09:45-10:15 slot going 1 for 4 at -1.49R, which invites narrowing the window. Week 1 shows
  no such pattern (+0.83R after 11:00 against +1.13R before). Noise.
- **Trigger families** — PD (PDH/PDL) went 1/5 and -3.50R in week 2, which looks like a case for
  dropping it, but the same family went 3/3 and +3.85R in week 1. Two weeks, opposite signs, n=8.
- **Stop construction** — no evidence either way. Stop distance in ATR barely separates winners
  (1.18 average) from losers (1.29), and every trade's risk sat in a narrow 0.215%-0.437% band.

### Confidence ledger

| Claim | Status |
|---|---|
| The post-TP1 remainder has no terminal alert; `strategy.exit` at `:2828` closes 100% | **Strong** — read directly from the code, and matches APLAPOLLO/ICICIPRULI going silent |
| Zero time-exit alerts across 32 breakout entries while ORB emits them | **Strong** — counted in both exports |
| Same-bar stop-outs are silenced by `bar_index > orbEntryBar`, explaining PIDILITIND and IEX | **Disproven, same day** — `orbEntryBar` is the signal bar, not the fill bar, so the guard never reaches a first-bar stop. Why those two went silent is still **unknown**; the fix shipped is cause-agnostic |
| Extended runner tiers are off in live config and would have added about +2.31R this week | **Strong** on "off" (config plus every `ExitQtyPct: 100`); **suggestive** on the magnitude, which assumes the un-run tranches exit at the ratchet |
| A TP1 beyond 2R produces no bookable trade | **Suggestive** — 0/4 pooled with a clean mechanical explanation, but n=4 |
| Four same-side positions on one impulse concentrate risk beyond what the caps intend | **Strong** as a description of 09-03; **unproven** that a side cap improves returns |
| Raising the score threshold would hurt | **Suggestive** — same sign in both weeks, and it cost +3.25R of week-2 winners, but the effect is small |
| VF >= 1.0 and RVOL >= 2.5 select winners | **Disproven as a stable effect for now** — strong in week 2, inverted in week 1 |
| Shorts outperform longs | **Suggestive** — same sign both weeks, confounded by a falling market on the two long-heavy days |
| The strategy has a positive edge | **Unproven, and not even live-tested** — +4.31R over 18 trades is inside chance, 4 of 20 endings are missing, and the whole sample is chart simulation with the engine disconnected (see the correction at the top of this entry) |

### Next review

Re-run at roughly 40-50 resolved trades. The three things that must be true for that review to
say more than this one: every trade has a recorded ending (Finding 1), the runner tiers are
actually running (Finding 2), and VF/RVOL are re-tested on the new sample without week 2's
numbers being reused to confirm themselves.

---

## 2026-09-02 — Extended runner tiers: TP1/TP2/TP3 profit booking, revertible via one input

The 2-tier exit (`tp1ExitQtyPct` at TP1, then either `tp1_5ExitQtyPct` or a hardcoded 100% at
TP2) meant TP2 always closed whatever remained — so RECLTD and FEDERALBNK running to TP3 on
2026-08-24 had nothing left to book there. `buildRunnerObsAlert` only logged that as an
unbooked move after the fact.

### The mechanism

New `useExtendedRunnerTiers` bool (default off — full revert path). On: TP1=30%, TP1.5=0%
(unchanged, still display-only, held for TP2), TP2=50% of whatever remains, TP3=100% of
whatever remains. A new `tp2ExitQtyPct` input (default 100) keeps TP2's old behaviour exactly
reproducible when the master toggle is off.

Companion python-side change, gated by `config.yaml`'s `bracket.use_extended_runner_tiers`:
`main.py`'s `_replace_runner_sl()` now anchors the post-partial-exit stop to whichever TP
level was just hit (TP1 → TP1.5 → TP2), not always TP1, and never loosens a stop already in
place. `compute_next_tp()` gained TP2 → TP3 so the partial-exit notification's "next target"
is correct once TP2 becomes a partial exit rather than always the final one.

### Why 30/35/35, considered against 40/30/30 and 50/25/25

The split has to answer two pulls at once: don't miss the runner on a trend day, and still
book whatever the move gave if TP2/TP3 never come. Neither pull has BREAKOUT-specific
evidence to lean on — the key-level engine has been live only since 2026-08-30 as forward-
data collection against the negative backtest two entries below (30.9% follow-through vs a
33.3% random-walk baseline), so there is no measured TP2/TP3 hit rate for this strategy yet.

An initial 40/30/30 default borrowed ORB's own number instead — 72.3% of TP1 trades also
reach TP1.5 (`STRATEGY-ANALYSIS.md`) — as the nearest available evidence, which made it a
conservative placeholder rather than an optimum for BREAKOUT specifically.

Since then the stock universe feeding BREAKOUT narrowed to trend-qualified names only. That
raises confidence in continuation without creating a new BREAKOUT number to calibrate
against, so the split was revised by hand: TP2 keeps exactly 50% of whatever remains after
TP1 (so TP2 and TP3 land equal by construction — (100-TP1)/2 each), which leaves TP1's own
percentage as the only knob. Lowering it from 40 to 30 gives 30/35/35.

50/25/25 was rejected: it books more at TP1 but shrinks the TP3 leg further, working against
not missing the runner. 20/40/40 or lower was rejected too: it leaves too little booked at
TP1 — the easiest target, R=1.0 — for a strategy with no proven edge yet, working against
banking whatever the move gave if it stops there.

The runner-SL ratchet is why 30% is defensible rather than reaching for a bigger TP1 bucket
out of caution: the 70% held past TP1 is not sitting exposed hoping for TP2 to arrive. Its
stop moves up to near TP1's price the moment TP1 fires and only ever tightens from there, so
it is either stopped out at a still-profitable level or continues. The exit fractions decide
what becomes cash at each checkpoint; the ratchet decides how much of the rest stays
protected regardless of whether a further checkpoint is ever reached.

Tests: `signal_engine/tests/test_main_partial_exit.py` — TP2→TP3 in `compute_next_tp`,
ratchet on/off (off byte-identical to the pre-existing TP1-buffer-only behaviour), ratchet
never loosens an existing SL. Two pre-existing characterization tests that asserted
`compute_next_tp(pos, "TP2") is None` were updated to the new TP2→TP3 contract.

### Follow-up, same day: the ratchet buffer needed to scale with the anchor's R-multiple

The ratchet above used a flat buffer — `tp1_runner_sl_buffer` (0.3R) subtracted from
whichever level the stop had just moved to. That is unchanged and correct at TP1: this SL was
never at breakeven, it sits at TP1 minus the buffer (~70% of the TP1 leg locked in), and that
predates this feature. It stops being correct once the ratchet reaches further out — a flat
0.3R buffer is 30% of TP1's own distance from entry but only 15% of TP2's, so the stop sits
proportionally closer to TP2, itself a level likely to draw other participants' stops, than
it ever sat relative to TP1. That is real stop-hunt exposure the flat version did not price
in.

Fixed: `buffer = tp1_runner_sl_buffer x risk_distance x anchor_multiplier`, where
`anchor_multiplier` is the R-multiple of whichever level the ratchet just moved to (1.0 at
TP1, 1.5 at TP1.5, 2.0 at TP2). Same 30%-of-leg headroom at every level instead of a fixed
absolute amount that shrinks in relative terms the further the ratchet extends. TP1 itself is
unaffected (multiplier 1.0 either way). New test covers the TP1.5 case (1.5x) directly, on
top of the TP2 case already covered.

Whether TP1's own long-standing ~70%-of-leg lock-in is itself worth moving closer to
breakeven is a separate question — it predates extended runner tiers, applies to every
strategy using the runner SL, and deserves its own deliberate review rather than being folded
into this feature.

---

## 2026-08-30b — Deployed live as forward-data collection; two bugs fixed, CLV gate relaxed

Deployed deliberately against the negative backtest below. The decision is the operator's and
is recorded as such: the entry immediately after this one says key-level breaks follow through
less often than chance, and nothing here contradicts it. The purpose is a live sample to review
after the first week, not a claim that the edge exists.

**Baseline for that review — the configuration these trades were produced by:**

| | Setting |
|---|---|
| Trading families | VA rejection + retest, PDH/PDL, IBH/IBL. VA **acceptance off** |
| ORB entries | off (ORB remains a drawn confluence level) |
| Score threshold | 7 |
| CLV gate | 0.50 / 0.50 (was 0.65 / 0.35) |
| Entry window | 09:45-11:45, one entry per symbol per session, PM window off |
| Stop | level -/+ 0.35 ATR, floored at max(0.5 ATR, 0.3% of price) |
| Targets | TP1 = nearest level less 0.15 ATR, floored 1R; TP2 = next level out; 50% booked at TP1 |
| Engine | 1% risk, 4 concurrent, 10 trades/day, `min_sl_pct` 0.002, time exit 14:45 |

### Bug — value-area runners never got their structural stop

`structural_runner_sl` in `main.py` resolved the triggering level by lower-casing the family
out of the trigger tag: `VAH-RT` to `context["vah"]`. But `klLvlNames` emits the previous
session's value area **P-prefixed** — `PVAH` / `PPOC` / `PVAL` — a rename made on 2026-08-26
(`c4ce9d47e`) one day after the lookup was written (`77c172a85`). The key never matched, the
`KeyError` was caught, and every `VAH-*` / `VAL-*` runner fell through to the `TP1 - 0.3R`
fallback instead of stopping at the level whose defence was the trade's thesis.

Six of the fourteen setup codes were affected. PD and IB were not — their names match.

`_LEVEL_FAMILIES` (tuple) became `_LEVEL_CONTEXT_KEYS` (family to wire key). The bare name is
still accepted as a fallback so alerts predating the rename resolve. The existing tests passed
throughout because they asserted against `{"trigger": "VAH-RT", "vah": ...}`, which is not what
goes over the wire; a test using the real eleven-level alert context was added.

### Bug — the BREAKOUT blacklist did not exist

`validator._check_blacklist` looks up `settings.blacklist[signal.strategy.upper()]`. Only
`_global`, `ORB` and `EMA9` were defined, so BHEL — hard-blocked for `ORB` on 0% WR across
Q1+Q2 2026 — was fully tradeable the moment the same setup arrived under the `BREAKOUT` tag.
`blacklist.BREAKOUT` added with the same entry.

### Change — CLV gate 0.65/0.35 to 0.50/0.50

Two reasons, the second the more important for a data-collection week.

The 49,677-event study in the entry below ranks the strict setting in the worse half:
unconditional breaks follow through 30.9%, `CLV >= 0.65` gives 30.3%, marubozu 29.6%. Demanding
a decisive break candle selects bars whose move has already happened.

And the gate was censoring its own evidence. `CLV` is carried in every entry alert and stored
in `trades.db.context`, but with the gate at 0.65 every logged long had `CLV >= 0.65` by
construction — zero variance, so no amount of live data could ever test it. At 0.50 the log
spans 0.50-1.00 and the first-week review can regress outcome on it.

Set to the midpoint rather than removed: a long trigger should still close in the upper half of
its own bar. Expect roughly 20-40% more signals; `max_trades_per_day` 10 and 4 concurrent slots
still bound the exposure.

### Not fixed, known, accept for now

The alert's `Entry:` is the confirming bar's **open**, but `calc_on_every_tick=false` means the
alert does not fire until that bar **closes** — so the engine sizes on a price one bar stale
while filling at market. `slippage_factor` 0.10 pads it and `_auto_close_on_tp_overshoot`
catches the extreme. Measurable after the fact: `trades.fill_price` versus `Entry`.

`strategy.exit(limit=tp1)` still closes 100% at TP1, so the TradingView Strategy Tester does not
model the 50% runner and its numbers are not this strategy's. Live P&L is the only valid read.

The Pine edits have **not** been compiled on TradingView.

---

## 2026-08-30 — The premise itself is negative: breaks follow through less than a coin flip

The 2026-08-28 entry below concluded that gross was "indistinguishable from zero" and blamed
the stop floor. That was too generous. Widening the stop and changing the target were both
tested and neither helps, because the problem is upstream of risk management: **the event the
whole engine trades does not happen often enough.**

### The measurement

Rather than tune a losing configuration, every PDH/PDL/IBH/IBL break in the universe was
collected — **49,677 events**, 208 F&O names, 59 sessions — and walked forward up to 12 bars
to see whether it reached **+1.0 ATR** in the break direction before giving back **0.5 ATR**.

The benchmark is the part that matters. For a driftless random walk the probability of
reaching `+a` before `-b` is `b / (a + b)` = **33.3%**. That is the number a breakout has to
beat to carry any information at all.

| | Value |
|---|---|
| Random-walk baseline | 33.3% |
| Observed follow-through | **30.9%** |
| t vs baseline | **-9.87** |
| Symbols significantly ABOVE baseline | **1 of 208** (5 expected by chance) |
| Symbols significantly BELOW baseline | 35 of 208 |

Key-level breaks on the NSE F&O universe follow through *less* often than chance. They are
mildly mean-reverting. That single fact explains the negative gross expectancy, and it
explains why every exit-side fix fails.

### Corollary 1 — the CLV gate is inverted

`klClvLongMin = 0.65` is meant to demand a decisive break candle. Measured on the same events,
it selects the **worse** half:

| Break-bar pattern | n | Follow % | t vs baseline |
|---|---|---|---|
| Doji | 1,214 | 34.3 | +0.69 |
| Big range (>1.5 ATR) | 16,818 | 31.3 | -5.70 |
| Close beyond prior high/low | 37,218 | 30.7 | -11.00 |
| Strong close, CLV >= 0.65 | 40,613 | 30.3 | -13.25 |
| Inside bar | 5,199 | 30.1 | -5.14 |
| Harami | 3,653 | 30.0 | -4.39 |
| Marubozu, CLV >= 0.8 | 30,321 | 29.6 | -14.25 |
| Engulfing | 8,731 | 29.3 | -8.21 |
| Hammer / shooting star | 1,163 | 28.2 | -3.89 |

Ranked by candle strength: marubozu (29.6) < strong close (30.3) < unconditional (30.9). **The
more decisive the break candle, the worse the follow-through** — the move already happened
inside that bar, so the entry buys its high. No pattern clears the 33.3% baseline. The only one
above it is the doji, at t = 0.69, which is not significant and is itself the absence of
conviction.

This extends the 2026-08-29 "Candlestick patterns measured, and mostly switched off" finding in
`PRD.md` to the key-level trigger specifically: there is no confirming candle to wait for.

### Corollary 2 — per-symbol selection does not work

The obvious response is "then trade only the names that do respect levels". Tested by splitting
the window in half and measuring each symbol twice, independently:

- Split-half correlation **r = +0.095** (p = 0.17), Spearman +0.096
- Of the top 20 in the first half, **2** remain top 20 in the second (4 expected by chance)
- Observed cross-symbol sd is 3.3 points; typical per-symbol standard error is 3.0 of those

The ranking is almost entirely sampling noise, so a per-symbol whitelist cannot be built from
this metric. Worked example: TCS reads 38.8% in the first half and 29.6% in the second (full
34.0%, t = +0.2 vs baseline); M&M reads 40.0% then 28.0% (full 33.5%, t = 0.0). They are
statistically identical, and M&M's first half beats TCS's second half. `BANKINDIA` is the one
symbol above baseline at t = +3.1 and stable across halves (45.3 / 42.3) — treat as a lead, not
a result: across 208 tests one such reading is roughly what multiple testing produces.

### Corollary 3 — the reversion is real but too small to trade

Fading the break (risk 1.0 ATR to make 0.5 ATR, fair value exactly zero):

| Event set | n | Fade win % | Edge (ATR) | t | Edge (bps) |
|---|---|---|---|---|---|
| All breaks | 49,677 | 69.1 | +0.036 | +11.50 | **+0.83** |
| Strong-close breaks | 40,613 | 69.7 | +0.045 | +13.25 | **+1.05** |
| Weak-close breaks | 9,064 | 66.2 | -0.007 | -0.95 | -0.16 |

t = 13.25 is about as certain as this kind of measurement gets, and it is worth **1.05 bps**
against a **8-10 bps** round trip. Statistically overwhelming, economically an order of
magnitude short. Do not build a fade strategy on it either.

### What was tested and did not help

All at 10 bps, full period, against the -0.376R / -2.04 bps shipped baseline (n = 1,522):

| Variant | net_R | gross_bps |
|---|---|---|
| Next key level, floored 1R (shipped) | -0.376 | -2.04 |
| Fixed 1:1 | -0.384 | -2.29 |
| Fixed 1:2 | -0.421 | -3.32 |
| Fixed 1:3 | -0.383 | -2.28 |
| Stop at initiative drive candle | -0.383 | -4.09 |
| Wider stop floor (1.0 ATR / 0.6%) | -0.274 | -6.45 |
| Daily HTF bias gate | — | worse in both windows |
| Engulfing required | — | see note |

The wider stop floor looks like an improvement in R and is not one: net_R rises only because a
bigger stop makes each R worth more, shrinking the fixed cost as a fraction of it, while
gross_bps — the cost-free measure — gets three times worse. This corrects the 2026-08-28 entry's
implication that the stop floor was the root cause.

**Note on the engulfing filter.** A first ablation on the filtered trade set (n = 384) showed
engulfing improving gross in BOTH windows (+0.94 IS, +2.73 OOS), which looked like a genuine
finding. At zero cost that variant nets +0.050R at **t = 0.76** — not significant — and the
sign flips below 4 bps. The 8,731-event measurement above then contradicted it outright. It was
small-sample noise; the large-sample result governs. Recorded because the intermediate number
is exactly the kind that gets shipped by mistake.

### Adapter changes

`key_level.py` gains four knobs, all defaulting to the previously shipped behaviour so the
baseline reproduces to the trade:

- `tp_mode` — `level` (shipped) | `r` | `level_min_r`, with `tp_r`
- `sl_mode` — `level` (shipped) | `drive` (beyond the initiative drive candle) | `wider`,
  with `drive_buffer_mult`, `min_sl_atr`, `min_sl_pct_price`
- `pattern` — `""` (shipped) | `engulf` | `engulf_or_clv` | `pin`
- `use_daily_bias` / `daily_bias_len` — HTF bias gate from completed sessions only

### What this does NOT cover

Orderflow of any kind. The VA family (`PVAH`/`PPOC`/`PVAL`) is still untested for the
1-minute-history reason given below. The opening-type / day-type classifier behind
`klBlockCounterBias` is still not implemented. A discretionary trader reading orderflow at the
level may have an edge these bars cannot see — this measures what the *rules* are worth without
that read, which is a floor, not a ceiling.

---

## 2026-08-28 — Key-level families backtested: gross zero, killed by the stop floor

Measured with `signal_engine/backtest/strategies/key_level.py` over 208 F&O names, 59
sessions of 5-minute bars. PD and IB families only — the VA family needs previous-session
volume-profile reconstruction from 1-minute bars and yfinance caps 1-minute history at 7
days, so `PVAH`/`PPOC`/`PVAL` remain untested.

| | n | win | gross_bps | net_R | t |
|---|---|---|---|---|---|
| shipped [IS] | 1008 | 38.2% | -1.89 | -0.366 | -8.85 |
| shipped [OOS] | 488 | 37.5% | -2.30 | -0.399 | -6.91 |
| at **0 bps cost** | 1496 | 38.2% | -2.02 | -0.059 | -1.77 |

Gross is indistinguishable from zero. What kills it is the stop: **median stop distance is
0.30% of price**, which is exactly the `minDist = math.max(atrVal * 0.5, ref * 0.003)` floor
in `klExecMap` binding on nearly every trade. At a 0.30% stop a 10 bps round trip costs
**0.33R per trade**; the ORB path at 0.72% pays 0.14R. The `klMaxChaseATR` cap keeps entry
within 1.0 ATR of the level, so the level-based stop is almost always tighter than the floor
— the floor governs, not the level, inverting the stated intent ("the level still governs
whenever it is already wide enough"). 44 of 207 symbols profitable.

### Found: the `-BRK` branch is unreachable

Zero of 1496 shipped trades were breaks. All four families fired only `-RT`.

`klTrackBreaks` sets `pdhBB := bar_index` on the break bar and runs at line 3494 — *before*
`klResolveSetup` at 3496. So on the break bar `bar_index - pdhBB == 0 <= klRetestMaxBars`,
and because the retest branch is checked first in the ladder, a bar crossing up through a
level satisfies `low <= pdh + band and close > pdh and bull` and is labelled `PDH-RT`.

Two consequences: `PDH-BRK` / `PDL-BRK` / `IBH-BRK` / `IBL-BRK` are dead code, and every
signal collects the `+2` "break-and-hold structure" retest bonus on a bar that has held
nothing — 2 points of a 7-point threshold.

Forcing one bar of separation makes breaks appear (529 of 1024 trades) and lifts gross from
-2.02 to -0.54 bps, but that is IS-only (IS +0.52, OOS -2.75), so it is not claimed as an
improvement. The bug is real regardless. **Not patched** — it changes which setups fire.

### Doc corrections

- The design table at the top still said "Alert-only by default". `enableKeyLevelExecution`
  has defaulted to **true** since 2026-08-22; annotated in place.
- Both blockers the changelog flagged as "must be fixed before execution is enabled" WERE
  fixed: `canTakeKeyLevelEntry` (line 2651) no longer consumes `orbRangeFilterPassed`, and
  `klArmedSL`/`klArmedT1` are latched (3626) and consumed (2701, 2822).
- `breakout.pine`'s ORB path already used the time-of-day volume baseline via `klVolFactor`;
  `orb.pine` did not, and has now been given `volBaselineMode`.
- The R:R fix (`tp1MinRR`, `useOrbStopFloor`) landed in both files. `breakout.pine` hardcodes
  `enableORB15Signals = true` after the compile-budget freeze, so the ORB60 default flip
  applied to `orb.pine` only.

---

## 2026-08-20 — Key-level merge (VP / PDH-PDL / IB)

Merged the volume-profile decision-assist logic into the ORB strategy. The premise: ORB
is a key-level breakout, and so are Value Area, Previous Day and Initial Balance breaks.
They share entry mechanics, so they should share one script.

### Design decisions

| Decision | Choice | Why |
|---|---|---|
| Merge mode | ORB **plus** new VA/PDH/IB triggers, each toggleable | The full key-level reading of the strategy; ORB logic untouched |
| Execution posture | ~~**Alert-only by default** (`enableKeyLevelExecution = false`)~~ **Superseded — now defaults to `true`, see 2026-08-22** | Observe and validate before risking capital on unproven setups |
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
`trade-analysis/SIGNAL-PERFORMANCE-2026-Q1.md`).

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
- the earlier 20 -> 50 MA change, recorded in `trade-analysis/SIGNAL-PERFORMANCE-2026-Q1.md:183`

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

---

## 2026-08-22n — Direction bug, day/open type, break-bar volume, chase guard

### BUG: every key-level LONG rendered as SHORT

`isBullish = everHadBreakUp` — an **ORB-only** flag, set solely inside the ORB breakout block.
With ORB demoted to a level it never sets, so the panel labelled a long trade SHORT and the
header arrow pointed the wrong way.

**Execution was never affected.** `orbTradeDirection` is set correctly by both pending
processors and is what TP/SL tracking and the exit alerts actually read. The damage was confined
to `isBullish` (dashboard label + trade-line drawing), `hasActiveBreakout` and the header arrow —
all now read `orbTradeDirection`.

Worth noting *why* this appeared only now: it was latent for as long as ORB was the only thing
that traded. Demoting ORB exposed it. Anything else keying off `everHadBreakUp` /
`everHadBreakDown` for "which way is this trade" is suspect for the same reason; the three genuine
"did an ORB break happen" uses (`isFailedBreak`, `brokeOutLate`, `hasValidOrbData`) were left alone.

### Open type, day type, bias

Three new rows, in the cheat sheet's own categories.

**`klOpenType()`** — classified over the first 30 minutes then **latched**. Measures excursion
each way from the open against the net move:

| Counter-move | Type | Expect |
|---|---|---|
| < 20% of total range | Open Drive | Trend day — enter first pullback with the drive |
| < 45% | Open Test Drive | Normal variation — trade the reversal |
| net move still > 35% | Open Rejection Reverse | Normal day — trade the gap fill toward POC |
| otherwise | Open Auction | No edge yet; wait for IB |

**`klDayType()`** — tracks extension beyond IBH/IBL as a % of the IB range, and names the day from
that plus IB width. **Provisional and updates all day** — a day only truly types at the close.

Both sides extended >15% → Neutral. One side >100% → Trend if IB narrow, else Double Distribution.
>50% → Normal Variation. <10% → Non-Trend if narrow, else Normal.

**`klBias()`** — the line the other two exist to produce: `STAND ASIDE` / `TWO-WAY — fade the
extremes` / `LONG — hold, do not fade` / `SHORT`. **Directional context, not a signal.** A LONG
bias with no setup is not a trade; the Setup row remains the only thing that fires.

### Retests are now scored on the break bar's volume

Your read of the chart was right: **the 10:20 break bar carried the volume, the 10:50 confirming
bar did not.** Scoring only the confirming bar systematically under-rates the `-RT` family — which
this model rates highest — because by the time price pulls back and closes back through the level,
participation has dried up by construction.

Retest setups now score on `max(current RVOL, highest RVOL over the retest window)`. Rejection and
acceptance are unchanged: for those the trigger bar *is* the event.

### Chase guard

`klMaxChaseATR` (1.0). Refuses a setup whose confirming bar has already closed more than one ATR
beyond its level. A retest is meant to be entered near the level it defended; if confirmation only
arrives after price has run a full ATR, the good part of the move is gone and the stop — still
measured from the level — is now far away, so R:R collapses quietly.

This does not improve the fill. It declines the ones that are already bad.

### The entry-timing problem this does NOT solve

The TCS trade entered at 2296 when IBH was 2292 — four points, most of them given up to the HTF
confirmation wait. The chase guard would have allowed it (4 points ≈ 0.8 ATR), correctly: it was a
mediocre fill, not a bad one.

Genuinely fixing the fill needs a **limit order at the level**, which is a `signal_engine` change,
not a Pine one. Worth noting the earlier "MARKET only" finding does **not** apply here: that was
about entering ON a breakout, where a limit only fills if price comes back — i.e. if the break
failed. A **retest** setup is the opposite case: the pullback *is* the thesis, so a resting limit
at level + buffer is exactly the right instrument. Options, in order of cost:

1. Alert carries a `LIMIT` price at the level; `signal_engine` places a limit with a timeout.
2. Lower `klConfirmTF` to 10 — less lag, weaker sweep filter.
3. Lower the strong-volume HTF bypass so genuine break volume skips the wait (that TCS break was
   0.9x, so it would not have qualified anyway).

---

## 2026-08-22o — PM window never opened: two independent causes

Charts: RELIANCE, AXISBANK, HDFCBANK, TCS (all 5-min, 2026-08-22 19:49-19:51). New rows all
render correctly — Open type, Day type, Bias, both IB denominators, headroom in R.

### Cause 1 — a per-bar threshold applied to a cumulative session measure

`inPMWindow` required `cachedSessionVF >= volumeMultiplier`, i.e. **1.2x**. But session VF is
*cumulative for the day*, not per-bar, and lives on a different scale — the cheat sheet's own
bands put **0.8-1.2 at "normal"**. Requiring 1.2 demanded a genuinely heavy DAY.

The four charts settle it: session VF of **0.47 / 0.70 / 0.76 / 0.98**. Not one reached 1.2, so
the window could not open on any of them. My error, and an obvious one in hindsight: a threshold
calibrated for one measure was pasted onto a different one.

New `pmMinSessionVF` input, default **0.8** — skip dead tape, not normal tape. The per-bar volume
filter still applies at the trigger, so this is a floor on the day, not a substitute for
confirmation.

### Cause 2 — both windows shared one entry slot

`canTakeEntry` and `canTakeKeyLevelEntry` both tested `not sessionEntryTaken`, a single
day-level flag. So on any day the morning filled, the afternoon window opened onto **no available
slot** — a "second entry window" structurally incapable of taking a second entry.

Split into `amEntryTaken` / `pmEntryTaken`, with `slotAvailable` resolving against whichever
window price is currently in. The day can now produce **at most one morning and one afternoon
entry**, never two of either. The verdict distinguishes them: a spent morning slot now reads
`morning entry taken — afternoon window reopens at 13:00` rather than a flat `DONE`.

### What the four charts say about setup quality

| Symbol | Setup | Score | Session VF | Day type | ADR used | Fired |
|---|---|---|---|---|---|---|
| HDFCBANK | SHORT PDL-RT | **10/7 +1lvl** | 0.98x normal | Normal | 81% | yes |
| TCS | LONG IBH-RT | **8/7 +1lvl** | 0.70x thin | — | 75% | yes |
| AXISBANK | SHORT VAL-RT | 5/7 | 0.76x thin | Normal Variation | 85% | no |
| RELIANCE | LONG VAL-REJ | 5/7 | 0.47x dead | Non-Trend | 32% | no |

Both setups that cleared the threshold were **retests carrying confluence**. Both that failed
landed at exactly 5/7 with **no confluence**. That is consistent with the model's own priority
ordering, but it is four charts — it identifies which *components* discriminate, and says nothing
statistically about whether IB beats PD beats VA. Ranking the level families needs a Strategy
Tester run with one trigger family enabled at a time.

### The finding that matters more than any of the above

**Every one of these four symbols was running below normal participation** — 0.47x to 0.98x, not
one above 1.0. Zarattini/Barbon/Aziz found that selecting the day's highest relative-volume names
did almost all the work in ORB, and nothing about that conclusion is ORB-specific.

Running this engine over a fixed mega-cap watchlist is fighting the edge rather than using it. The
highest-value improvement available is **not in this script** — it is a pre-market screener that
picks the day's top relative-volume symbols and points the engine at those.

### Cosmetic inconsistency, not fixed

RELIANCE shows `IB 52%IB/32%ADR narrow/trend?` while Day type reads `Non-Trend`. Not a
contradiction — IB width states the *potential*, day type reports that the potential did not
materialise (no extension) — but the two read as if they disagree. The `?` suffixes on the IB row
are now redundant given the Day type row names the day outright.

---

## 2026-08-22p — SL fired after a booked target; static/live markers

### BUG: the stop stayed armed at its ORIGINAL price after a target was booked

Traced from the TCS panel showing both `✅ TP1` and a hit SL.

The SL check sits inside `if not orbLinesFrozen ...`, and `orbLinesFrozen` is only set when
`lastTPHit` — the **last enabled** target. All four TP toggles are on, so that means **TP3**.
A trade that books TP1 and never reaches TP3 therefore keeps its original stop armed for the
rest of the session, and a later retrace to that price sets `orbSLHit` **and fires an SL alert**.

That alert reaches `signal_engine` as a live `SL HIT`. Either:
- TP1 closed the position outright (backtest model) — the alert refers to nothing; or
- TP1 was a 50% partial (live model) — `signal_engine` has already moved the stop to
  `TP1 − 0.3R`, so the original price is **below** where protection actually sits.

Both ways the alert is wrong. The phantom-exit guard on the Python side would likely catch it,
but the script should not be sending it.

Fixed: the SL setter is now gated on `not anyTPBooked` in both direction blocks.

**Also fixed:** the close-reason chain tested `if orbSLHit` *before* the targets, so a trade that
took TP1 and later stopped out reported a flat **-1R** — understating results and corrupting the
R accounting. It now requires that nothing was booked first.

**Known limitation, unchanged:** when one bar touches both a target and the stop, resolution stays
**optimistic** — the target is evaluated first in source order, so it wins. Intrabar order cannot
be recovered from a 5-min bar. The file already pulls 1-min arrays via `request.security_lower_tf`
for the volume profile, so a conservative resolution is available if this ever matters; it is not
implemented.

### Static vs live, marked on every row

Row labels now carry the convention, with the legend in the `── Key Levels` header tooltip:

| Marker | Meaning | Rows |
|---|---|---|
| **·** | Fixed once set, does not move again today | VAH/POC/VAL, PDH/PDL, IB H/L (after 10:15), Open type (after it latches) |
| **~** | Recalculated every bar | Vol Factor, Auction, Day type, Bias, ADR/used, ATR/SL, Setup, Confluence |

A `·` value read at 14:00 means what it meant at 10:30; a `~` value is only true as of this bar.

### Why open type is not a single-candle read

Asked whether open type and day type should come from the 09:15 candle alone. They should not,
and the reason is structural rather than a preference:

**The four opening types differ in what happens AFTER the first move.** Open Drive and Open
Rejection Reverse both begin with a directional push — they only diverge once you see whether it
held or reversed back through the open. One candle gives you *direction*; it cannot give you
*type*. Market Profile classifies on period A, the first 30 minutes, which is what the 6-bar
default already does, and it **latches** there.

`klOpenTypeBars` is now an input so it can be set to 1, accepting that Drive / Test Drive /
Rejection Reverse collapse into one bucket at that setting.

**Day type genuinely is live and should stay that way** — a day reveals its character through
extension beyond the IB, and freezing it at 09:20 would mean naming a Trend day before any trend
existed. That is why it carries `~` while Open type carries `·`.

### Also

- `klMinHeadroomR` 1.0 -> **1.5**. Three of the four reviewed charts produced setups at 75-85% of
  ADR already consumed.
- Dropped the `?` from `narrow/trend?` and `wide/range?`. The Day type row names the day outright
  now, so the IB row editorialising a guess alongside it read as a contradiction.

---

## 2026-08-22q — Eight-symbol validation: one real bug, one veto added

Charts: FEDERALBNK, INDUSTOWER, JSWENERGY, ABCAPITAL, ITC, COALINDIA, GAIL, BANDHANBNK
(5-min, 2026-08-22 20:34-20:37). All carry the `~` / `·` markers and the new slot verdict, so
they include every fix through `a5375bcf`.

### Confirmed working

| Fix | Evidence |
|---|---|
| Direction bug | ABCAPITAL: `Position Size: SHORT` on a short (entry 409.85, TP1 408.03 below). Previously would have read LONG |
| PM window verdict | ABCAPITAL: `SLOT USED — morning entry taken, afternoon window reopens at 13:00` |
| Static/live markers | Every panel |
| Stop floor | ABCAPITAL stop 410.88 vs signal-bar close ≈409.65 → 1.23 = exactly `0.3% × price`. FEDERALBNK identical |
| Open/day type/bias | All four categories observed across the set: Open Drive, Test Drive, Rejection Reverse, Auction; Normal Variation and Double Distribution |

### BUG: the ORB-width fix was only half applied

**JSWENERGY: `SHORT PDL-RT 8/7 at 09:50` — cleared every gate, and no trade was taken.**

`canTakeKeyLevelEntry` (without `orbRangeFilterPassed`) governed **arming** at line 3494. But the
**fill** gates at 2597/2713 and the **cancel** gates at 2831/2837 still read
`canTakeLongEntry` / `canTakeShortEntry`, which are `canTakeEntry` — carrying
`orbRangeFilterPassed`.

So a key-level setup passed its own gate, armed a pending, and was then **silently dropped on the
next bar by the ORB WIDTH filter**. The observation alert had already gone out and the arrow was
already drawn, which is why this looked like a working signal that simply never traded.

Fixed with `canFillLong` / `canFillShort`, which select the gate by the pending's own source
(`klPendingSource != ""` marks a key-level pending and survives from arming bar to fill bar).

**Lesson for this file: a gate change has to follow the pending through arm -> fill -> cancel.**
Three call sites, not one.

### Counter-bias veto

**ABCAPITAL took `SHORT VAH-REJ 8/7 +1lvl` while its own Bias row read `LONG — hold, do not
fade`. It stopped out for -1R.** The bias was computed, displayed, and completely ignored.

`klBlockCounterBias` (default on) vetoes a setup that opposes the day's established direction —
but **only on `Trend` and `Double Distribution` days**, where the cheat sheet's Avoid List opens
with "Never fade a Trend Day" and a Double Distribution is a trend that relocated.

Deliberately **not** applied to Normal / Normal Variation / Neutral: on those the extremes hold
and fading them *is* the play, so a blanket veto would remove the correct trade. ABCAPITAL was
Normal Variation, so this veto would **not** have saved it — stated plainly rather than claimed.

### Observations not acted on

**The 0.3% stop floor binds on essentially every trade.** ATR on these names is 0.52-0.87 on
₹350-550 prices — roughly 0.15-0.20% — so `klSlBufferMult × ATR` (0.35 × ATR ≈ 0.06%) is far
inside the floor. Every stop observed landed at 0.3%.

The stop is therefore **not level-based in practice**; it is a volatility-independent percentage.
That is defensible — 0.3% is about 1.7 ATR here, a reasonable stop — but it should be a decision,
not a side effect. Either accept it, or raise `klSlBufferMult` so structure binds more often.

**`ADR/used` readings of 86% / 102% / 114% are END-OF-DAY figures, not entry-time.** The row is
`~` (live). INDUSTOWER entered at 13:05 with roughly 2.0R of headroom by the projection; the 102%
accumulated afterwards. The headroom gate was working — worth stating because the raw numbers
look alarming.

**Every fired setup scored exactly 8/7.** Across four inspected symbols nothing scored 9+, and
nothing between 5 and 7 fired. The threshold is the binding constraint, and the score distribution
is narrow — which means it discriminates less than the component count suggests.

---

## 2026-08-22r — Renamed to intraday-breakout; morning-only; alert tags

### Strategy renamed

`strategy("intraday-orb")` -> **`strategy("intraday-breakout")`**.

### Alert tags — all three types, not just the entry

| Alert | Was | Now |
|---|---|---|
| Entry | `🟢 ORB LONG` | `🟢 BREAKOUT LONG` |
| TP hit | `✅ ORB TP1 HIT` | `✅ BREAKOUT TP1 HIT` |
| SL hit | `❌ SL HIT` *(no tag)* | `❌ BREAKOUT SL HIT` |
| Test | `🧪 TEST ALERT - ORB Strategy` | `🧪 TEST ALERT - BREAKOUT Strategy` |

**The SL alert carried no strategy tag at all.** `normalizer.py` falls back to
`_DEFAULT_STRATEGY` (= `ORB`) when a prefix is absent, so tagging only the entry would have
registered positions under `BREAKOUT` while routing their exits to `ORB`. All three now carry it.

`⏰ TIME EXIT` is deliberately left untagged: `normalizer.py` has no regex for it, so it is a
Telegram notification only. `signal_engine` runs its own time-exit scheduler (`main.py`).

Both normalizer regexes already accept an optional `[\w-]+` prefix, so **no parser change is
needed** — verified against `_TP_HIT_RE` and `_SL_HIT_RE`.

### REQUIRED signal_engine work — NOT done in this change

`signal_engine` must serve **both** `ORB` (from the frozen `orb.pine`) and `BREAKOUT`. These are
**additive**; nothing existing should be modified.

1. **`signal_engine/strategies.py`** — add alongside the existing constants:
   `BREAKOUT = "BREAKOUT"`
2. **`signal_engine/config.yaml`** — add a `BREAKOUT` key under **both** `blacklist:` and
   `strategy_profiles:`, mirroring the `ORB` shape: `hard: []` / `soft: []` /
   `soft_multiplier: 0.5`, and `product: MIS` with **no** `tp_levels` (the script sends
   `ExitQtyPct` per TP HIT, exactly as ORB does).
3. **`normalizer.py`** — leave `_DEFAULT_STRATEGY = ORB`. It is the fallback for *untagged*
   alerts, which now means legacy `orb.pine` output only.

**Unverified:** whether a missing `strategy_profiles.BREAKOUT` is tolerated or fatal.
`config.py` reads it via `yml.get("strategy_profiles", {})` with upper-cased keys, so absence
*probably* falls through to `broker.product` (MIS) — but this was not tested. Add the key rather
than rely on it.

**Until that config exists, do not run this script live.** A `BREAKOUT`-tagged alert reaching an
engine that only knows `ORB` is the failure mode with real money attached.

### Morning-only

`enableAfternoonWindow` default **true -> false**. The window and its four inputs stay fully
implemented and can be switched back on.

**This is not a claim that the afternoon is worse.** There is no evidence either way — four
inspected charts and roughly four trades is nothing, and the model doc endorses **both** windows
(§4E: 09:15-11:00 *or* 13:00-14:45). The reasoning is risk surface:

- Going live means every additional window is another set of behaviours to validate, and the
  afternoon path has never produced an observed trade.
- The structural argument favours the morning: the Initial Balance thesis *is* a morning thesis,
  prior-day levels are freshest before price has interacted with them, and a 15:00 time exit
  leaves an afternoon entry far less runway.
- Every setup observed across all twelve charts fired between **09:50 and 13:05**.

Turn it back on once the morning window has a track record worth comparing against.

### Activating the alert in TradingView

Script-side alerts are already enabled (`enableAlerts = true`). Nothing else to switch on.

1. Add `intraday-breakout` to the chart, 5-min, NSE symbol.
2. Create Alert -> Condition: **the script itself**, and choose **"Any alert() function call"** —
   that is the surface carrying entry / TP HIT / SL HIT. The named `alertcondition()` entries are
   a separate, optional surface.
3. Trigger: **Once Per Bar Close**. Expiry: open-ended.
4. Notifications -> **Webhook URL** -> the Telegram webhook, same as the ORB alerts.
5. Message: leave **empty** — the script emits the full Telegram JSON payload
   (`{"chat_id": ..., "text": ..., "parse_mode": "Markdown"}`) itself. Anything typed in that box
   replaces it and breaks the format.
6. Set `telegram_chat_id` in the script inputs to the destination channel.

---

## 2026-08-22s — The HTF gate had made -BRK setups unfireable

Raised from a chart observation: the breakout candle consistently carries more volume than the
retest candle. That is correct, it is structural rather than coincidental, and chasing it down
exposed a design defect.

### Every observed setup was a retest — and not because of the priority ordering

Across twelve charts every setup was `-RT` or `-REJ`. Not one `-BRK`. The engine *has* break
setups (`pdhBrk`, `pdlBrk`, `ibhBrk`, `iblBrk`), so the assumption was that retest priority was
simply winning. It was not.

```pine
ibhBrk  = close > ibh and close[1] <= ibh     // FIRST bar closing beyond the level
klHTFOK = ... klHTFClose > klSetupLevel        // last CLOSED 15m bar ALREADY beyond it
```

On the bar that first closes beyond a level, the last closed higher-timeframe bar is — **by
construction** — still on the old side. The two conditions are near mutually exclusive.

**The HTF gate silently converted this into a retest-only engine.** The only escape was
`klRVOL >= strongVolumeMultiplier` (1.8), and observed break RVOLs were 0.9x / 1.1x / 1.4x.

### Fix: breaks qualify on break-bar volume, not elapsed time

`klBreakMinRvol` (default 1.5). A `-BRK` now fires immediately when its break bar carries the
volume, bypassing the HTF wait entirely.

The reasoning is that **HTF confirmation is the wrong filter for a break.** It defends against
sweeps by waiting, and waiting is precisely what destroys a breakout entry. For a break the
discriminating filter is volume *at* the level, on the candle doing the breaking — which is what
the false-breakout literature says, and what the chart observation independently found.

Acceptance keeps the HTF requirement: its claim is that price is *holding* beyond value, and
waiting is the right way to test that. Retests are unaffected — the HTF has caught up by then.

### Honest expectation

**More signals, probably a lower win rate, and a higher average win.**

Requiring a retest systematically selects *against* the strongest breakouts: a genuine trend-day
break runs and never looks back, so a retest filter only fills on breaks that weakened enough to
return. That is a biased sample which excludes the fat tail where the large R multiples are.

Whether the trade-off is net positive is **an empirical question this change does not answer.**
It needs a Strategy Tester comparison with `klBreakMinRvol` at 0 (retest-only, the old behaviour)
versus 1.5. Setting it to 0 restores the previous behaviour exactly.

### On the fill-quality claim, more carefully

The TCS case gave up ~4 points entering at 2296 against IBH 2292. But measuring the others:

| Symbol | Level | Entry | Gap |
|---|---|---|---|
| FEDERALBNK | IB-H 358.65 | 358.95 | 0.30 (0.08%) |
| INDUSTOWER | IB-H 374.95 | 375.55 | 0.60 (0.16%) |

Those are tight fills, not chases. **TCS was the outlier, not the rule**, and the chase guard
added earlier already declines the genuinely bad ones. So the case for break entry rests on the
*selection bias* argument above, not on fill quality — which is a weaker and more honest basis
than "we are leaving points on the table."

---

## 2026-08-22t — Opposite-direction alerts suppressed; label showed the wrong volume

### FEDERALBNK: a LONG position with a SHORT alert behind it

The session held a LONG (`IBH-RT`) while the Setup row read `SHORT VAL-RT 8/7 11:10`, and a red
short arrow sits on the chart beside the long entry.

**No second trade was ever placed** — `slotAvailable` had already been consumed by the long, so
the short could not enter. But `klFire` did not know that: it fired the observation alert and drew
the arrow regardless.

With execution ON that is worse than untidy. A SHORT packet arrives in Telegram while a LONG is
open — noise at best, a contradictory instruction at worst.

`klFireGate` now takes `tradeable`, wired to `not enableKeyLevelExecution or canTakeKeyLevelEntry`.
Once the slot is spent no further setup fires, so opposite-direction alerts within a session are
gone. OBSERVE mode is deliberately left open so the engine can still be watched with execution off.

Note this also answers "first breakout must be preferred": the slot is first-come-first-served and
always was. What changed is that the losers of that race now stay quiet.

### The signal label was showing the wrong volume number

`klDrawSignal` printed `klSessionVF` — the **cumulative session** factor — not `klRVOL`, the
**bar** volume that actually gates the trigger. So `VAL-RT 1.4x` meant "the session is running
1.4x normal", *not* "this bar broke on 1.4x volume".

That made the break-volume question undiagnosable from a chart, for both of us. Label is now
`CODE  1.8x /0.7s` — **bar RVOL first**, session factor suffixed `s`.

### Why breaks may still not fire, honestly

Two independent reasons, beyond needing the script re-applied in TradingView:

1. **`klBreakMinRvol` = 1.5 on the break bar.** Unknown whether the observed breaks cleared it —
   see above, the labels were showing the wrong number. The corrected label will settle it.
2. **Breaks score ~2 points below retests by design.** The `+2` breakout-and-retest bonus is
   intentional, but against `klScoreThreshold = 7` it bites: a break with no confluence needs
   VWAP *and* EMA *and* strong RVOL *and* CLV *and* session VF all aligned to reach 8. Reachable,
   but demanding.

If breaks still do not appear once the corrected labels are visible, the lever is
`klScoreThreshold`, not `klBreakMinRvol` — and lowering a global threshold to admit one family is
a blunt instrument. A per-family threshold would be the cleaner fix, and is not implemented.

---

## 2026-08-22u — TCS re-check: the corrected label settles it, and it was not break-vs-retest

Chart `charts/TCS_2026-08-22_21-41-23_f20f5.png`, script renamed and re-applied
(`intraday-breakout` in the chart legend).

### Everything from the last three commits verified on one chart

| Change | Evidence |
|---|---|
| Stop floor | Stop **2289.11**, was 2291.23. 2296 − 2289.11 = 6.89 = **exactly 0.3%** |
| Size follows the wider stop | Max Shares **14**, was 20. Predicted "20 → ~13" |
| R:R cost of the floor | Risk ₹96, Reward ₹112 = **1.16R**. Predicted "1.67 → ~1.16" |
| `klEntrySource` | `Position Size: LONG · IBH-RT` — names the setup that opened the trade |
| SL-after-TP fix | `✅ TP1: 2303.98` booked, and the SL label carries **no ❌** despite price returning to 2288-2292. Previously this printed both |
| Retest window RVOL | Score **9/7**, was 8/7 — the window max supplied the volume points the trigger bar could not |

### The label correction answers the question, and the answer is neither option

**`IBH-RT 0.5x /0.9s`** — the retest bar traded on **half** its normal time-of-day volume.

The old label was showing the session factor, so the earlier reading of "1.4x" was wrong, and so
was the inference drawn from it. With the real number visible:

- `klBreakMinRvol` is 1.5. A break bar on this session would have had to be **3x** the volume of
  the bar that actually triggered. On a 0.70x session that is unlikely.
- So **entering on the break would not have fired either.** The break-vs-retest framing was not
  the binding constraint on this trade.

**The binding constraint is that TCS should not have been traded at all that day.** Session
factor 0.70x — "thin". The engine displayed that prominently and then ignored it, because the
session reading gated only the afternoon window.

### Session participation floor

`klMinSessionVF` (default **0.8**) now gates **every** entry. The verdict names it:
`⛔ NO TRADE — session volume 0.70x, stock is not in play today`.

This is the Zarattini/Barbon/Aziz finding applied where it belongs. That study found selection by
relative volume did almost all the work in breakout trading, and the pattern far less. Every
marginal or losing trade reviewed across twelve charts came from a session at **0.47x-0.98x**.

**Stated plainly: this filter cannot substitute for symbol selection.** It stops you trading a
dead name. It cannot find you a live one. That still needs a pre-market relative-volume screen
choosing which symbols the engine watches, which remains the largest single improvement available
and is outside this script.

### Also fixed

The `SLOT USED` verdict still read "afternoon window reopens at 13:00" after the PM window was
disabled by default — promising a window that no longer exists. Now reads "no second window
today" when `enableAfternoonWindow` is off.

---

## 2026-08-22v — FEDERALBNK / ABCAPITAL on live sessions: the premise was session-specific

Both symbols re-pulled with the current script, chosen because they were the only two of twelve
with a session factor above 1.0.

### ABCAPITAL: the same day, a different trade, opposite outcome

| | 20:36 chart | 22:08 chart |
|---|---|---|
| Setup | `SHORT VAH-REJ 8/7 +1lvl` **10:05** | `LONG VAH-RT 10/7 +1lvl` **09:55** |
| Result | **SL hit, -1R, WR 0%** | **✅ TP1 411.98 · ✅ TP1.5 412.75** |

The mechanism is traceable and is not luck:

1. Retests now score on `max(bar RVOL, window RVOL)`. The 09:55 `VAH-RT` had a modest trigger bar
   but a strong window, so it moved from below threshold to **10/7** and became eligible.
2. Being ten minutes earlier, it took the session slot.
3. The new `tradeable` gate then suppressed the 10:05 `VAH-REJ` — the trade that previously took
   the slot and lost.

The Bias row read `LONG — hold, do not fade` in both captures. Previously the engine traded
against it; now it does not. **Worth being precise: the counter-bias veto did not cause this.**
That veto only applies to Trend and Double Distribution days, and this was Normal Variation. The
improvement came from the scoring change plus the slot gate.

### The break-versus-retest premise, tested on live sessions

| Symbol | Setup | Bar RVOL | Level | Entry | Gap |
|---|---|---|---|---|---|
| FEDERALBNK | `IBH-RT` **11/7** | **4.1x** | IB-H 358.65 | 358.95 | 0.30 = **0.08%** |
| ABCAPITAL | `VAH-RT` **10/7** | ~1.5x | VAH 410.15 | 410.45 | 0.30 = **0.07%** |

**On sessions with real participation the retest entry arrives essentially AT the level, on high
bar volume.** FEDERALBNK triggered on a 4.1x bar — four times its normal time-of-day volume — and
filled 8 basis points above the level.

So the premise behind "we should enter on the break instead" was **true on TCS and false on both
of these**. TCS was a 0.70x dead session where the trigger bar itself ran 0.5x. These are 1.14x
and 1.16x sessions, and there were no points left on the table to recover.

That reinforces the session participation floor as the actual fix, and demotes break-versus-retest
to a second-order question.

### -BRK still has not fired

Both entries were retests. The remaining argument for break entries is the **selection-bias** one
— a trend day that never looks back is invisible to a retest filter — and neither of these days
was a trend day (both Normal Variation). So this set neither supports nor refutes it.

Given fills are landing within 0.08% of the level on live sessions, the practical case for `-BRK`
is weaker than it looked. The honest test is a Strategy Tester run with `klBreakMinRvol` at 0
versus 1.5 over a period containing genuine trend days.

### Everything else confirmed

- `SLOT USED — morning entry taken, no second window today` (PM window disabled, verdict correct)
- `Position Size: LONG · VAH-RT` / `LONG · IBH-RT`
- ABCAPITAL stop 408.92 = **0.373%**, wider than the 0.3% floor — so this one is genuinely
  level-based (VAH 410.15 − 0.35 × ATR), not floored. The floor binds on tight levels, not all
- ABCAPITAL TP1 = exactly 1.0R: no structural level sat far enough above entry, so `klCalcTargets`
  fell back to the risk multiple, as designed
- Both TP labels carry ✅ and neither SL label carries ❌ — the SL-after-TP gate holding

## 2026-08-26 — Every level named; previous-day VP levels get a P prefix

Two chart-review findings, both about identification rather than logic.

### Unlabelled lines

`klDrawLevel` dropped the TAG for any level further than `klTagMaxATR` (6) x ATR from close
but kept its LINE, so the chart carried horizontal lines the trader could not name. A line you
cannot identify is worse than no line — it reads as a level without saying which.

First attempt made suppression symmetric: a distant level we draw ourselves is dropped entirely
rather than left anonymous, and levels whose line comes from elsewhere (`drawLine=false`, the ORB
plots) are always tagged because that line cannot be removed. That fixed the anonymous lines but
created a worse problem — **PDH, 33 points from close on a mid-range day, disappeared**, and PDH
is exactly the level a trend day runs at.

Settled on `klTagMaxATR = 0.0`: every level is drawn AND tagged. The collision stagger already in
`klDrawLevel` (tags pushed right in `KL_TAG_STEP_BARS` increments until clear) was always the
real answer to stacked tags; the distance cutoff was solving a problem that was already solved.
The symmetric-suppression code is kept, so if a distance limit is ever re-enabled it hides rather
than orphans.

### PVAH / PPOC / PVAL

`VAH`/`POC`/`VAL` renamed to `PVAH`/`PPOC`/`PVAL`. These are the **previous** session's value
area, but TradingView's Session Volume Profile plots the **current** day's VAH/POC/VAL — two sets
of lines carrying the same three names at different prices, both on the chart at once.

The prefix also makes the scheme self-consistent, which it was not:

| Previous session | Today |
| --- | --- |
| `PVAH` `PPOC` `PVAL` `PDH` `PDL` | `ORH` `ORM` `ORL` `IBH` `IBM` `IBL` |

`PDH`/`PDL` already carried the P. Now P always means previous-session, with no exceptions.

Display only — three call sites in `klDrawLevel`, the `klLvlNames` array, the confluence-name
pushes and one dashboard row label. `klLvlNames` feeds a human-readable KEYLEVEL packet that goes
to a channel the signal engine does not listen on, and `parser.py` never reads level names, so the
trade pipeline is untouched. Setup codes (`VAH-ACC`, `VAL-REJ`) were deliberately left alone —
different namespace, real blast radius.

### Not a finding: the auto profile is fine

An earlier review claimed `klAutoProfile` was ~12 points off TradingView's SVP. That was wrong:
it compared breakout's **previous-day** levels against SVP's **current-day** levels. Two different
sessions. On a like-for-like read PPOC and PVAL match and PVAH is off by ~2 points, which is the
expected signature of a small `ticksPerRow` mismatch — POC is the modal bin and stable, while
VAH/VAL are the 70% boundary edges and land mid-bin. Tune ticks-per-row if exactness matters;
switching to manual VP is not warranted.
