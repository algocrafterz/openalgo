---
name: pinescript-strategy
description: Build, review or debug a tradeable PineScript strategy for OpenAlgo's signal_engine pipeline. Use whenever writing or modifying a .pine file under signal_engine/pinescripts/, wiring TradingView alerts to Telegram, registering a new strategy tag, backtesting a strategy before it goes live, or diagnosing why a signal never reached the broker. Also use for Pine v6 compile errors and repainting problems.
---

# Tradeable PineScript strategies for OpenAlgo

A strategy is not done when the chart looks right. It is done when a signal travels
the whole path to a broker order and back:

```
ema9-intraday.pine            alert() fires on a CLOSED bar
   -> TradingView alert       condition "Any alert() function call", webhook to Telegram
   -> Telegram channel        one message per event
   -> signal_engine listener  normalizer -> parser -> validator -> risk -> executor
   -> OpenAlgo /api/v1        placeorder / placesmartorder
   -> broker
```

Every link is a place a signal dies silently. Most of this skill is about those links.

Reference implementation: `signal_engine/pinescripts/intraday/orb/breakout.pine` is the
most production-hardened script in the repo. `signal_engine/pinescripts/intraday/ema9/`
is the smallest complete one and is easier to read end to end.

---

## 1. What every tradeable strategy must have

Non-negotiable, because the engine assumes all of it:

| Requirement | Why |
| --- | --- |
| A **strategy tag** (`ORB`, `BREAKOUT`, `EMA9`, `RSI-TP-MR`) on EVERY alert | The tag routes entries and exits to the same position. An untagged exit falls back to the default strategy and reconciles the wrong trade. |
| **Entry, stop and target computed on the SIGNAL bar** | The alert must advertise the same levels the chart drew. Recomputing later desynchronises the engine's R accounting. |
| **Signals only on `barstate.isconfirmed`** | An intrabar signal repaints. The engine cannot un-place an order. |
| A **stop that is always on the correct side** of entry | The validator rejects the signal, but a wrong-side stop usually means a logic bug upstream. |
| A stop no tighter than the strategy's **`min_sl_pct`** | The validator rejects tighter stops outright. Calibrate the config entry to the script's real stop geometry. |
| An **exit path for every way a trade can end** | TP, SL, time exit, and any strategy-specific exit. A missing exit alert leaves the engine holding a position the chart thinks is closed. |
| A **time exit** matching `config.yaml time_exit` | The engine runs its own clock. If the two disagree, whichever fires first wins and the logs stop making sense. |
| **Observation traffic that cannot parse** | Notes about levels reached after a position closed must never look like a trade. |

---

## 2. The alert contract

Alerts are Telegram `sendMessage` JSON. `signal_engine/normalizer.py` strips decoration
then `parser.py` reads `Key: Value` lines with `^(\w+)\s*:\s*(.+)$`.

### Formats that work

```
EMA9 LONG | RELIANCE          entry      -> Direction.LONG
EMA9 TP1 HIT | RELIANCE       target hit -> EXIT, tp_level=TP1, ExitQtyPct honoured
EMA9 SL HIT | RELIANCE        stop hit   -> EXIT
EMA9 EXIT | RELIANCE          any other  -> EXIT, put the cause in `Reason:`
RUNNER | RELIANCE             observation-> deliberately UNPARSEABLE, dropped
```

Mandatory body fields: `Symbol` (supplied by the pipe header), `Entry`, `SL`, `TP`.
`Target:` is aliased to `TP:`. Anything else with a single-word key lands in
`Signal.context` and becomes a column in the trade log at no cost.

### The traps that cost real signals

**A compact `LONG | Entry: 123` line yields NO entry field.** `^(\w+)\s*:` cannot match
past the pipe. TP HIT and SL HIT survive it only because the normalizer has dedicated
rewrite rules that synthesise the mandatory fields. A generic `EXIT` has no such rule,
so that shape drops the entire signal. This was a live bug in `breakout.pine`: its time
exit alert was silently discarded for months. Put `Side:` and `Entry:` on separate lines.

**A header like `TIME EXIT | SYM` parses as the strategy named `TIME`.** The pipe regex
is `^([\w-]+\s+(?:LONG|SHORT|EXIT))\s*\|`, so the word before `EXIT` is read as the tag.
Always lead with the real strategy tag.

