import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DiscretionaryApiError } from '@/api/discretionaryPnl'
import {
  breakdownRow,
  classification,
  consistency,
  costsBlock,
  dataQuality,
  EXCURSION_NOTE,
  emptySummary,
  emptyToday,
  excursionPosition,
  excursions,
  monthRows,
  pointsRow,
  position,
  provisional,
  riskBlock,
  summary,
  todayClosed,
  trade,
  tradeManagement,
  tradingBreakdowns,
  unresolvedInRange,
  weekdayRows,
} from '@/test/discretionaryFixtures'
import { act, render, screen, userEvent, waitFor, within } from '@/test/test-utils'

const summaryFn = vi.fn()
const fillsFn = vi.fn()
const unresolvedFn = vi.fn()
const excursionsFn = vi.fn()

vi.mock('@/api/discretionaryPnl', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/discretionaryPnl')>()
  return {
    ...actual,
    discretionaryPnlApi: {
      status: vi.fn(),
      summary: (...a: unknown[]) => summaryFn(...a),
      excursions: (...a: unknown[]) => excursionsFn(...a),
      tradeFills: (...a: unknown[]) => fillsFn(...a),
      unresolved: (...a: unknown[]) => unresolvedFn(...a),
    },
  }
})
vi.mock('@/hooks/useOrderEventRefresh', () => ({ useOrderEventRefresh: vi.fn() }))
// The app-wide socket, faked: tests emit order_update on it.
const sock = vi.hoisted(() => {
  const handlers = new Map<string, Set<(p: unknown) => void>>()
  return {
    handlers,
    socket: {
      on: (e: string, h: (p: unknown) => void) => {
        if (!handlers.has(e)) handlers.set(e, new Set())
        handlers.get(e)?.add(h)
      },
      off: (e: string, h: (p: unknown) => void) => {
        handlers.get(e)?.delete(h)
      },
    },
    emit: (e: string, p: unknown) => {
      for (const h of [...(handlers.get(e) ?? [])]) h(p)
    },
  }
})
vi.mock('@/components/socket/SocketProvider', () => ({
  useSocketContext: () => ({ socket: sock.socket, playAlertSound: () => {} }),
}))
// The canvas of the new per-position equity chart (lightweight-charts needs a real canvas).
vi.mock('./EquityCanvas', () => ({
  DRAWDOWN_COLOR: '#d97706',
  EquityCanvas: ({ points, mode }: { points: unknown[]; mode: string }) => (
    <div data-testid="equity-chart" data-mode={mode}>
      {points.length} points
    </div>
  ),
}))

const { DiscretionaryPerformance } = await import('./DiscretionaryPerformance')
const { useOrderEventRefresh } = await import('@/hooks/useOrderEventRefresh')

const money = (v: number) => `Rs ${v.toFixed(2)}`

/** The big number of the metric card with this label. */
function cardValue(label: string): string {
  const el = screen.getByText(label, { selector: 'div' })
  return el.parentElement?.querySelector('.font-mono')?.textContent ?? ''
}

/** Server daily rows for `n` consecutive close dates, so the page profile is not the session one. */
function dailyRows(n: number) {
  const base = summary().by_period.daily[0]
  return Array.from({ length: n }, (_, i) => {
    const date = `2026-11-${String(i + 1).padStart(2, '0')}`
    return { ...base, period: date, label: date, start: date, end: date }
  })
}

function withDays(n: number, extra: Partial<ReturnType<typeof summary>> = {}) {
  const base = summary()
  return summary({
    ...extra,
    by_period: { ...(extra.by_period ?? base.by_period), daily: dailyRows(n) },
  })
}

let lastClient: QueryClient

function renderIt(props: Partial<React.ComponentProps<typeof DiscretionaryPerformance>> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  lastClient = client
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

/** Advanced analysis is collapsed by default: open it before looking inside. */
async function openAdvanced() {
  const toggle = await screen.findByTestId('advanced-analysis-toggle')
  if (toggle.getAttribute('aria-expanded') !== 'true') await userEvent.click(toggle)
}

/** Opens the fills dialog of a trade: expand its row, then press its lifecycle button. */
async function openFills(symbol: string) {
  const expander = await screen.findByRole('button', {
    name: new RegExp(`details of trade \\d+, ${symbol}`),
  })
  if (expander.getAttribute('aria-expanded') !== 'true') await userEvent.click(expander)
  await userEvent.click(
    await screen.findByRole('button', {
      name: new RegExp(`Show fills and lifecycle of trade \\d+, ${symbol}`),
    })
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  summaryFn.mockResolvedValue(summary())
  excursionsFn.mockResolvedValue(excursions())
  unresolvedFn.mockResolvedValue({
    unresolved: [],
    unresolved_count: 0,
    orphans: [],
    orphans_count: 0,
  })
  fillsFn.mockResolvedValue({ build_id: 'b1', trade: trade(), fills: [], linked_fills: 0 })
})

describe('empty and error states', () => {
  it('says there are no discretionary trades yet and hints at the ledger sync', async () => {
    summaryFn.mockResolvedValue(emptySummary())
    renderIt()
    expect(await screen.findByText('No discretionary trades yet')).toBeInTheDocument()
    expect(screen.getByText(/ledger sync has to be running/i)).toBeInTheDocument()
    expect(screen.queryByText('Net realized (after est. charges)')).not.toBeInTheDocument()
  })

  it('says every execution awaits attribution, with the counts, and offers the details in the warning', async () => {
    summaryFn.mockResolvedValue({
      ...emptySummary(),
      executions_collected: 5,
      executions_unresolved: 5,
      unresolved_in_range: unresolvedInRange({ unresolved_fills: 5, orphan_fills: 0, total: 5 }),
      warnings: ['5 unresolved fill(s) are excluded from P&L until attributed.'],
    })
    unresolvedFn.mockResolvedValue({
      unresolved: [
        {
          id: 1,
          broker: 'flattrade',
          symbol: 'TCS',
          product: 'MIS',
          side: 'BUY',
          qty: 4,
          price: 2392.6,
          fill_time: '2026-11-18 09:46:18',
          order_id: 'O-1',
          attribution_state: 'unresolved',
        },
      ],
      unresolved_count: 5,
      orphans: [],
      orphans_count: 0,
    })
    renderIt()
    const block = await screen.findByTestId('discretionary-awaiting-attribution')
    expect(
      within(block).getByText(
        'No attributable trades available for performance analysis yet. 5 fills collected; 5 awaiting attribution. Their P&L is excluded until classification is resolved.'
      )
    ).toBeInTheDocument()
    expect(within(block).getByText(/Use View details in the warning above/)).toBeInTheDocument()
    expect(within(block).queryByText(/Unresolved fills below/)).not.toBeInTheDocument()
    expect(screen.queryByText('No discretionary trades yet')).not.toBeInTheDocument()
    expect(screen.queryByText(/ledger sync has to be running/i)).not.toBeInTheDocument()
    expect(screen.queryByText('Net realized (after est. charges)')).not.toBeInTheDocument()
    // the standalone section is gone; the one-line warning opens the list on demand
    expect(screen.queryByTestId('discretionary-unresolved')).not.toBeInTheDocument()
    expect(unresolvedFn).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'View details' }))
    expect(await screen.findByText('TCS')).toBeInTheDocument()
    expect(screen.getByText(/excluded from completed-trade statistics until/)).toBeInTheDocument()
    // the range banner carries the count; the ledger-wide backend line is not repeated
    expect(screen.getByTestId('unresolved-warning')).toHaveTextContent(
      '5 fills in the selected range'
    )
    expect(screen.queryByTestId('discretionary-warnings')).not.toBeInTheDocument()
  })

  it('fills in N from the counts', async () => {
    summaryFn.mockResolvedValue({
      ...emptySummary(),
      executions_collected: 7,
      executions_unresolved: 2,
    })
    renderIt()
    expect(
      await screen.findByText(
        'No attributable trades available for performance analysis yet. 7 fills collected; 2 awaiting attribution. Their P&L is excluded until classification is resolved.'
      )
    ).toBeInTheDocument()
  })

  it('keeps the generic message when the period holds no execution', async () => {
    summaryFn.mockResolvedValue({
      ...emptySummary(),
      executions_collected: 0,
      executions_unresolved: 0,
    })
    renderIt()
    expect(await screen.findByText('No discretionary trades yet')).toBeInTheDocument()
    expect(screen.getByText(/ledger sync has to be running/i)).toBeInTheDocument()
    expect(screen.queryByTestId('discretionary-awaiting-attribution')).not.toBeInTheDocument()
    // the unresolved list stays collapsed
    expect(screen.queryByText('No unresolved fills.')).not.toBeInTheDocument()
  })

  it('shows no empty state when trades exist, even with fills awaiting attribution', async () => {
    summaryFn.mockResolvedValue(summary({ executions_collected: 9, executions_unresolved: 3 }))
    renderIt()
    expect(await screen.findByText('Closed trades')).toBeInTheDocument()
    expect(screen.queryByTestId('discretionary-awaiting-attribution')).not.toBeInTheDocument()
    expect(screen.queryByText('No discretionary trades yet')).not.toBeInTheDocument()
    // no unresolved_in_range count (or 0): no warning and no standalone section
    expect(screen.queryByTestId('unresolved-warning')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Unresolved fills' })).not.toBeInTheDocument()
  })

  it('says the filters matched nothing when a filter is set', async () => {
    renderIt()
    await screen.findByText('Closed trades')
    summaryFn.mockResolvedValue(emptySummary())
    await userEvent.type(screen.getByLabelText('Symbol'), 'ZZZ{Enter}')
    expect(
      await screen.findByText('No discretionary trades match these filters')
    ).toBeInTheDocument()
  })

  it('shows the server message when the feature is off', async () => {
    summaryFn.mockRejectedValue(new DiscretionaryApiError('off', 'feature_disabled', 404))
    renderIt()
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The Discretionary source is turned off on the server.'
    )
  })

  it('shows a bad-request message as it is', async () => {
    summaryFn.mockRejectedValue(new DiscretionaryApiError('bad period', undefined, 400))
    renderIt()
    expect(await screen.findByRole('alert')).toHaveTextContent('bad period')
  })

  it('asks nothing until the custom range is complete', async () => {
    renderIt({ rangeReady: false })
    expect(await screen.findByText(/Choose a start and end date/)).toBeInTheDocument()
    expect(summaryFn).not.toHaveBeenCalled()
  })
})

