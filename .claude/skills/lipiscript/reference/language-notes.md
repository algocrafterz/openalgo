# LipiScript language notes

Two tiers below. **Verified** means it appears in a script that compiled on
GoCharting (`signal_engine/lipiscripts/intraday/orb.lipi`,
`initial_balance.lipi`). **Docs-only** means the official documentation
describes it but no compile has confirmed it — the docs are known to overstate
the grammar, so treat these as unavailable until proven.

---

# Verified surface

## Declaration

```
indicator("Session Based Custom ORB Optimized", overlay=true)
```

One declaration, first non-comment line. Named args use `name=value` with **no
spaces around `=`**.

## Comments

`//` only, and a comment **must begin on its own line**. Trailing comments after
code are rejected.

Section banners in the house style:

```
// ==== PERSISTENT VARIABLES ====
```

## Statements and blocks

Global-scope statements cannot begin with a space or tab. Local blocks are
braced and indented four spaces.

```
if longBreak {
    longTaken := true
}
```

## Variable declarations

| Mode | Behaviour | Pine equivalent |
| --- | --- | --- |
| (none) | reinitialised every bar | (none) |
| `static` | initialised once, persists across bars | `var` |
| `intra` | escapes rollback within a realtime bar | `varip` |

```
static float rangeHigh = na
static bool rangeLocked = false
sessionType = input.int(0, "Session Type (0=NSE, 1=US)")
```

`=` declares, `:=` reassigns. Bare `x = na` fails type inference — always give
the type: `static float x = na`.

## Operators

`+ - * / %` · `< <= != == > >=` · `not` `and` `or` · `cond ? a : b` · `[]`
history · `=` declare · `:=` reassign.

`and` binds tighter than `?:`, so this needs no parentheses:

```
plot(inSession and rangeLocked ? rangeHigh : na, color=color.green)
```

## Inputs

**Only two arguments: default and title.**

```
startHour = input.int(9, "Range Start Hour")
```

`minval`, `maxval`, `step`, `group`, `tooltip`, `inline` and `confirm` are
documented but rejected by the parser. Adding `step` produces:

```
mismatched input 'step' expecting {FundamentalDataType, FormType, Identifier, NEWLINE}
```

There is no verified `input.bool`. Use an int flag instead:

```
showMid = input.int(1, "Show Midpoint (0=no, 1=yes)")
```

For a fractional value, take an integer percent and divide:

```
extPercent = input.int(100, "Extension Percent of IB Range")
extAmount = ibRange * extPercent / 100
```

## Built-ins

Price: `open`, `high`, `low`, `close`.

Time: `hour`, `minute`, `dayofmonth`. These follow the **chart** timezone.

`na` value, and `na(x)` as a **function**:

```
rangeHigh := na(rangeHigh) ? high : math.max(rangeHigh, high)
if not inTimeWindow and inSession and not rangeLocked and not na(rangeHigh) {
```

`math.max`, `math.min`.

`talib.crossover(a, b)`, `talib.crossunder(a, b)`.

## Plotting

```
plot(series, color=color.green, linewidth=2, title="Range High")
```

Hide conditionally by plotting `na`; never wrap `plot()` in an `if`.

```
plotshape(longBreak, shape=shape.triangleup, location=location.belowbar, color=color.green, size=size.small, title="Long Breakout")
plotshape(shortBreak, shape=shape.triangledown, location=location.abovebar, color=color.red, size=size.small, title="Short Breakout")
```

Verified constants: `shape.triangleup`, `shape.triangledown`,
`location.belowbar`, `location.abovebar`, `size.small`, `color.green`,
`color.red`, `color.blue`, `color.orange`.

---

# Docs-only surface (unverified — do not use without a compile)

Given `input.int` already contradicts its own documentation, everything here is
suspect. If a script needs one of these, tell the user it is unverified and ask
them to report the editor error.

**Plot styles.** `style=plotStyle.line | linebr | stepLine | area | areabr |
bar | histogram | scatter | cross`. Without `linebr`, `na` gaps may be bridged
across the overnight gap; gating plots on an `inSession` flag is the verified
workaround.