**Emoji are stripped**, so they are safe in Telegram output but carry no meaning. This
repo's convention is plain text in source (see the root `CLAUDE.md`).

**Keys that are all digits, and values starting with `//`, are dropped** — that is what
keeps the `09:50 IST` footer and the chart URL out of the trade log. Do not rely on it
for anything else.

**Make observation alerts structurally unparseable**, not merely unlikely to parse: use a
header with no `LONG`/`SHORT`/`EXIT` token so `_parse_header` returns `None`. Route them
to a chat id the engine does not listen on.

Copy the builders from `alert-template.pine` in this skill directory.

### Verify before shipping

Never eyeball an alert format. Round-trip it:

```python
from signal_engine.normalizer import normalize
from signal_engine.parser import parse
sig = parse(normalize(msg))
assert sig is not None and sig.strategy == "EMA9"
```

Add the round-trip to `signal_engine/tests/test_<strategy>_alerts.py`. See
`test_ema9_alerts.py` for the shape: one test per alert type, plus one asserting the
broken shape still fails so it cannot be reintroduced.

---

## 3. Registering the strategy

Three files must agree or the engine rejects or mis-routes every signal:

1. `signal_engine/strategies.py` — add the tag constant.
2. `signal_engine/config.yaml` `strategy_profiles.<TAG>` — `product` (`MIS` for intraday)
   and `min_sl_pct` calibrated to the script's real stop geometry.
   **`product: MIS` is what puts the strategy in the 15:02 squareoff failsafe**
   (`scripts/openalgoscheduler.py`); omit the profile and open positions are not covered.
3. `signal_engine/config.yaml` `blacklist.<TAG>` — start with empty `hard`/`soft` lists
   and review monthly from the trade log.

Set `tp_levels` only if the script does NOT send `ExitQtyPct` in its TP alerts. Sending
`ExitQtyPct` is preferred: the strategy owns its exit fractions, not the config.

---

## 4. Backtest before it goes live

`signal_engine/backtest/` exists so a strategy is measured before it is wired to money.
Write an adapter (start from `adapter-template.py` here), then:

```bash
uv run --group analysis python -m signal_engine.backtest <name> --full
```

The adapter answers only "would this bar produce an entry, and with what stop and
target". The engine owns sessions, trade caps, next-bar fills, stop/target resolution,
costs and bookkeeping — so adapters stay small and every strategy is measured the
same way.

### Read the results honestly

- **Read the OOS row, not the IS row.** `Backtest.confirm()` prints in-sample,
  out-of-sample and full period. The in-sample row chose the settings; it cannot also
  validate them.
- **`|t| < 2` means indistinguishable from zero.** With a few hundred trades most
  differences are noise. Do not report a t=0.6 result as an edge.
- **Judge filters with `ablation()`, and only `BOTH` counts.** A filter that helps
  in-sample and fails out-of-sample was fitted to noise. On the EMA9 work the most
  upvoted community advice — a two-candle confirmation, ADX-rising, a 200 EMA trend
  filter, relative volume — every one of them was `IS only` or `neither`.
- **Compare in basis points, not only R.** R is not comparable across configs with
  different stop widths: widening the stop shrinks gross R and cost-in-R together, so an
  R-only table silently rewards wide stops. `gross_bps` sits directly against the cost line.
- **Know the cost line.** NSE intraday equity is roughly 8 bps of charges for a mid-size
  position plus a few bps of slippage. `cost-in-R = cost_pct x price / risk`, so a tight
  stop is expensive in R terms. Run `cost_sensitivity()`; for a marginal strategy the
  point where the sign flips IS the result.
- **Check `by_symbol()`.** A result carried by two names is noise.
- **Keep grids coarse.** Test structural choices, not third-decimal thresholds.

yfinance caps 5-minute history at 60 days, about 59 sessions. That is the binding
constraint on every intraday result. Detecting a genuine 0.05R edge at t=2 needs roughly
1600 trades. Use `data.from_openalgo` for longer history when it matters.

---

## 5. Pine v6 rules that actually bite

### Compile errors, and what causes them