describe('warnings', () => {
  it('lists actionable backend warnings and a sync problem, and hides informational lines', async () => {
    summaryFn.mockResolvedValue(
      summary({
        sync_health: {
          last_successful_sync: '2026-11-15 09:30',
          runs_by_day: {},
          days_without_sync: ['2026-11-16'],
        },
        warnings: [
          '2 unresolved fill(s) are excluded from P&L until attributed.',
          'last successful sync: 2026-11-15 09:30 IST',
          'unrealized P&L not requested.',
        ],
      })
    )
    renderIt()
    const banner = await screen.findByTestId('discretionary-warnings')
    expect(within(banner).getByText(/2 unresolved fill\(s\) are excluded/)).toBeInTheDocument()
    expect(within(banner).getByText(/no sync on 2026-11-16/)).toBeInTheDocument()
    expect(banner).not.toHaveTextContent('unrealized P&L not requested')
  })

  it('shows no banner for a healthy ledger, only the sync line', async () => {
    summaryFn.mockResolvedValue(
      summary({ warnings: ['last successful sync: 2026-11-17 09:30 IST'] })
    )
    renderIt()
    expect(await screen.findByTestId('discretionary-sync-line')).toHaveTextContent(
      'Ledger sync: last successful 2026-11-17 09:30 IST'
    )
    expect(screen.queryByTestId('discretionary-warnings')).not.toBeInTheDocument()
  })
})

describe('populated view', () => {
  it('shows net after charges, gross, unrealized not requested, and the total without it', async () => {
    renderIt()
    await screen.findByText('Net realized (after est. charges)')
    expect(cardValue('Net realized (after est. charges)')).toBe('Rs 47.50')
    expect(cardValue('Gross realized')).toBe('Rs 60.00')
    expect(cardValue('Total')).toBe('Rs 47.50')
    expect(cardValue('Unrealized')).toBe('—')
    expect(screen.getByTestId('unrealized-price-badge')).toHaveTextContent('Not requested')
    expect(
      screen.getByText(
        'net realized only: live prices not requested; net incomplete: 1 trade(s) without a charge estimate'
      )
    ).toBeInTheDocument()
    expect(screen.queryByText(/unrealized not included/)).not.toBeInTheDocument()
    expect(screen.getAllByText(/1 trade\(s\) without a charge estimate/).length).toBeGreaterThan(0)
  })

  it('shows no Sharpe, Sortino or Return % card and no note about them', async () => {
    renderIt()
    await screen.findByText('Net realized (after est. charges)')
    for (const name of ['Sharpe', 'Sortino', 'Return %']) {
      expect(screen.queryByText(name)).not.toBeInTheDocument()
    }
    expect(screen.queryByText('--')).not.toBeInTheDocument()
    expect(screen.queryByText(/no capital basis/)).not.toBeInTheDocument()
    expect(screen.queryByText(/realized_gross|net_of_charges/)).not.toBeInTheDocument()
    expect(screen.getByText(/^Net = gross realized minus estimated charges/)).toBeInTheDocument()
  })

  it('labels the drawdown basis and the equity curve caption', async () => {
    renderIt()
    await screen.findByText('Max drawdown')
    expect(
      screen.getByText('basis: gross realized, excludes charges and unrealized')
    ).toBeInTheDocument()
    // the curve is the NET realised equity built from the trades: the one trade without a net
    // (no charge estimate) is skipped and said so
    expect(screen.getByTestId('equity-chart')).toHaveTextContent('1 points')
    const caption = screen.getByTestId('net-equity-caption')
    expect(caption).toHaveTextContent('Net realised P&L after estimated charges')
    expect(caption).toHaveTextContent('Based on 1 completed trade(s)')
    expect(caption).toHaveTextContent('1 without a net P&L (no charge estimate) are left out')
  })

  it('shows win rate as a percent and the profit factor', async () => {
    renderIt()
    await screen.findByText('Win rate (net)', { selector: 'div' })
    expect(cardValue('Win rate (net)')).toBe('50.0%')
    expect(cardValue('Profit factor (net)')).toBe('2.50')
  })

  it('switches the period table between daily, weekly and monthly', async () => {
    // 10 close dates over 2 months: the medium profile offers all three tabs
    summaryFn.mockResolvedValue(withDays(10, { monthly: monthRows() }))
    renderIt()
    await openAdvanced()
    await screen.findByRole('tab', { name: 'Daily' })
    // every one of the 10 fixture days is flagged incomplete
    expect(screen.getAllByText('incomplete')).toHaveLength(10)
    await userEvent.click(screen.getByRole('tab', { name: 'Weekly' }))
    expect(await screen.findByText('2026-W47')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('tab', { name: 'Monthly' }))
    expect(await screen.findAllByText('2026-11')).not.toHaveLength(0)
  })

  it('shows the breakdown tables and the holding durations', async () => {
    renderIt()
    await openAdvanced()
    await screen.findByText('By symbol')
    expect(screen.getByText('Long vs Short')).toBeInTheDocument()
    expect(screen.getByText('By product')).toBeInTheDocument()
    expect(screen.getByText('Intraday vs overnight')).toBeInTheDocument()
    expect(screen.getAllByText('Long').length).toBeGreaterThan(0)
    expect(screen.getByText('Overnight')).toBeInTheDocument()
    const hold = screen.getByTestId('hold-durations')
    expect(within(hold).getByText('Winners')).toBeInTheDocument()
    expect(within(hold).getByText('30m')).toBeInTheDocument()
  })

  it('marks closed trades as estimated or charges unavailable', async () => {
    renderIt()
    const table = (await screen.findByText('Closed trades')).closest(
      '[data-slot="card"]'
    ) as HTMLElement
    // the row keeps the warning visible; the plain 'estimated' note is in the detail row
    expect(within(table).getByText('charges unavailable')).toBeInTheDocument()
    expect(within(table).queryByText('estimated')).not.toBeInTheDocument()
    expect(within(table).getByText('Rs 12.50')).toBeInTheDocument()
    expect(within(table).getByText('Rs 87.50')).toBeInTheDocument()
    await userEvent.click(
      within(table).getByRole('button', { name: /details of trade \d+, RELIANCE/ })
    )
    expect(within(table).getByText('estimated')).toBeInTheDocument()
  })

  it('notes a truncated trade list', async () => {
    summaryFn.mockResolvedValue(
      summary({ trades_total: 900, trades_shown: 2, trades_truncated: 898 })
    )
    renderIt()
    expect(await screen.findByTestId('trades-truncated')).toHaveTextContent(
      'Showing 2 of 900 trades'
    )
  })
})

describe('open positions and price states', () => {
  async function positionsCard() {
    const heading = await screen.findByText('Open positions')
    return heading.closest('[data-slot="card"]') as HTMLElement
  }

  it('shows an open position with status Open and no price when not requested', async () => {
    renderIt()
    const card = await positionsCard()
    expect(within(card).getByText('Open')).toBeInTheDocument()
    expect(within(card).getByText('Not requested')).toBeInTheDocument()
    expect(within(card).getByText('TCS')).toBeInTheDocument()
  })

  it('shows live, stale, cached and unavailable prices with their age', async () => {
    summaryFn.mockResolvedValue(
      summary({
        quotes_requested: true,
        open_positions: 4,
        unrealized_total: 300,
        positions: [
          position({ symbol: 'A', price_status: 'live', age_s: 5, ltp: 10, unrealized: 100 }),
          position({ symbol: 'B', price_status: 'stale', age_s: 600, ltp: 10, unrealized: 100 }),
          position({ symbol: 'C', price_status: 'cached', age_s: 125, ltp: 10, unrealized: 100 }),
          position({ symbol: 'D', price_status: 'unavailable' }),
        ],
        positions_total: 4,
      })
    )
    renderIt()
    const card = await positionsCard()
    expect(within(card).getByText('Live 5s')).toBeInTheDocument()
    expect(within(card).getByText('Stale 10m')).toBeInTheDocument()
    expect(within(card).getByText('Cached 2m')).toBeInTheDocument()
    expect(within(card).getByText('No price')).toBeInTheDocument()
    // The Unrealized card shows the worst price state and flags the unpriced position.
    expect(screen.getByTestId('unrealized-price-badge')).toHaveTextContent('No price')
    expect(screen.getByText('1 position(s) unpriced or not listed')).toBeInTheDocument()
    // a non-zero Unrealized card never sits beside a Total that says it is not included
    expect(screen.queryByText(/unrealized not included/)).not.toBeInTheDocument()
    expect(cardValue('Net realized + unrealized (partial: 1 positions unpriced)')).toBe('Rs 347.50')
  })

  it('adds a fully priced unrealized figure into the total', async () => {
    summaryFn.mockResolvedValue(
      summary({
        quotes_requested: true,
        unrealized_total: 250,
        positions: [position({ price_status: 'live', age_s: 2, ltp: 3050, unrealized: 250 })],
      })
    )
    renderIt()
    await screen.findByText(/^net realized \+ unrealized; net incomplete/)
    expect(cardValue('Total')).toBe('Rs 297.50')
  })

  it('flags an opening position that is unknown', async () => {
    summaryFn.mockResolvedValue(summary({ positions: [position({ opening_unknown: true })] }))
    renderIt()
    expect(await screen.findByText('opening position unknown')).toBeInTheDocument()
  })

  it('asks for quotes only after Fetch live prices is switched on', async () => {
    renderIt()
    await screen.findByText('Closed trades')
    expect(summaryFn).toHaveBeenLastCalledWith('30d', undefined, {}, false, 'all')
    await userEvent.click(screen.getByRole('switch', { name: 'Fetch live prices' }))
    await waitFor(() =>
      expect(summaryFn).toHaveBeenLastCalledWith('30d', undefined, {}, true, 'all')
    )
  })
})

