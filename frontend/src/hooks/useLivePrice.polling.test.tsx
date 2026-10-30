import { act } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderHook } from '@/test/test-utils'
import { useLivePrice } from './useLivePrice'

/**
 * How often useLivePrice asks /multiquotes.
 *
 * Every call fans out to one broker request per symbol on brokers with no batch quote call, and
 * the broker's request budget is shared with order placement. 2026-10-30: open Strategy P&L and
 * Performance tabs kept a steady stream of 18-symbol requests going (a request every 17 s while
 * the live feed was healthy), which queued the data limiter for up to 35 s. The polls must follow
 * the symbol set, not the identity of the items array, must not overlap, and must stand down while
 * the live feed is delivering every symbol.
 */
const marketData = new Map<string, { data: { ltp: number }; lastUpdate: number }>()
const getMultiQuotes = vi.fn()

vi.mock('./useMarketData', () => ({
  useMarketData: () => ({
    data: marketData,
    isConnected: true,
    isAuthenticated: true,
    isPaused: false,
    isFallbackMode: false,
    error: null,
  }),
}))

vi.mock('./useMarketStatus', () => ({
  useMarketStatus: () => ({ isMarketOpen: () => true, isAnyMarketOpen: () => true }),
}))

vi.mock('@/stores/authStore', () => ({ useAuthStore: () => ({ apiKey: 'k' }) }))

vi.mock('@/api/trading', () => ({
  tradingApi: { getMultiQuotes: (...args: unknown[]) => getMultiQuotes(...args) },
}))

const OK = { status: 'success', results: [] }

/** A live feed: every symbol's last update is always "now". */
function liveFeed(keys: string[]) {
  marketData.clear()
  for (const key of keys) {
    const entry = { data: { ltp: 100 } } as { data: { ltp: number }; lastUpdate: number }
    Object.defineProperty(entry, 'lastUpdate', { get: () => Date.now() })
    marketData.set(key, entry)
  }
}

const items = () => [
  { symbol: 'SBIN', exchange: 'NSE' },
  { symbol: 'INFY', exchange: 'NSE' },
]

describe('useLivePrice multiquotes polling', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    getMultiQuotes.mockReset()
    getMultiQuotes.mockResolvedValue(OK)
    marketData.clear()
  })
  afterEach(() => vi.useRealTimers())

  it('does not refetch when the items array is rebuilt with the same symbols', async () => {
    const { rerender } = renderHook(() => useLivePrice(items(), { enabled: true }))
    await act(async () => {})
    rerender()
    rerender()
    await act(async () => {})
    expect(getMultiQuotes).toHaveBeenCalledTimes(1)
  })

  it('refetches when the symbol set changes', async () => {
    let list = items()
    const { rerender } = renderHook(() => useLivePrice(list, { enabled: true }))
    await act(async () => {})
    list = [...list, { symbol: 'TCS', exchange: 'NSE' }]
    rerender()
    await act(async () => {})
    expect(getMultiQuotes).toHaveBeenCalledTimes(2)
  })

  it('stands down while the live feed delivers every symbol', async () => {
    liveFeed(['NSE:SBIN', 'NSE:INFY'])
    renderHook(() => useLivePrice(items(), { enabled: true, multiQuotesRefreshInterval: 30000 }))
    await act(async () => {})
    await act(async () => vi.advanceTimersByTimeAsync(120000))
    expect(getMultiQuotes).not.toHaveBeenCalled()
  })

  it('polls on its interval when a symbol has no fresh live price', async () => {
    liveFeed(['NSE:SBIN'])
    renderHook(() => useLivePrice(items(), { enabled: true, multiQuotesRefreshInterval: 30000 }))
    await act(async () => {})
    await act(async () => vi.advanceTimersByTimeAsync(60000))
    expect(getMultiQuotes).toHaveBeenCalledTimes(3)
  })

  it('keeps polling under a live feed when the caller needs the snapshot', async () => {
    liveFeed(['NSE:SBIN', 'NSE:INFY'])
    renderHook(() =>
      useLivePrice(items(), {
        enabled: true,
        multiQuotesRefreshInterval: 30000,
        pollWhenSocketLive: true,
      })
    )
    await act(async () => {})
    await act(async () => vi.advanceTimersByTimeAsync(60000))
    expect(getMultiQuotes).toHaveBeenCalledTimes(3)
  })

  it('does not start a request while the previous one is still in flight', async () => {
    getMultiQuotes.mockReturnValue(new Promise(() => {}))
    renderHook(() => useLivePrice(items(), { enabled: true, multiQuotesRefreshInterval: 30000 }))
    await act(async () => {})
    await act(async () => vi.advanceTimersByTimeAsync(120000))
    expect(getMultiQuotes).toHaveBeenCalledTimes(1)
  })
})
