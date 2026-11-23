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
// 9 close dates (more than 7: the day curve) with the same total, 85.5
const LONG_NETS: [string, number][] = [
  ['2026-11-02', 10],
  ['2026-11-03', -10],
  ['2026-11-04', 10],
  ['2026-11-05', -10],
  ['2026-11-06', 10],
  ['2026-11-09', -10],
  ...NETS,
]

function multiDay(extra: Record<string, unknown> = {}, nets: [string, number][] = NETS) {
  const base = summary()
  const daily = nets.map(([date, net]) => ({
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
  it('draws the cumulative net of the daily rows for a long range and ends at the period net P&L', async () => {
    summaryFn.mockResolvedValue(multiDay({}, LONG_NETS))
    renderIt({ onSelectDate: () => {} })
    const card = await screen.findByTestId('equity-curve-card')
    expect(within(card).getByText('Equity curve')).toBeInTheDocument()
    const chart = within(card).getByTestId('equity-chart')
    expect(chart).toHaveAttribute('data-mode', 'day')
    // nine days with a row; a weekday without one is not a point
    expect(chart).toHaveAttribute('data-count', '9')
    expect(Number(chart.getAttribute('data-last'))).toBeCloseTo(85.5, 2)
    // the last point equals the period net: no line says so (only a mismatch is shown)
    expect(within(card).queryByTestId('equity-net-reconciliation')).not.toBeInTheDocument()
    // labelled as cumulative net P&L, never as a balance or a return
    expect(within(card).getByTestId('net-equity-caption')).toHaveTextContent(
      /Cumulative net P&L after estimated charges/
    )
    // the "not a balance or a return" disclosure moved to the caption tooltip
    expect(within(card).getByTestId('net-equity-caption')).toHaveAttribute(
      'title',
      expect.stringMatching(/not an account balance or a return on capital/)
    )
    // one curve: no reading sentence, no drawdown figures in its text, no second chart
    expect(screen.getAllByTestId('equity-chart')).toHaveLength(1)
    expect(within(card).queryByTestId('equity-reading')).not.toBeInTheDocument()
    expect(within(card).getByTestId('equity-text-summary').textContent).not.toMatch(/drawdown/i)
  })

  it('warns when the last point differs from the period net P&L', async () => {
    summaryFn.mockResolvedValue(multiDay({ realized_net_of_charges: 90 }, LONG_NETS))
    renderIt()
    expect(await screen.findByTestId('equity-net-reconciliation')).toHaveAttribute(
      'data-kind',
      'mismatch'
    )
  })

  it('draws no daily bar chart (the calendar is the per-day view)', async () => {
    summaryFn.mockResolvedValue(multiDay({}, LONG_NETS))
    renderIt({ onSelectDate: () => {} })
    await screen.findByTestId('equity-curve-card')
    expect(screen.queryByTestId('daily-pnl-chart')).not.toBeInTheDocument()
    expect(screen.queryByTestId('daily-pnl-bar')).not.toBeInTheDocument()
    expect(screen.queryByText('Daily net P&L')).not.toBeInTheDocument()
    expect(screen.getByTestId('pnl-calendar')).toBeInTheDocument()
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

  it('draws one curve for a short range: per closed trade, not a second day curve', async () => {
    summaryFn.mockResolvedValue(multiDay())
    renderIt({ period: '7d' })
    const card = await screen.findByTestId('equity-curve-card')
    expect(within(card).getByText('Trade-by-trade P&L')).toBeInTheDocument()
    expect(within(card).getByTestId('equity-chart')).toHaveAttribute('data-mode', 'position')
    expect(screen.getAllByTestId('equity-chart')).toHaveLength(1)
    expect(screen.queryByTestId('equity-card')).not.toBeInTheDocument()
    expect(within(card).queryByText('Equity curve')).not.toBeInTheDocument()
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
    const card = await screen.findByTestId('equity-curve-card')
    expect(within(card).getByTestId('equity-chart')).toHaveAttribute('data-count', '1')
    expect(within(card).getByTestId('net-equity-caption')).toHaveTextContent(
      /Net P&L after estimated charges.*1 completed trade/
    )
    expect(within(card).getByTestId('net-equity-caption')).toHaveAttribute(
      'title',
      expect.stringMatching(/Unrealized P&L is not included/)
    )
  })

  it('draws exactly one curve for a single session, and no daily bars', async () => {
    summaryFn.mockResolvedValue(summary())
    renderIt({ period: '1d' })
    await screen.findByTestId('equity-curve-card')
    expect(screen.getAllByTestId('equity-chart')).toHaveLength(1)
    expect(screen.queryByTestId('daily-pnl-chart')).not.toBeInTheDocument()
  })

  it('uses the same filtered response: a broker pick is sent to the server and the curve follows', async () => {
    summaryFn.mockResolvedValue(multiDay({}, LONG_NETS))
    renderIt()
    await screen.findByTestId('equity-curve-card')
    await userEvent.click(screen.getByRole('combobox', { name: 'Broker' }))
    const option = await screen.findByRole('option', { name: 'flattrade' })
    summaryFn.mockResolvedValue(
      multiDay(
        {
          realized_net_of_charges: 60,
        },
        LONG_NETS.slice(0, -1)
      )
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

describe('Performance breakdowns follow the data', () => {
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
    const card = await screen.findByTestId('breakdowns-card')
    for (const name of ['Symbol', 'Long / Short', 'Time of day', 'Product']) {
      expect(within(card).getByRole('heading', { name })).toBeInTheDocument()
    }
    expect(within(card).queryByRole('tab')).not.toBeInTheDocument()
    // tables only: no bar chart for any repeated dimension
    expect(screen.queryByTestId('symbol-chart')).not.toBeInTheDocument()
    expect(screen.queryByTestId('time-of-day-chart')).not.toBeInTheDocument()
    expect(screen.queryByTestId('net-bar')).not.toBeInTheDocument()
    // every section's table is visible together, no click
    expect(within(card).getByText('09:15-10:00')).toBeInTheDocument()
    expect(card.querySelectorAll('table')).toHaveLength(4)
  })

  it('shows every section that has two or more groups at once, without any click, and omits the rest', async () => {
    summaryFn.mockResolvedValue(
      rich({
        by_symbol: [breakdownRow('INFY')],
        by_product: [breakdownRow('MIS')],
      })
    )
    renderIt()
    const card = await screen.findByTestId('breakdowns-card')
    // present together: direction and time of day have two groups
    for (const name of ['Long / Short', 'Time of day']) {
      expect(within(card).getByRole('heading', { name })).toBeInTheDocument()
    }
    expect(within(card).getByText('09:15-10:00')).toBeInTheDocument()
    expect(within(card).getByText('Short')).toBeInTheDocument()
    // absent: one symbol and one product are nothing to compare, and no empty stand-in is drawn
    for (const name of ['Symbol', 'Product']) {
      expect(within(card).queryByRole('heading', { name })).not.toBeInTheDocument()
    }
    expect(within(card).queryByText('None in this period')).not.toBeInTheDocument()
    expect(within(card).queryByRole('tab')).not.toBeInTheDocument()
    // order of the headings follows the fixed section order
    const headings = within(card)
      .getAllByRole('heading', { level: 3 })
      .map((h) => h.textContent)
    expect(headings.indexOf('Long / Short')).toBeLessThan(headings.indexOf('Time of day'))
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
    for (const name of ['Symbol', 'Long / Short', 'Time of day', 'Product']) {
      expect(screen.queryByRole('heading', { name })).not.toBeInTheDocument()
    }
    expect(screen.queryByTestId('breakdowns-card')).not.toBeInTheDocument()
  })
})
