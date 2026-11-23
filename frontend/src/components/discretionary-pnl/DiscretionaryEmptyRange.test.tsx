import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  emptySummary,
  emptyToday,
  excursionPosition,
  excursions,
  position,
  summary,
  trade,
  unresolvedInRange,
} from '@/test/discretionaryFixtures'
import { render, screen, within } from '@/test/test-utils'

const summaryFn = vi.fn()
const excursionsFn = vi.fn()

vi.mock('@/api/discretionaryPnl', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/discretionaryPnl')>()
  return {
    ...actual,
    discretionaryPnlApi: {
      status: vi.fn(),
      summary: (...a: unknown[]) => summaryFn(...a),
      excursions: (...a: unknown[]) => excursionsFn(...a),
      tradeFills: vi.fn(),
      unresolved: vi.fn().mockResolvedValue({
        unresolved: [],
        unresolved_count: 0,
        orphans: [],
        orphans_count: 0,
      }),
    },
  }
})
vi.mock('@/hooks/useOrderEventRefresh', () => ({ useOrderEventRefresh: vi.fn() }))
vi.mock('@/components/socket/SocketProvider', () => ({
  useSocketContext: () => ({
    socket: { on: () => {}, off: () => {} },
    playAlertSound: () => {},
  }),
}))
vi.mock('./EquityCanvas', () => ({
  DRAWDOWN_COLOR: '#d97706',
  EquityCanvas: () => <div data-testid="equity-chart" />,
}))

const { DiscretionaryPerformance } = await import('./DiscretionaryPerformance')

const money = (v: number) => `Rs ${v.toFixed(2)}`
const NO_CLOSED = 'No closed trades in this date range.'

function renderIt(props: Partial<React.ComponentProps<typeof DiscretionaryPerformance>> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <DiscretionaryPerformance
        period="custom"
        range={{ start: '2026-11-14', end: '2026-11-15' }}
        rangeReady
        formatCurrency={money}
        isDarkMode={false}
        {...props}
      />
    </QueryClientProvider>
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  excursionsFn.mockResolvedValue(excursions())
})

describe('empty range states', () => {
  it('a range that ends before the reliable start shows the notice and no unmatched-fills text', async () => {
    summaryFn.mockResolvedValue({
      ...emptySummary(),
      period: {
        period: 'custom',
        start: '2026-11-10',
        end: '2026-11-12',
        reliable_since: '2026-11-18',
      },
      today: emptyToday({ session_date: '2026-11-23' }),
      executions_collected: 5,
      executions_unresolved: 5,
      unresolved_in_range: unresolvedInRange({ unresolved_fills: 5, orphan_fills: 0, total: 5 }),
    })
    renderIt({ range: { start: '2026-11-10', end: '2026-11-12' } })
    expect(await screen.findByTestId('reliable-start-notice')).toHaveTextContent(
      'Your ledger is reliable from'
    )
    expect(screen.getByText(NO_CLOSED)).toBeInTheDocument()
    expect(screen.queryByText(/No trades to show yet/)).not.toBeInTheDocument()
    // the fills counted belong to the first reliable day, not to the range asked for
    expect(screen.queryByTestId('data-warning')).not.toBeInTheDocument()
    expect(screen.queryByText(/could not be matched to a trade, so/)).not.toBeInTheDocument()
    expect(screen.queryByText('No discretionary trades yet')).not.toBeInTheDocument()
    expect(screen.queryByText(/logged in to your broker/i)).not.toBeInTheDocument()
  })

  it('an empty range of a ledger that has trades elsewhere says so and not "no trades yet"', async () => {
    summaryFn.mockResolvedValue({
      ...emptySummary(),
      period: {
        period: 'custom',
        start: '2026-11-14',
        end: '2026-11-15',
        reliable_since: '2026-11-01',
      },
      today: emptyToday({ session_date: '2026-11-23' }),
    })
    renderIt()
    expect(await screen.findByText(NO_CLOSED)).toBeInTheDocument()
    expect(screen.queryByText('No discretionary trades yet')).not.toBeInTheDocument()
    expect(screen.queryByText(/logged in to your broker/i)).not.toBeInTheDocument()
    expect(screen.queryByText('Net realized (after est. charges)')).not.toBeInTheDocument()
    // no zero-trade ratio sentence, no low-sample wording
    expect(screen.queryByTestId('discretionary-sample-note')).not.toBeInTheDocument()
    expect(screen.queryByText(/Ratios based on/)).not.toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/low sample|insufficient|0 completed trade/i)
  })

  it('keeps the no-trades-yet text when the ledger itself is empty', async () => {
    summaryFn.mockResolvedValue(emptySummary())
    renderIt({ period: 'all', range: undefined })
    expect(await screen.findByText('No discretionary trades yet')).toBeInTheDocument()
    expect(screen.getByText(/logged in to your broker/i)).toBeInTheDocument()
    expect(screen.queryByText(NO_CLOSED)).not.toBeInTheDocument()
  })

  it('draws no 0.00 for figures with no closed trade behind them, and keeps the open position', async () => {
    const base = summary()
    summaryFn.mockResolvedValue(
      summary({
        period: {
          period: 'custom',
          start: '2026-11-14',
          end: '2026-11-15',
          reliable_since: '2026-11-01',
        },
        today: emptyToday({ session_date: '2026-11-23', open_positions: 1 }),
        trades: [],
        trades_total: 0,
        positions: [position()],
        positions_total: 1,
        open_positions: 1,
        realized_gross: 0,
        charges: 0,
        net: 0,
        realized_net_of_charges: 0,
        costs: null,
        consistency: null,
        risk: null,
        metrics: {
          ...base.metrics,
          trades_count: 0,
          winning_trades: 0,
          losing_trades: 0,
          win_rate: null,
          gross_profit: 0,
          gross_loss: 0,
          net_profit: 0,
          profit_factor: null,
          expectancy: null,
          avg_win: null,
          avg_loss: null,
          max_drawdown: 0,
        },
        by_period: { daily: [], weekly: [], monthly: [] },
      })
    )
    renderIt()
    const header = await screen.findByTestId('discretionary-summary')
    expect(within(header).getByTestId('summary-net')).toHaveTextContent('—')
    expect(within(header).getByTestId('summary-count')).toHaveTextContent('0')
    const stats = screen.getByTestId('discretionary-statistics')
    expect(stats.textContent ?? '').not.toContain('Rs 0.00')
    expect(screen.getByTestId('group-performance').textContent ?? '').not.toContain('Rs 0.00')
    // the summary cards (the Today panel is the real current session and keeps its own figures)
    for (const id of ['summary-net', 'summary-count', 'summary-win-rate']) {
      expect(within(header).getByTestId(id).textContent ?? '').not.toContain('Rs 0.00')
    }
    expect(screen.getByTestId('ledger-card')).toHaveTextContent(NO_CLOSED)
    expect(screen.getByTestId('open-positions-card')).toBeInTheDocument()
    expect(screen.queryByText('No discretionary trades yet')).not.toBeInTheDocument()
  })

  it('still shows money when a partial exit closed legs but no trade completed', async () => {
    const base = summary()
    summaryFn.mockResolvedValue(
      summary({
        trades: [],
        trades_total: 0,
        positions: [position()],
        positions_total: 1,
        realized_gross: 120,
        charges: 20,
        net: 100,
        realized_net_of_charges: 100,
        metrics: { ...base.metrics, trades_count: 0, winning_trades: 0, losing_trades: 0 },
      })
    )
    renderIt({ period: '30d', range: undefined })
    const header = await screen.findByTestId('discretionary-summary')
    expect(within(header).getByTestId('summary-net')).toHaveTextContent('Rs 100.00')
  })
})

