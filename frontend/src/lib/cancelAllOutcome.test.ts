import { describe, expect, it } from 'vitest'
import { cancelAllOutcome } from './cancelAllOutcome'

describe('cancelAllOutcome', () => {
  it('refreshes after a full success', () => {
    expect(
      cancelAllOutcome({ status: 'success', message: 'Successfully canceled 3 orders' })
    ).toEqual({
      toast: 'success',
      message: 'Successfully canceled 3 orders',
      refresh: true,
    })
  })

  it('refreshes after a partial failure so the orders still working are visible', () => {
    const outcome = cancelAllOutcome({
      status: 'error',
      message: 'Canceled 2 orders. Failed to cancel 1 orders.',
    })
    expect(outcome.toast).toBe('error')
    expect(outcome.refresh).toBe(true)
    expect(outcome.message).toBe('Canceled 2 orders. Failed to cancel 1 orders.')
  })

  it('refreshes after a request that failed without a body', () => {
    expect(cancelAllOutcome(undefined)).toEqual({
      toast: 'error',
      message: 'Failed to cancel all orders',
      refresh: true,
    })
  })

  it('does not refresh when there was nothing to cancel', () => {
    expect(cancelAllOutcome({ status: 'info' }).refresh).toBe(false)
  })

  it('treats a documented partial success (status success, failures listed) as an error', () => {
    const outcome = cancelAllOutcome({
      status: 'success',
      message: 'Successfully canceled 2 orders (Failed to cancel 1 orders)',
      failed_cancellations: [{ orderid: 'SL1', message: 'boom' }],
    })
    expect(outcome).toEqual({
      toast: 'error',
      message: 'Successfully canceled 2 orders (Failed to cancel 1 orders)',
      refresh: true,
    })
  })

  it('names the failure count when the body has no message', () => {
    expect(cancelAllOutcome({ status: 'success', failed_cancellations: [{}, {}] }).message).toBe(
      'Failed to cancel 2 order(s)'
    )
  })
})
