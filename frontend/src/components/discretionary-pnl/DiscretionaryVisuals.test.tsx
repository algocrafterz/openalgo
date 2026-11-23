import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  breakdownRow,
  consistency,
  excursions,
  riskBlock,
  summary,
  tradingBreakdowns,
} from '@/test/discretionaryFixtures'
import { render, screen, userEvent, waitFor, within } from '@/test/test-utils'

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
  EquityCanvas: ({
    points,
    mode,
  }: {
    points: { equity: number; closed_at: string }[]
    mode: string
  }) => (
    <div
      data-testid="equity-chart"
      data-mode={mode}
      data-last={points.length ? points[points.length - 1].equity : ''}
      data-count={points.length}
    />
  ),
}))

const { DiscretionaryPerformance } = await import('./DiscretionaryPerformance')

const money = (v: number) => `Rs ${v.toFixed(2)}`

function renderIt(props: Partial<React.ComponentProps<typeof DiscretionaryPerformance>> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <DiscretionaryPerformance
        period="30d"
        rangeReady
        formatCurrency={money}
        isDarkMode={false}
        {...props}
      />
    </QueryClientProvider>
  )
}

const NETS: [string, number][] = [
  ['2026-11-16', 100],
  ['2026-11-17', -40],
  ['2026-11-19', 25.5],
]

function multiDay(extra: Record<string, unknown> = {}) {
  const base = summary()
  const daily = NETS.map(([date, net]) => ({
    ...base.by_period.daily[0],
    period: date,
    label: date,
    start: date,
    end: date,
    realized_gross: net + 10,
    charges: 10,
    net_of_charges: net,
    net_charges_complete: true,
    charges_unavailable_trades: 0,
  }))
  return summary({
    realized_net_of_charges: 85.5,
    by_period: { ...base.by_period, daily },
    consistency: consistency(),
    risk: riskBlock(),
    ...extra,
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  excursionsFn.mockResolvedValue(excursions())
})

describe('Equity curve on the page', () => {
  it('draws the cumulative net of the daily rows and ends at the period net P&L', async () => {
    summaryFn.mockResolvedValue(multiDay())
    renderIt({ onSelectDate: () => {} })
    const card = await screen.findByTestId('equity-curve-card')
    expect(within(card).getByText('Equity curve')).toBeInTheDocument()
    const chart = within(card).getByTestId('equity-chart')
    expect(chart).toHaveAttribute('data-mode', 'day')
    // three days with a row; 18 November has none and is not a point
    expect(chart).toHaveAttribute('data-count', '3')
    expect(Number(chart.getAttribute('data-last'))).toBeCloseTo(85.5, 2)
    expect(within(card).getByTestId('equity-net-reconciliation')).toHaveAttribute(
      'data-kind',
      'match'
    )
    // labelled as cumulative net P&L, never as a balance or a return
    expect(within(card).getByTestId('net-equity-caption')).toHaveTextContent(
      /Cumulative net P&L.*not an account balance or a return on capital/
    )
  })

  it('warns when the last point differs from the period net P&L', async () => {
    summaryFn.mockResolvedValue(multiDay({ realized_net_of_charges: 90 }))
    renderIt()
    expect(await screen.findByTestId('equity-net-reconciliation')).toHaveAttribute(
      'data-kind',
      'mismatch'
    )
  })

  it('draws the supporting daily bars from the NET P&L, not the gross', async () => {
    summaryFn.mockResolvedValue(multiDay())
    renderIt()
    const card = await screen.findByTestId('equity-curve-card')
    const bars = within(card).getAllByTestId('daily-pnl-bar')
    expect(bars).toHaveLength(3)
    expect(bars[0].getAttribute('title')).toMatch(/^2026-11-16: net Rs 100\.00; gross Rs 110\.00/)
    expect(within(card).getByText('Daily net P&L')).toBeInTheDocument()
    expect(within(card).queryByText(/gross\)/)).not.toBeInTheDocument()
    expect(within(card).getByTestId('daily-pnl-days')).toHaveTextContent(
      '2 profitable / 1 losing day(s)'
    )
  })

  it('sits after the P&L Calendar and before Trading statistics', async () => {
    summaryFn.mockResolvedValue(multiDay())
    renderIt({ onSelectDate: () => {} })
    await screen.findByTestId('pnl-calendar')
    const ids = ['pnl-calendar', 'equity-curve-card', 'discretionary-statistics']
    const els = ids.map((id) => screen.getByTestId(id))
    for (let i = 1; i < els.length; i++) {
      expect(
        els[i - 1].compareDocumentPosition(els[i]) & Node.DOCUMENT_POSITION_FOLLOWING
      ).toBeTruthy()
    }
  })

  it('keeps Trade-by-trade P&L as a separate, per-trade chart for a short range', async () => {
    summaryFn.mockResolvedValue(multiDay())
    renderIt({ period: '7d' })
    await screen.findByTestId('equity-curve-card')
    const tradeCard = screen.getByTestId('equity-card')
    expect(within(tradeCard).getByText('Trade-by-trade P&L')).toBeInTheDocument()
    expect(within(tradeCard).getByTestId('equity-chart')).toHaveAttribute('data-mode', 'position')
  })

  it('still draws the trade-by-trade chart with its explanation for a single completed trade', async () => {
    const one = summary()
    summaryFn.mockResolvedValue({
      ...one,
      trades: one.trades.slice(0, 1),
      trades_total: 1,
      trades_shown: 1,
      metrics: { ...one.metrics, trades_count: 1 },
    })
    renderIt({ period: '1d' })
    const card = await screen.findByTestId('equity-card')
    expect(within(card).getByTestId('equity-chart')).toHaveAttribute('data-count', '1')
    expect(within(card).getByTestId('net-equity-caption')).toHaveTextContent(
      /Net realised P&L after estimated charges.*excludes unrealized P&L.*Based on 1 completed trade/
    )
  })

  it('is not shown for a single session, which Trade-by-trade P&L covers', async () => {
    summaryFn.mockResolvedValue(summary())
    renderIt({ period: '1d' })
    await screen.findByTestId('equity-card')
    expect(screen.queryByTestId('equity-curve-card')).not.toBeInTheDocument()
    expect(screen.queryByTestId('daily-pnl-chart')).not.toBeInTheDocument()
  })

  it('uses the same filtered response: a broker pick is sent to the server and the curve follows', async () => {
    summaryFn.mockResolvedValue(multiDay())
    renderIt()
    await screen.findByTestId('equity-curve-card')
    await userEvent.click(screen.getByRole('combobox', { name: 'Broker' }))
    const option = await screen.findByRole('option', { name: 'flattrade' })
    summaryFn.mockResolvedValue(
      multiDay({
        realized_net_of_charges: 60,
        by_period: {
          ...multiDay().by_period,
          daily: multiDay().by_period.daily.slice(0, 2),
        },
      })
    )
    await userEvent.click(option)
    await waitFor(() =>
      expect(summaryFn).toHaveBeenLastCalledWith(
        '30d',
        undefined,
        { broker: 'flattrade', account: undefined, symbol: undefined },
        false,
        'all'
      )
    )
    await waitFor(() =>
      expect(
        Number(
          within(screen.getByTestId('equity-curve-card'))
            .getByTestId('equity-chart')
            .getAttribute('data-last')
        )
      ).toBeCloseTo(60, 2)
    )
  })
})

