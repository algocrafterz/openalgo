/**
 * The Today's market state line on the Strategy P&L page is read-only context: it shows the
 * label from the market state API, Unclassified when there is none, and nothing when the
 * request fails.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@/test/test-utils'

const today = vi.fn()
vi.mock('@/api/strategyPnl', () => ({
  strategyPnlApi: { marketStateToday: () => today() },
}))

const { TodayMarketStateLine } = await import('./TodayMarketStateLine')

function renderLine() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <TodayMarketStateLine />
    </QueryClientProvider>
  )
}

beforeEach(() => vi.clearAllMocks())

describe('TodayMarketStateLine', () => {
  it('shows trend, volatility, the as-of date and the benchmark', async () => {
    today.mockResolvedValue({
      status: 'success',
      benchmark: 'NIFTY 50 (daily)',
      trend: 'Uptrend',
      volatility: 'High',
      as_of: '2026-11-26',
    })
    renderLine()
    expect(await screen.findByTestId('today-trend')).toHaveTextContent('Uptrend')
    expect(screen.getByTestId('today-volatility')).toHaveTextContent('High volatility')
    expect(screen.getByTestId('today-market-state')).toHaveTextContent('as of 2026-11-26')
    expect(screen.getByTestId('today-market-state')).toHaveTextContent('NIFTY 50 (daily)')
  })

  it('shows Unclassified when no label applies', async () => {
    today.mockResolvedValue({
      status: 'success',
      trend: null,
      volatility: null,
      as_of: null,
      reason: 'no_bars',
    })
    renderLine()
    expect(await screen.findByTestId('today-trend')).toHaveTextContent('Unclassified')
    expect(screen.getByTestId('today-volatility')).toHaveTextContent('Unclassified')
  })

  it('shows nothing when the request fails', async () => {
    today.mockResolvedValue({ status: 'error', message: 'Could not read' })
    renderLine()
    await waitFor(() => expect(today).toHaveBeenCalled())
    expect(screen.queryByTestId('today-market-state')).not.toBeInTheDocument()
  })
})