**Levels and fills.**
`hline(price, title, color, style, linewidth, editable)` with
`hlineStyle.solid | dotted | dashed`; `price` must be constant.
`fill(plot1, plot2, color, title, ...)` and `fill(hline1, hline2, ...)`.

**Other inputs.** `input()`, `input.float()`, `input.bool()`, `input.color()`,
`input.string()`, `input.text_area()`, `input.price()`, `input.source()`.
`input.session()` and `input.symbol()` are indexed with no signature at all.

**Other outputs.** `bgcolor()`, `plotbar()`, `plotcandle()`, `plotchar()`,
`plotarrow()`.

**Colors.** `color.new(color, transparency)`, `color.rgb()`,
`color.from_gradient()`. The docs show both `color.new(c, 70)` and
`color.new(c, 0.1)`, so even the transparency scale is ambiguous.

**Time.** `time`, `timenow`, `time_close`, `second`, `dayofweek`, `month`,
`year`, `weekofyear()`, `timestamp()`, `str.format_time()`.

**Namespaces.** `syminfo.*` (`.ticker`, `.mintick`, `.timezone`, `.session`,
`.currency`, `.root`, `.asset_type`), `interval.*` (`.isintraday`, `.isdaily`,
`.multiplier`, ...), `barstate.*` (`.isfirst`, `.islast`, `.isnew`,
`.isrealtime`, ...), `bar_index`.

**Indicators.** `talib.sma`, `.ema`, `.macd`, `.rsi`, `.bb`, `.supertrend`,
`.vwma`, `.tsi`, `.cum`, `.barssince`, `.highest`, `.pivothigh`.

**Math.** `math.abs`, `.log`, `.floor`, `.round`, `.avg`, `.sin`, `.cos`,
`.random`. `nz(x)` / `nz(x, replacement)`.

**Control flow.** `if` as an expression, and `switch`:

```
x = if close > open {
    close
}
else {
    open
}

switch s {
    case "Hello": y := 1
    default: y := 3
}
```

**Alerts.** `alertcondition(cond, title="...", message="...")` and
`alert("text")` inside an `if`. Used in `initial_balance.lipi` but not yet
confirmed by a compile.

**Objects.** `line`, `linefill`, `box`, `polyline`, `label`, `table`,
`chart.point` exist as types, and user-defined types support `.new()` and
`.copy()`. The constructors for the built-in drawing objects (`label.new`,
`box.new`, `line.new`) have no documented signature — treat as unavailable.

**User-defined functions.**

```
barIsUp() {
    return close > open
}
```

---

# Platform limits

- 64 plot counts per script; `plot`, `plotshape`, `plotarrow`, `plotbar`,
  `plotcandle`, `plotchar`, `bgcolor` and series-color `fill` all consume them
- **`series[N]` (the `[]` history operator) hard-errors above N=500, for any
  timeframe** - confirmed by an actual runtime error in this repo:
  `Runtime error at index 0. The script is trying to access historical data
  outside allowed range(525). The allowed range is 0 to 500.` This is a
  separate, tighter limit than "9,000 bars back" below - that figure appears
  to describe total bars a chart can load/display, not the max depth a
  single `[]` reference may reach. Treat 500 as the real ceiling for any
  `series[N]` literal until proven otherwise. Also confirmed: in
  `cond ? volume[a] : volume[b]`, only the branch actually selected by `cond`
  is evaluated at runtime - the unselected branch's out-of-range literal does
  not error. Do not rely on this to smuggle a >500 literal into unused code;
  it was only confirmed for this one construct.
- 9,000 bars back (chart display/load, not `[]` depth - see above), 500 bars
  forward
- 80,000 compiled tokens; 1,000 variables per scope; 550 scopes total
- 20s execution (basic) / 40s (paid); 500ms per loop per bar
- 2-minute compile limit; three consecutive compile warnings trigger a 1-hour ban