| Error | Cause | Fix |
| --- | --- | --- |
| `mismatched input 'else'` | A **blank line between an `if` block and its `else`** | Delete the blank line. Comments inside the block are fine. |
| `Syntax error` on a wrapped line | A **continuation line indented by a multiple of 4** reads as a new block | Indent continuations by 5, 6, or any non-multiple of 4 |
| `Cannot use function declaration inside ...` | A function defined inside `if`/`for` | Declare every function at global scope |
| `Cannot call 'table.new' with 'position'=series string` | Position resolved through a helper function | Compute the ternary inline; a user function's return degrades to `series` |
| `Cannot use 'plot' in local scope` | `plot`/`plotshape`/`hline`/`bgcolor`/`barcolor` inside a conditional | Move to global scope, put the condition in the value: `plot(cond ? v : na)` |
| `Undeclared identifier` on a var used earlier in the file | Pine is single-pass | Declare before first use; hoist shared state above every reader |
| `The 'bool' type cannot be 'na'` | `bool flag = na` | `bool flag = false` |
| Inconsistent values on history reload | `ta.*` called inside a conditional | Call every `ta.*` unconditionally at global scope; branch on the result |

### Repainting

- Signals gate on `barstate.isconfirmed`. Without it the alert fires intrabar and can
  un-fire; the broker order cannot.
- Higher-timeframe and previous-day values use the confirmed-bar idiom:
  `request.security(sym, tf, expr[1], lookahead=barmerge.lookahead_on)`.
- A pivot confirms `n` bars after it prints. That lag is inherent; removing it is
  lookahead, not cleverness.
- Alerts use `alert.freq_once_per_bar` (or `_close`), never `freq_all`.

### Execution semantics to keep in step with the backtest

- `process_orders_on_close=false` fills at the NEXT bar's open, which is what a live
  market order on the alert does. The gap between the signal close and the fill is a
  real cost.
- When a bar spans both stop and target, assume the **stop**. At 5-minute resolution the
  sequence is unknowable and the optimistic read flatters the backtest.
- `strategy.exit()` re-issued with the same id AMENDS the order; use that for trails, and
  ratchet only — a trail must never loosen.

### Resource hygiene on the chart

- `max_labels_count` / `max_lines_count` / `max_boxes_count` are hard ceilings. Prefer
  `plot(cond ? level : na, style=plot.style_linebr)` over line objects for live trade
  levels: nothing to allocate, track, or clean up.
- Create a table once with `var table t = na` plus `if na(t)`, then refill cells. Do not
  `table.delete` and rebuild on every realtime tick.

### NaN, the quiet one

**NaN fails every naive comparison, in both directions.** `risk <= 0` is False when
`risk` is NaN, and so is `risk / close < min_sl_pct`. A guard built only from
comparisons lets NaN straight through. This bit the EMA9 backtest: during indicator
warmup a rolling `swing_hi` was NaN, the stop was NaN, the setup was consumed, and the
fill was then dropped because `NaN > 0` is False — real day-one signals vanished with no
error anywhere. Guard with `na(x)` in Pine and `np.isnan(x)` in Python, explicitly.

Indicator warmup is the usual source: `ta.highest/lowest`, `ta.sma`, `rolling(...)` are
all NaN until their window fills.

---

## 6. Checklist before calling it done

- [ ] Every alert leads with the strategy tag; `Entry`/`SL`/`TP` are each on their own line
- [ ] Entry, TP, SL, time-exit and any custom exit all emit an alert
- [ ] Observation alerts are structurally unparseable and routed to a separate chat id
- [ ] Round-trip test through `normalize` + `parse` for every alert type, in `signal_engine/tests/`
- [ ] Tag registered in `strategies.py`; `strategy_profiles` has `product` and a calibrated `min_sl_pct`; blacklist section exists
- [ ] Time exit matches `config.yaml time_exit`
- [ ] Signals gated on `barstate.isconfirmed`; HTF and previous-day reads use the confirmed-bar idiom
- [ ] No `ta.*` inside a conditional; no `plot`/function declaration in local scope
- [ ] NaN guarded explicitly wherever an indicator is still warming up
- [ ] Backtest adapter written; `confirm()` and `ablation()` run; **the OOS row reported, not the IS row**
- [ ] Defaults justified by something that held in BOTH windows, or by mechanics — never by an in-sample best
- [ ] If the result is marginal, the verdict and its numbers are written into the script header
- [ ] No emojis or icons in source, comments or commit messages (root `CLAUDE.md`)
