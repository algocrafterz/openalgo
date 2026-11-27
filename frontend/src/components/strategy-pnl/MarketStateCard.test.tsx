import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@/test/test-utils'

const marketState = vi.fn()

vi.mock('@/api/strategyPnl', () => ({
  strategyPnlApi: { marketState: (...a: unknown[]) => marketState(...a) },
}))

const { MARKET_STATE_FOOTNOTE, MarketStateCard } = await import('./MarketStateCard')

const money = (v: number) => `Rs ${v.toFixed(2)}`

function row(name: string, over: Record<string, unknown> = {}) {
  return {
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
    ...over,
  }
}

function response(over: Record<string, unknown> = {}) {
  return {
    status: 'success',
    benchmark: 'NIFTY 50 (daily)',
    methodology: {
      small_sample_text: 'Small sample: do not draw conclusions',
      small_sample_trades: 30,
    },
    source: 'strategy',
    mode: 'live',
    trades_count: 12,
    trend: [
      row('Uptrend', {
        trades: 5,
        wins: 3,
        win_rate: 60,
        gross_pnl: 1000,
        net_pnl: 900,
        avg_net_per_trade: 180,
        profit_factor: 2.5,
        net_coverage: { costed: 5, total: 5 },
      }),
      row('Downtrend', {
        trades: 4,
        wins: 1,
        win_rate: 25,
        gross_pnl: -300,
        net_coverage: { costed: 3, total: 4 },
      }),
      row('Range'),
      row('Unclassified', {
        trades: 3,
        wins: 0,
        win_rate: 0,
        gross_pnl: -50,
        net_pnl: -60,
        avg_net_per_trade: -20,
        net_coverage: { costed: 3, total: 3 },
      }),
    ],
    volatility: [
      row('Low'),
      row('Normal', { trades: 12, wins: 4, win_rate: 33.3, gross_pnl: 650 }),
      row('High'),
      row('Unclassified'),
    ],
    unclassified: {
      trend: { trades: 3, reasons: { stale_bar: 3 } },
      volatility: { trades: 0, reasons: {} },
    },
    data_status: {
      state: 'ok',
      bars: 1237,
      dropped_bars: 2,
      first_bar: '2021-11-22',
      last_bar: '2026-11-26',
    },
    ...over,
  }
}

function renderCard(props: Record<string, unknown> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MarketStateCard
        source="strategy"
        period="30d"
        clientMode="live"
        formatCurrency={money}
        {...props}
      />
    </QueryClientProvider>
  )
}

beforeEach(() => vi.clearAllMocks())

