### /trading is a full-screen terminal

Traders compare /trading with the best charting terminals they use, so its
finish is held to that bar: borrow their craft (density, crispness, one icon
weight, quiet states), never their layout, grouping or wording, and **no brand or
product names anywhere in /trading**: code, comments, class names, tooltips and
copy describe things generically.

- **No app navbar on /trading.** The terminal takes the whole viewport. Its one
  toolbar band carries a Dashboard link at the left and the account controls at
  the right: broker badge, Live or Analyze badge, mode switch, theme toggle and
  profile menu. Both the navbar and the terminal render the same
  `components/layout/AccountControls.tsx`; never fork that logic into the
  terminal, or mode switching and its warning toasts drift apart.
- **Those four (Dashboard, mode switch, theme toggle, profile menu) are visible
  at every width**, outside the toolbar's scrolling part. Everything else gives
  way first: labels collapse to icons with tooltips on narrower screens, and the
  row must fit at 1366 px. `Trading.test.tsx` pins the four by accessible name.
- **Every glyph comes from the chart engine**, through the generated
  `chromeIcons.ts` and `drawingToolMetadata.ts` and the `ChromeIcon` component in
  `lib/trading/glyphs.tsx`, at the engine's line weight. The toolbar never imports
  `openalgo-charts/draw` for an icon: that would load the drawing renderer with
  the toolbar. Where the engine has no glyph, the app icon kept in its place is
  drawn at the same weight.
- **The drawing rail follows the engine's reference host rail**
  (`examples/yfinance/src/rail.js` in openalgo-charts): glyph sizes, group carets
  and flyouts, held-tool and magnet dots, the pinned controls block, and the fold
  with its tab at the chart's lower left (`oa-trading-rail-folded` in local
  storage). Change both together, or the two drift.
- **Three looks, one source.** Live light, live dark and analyzer (the violet
  accent) all come from the app's theme tokens through
  `lib/trading/chartTheme.ts` and the tokens in `index.css`; no component carries
  a palette of its own. A visual change is checked in all three, and the theme
  toggle stays disabled in analyzer, as it is in the navbar.
- **The chart the toolbar drives** is chosen from the layout menu ("Selected
  chart"), and the focused pane's frame shows the accent. Keep the toolbar's
  `data-toolbar-pane` attribute: the browser harness reads it.

