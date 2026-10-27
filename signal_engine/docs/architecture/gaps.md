# Architecture gaps, discrepancies and proposals (2026-10-22)

Everything here is either verified (evidence cited) or labelled as a hypothesis. "Fixed (branch)"
means fixed on `review/signal-engine-audit-2026-10-22`, not merged or deployed. "Proposed" means not
implemented: it needs an owner decision. The full audit report with severities is
`../AUDIT-2026-10-22.md`.

## Undocumented or wrong behavior found

| ID | Gap | Evidence | Status |
| --- | --- | --- | --- |
| G1 | Runner warm-up reads only `db/historify.duckdb`; the 15:35 backfill skips symbols that are 1 day stale (`min_stale=2`), so every other trading day the runner starts without yesterday's session and silently uses the session before as "previous" | Backfill log fetch pattern 09-29..10-22; BREAKOUT-PY PFC 2026-10-14 entry carried 10-12's PVAH/PPOC/PVAL (333.875/333.125/332.375), 10-13's are 337.375/336.625/334.375 | Fixed (branch, 2026-10-23): daily backfill (`--daily`) and warm-up validation in `HistorifyLoader` |
| G2 | Restart attribution never matched a side: `str(Direction.LONG)` is `"Direction.LONG"` on Python 3.12, so the multi-strategy leg split never ran | `test_startup_direction_attribution.py` (3 tests red before the fix) | Fixed (branch) |
| G3 | Time-exit verification read `-1` as "API error, treat as closed"; a one-share short is also `-1` | `fetch_open_position` contract; only caller was `tracker._verify_positions_closed` | Fixed (branch): `fetch_net_position`, unread positions alerted |
| G4 | `TimeExitScheduler` and tracker tasks are unawaited `create_task`s; an exception ended the scheduler silently | `startup._run_engine`, no supervision | Fixed (branch) for the scheduler; the tracker poll was already guarded |
| G5 | Windows watchdog and heartbeat tasks miss the whole day when the PC wakes after 09:00 | Task Scheduler event 153 on 10-19, 10-20, 10-22; wake times 09:23, 12:36, 10:19 | Fixed (branch) in `createTaskOpenAlgoScheduler.ps1`; live tasks unchanged until re-registered |
| G6 | `backfill --verify` could not see a symbol never stored for an interval (M&M, NIFTY have no daily bars) | read-only store query | Fixed (branch) |
| G7 | Entry row is written after stop placement, fill fetch and bad-fill guard; a crash in that window leaves a broker position with no `trades.db` row, which restart cannot restore | `main._handle_entry` / `_establish_position` order | Proposed (P2) |
| G8 | `backtest/volume_profile.py` profiled the closing-auction print, and claimed a flat Rs 0.05 NSE tick | code and the 2026-09-30 continuous-session decision | Fixed (branch) |
| G9 | Two value-area implementations with different methods (range-spread Pine port vs close-binned library) | `features/value_area.py`, `backtest/volume_profile.py` | Proposed: consolidate on the Pine port (P3) |
| G10 | Backfill failures reached only a log file | `backfill_cron.sh` | Fixed (branch): Bot API alert |
| G11 | `eod.sh` header says cron 15:45; crontab runs it 15:25 (before the 15:30 close); crontab comment says AutoStop 15:30, the real task is 16:00 | `crontab -l`, `eod.sh`, task definitions | Proposed: move to 15:45 and the claude review to 15:55 (needs approval) |
| G12 | `weekly.sh` documents a cron that does not exist; last weekly report 2026-09-24 | `crontab -l`, `reports/` | Proposed: retire or reinstate (owner decision) |
| G13 | Tests read real local databases: `test_flattrade_transform` (SymToken tick from `db/openalgo.db`), backfill calendar test (`db/historify.duckdb`) | both fail in a fresh worktree | Calendar test fixed (branch); flattrade test is OpenAlgo core, left alone |
| G15 | The 14:45 time exit and its retry flatten the whole broker account, not the tracked strategies. `close_all_positions(strategy)` calls `closeposition` with no symbol, and `cancel_all_orders(strategy)` calls `cancelallorder`; OpenAlgo ignores the strategy tag for both. Every open position in every product (CNC and NRML included) is closed under the tag `Squareoff`, and every working order is cancelled, so CNC, manual and other tools' positions and orders are affected too. `time_exit_all`'s docstring ("CNC positions are excluded") is true only of the tracker. Today every configured strategy is MIS, so the flatten closes what it should; the exposure is anything the tracker does not own. Strategy-level verification alone is unsafe with these calls: during the exit every tracked strategy on a symbol stays registered until booking, so `fetch_own_position` (net minus the other strategies' tracked quantity) reads a correctly flat symbol as open whenever two strategies held it (BREAKOUT +10, BREAKOUT-PY +5: net 0, own = -5), giving false retries, a false TIME EXIT FAILED alert and an unconfirmed booking | `api_client.close_all_positions`/`cancel_all_orders`; `services/close_position_service.py` and `sandbox_service.sandbox_close_position` (no symbol: loop every open position); `broker/flattrade/api/order_api.close_all_positions` (every `netqty != 0`, any `prd`); `services/cancel_all_order_service.py` (every open order); review of fix stack 2026-10-24 | Documented, not fixed (owner decision 2026-10-24): see proposal 6 |
| G14 | `../HOW-IT-WORKS.md` describes ORB-era values (5 positions, 12 trades a day, 15:00 time exit) that no longer match `config.yaml` (2 positions, 10 trades, 14:45) | doc vs config | Documentation debt; not changed |

## Proposals that need approval (not implemented)

1. **G1, runner freshness.** Implemented on the branch 2026-10-23 (both parts, as approved): daily
   backfill and fail-closed warm-up validation. Not implemented: fetching a missing session from
   the OpenAlgo history API at warm-up instead of excluding the symbol. Open: the 2026-11-08 Sunday
   Muhurat session (cron runs Mon-Fri, so Monday 11-09 would exclude every symbol unless a backfill
   runs on 11-08 evening).
2. **G7.** Write the entry row immediately after `send_order` returns SUCCESS, then update it with
   the fill and stop id. Changes persistence order and the `trades.db` row lifecycle.
3. **Time-exit unread policy (G3 follow-up).** Today an unreadable position is treated as closed
   (now alerted). Alternative: treat it as unconfirmed, retry the square-off, write no closing row.
4. **G11 schedule.** `eod.sh` at 15:45, `eod_claude_review.sh` at 15:55 (before the 16:00 stop).
5. **Telegram.** See the audit report, Report A: staged `notify_level` change, no code removal.
6. **G15, per-position time-exit square-off (follow-up, not started).** Replace both account-wide
   calls in `_square_off_strategies` and the `_verify_positions_closed` retry. Cancel only the
   tracked strategies' working orders, by order id. Send one MARKET exit per (symbol, exchange,
   product) group for exactly the tracked signed quantity. Verify each group against its untracked
   baseline (net before the exit minus the tracked quantity), and retry only the residual. Booking
   then uses the known order ids. Requirements before deployment: regression tests with two
   strategies on one symbol and with an untracked position on that symbol, paper-mode validation
   over several sessions, and a deployment separate from any OpenAlgo upgrade so a behavior change
   can be attributed. Changes the 14:45 exit mechanism, so it needs approval of the design first.

## How to regenerate

There is no generator: the diagrams are hand-written Mermaid derived from the files named in each
node. When code named in a diagram changes, update the diagram in the same commit. To re-verify the
schedules: `crontab -l`, and in PowerShell `Get-ScheduledTask | ? TaskPath -notlike '\Microsoft\*'`.
