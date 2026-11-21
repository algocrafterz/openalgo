import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { DiscretionaryTrade } from '@/api/discretionaryPnl'
import { exportCsvUrl } from '@/api/discretionaryPnl'
import {
  consistency,
  costsBlock,
  dataQuality,
  emptyToday,
  excursionPosition,
  excursions,
  monthRows,
  pointsRow,
  riskBlock,
  summary,
  todayClosed,
  trade,
  tradeManagement,
  tradingBreakdowns,
  unresolvedInRange,
  weekdayRows,
} from '@/test/discretionaryFixtures'
import { render, screen, userEvent, within } from '@/test/test-utils'

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
  EquityCanvas: ({ points, mode }: { points: unknown[]; mode: string }) => (
    <div data-testid="equity-chart" data-mode={mode}>
      {points.length} points
    </div>
  ),
}))

const { DiscretionaryPerformance } = await import('./DiscretionaryPerformance')
const { ExcursionPanel } = await import('./ExcursionPanel')

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

function dailyRows(n: number) {
  const base = summary().by_period.daily[0]
  return Array.from({ length: n }, (_, i) => {
    const date = `2026-${i < 20 ? '10' : '11'}-${String((i % 28) + 1).padStart(2, '0')}`
    return { ...base, period: date, label: date, start: date, end: date }
  })
}

function full(days: number, months: number) {
  const base = summary()
  return summary({
    by_period: { ...base.by_period, daily: dailyRows(days) },
    consistency: consistency(),
    risk: riskBlock(),
    breakdowns: tradingBreakdowns(),
    monthly: monthRows().slice(0, months),
    weekday: weekdayRows(),
    trade_management: tradeManagement(),
    costs: costsBlock(),
    points_by_instrument: [pointsRow()],
    today: emptyToday({ closed_positions: [todayClosed()], completed_positions: 1, net: 20 }),
  })
}

/** Advanced analysis is collapsed by default: open it before looking inside. */
async function openAdvanced() {
  const toggle = await screen.findByTestId('advanced-analysis-toggle')
  if (toggle.getAttribute('aria-expanded') !== 'true') await userEvent.click(toggle)
}

beforeEach(() => {
  vi.clearAllMocks()
  summaryFn.mockResolvedValue(summary())
  excursionsFn.mockResolvedValue(excursions())
})

describe('summary header', () => {
  it('leads with three large figures and one dense supporting row', async () => {
    summaryFn.mockResolvedValue(full(3, 1))
    renderIt()
    const header = await screen.findByTestId('discretionary-summary')
    // the verdict moved to Trading insights, below the closed trades
    expect(within(header).queryByTestId('summary-verdict')).not.toBeInTheDocument()
    expect(screen.getByTestId('summary-verdict')).toHaveTextContent(
      'Profitable on net: Rs 47.50 after estimated charges from 2 completed trades. Win rate 50.0%.'
    )
    expect(within(header).getByTestId('summary-net')).toHaveTextContent('Rs 47.50')
    expect(within(header).getByTestId('summary-count')).toHaveTextContent('2')
    expect(within(header).getByTestId('summary-count')).toHaveTextContent('Completed trades')
    expect(within(header).getByTestId('summary-win-rate')).toHaveTextContent('50.0%')
    const support = within(header).getByTestId('summary-support')
    for (const label of [
      'Profit factor (net)',
      'Expectancy (net)',
      'Max drawdown (net)',
      'Gross realized',
      'Charges (est.)',
      'Largest winner',
      'Largest loser',
    ]) {
      expect(within(support).getByText(label)).toBeInTheDocument()
    }
    // the drawdown figure is the Risk card's
    expect(within(support).getByTestId('summary-drawdown')).toHaveTextContent('-Rs 320.50')
    expect(within(header).getByRole('link', { name: 'Jump to closed trades' })).toHaveAttribute(
      'href',
      '#discretionary-ledger'
    )
  })

  it('names one weakness from existing fields only', async () => {
    summaryFn.mockResolvedValue(full(3, 1))
    renderIt()
    // charges are 12.5% of gross profit and the largest loss is smaller than the largest win here,
    // so the first applicable rule is the current drawdown
    expect(await screen.findByTestId('summary-weakness')).toHaveTextContent(
      'Needs attention: Currently Rs 120.00 below the best net equity so far.'
    )
  })

  it("merges today's session into one line and keeps every figure one click away", async () => {
    summaryFn.mockResolvedValue(full(3, 1))
    renderIt()
    const line = await screen.findByTestId('today-line')
    expect(line).toHaveTextContent('Today (2026-11-17): net Rs 20.00 after estimated charges')
    expect(line.tagName).toBe('DETAILS')
    const today = within(line).getByTestId('discretionary-today')
    for (const label of ['Realised (gross)', 'Net after est. charges', 'Completed positions']) {
      expect(within(today).getByText(label)).toBeInTheDocument()
    }
  })

  it('says the session is the range shown when the period is just today', async () => {
    const s = full(1, 1)
    summaryFn.mockResolvedValue({
      ...s,
      period: { ...s.period, start: '2026-11-17', end: '2026-11-17' },
    })
    renderIt({ period: '1d' })
    expect(await screen.findByTestId('today-line')).toHaveTextContent(
      "Today's session (2026-11-17) is the range shown above"
    )
  })
})

