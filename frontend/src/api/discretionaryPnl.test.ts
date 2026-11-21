/**
 * The status call must never break the Performance page: a 404 from an older backend, a network
 * error and a disabled flag all read as "not available". Summary params must carry quotes=0 by
 * default so no broker quote call is made unless the user asks.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

const get = vi.fn()
vi.mock('./client', () => ({ webClient: { get: (...a: unknown[]) => get(...a) } }))

const { discretionaryPnlApi, exportCsvUrl, scopeParams, summaryParams } = await import(
  './discretionaryPnl'
)

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
    expect(Object.fromEntries(p)).toEqual({
      period: '30d',
      category: 'all',
      quotes: '0',
      broker: 'flattrade',
    })
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
      category: 'all',
      quotes: '1',
      symbol: 'TCS',
      start: '2026-11-01',
      end: '2026-11-10',
    })
  })

  it('sends the category, and only the three known values are offered', () => {
    for (const category of ['all', 'intraday', 'swing'] as const) {
      const p = summaryParams('30d', undefined, {}, false, category)
      expect(p.get('category')).toBe(category)
    }
  })

  it('leaves a stale range out for a preset period', () => {
    const p = summaryParams('7d', { start: '2026-11-01', end: '2026-11-10' }, {}, false)
    expect(p.has('start')).toBe(false)
  })
})

describe('scope shared by summary, excursions and export', () => {
  const range = { start: '2026-11-01', end: '2026-11-10' }
  const filters = { broker: 'zerodha', account: 'ZR1', symbol: 'TCS' }

  it('is the summary query without quotes, so the three cannot drift', () => {
    const summary = Object.fromEntries(summaryParams('custom', range, filters, true, 'swing'))
    const scope = Object.fromEntries(scopeParams('custom', range, filters, 'swing'))
    delete (summary as Record<string, string>).quotes
    expect(scope).toEqual(summary)
    expect(scope).toEqual({
      period: 'custom',
      category: 'swing',
      broker: 'zerodha',
      account: 'ZR1',
      symbol: 'TCS',
      start: '2026-11-01',
      end: '2026-11-10',
    })
  })

  it('builds the CSV link from the same scope', () => {
    const url = exportCsvUrl('custom', range, filters, 'intraday')
    const [path, query] = url.split('?')
    expect(path).toBe('/api/discretionary-pnl/export.csv')
    expect(Object.fromEntries(new URLSearchParams(query))).toEqual(
      Object.fromEntries(scopeParams('custom', range, filters, 'intraday'))
    )
  })

  it('asks the excursions endpoint with the same scope', async () => {
    get.mockResolvedValue({ data: { status: 'success', positions: [] } })
    await discretionaryPnlApi.excursions('7d', undefined, { symbol: 'TCS' }, 'swing')
    const url = get.mock.calls[0][0] as string
    expect(url.startsWith('/api/discretionary-pnl/excursions?')).toBe(true)
    expect(Object.fromEntries(new URLSearchParams(url.split('?')[1]))).toEqual({
      period: '7d',
      category: 'swing',
      symbol: 'TCS',
    })
  })

  it('sends category on the summary request', async () => {
    get.mockResolvedValue({ data: { status: 'success' } })
    await discretionaryPnlApi.summary('7d', undefined, {}, false, 'intraday')
    expect(get.mock.calls[0][0]).toContain('category=intraday')
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