describe('Performance breakdown charts follow the data', () => {
  const rich = (extra: Record<string, unknown> = {}) => {
    const s = multiDay()
    return {
      ...s,
      metrics: { ...s.metrics, trades_count: 12 },
      breakdowns: tradingBreakdowns({
        time_of_day: [
          breakdownRow('09:15-10:00', { trades: 6, net_of_charges: 300 }),
          breakdownRow('10:00-11:00', { trades: 6, net_of_charges: -120 }),
        ],
      }),
      by_symbol: [
        breakdownRow('RELIANCE', { net_of_charges: 300 }),
        breakdownRow('INFY', { net_of_charges: -120 }),
      ],
      by_side: [breakdownRow('LONG', { trades: 5 }), breakdownRow('SHORT', { trades: 4 })],
      by_product: [breakdownRow('MIS'), breakdownRow('CNC')],
      ...extra,
    }
  }

  it('shows symbol, direction, product and time-of-day when the thresholds are met', async () => {
    summaryFn.mockResolvedValue(rich())
    renderIt()
    await screen.findByTestId('performance-breakdowns')
    expect(await screen.findByTestId('symbol-chart')).toBeInTheDocument()
    expect(screen.getByText('Long vs Short')).toBeInTheDocument()
    expect(screen.getByText('By product')).toBeInTheDocument()
    const tod = screen.getByTestId('time-of-day-chart')
    const bars = within(tod).getAllByTestId('net-bar')
    expect(bars).toHaveLength(2)
    expect(bars[0].getAttribute('title')).toBe('09:15-10:00 (6): net +Rs 300.00 from 6 trade(s)')
  })

  it('hides them when there is one symbol, one product, one direction and one time-of-day group, whatever the trade count', async () => {
    const s = rich()
    summaryFn.mockResolvedValue({
      ...s,
      metrics: { ...s.metrics, trades_count: 9 },
      by_symbol: [breakdownRow('INFY')],
      by_product: [breakdownRow('MIS')],
      by_side: [breakdownRow('LONG', { trades: 8 }), breakdownRow('SHORT', { trades: 0 })],
      breakdowns: tradingBreakdowns({
        time_of_day: [breakdownRow('09:15-10:00', { trades: 9 })],
      }),
    })
    renderIt()
    await screen.findByTestId('equity-curve-card')
    expect(screen.queryByTestId('symbol-chart')).not.toBeInTheDocument()
    expect(screen.queryByText('Long vs Short')).not.toBeInTheDocument()
    expect(screen.queryByText('By product')).not.toBeInTheDocument()
    expect(screen.queryByTestId('time-of-day-chart')).not.toBeInTheDocument()
  })
})
