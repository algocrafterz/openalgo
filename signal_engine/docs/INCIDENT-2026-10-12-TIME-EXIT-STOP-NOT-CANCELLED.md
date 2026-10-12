# Incident 2026-10-12: a stop left working after the 14:45 time exit opened a position

Mode: ANALYZE (paper, OpenAlgo sandbox). Strategy: EVWAP-EARLY-PY (the defect affected every strategy).

## What happened (IST)

| Time | Event |
| --- | --- |
| 13:05:23 | EVWAP-EARLY-PY SHORT PAYTM 93 @ 1650.10; broker SL-M BUY 93 placed 13:05:24 (id 26101208861159) |
| 14:45:04 | Time exit: `cancel_all_orders` then `close_all_positions` per strategy |
| 14:45:06 | Untagged square-off BUY 93 flattens PAYTM. The SL-M is NOT cancelled |
| 14:45:10 | `tracker._confirm_orders_cancelled` reads the order book: 11 SL-M orders still working (ORB, BREAKINGTRADE, BREAKINGTRADE-WATCHLIST, EVWAP-EARLY-PY) |
| 14:45:10-15 | Three re-cancels through `cancel_all_orders`: none takes effect |
| 14:45:17 | CRITICAL log and Telegram alert listing all 11 orders |
| 15:12:51 | PAYTM SL-M BUY 93 triggers on the flat position: an unintended LONG 93 |
| 15:15:00 | Sandbox MIS auto square-off sells 93 and cancels the other 10 stops. Loss on the unintended long: Rs 260.40 |

## Root cause

OpenAlgo core. `services/sandbox_service.py` `sandbox_cancel_all_orders` selected orders with status in
`["open", "pending", "trigger_pending"]` (underscore), while `sandbox/order_manager.py` stores SL/SL-M orders
as `"trigger pending"` (space), introduced by upstream commit 3e7132a52 (2026-07-24) without updating the
filter. Cancel-all therefore skipped every stop and answered success ("No open orders to cancel"). Since
2026-07-24 every 14:45 time exit left all stops working until the 15:15 sandbox square-off.

## Detection

The order-book confirmation added on 2026-10-09 (`cb9a36cc5`) detected the working stops and alerted. It did
not prevent the fill, because its re-cancels used the same broken cancel-all.

## Fixes

1. signal_engine `tracker._confirm_orders_cancelled`: cancels each working order by its order id
   (`cancel_order`), which the sandbox accepts for "trigger pending"; cancel-all only when the order book is
   unreadable. Re-reads the book after each pass; CRITICAL log and alert stay when it cannot be confirmed.
2. OpenAlgo core `services/sandbox_service.py`: cancel-all selects "trigger pending" orders and reports
   failure when any working order could not be cancelled (separate commit, owner-approved core change).

## Follow-up

- Telegram delivery of the 14:45:17 alert: no send failure was logged; owner to confirm receipt.
- Check the time-exit log daily for "still working" until several clean sessions are seen.