describe('filters', () => {
  it('sends the symbol filter on Enter', async () => {
    renderIt()
    await screen.findByText('Closed trades')
    await userEvent.type(screen.getByLabelText('Symbol'), 'TCS{Enter}')
    await waitFor(() =>
      expect(summaryFn).toHaveBeenLastCalledWith(
        '30d',
        undefined,
        { broker: undefined, account: undefined, symbol: 'TCS' },
        false,
        'all'
      )
    )
    // the same filters reach the lazy MAE / MFE call
    await waitFor(() =>
      expect(excursionsFn).toHaveBeenLastCalledWith(
        '30d',
        undefined,
        { broker: undefined, account: undefined, symbol: 'TCS' },
        'all'
      )
    )
  })

  it('offers the brokers found in the data and filters on a pick', async () => {
    renderIt()
    await screen.findByText('Closed trades')
    await userEvent.click(screen.getByRole('combobox', { name: 'Broker' }))
    expect(await screen.findByRole('option', { name: 'zerodha' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('option', { name: 'zerodha' }))
    await waitFor(() =>
      expect(summaryFn).toHaveBeenLastCalledWith(
        '30d',
        undefined,
        { broker: 'zerodha', account: undefined, symbol: undefined },
        false,
        'all'
      )
    )
  })

  it('clears the filters', async () => {
    renderIt()
    await screen.findByText('Closed trades')
    await userEvent.type(screen.getByLabelText('Symbol'), 'TCS{Enter}')
    await userEvent.click(await screen.findByRole('button', { name: 'Clear filters' }))
    await waitFor(() =>
      expect(summaryFn).toHaveBeenLastCalledWith('30d', undefined, {}, false, 'all')
    )
    expect(screen.getByLabelText('Symbol')).toHaveValue('')
  })

  it('passes a custom range through', async () => {
    renderIt({ period: 'custom', range: { start: '2026-11-01', end: '2026-11-10' } })
    await screen.findByText('Closed trades')
    expect(summaryFn).toHaveBeenLastCalledWith(
      'custom',
      { start: '2026-11-01', end: '2026-11-10' },
      {},
      false,
      'all'
    )
  })
})

describe('unresolved fills warning', () => {
  const fill = {
    id: 5,
    broker: 'flattrade',
    symbol: 'WIPRO',
    product: 'MIS',
    side: 'BUY',
    qty: 3,
    price: 250,
    fill_time: '2026-11-16 10:00:00',
    order_id: 'O-9',
    attribution_state: 'unresolved',
  }

  it('shows a one-line warning with the count only when fills in the range are unattributed', async () => {
    summaryFn.mockResolvedValue(summary({ unresolved_in_range: unresolvedInRange() }))
    renderIt()
    const banner = await screen.findByTestId('unresolved-warning')
    expect(banner).toHaveTextContent(
      '3 fills in the selected range could not be attributed and are excluded from completed-trade statistics.'
    )
    // nothing is read until the details are asked for
    expect(unresolvedFn).not.toHaveBeenCalled()
  })

  it('shows no warning when the count is 0 or the server does not send it', async () => {
    summaryFn.mockResolvedValue(
      summary({
        unresolved_in_range: unresolvedInRange({ unresolved_fills: 0, orphan_fills: 0, total: 0 }),
      })
    )
    renderIt()
    await screen.findByText('Closed trades')
    expect(screen.queryByTestId('unresolved-warning')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'View details' })).not.toBeInTheDocument()
  })

  it('removes the old standalone Unresolved fills section', async () => {
    summaryFn.mockResolvedValue(summary({ unresolved_in_range: unresolvedInRange() }))
    renderIt()
    await screen.findByTestId('unresolved-warning')
    expect(screen.queryByTestId('discretionary-unresolved')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Unresolved fills' })).not.toBeInTheDocument()
  })

  it('opens the existing details with the unresolved and the unplaced fills', async () => {
    unresolvedFn.mockResolvedValue({
      unresolved: [fill],
      unresolved_count: 1,
      orphans: [{ id: 1, symbol: 'ORPH', reason: 'opening_not_in_ledger', qty: 1 }],
      orphans_count: 1,
    })
    summaryFn.mockResolvedValue(summary({ unresolved_in_range: unresolvedInRange() }))
    renderIt()
    await userEvent.click(await screen.findByRole('button', { name: 'View details' }))
    const dialog = await screen.findByTestId('unresolved-dialog')
    expect(await within(dialog).findByText('WIPRO')).toBeInTheDocument()
    expect(within(dialog).getByText('ORPH')).toBeInTheDocument()
    expect(within(dialog).queryByText('opening_not_in_ledger')).not.toBeInTheDocument()
    expect(
      within(dialog).getAllByText('The fill that opened this position is not in the ledger').length
    ).toBeGreaterThan(0)
    expect(
      within(dialog).getByText(
        'Not yet attributed to a strategy or to you, so it is left out of P&L'
      )
    ).toBeInTheDocument()
  })

  it('says so when the list is empty', async () => {
    summaryFn.mockResolvedValue(summary({ unresolved_in_range: unresolvedInRange() }))
    renderIt()
    await userEvent.click(await screen.findByRole('button', { name: 'View details' }))
    expect(await screen.findByText('No unresolved fills.')).toBeInTheDocument()
  })

  it('shows an error with a retry when the list cannot be read', async () => {
    unresolvedFn.mockRejectedValueOnce(
      new DiscretionaryApiError('Could not read the fills.', undefined, 500)
    )
    summaryFn.mockResolvedValue(summary({ unresolved_in_range: unresolvedInRange() }))
    renderIt()
    await userEvent.click(await screen.findByRole('button', { name: 'View details' }))
    const alert = await screen.findByText('Could not read the fills.')
    expect(alert.closest('[data-slot="alert"]')).toHaveClass('text-destructive')
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('No unresolved fills.')).toBeInTheDocument()
  })
})

