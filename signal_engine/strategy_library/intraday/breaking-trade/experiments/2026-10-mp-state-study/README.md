# Market Profile state study, October 2026

Isolated, read-only research on whether the BreakingTrade website's Market Profile labels carry
forward-looking information. Nothing here is imported by the engine or the scanner. Findings are
in `../../STRATEGY-LOG.md` (entries 2026-10-28 03:40 and 04:30).

| File | Question |
|---|---|
| `state_study.py` | Every state read at 09:35 / 10:20 / 11:01 on all ~230 F&O stocks: does it predict the rest of the day beyond the average stock? |
| `volume_study.py` | Does volume participation (pace-adjusted surge) decide whether the morning's move continues? |
| `picks_by_volume.py` | The same split applied to the scanner's own picks (needs `../2026-10-amt-replay/results/selection_2026-08-18.csv` from `selection_check.py`) |
| `event_study.py` | Acting at the FIRST poll a label or a guide scan appears, and 10 / 20 minutes later |
| `results/` | Per-stock-day CSVs (gitignored; rerun to regenerate) |

Inputs (read-only): `signal_engine/data/breakingtrade.db` (`snapshots`), `db/historify.duckdb`
(1m bars, as of 2026-10-27 20:31 IST). Run each from the openalgo checkout with
`uv run python signal_engine/strategy_library/intraday/breaking-trade/experiments/2026-10-mp-state-study/<file>`.

Shared method: direction for each state fixed from auction theory or the vendor guide before
running; entry at the 1m open after the decision minute; excess = return minus the equal-weight
mean of the universe over the same minutes, signed by that direction; t clustered by day;
periods 2026-08-18..09-23 and 2026-09-24..10-27.
