/**
 * What the Order Book and Scalping screens show after "Cancel all".
 *
 * The API contract (docs/api/order-management/cancelallorder.md) answers a partial cancel with
 * status "success" and a non-empty failed_cancellations list: the request was processed, but those
 * orders are still working. That must never read as a completed cancel, so a non-empty list is an
 * error whatever the status says, and the order list is refreshed so the orders left working are
 * visible (incident 2026-10-12).
 */
export type CancelAllToast = 'success' | 'info' | 'error'

export interface CancelAllOutcome {
  toast: CancelAllToast
  message: string
  refresh: boolean
}

export interface CancelAllBody {
  status?: string
  message?: string
  failed_cancellations?: unknown[]
}

/** Outcome of a cancel-all request that returned a body (any HTTP status). */
export function cancelAllOutcome(body: CancelAllBody | undefined): CancelAllOutcome {
  const failed = body?.failed_cancellations?.length ?? 0
  if (failed > 0) {
    return {
      toast: 'error',
      message: body?.message || `Failed to cancel ${failed} order(s)`,
      refresh: true,
    }
  }
  if (body?.status === 'success') {
    return { toast: 'success', message: body.message || 'All orders cancelled', refresh: true }
  }
  if (body?.status === 'info') {
    return { toast: 'info', message: body.message || 'No open orders to cancel', refresh: false }
  }
  return { toast: 'error', message: body?.message || 'Failed to cancel all orders', refresh: true }
}