describe('four groups and the metric inventory', () => {
  it('shows the four groups, in two columns from md and one column below', async () => {
    summaryFn.mockResolvedValue(full(10, 2))
    renderIt()
    // Performance sits with the summary; risk and management are in Advanced analysis
    const perf = await screen.findByTestId('discretionary-groups-performance')
    expect(within(perf).getByTestId('group-performance')).toBeInTheDocument()
    await openAdvanced()
    const groups = await screen.findByTestId('discretionary-groups-analysis')
    expect(groups.className).toContain('grid-cols-1')
    expect(groups.className).toContain('md:grid-cols-2')
    for (const id of ['risk-strip', 'group-trade-management']) {
      expect(within(groups).getByTestId(id)).toBeInTheDocument()
    }
    // MAE / MFE and profit capture are in the MAE / MFE card only
    expect(screen.queryByTestId('group-trade-quality')).not.toBeInTheDocument()
  })

  it('still shows every metric label the page showed before the regrouping', async () => {
    summaryFn.mockResolvedValue(full(40, 4))
    renderIt()
    await screen.findByTestId('discretionary-groups-performance')
    await openAdvanced()
    const previous = [
      // the twelve former metric cards
      'Net realized (after est. charges)',
      'Gross realized',
      'Unrealized',
      'Total won (net)',
      'Total lost (net)',
      'Win rate (net)',
      'Profit factor (net)',
      'Completed trades',
      'Avg win (net)',
      'Avg loss (net)',
      'Expectancy (net)',
      // risk and consistency (largest win / loss and the maximum drawdown are in the summary only)
      'Largest winner',
      'Largest loser',
      'Avg daily P&L (gross)',
      'Profitable / losing days',
      'Max consecutive winning trades',
      'Max consecutive losing trades',
      'Current streak',
      'Avg holding time',
      'Max drawdown (net)',
      'Current drawdown',
      // trade management and costs
      'Winners: average hold',
      'Losers: average hold',
      'Median net P&L',
      'Charges (est.)',
      'Charges as % of gross profit',
      // tables and cards
      'Points by instrument',
      'MAE / MFE',
      'Closed trades',
      'Open positions',
      'How you trade',
      'P&L by period',
      'Net P&L by month and weekday',
      'By symbol',
      'Long vs Short',
      'By product',
      'Intraday vs overnight',
    ]
    const missing = previous.filter(
      (label) => screen.queryAllByText(label, { exact: true }).length === 0
    )
    expect(missing).toEqual([])
    // the total card keeps its own label (no unrealized figure requested here)
    expect(screen.getByText('Total', { selector: 'div' })).toBeInTheDocument()
  })

  it('shows each hold figure once: the old Breakdowns duplicate is gone when trade management is sent', async () => {
    summaryFn.mockResolvedValue(full(40, 4))
    renderIt()
    await openAdvanced()
    await screen.findByTestId('group-trade-management')
    expect(screen.queryByTestId('hold-durations')).not.toBeInTheDocument()
    expect(screen.getByTestId('tm-winner-hold')).toBeInTheDocument()
    expect(screen.getByTestId('risk-hold')).toBeInTheDocument()
  })

  it('falls back to the hold cards when an older server sends no trade-management block', async () => {
    renderIt()
    await openAdvanced()
    const hold = await screen.findByTestId('hold-durations')
    expect(within(hold).getByText('Winners')).toBeInTheDocument()
    expect(within(hold).getByText('30m')).toBeInTheDocument()
  })
})