describe('trade fills dialog', () => {
  it('opens the fills of the clicked trade', async () => {
    fillsFn.mockResolvedValue({
      build_id: 'b1',
      trade: trade(),
      linked_fills: 2,
      fills: [
        {
          fill_ref: 1,
          role: 'ENTRY',
          qty_used: 10,
          charge_allocated: 6.25,
          price: 100,
          fill_time: '2026-11-16 09:20:00',
          order_id: 'ORD-ENTRY',
          fill_id: 'f1',
          broker: 'flattrade',
          account: 'FT1',
          symbol: 'RELIANCE',
          exchange: 'NSE',
          product: 'MIS',
          side: 'BUY',
          attribution_state: 'manual',
        },
        {
          fill_ref: 2,
          role: 'EXIT',
          qty_used: 10,
          charge_allocated: 6.25,
          price: 110,
          fill_time: '2026-11-16 10:05:00',
          order_id: 'ORD-EXIT',
          fill_id: 'f2',
          broker: 'flattrade',
          account: 'FT1',
          symbol: 'RELIANCE',
          exchange: 'NSE',
          product: 'MIS',
          side: 'SELL',
          attribution_state: 'manual',
        },
      ],
    })
    renderIt()
    await openFills('RELIANCE')
    const dialog = await screen.findByRole('dialog')
    expect(await within(dialog).findByText('ORD-ENTRY')).toBeInTheDocument()
    expect(within(dialog).getByText('ORD-EXIT')).toBeInTheDocument()
    expect(
      within(dialog)
        .getAllByTestId('lifecycle-label')
        .map((e) => e.textContent)
    ).toEqual(['Entry', 'Final exit'])
    expect(
      within(dialog)
        .getAllByTestId('lifecycle-net')
        .map((e) => e.textContent)
    ).toEqual(['+10', '0'])
    expect(
      within(dialog).getByText(/Every fill that opened, added to, reduced or closed this trade/)
    ).toBeInTheDocument()
    expect(within(dialog).queryByText(/legs/)).not.toBeInTheDocument()
    expect(fillsFn).toHaveBeenCalledWith(11, 'b1')
  })

  it('groups the partial fills of one order and nests them under it', async () => {
    const f = (
      fill_ref: number,
      role: 'ENTRY' | 'EXIT',
      side: string,
      qty: number,
      price: number,
      time: string,
      order: string
    ) => ({
      fill_ref,
      role,
      qty_used: qty,
      charge_allocated: 0.25,
      price,
      fill_time: `2026-11-16 ${time}`,
      order_id: order,
      fill_id: `f${fill_ref}`,
      broker: 'flattrade',
      account: 'FT1',
      symbol: 'RELIANCE',
      exchange: 'NSE',
      product: 'MIS',
      side,
      attribution_state: 'manual',
    })
    fillsFn.mockResolvedValue({
      build_id: 'b1',
      trade: trade({ closed_quantity: 25 }),
      linked_fills: 3,
      // stored out of order: the dialog sorts them
      fills: [
        f(3, 'EXIT', 'SELL', 25, 2390, '10:30:00', 'ORD-X'),
        f(2, 'ENTRY', 'BUY', 21, 2385, '10:08:46', 'ORD-92152'),
        f(1, 'ENTRY', 'BUY', 4, 2385, '10:08:45', 'ORD-92152'),
      ],
    })
    renderIt()
    await openFills('RELIANCE')
    const dialog = await screen.findByRole('dialog')
    const events = await within(dialog).findAllByTestId('lifecycle-event')
    expect(events).toHaveLength(2)
    expect(events[0]).toHaveTextContent('ORD-92152')
    expect(events[0]).toHaveTextContent('25')
    expect(events[0]).toHaveTextContent('2385.00')
    expect(events[0]).toHaveTextContent('+25')
    expect(events[1]).toHaveTextContent('Final exit')
    // the individual fills are nested beneath the order, behind an accessible button
    expect(within(dialog).queryAllByTestId('lifecycle-fill')).toHaveLength(0)
    const toggle = within(events[0]).getByRole('button', { name: /Show the 2 fills of this order/ })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await userEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    const nested = within(dialog).getAllByTestId('lifecycle-fill')
    expect(nested.map((r) => r.textContent)).toEqual([
      expect.stringContaining('4'),
      expect.stringContaining('21'),
    ])
    expect(nested[0]).toHaveTextContent('f1')
    expect(nested[1]).toHaveTextContent('f2')
  })

  it('shows the error when the fills cannot be read', async () => {
    fillsFn.mockRejectedValue(new DiscretionaryApiError('Trade not found.', 'trade_not_found', 404))
    renderIt()
    await openFills('RELIANCE')
    const dialog = await screen.findByRole('dialog')
    expect(await within(dialog).findByText('Trade not found.')).toBeInTheDocument()
  })

  it('shows a 500 as a destructive alert with a retry that asks again', async () => {
    fillsFn.mockRejectedValueOnce(
      new DiscretionaryApiError('Could not read the trade fills.', undefined, 500)
    )
    renderIt()
    await openFills('RELIANCE')
    const dialog = await screen.findByRole('dialog')
    expect(await within(dialog).findByText('Could not read the trade fills.')).toBeInTheDocument()
    expect(dialog.querySelector('[data-slot="alert"]')).toHaveClass('text-destructive')
    expect(fillsFn).toHaveBeenCalledTimes(1)
    await userEvent.click(within(dialog).getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(fillsFn).toHaveBeenCalledTimes(2))
    expect(await within(dialog).findByText(/Held/)).toBeInTheDocument()
  })

  it('asks with the build of the summary the row came from, and says so when the data changed', async () => {
    fillsFn.mockRejectedValue(new DiscretionaryApiError('The data changed', 'build_changed', 409))
    renderIt()
    await openFills('RELIANCE')
    const dialog = await screen.findByRole('dialog')
    expect(fillsFn).toHaveBeenCalledWith(11, 'b1')
    expect(
      await within(dialog).findByText(/data changed since this page loaded/i)
    ).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    summaryFn.mockClear()
    await userEvent.click(within(dialog).getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await waitFor(() => expect(summaryFn).toHaveBeenCalled())
  })

  it('does not reuse a cached answer for the same trade id under a new build', async () => {
    renderIt()
    await openFills('RELIANCE')
    await screen.findByRole('dialog')
    await waitFor(() => expect(fillsFn).toHaveBeenCalledTimes(1))
    await userEvent.keyboard('{Escape}')
    summaryFn.mockResolvedValue(summary({ build_id: 'b2' }))
    await userEvent.click(screen.getByRole('switch', { name: 'Fetch live prices' }))
    await waitFor(() => expect(screen.queryByText(/updating/)).not.toBeInTheDocument())
    await openFills('RELIANCE')
    await waitFor(() => expect(fillsFn).toHaveBeenLastCalledWith(11, 'b2'))
    expect(fillsFn).toHaveBeenCalledTimes(2)
  })
})

describe('data quality at the bottom of the page', () => {
  const clear = {
    collected: 26,
    awaiting_attribution: 0,
    awaiting_grace: 0,
    awaiting_other: 0,
    attributed_open: 0,
    not_placed: 0,
    included: 26,
    reconciles: true,
    charges_incomplete_trades: 0,
    charges_incomplete_legs: 0,
  }

  it('shows one line when everything reconciles, below the closed trades and insights', async () => {
    summaryFn.mockResolvedValue(summary({ data_quality: dataQuality(clear) }))
    renderIt()
    const line = await screen.findByTestId('dq-all-clear')
    expect(line).toHaveTextContent(`26 fills, all in ${summary().trades_total} completed trades`)
    expect(screen.queryByTestId('dq-awaiting')).not.toBeInTheDocument()
    expect(screen.queryByTestId('dq-open')).not.toBeInTheDocument()
    expect(screen.queryByTestId('dq-not_placed')).not.toBeInTheDocument()
    expect(screen.queryByTestId('dq-sync')).not.toBeInTheDocument()
    const dq = screen.getByTestId('data-quality')
    for (const id of ['ledger-card', 'trading-insights', 'advanced-analysis']) {
      expect(
        screen.getByTestId(id).compareDocumentPosition(dq) & Node.DOCUMENT_POSITION_FOLLOWING
      ).toBeTruthy()
    }
  })

  it('keeps fills that await attribution, and the non-zero counts, fully visible', async () => {
    summaryFn.mockResolvedValue(
      summary({
        data_quality: dataQuality({
          ...clear,
          collected: 28,
          awaiting_attribution: 2,
          awaiting_grace: 1,
          awaiting_other: 1,
          reconciles: false,
        }),
      })
    )
    renderIt()
    const awaiting = await screen.findByTestId('dq-awaiting')
    expect(awaiting).toHaveTextContent('2')
    expect(awaiting).toHaveTextContent('1 in the safety window, 1 other')
    expect(screen.getByTestId('dq-collected')).toHaveTextContent('28')
    expect(screen.getByText('counts do not add up')).toBeInTheDocument()
    expect(screen.queryByTestId('dq-all-clear')).not.toBeInTheDocument()
    // zero tiles stay hidden
    expect(screen.queryByTestId('dq-open')).not.toBeInTheDocument()
    expect(screen.queryByTestId('dq-not_placed')).not.toBeInTheDocument()
  })
})

describe('error and stale-data states', () => {
  it('shows a failed load as a destructive alert with a Retry that refetches', async () => {
    summaryFn.mockRejectedValueOnce(new DiscretionaryApiError('boom', undefined, 500))
    renderIt()
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveClass('text-destructive')
    expect(alert).toHaveTextContent('boom')
    await userEvent.click(within(alert).getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('Closed trades')).toBeInTheDocument()
    expect(summaryFn).toHaveBeenCalledTimes(2)
  })

  it('keeps the earlier data and says the last refresh failed when a refetch fails', async () => {
    renderIt()
    await screen.findByText('Closed trades')
    expect(screen.queryByTestId('discretionary-refresh-failed')).not.toBeInTheDocument()
    summaryFn.mockRejectedValue(
      new DiscretionaryApiError('Could not build the discretionary summary.', undefined, 500)
    )
    // what an order event or the poll does: refetch the summary already on screen
    await act(() => lastClient.invalidateQueries({ queryKey: ['discretionary-pnl', 'summary'] }))
    const notice = await screen.findByTestId('discretionary-refresh-failed')
    expect(notice).toHaveTextContent('Last refresh failed, showing earlier data')
    expect(screen.getByText('Closed trades')).toBeInTheDocument()
    expect(screen.getAllByText('RELIANCE').length).toBeGreaterThan(0)
    summaryFn.mockResolvedValue(summary())
    await userEvent.click(within(notice).getByRole('button', { name: 'Retry' }))
    await waitFor(() =>
      expect(screen.queryByTestId('discretionary-refresh-failed')).not.toBeInTheDocument()
    )
  })
})

describe('incomplete net in the tables', () => {
  it('marks Net of charges incomplete in a breakdown row with unavailable charges', async () => {
    summaryFn.mockResolvedValue(
      summary({
        by_symbol: [
          breakdownRow('INFY', { charges_unavailable_trades: 1, net_charges_complete: false }),
          breakdownRow('TCS'),
        ],
        by_side: [breakdownRow('LONG', { charges_unavailable_trades: 2 })],
        by_product: [breakdownRow('MIS')],
      })
    )
    renderIt()
    await openAdvanced()
    const bySymbol = (await screen.findByText('By symbol')).parentElement as HTMLElement
    expect(within(bySymbol).getAllByText('incomplete')).toHaveLength(1)
    const bySide = screen.getByText('Long vs Short').parentElement as HTMLElement
    expect(within(bySide).getAllByText('incomplete')).toHaveLength(1)
    const byProduct = screen.getByText('By product').parentElement as HTMLElement
    expect(within(byProduct).queryByText('incomplete')).not.toBeInTheDocument()
  })

  it('flags the Total as incomplete and as partial when positions are unpriced', async () => {
    summaryFn.mockResolvedValue(
      summary({
        quotes_requested: true,
        open_positions: 1,
        unrealized_total: 0,
        positions: [position({ price_status: 'unavailable' })],
      })
    )
    renderIt()
    await screen.findByText('Net realized + unrealized (partial: 1 positions unpriced)')
    expect(
      screen.getByText('partial total; net incomplete: 1 trade(s) without a charge estimate')
    ).toBeInTheDocument()
  })
})

describe('labels and layout', () => {
  it('labels the win / loss figures as net of charges, as the server computes them', async () => {
    renderIt()
    await screen.findByText('Closed trades')
    await openAdvanced()
    for (const label of [
      'Win rate (net)',
      'Profit factor (net)',
      'Avg win (net)',
      'Avg loss (net)',
      'Expectancy (net)',
      'Total won (net)',
      'Total lost (net)',
    ]) {
      expect(screen.getByText(label, { selector: 'div' })).toBeInTheDocument()
    }
    // the old gross labels would misstate the basis
    for (const label of ['Avg win (gross)', 'Avg loss (gross)', 'Gross profit', 'Gross loss']) {
      expect(screen.queryByText(label)).not.toBeInTheDocument()
    }
    expect(screen.getByText('Gross realized', { selector: 'div' })).toBeInTheDocument()
  })

  it('shows the account in the closed trade detail row and an Account column in open positions', async () => {
    renderIt()
    const closed = (await screen.findByText('Closed trades')).closest(
      '[data-slot="card"]'
    ) as HTMLElement
    expect(within(closed).queryByRole('columnheader', { name: 'Account' })).not.toBeInTheDocument()
    expect(within(closed).queryByText(trade().account)).not.toBeInTheDocument()
    await userEvent.click(
      within(closed).getByRole('button', { name: /details of trade \d+, RELIANCE/ })
    )
    const detail = within(closed).getByTestId('closed-trade-detail')
    expect(within(detail).getByText('Account')).toBeInTheDocument()
    expect(within(detail).getByText(trade().account)).toBeInTheDocument()
    const open = screen.getByText('Open positions').closest('[data-slot="card"]') as HTMLElement
    expect(within(open).getByRole('columnheader', { name: 'Account' })).toBeInTheDocument()
    expect(within(open).getByText(position().account)).toBeInTheDocument()
  })

  it('keeps every breakdown column reachable: one column layout, scrollable tables', async () => {
    renderIt()
    await openAdvanced()
    const bySymbol = (await screen.findByText('By symbol')).parentElement as HTMLElement
    for (const name of ['Gross P&L', 'Net of charges', 'Trades']) {
      expect(within(bySymbol).getByRole('columnheader', { name })).toBeInTheDocument()
    }
    expect(bySymbol).toHaveClass('min-w-0', 'overflow-x-auto')
    expect(bySymbol.parentElement).toHaveClass('grid-cols-1')
    expect(bySymbol.parentElement?.className).not.toMatch(/(^|\s)lg:grid-cols-2/)
  })

  it('lets a keyboard or screen reader user open a trade: a real button expands the detail row', async () => {
    renderIt()
    const expander = await screen.findByRole('button', { name: /details of trade 1, RELIANCE/ })
    // a real button in the first cell; the row itself is not a button
    expect(expander.tagName).toBe('BUTTON')
    expect(expander).toHaveAttribute('aria-expanded', 'false')
    expect(expander.closest('tr')).not.toHaveAttribute('role', 'button')
    expect(expander.closest('tr')).not.toHaveAttribute('tabindex')
    expect(expander.closest('td')).toBe(expander.closest('tr')?.firstElementChild)
    expect(screen.queryByTestId('closed-trade-detail')).not.toBeInTheDocument()
    expander.focus()
    await userEvent.keyboard('{Enter}')
    expect(expander).toHaveAttribute('aria-expanded', 'true')
    const detail = screen.getByTestId('closed-trade-detail')
    expect(expander.getAttribute('aria-controls')).toBe(detail.id)
    await userEvent.keyboard(' ')
    expect(expander).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTestId('closed-trade-detail')).not.toBeInTheDocument()
    await userEvent.keyboard('{Enter}')
    await userEvent.click(
      within(screen.getByTestId('closed-trade-detail')).getByRole('button', {
        name: /Show fills and lifecycle of trade 1, RELIANCE/,
      })
    )
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
  })
})

