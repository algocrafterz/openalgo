---
name: strategy-performance-analyst
description: Use when asked to "evaluate all paper-traded strategies", "evaluate live strategies", "compare paper and live", "which strategies are improving or deteriorating", "compare recent performance with earlier", "strategy health check" or "trend report". Read-only trend and health analysis of every strategy that trades in a mode (paper, live or both), recent window vs previous window vs full history, with trade-level diagnosis, evidence labels and ranked recommendations for the owner. For one strategy's single session use paper-review. For execution incidents use execution-health.
---

# Strategy performance analyst

One run: `/strategy-performance-analyst <paper|live|both> [YYYY-MM-DD cut-off]`. Default mode: paper. Default cut-off: the last session that has data in that mode.

This skill is read-only. It reuses the Strategy Daily Performance calculations. It adds no metrics engine.

## 0. Scope and boundaries

- Do not edit code or config. Do not restart anything. Do not place, change or cancel orders.
- Do not commit. Do not post to Telegram. Do not write files in the repo except the report the owner asks for.
- Open every SQLite file as `file:<path>?mode=ro` with `uri=True`. The script in `references/query-recipes.md` does this and prints a `RO_AUDIT` list.
- Run Python from the repo root: `PYTHONPATH=. uv run python ...`. Use `git --no-optional-locks` for git.
- Never run a full test suite. Never source `signal_engine/.env` (CRLF). Never print `.env` values. Mask any secret.
- No unicode in log lines. No emojis in the report.
- Do not invent or simulate data. If a mode has no data, report that limit and stop for that mode.
- A recommendation is a proposal for the owner. The owner decides. For live, never recommend an automatic change to a live config or parameter. Say what to check. The owner decides what to do.

## 1. Modes

| Mode | Reads | Rule |
| --- | --- | --- |
| paper | rows stored as `analyze` | Paper performance, estimated costs, differences from expected live execution |
| live | rows stored as `live` | Actual realised performance, execution quality and operational risk first |
| both | paper and live, each on its own | Two separate assessments. Then one comparison of rates and execution differences only |

- Strategy discovery is automatic for each mode (step 1). A strategy that trades in both modes is analysed twice, in two separate runs.
- Never mix paper and live trades in one calculation. Never sum P&L or trade counts across modes. A table may show them in separate columns. It may not show a total.
- The comparison in `both` mode uses rates and per-trade values only: win rate, profit factor, trades per session, slippage bps, decline rate, timeout rate, charges as a share of gross. It explains the cost basis (estimated vs actual) and the fill quality (sandbox price feed vs broker fills).
- If one mode has no data, write `comparison n/a - insufficient data` and give the other mode's report alone.
- How each mode is stored, which DB and table holds it, and what the data volume is: `references/data-source-map.md`. Read it first. At the last check there were 0 live strategy trades.

## 2. Reused infrastructure

| What | Where |
| --- | --- |
| Dashboard page | `frontend/src/pages/StrategyDailyPerformance.tsx` (route `react_strategy_daily_performance` in `blueprints/react_app.py`) |
| API | `blueprints/strategy_pnl.py`: `/api/strategy-pnl/daily`, `/daily/excursions`, `/compare`, `/trades/export` (login needed; the client never sends mode; the mode is the app toggle) |
| Orchestration | `services/strategy_daily_performance_service.py`: `get_daily_performance`, `get_strategy_comparison`, `get_excursion_quality` |
| Metric formulas | `services/strategy_metrics_service.py` (`compute_metrics`), `services/performance_stats.py` (Sharpe, Sortino, edge t-test, grades) |
| Per-trade enrichment | `services/strategy_trade_enrichment.py` (`enrich_trades`, `summarize_enrichment`, `symbol_breakdown`, `entry_hour_breakdown`): charges, R multiple, entry slippage |
| Trade ledger | `db/openalgo.db` table `strategy_closed_trades` (`mode='analyze'` or `'live'`). Reliable dates are source- and mode-specific: see section 3a |
| Signals, declines, EXIT rows | `signal_engine/data/trades.db` table `trades` (`trade_mode='analyze'` or `'live'`). EXIT rows carry `context.exit_types` |
| Skips before dispatch | `signal_engine/data/runner_state.db` table `shadow_events` (no mode column; `bar_epoch + 19800` = IST) |
| Paper fills and orders | `db/sandbox.db` tables `sandbox_trades`, `sandbox_orders` |
| Live fills | The broker. No fills table exists in the databases. Daily broker snapshots: `signal_engine/data/tradebook/` |
| Reconciliation | `python -m signal_engine.analysis.pages_vs_fills --mode analyze|live <day>` |
| Declines and ledger | `python -m signal_engine.analysis --since <day> --declined` |
| EVWAP-EARLY-PY detail | `python -m signal_engine.analysis.evwap_early_report --day <day>` (paper) |
| BREAKINGTRADE detail | `python -m signal_engine.analysis.breakingtrade.shadow --since <day>` (paper) |
| Execution incidents | `python -m signal_engine.analysis.execution_health --day <day> --format md` (no mode option; reads `trades.db` and the engine log) |
| Strategy list | `strategy_profiles` in `signal_engine/config.yaml` |
| Doc | `docs/strategy-daily-performance.md` |

