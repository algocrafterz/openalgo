# Runbook: broker position with no entry row (untracked position)

> **LIMITATION - read before step 3B and before integrating `fix/entry-row-on-accept`.**
> Restart attribution is reliable for a symbol traded by ONE strategy that day. When several
> strategies trade the same symbol, splitting the broker position back into one tracked position per
> strategy depends on fix **F3** (`startup._direction_name`) in branch **#3**
> (`fix/audit-fixes-2026-10-22`). Without F3 the side never matches, the per-strategy split never
> runs, and the restore falls back to one merged position under one strategy. Integrate #3 before
> relying on 3B for a multi-strategy symbol.
>
> **Tested 2026-10-23:** step 3B, run as written (placeholders filled) against a temporary
> trades.db for a single-strategy PFC long of 930 (BREAKOUT-PY, entry order `ENTRY-1`, average
> fill 326.10), wrote exactly one entry row (`row id 1`), and the restart lookup
> (`startup._lookup_entry_legs("PFC", broker_qty=930, is_long=True)`) found it with order id
> `ENTRY-1`, quantity 930 and side LONG. No production database was touched.

Applies after branch `fix/entry-row-on-accept`. The engine writes the entry row the moment the
broker accepts an order, so an engine position without a row means that write failed twice, or the
position was not opened by the engine. Either way the engine is not managing it.

**The account-wide square-offs are not the fix.** The 14:45 engine time exit and the 14:55
`openAlgoSquareOff` task call OpenAlgo's close-all, which happens to close every open position, and
the broker squares MIS off itself. They only limit damage at the end of the day. Until then the
position has no take-profit, no trailing, no no-progress exit and no ledger record. Act on the alert
the same session.

## 1. How it is detected

| When | Signal | Where |
| --- | --- | --- |
| At entry, both writes failed | `ENTRY NOT RECORDED: <SYM> [<STRATEGY>] order <ID> is open at the broker but trades.db could not record it` | Telegram admin channel; CRITICAL in `signal_engine/logs/errors_<DAY>.jsonl` |
| At any engine restart | `RESTART: broker positions with no entry trade in trades.db today: <SYM> <+/-QTY>, ...` (event `untracked_broker_position`) | Telegram admin channel; CRITICAL `Tracker restore [<SYM>]: broker holds <QTY> but there is no entry trade in trades.db today` |
| At EOD | the symbol's fills listed as broker fills with no matching engine order | `reports/eod-<DAY>.md` |

Both alerts are failure events: they are delivered at every `notify_level`, including `alerts`.

## 2. Identify the position (read-only)

1. Broker side: OpenAlgo **Positions** page (or `positionbook` API): symbol, product, net quantity,
   average price. **Order book**: the entry order id from the alert, and any working SL / SL-M order on
   the symbol with its strategy tag and trigger price.
2. Engine side, confirm there is no entry row:

   ```bash
   sqlite3 "file:/home/anand/github/openalgo/signal_engine/data/trades.db?mode=ro" \
     "SELECT id, strategy, direction, status, order_id, quantity, executed_at FROM trades
      WHERE symbol = '<SYM>' AND date(executed_at) = date('now', 'localtime') ORDER BY id"
   ```

3. Classify:
   * The order id is one the engine sent (strategy tag of a configured strategy, matches a `Parsed
     signal` / `Order placed for` log line): an engine entry whose row was lost. Continue with 3A or 3B.
   * Otherwise: a manual or external trade. The engine is right not to manage it; handle it at the
     broker and stop here.

## 3. Reconcile (choose one; both change production state and need the operator's decision)

### 3A. Flatten (default, same session)

1. Cancel the symbol's working stop order first (a stop left on a flat position can open a new one).
2. Close the position (OpenAlgo Positions page, close that symbol only).
3. Confirm in the order book: no working order on the symbol; in the positions page: quantity 0.
4. Note the order ids in the day's EOD review; the ledger keeps showing them as unmatched fills,
   which is correct (no engine trade record exists).

### 3B. Re-adopt (keep the trade under engine management)

Restart restores a position only from a matching SUCCESS entry row: same side and the exact broker
quantity (`startup._lookup_entry_legs`). Write that row with the engine's own writer, then restart.

1. From the order book take: strategy tag, side, quantity, average fill price, entry order id, the
   stop's trigger price; from the original alert or signal log take the signal entry and target.
2. Write the row (nothing else should be writing trades.db at that moment):

   ```bash
   cd /home/anand/github/openalgo && PYTHONPATH=. uv run python - <<'PY'
   from signal_engine import db
   from signal_engine.models import Action, Direction, Order, OrderStatus, Signal, TradeResult
   db.set_trade_mode("analyze")          # or "live": must match OpenAlgo's mode
   sig = Signal(strategy="<STRATEGY>", direction=Direction.LONG, symbol="<SYM>",
                entry=<SIGNAL_ENTRY>, sl=<STOP_TRIGGER>, tp=<TARGET_OR_0>,
                raw_message="manual re-adoption: entry row lost (runbook 3B)")
   order = Order(symbol="<SYM>", exchange="NSE", action=Action.BUY, quantity=<BROKER_QTY>,
                 price=0.0, order_type="MARKET", product="MIS", strategy_tag="<STRATEGY>")
   result = TradeResult(order_id="<ENTRY_ORDER_ID>", status=OrderStatus.SUCCESS,
                        message="re-adopted after lost entry row")
   result.fill_price = <AVG_FILL>
   print("row id", db.save(sig, order, result))
   PY
   ```

   For a short, use `Direction.SHORT` and `Action.SELL`. A printed `row id None` means the write
   failed again: stop and use 3A.
3. Restart the engine through the normal control script. Expect in the log:
   `Tracker restored [<SYM>:<STRATEGY>]: qty=<QTY> entry=... sl=... tp=... sl_order=<ID>` and no
   `RESTART: broker positions with no entry trade` alert for the symbol.
4. If the stop was not recovered (`sl_order=UNKNOWN`), the next poll re-places it
   (`_retry_missing_sl`); confirm one working stop in the order book, not two.

## 4. Afterwards

* Find why both writes failed (`errors_<DAY>.jsonl`: `Failed to save trade`), for example a locked
  database or a full disk, before the next session.
* Record the incident and the chosen path in the EOD review.
