/**
 * The Performance page keeps its Strategies view by default, and the Discretionary source is
 * reachable only when the backend says it is enabled. The Discretionary view is stubbed here; its
 * own tests cover it. The capital the page keeps per mode must not be touched by the new source.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, userEvent, waitFor } from '@/test/test-utils'

const status = vi.fn()
const compare = vi.fn()
const daily = vi.fn()

vi.mock('@/api/discretionaryPnl', () => ({
  discretionaryPnlApi: { status: () => status() },
}))
vi.mock('@/api/strategyPnl', () => ({
  strategyPnlApi: {
    compare: (...a: unknown[]) => compare(...a),
    daily: (...a: unknown[]) => daily(...a),
    exportTrades: vi.fn(),
    excursions: vi.fn().mockResolvedValue({ status: 'success' }),
    listReports: vi.fn().mockResolvedValue([]),
  },
  strategyReportUrl: (f: string) => f,
}))
const orderEventRefresh = vi.fn()
vi.mock('@/hooks/useOrderEventRefresh', () => ({
  useOrderEventRefresh: (...a: unknown[]) => orderEventRefresh(...a),
}))
vi.mock('@/components/discretionary-pnl/DiscretionaryPerformance', () => ({
  DiscretionaryPerformance: () => <div data-testid="discretionary-view" />,
}))
vi.mock('@/components/strategy-pnl/EquityDrawdownChart', () => ({
  EquityDrawdownChart: () => null,
}))
vi.mock('@/components/strategy-pnl/StrategyReports', () => ({
  StrategyReports: () => <div data-testid="strategy-reports" />,
}))
vi.mock('@/components/DataHealthBanner', () => ({ DataHealthBanner: () => null }))
const sock = vi.hoisted(() => {
  const handlers = new Map<string, Set<(p: unknown) => void>>()
  return {
    handlers,
    socket: {
      on: (e: string, h: (p: unknown) => void) => {
        if (!handlers.has(e)) handlers.set(e, new Set())
        handlers.get(e)?.add(h)
      },
      off: (e: string, h: (p: unknown) => void) => {
        handlers.get(e)?.delete(h)
      },
    },
  }
})
vi.mock('@/components/socket/SocketProvider', () => ({
  useSocketContext: () => ({ socket: sock.socket, playAlertSound: () => {} }),
}))

const { default: StrategyDailyPerformance } = await import('./StrategyDailyPerformance')

let lastClient: QueryClient

function renderPage(url: string) {
  window.history.pushState({}, '', url)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  lastClient = client
  return render(
    <QueryClientProvider client={client}>
      <StrategyDailyPerformance />
    </QueryClientProvider>
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  compare.mockResolvedValue({
    status: 'success',
    mode: 'live',
    strategies: [],
    all_strategies: ['alpha', 'beta'],
  })
  daily.mockResolvedValue({ status: 'success', mode: 'live', trades_count: 0 })
})

describe('Strategies view is the default', () => {
  it('shows no selector and no discretionary view when the feature is disabled', async () => {
    status.mockResolvedValue(false)
    renderPage('/strategy-pnl/daily')
    expect(await screen.findByText('Strategy Daily Performance')).toBeInTheDocument()
    await waitFor(() => expect(status).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: 'Discretionary' })).not.toBeInTheDocument()
    expect(screen.queryByTestId('discretionary-view')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Capital per strategy (Rs)')).toBeInTheDocument()
    expect(screen.getByText('Strategy Comparison')).toBeInTheDocument()
    await waitFor(() => expect(compare).toHaveBeenCalled())
  })

  it('falls back to strategies when the URL asks for discretionary but it is disabled', async () => {
    status.mockResolvedValue(false)
    renderPage('/strategy-pnl/daily?source=discretionary')
    expect(await screen.findByText('Strategy Daily Performance')).toBeInTheDocument()
    expect(screen.queryByTestId('discretionary-view')).not.toBeInTheDocument()
    await waitFor(() => expect(compare).toHaveBeenCalled())
  })

  it('falls back to strategies when the status call fails (older backend)', async () => {
    status.mockRejectedValue(new Error('404'))
    renderPage('/strategy-pnl/daily?source=discretionary')
    expect(await screen.findByText('Strategy Daily Performance')).toBeInTheDocument()
    expect(screen.queryByTestId('discretionary-view')).not.toBeInTheDocument()
  })

  it('keeps an old ?strategy=X link working and shows the selector when enabled', async () => {
    status.mockResolvedValue(true)
    renderPage('/strategy-pnl/daily?strategy=alpha&period=30d')
    expect(await screen.findByRole('button', { name: 'Discretionary' })).toBeInTheDocument()
    expect(screen.getByText('Strategy Daily Performance')).toBeInTheDocument()
    await waitFor(() => expect(compare).toHaveBeenCalled())
    expect(compare.mock.calls[0][2]).toMatchObject({ include: ['alpha'], exclude: [] })
    expect(window.location.search).toContain('strategy=alpha')
  })
})

describe('Discretionary source', () => {
  it('shows the discretionary view and makes no strategy request or capital write', async () => {
    status.mockResolvedValue(true)
    renderPage('/strategy-pnl/daily?source=discretionary&period=30d&capital=50000')
    expect(await screen.findByTestId('discretionary-view')).toBeInTheDocument()
    expect(screen.getByText('Discretionary Performance')).toBeInTheDocument()
    expect(screen.queryByLabelText('Capital per strategy (Rs)')).not.toBeInTheDocument()
    expect(screen.queryByText('Strategy Comparison')).not.toBeInTheDocument()
    expect(screen.queryByTestId('strategy-reports')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Export CSV/ })).not.toBeInTheDocument()
    expect(compare).not.toHaveBeenCalled()
    expect(daily).not.toHaveBeenCalled()
    // The capital in the URL is left exactly as it was, and nothing was stored for it.
    expect(new URLSearchParams(window.location.search).get('capital')).toBe('50000')
    expect(localStorage.length).toBe(0)
  })

  it('does not start strategy requests while the status is still unknown', async () => {
    let resolve: (v: boolean) => void = () => {}
    status.mockReturnValue(
      new Promise<boolean>((r) => {
        resolve = r
      })
    )
    renderPage('/strategy-pnl/daily?source=discretionary')
    await waitFor(() => expect(status).toHaveBeenCalled())
    expect(compare).not.toHaveBeenCalled()
    expect(daily).not.toHaveBeenCalled()
    resolve(true)
    expect(await screen.findByTestId('discretionary-view')).toBeInTheDocument()
    expect(compare).not.toHaveBeenCalled()
  })

  it('switches source from the selector and back, keeping the URL in step', async () => {
    status.mockResolvedValue(true)
    renderPage('/strategy-pnl/daily?period=30d&strategy=alpha')
    await userEvent.click(await screen.findByRole('button', { name: 'Discretionary' }))
    expect(await screen.findByTestId('discretionary-view')).toBeInTheDocument()
    expect(window.location.search).toContain('source=discretionary')
    expect(window.location.search).toContain('period=30d')
    expect(window.location.search).toContain('strategy=alpha')
    await userEvent.click(screen.getByRole('button', { name: 'Strategies (current mode)' }))
    expect(await screen.findByText('Strategy Daily Performance')).toBeInTheDocument()
    expect(window.location.search).not.toContain('source')
    expect(window.location.search).toContain('strategy=alpha')
    await waitFor(() => expect(compare).toHaveBeenCalled())
  })
})

describe('URL and refresh housekeeping', () => {
  it('replaces a stale ?source=discretionary when the server says it is disabled, with no history entry', async () => {
    status.mockResolvedValue(false)
    renderPage('/strategy-pnl/daily?source=discretionary&period=30d&strategy=alpha')
    const lengthBefore = window.history.length
    await waitFor(() => expect(window.location.search).not.toContain('source'))
    expect(window.location.search).toContain('period=30d')
    expect(window.location.search).toContain('strategy=alpha')
    expect(window.history.length).toBe(lengthBefore)
    expect(await screen.findByText('Strategy Daily Performance')).toBeInTheDocument()
  })

  it('keeps ?source=discretionary while the status is unknown or enabled', async () => {
    status.mockResolvedValue(true)
    renderPage('/strategy-pnl/daily?source=discretionary')
    expect(await screen.findByTestId('discretionary-view')).toBeInTheDocument()
    expect(window.location.search).toContain('source=discretionary')
  })

  it('does not invalidate the discretionary queries on an order event: the view owns that', async () => {
    status.mockResolvedValue(true)
    renderPage('/strategy-pnl/daily?source=discretionary')
    await screen.findByTestId('discretionary-view')
    const spy = vi.spyOn(lastClient, 'invalidateQueries')
    const refresh = orderEventRefresh.mock.calls.at(-1)?.[0] as () => void
    refresh()
    const keys = spy.mock.calls.map((c) => JSON.stringify((c[0] as { queryKey: unknown }).queryKey))
    expect(keys).toContain(JSON.stringify(['strategy-daily']))
    expect(keys.some((k) => k.includes('discretionary-pnl'))).toBe(false)
  })

  it('Strategies view: no order_update listener, so a broker fill refetches nothing there', async () => {
    status.mockResolvedValue(true)
    renderPage('/strategy-pnl/daily')
    await waitFor(() => expect(compare).toHaveBeenCalled())
    expect(sock.handlers.get('order_update')?.size ?? 0).toBe(0)
    // the Strategies refresh is unchanged: same events as before
    expect(orderEventRefresh.mock.calls.at(-1)?.[1]).toEqual({
      events: ['order_event', 'analyzer_update'],
    })
  })
})
