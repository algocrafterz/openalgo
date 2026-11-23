import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { DiscretionaryTrade } from '@/api/discretionaryPnl'
import { exportCsvUrl } from '@/api/discretionaryPnl'
import { fmtNetMoney, fmtProfitFactor } from '@/lib/discretionaryPnlView'
import {
  breakdownRow,
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
    sample: { ...base.sample!, trading_days: days },
    trade_management: tradeManagement(),
    costs: costsBlock(),
    points_by_instrument: [pointsRow(), pointsRow({ symbol: 'INFY' })],
    by_side: [breakdownRow('LONG', { trades: 5 }), breakdownRow('SHORT', { trades: 4 })],
    by_product: [breakdownRow('MIS'), breakdownRow('NRML')],
    by_holding: {
      intraday: breakdownRow('', { key: undefined }),
      overnight: breakdownRow('', { key: undefined, trades: 2 }),
    },
    today: emptyToday({ closed_positions: [todayClosed()], completed_positions: 1, net: 20 }),
  })
}

/** `full` with enough completed trades for the time-of-day and holding-time patterns. */
function fullRich(days: number, months: number) {
  const f = full(days, months)
  return { ...f, metrics: { ...f.metrics, trades_count: 12 } }
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
  it('leads with three large figures; every other statistic has one home in Trading statistics', async () => {
    summaryFn.mockResolvedValue(full(3, 1))
    renderIt()
    const header = await screen.findByTestId('discretionary-summary')
    // the summary cards carry net P&L, count and win rate; no sentence repeats them
    expect(screen.queryByTestId('summary-verdict')).not.toBeInTheDocument()
    expect(within(header).getByTestId('summary-net')).toHaveTextContent('Rs 47.50')
    expect(within(header).getByTestId('summary-count')).toHaveTextContent('2')
    expect(within(header).getByTestId('summary-count')).toHaveTextContent('Completed trades')
    expect(within(header).getByTestId('summary-win-rate')).toHaveTextContent('50.0%')
    // the supporting row moved: nothing of it is in the header any more
    expect(screen.queryByTestId('summary-support')).not.toBeInTheDocument()
    const stats = screen.getByTestId('discretionary-statistics')
    // the second row under the headline carries these three, once, and not the statistics card
    for (const label of ['Profit factor (net)', 'Expectancy (net)', 'Max drawdown (net)']) {
      expect(within(header).getAllByText(label)).toHaveLength(1)
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
      expect(within(header).queryByText(label)).not.toBeInTheDocument()
      expect(within(stats).getAllByText(label)).toHaveLength(1)
    }
    // the drawdown figure is in the second row under the headline
    expect(within(header).getByTestId('summary-drawdown')).toHaveTextContent('-Rs 320.50')
    expect(within(header).getByRole('link', { name: 'Jump to closed trades' })).toHaveAttribute(
      'href',
      '#discretionary-ledger'
    )
  })

  it('has no Trading insights card: its rules are hints on the Charges and Largest loser tiles', async () => {
    const f = fullRich(3, 1)
    const c = f.consistency!
    summaryFn.mockResolvedValue({
      ...f,
      costs: costsBlock({ charges_pct_of_gross_profit: 42.5 }),
      consistency: {
        ...c,
        largest_win: { ...c.largest_win!, net_pnl: 100 },
        largest_loss: { ...c.largest_loss!, net_pnl: -250 },
      },
    })
    renderIt()
    await screen.findByTestId('discretionary-statistics')
    expect(screen.queryByTestId('trading-insights')).not.toBeInTheDocument()
    expect(screen.queryByTestId('summary-weakness')).not.toBeInTheDocument()
    expect(screen.queryByText(/Needs attention|Trading insights/)).not.toBeInTheDocument()
    expect(screen.getByTestId('summary-charges')).toHaveTextContent(
      '42.5% of the gross profit of your winning trades, over 12 completed trades'
    )
    expect(screen.getByTestId('summary-largest-loss')).toHaveTextContent(
      'larger than your largest winner (Rs 100.00), over 12 completed trades'
    )
    // the former drawdown rule is not restated anywhere: the Max drawdown tile carries it
    expect(document.body.textContent ?? '').not.toMatch(/below its highest point in this period/)
  })

  it('states no charges or loser hint when neither rule applies', async () => {
    summaryFn.mockResolvedValue(fullRich(3, 1))
    renderIt()
    await screen.findByTestId('discretionary-statistics')
    expect(screen.getByTestId('summary-charges')).not.toHaveTextContent(
      /turned a gross profit|% of the gross profit/
    )
    expect(screen.getByTestId('summary-largest-loss')).not.toHaveTextContent(/larger than/)
  })

  it("shows today's session as one line, without a panel, tiles or a second closed-trades table", async () => {
    const onSelectDate = vi.fn()
    summaryFn.mockResolvedValue(full(3, 1))
    renderIt({ onSelectDate })
    const line = await screen.findByTestId('today-line')
    expect(line).toHaveTextContent(
      'Today (2026-11-17): net Rs 20.00 after estimated charges, 1 closed, 0 open.'
    )
    expect(line.tagName).toBe('DIV')
    expect(line.querySelectorAll('table')).toHaveLength(0)
    expect(screen.queryByTestId('discretionary-today')).not.toBeInTheDocument()
    expect(screen.queryByText("Today's Performance")).not.toBeInTheDocument()
    // exit-fill count and partially closed are hint text
    expect(within(line).getByTestId('today-hint')).toHaveTextContent(/exit fills.*partially closed/)
    await userEvent.click(within(line).getByRole('button', { name: /show today's trades/i }))
    expect(onSelectDate).toHaveBeenCalledWith('2026-11-17')
  })

  it('shows no second Today panel when the report date is today, and names the date', async () => {
    const s = full(1, 1)
    summaryFn.mockResolvedValue({
      ...s,
      period: { ...s.period, start: '2026-11-17', end: '2026-11-17' },
    })
    renderIt({ period: '1d' })
    await screen.findByTestId('discretionary-summary')
    expect(screen.queryByTestId('today-line')).not.toBeInTheDocument()
    expect(screen.getByText(/Report date:/)).toBeInTheDocument()
  })

  it('drops the Today line for a historical date when today has no activity', async () => {
    const s = full(1, 1)
    summaryFn.mockResolvedValue({ ...s, today: emptyToday() })
    renderIt({ period: '1d' })
    await screen.findByTestId('discretionary-summary')
    expect(screen.queryByTestId('today-line')).not.toBeInTheDocument()
  })
})

describe('metric inventory after removing duplicates', () => {
  it('has one Risk and consistency card with trade management inside it, and no other group cards', async () => {
    summaryFn.mockResolvedValue(full(10, 2))
    renderIt()
    await screen.findByTestId('discretionary-statistics')
    expect(screen.queryByTestId('group-performance')).not.toBeInTheDocument()
    expect(screen.queryByTestId('discretionary-groups-performance')).not.toBeInTheDocument()
    expect(screen.queryByTestId('discretionary-groups-analysis')).not.toBeInTheDocument()
    expect(screen.queryByTestId('group-trade-management')).not.toBeInTheDocument()
    await openAdvanced()
    const risk = await screen.findByTestId('risk-strip')
    expect(within(risk).getByTestId('trade-management')).toBeInTheDocument()
    expect(screen.getAllByTestId('trade-management')).toHaveLength(1)
    // MAE / MFE and profit capture are in the MAE / MFE card only
    expect(screen.queryByTestId('group-trade-quality')).not.toBeInTheDocument()
  })

  it('still shows every metric the page showed, each in its place and none twice as a label', async () => {
    const rich = fullRich(40, 4)
    summaryFn.mockResolvedValue({
      ...rich,
      breakdowns: tradingBreakdowns({
        weekday: [breakdownRow('Mon', { trades: 3 }), breakdownRow('Tue', { trades: 4 })],
        hold_bucket: [
          breakdownRow('15-60m', { trades: 3 }),
          breakdownRow('overnight', { trades: 2 }),
        ],
      }),
    })
    renderIt()
    await screen.findByTestId('discretionary-statistics')
    await openAdvanced()
    const labels = [
      'Net realized (after est. charges)',
      'Gross realized',
      'Win rate (net)',
      'Profit factor (net)',
      'Completed trades',
      'Avg win (net)',
      'Avg loss (net)',
      'Expectancy (net)',
      'Largest winner',
      'Largest loser',
      'Avg daily P&L (net)',
      'Profitable / losing days (gross)',
      'Max consecutive winning trades',
      'Max consecutive losing trades',
      'Current streak',
      'Avg holding time',
      'Max drawdown (net)',
      'Winners: average hold',
      'Losers: average hold',
      'Median net P&L',
      'Charges (est.)',
      "Est. charges as % of winners' gross profit",
      'Points by instrument',
      'MAE / MFE',
      'Closed trades',
      'Open positions',
      'Where and when you make or lose money',
      'P&L by period',
    ]
    const missing = labels.filter((l) => screen.queryAllByText(l, { exact: true }).length === 0)
    expect(missing).toEqual([])
    const twice = labels.filter(
      (l) =>
        !['Closed trades', 'Open positions', 'Charges (est.)', 'Points by instrument'].includes(
          l
        ) && screen.queryAllByText(l, { exact: true }).length > 1
    )
    expect(twice).toEqual([])
    // the removed tiles' figures live in the hints of the tiles they were folded into
    const text = document.body.textContent ?? ''
    expect(text).toMatch(/won Rs [\d.,-]+ \/ lost Rs [\d.,-]+/) // Total won / lost, in the Profit factor hint
    expect(screen.getByTestId('summary-current-drawdown')).toHaveTextContent(
      /now .*(below the highest point|at its highest point)/
    )
    // the sections of the one breakdown card, all shown together
    for (const name of [
      'Symbol',
      'Long / Short',
      'Time of day',
      'Weekday',
      'Holding time',
      'Product',
      'Intraday vs overnight',
    ]) {
      expect(
        within(screen.getByTestId('breakdowns-card')).getByRole('heading', { name })
      ).toBeInTheDocument()
    }
    // removed blocks, as titles
    for (const gone of [
      'Performance',
      'Trading insights',
      'Daily net P&L',
      'Net P&L by month and weekday',
      'Trade management and costs',
      'Current drawdown',
      'Total won (net)',
      'Total lost (net)',
    ]) {
      expect(screen.queryAllByText(gone, { exact: true })).toHaveLength(0)
    }
  })

  it('states the current drawdown once, with its basis, in the Max drawdown tile', async () => {
    summaryFn.mockResolvedValue(full(3, 1))
    renderIt()
    await screen.findByTestId('discretionary-statistics')
    expect(screen.getAllByTestId('summary-current-drawdown')).toHaveLength(1)
    expect(screen.getByTestId('summary-current-drawdown')).toHaveTextContent(
      'now Rs 120.00 below the highest point (net, by close time)'
    )
    expect(screen.queryByTestId('risk-current-drawdown')).not.toBeInTheDocument()
    // no other element names a current drawdown amount
    expect(document.body.textContent ?? '').not.toMatch(/current drawdown/i)
  })

  it('shows each hold figure once: the old Breakdowns duplicate is gone when trade management is sent', async () => {
    summaryFn.mockResolvedValue(full(40, 4))
    renderIt()
    await openAdvanced()
    await screen.findByTestId('risk-strip')
    expect(screen.queryByTestId('hold-durations')).not.toBeInTheDocument()
    expect(screen.getByTestId('tm-winner-hold')).toBeInTheDocument()
    expect(screen.getByTestId('tm-winner-hold')).toHaveTextContent('median')
    expect(screen.getByTestId('tm-loser-hold')).toHaveTextContent('median')
    expect(screen.getByTestId('risk-hold')).toBeInTheDocument()
  })

  it('falls back to the hold tiles when an older server sends no trade-management block', async () => {
    renderIt()
    await openAdvanced()
    const hold = await screen.findByTestId('hold-durations')
    expect(within(hold).getByText('Winners: average hold')).toBeInTheDocument()
    expect(within(hold).getByText('30m')).toBeInTheDocument()
  })
})

describe('sections follow the data', () => {
  it('hides the period tables for one date and says so, with one per-trade curve', async () => {
    summaryFn.mockResolvedValue(full(1, 1))
    renderIt({ period: '1d' })
    expect(await screen.findByTestId('profile-note')).toHaveTextContent(
      'Weekly and monthly tables are hidden for one trading day'
    )
    await openAdvanced()
    expect(screen.queryByTestId('calendar-card')).not.toBeInTheDocument()
    expect(screen.queryByTestId('period-card')).not.toBeInTheDocument()
    expect(screen.queryByTestId('daily-pnl-chart')).not.toBeInTheDocument()
    expect(screen.getByText('Trade-by-trade P&L')).toBeInTheDocument()
    expect(screen.getByTestId('equity-chart')).toHaveAttribute('data-mode', 'position')
    expect(screen.getAllByTestId('equity-chart')).toHaveLength(1)
    // the breakdown card is not collapsed away
    expect(screen.queryByTestId('more-breakdowns')).not.toBeInTheDocument()
  })

  it('shows no period tabs for 2 to 7 dates, with the note, and one per-trade curve', async () => {
    summaryFn.mockResolvedValue(full(5, 2))
    renderIt({ period: '7d' })
    await openAdvanced()
    await screen.findByTestId('risk-strip')
    expect(screen.queryByTestId('period-card')).not.toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: 'Daily' })).not.toBeInTheDocument()
    expect(screen.queryByTestId('weekday-chart')).not.toBeInTheDocument()
    expect(screen.queryByTestId('daily-pnl-chart')).not.toBeInTheDocument()
    expect(screen.getByText('Trade-by-trade P&L')).toBeInTheDocument()
    expect(screen.getAllByTestId('equity-chart')).toHaveLength(1)
    expect(screen.getByTestId('profile-note')).toHaveTextContent('5 trading days')
  })

  it('shows the weekly and monthly tabs (no Daily) and the end-of-day curve for a long range', async () => {
    summaryFn.mockResolvedValue(full(40, 4))
    renderIt({ period: 'all' })
    await openAdvanced()
    await screen.findByTestId('period-card')
    expect(screen.queryByTestId('calendar-card')).not.toBeInTheDocument()
    expect(screen.queryByTestId('monthly-chart')).not.toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: 'Daily' })).not.toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Weekly' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Monthly' })).toBeInTheDocument()
    expect(screen.queryByTestId('profile-note')).not.toBeInTheDocument()
    expect(screen.getByTestId('equity-chart')).toHaveAttribute('data-mode', 'day')
    expect(screen.getAllByTestId('equity-chart')).toHaveLength(1)
    expect(screen.getByTestId('equity-reconciliation')).toHaveTextContent('end-of-day totals')
    // the day-curve note carries no drawdown figure: the Max drawdown tile states it
    expect(screen.getByTestId('equity-reconciliation').textContent).not.toMatch(/Rs \d/)
  })

  for (const [name, days, months, period] of [
    ['session', 1, 1, '1d'],
    ['short', 4, 1, '7d'],
    ['medium', 12, 2, '30d'],
    ['long', 40, 4, 'all'],
  ] as const) {
    it(`keeps the data warning and classification in the ${name} profile`, async () => {
      summaryFn.mockResolvedValue({
        ...full(days, months),
        unresolved_in_range: unresolvedInRange(),
        warnings: ['1 trade(s) have no charge estimate (instrument not modelled).'],
      })
      renderIt({ period })
      expect(await screen.findByTestId('unmatched-fills-note')).toBeInTheDocument()
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

  it('orders the page: summary, calendar, curve, quality, where and when, closed trades, advanced analysis', async () => {
    summaryFn.mockResolvedValue({ ...fullRich(12, 2), data_quality: dataQuality() })
    renderIt({ onSelectDate: () => {} })
    await screen.findByTestId('pnl-calendar')
    await screen.findByTestId('calendar-month-totals')
    await openAdvanced()
    const ids = [
      'discretionary-summary',
      'pnl-calendar',
      'equity-curve-card',
      'discretionary-statistics',
      'breakdowns-card',
      'ledger-card',
      'open-positions-card',
      'advanced-analysis',
      'risk-strip',
      'points-card',
      'excursion-card',
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
  it('stays quiet when the curve equals the Max drawdown figure', async () => {
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
    await screen.findByTestId('equity-chart-block')
    // agreement is the normal case and is not announced; only a difference is shown
    expect(screen.queryByTestId('equity-reconciliation')).not.toBeInTheDocument()
  })

  it('says exactly why when the trade list is cut to the newest N', async () => {
    summaryFn.mockResolvedValue(
      summary({ trades_total: 640, trades_shown: 500, trades_truncated: 140, risk: riskBlock() })
    )
    renderIt({ period: '1d' })
    expect(await screen.findByTestId('equity-reconciliation')).toHaveTextContent(
      'covers the latest 500 of 640 trades'
    )
    expect(screen.getByTestId('equity-reconciliation')).toHaveTextContent(
      'Max drawdown (Rs 320.50) covers all'
    )
  })

  it('says so when a position has no net and the Risk card counts them differently', async () => {
    summaryFn.mockResolvedValue(
      summary({ risk: riskBlock({ positions: 2, net_unavailable_positions: 0 }) })
    )
    renderIt({ period: '1d' })
    const caption = await screen.findByTestId('equity-reconciliation')
    expect(caption).toHaveAttribute('data-kind', 'mismatch')
    expect(caption).toHaveTextContent('Max drawdown (Rs 320.50) counts 2')
    expect(caption).toHaveTextContent('Chart drawdown (Rs 0.00) counts 1 trade;')
    expect(caption.textContent).not.toMatch(/reconcil|Risk card/i)
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
    panel(
      excursions({
        positions: [excursionPosition({ id: 1 }), excursionPosition({ id: 2, net_pnl: -30 })],
      })
    )
    const table = screen.getByTestId('scatter-table')
    expect(table.tagName).toBe('DETAILS')
    expect(table.querySelectorAll('tbody tr').length).toBeGreaterThan(0)
  })

  it('does not show profit capture or exit efficiency: MFE is only a lower bound', () => {
    panel()
    expect(screen.queryByTestId('profit-capture-block')).not.toBeInTheDocument()
    expect(screen.queryByTestId('profit-capture-row')).not.toBeInTheDocument()
    expect(screen.getByTestId('excursion-panel').textContent ?? '').not.toMatch(
      /profit capture|captured|efficiency/i
    )
    // the precision note moved to the tooltip of the coverage line
    expect(screen.getByTestId('excursions-coverage')).toHaveAttribute(
      'title',
      expect.stringMatching(/lower bound/)
    )
  })
})

describe('second metric row, compact open positions and kept coverage notes', () => {
  it('shows net expectancy, profit factor and max drawdown under the headline with the statistics values', async () => {
    const s = full(3, 1)
    summaryFn.mockResolvedValue(s)
    renderIt()
    const header = await screen.findByTestId('discretionary-summary')
    const row = within(header).getByTestId('summary-key-stats')
    const m = s.metrics
    expect(row).toHaveTextContent('Expectancy (net)')
    expect(row).toHaveTextContent(fmtNetMoney(m.expectancy, money))
    expect(row).toHaveTextContent('net, per trade')
    expect(row).toHaveTextContent('Profit factor (net)')
    expect(row).toHaveTextContent(
      fmtProfitFactor(m.profit_factor, m.winning_trades, m.losing_trades, true)
    )
    // total won and lost are folded into this tile's hint
    expect(row).toHaveTextContent(
      `won ${money(m.gross_profit)} / lost ${money(m.gross_loss)} (net, completed trades)`
    )
    expect(row).toHaveTextContent('Max drawdown (net)')
    expect(row).toHaveTextContent('-Rs 320.50')
    expect(row).toHaveTextContent(`over ${s.risk?.positions} trade`)
    // the current drawdown is folded into the Max drawdown hint, with its basis
    expect(row).toHaveTextContent(
      `now ${money(s.risk?.current_drawdown ?? 0)} below the highest point (net, by close time)`
    )
    // tooltips are kept
    expect(within(row).getByText('Profit factor (net)').closest('[title]')).toHaveAttribute(
      'title',
      expect.stringContaining('Above 1.00 means winners outweigh losers')
    )
    // the row sits directly under the three headline cards
    expect(header.children[1].querySelectorAll('[data-testid^="summary-"]').length).toBeGreaterThan(
      2
    )
    expect(header.children[2]).toBe(row)
  })

  it('keeps the headline summary text unchanged', async () => {
    summaryFn.mockResolvedValue(full(3, 1))
    renderIt()
    const header = await screen.findByTestId('discretionary-summary')
    expect(within(header).getByTestId('summary-net')).toHaveTextContent(
      'Net realized (after est. charges)Rs 47.501 trade(s) without a charge estimate: not complete'
    )
    expect(within(header).getByTestId('summary-count')).toHaveTextContent(
      'Completed trades21 wins / 1 losses'
    )
    expect(within(header).getByTestId('summary-win-rate')).toHaveTextContent(
      'Win rate (net)50.0%a win is a profit after estimated charges'
    )
    expect(within(header).getByTestId('win-rate-counts')).toHaveTextContent('1 wins / 1 losses')
  })

  it('shows a dash for expectancy and profit factor, and no drawdown, with no completed trades', async () => {
    const base = full(3, 1)
    summaryFn.mockResolvedValue({
      ...base,
      trades: [],
      trades_total: 0,
      metrics: { ...base.metrics, trades_count: 0, winning_trades: 0, losing_trades: 0 },
    })
    renderIt()
    const header = await screen.findByTestId('discretionary-summary')
    const row = within(header).getByTestId('summary-key-stats')
    expect(within(row).getByText('Expectancy (net)').parentElement).toHaveTextContent('—')
    expect(within(row).getByText('Profit factor (net)').parentElement).toHaveTextContent('—')
  })

  it('renders open positions as one line when there are none, and the card when there are', async () => {
    summaryFn.mockResolvedValue({ ...full(3, 1), positions: [], positions_total: 0 })
    const first = renderIt()
    const line = await screen.findByTestId('open-positions-card')
    expect(line.tagName).toBe('P')
    expect(line).toHaveTextContent(/^No open positions\.$/)
    expect(within(line).queryByRole('table')).not.toBeInTheDocument()
    first.unmount()

    summaryFn.mockResolvedValue(full(3, 1))
    renderIt()
    const card = await screen.findByTestId('open-positions-card')
    expect(card.tagName).not.toBe('P')
    expect(card).toHaveTextContent('Open positions')
    expect(within(card).getByRole('table')).toBeInTheDocument()
  })

  it('keeps the data warning and the MAE / MFE coverage line', async () => {
    summaryFn.mockResolvedValue({
      ...full(3, 2),
      unresolved_in_range: unresolvedInRange(),
      warnings: ['1 trade(s) have no charge estimate (instrument not modelled).'],
    })
    renderIt()
    expect(await screen.findByTestId('unmatched-fills-note')).toBeInTheDocument()
    await openAdvanced()
    expect(await screen.findByTestId('excursions-coverage')).toHaveTextContent(
      /\d+ of \d+ completed positions with 1-minute price data/
    )
  })
})

describe('duplicate information is removed from the page', () => {
  it('has none of the removed blocks, for a long range with every block sent', async () => {
    summaryFn.mockResolvedValue(fullRich(40, 4))
    renderIt({ period: 'all', onSelectDate: () => {} })
    await screen.findByTestId('discretionary-statistics')
    await openAdvanced()
    for (const id of [
      'daily-pnl-chart',
      'daily-pnl-bar',
      'calendar-card',
      'weekday-chart',
      'monthly-chart',
      'symbol-chart',
      'time-of-day-chart',
      'net-bar',
      'group-performance',
      'trading-insights',
      'summary-weakness',
      'stat-win-loss-bars',
      'group-trade-management',
      'how-you-trade',
      'more-breakdowns',
      'performance-breakdowns',
      'equity-card',
      'equity-reading',
      'risk-current-drawdown',
      'discretionary-today',
    ]) {
      expect(screen.queryByTestId(id), id).not.toBeInTheDocument()
    }
    expect(screen.queryByRole('tab', { name: 'Daily' })).not.toBeInTheDocument()
  })

  it('shows one tabbed breakdown card, one table at a time, with the weekday by entry day only', async () => {
    const rich = fullRich(40, 4)
    summaryFn.mockResolvedValue({
      ...rich,
      breakdowns: tradingBreakdowns({
        weekday: [breakdownRow('Mon', { trades: 3 }), breakdownRow('Tue', { trades: 4 })],
      }),
    })
    renderIt({ period: 'all' })
    const card = await screen.findByTestId('breakdowns-card')
    expect(screen.getAllByTestId('breakdowns-card')).toHaveLength(1)
    expect(within(card).getAllByRole('heading', { name: /weekday/i })).toHaveLength(1)
    // the weekday table is on the page without any click
    expect(within(card).getByRole('columnheader', { name: 'Entry day' })).toBeInTheDocument()
    expect(within(card).getAllByRole('columnheader', { name: 'Entry day' })).toHaveLength(1)
    // the page has no other weekday table or chart
    expect(screen.queryByText(/by weekday/i)).not.toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: 'Weekday' })).not.toBeInTheDocument()
  })

  it('draws exactly one cumulative-net curve for every range length', async () => {
    for (const [period, days, months] of [
      ['1d', 1, 1],
      ['7d', 5, 1],
      ['30d', 12, 2],
      ['all', 40, 4],
    ] as const) {
      summaryFn.mockResolvedValue(full(days, months))
      const view = renderIt({ period })
      await screen.findByTestId('equity-curve-card')
      expect(screen.getAllByTestId('equity-chart'), `${period}`).toHaveLength(1)
      expect(screen.getAllByTestId('equity-curve-card')).toHaveLength(1)
      view.unmount()
    }
  })

  it('keeps the calendar day click and the closed-trades jump link', async () => {
    summaryFn.mockResolvedValue(full(10, 2))
    renderIt({ onSelectDate: () => {} })
    await screen.findByTestId('pnl-calendar')
    expect(screen.getByRole('link', { name: 'Jump to closed trades' })).toHaveAttribute(
      'href',
      '#discretionary-ledger'
    )
    expect(document.getElementById('discretionary-ledger')).not.toBeNull()
    expect(document.getElementById('discretionary-points')).toBeNull() // collapsed until opened
    await openAdvanced()
    expect(document.getElementById('discretionary-points')).not.toBeNull()
    expect(document.getElementById('discretionary-excursion')).not.toBeNull()
  })

  it('shows the Net realized once, with no second net total printed beside it', async () => {
    summaryFn.mockResolvedValue(full(3, 1))
    renderIt()
    const net = await screen.findByTestId('summary-net')
    expect(net).toHaveTextContent('Rs 47.50')
    // the Performance card total and the equity "last point equals" line are gone
    expect(screen.queryByTestId('summary-total')).not.toBeInTheDocument()
    expect(screen.queryByText(/The last point .* equals the realised net/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Month net|Net P&L by month/)).toBeNull()
  })

  it('keeps the coverage warnings: charges incomplete, material warning, MAE / MFE count', async () => {
    summaryFn.mockResolvedValue({
      ...full(3, 1),
      unresolved_in_range: unresolvedInRange(),
      warnings: ['1 trade(s) have no charge estimate (instrument not modelled).'],
    })
    renderIt()
    expect(await screen.findByTestId('unmatched-fills-note')).toBeInTheDocument()
    expect(screen.getByTestId('summary-net')).toHaveTextContent(
      'without a charge estimate: not complete'
    )
    expect(screen.getByTestId('summary-charges')).toHaveTextContent('without an estimate')
    await openAdvanced()
    expect(await screen.findByTestId('excursions-coverage')).toHaveTextContent(
      /\d+ of \d+ completed positions with 1-minute price data/
    )
  })
})