describe('completed trades (a position from flat to flat)', () => {
  it('shows total bought and total sold instead of a quantity and a legs badge', async () => {
    summaryFn.mockResolvedValue(
      summary({
        trades: [
          trade({ legs: 2, closed_quantity: 45 }),
          trade({ id: 12, symbol: 'INFY', closed_quantity: 7 }),
        ],
      })
    )
    renderIt()
    const table = (await screen.findByText('Closed trades')).closest(
      '[data-slot="card"]'
    ) as HTMLElement
    expect(within(table).getByRole('columnheader', { name: 'Bought / Sold' })).toBeInTheDocument()
    expect(within(table).queryByRole('columnheader', { name: 'Qty' })).not.toBeInTheDocument()
    const cells = within(table).getAllByTestId('bought-sold')
    expect(cells.map((c) => c.textContent)).toEqual(['45 / 45', '7 / 7'])
    expect(cells[0]).toHaveAttribute('title', 'Bought 45, sold 45')
    // no legs badge, with or without partial exits
    expect(within(table).queryByText(/\blegs?\b/)).not.toBeInTheDocument()
  })

  it('shows eleven visible columns and numbers the trades by opening time', async () => {
    summaryFn.mockResolvedValue(
      summary({
        trades: [
          trade({
            id: 12,
            symbol: 'INFY',
            opened_at: '2026-11-16 11:00:00',
            closed_at: '2026-11-16 11:30:00',
          }),
          trade({ id: 11, opened_at: '2026-11-16 09:20:00', closed_at: '2026-11-16 10:05:00' }),
        ],
      })
    )
    renderIt()
    const table = (await screen.findByText('Closed trades')).closest(
      '[data-slot="card"]'
    ) as HTMLElement
    expect(
      within(table)
        .getAllByRole('columnheader')
        .map((h) => h.textContent)
    ).toEqual([
      'Trade #',
      'Symbol',
      'Direction',
      'Entry → exit',
      'Held',
      'Bought / Sold',
      'Avg entry',
      'Avg exit',
      'Gross P&L',
      'Charges (est.)',
      'Net P&L',
    ])
    // the table keeps the server's newest-first order; numbers follow opening time
    expect(screen.getByRole('button', { name: /details of trade 2, INFY/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /details of trade 1, RELIANCE/ })).toBeInTheDocument()
  })

  it('shows the realized-so-far of an open position partly closed', async () => {
    summaryFn.mockResolvedValue(summary({ positions: [position({ realized_so_far: 200 })] }))
    renderIt()
    const open = (await screen.findByText('Open positions')).closest(
      '[data-slot="card"]'
    ) as HTMLElement
    expect(within(open).getByRole('columnheader', { name: 'Realized so far' })).toBeInTheDocument()
    expect(within(open).getByText('Rs 200.00')).toBeInTheDocument()
  })

  it('shows a dash for the ratios when a partial exit made money but no trade completed', async () => {
    const base = summary()
    summaryFn.mockResolvedValue(
      summary({
        trades: [],
        trades_total: 0,
        realized_gross: 200,
        metrics: {
          ...base.metrics,
          trades_count: 0,
          winning_trades: 0,
          losing_trades: 0,
          win_rate: 0,
          profit_factor: 0,
          avg_win: 0,
          avg_loss: 0,
          expectancy: 0,
          net_profit: 200,
        },
        positions: [position({ realized_so_far: 200 })],
      })
    )
    renderIt()
    await screen.findByText('Open positions')
    expect(cardValue('Completed trades')).toBe('0')
    for (const label of [
      'Win rate (net)',
      'Profit factor (net)',
      'Avg win (net)',
      'Avg loss (net)',
      'Expectancy (net)',
    ]) {
      expect(cardValue(label)).toBe('—')
    }
  })

  it('lists the fills of every leg in the fills dialog', async () => {
    const fill = (fill_ref: number, role: 'ENTRY' | 'EXIT', qty: number, price: number) => ({
      fill_ref,
      role,
      qty_used: qty,
      charge_allocated: 0.5,
      price,
      fill_time: '2026-11-16 09:20:00',
      order_id: `ORD-${fill_ref}`,
      fill_id: `f${fill_ref}`,
      broker: 'flattrade',
      account: 'FT1',
      symbol: 'RELIANCE',
      exchange: 'NSE',
      product: 'MIS',
      side: role === 'ENTRY' ? 'BUY' : 'SELL',
      attribution_state: 'manual',
    })
    fillsFn.mockResolvedValue({
      build_id: 'b1',
      trade: trade({ legs: 2, closed_quantity: 45 }),
      linked_fills: 4,
      fills: [fill(1, 'ENTRY', 45, 100), fill(2, 'EXIT', 20, 110), fill(3, 'EXIT', 25, 108)],
    })
    renderIt()
    await openFills('RELIANCE')
    const dialog = await screen.findByRole('dialog')
    expect(await within(dialog).findByText('ORD-1')).toBeInTheDocument()
    expect(within(dialog).getByText('ORD-2')).toBeInTheDocument()
    expect(within(dialog).getByText('ORD-3')).toBeInTheDocument()
    expect(
      within(dialog)
        .getAllByTestId('lifecycle-label')
        .map((e) => e.textContent)
    ).toEqual(['Entry', 'Partial exit', 'Final exit'])
    expect(
      within(dialog)
        .getAllByTestId('lifecycle-net')
        .map((e) => e.textContent)
    ).toEqual(['+45', '+25', '0'])
    expect(within(dialog).getByTestId('lifecycle-peak')).toHaveTextContent(
      'Peak position in this trade 45'
    )
  })
})

