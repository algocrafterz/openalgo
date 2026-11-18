/**
 * The status call must never break the Performance page: a 404 from an older backend, a network
 * error and a disabled flag all read as "not available". Summary params must carry quotes=0 by
 * default so no broker quote call is made unless the user asks.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

const get = vi.fn()
vi.mock('./client', () => ({ webClient: { get: (...a: unknown[]) => get(...a) } }))

const { discretionaryPnlApi, summaryParams } = await import('./discretionaryPnl')

beforeEach(() => vi.clearAllMocks())

describe('status', () => {
  it('is true only for success with enabled true', async () => {
    get.mockResolvedValue({ data: { status: 'success', enabled: true } })
    expect(await discretionaryPnlApi.status()).toBe(true)
    expect(get).toHaveBeenCalledWith('/api/discretionary-pnl/status')
  })

  it('is false when disabled', async () => {
    get.mockResolvedValue({ data: { status: 'success', enabled: false } })
    expect(await discretionaryPnlApi.status()).toBe(false)
  })

  it('is false on a 404 or a network error instead of throwing', async () => {
    get.mockRejectedValue({ response: { status: 404, data: { status: 'error' } } })
    expect(await discretionaryPnlApi.status()).toBe(false)
    get.mockRejectedValue(new Error('Network Error'))
    expect(await discretionaryPnlApi.status()).toBe(false)
  })

  it('is false for an unexpected body', async () => {
    get.mockResolvedValue({ data: '<html>' })
    expect(await discretionaryPnlApi.status()).toBe(false)
  })
})

describe('summary params', () => {
  it('asks for no quotes by default and sends only the filters that are set', () => {
    const p = summaryParams(
      '30d',
      undefined,
      { broker: ' flattrade ', symbol: '', account: undefined },
      false
    )
    expect(Object.fromEntries(p)).toEqual({ period: '30d', quotes: '0', broker: 'flattrade' })
  })

  it('sends quotes=1 and the custom range', () => {
    const p = summaryParams(
      'custom',
      { start: '2026-11-01', end: '2026-11-10' },
      { symbol: 'TCS' },
      true
    )
    expect(Object.fromEntries(p)).toEqual({
      period: 'custom',
      quotes: '1',
      symbol: 'TCS',
      start: '2026-11-01',
      end: '2026-11-10',
    })
  })

  it('leaves a stale range out for a preset period', () => {
    const p = summaryParams('7d', { start: '2026-11-01', end: '2026-11-10' }, {}, false)
    expect(p.has('start')).toBe(false)
  })
})

describe('reads', () => {
  it('turns a 404 feature_disabled body into an error with the code', async () => {
    get.mockRejectedValue({
      response: {
        status: 404,
        data: { status: 'error', code: 'feature_disabled', message: 'off' },
      },
    })
    await expect(discretionaryPnlApi.summary('30d', undefined, {}, false)).rejects.toMatchObject({
      message: 'off',
      code: 'feature_disabled',
      httpStatus: 404,
    })
  })

  it('turns a 400 message into an error', async () => {
    get.mockRejectedValue({
      response: { status: 400, data: { status: 'error', message: 'bad period' } },
    })
    await expect(discretionaryPnlApi.summary('30d', undefined, {}, false)).rejects.toThrow(
      'bad period'
    )
  })

  it('uses a fallback message when there is no body', async () => {
    get.mockRejectedValue(new Error('boom'))
    await expect(discretionaryPnlApi.unresolved({})).rejects.toThrow(
      'Could not load the unresolved fills'
    )
  })

  it('fetches the fills of a trade by id', async () => {
    get.mockResolvedValue({ data: { status: 'success', fills: [], linked_fills: 0 } })
    await discretionaryPnlApi.tradeFills(42, 'b-1 x')
    expect(get).toHaveBeenCalledWith('/api/discretionary-pnl/trades/42/fills?build_id=b-1+x')
  })

  it('keeps the build_changed code of a 409 so the page can say the data changed', async () => {
    get.mockRejectedValue({
      response: { status: 409, data: { status: 'error', code: 'build_changed', message: 'moved' } },
    })
    await expect(discretionaryPnlApi.tradeFills(42, 'old')).rejects.toMatchObject({
      code: 'build_changed',
      httpStatus: 409,
    })
  })
})