Metric definitions: win rate = wins / trades. Profit factor = gross profit / gross loss. Expectancy = win rate x avg win - loss rate x avg loss.
R = realized P&L / (abs(signal entry - signal stop) x quantity), matched to the signal by strategy, symbol, direction, date and price.
Gross and net are separate: net = gross - charges.
Charges: every figure the script gives is an estimate from the Performance page schedule, in both modes. No actual-charge source exists in the repo databases. If the owner gives broker contract notes, use them for live and label them ACTUAL. Otherwise write "charges: estimated".
Positive entry slippage = fill worse than the alert price. Negative = better.

## 3. Step 1: discover active strategies (per mode)

1. Save the script in `references/query-recipes.md` to `$TMPDIR/sepa.py`. Run it with `--mode paper`, `--mode live` or `--mode both`.
2. A strategy is active in a mode when it has a closed trade, a signal, a decline or a timeout in that mode in the last 10 sessions with data. The script applies this rule.
3. A strategy in `strategy_profiles` with no row in the last 10 sessions of the mode is INACTIVE. List it with its last-seen date. Do not analyse it.
4. If the script prints `no <mode> data`, write that in the report (use `references/example-report-live-unavailable.md` as the pattern). Do not estimate or borrow data from the other mode.
5. Classify every weekday between the first session and the cut-off. Weekends and NSE holidays (`market_holidays`, type `TRADING_HOLIDAY`) are neither missing nor quiet. For other weekdays:

| Class | Rule | Meaning |
| --- | --- | --- |
| MISSING SESSION | no rows in the mode AND the engine log of that mode (`signal_engine/logs/signal_engine_<analyze or live>_<day>.log`) is missing or empty | The engine should have run. There is no proof it did. List each day and open an Investigate item. |
| NO-TRADE SESSION (quiet day) | the engine ran (log non-empty, or other rows exist that day) and this strategy had no signal or trade | Legitimate. Not a gap. Count it separately per strategy (`no_trade_sessions_in_window`). |
| OTHER-MODE DAY | the engine ran in the other mode that day | Not a session of this mode. |

6. Never count a MISSING SESSION as a quiet day or as a loss.

### 3a. Data-reliability start dates (source and mode specific)

State the start date for every source you use. Do not use one date for all.

| Source | Paper | Live |
| --- | --- | --- |
| `strategy_closed_trades` | 2026-09-25 (`LEDGER_RELIABLE_SINCE`) | 2026-09-25 is only the code floor. Not verified on live rows. Write "unknown" |
| `trades.db` | rows from 2026-09-21; used from 2026-09-25 | unknown |
| `sandbox.db` | from 2025-12-29 | not applicable |

Rows before the date are excluded. If the code constant changes, re-read it. If new live rows appear, check that their entry and exit prices match the broker records before you trust the floor, and write the result.

## 4. Step 2: pull metrics for comparable windows

Use three windows per strategy and mode. Default sizes:

