---
name: lipiscript
description: Write, review or debug a GoCharting LipiScript (Lipi) indicator or study. Use whenever the user mentions GoCharting, LipiScript, Lipi, a .lipi file, the Lipi Editor, or asks to port a TradingView Pine Script indicator to GoCharting. Covers the language's deviations from Pine v6, the verified-by-compilation built-in surface, the compile checklist, and wiring GoCharting alerts to OpenAlgo webhooks.
---

# LipiScript (GoCharting)

LipiScript is GoCharting's cloud-hosted charting language — a Go-based DSL that
reads like Pine Script but **is not Pine**. Several core keywords differ, and
porting Pine verbatim will not compile.

## The governing rule: the docs overstate the grammar

`https://gocharting.com/docs/scripting/` documents parameters the parser
rejects. Confirmed example: the docs give
`input.int(defval, title, minval, maxval, step, tooltip, inline, group, confirm)`,
but the real grammar accepts only `input.int(defval, title)`. Anything more
fails with:

```
mismatched input 'step' expecting {FundamentalDataType, FormType, Identifier, NEWLINE}
```

That error shape — "expecting ... Identifier, NEWLINE" — means the parser gave
up mid-call and started looking for a new statement. It almost always means an
argument the grammar does not know.

So: **a working script outranks the documentation.** Write only from the
verified-by-compilation column in `reference/language-notes.md`. Treat every
docs-only feature as unavailable until a compile proves otherwise.

There is no local compiler. The only verification is pasting into the Lipi
Editor at the bottom of a GoCharting chart and clicking Save. Author for
compile-on-first-try.

## Working references in this repo

- `signal_engine/lipiscripts/intraday/orb.lipi` — session-based opening range
  breakout. This is the canonical proven-syntax file. Copy its idioms.
- `signal_engine/lipiscripts/intraday/initial_balance.lipi` — NSE 09:15-10:15
  initial balance with midpoint, extensions and breakout alerts. Template for
  any session/time-window study.

## Workflow

1. Read `reference/language-notes.md`.
2. Read `orb.lipi` and mirror its call style exactly.
3. Run the compile checklist below.
4. Hand over the script plus load steps: Lipi Editor -> New -> name -> paste ->
   Save -> Add to Chart.
5. Say plainly that you could not compile it, and ask for the first editor error
   verbatim if one appears.

## The Pine traps

| Pine v6 | LipiScript |
| --- | --- |
| `var x = 0` | `static x = 0` |
| `varip x = 0` | `intra x = 0` |
| `ta.ema(close, 9)` | `talib.ema(close, 9)` |
| `input.int(9, "H", minval=0)` | `input.int(9, "H")` — no minval/maxval/step |
| `input.bool(true, "Show")` | `input.int(1, "Show (0=no, 1=yes)")` |
| `color=color.red` with spaces | `color=color.red`, no spaces around `=` |
| `close > open  // trailing comment` | comments must start on their own line |

`and` / `or` / `not`, `=` vs `:=`, and `[]` history all match Pine.

## Compile checklist

- [ ] One `indicator("Name", overlay=true)` on the first non-comment line.
- [ ] Every `input.*` call has exactly two arguments: default and title.
- [ ] Booleans are `input.int(0|1, "Label (0=no, 1=yes)")`, not `input.bool`.
- [ ] Named arguments are `name=value` with no spaces around `=`.
- [ ] No `var` / `varip`; use `static` / `intra`.
- [ ] No `ta.` prefix; use `talib.`.
- [ ] No trailing `//` comments. Every comment starts its own line.
- [ ] No global-scope statement begins with a space or tab; local blocks are
      braced and indented four spaces.
- [ ] Every `plot()` / `plotshape()` is in global scope, never inside `if` or a
      function. Hide a plot by passing `na`, not by a conditional call.
- [ ] Variables that can hold `na` are declared with an explicit type
      (`static float x = na`); bare `x = na` fails type inference.
- [ ] `na` is tested with the function `na(x)` / `not na(x)`.
- [ ] No `plotStyle.*`, `hline()`, `fill()`, `bgcolor()`, `input.bool`,
      `input.color`, `input.float`, `label.new`, `box.new`, `line.new` unless
      the user has confirmed a compile — all are docs-only, unverified.
- [ ] 64 plot counts max.

## NSE specifics

- `hour` / `minute` follow the **chart's** timezone, not the exchange's. Expose
  session start as `input.int` hour and minute rather than hardcoding 9 and 15,
  so a chart left on UTC is fixed in the settings panel instead of the code.
- NSE regular session is 09:15-15:30 IST, 375 minutes.
- Compute `minuteOfDay = hour * 60 + minute` and compare against integer
  bounds. It is clearer and less error-prone than nested hour/minute
  comparisons once a window crosses an hour boundary.
- Bar timestamps may be open- or close-stamped. For any window boundary offer
  both forms (`>= open and < close` vs `> open and <= close`) behind a
  `stampMode` input; getting it wrong shifts the window one bar and silently
  corrupts the level.
- Reset per-day state on `na(dayofmonth[1]) or dayofmonth != dayofmonth[1]`.
  `orb.lipi` instead resets on an exact session-open bar match, which breaks on
  close-stamped feeds and on intervals that do not align to 09:15.
- Gate plots on an `inSession` flag so levels do not run into the post-market.

## Alerts and OpenAlgo

`alertcondition(cond, title="...", message="...")` registers a condition in
GoCharting's alert widget; `alert("text")` fires inline from inside an `if`.

The order JSON does **not** go in the script — GoCharting alerts carry the
payload configured in the alert dialog:

1. Generate it in OpenAlgo at `/gocharting/` (Platforms -> Configure
   GoCharting): symbol, exchange, product, action, quantity.
2. Paste the webhook URL (`<HOST_SERVER>/api/v1/placeorder`) and that JSON into
   the GoCharting alert.
3. Point the alert at the script's `alertcondition` by title.

Webhook alerts require a GoCharting Premium plan. Full setup:
`frontend/public/docs/gocharting_webhook_setup.md`.
