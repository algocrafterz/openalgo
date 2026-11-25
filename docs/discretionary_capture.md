# Discretionary page: MAE, MFE, capture and giveback

Source of truth: `services/discretionary_analytics.py` (`position_excursion`,
`position_realized_points`, `position_capture`, `instrument_capture_stats`,
`excursion_aggregates`, `CAPTURE_MIN_BARS_BETWEEN_FILLS`) and
`services/discretionary_pnl_service.py` (`position_excursion_result`, `excursion_rows`,
`EXPORT_COLUMNS`). Served by `GET /api/discretionary-pnl/excursions` and
`GET /api/discretionary-pnl/export.csv`.

## MAE and MFE are conservative lower bounds

NSE cash equity only, from stored 1-minute candles, for completed positions.

- Between two fills the open quantity and the average entry in force are constant. An entry fill
  re-weights the average. An exit fill only reduces the quantity.
- Per unit: long adverse = average - low, favourable = high - average. Short is mirrored. Each is
  floored at 0. MAE and MFE (points per unit) are the worst and best per-unit values over the
  path. The rupee figures (`mae_amount`, `mfe_amount`) are the largest (per-unit excursion x
  quantity open at that moment), so they can come from a different moment than the points.
- The candle of a minute that holds a fill is NOT used for its high or low. A candle does not say
  whether its extreme came before or after the fill. Only the fill price counts for that minute
  (against the average before and after the fill). Every other minute between two consecutive
  fills uses its full high and low. The result never overstates. No upper bound is calculated.
- Candles are stamped with the minute they start. Every session minute from the first to the last
  fill minute (both included) must have a valid candle. If one is missing (`missing_bars`) or
  invalid (`invalid_bars`), the whole excursion is unavailable. Nothing is estimated.
- `bars_expected` = session minutes from the first to the last fill minute, both included.
  `bars_used` = full candles actually used: the minutes between consecutive fills that hold no
  fill. `bars_used` is 0 when all fills are in the same minute or in adjacent minutes.
- Rounding: points to 4 decimals, rupee figures to 2.
- Gross basis. Charges are never part of MAE, MFE, points, capture or giveback.

## Realised points, capture and giveback

One basis everywhere: the average entry price in force at each exit.

- **Realised points** (`realized_points`, gross, per unit) = sum over exit fills of
  `sign x (exit price - average entry in force at that exit) x exit quantity`, divided by the
  total exited quantity. `sign` is +1 long, -1 short. Charges are money, not points, and are never
  deducted. When every entry comes before the first exit, this equals the overall average exit
  minus the overall average entry, which is `points_per_unit`. When an entry follows a partial
  exit, the two differ, because an earlier exit is never measured against a later average. The
  value is `null` when the direction or the fills are not consistent. It is also given for a
  position that is left out of capture, when it can be computed.
- **Capture** (`profit_capture`) = realised points / MFE points. The API gives a fraction (0.62
  is 62%). It is not capped.
- **Giveback** (`giveback_points`) = MFE points - realised points (gross points per unit). It is
  `null` unless the position is in capture.
- **MAE above half of MFE**: a position counts when MAE points > 0.5 x MFE points. The
  denominator is the positions of the instrument with an available excursion, at least
  `CAPTURE_MIN_BARS_BETWEEN_FILLS` full candle, and MFE above 0. This test does not need a net
  winner.
- **Coverage rule.** `CAPTURE_MIN_BARS_BETWEEN_FILLS = 1`: at least one full candle lies between
  the fills (`bars_used` >= 1). Without it, MFE comes from a few fill prices, not from a price
  path.

Eligibility. A position enters capture only when all of these hold. The checks run in this
order. The first check that fails gives `capture_reason` (`capture_reason_text` in the API):

1. `net_unavailable`: net P&L after charges is not known.
2. `not_net_winner`: net P&L is 0 or below.
3. `excursion_unavailable`: no MAE and MFE (see the reasons below).
4. `insufficient_candle_coverage`: `bars_used` is below the constant.
5. `mfe_not_positive`: MFE is 0.
6. `data_inconsistency`: realised points are unknown or above MFE.