describe("today's performance", () => {
  it('is shown above the metric cards and opens the fills of a closed row with the build id', async () => {
    summaryFn.mockResolvedValue(
      summary({
        today: emptyToday({
          realized_gross: 450,
          net: 437.5,
          completed_positions: 1,
          closed_positions: [todayClosed()],
        }),
      })
    )
    renderIt()
    const section = await screen.findByTestId('discretionary-today')
    expect(within(section).getByText("Today's closed positions")).toBeInTheDocument()
    // Today's figures sit in the summary header, right after the headline figures and before the groups
    const cards = screen.getByText('Net realized (after est. charges)')
    expect(cards.compareDocumentPosition(section) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    const groups = screen.getByTestId('discretionary-groups-performance')
    expect(section.compareDocumentPosition(groups) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    await userEvent.click(within(section).getByTestId('today-closed-row'))
    await waitFor(() => expect(fillsFn).toHaveBeenCalledWith(11, 'b1'))
  })

  it('coexists with the awaiting-attribution state: zeros and the pending note', async () => {
    summaryFn.mockResolvedValue({
      ...emptySummary(),
      executions_collected: 1,
      executions_unresolved: 1,
      today: emptyToday({
        pending_attribution: {
          ...emptyToday().pending_attribution,
          grace_count: 1,
          earliest_attributable_at: '2026-11-17 11:20:00',
          grace_fills: [
            {
              id: 1,
              broker: 'flattrade',
              account: 'A1',
              symbol: 'TCS',
              exchange: 'NSE',
              product: 'MIS',
              side: 'BUY',
              qty: 4,
              price: 2392.6,
              fill_time: '2026-11-17 10:50:00',
              attributable_from: '2026-11-17 11:20:00',
              due: false,
              affects_open_position: false,
            },
          ],
        },
      }),
    })
    renderIt()
    expect(await screen.findByTestId('discretionary-awaiting-attribution')).toBeInTheDocument()
    const section = screen.getByTestId('discretionary-today')
    expect(within(section).getByTestId('today-pending')).toHaveTextContent(
      'Earliest attribution at 11:20 IST'
    )
    expect(within(section).getByText('No positions closed today yet')).toBeInTheDocument()
  })

  it('is absent when an older server sends no today block', async () => {
    renderIt()
    await screen.findByText('Net realized (after est. charges)')
    expect(screen.queryByTestId('discretionary-today')).not.toBeInTheDocument()
  })
})

describe('dashboard additions', () => {
  function full() {
    return summary({
      sample: {
        completed_trades: 2,
        trading_days: 1,
        min_trades: 30,
        min_days: 10,
        sufficient: false,
      },
      consistency: consistency(),
      breakdowns: tradingBreakdowns(),
      data_quality: dataQuality(),
    })
  }

  it('renders every new section in page order with sample context', async () => {
    // three close dates: the daily bars are part of this profile (they are hidden for one day)
    summaryFn.mockResolvedValue(withDays(3, full()))
    renderIt()
    const dq = await screen.findByTestId('data-quality')
    await openAdvanced()
    const risk = screen.getByTestId('risk-strip')
    const how = screen.getByTestId('how-you-trade')
    expect(screen.getByTestId('daily-pnl-chart')).toBeInTheDocument()
    expect(screen.getByTestId('discretionary-sample-note')).toHaveTextContent(
      'Ratios based on 2 completed trade(s) over 1 trading day(s).'
    )
    // the risk strip, then How you trade (both in Advanced analysis), then data quality at the bottom
    expect(risk.compareDocumentPosition(how) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(how.compareDocumentPosition(dq) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // the existing breakdown tables stay, so no table is shown twice
    expect(screen.getByText('By symbol')).toBeInTheDocument()
    expect(within(how).queryByText('By symbol')).not.toBeInTheDocument()
  })

  it('opens the fills of the largest win with the summary build id', async () => {
    summaryFn.mockResolvedValue(full())
    renderIt()
    await userEvent.click(await screen.findByRole('button', { name: /largest win, RELIANCE/ }))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(fillsFn).toHaveBeenCalledWith(11, 'b1')
    await userEvent.keyboard('{Escape}')
    await userEvent.click(screen.getByRole('button', { name: /largest loss, INFY/ }))
    await waitFor(() => expect(fillsFn).toHaveBeenLastCalledWith(12, 'b1'))
  })

  it('shows a dash with the reason for the maximum drawdown of a single trading day, in the summary only', async () => {
    summaryFn.mockResolvedValue(
      summary({
        consistency: consistency({
          max_drawdown: null,
          max_drawdown_unavailable_reason: 'needs at least 2 trading days',
        }),
      })
    )
    renderIt()
    const tile = await screen.findByTestId('summary-drawdown')
    expect(tile).toHaveTextContent('—')
    expect(
      screen.getByText(
        'needs at least 2 trading days; basis: gross realized, excludes charges and unrealized'
      )
    ).toBeInTheDocument()
    await openAdvanced()
    // the Risk card no longer repeats it
    expect(screen.queryByTestId('risk-drawdown')).not.toBeInTheDocument()
  })

  it('shows the position age next to the opening date', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-11-17T05:45:00Z')) // 11:15 IST
    try {
      summaryFn.mockResolvedValue(
        summary({ positions: [position({ opened_at: '2026-11-17 09:00:00' })] })
      )
      renderIt()
      const open = (await screen.findByText('Open positions')).closest(
        '[data-slot="card"]'
      ) as HTMLElement
      expect(within(open).getByRole('columnheader', { name: 'Age' })).toBeInTheDocument()
      expect(within(open).getByText('2h 15m')).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps the awaiting message and shows no provisional panel when only unresolved fills exist', async () => {
    summaryFn.mockResolvedValue({
      ...emptySummary(),
      executions_collected: 5,
      executions_unresolved: 5,
      data_quality: dataQuality({
        collected: 5,
        awaiting_attribution: 5,
        awaiting_grace: 0,
        awaiting_other: 5,
        attributed_open: 0,
        included: 0,
        not_placed: 0,
      }),
      // the server still sends it; the page no longer shows it
      provisional: provisional(),
    })
    renderIt()
    const block = await screen.findByTestId('discretionary-awaiting-attribution')
    expect(block).toHaveTextContent(
      'No attributable trades available for performance analysis yet. 5 fills collected; 5 awaiting attribution. Their P&L is excluded until classification is resolved.'
    )
    expect(screen.queryByTestId('provisional-panel')).not.toBeInTheDocument()
    expect(screen.queryByText(/Pending attribution: provisional figures/)).not.toBeInTheDocument()
    expect(screen.getByTestId('dq-awaiting')).toHaveTextContent('5')
    expect(screen.queryByText('Net realized (after est. charges)')).not.toBeInTheDocument()
  })

  it('keeps provisional figures out of the populated page', async () => {
    summaryFn.mockResolvedValue({ ...full(), provisional: provisional() })
    renderIt()
    await screen.findByText('Closed trades')
    expect(screen.queryByTestId('provisional-panel')).not.toBeInTheDocument()
    expect(cardValue('Gross realized')).toBe('Rs 60.00')
    expect(cardValue('Completed trades')).toBe('2')
  })
})

describe('refresh after a broker fill (order_update socket event)', () => {
  const liveFill = { mode: 'live', filled_quantity: 10, symbol: 'TCS' }

  async function rendered() {
    const view = renderIt()
    await waitFor(() => expect(summaryFn).toHaveBeenCalled())
    vi.useFakeTimers()
    const spy = vi.spyOn(lastClient, 'invalidateQueries')
    const keys = () =>
      spy.mock.calls.map((c) => JSON.stringify((c[0] as { queryKey: unknown }).queryKey))
    return { view, keys }
  }

  function advance(ms: number) {
    act(() => {
      vi.advanceTimersByTime(ms)
    })
  }

  afterEach(() => {
    vi.useRealTimers()
  })

  it('invalidates the discretionary queries 10 s and 25 s after a live fill, not before', async () => {
    const { keys } = await rendered()
    act(() => sock.emit('order_update', liveFill))
    advance(9_999)
    expect(keys()).toEqual([])
    advance(1)
    expect(keys()).toEqual([JSON.stringify(['discretionary-pnl'])])
    advance(15_000)
    expect(keys()).toEqual([
      JSON.stringify(['discretionary-pnl']),
      JSON.stringify(['discretionary-pnl']),
    ])
    advance(60_000)
    expect(keys().length).toBe(2)
  })

  it('coalesces a burst of partial fills into one pair of refetches after the last event', async () => {
    const { keys } = await rendered()
    for (let i = 1; i <= 5; i++) {
      act(() => sock.emit('order_update', { ...liveFill, filled_quantity: i * 5 }))
      advance(1_000)
    }
    // last event at t=4 s: refetches at 14 s and 29 s
    advance(8_999)
    expect(keys()).toEqual([])
    advance(1)
    expect(keys().length).toBe(1)
    advance(15_000)
    expect(keys().length).toBe(2)
  })

  it('ignores analyze/paper updates and unfilled orders', async () => {
    const { keys } = await rendered()
    act(() => {
      sock.emit('order_update', { mode: 'analyze', filled_quantity: 10 })
      sock.emit('order_update', { mode: 'live', filled_quantity: 0 })
      sock.emit('order_update', { mode: 'live' })
      sock.emit('order_update', null)
    })
    advance(60_000)
    expect(keys()).toEqual([])
  })

  it('clears its timers and its listener on unmount', async () => {
    const { view, keys } = await rendered()
    act(() => sock.emit('order_update', liveFill))
    view.unmount()
    expect(sock.handlers.get('order_update')?.size ?? 0).toBe(0)
    advance(60_000)
    expect(keys()).toEqual([])
  })

  it('keeps the existing order-event refresh unchanged, so one fill is not refetched twice', async () => {
    await rendered()
    const events = vi.mocked(useOrderEventRefresh).mock.calls.at(-1)?.[1]?.events
    expect(events).toEqual(['order_event', 'analyzer_update'])
    expect(sock.handlers.get('order_update')?.size).toBe(1)
  })
})

describe('category selector', () => {
  it('offers All trades, Intraday and Swing, and asks for all by default', async () => {
    renderIt()
    await screen.findByText('Closed trades')
    const group = screen.getByRole('group', { name: 'Trade category' })
    expect(
      within(group)
        .getAllByRole('button')
        .map((b) => b.textContent)
    ).toEqual(['All trades', 'Intraday', 'Swing'])
    expect(within(group).getByRole('button', { name: 'All trades' })).toHaveAttribute(
      'aria-pressed',
      'true'
    )
    expect(summaryFn).toHaveBeenLastCalledWith('30d', undefined, {}, false, 'all')
  })

  it('refetches the summary and the excursions with the chosen category', async () => {
    renderIt()
    await screen.findByText('Closed trades')
    await userEvent.click(screen.getByRole('button', { name: 'Swing' }))
    await waitFor(() =>
      expect(summaryFn).toHaveBeenLastCalledWith('30d', undefined, {}, false, 'swing')
    )
    await waitFor(() =>
      expect(excursionsFn).toHaveBeenLastCalledWith('30d', undefined, {}, 'swing')
    )
    expect(screen.getByRole('button', { name: 'Swing' })).toHaveAttribute('aria-pressed', 'true')
    await userEvent.click(screen.getByRole('button', { name: 'Intraday' }))
    await waitFor(() =>
      expect(summaryFn).toHaveBeenLastCalledWith('30d', undefined, {}, false, 'intraday')
    )
  })

  it('shows the classification counts, the unclassified count with its reasons, and the note', async () => {
    summaryFn.mockResolvedValue(summary({ classification: classification() }))
    renderIt()
    const block = await screen.findByTestId('classification')
    expect(block).toHaveTextContent('1 intraday, 0 swing, 1 unclassified')
    expect(block).toHaveTextContent('1 unclassified position(s) are included in All trades only.')
    expect(block).toHaveTextContent('unclassified (reason shown) is never guessed')
    expect(within(block).getByTestId('unclassified-count')).toHaveAttribute(
      'title',
      '1: the fill that opened it is not in the ledger'
    )
  })

  it('says unclassified positions are left out of a category view', async () => {
    summaryFn.mockResolvedValue(
      summary({ classification: classification({ selected: 'swing' }), category: 'swing' })
    )
    renderIt()
    await userEvent.click(await screen.findByRole('button', { name: 'Swing' }))
    expect(await screen.findByTestId('classification')).toHaveTextContent(
      'are left out of this view; they appear in All trades only'
    )
  })

  it('does not break when the classification block is null', async () => {
    summaryFn.mockResolvedValue(summary({ classification: null }))
    renderIt()
    await screen.findByText('Closed trades')
    expect(screen.queryByTestId('classification')).not.toBeInTheDocument()
  })
})

describe('new analysis blocks', () => {
  function withBlocks(extra: Partial<ReturnType<typeof summary>> = {}) {
    return summary({
      consistency: consistency(),
      risk: riskBlock(),
      points_by_instrument: [
        pointsRow(),
        pointsRow({
          symbol: 'NIFTY',
          exchange: 'NFO',
          net_points: -40,
          avg_win_points: null,
          largest_win_points: null,
        }),
        pointsRow({ symbol: 'RELIANCE', exchange: 'BSE', net_points: 3 }),
      ],
      monthly: monthRows(),
      weekday: weekdayRows(),
      trade_management: tradeManagement(),
      costs: costsBlock(),
      ...extra,
    })
  }

  it('shows points per instrument with symbol and exchange and no combined total', async () => {
    summaryFn.mockResolvedValue(withBlocks())
    renderIt()
    await openAdvanced()
    const card = (await screen.findByTestId('points-card')) as HTMLElement
    expect(card).toHaveTextContent('Points per unit')
    const rows = within(card).getAllByTestId('points-row')
    expect(rows).toHaveLength(3)
    expect(rows[0]).toHaveTextContent('RELIANCE')
    expect(rows[0]).toHaveTextContent('NSE')
    expect(rows[1]).toHaveTextContent('NIFTY')
    expect(rows[1]).toHaveTextContent('NFO')
    // the same symbol on two exchanges stays two rows
    expect(rows[2]).toHaveTextContent('BSE')
    expect(within(card).queryByText(/^total$/i)).not.toBeInTheDocument()
    expect(card.textContent).not.toMatch(/\btotal\b/i)
    // a missing figure is a dash, not 0
    expect(rows[1]).toHaveTextContent('—')
  })

  it('shows drawdown in rupees without a percentage, and the streaks', async () => {
    summaryFn.mockResolvedValue(withBlocks())
    renderIt()
    await openAdvanced()
    const strip = await screen.findByTestId('risk-strip')
    // the maximum drawdown is in the summary only; the strip keeps the current drawdown
    expect(within(strip).queryByTestId('risk-drawdown')).not.toBeInTheDocument()
    expect(screen.getByTestId('summary-drawdown')).toHaveTextContent('-Rs 320.50')
    expect(within(strip).getByTestId('risk-current-drawdown')).toHaveTextContent('-Rs 120.00')
    expect(strip.textContent).not.toMatch(/%/)
    expect(within(strip).getByTestId('risk-win-streak')).toHaveTextContent('3')
    expect(within(strip).getByTestId('risk-loss-streak')).toHaveTextContent('2')
    expect(within(strip).getByTestId('risk-current-streak')).toHaveTextContent('2 losses')
    expect(screen.getByTestId('risk-basis')).toHaveTextContent('1 position(s) without a net P&L')
    // one net drawdown, not a second (gross) card next to it
    expect(screen.queryByText(/basis: gross realized/)).not.toBeInTheDocument()
    // the largest win and loss are in the summary, not repeated in this strip
    expect(within(strip).queryByTestId('risk-largest-win')).not.toBeInTheDocument()
    expect(screen.getByTestId('summary-largest-win')).toBeInTheDocument()
  })

  it('shows trade management and costs, with the reason when the percentage is null', async () => {
    summaryFn.mockResolvedValue(
      withBlocks({
        costs: costsBlock({
          gross_profit: 0,
          charges_pct_of_gross_profit: null,
          reason: 'no gross profit',
        }),
      })
    )
    renderIt()
    await openAdvanced()
    const tm = await screen.findByTestId('trade-management')
    expect(within(tm).getByTestId('tm-winner-hold')).toHaveTextContent('45m')
    expect(within(tm).getByTestId('tm-winner-hold')).toHaveTextContent('median 30m, 3 position(s)')
    expect(within(tm).getByTestId('tm-loser-hold')).toHaveTextContent('2h 05m')
    expect(within(tm).getByTestId('tm-median-net')).toHaveTextContent('Rs 42.50')
    expect(within(tm).getByTestId('cost-pct')).toHaveTextContent('no gross profit')
    expect(within(tm).getByTestId('cost-pct')).not.toHaveTextContent(/\d%/)
    expect(tm).toHaveTextContent('1 break-even position(s)')
  })

  it('shows the charges percentage when there is gross profit', async () => {
    summaryFn.mockResolvedValue(withBlocks())
    renderIt()
    await openAdvanced()
    expect(await screen.findByTestId('cost-pct')).toHaveTextContent('15.0%')
    // total charges are shown once, in the summary
    expect(screen.queryByTestId('cost-total')).not.toBeInTheDocument()
    expect(screen.getByTestId('summary-charges')).toBeInTheDocument()
  })

  it('shows the monthly and weekday tables and bars, with signs and the close-date basis', async () => {
    // 10 close dates over 2 months: monthly and weekday tables are part of this profile
    const base = withBlocks()
    summaryFn.mockResolvedValue({ ...base, by_period: { ...base.by_period, daily: dailyRows(10) } })
    renderIt()
    await openAdvanced()
    const card = await screen.findByTestId('calendar-card')
    const rows = within(card).getAllByTestId('calendar-row')
    expect(rows).toHaveLength(5)
    expect(rows[0]).toHaveTextContent('2026-10')
    expect(rows[0]).toHaveTextContent('75.0%')
    expect(rows[1]).toHaveTextContent('Rs -150.00')
    // a weekday without positions shows a dash for win rate and net
    expect(rows[3]).toHaveTextContent('Tue')
    expect(rows[3]).toHaveTextContent('—')
    expect(within(card).getByRole('columnheader', { name: 'Weekday' })).toBeInTheDocument()
    const monthly = within(card).getByTestId('monthly-chart')
    const bars = within(monthly).getAllByTestId('net-bar')
    expect(bars[0]).toHaveTextContent('+Rs 460.00')
    expect(bars[1]).toHaveTextContent('-Rs 150.00')
    expect(monthly).toHaveTextContent('Net P&L in rupees (Rs)')
    expect(within(card).getByTestId('weekday-chart')).toBeInTheDocument()
  })

  it('shows the symbol chart from the by-symbol breakdown', async () => {
    summaryFn.mockResolvedValue(
      withBlocks({
        by_symbol: [
          breakdownRow('RELIANCE', { net_of_charges: 300, trades: 3 }),
          breakdownRow('INFY', { net_of_charges: -80, trades: 2 }),
        ],
      })
    )
    renderIt()
    await openAdvanced()
    const chart = await screen.findByTestId('symbol-chart')
    const bars = within(chart).getAllByTestId('net-bar')
    expect(bars[0]).toHaveTextContent('RELIANCE')
    expect(bars[0]).toHaveTextContent('+Rs 300.00')
    expect(bars[1]).toHaveTextContent('-Rs 80.00')
  })

  it('does not crash and shows nothing misleading when every new block is null', async () => {
    summaryFn.mockResolvedValue(
      summary({
        consistency: consistency(),
        risk: null,
        points_by_instrument: null,
        monthly: null,
        weekday: null,
        trade_management: null,
        costs: null,
        classification: null,
        reconciliation: null,
      })
    )
    renderIt()
    await openAdvanced()
    await screen.findByText('Closed trades')
    expect(screen.queryByTestId('points-card')).not.toBeInTheDocument()
    expect(screen.queryByTestId('calendar-card')).not.toBeInTheDocument()
    expect(screen.queryByTestId('trade-management')).not.toBeInTheDocument()
    // without the net risk block the gross figures stay, labelled as gross
    expect(screen.getByTestId('summary-drawdown')).toHaveTextContent('gross realized')
    expect(screen.queryByTestId('risk-drawdown')).not.toBeInTheDocument()
    expect(screen.queryByTestId('risk-current-drawdown')).not.toBeInTheDocument()
  })

  it('shows a small note for completed trades without a net, only above 0', async () => {
    summaryFn.mockResolvedValue(
      summary({ metrics: { ...summary().metrics, net_basis_trades: 1, net_unavailable_trades: 1 } })
    )
    renderIt()
    expect(await screen.findByTestId('net-unavailable-note')).toHaveTextContent(
      '1 completed trade(s) have no net P&L'
    )
  })

  it('shows no such note at 0', async () => {
    summaryFn.mockResolvedValue(
      summary({ metrics: { ...summary().metrics, net_basis_trades: 2, net_unavailable_trades: 0 } })
    )
    renderIt()
    await screen.findByText('Closed trades')
    expect(screen.queryByTestId('net-unavailable-note')).not.toBeInTheDocument()
  })

  it('does not scale anything: rupee fields and points go through the page formatter once', async () => {
    summaryFn.mockResolvedValue(withBlocks())
    renderIt({ formatCurrency: (v: number) => `Rs ${v.toFixed(2)}` })
    await openAdvanced()
    const card = await screen.findByTestId('points-card')
    // points are shown as recorded, not as rupees
    expect(card).not.toHaveTextContent('Rs')
    expect(within(card).getAllByTestId('points-row')[0]).toHaveTextContent('+25.00')
  })
})

describe('MAE / MFE', () => {
  it('loads after the summary and shows the note, coverage and precision text', async () => {
    renderIt()
    await openAdvanced()
    const panel = await screen.findByTestId('excursion-panel')
    expect(excursionsFn).toHaveBeenCalledWith('30d', undefined, {}, 'all')
    expect(within(panel).getByTestId('excursions-note')).toHaveTextContent(EXCURSION_NOTE)
    expect(within(panel).getByTestId('excursions-coverage')).toHaveTextContent(
      '1 of 2 positions available'
    )
    expect(within(panel).getByTestId('excursions-coverage')).toHaveTextContent(
      'unsupported segment'
    )
    expect(within(panel).getByTestId('excursions-precision')).toHaveTextContent(
      'Minute-bar based lower bound'
    )
  })

  it('does not block the page while it loads', async () => {
    excursionsFn.mockReturnValue(new Promise(() => {}))
    renderIt()
    await openAdvanced()
    expect(await screen.findByText('Closed trades')).toBeInTheDocument()
    expect(screen.getByTestId('excursions-status')).toHaveTextContent('Loading MAE / MFE')
  })

  it('asks for nothing when there are no closed trades', async () => {
    summaryFn.mockResolvedValue(emptySummary())
    renderIt()
    await screen.findByText('No discretionary trades yet')
    expect(excursionsFn).not.toHaveBeenCalled()
  })

  it('shows the aggregate table: averages, medians, profit capture of winners', async () => {
    renderIt()
    await openAdvanced()
    const panel = await screen.findByTestId('excursion-panel')
    const rows = within(panel).getAllByTestId('excursion-row')
    expect(rows[0]).toHaveTextContent('points')
    expect(rows[0]).toHaveTextContent('2.50')
    expect(rows[1]).toHaveTextContent('14.00')
    expect(rows[2]).toHaveTextContent('Rs 25.00')
    const capture = within(panel).getByTestId('profit-capture-row')
    expect(capture).toHaveTextContent('1 winner(s)')
    expect(capture).toHaveTextContent('71.4%')
    // losers have no sample: dashes, never 0
    expect(rows[0].textContent).not.toMatch(/0\.00.*—|—.*0\.00/)
  })

  it('shows a dash with the reason, never 0, for an unavailable position in the detail row', async () => {
    renderIt()
    const table = (await screen.findByText('Closed trades')).closest(
      '[data-slot="card"]'
    ) as HTMLElement
    await userEvent.click(within(table).getByRole('button', { name: /details of trade \d+, INFY/ }))
    await userEvent.click(
      within(table).getByRole('button', { name: /details of trade \d+, RELIANCE/ })
    )
    const [relDetail, infyDetail] = within(table).getAllByTestId('closed-trade-detail')
    await waitFor(() =>
      expect(within(infyDetail).getAllByTestId('mae-unavailable')).toHaveLength(1)
    )
    const dash = within(infyDetail).getByTestId('mae-unavailable')
    expect(dash).toHaveTextContent('—')
    expect(dash).toHaveAttribute('title', 'unsupported segment')
    expect(within(infyDetail).getByTestId('mfe-unavailable')).toHaveAttribute(
      'title',
      'unsupported segment'
    )
    expect(infyDetail.textContent).not.toMatch(/\b0\.00\b/)
    // the available one shows its points
    expect(relDetail).toHaveTextContent('2.50')
    expect(relDetail).toHaveTextContent('14.00')
  })

  it('words the MFE as a market measurement, not achievable profit', async () => {
    renderIt()
    const table = (await screen.findByText('Closed trades')).closest(
      '[data-slot="card"]'
    ) as HTMLElement
    await userEvent.click(
      within(table).getByRole('button', { name: /details of trade \d+, RELIANCE/ })
    )
    const detail = within(table).getByTestId('closed-trade-detail')
    const mfe = await within(detail).findByTestId('mfe-value')
    expect(mfe.getAttribute('title')).toContain(
      'price move x quantity open at that moment; a market measurement, not achievable profit'
    )
    await openAdvanced()
    const panel = await screen.findByTestId('excursion-panel')
    expect(panel).toHaveTextContent(
      'MFE: largest favourable price move while open (market measurement, not realisable P&L)'
    )
    expect(panel).not.toHaveTextContent('best move for you')
    expect(screen.getByTestId('profit-capture-block')).toHaveTextContent(
      'of the favourable move (minute bars, lower bound)'
    )
    // the lower-bound text stays
    expect(screen.getByTestId('excursions-precision')).toHaveTextContent('lower bound')
  })

  it('shows dashes while the excursions have not loaded, not zero', async () => {
    excursionsFn.mockReturnValue(new Promise(() => {}))
    renderIt()
    await userEvent.click(
      await screen.findByRole('button', { name: /details of trade \d+, RELIANCE/ })
    )
    const detail = await screen.findByTestId('closed-trade-detail')
    expect(within(detail).getByTestId('mae-unavailable')).toHaveAttribute(
      'title',
      'MAE / MFE is not loaded yet'
    )
  })

  it('does not join excursions of another build to the rows', async () => {
    excursionsFn.mockResolvedValue(excursions({ build_id: 'other' }))
    renderIt()
    await openAdvanced()
    await screen.findByTestId('excursion-panel')
    await userEvent.click(screen.getByRole('button', { name: /details of trade \d+, RELIANCE/ }))
    const detail = screen.getByTestId('closed-trade-detail')
    expect(within(detail).getByTestId('mae-unavailable')).toBeInTheDocument()
  })

  it('shows an error with a retry when the excursions fail, and the rest of the page stays', async () => {
    excursionsFn.mockRejectedValueOnce(new DiscretionaryApiError('Could not compute MAE / MFE.'))
    renderIt()
    await openAdvanced()
    expect(await screen.findByText('MAE / MFE could not be loaded.')).toBeInTheDocument()
    expect(screen.getByText('Closed trades')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByTestId('excursion-panel')).toBeInTheDocument()
  })

  it('plots winners and losers differently and leaves unavailable positions out', async () => {
    excursionsFn.mockResolvedValue(
      excursions({
        positions: [
          excursionPosition({ id: 1 }),
          excursionPosition({ id: 2, net_pnl: -30, mae_points: 6, mfe_points: 3 }),
          excursionPosition({
            id: 3,
            status: 'unavailable',
            reason: 'missing_bars',
            reason_text: 'price data has missing minutes',
            mae_points: null,
            mfe_points: null,
          }),
        ],
      })
    )
    renderIt()
    await openAdvanced()
    const scatter = await screen.findByTestId('mae-mfe-scatter')
    expect(within(scatter).getAllByTestId('scatter-winner')).toHaveLength(1)
    expect(within(scatter).getAllByTestId('scatter-loser')).toHaveLength(1)
    expect(scatter).toHaveTextContent('MAE (points per unit)')
    expect(scatter).toHaveTextContent('MFE (points per unit)')
    expect(scatter).toHaveTextContent('winner (1)')
    expect(scatter).toHaveTextContent('loser (1)')
  })

  it('says there is nothing to plot when no position has MAE / MFE', async () => {
    excursionsFn.mockResolvedValue(
      excursions({
        positions: [
          excursionPosition({
            status: 'unavailable',
            reason: 'after_market_close',
            reason_text: 'available after market close',
            mae_points: null,
            mfe_points: null,
          }),
        ],
      })
    )
    renderIt()
    await openAdvanced()
    expect(await screen.findByTestId('mae-mfe-scatter-empty')).toBeInTheDocument()
  })
})

describe('closed positions table', () => {
  it('has the eleven primary columns and moves the secondary ones to the detail row', async () => {
    renderIt()
    const table = (await screen.findByText('Closed trades')).closest(
      '[data-slot="card"]'
    ) as HTMLElement
    for (const name of [
      'Trade #',
      'Symbol',
      'Direction',
      'Entry → exit',
      'Held',
      'Bought / Sold',
      'Avg entry',
      'Avg exit',
      'Gross P&L',
      'Charges (est.)',
      'Net P&L',
    ]) {
      expect(within(table).getByRole('columnheader', { name })).toBeInTheDocument()
    }
    for (const name of [
      'Category',
      'Points / unit',
      'MAE',
      'MFE',
      'Product',
      'Broker',
      'Account',
    ]) {
      expect(within(table).queryByRole('columnheader', { name })).not.toBeInTheDocument()
    }
    await userEvent.click(
      within(table).getByRole('button', { name: /details of trade \d+, RELIANCE/ })
    )
    const detail = within(table).getByTestId('closed-trade-detail')
    for (const label of [
      'Points / unit',
      'MAE',
      'MFE',
      'Category',
      'Product',
      'Broker',
      'Account',
    ]) {
      expect(within(detail).getByText(label)).toBeInTheDocument()
    }
    expect(
      within(detail).getByRole('button', { name: /Show fills and lifecycle/ })
    ).toBeInTheDocument()
  })

  it('shows the exchange, the times, points per unit and a dash for a missing net', async () => {
    summaryFn.mockResolvedValue(
      summary({
        trades: [
          trade({ category: 'intraday', points_per_unit: 10 }),
          trade({
            id: 12,
            symbol: 'INFY',
            exchange: 'BSE',
            direction: 'SHORT',
            realized_pnl: -40,
            net_pnl: null,
            charges: null,
            charges_status: 'unavailable',
            category: 'swing',
            points_per_unit: -4.5,
            opened_at: '2026-11-13 10:00:00',
            closed_at: '2026-11-16 11:30:00',
          }),
        ],
      })
    )
    renderIt()
    const relBtn = await screen.findByRole('button', { name: /details of trade \d+, RELIANCE/ })
    const infyBtn = screen.getByRole('button', { name: /details of trade \d+, INFY/ })
    const rel = relBtn.closest('tr') as HTMLElement
    const infy = infyBtn.closest('tr') as HTMLElement
    expect(rel).toHaveTextContent('RELIANCENSE')
    // same day: the exit shows only its clock
    expect(rel).toHaveTextContent('2026-11-16 09:20 → 10:05')
    expect(rel).toHaveTextContent('Long')
    expect(infy).toHaveTextContent('INFYBSE')
    // another day: both stamps in full
    expect(infy).toHaveTextContent('2026-11-13 10:00 → 2026-11-16 11:30')
    expect(infy).toHaveTextContent('Short')
    // net unavailable: a dash, not 0
    const cells = within(infy).getAllByRole('cell')
    expect(cells[10]).toHaveTextContent('—')
    await userEvent.click(relBtn)
    await userEvent.click(infyBtn)
    const [relDetail, infyDetail] = screen.getAllByTestId('closed-trade-detail')
    expect(relDetail).toHaveTextContent('+10.00')
    expect(relDetail).toHaveTextContent('Intraday')
    expect(infyDetail).toHaveTextContent('Swing')
    expect(infyDetail).toHaveTextContent('-4.50')
  })

  it('shows Unclassified with the reason as a tooltip', async () => {
    summaryFn.mockResolvedValue(
      summary({
        trades: [
          trade({
            category: 'unclassified',
            category_reason: 'opening_unknown',
            opening_unknown: true,
          }),
        ],
      })
    )
    renderIt()
    // the reliability flag stays on the row
    expect(await screen.findByText('opening unknown')).toBeInTheDocument()
    await userEvent.click(
      await screen.findByRole('button', { name: /details of trade \d+, RELIANCE/ })
    )
    const badge = await screen.findByTestId('category-badge')
    expect(badge).toHaveTextContent('Unclassified')
    expect(badge).toHaveAttribute(
      'title',
      'Unclassified: the fill that opened it is not in the ledger'
    )
    expect(screen.queryByText('opening_unknown')).not.toBeInTheDocument()
  })

  it('keeps the table scrolling inside its card', async () => {
    renderIt()
    const table = (await screen.findByText('Closed trades')).closest('[data-slot="card"]')
    expect(table?.querySelector('[data-slot="table-container"]')).toHaveClass('overflow-x-auto')
  })

  it('says showing N of M when the list is cut, using trades_shown and trades_total', async () => {
    summaryFn.mockResolvedValue(
      summary({ trades_total: 640, trades_shown: 500, trades_truncated: 140 })
    )
    renderIt()
    expect(await screen.findByTestId('trades-truncated')).toHaveTextContent(
      'Showing 500 of 640 trades'
    )
  })

  it('says nothing about a cut list when nothing is cut', async () => {
    renderIt()
    await screen.findByText('Closed trades')
    expect(screen.queryByTestId('trades-truncated')).not.toBeInTheDocument()
  })
})

describe('export CSV', () => {
  it('links to the export with the same scope as the summary, category included', async () => {
    const { summaryParams } = await import('@/api/discretionaryPnl')
    renderIt({ period: 'custom', range: { start: '2026-11-01', end: '2026-11-10' } })
    await screen.findByText('Closed trades')
    await userEvent.type(screen.getByLabelText('Symbol'), 'TCS{Enter}')
    await userEvent.click(screen.getByRole('button', { name: 'Intraday' }))
    await waitFor(() =>
      expect(screen.getByTestId('export-csv').getAttribute('href')).toContain('category=intraday')
    )
    const link = screen.getByTestId('export-csv')
    expect(screen.getByRole('link', { name: /Export CSV/ })).toBe(link)
    const href = link.getAttribute('href') as string
    expect(href.startsWith('/api/discretionary-pnl/export.csv?')).toBe(true)
    const exportParams = Object.fromEntries(new URLSearchParams(href.split('?')[1]))
    const summaryScope = Object.fromEntries(
      summaryParams(
        'custom',
        { start: '2026-11-01', end: '2026-11-10' },
        { broker: undefined, account: undefined, symbol: 'TCS' },
        false,
        'intraday'
      )
    )
    delete summaryScope.quotes
    expect(exportParams).toEqual(summaryScope)
    expect(exportParams).toMatchObject({
      period: 'custom',
      start: '2026-11-01',
      end: '2026-11-10',
      symbol: 'TCS',
      category: 'intraday',
    })
  })
})

describe('no net P&L: ratios are null, shown as a dash and never as 0', () => {
  const nullRatios = {
    win_rate: null,
    profit_factor: null,
    expectancy: null,
    avg_win: null,
    avg_loss: null,
  } as const

  function cardValue(label: string): string {
    const card = screen.getByText(label).parentElement as HTMLElement
    return (card.children[1] as HTMLElement).textContent ?? ''
  }

  it('renders null win rate, profit factor, expectancy and averages as a dash', async () => {
    const base = summary()
    summaryFn.mockResolvedValue(
      summary({
        metrics: {
          ...base.metrics,
          ...nullRatios,
          winning_trades: 0,
          losing_trades: 0,
          net_basis_trades: 0,
          net_unavailable_trades: 2,
          count_unavailable_reason: 'no completed trades with a net P&L',
        },
        by_symbol: [
          breakdownRow('INFY', { ...nullRatios, wins: 0, losses: 0, trades: 2 }),
          breakdownRow('TCS'),
        ],
        breakdowns: tradingBreakdowns({
          time_of_day: [
            breakdownRow('09:15-10:00', {
              ...nullRatios,
              wins: 0,
              losses: 0,
              trades: 2,
              trading_days: 1,
              sample_ok: false,
            }),
          ],
        }),
      })
    )
    renderIt()
    await screen.findByText('Win rate (net)')
    for (const label of [
      'Win rate (net)',
      'Profit factor (net)',
      'Avg win (net)',
      'Avg loss (net)',
      'Expectancy (net)',
    ]) {
      expect(cardValue(label)).toBe('—')
    }
    expect(screen.getByText('no completed trade has a net P&L')).toBeInTheDocument()
    expect(screen.queryByText('0.0%')).not.toBeInTheDocument()
    expect(screen.queryByText('0.00')).not.toBeInTheDocument()
    await openAdvanced()
    const bySymbol = (await screen.findByText('By symbol')).parentElement as HTMLElement
    expect(within(bySymbol).getAllByText('—').length).toBeGreaterThan(0)
  })
})