describe('header and totals', () => {
  it('shows no Sharpe, Sortino or return % wording anywhere on the page', async () => {
    summaryFn.mockResolvedValue(summary({ trades: [trade()] }))
    const { container } = renderIt({ period: '30d', range: undefined })
    await screen.findByTestId('discretionary-summary')
    expect(container.textContent ?? '').not.toMatch(/sharpe|sortino|return %|capital basis/i)
  })
})

describe('MAE / MFE reasons', () => {
  it('reads as full phrases when coverage is partial', async () => {
    const gone = (id: number, reason: string, text: string) =>
      excursionPosition({
        id,
        status: 'unavailable',
        reason,
        reason_text: text,
        mae_points: null,
        mfe_points: null,
      })
    const data = excursions({
      positions: [
        excursionPosition({ id: 1 }),
        gone(2, 'historify_incomplete', 'price data for that day is not complete yet'),
        gone(3, 'missing_bars', 'some minutes are missing'),
      ],
    })
    data.aggregates.coverage = {
      available: 1,
      total: 7,
      label: '1 of 7 positions available',
      unavailable_reasons: { historify_incomplete: 6, missing_bars: 6 },
    }
    const { ExcursionPanel } = await import('./ExcursionPanel')
    render(
      <ExcursionPanel
        data={data}
        loading={false}
        failed={false}
        onRetry={() => {}}
        formatCurrency={money}
      />
    )
    const line = screen.getByTestId('excursions-coverage')
    expect(line).toHaveTextContent('1 of 7 completed positions')
    expect(line).toHaveTextContent('minute')
    expect(line).toHaveTextContent(
      '6 positions: price data for that day is not complete yet; 6 positions: some minutes are missing'
    )
  })

  it('uses the server wording after the count and colon when zero positions have data', async () => {
    const withText = excursions({
      positions: [
        excursionPosition({
          status: 'unavailable',
          reason: 'fill_outside_session',
          reason_text: 'a fill is outside the exchange session',
          mae_points: null,
          mfe_points: null,
        }),
      ],
    })
    withText.aggregates.coverage = {
      available: 0,
      total: 20,
      label: '0 of 20 positions available',
      unavailable_reasons: { fill_outside_session: 20 },
    }
    const { ExcursionPanel } = await import('./ExcursionPanel')
    render(
      <ExcursionPanel
        data={withText}
        loading={false}
        failed={false}
        onRetry={() => {}}
        formatCurrency={money}
      />
    )
    const none = screen.getByTestId('excursions-none')
    expect(none).toHaveTextContent('0 of 20 completed positions have 1-minute price bars')
    expect(none).toHaveTextContent('20 positions: a fill is outside the exchange session')
    expect(none.textContent ?? '').not.toMatch(/\(\d+ a fill|; \d+ price data/)
  })
})