Capture above 100% cannot occur on this basis. Every exit price is a point of the observed path,
measured against the average in force at that exit. If it happens, the position is excluded as
`data_inconsistency` and counted. It is never clipped to 100%.

Reasons for an unavailable excursion (`reason`, and `excursion_reason` in the CSV):
`unsupported_segment` (not NSE cash equity), `no_fills`, `fill_times_unreliable`,
`inconsistent_fills`, `session_calendar_unavailable`, `fill_outside_session`, `invalid_bars`,
`missing_bars`, `bar_read_failed`, `not_computed_cap`, `after_market_close` and
`historify_incomplete`.

MAE and MFE stay shown for a position that is left out of capture.

## Per instrument, never pooled

Instrument = symbol + exchange. Capture, giveback and the points averages are never combined
across instruments. `aggregates.instruments[]` has one entry per instrument in scope, sorted by
symbol and exchange. `aggregates.instrument_count` is their number.

- Each entry has `symbol`, `exchange`, `available`, `winners` and `losers` (MAE and MFE in points
  and rupees: count, average, median) and `capture`.
- `capture` has `positions` (N, all completed positions of the instrument in scope), `captured`
  (n), `excluded` (count per reason), `excluded_total`, `median_capture`,
  `median_giveback_points`, `mae_over_half` (`count`, `denominator`, `share`) and `basis`.
  n + `excluded_total` = N always. The medians use the n captured positions only. `share` =
  `count` / `denominator`. It is `null` when the denominator is 0.
- `aggregates.winners` and `aggregates.losers` keep the rupee figures for the whole scope. Their
  `mae_points` and `mfe_points`, and the winners' `profit_capture`, are filled only when the
  available positions are of one instrument. They are `null` when more than one instrument has
  an available position.
- The MAE/MFE scatter is drawn only for a single instrument.
- There is no daily or total points figure across instruments.
- A winner in these figures has net P&L above 0 (after estimated charges). A loser has net P&L
  below 0. A position with unknown or zero net P&L is in coverage only.

## Counts and coverage

- `aggregates.coverage` gives `available` of `total` positions, and the count per unavailable
  reason. Each figure is shown with its sample size (n of N). The counts make no claim about
  statistical significance. No minimum number of trades applies.
- An unavailable figure is left out. It is never shown as 0.

## API and CSV fields

Position row (`positions[]`): `profit_capture`, `realized_points`, `giveback_points`,
`capture_reason`, `capture_reason_text`, `bars_used`, `bars_expected`, plus the existing
`status`, `reason`, `mae_points`, `mfe_points`, `mae_amount` and `mfe_amount`.
`profit_capture` keeps its name. It now uses the in-force basis described above. The value is
unchanged for a position whose entries all come before its first exit.

CSV columns (`EXPORT_COLUMNS`): the existing columns keep their order. Five columns are appended
after `excursion_reason`: `realized_points`, `giveback_points`, `capture_reason`, `bars_used`,
`bars_expected`. `profit_capture` stays in its old place. An empty cell means unknown, never
zero. The CSV has every closed position of the scope. The 300-position cap below also applies to
its excursion columns.

## Limitations

- No upper bound of the true excursion is calculated. The true MAE and MFE can be larger.
- NSE cash equity only. Other segments are `unsupported_segment` and have no MAE, MFE or capture.
- Capture needs candle coverage (see the rule above) and a net winner. Net losers and break-even
  positions have no capture.
- At most `EXCURSION_MAX_POSITIONS` = 300 NSE equity positions are read per request, newest close
  first. The rest are `not_computed_cap`.
- A position closed today has no excursion (`after_market_close`) until the exchange session
  ends. After that, it stays unavailable (`historify_incomplete`) until Historify has every
  minute of the day.
- An available result for a position that closed on an earlier day is cached in memory for 6
  hours. An unavailable result is not cached. The excursions response is also cached for a few
  seconds per request scope. A restart clears both caches. After a code change, restart the app
  to load it.
- A small sample is indicative only. A median of 3 positions is not a trend.
- Several instruments in one range: the per-instrument path is tested on synthetic data only. The
  real data in `test/fixtures/discretionary_capture_tcs_2026_11_23.json` is four positions of one
  instrument (TCS, 2026-11-23).
