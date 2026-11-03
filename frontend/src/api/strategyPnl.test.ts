/**
 * The Performance API refuses to turn "nothing selected" into a whole-portfolio
 * request. Catches an implementation that drops the none flag when it builds
 * the query string, which would show the whole portfolio under an empty choice.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

const get = vi.fn()
vi.mock('./client', () => ({ webClient: { get: (...a: unknown[]) => get(...a) } }))

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
