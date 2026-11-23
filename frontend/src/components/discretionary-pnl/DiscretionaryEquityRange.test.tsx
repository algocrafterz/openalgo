import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  breakdownRow,
  consistency,
  excursions,
  monthRows,
  riskBlock,
  summary,
  trade,
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
    // 3 close dates: one curve, drawn per closed trade; the report period is stated above it
    expect(within(card).getByTestId('equity-chart')).toHaveAttribute('data-mode', 'position')
    expect(within(card).queryByTestId('equity-range-label')).not.toBeInTheDocument()
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
      'Differs from the report period (16 Nov 2026 to 23 Nov 2026)'
    )
    // the last point equals the range's net: nothing to warn about, so no line is shown
    expect(within(card).queryByTestId('equity-net-reconciliation')).not.toBeInTheDocument()
    expect(chart).toHaveAttribute('data-mode', 'day')
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
      'Fewer than two trading days'
    )
    expect(screen.getByTestId('equity-curve-card')).not.toHaveTextContent('Rs 0.00')
  })

  it('goes back to the report period without a request', async () => {
    renderIt()
    await userEvent.click(await screen.findByTestId('equity-range-3m'))
    await screen.findByText(/Range shown/, {}, SLOW)
    summaryFn.mockClear()
    await userEvent.click(screen.getByTestId('equity-range-period'))
    // back on the report period's own curve (per trade for 3 dates), with no new request
    expect(
      await within(screen.getByTestId('equity-curve-card')).findByTestId('equity-chart')
    ).toHaveAttribute('data-mode', 'position')
    expect(screen.queryByText(/Range shown/)).not.toBeInTheDocument()
    expect(screen.queryByText(/This range is separate/)).not.toBeInTheDocument()
    expect(summaryFn).not.toHaveBeenCalled()
  })
})

describe('missing days are flagged, never zero', () => {
  it('names days whose broker trades were not collected, on the day curve', async () => {
    serve('2026-11-16', {
      sync_health: {
        last_successful_sync: null,
        runs_by_day: {},
        days_without_sync: ['2026-11-18'],
      },
    })
    renderIt()
    const card = await screen.findByTestId('equity-curve-card')
    // 3 close dates draw per trade; the day curve (and its flagged gaps) is the range curve
    await userEvent.click(within(card).getByTestId('equity-range-1m'))
    expect(await within(card).findByTestId('equity-uncollected-days', {}, SLOW)).toHaveTextContent(
      'Your broker trades were not collected on 18 Nov. Those days have no point'
    )
    expect(within(card).getByTestId('equity-chart')).toHaveAttribute('data-mode', 'day')
    expect(within(card).queryByTestId('daily-pnl-chart')).not.toBeInTheDocument()
    expect(card).not.toHaveTextContent('Rs 0.00')
  })
})

describe('calendar month, equity curve end and report period net agree on the same range', () => {
  it('shows one figure in all three, and the same rows behind them', async () => {
    summaryFn.mockImplementation(
      (_p: string, range: { start: string; end: string } | undefined) => {
        const r = range ?? { start: '2026-11-01', end: TODAY }
        const start = r.start < '2026-11-01' ? '2026-11-01' : r.start
        const rows = ALL_ROWS.filter(([d]) => d >= start && d <= r.end)
        return Promise.resolve(
          answer(ALL_ROWS, start, r.end, {
            // the trades behind the rows, so the per-trade curve ends at the same net
            trades: rows.map(([d, n], i) =>
              trade({ id: i + 1, trade_date: d, closed_at: `${d} 10:00:00`, net_pnl: n })
            ),
          })
        )
      }
    )
    renderIt({ onSelectDate: () => {} })
    const calendarTotal = await screen.findByTestId('calendar-total-net', {}, SLOW)
    await waitFor(() => expect(calendarTotal).toHaveTextContent('Rs 85.50'), SLOW)
    const curve = within(screen.getByTestId('equity-curve-card')).getByTestId('equity-chart')
    expect(Number(curve.getAttribute('data-last'))).toBeCloseTo(85.5, 2)
    expect(screen.getByTestId('summary-net')).toHaveTextContent('Rs 85.50')
    // all three agree, so there is no reconciliation line at all
    expect(
      within(screen.getByTestId('equity-curve-card')).queryByTestId('equity-net-reconciliation')
    ).not.toBeInTheDocument()
  })
})

describe('Trading statistics hierarchy', () => {
  it('puts expectancy, profit factor and drawdown in a compact row under the headline, once, and the supporting figures in the card', async () => {
    renderIt()
    const stats = await screen.findByTestId('discretionary-statistics')
    const header = screen.getByTestId('discretionary-summary')
    const keyRow = within(header).getByTestId('summary-key-stats')
    const headline = within(header).getByTestId('summary-net').parentElement as HTMLElement
    const support = within(stats).getByTestId('stats-supporting')
    expect(within(stats).queryByTestId('stats-primary')).not.toBeInTheDocument()
    for (const label of ['Expectancy (net)', 'Profit factor (net)', 'Max drawdown (net)']) {
      expect(within(keyRow).getByText(label)).toBeInTheDocument()
      expect(within(stats).queryByText(label)).not.toBeInTheDocument()
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
      expect(within(keyRow).queryByText(label)).not.toBeInTheDocument()
    }
    // the row follows the three headline cards directly, and the statistics card is below it
    expect(headline.compareDocumentPosition(keyRow) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(keyRow.compareDocumentPosition(stats) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // compact tiles, and nothing of the headline repeated in the row or the card
    expect(
      [...keyRow.querySelectorAll('[data-size]')].every(
        (e) => e.getAttribute('data-size') === 'support'
      )
    ).toBe(true)
    for (const label of ['Net realized', 'Completed trades', 'Win rate']) {
      expect(within(stats).queryByText(new RegExp(`^${label}`))).not.toBeInTheDocument()
      expect(within(keyRow).queryByText(new RegExp(`^${label}`))).not.toBeInTheDocument()
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
    expect(screen.queryByTestId('breakdowns-card')).not.toBeInTheDocument()
    expect(screen.queryByText('Performance breakdowns')).not.toBeInTheDocument()
    expect(screen.queryByTestId('more-breakdowns')).not.toBeInTheDocument()
    expect(screen.queryByText(/Net P&L by weekday/)).not.toBeInTheDocument()
  })

  it('treats empty arrays like no data', async () => {
    summaryFn.mockResolvedValue(rich({ weekday: [], monthly: [] }))
    renderIt()
    await screen.findByTestId('discretionary-statistics')
    expect(screen.queryByTestId('breakdowns-card')).not.toBeInTheDocument()
  })

  it('does not point at More breakdowns when that section is not shown', async () => {
    summaryFn.mockResolvedValue(rich())
    renderIt()
    await screen.findByTestId('discretionary-statistics')
    const note = screen.queryByTestId('profile-note')
    if (note) expect(note).not.toHaveTextContent('More breakdowns')
  })

  it('has no month or close-date weekday card even when the server sends their rows', async () => {
    summaryFn.mockResolvedValue(rich({ weekday: weekdayRows(), monthly: monthRows() }))
    renderIt()
    await screen.findByTestId('discretionary-statistics')
    expect(screen.queryByTestId('calendar-card')).not.toBeInTheDocument()
    expect(screen.queryByTestId('weekday-chart')).not.toBeInTheDocument()
    expect(screen.queryByTestId('monthly-chart')).not.toBeInTheDocument()
    expect(screen.queryByText(/Net P&L by (month|weekday)/)).not.toBeInTheDocument()
  })
})