describe('MarketStateCard', () => {
  it('shows a trend table and a separate volatility table with every class', async () => {
    marketState.mockResolvedValue(response())
    renderCard()
    expect(await screen.findByText('Market State Analysis')).toBeInTheDocument()
    for (const name of ['Uptrend', 'Downtrend', 'Range', 'Low', 'Normal', 'High']) {
      expect(await screen.findByTestId(`market-state-row-${name}`)).toBeInTheDocument()
    }
    expect(screen.getAllByTestId('market-state-row-Unclassified')).toHaveLength(2)
    expect(screen.getAllByRole('table')).toHaveLength(2)
    const up = within(screen.getByTestId('market-state-row-Uptrend'))
    expect(up.getByText('60.0%')).toBeInTheDocument()
    expect(up.getByText('Rs 1000.00')).toBeInTheDocument()
    expect(up.getByText('Rs 900.00')).toBeInTheDocument()
    expect(up.getByText('Rs 180.00')).toBeInTheDocument()
    expect(up.getByText('2.50')).toBeInTheDocument()
  })

  it('shows a dash with the coverage when net is incomplete, never a number', async () => {
    marketState.mockResolvedValue(response())
    renderCard()
    const down = within(await screen.findByTestId('market-state-row-Downtrend'))
    expect(down.getByText('Rs -300.00')).toBeInTheDocument()
    const dashes = down.getAllByTitle('Net unavailable: 3 of 4 trades costed')
    expect(dashes).toHaveLength(3) // net, avg net, profit factor
    expect(down.getByText('3 of 4 costed')).toBeInTheDocument()
  })

  it('marks small samples per row and states the disclosure', async () => {
    marketState.mockResolvedValue(response())
    renderCard()
    const up = within(await screen.findByTestId('market-state-row-Uptrend'))
    expect(up.getByText('Small sample')).toBeInTheDocument()
    expect(screen.getAllByText(/Small sample: do not draw conclusions/).length).toBeGreaterThan(0)
    const empty = within(screen.getByTestId('market-state-row-Range'))
    expect(empty.queryByText('Small sample')).toBeNull()
  })

  it('does not flag a row of 30 or more trades', async () => {
    const big = response()
    big.trend[0] = row('Uptrend', {
      trades: 30,
      wins: 15,
      win_rate: 50,
      gross_pnl: 10,
      net_pnl: 5,
      avg_net_per_trade: 0.17,
      net_coverage: { costed: 30, total: 30 },
      small_sample: false,
    })
    marketState.mockResolvedValue(big)
    renderCard()
    const up = within(await screen.findByTestId('market-state-row-Uptrend'))
    expect(up.queryByText('Small sample')).toBeNull()
  })

  it('explains unclassified trades and shows the footnote', async () => {
    marketState.mockResolvedValue(response())
    renderCard()
    expect(await screen.findByText(/3 of 12 trades have no trend label/)).toBeInTheDocument()
    expect(screen.getByTestId('market-state-footnote')).toHaveTextContent(MARKET_STATE_FOOTNOTE)
    expect(MARKET_STATE_FOOTNOTE).toContain('residual class, not a confirmed range-bound market')
    expect(MARKET_STATE_FOOTNOTE).toContain('previous completed trading day')
  })

  it('says so when the benchmark data cannot be read', async () => {
    marketState.mockResolvedValue(
      response({
        data_status: {
          state: 'no_data',
          bars: 0,
          dropped_bars: 0,
          first_bar: null,
          last_bar: null,
        },
      })
    )
    renderCard()
    expect(await screen.findByText(/every trade is Unclassified/)).toBeInTheDocument()
  })

  it('shows an empty state when there are no closed trades', async () => {
    marketState.mockResolvedValue(response({ trades_count: 0 }))
    renderCard()
    expect(await screen.findByText('No closed trades to classify')).toBeInTheDocument()
    expect(screen.queryByRole('table')).toBeNull()
  })

  it('shows the server message on an error', async () => {
    marketState.mockResolvedValue({
      status: 'error',
      message: 'Could not build the market state analysis.',
    })
    renderCard()
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not build the market state analysis.'
    )
    expect(screen.getByTestId('market-state-footnote')).toBeInTheDocument()
  })

  it('shows a plain message when the request itself fails', async () => {
    marketState.mockRejectedValue(new Error('boom'))
    renderCard()
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not load the market state analysis. Please try again.'
    )
  })

  it('shows a spinner while loading', () => {
    marketState.mockReturnValue(new Promise(() => undefined))
    renderCard()
    expect(screen.getByRole('status')).toBeInTheDocument()
  })

  it('does not request anything while disabled', () => {
    renderCard({ enabled: false })
    expect(marketState).not.toHaveBeenCalled()
  })

  it('requests with the page scope and refetches when the mode changes', async () => {
    marketState.mockResolvedValue(response())
    const filter = { include: ['ORB'], exclude: [] }
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const ui = (mode: 'live' | 'analyze') => (
      <QueryClientProvider client={client}>
        <MarketStateCard
          source="strategy"
          period="custom"
          range={{ start: '2026-11-01', end: '2026-11-20' }}
          filter={filter}
          clientMode={mode}
          formatCurrency={money}
        />
      </QueryClientProvider>
    )
    const { rerender } = render(ui('live'))
    await waitFor(() => expect(marketState).toHaveBeenCalledTimes(1))
    expect(marketState).toHaveBeenCalledWith(
      'strategy',
      'custom',
      { start: '2026-11-01', end: '2026-11-20' },
      filter,
      undefined
    )
    rerender(ui('analyze'))
    await waitFor(() => expect(marketState).toHaveBeenCalledTimes(2))
  })

  it('passes the discretionary filters for the discretionary source', async () => {
    marketState.mockResolvedValue(response({ source: 'discretionary' }))
    renderCard({
      source: 'discretionary',
      discretionary: { broker: 'zerodha', category: 'intraday' },
    })
    await waitFor(() => expect(marketState).toHaveBeenCalledTimes(1))
    expect(marketState.mock.calls[0][0]).toBe('discretionary')
    expect(marketState.mock.calls[0][4]).toEqual({ broker: 'zerodha', category: 'intraday' })
  })
})
