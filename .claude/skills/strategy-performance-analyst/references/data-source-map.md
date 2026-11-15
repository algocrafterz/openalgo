# Data-source map: paper versus live

Checked by reading the code and by read-only queries on 2026-11-15. Do not assume. Re-check if the code changes.

## How the two modes are stored

There is no `is_sandbox` column. The mode is a text value. Paper = `analyze`. Live = `live`.
The mode of the whole OpenAlgo app comes from one toggle (`get_analyze_mode()`). It is not per strategy.
The engine takes the phase from OpenAlgo (`signal_engine/mode_guard.py`). A mid-session flip halts new entries. It does not relabel old rows.

| Item | Paper | Live |
| --- | --- | --- |
| Closed-trade ledger | `db/openalgo.db` `strategy_closed_trades` where `mode='analyze'` (1,537 rows, 8 strategies, 2026-09-08 to 2026-11-13) | same table where `mode='live'`: **0 rows** (also 0 in the 5 older backups of the file that were checked) |
| Open legs | `strategy_positions.mode='analyze'` (356 rows; 44 rows are `unknown`) | `mode='live'`: 0 rows |
| Order tags (order id to strategy) | `strategy_order_tags.mode='analyze'` (2,552 rows) | `mode='live'`: 0 rows. `pages_vs_fills --mode live` needs these tags |
| Signals, declines, timeouts, EXIT rows | `signal_engine/data/trades.db` `trades.trade_mode='analyze'` (3,826 rows: SUCCESS 2,920, DECLINED 861, TIMEOUT 45) | `trade_mode='live'`: 0 rows. Older rows (221, 2026-03-09 to 2026-08-23) have `trade_mode` NULL and exist only in the backups `trades.db.bak-*`. Their mode is not recorded. Do not use them as live |
| Skips before dispatch | `signal_engine/data/runner_state.db` `shadow_events` (no mode column; engine-wide) | same table, no mode column. Cannot be split by mode |
| Fills | `db/sandbox.db` `sandbox_trades` (3,176) and `sandbox_orders` (4,480: complete 3,175, cancelled 1,305, rejected 0) | The broker. The engine saves a daily broker tradebook snapshot in `signal_engine/data/tradebook/tradebook_<day>.json` (36 files, 2026-09-21 on). No live fills table exists in the databases |
| Order and reject log | `order_logs` (api_type `placeorder`, `orderstatus`, ...) in `db/openalgo.db`. It has no mode column. `sandbox_orders.rejection_reason` for paper rejects | Same `order_logs`. No mode column, so it cannot be split by mode. The broker order book (API) is the only place for live rejects |
| Actual charges | none. Estimated only | none in the databases (no contract-note or charges table). Estimated only unless the owner supplies broker data |
| Estimated charges | `services/strategy_trade_enrichment.py` `estimate_trade_costs` (Performance page schedule; entry brokerage once per position) | same function, same schedule. It keys on instrument and product, not on mode |
| Slippage | `enrich_trades`: ledger entry price versus the matched signal entry (alert price) | same code. For live the ledger entry is a broker fill |
| Engine log | `signal_engine/logs/signal_engine_analyze_<day>.log` | `signal_engine_live_<day>.log`. 19 files exist (2026-10-22 to 2026-11-15). 17 are 0 bytes. 2026-10-31 (6.9 KB) and 2026-11-02 (572 bytes) hold only start-up lines, including `EVWAP-EARLY-PY is paper-only and OpenAlgo is in live mode, so it is NOT started`. No trading lines |

## Services and tools by mode

| Tool | Paper | Live |
| --- | --- | --- |
| `blueprints/strategy_pnl.py` (`/api/strategy-pnl/*`) and `services/strategy_daily_performance_service.py` | Used when the app toggle is analyze. The client never sends mode. | Used when the toggle is live. The mode is `_current_mode()`. You cannot ask for the other mode through the API. The analysis script reads the tables directly instead. |
| `database.strategy_book_db.get_closed_trades(mode=...)` | `mode='analyze'` | `mode='live'` (parameter exists) |
| `services/strategy_metrics_service.py` `LEDGER_RELIABLE_SINCE` | 2026-09-25. One constant. The page filters on it for every mode. | Same constant applies. There is no live-specific reliability test. Treat the live date as unknown until the first live rows are validated. |
| `signal_engine.analysis.pages_vs_fills --mode analyze\|live <day>` | Compares sandbox fills with the page | Compares the broker tradebook snapshot (tagged by `strategy_order_tags` `mode='live'`) with the page. Needs live tags. None exist. |
| `signal_engine.analysis.execution_health --day <day>` | Reads `trades.db` and the engine log. No `--mode` option. | Same inputs. Do not read its output as live unless `trades.db` holds live rows for that day. |
| `signal_engine.analysis --since <day> --declined` | Splits buckets by `trade_mode` | Same. NULL mode rows show as `unknown`. |
| `evwap_early_report`, `breakingtrade.shadow` | Paper strategies. Shadow adds 4 bps per side slippage. | Not applicable until a strategy runs live. |

## Reliability start dates (source and mode specific)

| Source | Paper | Live |
| --- | --- | --- |
| `strategy_closed_trades` | 2026-09-25 | 2026-09-25 by the code floor, but not verified (no rows). Unknown. |
| `trades.db` | rows from 2026-09-21; the skill uses 2026-09-25 to match the ledger | unknown (no rows) |
| `sandbox.db` | complete from 2025-12-29 (first order) | not applicable |
| Broker tradebook snapshots | not applicable | files exist from 2026-09-21; not opened. No live tags or rows exist to match them to |

## Live data volume

Live strategy trades found: **0**. No live closed trades, no live signals, no live order tags, no live positions.
The only live evidence is start-up log lines on 2026-10-31 and 2026-11-02. A live analysis today can only report this limitation.
