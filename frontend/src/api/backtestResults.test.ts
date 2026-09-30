import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({ get: vi.fn() as unknown as (...args: unknown[]) => unknown }))
const get = client.get as unknown as ReturnType<typeof vi.fn>
vi.mock('./client', () => ({ webClient: client }))

/**
 * Swaps in a plain (non-spy) function that rejects: vitest's spy wrapper re-throws a rejected
 * result and reports it as an unhandled rejection.
 */
const rejectWith = (error: unknown) => {
  client.get = () => Promise.reject(error)
}

import { backtestResultsApi } from './backtestResults'

describe('backtestResultsApi', () => {
  beforeEach(() => {
    client.get = get
    get.mockReset()
  })

  it('list returns the response body', async () => {
    get.mockResolvedValue({ data: { status: 'success', ideas: [] } })
    const res = await backtestResultsApi.list()
    expect(get).toHaveBeenCalledWith('/api/backtest-results')
    expect(res.status).toBe('success')
  })

  it('idea encodes the id into the path', async () => {
    get.mockResolvedValue({ data: { status: 'success' } })
    await backtestResultsApi.idea('IDEA-001')
    expect(get).toHaveBeenCalledWith('/api/backtest-results/IDEA-001')
    await backtestResultsApi.idea('a/b')
    expect(get).toHaveBeenLastCalledWith('/api/backtest-results/a%2Fb')
  })

  it('compare sends both run ids as query params', async () => {
    get.mockResolvedValue({ data: { status: 'success' } })
    await backtestResultsApi.compare('IDEA-001', 'run one', 'run-2')
    expect(get).toHaveBeenCalledWith('/api/backtest-results/IDEA-001/compare?a=run+one&b=run-2')
  })

  it('recovers the JSON error body from a rejected request', async () => {
    rejectWith({ response: { data: { status: 'error', message: 'not found' } } })
    expect(await backtestResultsApi.idea('IDEA-999')).toEqual({
      status: 'error',
      message: 'not found',
    })
  })

  it('falls back to a generic message on a network failure', async () => {
    rejectWith(new Error('offline'))
    expect(await backtestResultsApi.list()).toEqual({
      status: 'error',
      message: 'Failed to load backtests',
    })
    expect((await backtestResultsApi.idea('IDEA-001')).message).toBe(
      'Failed to load backtest history'
    )
    expect((await backtestResultsApi.compare('IDEA-001', 'a', 'b')).message).toBe(
      'Failed to compare runs'
    )
  })
})
