# signal_engine

Map of this directory: what lives where and where to add new files. Rules live in the
repo `CLAUDE.md`; idea status lives in `research/REGISTRY.md`; change history lives in
`docs/REFACTOR-LOG.md`. Each fact has one owner, so link rather than copy.

| Area | Path | Put here |
| --- | --- | --- |
| Live pipeline (runtime) | `main.py`, `listener.py`, `parser.py`, `validator.py`, `risk.py`, `executor.py`, `tracker.py`, `notifier.py`, `startup.py`, `config.py`, `config.yaml` | Code that runs during market hours. Kept flat because `signal_engine.<module>` is imported by scripts, cron and tests |
| Strategy library | `strategy_library/{intraday,swing}/<name>/` | `.pine` sources, `STRATEGY-ANALYSIS.md` / `STRATEGY-LOG.md`, strategy-specific assets |
| GoCharting ports | `strategy_library/lipiscripts/` | `.lipi` indicator ports |
| Research | `research/` | Ideas, verdicts, backtest run records; see `research/README.md` |
| Backtest engine | `backtest/` | Engine, data, metrics, validation suite; `backtest/strategies/` holds one adapter per strategy |
| Reporting code | `analysis/` | EOD and weekly review code and the BreakingTrade tooling |
| Generated reports | `reports/` | Output of `analysis/eod.sh` and `analysis/weekly.sh` (gitignored) |
| Operations | `scripts/` | Service control, scheduler, cron wrappers |
| Runtime state | `data/`, `logs/` | Databases, sessions, logs (gitignored) |
| Design and history | `docs/` | PRD, HOW-IT-WORKS, REFACTOR-LOG, reviews, restrictions |
| Tests | `tests/` | `PYTHONPATH=. uv run pytest signal_engine/tests/ -v` |

## Where does a new file go

- A new strategy idea: `research/ideas/` (copy `_template`), not the strategy library.
- Once it is being built: add `strategy_library/<horizon>/<name>/` and link it from the idea.
- A backtest result: a run record in that idea's `runs/`, plus prose only if it adds insight.
- A new backtest adapter: `backtest/strategies/<name>.py`.
- Design notes, reviews, one-off analyses: `docs/`.
