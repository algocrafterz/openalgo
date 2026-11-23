import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  breakdownRow,
  consistency,
  excursions,
  monthRows,
  riskBlock,
  summary,
  tradingBreakdowns,
  weekdayRows,
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
  EquityCanvas: ({ points, mode }: { points: { equity: number }[]; mode: string }) => (
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
const SLOW = { timeout: 5000 }
const TODAY = '2026-11-23'

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

function dayRow(date: string, net: number) {
  return {
    ...summary().by_period.daily[0],
    period: date,
    label: date,
    start: date,
    end: date,
    realized_gross: net + 10,
    charges: 10,
    net_of_charges: net,
    net_charges_complete: true,
    charges_unavailable_trades: 0,
  }
}

/** A summary for `start`..`end` whose daily rows are those inside it and whose net is their sum. */
function answer(
  rows: [string, number][],
  start: string,
  end: string,
  extra: Record<string, unknown> = {}
) {
  const base = summary()
  const daily = rows.filter(([d]) => d >= start && d <= end).map(([d, n]) => dayRow(d, n))
  const net = Math.round(daily.reduce((a, r) => a + r.net_of_charges, 0) * 100) / 100
  return summary({
    period: { period: 'custom', start, end, reliable_since: '2025-01-01' },
    realized_net_of_charges: net,
    by_period: { ...base.by_period, daily },
    consistency: consistency(),
    risk: riskBlock(),
    today: { ...base.today, session_date: TODAY },
    ...extra,
  })
}

const ALL_ROWS: [string, number][] = [
  ['2026-08-27', 300],
  ['2026-09-14', -120],
  ['2026-10-26', 40],
  ['2026-11-16', 100],
  ['2026-11-17', -40],
  ['2026-11-19', 25.5],
]

/** The page asks for 2026-11-16..2026-11-23; any custom range is answered from ALL_ROWS. */
function serve(pageStart = '2026-11-16', extra: Record<string, unknown> = {}) {
  summaryFn.mockImplementation(
    (_period: string, range: { start: string; end: string } | undefined, _filters: unknown) => {
      const r = range ?? { start: pageStart, end: TODAY }
      return Promise.resolve(answer(ALL_ROWS, r.start, r.end, extra))
    }
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  excursionsFn.mockResolvedValue(excursions())
  serve()
})

describe('Equity Curve range choices', () => {
  it('offers 1M, 3M, 6M and All, asks the existing summary for a custom range and changes only the curve', async () => {
    renderIt()
    const card = await screen.findByTestId('equity-curve-card')
    const choices = within(card).getByTestId('equity-range-choices')
    for (const key of ['period', '1m', '3m', '6m', 'all']) {
      expect(within(choices).getByTestId(`equity-range-${key}`)).toBeEnabled()
    }
    expect(within(card).getByTestId('equity-range-label')).toHaveTextContent(
      'Report period: 16 Nov 2026 to 23 Nov 2026.'
    )
    const netBefore = screen.getByTestId('summary-net').textContent

    await userEvent.click(within(card).getByTestId('equity-range-3m'))
    await waitFor(
      () =>
        expect(summaryFn).toHaveBeenCalledWith(
          'custom',
          { start: '2026-08-23', end: TODAY },
          { broker: undefined, account: undefined, symbol: undefined },
          false,
          'all'
        ),
      SLOW
    )
    await waitFor(
      () =>
        expect(within(card).getByTestId('equity-range-label')).toHaveTextContent(
          'Range shown: 23 Aug 2026 to 23 Nov 2026.'
        ),
      SLOW
    )
    // the curve now runs over the days of that range, ending at that range's net
    const chart = within(card).getByTestId('equity-chart')
    expect(chart).toHaveAttribute('data-count', '6')
    expect(Number(chart.getAttribute('data-last'))).toBeCloseTo(305.5, 2)
    // the report date and the rest of the page did not move
    expect(screen.getByTestId('summary-net').textContent).toBe(netBefore)
    expect(screen.getByText('Report period:')).toBeInTheDocument()
    expect(screen.getByText(/16 Nov 2026 - 23 Nov 2026/)).toBeInTheDocument()
    // and it says why its total is not the report period's net
    expect(within(card).getByTestId('equity-range-label')).toHaveTextContent(
      'This range is separate from the report period above (16 Nov 2026 to 23 Nov 2026), so its total can differ'
    )
    expect(within(card).getByTestId('equity-net-reconciliation')).toHaveAttribute(
      'data-kind',
      'match'
    )
    expect(within(card).getByTestId('equity-net-reconciliation')).toHaveTextContent('of this range')
  })

  it('starts All at the first tracked date', async () => {
    renderIt()
    await userEvent.click(await screen.findByTestId('equity-range-all'))
    await waitFor(
      () =>
        expect(summaryFn).toHaveBeenCalledWith(
          'custom',
          { start: '2025-01-01', end: TODAY },
          expect.anything(),
          false,
          'all'
        ),
      SLOW
    )
  })

  it('keeps the broker filter on the range request', async () => {
    renderIt()
    await screen.findByTestId('equity-curve-card')
    await userEvent.click(screen.getByRole('combobox', { name: 'Broker' }))
    await userEvent.click(await screen.findByRole('option', { name: 'flattrade' }))
    await userEvent.click(await screen.findByTestId('equity-range-6m'))
    await waitFor(
      () =>
        expect(summaryFn).toHaveBeenCalledWith(
          'custom',
          { start: '2026-05-23', end: TODAY },
          { broker: 'flattrade', account: undefined, symbol: undefined },
          false,
          'all'
        ),
      SLOW
    )
  })

  it('switches off a range that is longer than the tracked history and says why', async () => {
    serve('2026-11-16', {
      period: { period: 'custom', start: '2026-11-16', end: TODAY, reliable_since: '2026-10-25' },
    })
    renderIt()
    const card = await screen.findByTestId('equity-curve-card')
    expect(within(card).getByTestId('equity-range-1m')).toBeDisabled()
    expect(within(card).getByTestId('equity-range-3m')).toBeDisabled()
    expect(within(card).getByTestId('equity-range-6m')).toBeDisabled()
    expect(within(card).getByTestId('equity-range-3m')).toHaveAttribute(
      'title',
      'Your tracked history starts on 2026-10-25, which is less than 3 months ago.'
    )
    expect(within(card).getByTestId('equity-range-all')).toBeEnabled()
    summaryFn.mockClear()
    await userEvent.click(within(card).getByTestId('equity-range-3m'))
    expect(summaryFn).not.toHaveBeenCalled()
  })

  it('shows no choices when the history adds nothing to the report period', async () => {
    serve('2026-11-16', {
      period: { period: 'custom', start: '2026-11-16', end: TODAY, reliable_since: '2026-11-16' },
    })
    renderIt()
    const card = await screen.findByTestId('equity-curve-card')
    expect(within(card).queryByTestId('equity-range-choices')).not.toBeInTheDocument()
    expect(within(card).getByTestId('equity-chart')).toBeInTheDocument()
  })

  it('does not pretend: a range with fewer than two trading days draws no curve', async () => {
    renderIt()
    await userEvent.click(await screen.findByTestId('equity-range-1m'))
    // 2026-10-23..2026-11-23 holds the four rows from 26 Oct; use a history with one row instead
    summaryFn.mockImplementation(
      (_p: string, range: { start: string; end: string } | undefined) => {
        const r = range ?? { start: '2026-11-16', end: TODAY }
        return Promise.resolve(
          answer(r.start === '2026-11-16' ? ALL_ROWS : [['2026-11-17', -40]], r.start, r.end)
        )
      }
    )
    await userEvent.click(screen.getByTestId('equity-range-6m'))
    expect(await screen.findByTestId('equity-range-empty', {}, SLOW)).toHaveTextContent(
      'fewer than two trading days'
    )
    expect(screen.getByTestId('equity-curve-card')).not.toHaveTextContent('Rs 0.00')
  })

  it('goes back to the report period without a request', async () => {
    renderIt()
    await userEvent.click(await screen.findByTestId('equity-range-3m'))
    await screen.findByText(/Range shown/, {}, SLOW)
    summaryFn.mockClear()
    await userEvent.click(screen.getByTestId('equity-range-period'))
    expect(await screen.findByText(/Report period: 16 Nov 2026 to 23 Nov 2026/)).toBeInTheDocument()
    expect(screen.queryByText(/This range is separate/)).not.toBeInTheDocument()
  })
})

describe('missing days are flagged, never zero', () => {
  it('names days whose broker trades were not collected and leaves them out of the daily bars', async () => {
    serve('2026-11-16', {
      sync_health: {
        last_successful_sync: null,
        runs_by_day: {},
        days_without_sync: ['2026-11-18'],
      },
    })
    renderIt()
    const card = await screen.findByTestId('equity-curve-card')
    expect(within(card).getByTestId('equity-uncollected-days')).toHaveTextContent(
      'Your broker trades were not collected on 18 Nov. Those days have no point'
    )
    // 16, 17, 19 are bars; 18 is a flagged gap, 20 (a weekday with no row) a plain gap
    expect(within(card).getAllByTestId('daily-pnl-bar')).toHaveLength(3)
    expect(within(card).getAllByTestId('daily-pnl-uncollected')).toHaveLength(1)
    expect(within(card).getByTestId('daily-pnl-uncollected')).toHaveAttribute(
      'title',
      expect.stringContaining('no figure for this day')
    )
    expect(within(card).queryByTestId('daily-pnl-gap')).not.toBeInTheDocument()
  })

  it('leaves a gap for a weekday without a row, between the real days', async () => {
    renderIt()
    const card = await screen.findByTestId('equity-curve-card')
    const bars = within(card).getAllByTestId('daily-pnl-bar')
    expect(bars).toHaveLength(3)
    // 18 November (Wednesday) is a gap with no bar and no zero
    expect(within(card).getAllByTestId('daily-pnl-gap').length).toBeGreaterThanOrEqual(1)
    expect(within(card).getByTestId('daily-pnl-chart')).toHaveTextContent('not a day of zero')
  })
})

describe('calendar month, equity curve end and report period net agree on the same range', () => {
  it('shows one figure in all three, and the same rows behind them', async () => {
    summaryFn.mockImplementation(
      (_p: string, range: { start: string; end: string } | undefined) => {
        const r = range ?? { start: '2026-11-01', end: TODAY }
        return Promise.resolve(
          answer(ALL_ROWS, r.start < '2026-11-01' ? '2026-11-01' : r.start, r.end)
        )
      }
    )
    renderIt({ onSelectDate: () => {} })
    const calendarTotal = await screen.findByTestId('calendar-total-net', {}, SLOW)
    await waitFor(() => expect(calendarTotal).toHaveTextContent('Rs 85.50'), SLOW)
    const curve = within(screen.getByTestId('equity-curve-card')).getByTestId('equity-chart')
    expect(Number(curve.getAttribute('data-last'))).toBeCloseTo(85.5, 2)
    expect(screen.getByTestId('summary-net')).toHaveTextContent('Rs 85.50')
    expect(
      within(screen.getByTestId('equity-curve-card')).getByTestId('equity-net-reconciliation')
    ).toHaveAttribute('data-kind', 'match')
  })
})

describe('Trading statistics hierarchy', () => {
  it('puts expectancy, profit factor and drawdown above the supporting figures, without repeating the summary', async () => {
    renderIt()
    const stats = await screen.findByTestId('discretionary-statistics')
    const primary = within(stats).getByTestId('stats-primary')
    const support = within(stats).getByTestId('stats-supporting')
    for (const label of ['Expectancy (net)', 'Profit factor (net)', 'Max drawdown (net)']) {
      expect(within(primary).getByText(label)).toBeInTheDocument()
    }
    for (const label of [
      'Gross realized',
      'Charges (est.)',
      'Avg win (net)',
      'Avg loss (net)',
      'Largest winner',
      'Largest loser',
    ]) {
      expect(within(support).getByText(label)).toBeInTheDocument()
      expect(within(primary).queryByText(label)).not.toBeInTheDocument()
    }
    expect(primary.compareDocumentPosition(support) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // the primary tiles are marked larger; one home per metric, and nothing of the summary here
    expect(
      [...primary.querySelectorAll('[data-size]')].every(
        (e) => e.getAttribute('data-size') === 'primary'
      )
    ).toBe(true)
    for (const label of ['Net realized', 'Completed trades', 'Win rate']) {
      expect(within(stats).queryByText(new RegExp(`^${label}`))).not.toBeInTheDocument()
    }
    for (const label of ['Expectancy (net)', 'Profit factor (net)', 'Max drawdown (net)']) {
      expect(screen.getAllByText(label)).toHaveLength(1)
    }
  })
})

describe('breakdowns appear only with something to compare', () => {
  const rich = (extra: Record<string, unknown> = {}) =>
    answer(ALL_ROWS, '2026-11-16', TODAY, {
      // one populated group in each table: nothing to compare
      breakdowns: tradingBreakdowns({
        time_of_day: [breakdownRow('09:15-10:00')],
        hold_bucket: [breakdownRow('15-60m')],
      }),
      by_symbol: [breakdownRow('INFY')],
      by_side: [breakdownRow('LONG', { trades: 5 })],
      by_product: [breakdownRow('MIS')],
      monthly: null,
      weekday: null,
      // the sample is shown beside the figures; it does not gate a table
      sample: {
        completed_trades: 12,
        trading_days: 12,
        min_trades: 30,
        min_days: 10,
        sufficient: false,
      },
      ...extra,
    })
  const mondayOnly = () =>
    weekdayRows().map((r) => (r.weekday === 'Mon' ? r : { ...r, positions: 0 }))

  it('renders no section, heading or shell for empty or single-category weekday and month data', async () => {
    summaryFn.mockResolvedValue(rich({ weekday: mondayOnly(), monthly: monthRows().slice(0, 1) }))
    renderIt()
    await screen.findByTestId('discretionary-statistics')
    expect(screen.queryByTestId('calendar-card')).not.toBeInTheDocument()
    expect(screen.queryByTestId('performance-breakdowns')).not.toBeInTheDocument()
    expect(screen.queryByText('Performance breakdowns')).not.toBeInTheDocument()
    expect(screen.queryByTestId('more-breakdowns')).not.toBeInTheDocument()
    expect(screen.queryByText(/Net P&L by weekday/)).not.toBeInTheDocument()
  })

  it('treats empty arrays like no data', async () => {
    summaryFn.mockResolvedValue(rich({ weekday: [], monthly: [] }))
    renderIt()
    await screen.findByTestId('discretionary-statistics')
    expect(screen.queryByTestId('performance-breakdowns')).not.toBeInTheDocument()
  })

  it('does not point at More breakdowns when that section is not shown', async () => {
    summaryFn.mockResolvedValue(rich())
    renderIt()
    await screen.findByTestId('discretionary-statistics')
    const note = screen.queryByTestId('profile-note')
    if (note) expect(note).not.toHaveTextContent('More breakdowns')
  })

  it('shows the weekday chart, titled for what is in it, once two weekdays have trades', async () => {
    summaryFn.mockResolvedValue(rich({ weekday: weekdayRows() }))
    renderIt()
    const section = await screen.findByTestId('performance-breakdowns')
    const card = within(section).getByTestId('calendar-card')
    expect(card).toHaveTextContent('Net P&L by weekday')
    expect(card).not.toHaveTextContent('Net P&L by month')
    expect(within(card).queryByTestId('monthly-chart')).not.toBeInTheDocument()
    expect(within(card).getByTestId('weekday-chart')).toBeInTheDocument()
  })
})
