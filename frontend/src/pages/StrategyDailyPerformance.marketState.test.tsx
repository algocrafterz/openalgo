/**
 * The Strategy Daily Performance page shows the Market State Analysis card for a multi-day
 * report in the strategy source, passes the client mode, and leaves it out for a single-day view
 * and for an empty report. Everything else on the page is covered by its own tests.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@/test/test-utils'

const status = vi.fn()
const compare = vi.fn()
const daily = vi.fn()
const marketState = vi.fn()

vi.mock('@/api/discretionaryPnl', () => ({
  discretionaryPnlApi: { status: () => status() },
}))
vi.mock('@/api/strategyPnl', () => ({
  strategyPnlApi: {
    compare: (...a: unknown[]) => compare(...a),
    daily: (...a: unknown[]) => daily(...a),
    marketState: (...a: unknown[]) => marketState(...a),
    exportTrades: vi.fn(),
    excursions: vi.fn().mockResolvedValue({ status: 'success' }),
    listReports: vi.fn().mockResolvedValue([]),
  },
  strategyReportUrl: (f: string) => f,
}))
vi.mock('@/hooks/useOrderEventRefresh', () => ({ useOrderEventRefresh: vi.fn() }))
vi.mock('@/components/discretionary-pnl/DiscretionaryPerformance', () => ({
  DiscretionaryPerformance: () => <div data-testid="discretionary-view" />,
}))
vi.mock('@/components/strategy-pnl/EquityDrawdownChart', () => ({
  EquityDrawdownChart: () => null,
  NET_LINE_COLOR: '#2563eb',
}))
vi.mock('@/components/strategy-pnl/StrategyReports', () => ({
  StrategyReports: () => <div data-testid="strategy-reports" />,
}))
vi.mock('@/components/DataHealthBanner', () => ({ DataHealthBanner: () => null }))
vi.mock('@/components/socket/SocketProvider', () => ({
  useSocketContext: () => ({
    socket: { on: () => undefined, off: () => undefined },
    playAlertSound: () => undefined,
  }),
}))

const { default: StrategyDailyPerformance } = await import('./StrategyDailyPerformance')

function renderPage(url: string) {
  window.history.pushState({}, '', url)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <StrategyDailyPerformance />
    </QueryClientProvider>
  )
}

const emptyRow = (name: string) => ({
  name,
  trades: 0,
  wins: 0,
  win_rate: null,
  gross_pnl: null,
  charges_estimated: null,
  net_pnl: null,
  avg_net_per_trade: null,
  profit_factor: null,
  net_coverage: { costed: 0, total: 0 },
  small_sample: true,
})

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  status.mockResolvedValue(false)
  compare.mockResolvedValue({ status: 'success', mode: 'live', strategies: [], all_strategies: [] })
  marketState.mockResolvedValue({
    status: 'success',
    mode: 'live',
    trades_count: 2,
    trend: ['Uptrend', 'Downtrend', 'Range', 'Unclassified'].map(emptyRow),
    volatility: ['Low', 'Normal', 'High', 'Unclassified'].map(emptyRow),
    unclassified: {
      trend: { trades: 0, reasons: {} },
      volatility: { trades: 0, reasons: {} },
    },
    data_status: { state: 'ok', bars: 1, dropped_bars: 0, first_bar: null, last_bar: null },
  })
})

describe('Market State Analysis on the Strategy Daily Performance page', () => {
  it('renders the card for a multi-day report and requests it for the strategy source', async () => {
    daily.mockResolvedValue({
      status: 'success',
      mode: 'live',
      trades_count: 2,
      start_date: '2026-11-01',
      end_date: '2026-11-20',
    })
    renderPage('/strategy-pnl/daily?period=30d&strategy=alpha')
    expect(await screen.findByTestId('market-state-card')).toBeInTheDocument()
    expect(screen.getByText('Market State Analysis')).toBeInTheDocument()
    await waitFor(() => expect(marketState).toHaveBeenCalled())
    const [source, period, , filter] = marketState.mock.calls[0]
    expect(source).toBe('strategy')
    expect(period).toBe('30d')
    expect(filter).toMatchObject({ include: ['alpha'], exclude: [] })
  })

  it('leaves the card out when there are no closed trades', async () => {
    daily.mockResolvedValue({ status: 'success', mode: 'live', trades_count: 0 })
    renderPage('/strategy-pnl/daily?period=30d')
    await waitFor(() => expect(daily).toHaveBeenCalled())
    expect(screen.queryByTestId('market-state-card')).not.toBeInTheDocument()
    expect(marketState).not.toHaveBeenCalled()
  })

  it('leaves the card out of a single-day view', async () => {
    daily.mockResolvedValue({
      status: 'success',
      mode: 'live',
      trades_count: 2,
      start_date: '2026-11-20',
      end_date: '2026-11-20',
      trades: [],
    })
    renderPage('/strategy-pnl/daily?period=1d')
    await waitFor(() => expect(daily).toHaveBeenCalled())
    expect(screen.queryByTestId('market-state-card')).not.toBeInTheDocument()
  })

  it('makes no market state request for the discretionary source (its own view owns that)', async () => {
    status.mockResolvedValue(true)
    renderPage('/strategy-pnl/daily?source=discretionary')
    expect(await screen.findByTestId('discretionary-view')).toBeInTheDocument()
    expect(marketState).not.toHaveBeenCalled()
  })
})