| Window | Definition |
| --- | --- |
| recent | last 5 sessions that have data in the mode |
| previous | the 5 sessions before recent |
| all | every session since the reliable-since date of the mode (the strategy's own first trade date is its start) |

- If fewer than 10 sessions exist, use recent = last half, previous = first half. Say so.
- Set `--n` to change the window size. State the window dates in the report.
- Read these per window: trades, win rate, profit factor, gross, charges (estimated or actual, labelled), net after charges, expectancy in INR and in R (gross and net), avg win R, avg loss R, entry slippage in bps, max drawdown, trades per own session, declines by reason, timeouts.
- An empty window (0 trades) or a missing metric is written `n/a - insufficient data`. Never divide by zero. Never fill a gap with 0.
- Cross-check the newest day: `pages_vs_fills --mode analyze <cut-off>` (paper) or `--mode live <cut-off>` (live; needs live order tags). A FAIL is a BUG or EXECUTION item with the exact numbers.
- For the dashboard view of the same numbers, call `/api/strategy-pnl/compare?period=custom&start=...&end=...` in the browser (login). The dashboard shows only the mode the app is in. Do not read cookies.

## 5. Step 3: classify each metric

Counts here are strategy-specific and mode-specific. `strategy_sessions_total` = distinct days with at least one closed trade for that strategy in that mode, from the reliable-since date to the cut-off. Do not use the global session count. Do not use "sessions after the first trade". A paper count never counts toward a live label, and the reverse.

Sample-size rules (first match wins):

1. A window with fewer than 10 trades: NOT ASSESSED.
2. A window with 10 to 19 trades: LOW CONFIDENCE. Show the direction. Do not call it a trend.
3. A strategy with fewer than 20 `strategy_sessions_total` in that mode: no tuning recommendation (paper-review rule). Say "fewer than 20 sessions" and give the number.
4. The overall confidence label of a strategy uses its own session count and its own trade counts in that mode. A strategy with 20 sessions or more can still be LOW CONFIDENCE if its windows are small.

Thresholds (recent vs previous, both windows 20 trades or more):

| Metric | Improving | Deteriorating |
| --- | --- | --- |
| Win rate | up 10 points or more | down 10 points or more |
| Profit factor | up 25% or more | down 25% or more |
| Expectancy, net R | up 0.10 R or more | down 0.10 R or more |
| Trades per session | (descriptive) up 30% or more | down 30% or more |
| Entry slippage (bps) | down 3 bps or more (better) | up 3 bps or more (worse) |
| Max drawdown (INR) | smaller by 25% or more | larger by 25% or more |

Everything else is STABLE. If a window is NOT ASSESSED or empty, the metric is `n/a - insufficient data`. It is not STABLE.

Overall trend per strategy:

- IMPROVING: at least 2 of (win rate, profit factor, net expectancy R) improve and none deteriorates.
- DETERIORATING: at least 2 of those three deteriorate and none improves.
- Otherwise STABLE or MIXED.
- Check concentration: remove the best day and the worst day from the recent window (`net_gross_without_best_worst_day`). If the sign of net P&L changes, say "driven by one day".
- Compare recent with `all`. A recent window better than `all` but `all` still negative is "recovering, not proven".

## 6. Step 4: diagnose with trade-level data

For each strategy that is not STABLE or has net P&L below zero, collect:

1. Win/loss mix and avg win R vs avg loss R (script output).
2. Entry slippage in bps (script output). Compare with the previous window.
3. Exit-type mix (`exit_mix`). Large `NO-PROGRESS`, `SL`, `TRAILING_STOP` or `TIME` shares are EVIDENCE of how trades end.
4. Trade frequency per own session and LONG/SHORT split.
5. Per-symbol best and worst (`symbol_breakdown`). One symbol with one trade is noise. Say so.
6. Entry hour in IST (`entry_hour_breakdown`). `closed_at` and `opened_at` are IST already.
7. Daily P&L (`pnl_by_day`) to find one-day drivers.
8. Execution issues: declines by reason, `TIMEOUT` rows, paper `sandbox_orders` rejected count, `execution_health` verdict for the cut-off and the days in the window, `data_quality='execution_issue'` rows.
9. Market conditions: the repo has no stored regime tag. Use NIFTY direction only when you can read it from a source and name that source. Otherwise write "market regime: not available".

Label every statement:

- `EVIDENCE` plus the source (query, command, file and row or number).
- `HYPOTHESIS` for any cause you did not prove.
Never mix the two in one sentence. A cause needs data. A correlation across fewer than 20 trades is a HYPOTHESIS.

For live, put these first when data exists: actual fills (price, quantity, partial fills), realised P&L, actual charges (if the owner gave broker data; else estimated and labelled), slippage versus alert price, rejected and cancelled orders from the broker order book (not stored in the databases: ask the owner or use the broker page), declines and timeouts, `pages_vs_fills --mode live` verdict, `execution_health`, and any halt or mode-flip lines in the live engine log. Then the strategy metrics.

## 7. Step 5: cross-strategy buckets

Place each active strategy of a mode in one bucket (separately per mode):

- Consistent performer: STABLE, net expectancy R above 0 in recent, previous and all, each window 20 trades or more.
- Improving: IMPROVING by step 3.
- Deteriorating: DETERIORATING by step 3.
- Needs investigation: any of: a window under 20 trades, a recent window that contradicts `all`, a one-day driver, a reconciliation FAIL, an execution REGRESSION touching its fills, a MISSING SESSION inside its windows, or fewer than 20 own sessions in the mode.

If two rules fit, use Needs investigation and state both reasons.

## 8. Step 6: recommendations

For each strategy give: what is working, what is not working, what changed, why (EVIDENCE or HYPOTHESIS), and one action: Keep unchanged / Investigate / Fix (a bug) / Optimise (proposal only).

Rank by impact (INR per own session and share of the mode's total) then by confidence (sample size, evidence strength). Rank each mode separately.

Before any action, check the frozen rules:

1. Read `~/.claude/projects/-home-anand-github-openalgo/memory/MEMORY.md` and the strategy's project memory. Search for `frozen`.
2. Known at the last run: EVWAP-EARLY-PY (frozen for observation: bug fixes only), BREAKOUT v1.0 (frozen spec; inactive), BREAKINGTRADE and BREAKINGTRADE-RI (owner leave-as-is since 2026-10-28; re-evaluate after about 20 sessions with `breakingtrade.shadow`; no third variant), IDEA-034 recorder (frozen). Re-check, because this list ages.
3. Frozen strategy: allow only a bug fix or an investigation. Never recommend a parameter change.
4. A strategy with no freeze record (for example ORB, ORB-PY at the last run): say "no freeze record found" and ask the owner. **No freeze record is NOT permission to change anything.** An unfrozen strategy still gets no parameter change from this analysis alone. Any change needs owner approval and sample-size support (20 own sessions or more in the mode, and windows of 20 trades or more).
5. Overfitting guard: no parameter change from fewer than 20 own sessions in the mode. Do not tune on one best or worst symbol, hour or day. Give the sample size and the overfit risk with every optimise item.
6. Do not give parameter values. Name the area only.
7. Live: do not recommend any change to a live config, live parameter, live order or live risk limit as an action to apply. Write "for the owner to decide" and list the evidence. Paper results are not evidence for a live change on their own.
8. Record confirmed defects in the bug tracker per the `bug-tracker` skill (`uv run python -m signal_engine.bugs list --all` first). This is the owner's tracker. Do not edit it from this skill unless asked.

## 9. Step 7: report

Reply in chat. Save a file only when the owner asks. Plain headings, no emojis. Every report has a Limitations section.

```
Strategy performance analysis, mode <PAPER|LIVE|BOTH>, cut-off <date> (IST)

1. Overall assessment      2-4 sentences: how many active, how many in each bucket, total net after charges (per mode, never combined)
2. Scope and sources       mode, data cut-off, windows with dates, reliable-since date per source, sources, own sample sizes, missing sessions vs quiet days
3. Comparison table        one row per strategy: bucket, trades (recent/prev/all), win rate, PF, net expectancy R,
                           net after charges, slippage bps, confidence
4. Per-strategy findings   working, not working, what changed, EVIDENCE / HYPOTHESIS lines, frozen status
5. Mode section            see below
6. Significant changes     the largest moves with numbers
7. Likely explanations     each one labelled HYPOTHESIS unless proven
8. Recommendations         ranked: action, strategy, impact, confidence, frozen check, overfit risk
9. Limitations             see section 10
10. Plain-words summary    4-8 sentences for a trader with no coding background
Bottom Line               last section of the chat reply
```

Mode section (item 5):

- paper: paper performance and estimated costs, plus the differences from expected live execution (fill model, slippage, rejections, charges). Label the live side HYPOTHESIS unless live data exists.
- live: actual realised performance, actual costs where available, execution quality (fills, slippage, rejects, reconciliation) and operational risk first. Put this section before the strategy metrics when the live data is large enough to matter.
- both: the paper assessment, the live assessment, then the comparison: rates and execution differences only, cost basis (estimated vs actual) and fill quality (sandbox feed vs broker). No combined totals.

The chat reply must end with `Bottom Line`:

- Plain English. 3 to 5 bullets or under 100 words.
- Cover: effect on trading, signals, sandbox and live; decisions needed from the owner; next steps; risk (harmless / operational / trading).
- No commit hashes, file names or test counts in it.

## 10. Limitations (state these in every report)

- Paper fills come from the sandbox price feed. Live fills differ in slippage, queueing and rejections.
- Samples are small. Few strategies reach 20 own sessions or 20 trades per window. Sharpe and Sortino need 60 sessions. This skill does not report them for short samples.
- Charges are estimates in both modes. No actual-charge source exists in the repo databases. The ledger holds realised P&L per closing fill. A partial exit is a separate row.
- R needs a matched signal stop. Trades without one are excluded (`r_covered` shows how many are used).
- No market regime tag exists in the stored data. Regime effects are HYPOTHESIS.
- Session classes come from rows and engine logs. An empty or missing log is evidence that the engine did not run, not proof of the cause. Check each MISSING SESSION.
- Reliable-since dates differ by source and mode (section 3a). Earlier rows are excluded. The live date is unknown.
- Live rejects and fills are not in the databases. The live part of a report depends on broker data the owner supplies.
- `execution_health` has no mode option. Its output is paper-based unless live rows exist.
- The `signal_engine.analysis` "execution quality" exit slippage block mixes order types. Do not use it as proof.
- Not computed: MAE/MFE (lazy endpoint, needs bar reads), per-trade intraday charts, portfolio correlation.
- Shadow and dashboard numbers differ in basis (shadow adds 4 bps per side slippage). Name the basis when you quote both.
