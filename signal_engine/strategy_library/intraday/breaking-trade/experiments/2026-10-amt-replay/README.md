# BreakingTrade replay experiments, October 2026

Isolated research code. Nothing here is imported by the engine, the scanner or any service, and
nothing here changes the running paper strategy. Findings and decisions are in
`../../STRATEGY-LOG.md` (entries 2026-10-27 23:58, 2026-10-28 01:28, 2026-10-28 02:10 and
2026-10-28 17:40).

| File | What it does |
|---|---|
| `bt_replay.py` | Replays each EXECUTED paper trade (trades.db) on Historify 1m bars under the engine's exits; stop and no-progress variants |
| `amt_replay.py` | Rebuilds entries from the scanner's picks (breakingtrade.db `scan_hits`) and tests one Market Profile change at a time (B, E1-E7, E17, E17M, E17H) |
| `selection_check.py` | The scanner's picks alone: move in the pick's direction after the pick, against the average F&O stock over the same minutes (`uv run python $D/selection_check.py`) |
| `results/` | Per-trade CSVs written by the two scripts. Gitignored like every CSV in the repo, so they exist only on the machine that ran them; rerun the commands below to regenerate |

## Inputs (read-only)

- `signal_engine/data/trades.db`: paper entries and exits, strategy `BREAKINGTRADE`, `trade_mode='analyze'`
- `signal_engine/data/breakingtrade.db`: `scan_hits`, `snapshots`
- `db/historify.duckdb`: NSE 1m and daily bars, and NIFTY (`NSE_INDEX`)

Historify is updated by the scheduled backfill, so a rerun after a backfill that corrects old bars
can differ slightly from the recorded figures. The recorded runs used the database as of
2026-10-27 20:31 IST.

## Commands (from the openalgo checkout)

```sh
D=signal_engine/strategy_library/intraday/breaking-trade/experiments/2026-10-amt-replay
# executed-trade replays: <name> <no-progress minutes> <stop mode: ib | tail_outer | tail_inner>
uv run python $D/bt_replay.py baseline 90 ib
uv run python $D/bt_replay.py np60 60 ib
uv run python $D/bt_replay.py tail_outer 90 tail_outer
uv run python $D/bt_replay.py tail_inner 90 tail_inner
# rebuilt entries: test period (default 2026-09-24 onward) and holdout
for v in B E1 E2 E3 E4 E5 E6 E7 E17; do uv run python $D/amt_replay.py $v; done
for v in B E1 E7 E17; do uv run python $D/amt_replay.py $v 2026-08-18 2026-09-24; done
# RI stop variants (2026-10-28 17:17 log entry): E17 entries, stop at the IB midpoint / broken edge
for v in E17 E17M E17H; do uv run python $D/amt_replay.py $v; uv run python $D/amt_replay.py $v 2026-08-18 2026-09-24; done
```

## Assumptions shared by every variant

- Exits: SL-M at the stop; staged targets 50/30/20 at 1.0/1.5/2.0 x the TP1 distance with the
  runner stop at each level minus 0.3 x its R; loss cut at 20 min below -80% progress; no-progress
  gate at 90 min below 20% progress; time exit 14:45. A stop and a target in one bar count as the
  stop. Gates act at the next 1m open.
- Entry (amt_replay): order at the first scanner poll after the trigger bar closes, filled at that
  minute's 1m open.
- Sizing: 1% of capital over the stop distance with the 0.10 slippage factor, notional capped at
  5x capital. Capital per day is estimated from that day's executed trades, else the median; this
  changes rupee figures only, never expectancy in R.
- Costs: Flattrade statutory charges (`portfolio.costs.india_intraday`) plus 4 bps slippage per
  side (`net4`), and 8 bps as a stress case (`net8`).
- Look-ahead: amt_replay recomputes every accepted decision from bars before the order minute and
  aborts on any difference; E17 trades are also checked rule by rule (`audit_e17`).
