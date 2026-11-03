/**
 * The Performance API refuses to turn "nothing selected" into a whole-portfolio
 * request. Catches an implementation that drops the none flag when it builds
 * the query string, which would show the whole portfolio under an empty choice.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

const get = vi.fn()
const post = vi.fn()
vi.mock('./client', () => ({
  webClient: { get: (...a: unknown[]) => get(...a), post: (...a: unknown[]) => post(...a) },
}))

const { strategyPnlApi } = await import('./strategyPnl')

const NONE = { include: [], exclude: [], none: true }

beforeEach(() => {
  vi.clearAllMocks()
  get.mockResolvedValue({ data: { status: 'success' }, headers: {} })
})

function sentParams(): URLSearchParams {
  const url = String(get.mock.calls[0][0])
  return new URLSearchParams(url.split('?')[1])
}

describe('a filter with nothing selected', () => {
  it('rejects daily without calling the server', async () => {
    await expect(strategyPnlApi.daily(NONE, '1d')).rejects.toThrow('Select at least one strategy.')
    expect(get).not.toHaveBeenCalled()
  })

  it('rejects compare without calling the server', async () => {
    await expect(strategyPnlApi.compare('1d', undefined, NONE)).rejects.toThrow(
      'Select at least one strategy.'
    )
    expect(get).not.toHaveBeenCalled()
  })

  it('rejects exportTrades without calling the server', async () => {
    await expect(strategyPnlApi.exportTrades(NONE, '1d')).rejects.toThrow(
      'Select at least one strategy.'
    )
    expect(get).not.toHaveBeenCalled()
  })

  it('still allows compare with no filter, the whole-portfolio list request', async () => {
    await strategyPnlApi.compare('1d', undefined, undefined)
    expect(get).toHaveBeenCalledTimes(1)
    expect(sentParams().has('strategy')).toBe(false)
  })
})

describe('daily request parameters', () => {
  it('sends repeated strategy and exclude params and no select param', async () => {
    await strategyPnlApi.daily({ include: ['A', 'B'], exclude: ['C', 'D'] }, '7d')
    const params = sentParams()
    expect(params.getAll('strategy')).toEqual(['A', 'B'])
    expect(params.getAll('exclude')).toEqual(['C', 'D'])
    expect(params.has('select')).toBe(false)
  })

  it('sends no strategy params for the legacy null selection', async () => {
    await strategyPnlApi.daily(null, '1d')
    const params = sentParams()
    expect(params.has('strategy')).toBe(false)
    expect(params.has('exclude')).toBe(false)
  })

  it('sends one strategy param for a legacy name', async () => {
    await strategyPnlApi.daily('ORB', '1d')
    expect(sentParams().getAll('strategy')).toEqual(['ORB'])
  })
})

describe('capital parameter', () => {
  it('is sent on daily only when set', async () => {
    await strategyPnlApi.daily(null, '7d', undefined, 1000000)
    expect(sentParams().get('capital')).toBe('1000000')
  })

  it('is left out of daily when absent or null', async () => {
    await strategyPnlApi.daily(null, '7d')
    expect(sentParams().has('capital')).toBe(false)
    get.mockClear()
    await strategyPnlApi.daily(null, '7d', undefined, null)
    expect(sentParams().has('capital')).toBe(false)
  })

  it('is never sent by compare or export', async () => {
    await strategyPnlApi.compare('7d')
    expect(sentParams().has('capital')).toBe(false)
    get.mockClear()
    await strategyPnlApi.exportTrades(null, '7d')
    expect(sentParams().has('capital')).toBe(false)
  })
})

describe('evaluation calls', () => {
  const FILTER = { include: ['B', 'A'], exclude: ['C'] }

  beforeEach(() => {
    post.mockResolvedValue({ data: { status: 'success' } })
  })

  it('posts the selection as JSON to prepare', async () => {
    await strategyPnlApi.prepareEvaluation(FILTER, '30d')
    expect(post).toHaveBeenCalledTimes(1)
    const [url, body] = post.mock.calls[0]
    expect(url).toBe('/api/strategy-pnl/evaluation/prepare')
    expect(body).toEqual({ period: '30d', strategy: ['B', 'A'], exclude: ['C'] })
  })

  it('sends capital only when it is set', async () => {
    await strategyPnlApi.prepareEvaluation(FILTER, '30d', undefined, 500000)
    expect(post.mock.calls[0][1]).toMatchObject({ capital: 500000 })
    post.mockClear()
    await strategyPnlApi.prepareEvaluation(FILTER, '30d', undefined, null)
    expect('capital' in post.mock.calls[0][1]).toBe(false)
  })

  it('sends the dates only for a custom period', async () => {
    const range = { start: '2026-01-01', end: '2026-01-31' }
    await strategyPnlApi.prepareEvaluation(FILTER, 'custom', range)
    expect(post.mock.calls[0][1]).toMatchObject(range)
    post.mockClear()
    await strategyPnlApi.prepareEvaluation(FILTER, '7d', range)
    expect('start' in post.mock.calls[0][1]).toBe(false)
  })

  it('leaves out empty include and exclude lists and never sends the mode', async () => {
    await strategyPnlApi.prepareEvaluation({ include: [], exclude: [] }, '7d')
    const body = post.mock.calls[0][1]
    expect('strategy' in body).toBe(false)
    expect('exclude' in body).toBe(false)
    expect('mode' in body).toBe(false)
  })

  it('posts the pasted text with the selection to validate', async () => {
    await strategyPnlApi.validateEvaluation(FILTER, '30d', undefined, 1000, 'pasted text')
    const [url, body] = post.mock.calls[0]
    expect(url).toBe('/api/strategy-pnl/evaluation/validate')
    expect(body).toEqual({
      period: '30d',
      strategy: ['B', 'A'],
      exclude: ['C'],
      capital: 1000,
      response: 'pasted text',
    })
  })

  it('rejects both calls without a request when nothing is selected', async () => {
    await expect(strategyPnlApi.prepareEvaluation(NONE, '1d')).rejects.toThrow(
      'Select at least one strategy.'
    )
    await expect(
      strategyPnlApi.validateEvaluation(NONE, '1d', undefined, null, 'x')
    ).rejects.toThrow('Select at least one strategy.')
    expect(post).not.toHaveBeenCalled()
  })

  it('returns the server message from an error response', async () => {
    post.mockRejectedValue({
      response: { data: { status: 'error', message: 'Paste a shorter analysis.', problems: [] } },
    })
    const res = await strategyPnlApi.validateEvaluation(FILTER, '30d', undefined, null, 'x')
    expect(res.status).toBe('error')
    expect(res.message).toBe('Paste a shorter analysis.')
  })

  it('uses a plain sentence when the server sent no message', async () => {
    post.mockRejectedValue(new Error('Request failed with status code 500'))
    const res = await strategyPnlApi.prepareEvaluation(FILTER, '30d')
    expect(res.message).toBe('The evaluation could not be prepared. Try again in a moment.')
  })
})
