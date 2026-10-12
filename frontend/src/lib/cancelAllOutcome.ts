/**
 * What the Order Book shows after "Cancel all".
 *
 * A failed or partial cancel-all (HTTP 500 with failed_cancellations, since 2026-10-12) must still
 * refresh the order list: the orders that could not be cancelled are still working, and the trader
 * has to see them. Before, only a full success refreshed, so stops left working stayed out of view.
 */
export type CancelAllToast = 'success' | 'info' | 'error'

export interface CancelAllOutcome {
  toast: CancelAllToast
  message: string
  refresh: boolean
}

interface CancelAllBody {
  status?: string
  message?: string
}

/** Outcome of a cancel-all request that returned a body (any HTTP status). */
export function cancelAllOutcome(body: CancelAllBody | undefined): CancelAllOutcome {
  if (body?.status === 'success') {
    return { toast: 'success', message: body.message || 'All orders cancelled', refresh: true }
  }
  if (body?.status === 'info') {
    return { toast: 'info', message: body.message || 'No open orders to cancel', refresh: false }
  }
  return { toast: 'error', message: body?.message || 'Failed to cancel all orders', refresh: true }
}