describe('sections follow the data', () => {
  it('hides the month, weekday, period and daily-bar sections for one date and says so', async () => {
    summaryFn.mockResolvedValue(full(1, 1))
    renderIt({ period: '1d' })
    expect(await screen.findByTestId('profile-note')).toHaveTextContent(
      'Month, weekday and period tables are hidden because this view covers one trading day'
    )
    await openAdvanced()
    expect(screen.queryByTestId('calendar-card')).not.toBeInTheDocument()
    expect(screen.queryByTestId('period-card')).not.toBeInTheDocument()
    expect(screen.queryByTestId('daily-pnl-chart')).not.toBeInTheDocument()
    expect(screen.getByText('Session progression')).toBeInTheDocument()
    expect(screen.getByTestId('equity-chart')).toHaveAttribute('data-mode', 'position')
    // collapsed, not removed
    expect(screen.getByTestId('more-breakdowns')).not.toHaveAttribute('open')
  })

  it('shows weekday and the daily tab only for 2 to 7 dates, with the note', async () => {
    summaryFn.mockResolvedValue(full(5, 2))
    renderIt({ period: '7d' })
    await openAdvanced()
    await screen.findByTestId('calendar-card')
    expect(screen.queryByTestId('monthly-chart')).not.toBeInTheDocument()
    expect(screen.getByTestId('weekday-chart')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Daily' })).toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: 'Weekly' })).not.toBeInTheDocument()
    expect(screen.getByTestId('daily-pnl-chart')).toBeInTheDocument()
    expect(screen.getByText('Cumulative net P&L')).toBeInTheDocument()
    expect(screen.getByTestId('profile-note')).toHaveTextContent('5 trading days')
  })

  it('shows everything, open, for a long range, with the end-of-day curve explained', async () => {
    summaryFn.mockResolvedValue(full(40, 4))
    renderIt({ period: 'all' })
    await openAdvanced()
    await screen.findByTestId('calendar-card')
    expect(screen.getByTestId('monthly-chart')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Monthly' })).toBeInTheDocument()
    expect(screen.getByTestId('more-breakdowns')).toHaveAttribute('open')
    expect(screen.queryByTestId('profile-note')).not.toBeInTheDocument()
    expect(screen.getByTestId('equity-chart')).toHaveAttribute('data-mode', 'day')
    expect(screen.getByTestId('equity-reconciliation')).toHaveTextContent('end-of-day')
  })

  for (const [name, days, months, period] of [
    ['session', 1, 1, '1d'],
    ['short', 4, 1, '7d'],
    ['medium', 12, 2, '30d'],
    ['long', 40, 4, 'all'],
  ] as const) {
    it(`keeps the unresolved banner, warnings, data quality and classification in the ${name} profile`, async () => {
      summaryFn.mockResolvedValue({
        ...full(days, months),
        unresolved_in_range: unresolvedInRange(),
        warnings: ['1 trade(s) have no charge estimate (instrument not modelled).'],
      })
      renderIt({ period })
      expect(await screen.findByTestId('unresolved-warning')).toBeInTheDocument()
      expect(screen.getByTestId('discretionary-warnings')).toBeInTheDocument()
      expect(screen.getByTestId('discretionary-filters')).toBeInTheDocument()
      expect(screen.getByTestId('export-csv')).toBeInTheDocument()
    })
  }

  it('keeps the CSV link and the ledger as they were, whatever the profile', async () => {
    summaryFn.mockResolvedValue(full(1, 1))
    renderIt({ period: '1d' })
    const link = await screen.findByTestId('export-csv')
    expect(link.getAttribute('href')).toBe(exportCsvUrl('1d', undefined, {}, 'all'))
    expect(screen.getByTestId('ledger-card')).toHaveAttribute('id', 'discretionary-ledger')
  })

  it('orders the page: summary, session progression, closed trades, insights, advanced analysis, data quality', async () => {
    summaryFn.mockResolvedValue({ ...full(12, 2), data_quality: dataQuality() })
    renderIt()
    await screen.findByTestId('discretionary-groups-performance')
    await openAdvanced()
    const ids = [
      'discretionary-summary',
      'discretionary-groups-performance',
      'equity-card',
      'ledger-card',
      'open-positions-card',
      'trading-insights',
      'advanced-analysis',
      'discretionary-groups-analysis',
      'how-you-trade',
      'points-card',
      'excursion-card',
      'data-quality',
    ]
    const els = ids.map((id) => screen.getByTestId(id))
    for (let i = 1; i < els.length; i++) {
      expect(
        els[i - 1].compareDocumentPosition(els[i]) & Node.DOCUMENT_POSITION_FOLLOWING
      ).toBeTruthy()
    }
  })
})

describe('equity captions on the page', () => {
  it('says the curve equals the Risk card when nothing is cut', async () => {
    const t = (id: number, net: number): DiscretionaryTrade =>
      trade({
        id,
        closed_at: `2026-11-16 10:0${id}:00`,
        net_pnl: net,
        charges: 1,
        charges_status: 'ok',
      })
    summaryFn.mockResolvedValue(
      summary({
        trades: [t(1, -50), t(2, 30), t(3, -20), t(4, 80)],
        trades_total: 4,
        risk: riskBlock({
          positions: 4,
          net_unavailable_positions: 0,
          max_drawdown: 50,
          current_drawdown: 0,
          peak_equity: 40,
          latest_equity: 40,
        }),
      })
    )
    renderIt({ period: '1d' })
    expect(await screen.findByTestId('equity-reconciliation')).toHaveTextContent(
      'Max drawdown on this curve equals the Risk card'
    )
  })

  it('says exactly why when the trade list is cut to the newest N', async () => {
    summaryFn.mockResolvedValue(
      summary({ trades_total: 640, trades_shown: 500, trades_truncated: 140, risk: riskBlock() })
    )
    renderIt({ period: '1d' })
    expect(await screen.findByTestId('equity-reconciliation')).toHaveTextContent(
      'Curve shows the newest 500 of 640 closed positions'
    )
    expect(screen.getByTestId('equity-reconciliation')).toHaveTextContent(
      'the Risk card covers all 640'
    )
  })

  it('says so when a position has no net and the Risk card counts them differently', async () => {
    summaryFn.mockResolvedValue(
      summary({ risk: riskBlock({ positions: 2, net_unavailable_positions: 0 }) })
    )
    renderIt({ period: '1d' })
    const caption = await screen.findByTestId('equity-reconciliation')
    expect(caption).toHaveAttribute('data-kind', 'mismatch')
    expect(caption).toHaveTextContent('1 left out')
    expect(caption).toHaveTextContent('Not reconciled')
  })
})

describe('MAE / MFE presentation', () => {
  const noop = () => {}
  const panel = (data = excursions()) =>
    render(
      <ExcursionPanel
        data={data}
        loading={false}
        failed={false}
        onRetry={noop}
        formatCurrency={money}
      />
    )

  it('shows a legend with shape and colour, labelled axes and a tooltip on hover and focus', async () => {
    panel(
      excursions({
        positions: [
          excursionPosition({ id: 1 }),
          excursionPosition({
            id: 2,
            net_pnl: -30,
            mae_points: 6,
            mfe_points: 3,
            symbol: 'INFY',
            direction: 'SHORT',
          }),
          excursionPosition({ id: 3, net_pnl: 0 }),
        ],
      })
    )
    const scatter = screen.getByTestId('mae-mfe-scatter')
    expect(scatter).toHaveTextContent('MAE (points per unit)')
    expect(scatter).toHaveTextContent('MFE (points per unit)')
    const legend = within(scatter).getByTestId('scatter-legend')
    expect(legend).toHaveTextContent('Green circle: winner (1)')
    expect(legend).toHaveTextContent('Red diamond: loser (1)')
    expect(legend).toHaveTextContent('Grey square: break-even or no net (1)')
    expect(scatter.querySelector('svg')?.getAttribute('class')).not.toContain('max-w-[520px]')
    const loser = within(scatter).getByTestId('scatter-loser')
    expect(loser).toHaveAttribute('tabindex', '0')
    await userEvent.hover(loser)
    const tip = within(scatter).getByTestId('scatter-tooltip')
    expect(tip).toHaveTextContent('INFY Short')
    expect(tip).toHaveTextContent('Entry 2026-11-16 09:20')
    expect(tip).toHaveTextContent('Exit 2026-11-16 10:05')
    expect(tip).toHaveTextContent('Net P&L: Rs -30.00')
    expect(tip).toHaveTextContent('MAE 6.00 points per unit')
    expect(tip).toHaveTextContent('MFE 3.00 points per unit')
    await userEvent.unhover(loser)
    expect(within(scatter).queryByTestId('scatter-tooltip')).not.toBeInTheDocument()
    within(scatter).getByTestId('scatter-winner').focus()
    expect(await within(scatter).findByTestId('scatter-tooltip')).toHaveTextContent('RELIANCE Long')
  })

  it('offers the plotted values as a table for keyboard and screen reader users', () => {
    panel()
    const table = screen.getByTestId('scatter-table')
    expect(table.tagName).toBe('DETAILS')
    expect(table.querySelectorAll('tbody tr').length).toBeGreaterThan(0)
  })

  it('shows profit capture as a labelled block with the formula, and a dash with a reason when unavailable', () => {
    const { unmount } = panel()
    const block = screen.getByTestId('profit-capture-block')
    expect(block).toHaveTextContent('Winners captured')
    expect(block).toHaveTextContent('of the favourable move (minute bars, lower bound) (median')
    expect(block.getAttribute('title')).toContain('gross points a winner made / its MFE points')
    // the aggregate table and its caveat are as before
    expect(screen.getByTestId('profit-capture-row')).toBeInTheDocument()
    expect(screen.getByTestId('excursions-precision')).toHaveTextContent(
      'it can pass 100% because MFE is a lower bound'
    )
    unmount()
    const none = excursions()
    none.aggregates.winners.profit_capture = { count: 0, average: null, median: null }
    panel(none)
    const empty = screen.getByTestId('profit-capture-block')
    expect(empty).toHaveTextContent(
      'Winners captured — of the favourable move (minute bars, lower bound)'
    )
    expect(empty).toHaveTextContent('no winning position has both gross points')
    expect(empty).not.toHaveTextContent('0.0%')
  })
})
