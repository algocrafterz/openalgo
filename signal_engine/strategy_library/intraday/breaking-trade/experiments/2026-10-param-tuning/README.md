# BreakingTrade parameter tuning, October 2026 (research only)

Read-only. Reuses `../2026-10-amt-replay/amt_replay.py` and `bt_replay.py` without editing them.
Reads `data/trades.db`, `data/breakingtrade.db` and `db/historify.duckdb` read-only; writes only
`results/` (gitignored CSVs). Historify as of 2026-10-27.

| File | Purpose |
|---|---|
| `PREREG.md` | Grid and acceptance rule, written before any trial ran |
| `tune.py` | Parameterised copy of decide/build/evaluate/simulate; all trials, both periods, look-ahead check on every trade |
| `analyze.py` | Applies the acceptance rule to `results/summary_*.csv` |
| `REPORT.md` | Findings |

## Reproduce (from the openalgo checkout)

```sh
D=signal_engine/strategy_library/intraday/breaking-trade/experiments/2026-10-param-tuning
uv run python $D/tune.py check                     # baseline must print identical=True for P1 and P2
uv run python $D/tune.py run                       # 21 pre-registered trials + B (about 4 min)
uv run python $D/tune.py run B tp_1.5 np_min_150   # exploratory neighbours, writes summary_extra.csv
uv run python $D/analyze.py                        # table + rule verdicts
uv run python $D/analyze.py summary_extra.csv
```

Periods: P1 = picks 2026-08-18 .. 2026-09-23, P2 = 2026-09-24 .. 2026-10-27.
