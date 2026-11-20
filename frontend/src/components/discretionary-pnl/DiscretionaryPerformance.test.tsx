import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DiscretionaryApiError } from '@/api/discretionaryPnl'
import {
  breakdownRow,
  consistency,
  dataQuality,
  emptySummary,
  emptyToday,
  position,
  provisional,
  summary,
  todayClosed,
  trade,
  tradingBreakdowns,
} from '@/test/discretionaryFixtures'
import { act, render, screen, userEvent, waitFor, within } from '@/test/test-utils'

const summaryFn = vi.fn()
const fillsFn = vi.fn()
const unresolvedFn = vi.fn()

vi.mock('@/api/discretionaryPnl', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/discretionaryPnl')>()
  return {
    ...actual,
    discretionaryPnlApi: {
      status: vi.fn(),
      summary: (...a: unknown[]) => summaryFn(...a),
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
vi.mock('@/components/strategy-pnl/EquityDrawdownChart', () => ({
  EquityDrawdownChart: ({ equityCurve }: { equityCurve: unknown[] }) => (
    <div data-testid="equity-chart">{equityCurve.length} points</div>
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

beforeEach(() => {
  vi.clearAllMocks()
  summaryFn.mockResolvedValue(summary())
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

  it('says every execution awaits attribution, with the counts, and opens the unresolved list', async () => {
    summaryFn.mockResolvedValue({
      ...emptySummary(),
      executions_collected: 5,
      executions_unresolved: 5,
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
        'No attributable trades available for performance analysis yet. 5 executions collected; 5 awaiting attribution. Their P&L is excluded until classification is resolved.'
      )
    ).toBeInTheDocument()
    expect(screen.queryByText('No discretionary trades yet')).not.toBeInTheDocument()
    expect(screen.queryByText(/ledger sync has to be running/i)).not.toBeInTheDocument()
    expect(screen.queryByText('Net realized (after est. charges)')).not.toBeInTheDocument()
    // the unresolved list is shown open, with its count and rows; the warning stays
    const trigger = screen.getByRole('button', { name: 'Unresolved fills' })
    await waitFor(() => expect(trigger).toHaveTextContent('(5)'))
    expect(await screen.findByText('TCS')).toBeInTheDocument()
    expect(screen.getByText(/could not be reliably classified are excluded/)).toBeInTheDocument()
    expect(screen.getByTestId('discretionary-warnings')).toHaveTextContent(
      '5 unresolved fill(s) are excluded'
    )
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
        'No attributable trades available for performance analysis yet. 7 executions collected; 2 awaiting attribution. Their P&L is excluded until classification is resolved.'
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
    expect(screen.getByRole('button', { name: 'Unresolved fills' })).toBeInTheDocument()
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

  it('withholds Sharpe, Sortino and Return % with the reason', async () => {
    renderIt()
    await screen.findByText('Sharpe')
    expect(screen.getAllByText('--')).toHaveLength(3)
    // the reason is written once, under the cards, not on every card as well
    expect(screen.queryByText('Not shown: no capital basis')).not.toBeInTheDocument()
    expect(
      screen.getAllByText('Sharpe, Sortino, Return %: not shown: no capital basis')
    ).toHaveLength(1)
    expect(screen.queryByText(/realized_gross|net_of_charges/)).not.toBeInTheDocument()
    expect(screen.getByText(/^Net = gross realized minus estimated charges/)).toBeInTheDocument()
  })

  it('labels the drawdown basis and the equity curve caption', async () => {
    renderIt()
    await screen.findByText('Max drawdown')
    expect(
      screen.getByText('basis: gross realized, excludes charges and unrealized')
    ).toBeInTheDocument()
    expect(screen.getByTestId('equity-chart')).toHaveTextContent('2 points')
    expect(
      screen.getByText('Gross realized P&L, excludes charges and unrealized')
    ).toBeInTheDocument()
  })

  it('shows win rate as a percent and the profit factor', async () => {
    renderIt()
    await screen.findByText('Win rate', { selector: 'div' })
    expect(cardValue('Win rate')).toBe('50.0%')
    expect(cardValue('Profit factor')).toBe('2.50')
  })

  it('switches the period table between daily, weekly and monthly', async () => {
    renderIt()
    await screen.findByRole('tab', { name: 'Daily' })
    expect(screen.getByText('incomplete')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('tab', { name: 'Weekly' }))
    expect(await screen.findByText('2026-W47')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('tab', { name: 'Monthly' }))
    expect(await screen.findAllByText('2026-11')).not.toHaveLength(0)
  })

  it('shows the breakdown tables and the holding durations', async () => {
    renderIt()
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

  it('marks closed trades as estimated or charges unavailable, with status Closed', async () => {
    renderIt()
    const table = (await screen.findByText('Closed trades')).closest(
      '[data-slot="card"]'
    ) as HTMLElement
    expect(within(table).getByText('estimated')).toBeInTheDocument()
    expect(within(table).getByText('charges unavailable')).toBeInTheDocument()
    expect(within(table).getAllByText('Closed')).toHaveLength(2)
    expect(within(table).getByText('Rs 12.50')).toBeInTheDocument()
    expect(within(table).getByText('Rs 87.50')).toBeInTheDocument()
  })

  it('notes a truncated trade list', async () => {
    summaryFn.mockResolvedValue(summary({ trades_total: 900, trades_truncated: 898 }))
    renderIt()
    expect(await screen.findByText(/Showing the newest 2 of 900 trades/)).toBeInTheDocument()
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
    expect(summaryFn).toHaveBeenLastCalledWith('30d', undefined, {}, false)
    await userEvent.click(screen.getByRole('switch', { name: 'Fetch live prices' }))
    await waitFor(() => expect(summaryFn).toHaveBeenLastCalledWith('30d', undefined, {}, true))
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
        false
      )
    )
    expect(unresolvedFn).toHaveBeenLastCalledWith({
      broker: undefined,
      account: undefined,
      symbol: 'TCS',
    })
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
        false
      )
    )
  })

  it('clears the filters', async () => {
    renderIt()
    await screen.findByText('Closed trades')
    await userEvent.type(screen.getByLabelText('Symbol'), 'TCS{Enter}')
    await userEvent.click(await screen.findByRole('button', { name: 'Clear filters' }))
    await waitFor(() => expect(summaryFn).toHaveBeenLastCalledWith('30d', undefined, {}, false))
    expect(screen.getByLabelText('Symbol')).toHaveValue('')
  })

  it('passes a custom range through', async () => {
    renderIt({ period: 'custom', range: { start: '2026-11-01', end: '2026-11-10' } })
    await screen.findByText('Closed trades')
    expect(summaryFn).toHaveBeenLastCalledWith(
      'custom',
      { start: '2026-11-01', end: '2026-11-10' },
      {},
      false
    )
  })
})

describe('unresolved fills', () => {
  it('is collapsed with a count, and explains the exclusion when opened', async () => {
    unresolvedFn.mockResolvedValue({
      unresolved: [
        {
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
        },
      ],
      unresolved_count: 1,
      orphans: [{ id: 1, symbol: 'ORPH', reason: 'opening_not_in_ledger', qty: 1 }],
      orphans_count: 1,
    })
    renderIt()
    const trigger = await screen.findByRole('button', { name: 'Unresolved fills' })
    await waitFor(() => expect(trigger).toHaveTextContent('(2)'))
    expect(screen.queryByText('WIPRO')).not.toBeInTheDocument()
    await userEvent.click(trigger)
    expect(
      await screen.findByText(/could not be reliably classified are excluded from P&L/)
    ).toBeInTheDocument()
    expect(screen.getByText('WIPRO')).toBeInTheDocument()
    expect(screen.getByText('ORPH')).toBeInTheDocument()
    expect(screen.queryByText('opening_not_in_ledger')).not.toBeInTheDocument()
    expect(
      screen.getAllByText('The fill that opened this position is not in the ledger').length
    ).toBeGreaterThan(0)
    expect(
      screen.getByText('Not yet attributed to a strategy or to you, so it is left out of P&L')
    ).toBeInTheDocument()
  })

  it('says so when there are none', async () => {
    renderIt()
    const trigger = await screen.findByRole('button', { name: 'Unresolved fills' })
    await userEvent.click(trigger)
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
    const row = await screen.findByRole('button', { name: /Show fills of RELIANCE/ })
    await userEvent.click(row)
    const dialog = await screen.findByRole('dialog')
    expect(await within(dialog).findByText('ORD-ENTRY')).toBeInTheDocument()
    expect(within(dialog).getByText('ORD-EXIT')).toBeInTheDocument()
    expect(within(dialog).getByText('Entry')).toBeInTheDocument()
    expect(within(dialog).getByText('Exit')).toBeInTheDocument()
    expect(fillsFn).toHaveBeenCalledWith(11, 'b1')
  })

  it('shows the error when the fills cannot be read', async () => {
    fillsFn.mockRejectedValue(new DiscretionaryApiError('Trade not found.', 'trade_not_found', 404))
    renderIt()
    await userEvent.click(await screen.findByRole('button', { name: /Show fills of RELIANCE/ }))
    const dialog = await screen.findByRole('dialog')
    expect(await within(dialog).findByText('Trade not found.')).toBeInTheDocument()
  })

  it('shows a 500 as a destructive alert with a retry that asks again', async () => {
    fillsFn.mockRejectedValueOnce(
      new DiscretionaryApiError('Could not read the trade fills.', undefined, 500)
    )
    renderIt()
    await userEvent.click(await screen.findByRole('button', { name: /Show fills of RELIANCE/ }))
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
    await userEvent.click(await screen.findByRole('button', { name: /Show fills of RELIANCE/ }))
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
    await userEvent.click(await screen.findByRole('button', { name: /Show fills of RELIANCE/ }))
    await screen.findByRole('dialog')
    await waitFor(() => expect(fillsFn).toHaveBeenCalledTimes(1))
    await userEvent.keyboard('{Escape}')
    summaryFn.mockResolvedValue(summary({ build_id: 'b2' }))
    await userEvent.click(screen.getByRole('switch', { name: 'Fetch live prices' }))
    await waitFor(() => expect(screen.queryByText(/updating/)).not.toBeInTheDocument())
    await userEvent.click(await screen.findByRole('button', { name: /Show fills of RELIANCE/ }))
    await waitFor(() => expect(fillsFn).toHaveBeenLastCalledWith(11, 'b2'))
    expect(fillsFn).toHaveBeenCalledTimes(2)
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

describe('unresolved list errors', () => {
  it('shows an error indicator instead of (0) when the query fails, and retries', async () => {
    unresolvedFn.mockRejectedValueOnce(
      new DiscretionaryApiError('Could not read the fills.', undefined, 500)
    )
    renderIt()
    const trigger = await screen.findByRole('button', { name: 'Unresolved fills' })
    expect(await within(trigger).findByTestId('unresolved-error-indicator')).toBeInTheDocument()
    expect(trigger).not.toHaveTextContent('(0)')
    await userEvent.click(trigger)
    const alert = await screen.findByText('Could not read the fills.')
    expect(alert.closest('[data-slot="alert"]')).toHaveClass('text-destructive')
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(trigger).toHaveTextContent('(0)'))
    expect(within(trigger).queryByTestId('unresolved-error-indicator')).not.toBeInTheDocument()
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
  it('labels the per-trade averages as gross', async () => {
    renderIt()
    await screen.findByText('Closed trades')
    expect(screen.getByText('Avg win (gross)')).toBeInTheDocument()
    expect(screen.getByText('Avg loss (gross)')).toBeInTheDocument()
    expect(screen.getByText('Gross profit')).toBeInTheDocument()
  })

  it('shows an Account column in the closed trades and open positions tables', async () => {
    renderIt()
    const closed = (await screen.findByText('Closed trades')).closest(
      '[data-slot="card"]'
    ) as HTMLElement
    expect(within(closed).getByRole('columnheader', { name: 'Account' })).toBeInTheDocument()
    expect(within(closed).getByText('ZR1')).toBeInTheDocument()
    const open = screen.getByText('Open positions').closest('[data-slot="card"]') as HTMLElement
    expect(within(open).getByRole('columnheader', { name: 'Account' })).toBeInTheDocument()
    expect(within(open).getByText(position().account)).toBeInTheDocument()
  })

  it('keeps every breakdown column reachable: one column layout, scrollable tables', async () => {
    renderIt()
    const bySymbol = (await screen.findByText('By symbol')).parentElement as HTMLElement
    for (const name of ['Gross P&L', 'Net of charges', 'Trades']) {
      expect(within(bySymbol).getByRole('columnheader', { name })).toBeInTheDocument()
    }
    expect(bySymbol).toHaveClass('min-w-0', 'overflow-x-auto')
    expect(bySymbol.parentElement).toHaveClass('grid-cols-1')
    expect(bySymbol.parentElement?.className).not.toMatch(/(^|\s)lg:grid-cols-2/)
  })

  it('lets a keyboard or screen reader user open a trade: rows are buttons', async () => {
    renderIt()
    const row = await screen.findByRole('button', { name: /Show fills of RELIANCE/ })
    expect(row.tagName).toBe('TR')
    row.focus()
    await userEvent.keyboard('{Enter}')
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
  })
})

describe('completed trades (a position from flat to flat)', () => {
  it('shows a legs badge on a trade closed in parts', async () => {
    summaryFn.mockResolvedValue(
      summary({
        trades: [trade({ legs: 2, closed_quantity: 45 }), trade({ id: 12, symbol: 'INFY' })],
      })
    )
    renderIt()
    const row = await screen.findByRole('button', { name: /Show fills of RELIANCE/ })
    expect(within(row).getByText('2 legs')).toBeInTheDocument()
    expect(screen.getAllByText(/legs$/)).toHaveLength(1) // a one-leg trade has no badge
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
      'Win rate',
      'Profit factor',
      'Avg win (gross)',
      'Avg loss (gross)',
      'Expectancy',
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
    await userEvent.click(await screen.findByRole('button', { name: /Show fills of RELIANCE/ }))
    const dialog = await screen.findByRole('dialog')
    expect(await within(dialog).findByText('ORD-1')).toBeInTheDocument()
    expect(within(dialog).getByText('ORD-2')).toBeInTheDocument()
    expect(within(dialog).getByText('ORD-3')).toBeInTheDocument()
    expect(within(dialog).getAllByText('Exit')).toHaveLength(2)
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
    // the section comes before the period cards
    const cards = screen.getByText('Net realized (after est. charges)')
    expect(section.compareDocumentPosition(cards) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
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
    summaryFn.mockResolvedValue(full())
    renderIt()
    const dq = await screen.findByTestId('data-quality')
    const risk = screen.getByTestId('risk-strip')
    const how = screen.getByTestId('how-you-trade')
    expect(screen.getByTestId('daily-pnl-chart')).toBeInTheDocument()
    expect(screen.getByTestId('discretionary-sample-note')).toHaveTextContent(
      'Ratios based on 2 completed trade(s) over 1 trading day(s).'
    )
    // data quality near the top, then the risk strip, then How you trade
    expect(dq.compareDocumentPosition(risk) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(risk.compareDocumentPosition(how) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
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

  it('shows a dash with the reason on the drawdown card for a single trading day', async () => {
    summaryFn.mockResolvedValue(
      summary({
        consistency: consistency({
          max_drawdown: null,
          max_drawdown_unavailable_reason: 'needs at least 2 trading days',
        }),
      })
    )
    renderIt()
    // the metric card comes first; the risk strip below has its own Max drawdown tile
    const [label] = await screen.findAllByText('Max drawdown', { selector: 'div' })
    expect(label.parentElement?.querySelector('.font-mono')?.textContent).toBe('—')
    expect(screen.getByTestId('risk-drawdown')).toHaveTextContent('needs at least 2 trading days')
    expect(
      screen.getByText(
        'needs at least 2 trading days; basis: gross realized, excludes charges and unrealized'
      )
    ).toBeInTheDocument()
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

  it('keeps the awaiting message and adds the provisional TCS panel when only unresolved fills exist', async () => {
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
      provisional: provisional(),
    })
    renderIt()
    const block = await screen.findByTestId('discretionary-awaiting-attribution')
    expect(block).toHaveTextContent(
      'No attributable trades available for performance analysis yet. 5 executions collected; 5 awaiting attribution. Their P&L is excluded until classification is resolved.'
    )
    const panel = screen.getByTestId('provisional-panel')
    expect(panel).toHaveTextContent(
      'Pending attribution: provisional figures (not included in any total above)'
    )
    expect(within(panel).getByTestId('provisional-row')).toHaveTextContent('Rs -92.65')
    expect(screen.getByTestId('dq-awaiting')).toHaveTextContent('5')
    // nothing provisional reaches the headline: the cards are not rendered at all here
    expect(screen.queryByText('Net realized (after est. charges)')).not.toBeInTheDocument()
  })

  it('keeps provisional figures out of the populated headline cards', async () => {
    summaryFn.mockResolvedValue({ ...full(), provisional: provisional() })
    renderIt()
    await screen.findByTestId('provisional-panel')
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

function tcsFill(fill_ref: number, role: 'ENTRY' | 'EXIT', minute: number) {
  return {
    fill_ref,
    role,
    qty_used: role === 'ENTRY' ? 20 : 15,
    charge_allocated: 1.5,
    price: role === 'ENTRY' ? 3000 + fill_ref : 3010 + fill_ref,
    fill_time: `2026-11-19 ${role === 'ENTRY' ? '09' : '11'}:${String(minute).padStart(2, '0')}:00`,
    order_id: `ORD-${role}-${fill_ref}`,
    fill_id: `f${fill_ref}`,
    broker: 'flattrade',
    account: 'FT1',
    symbol: 'TCS',
    exchange: 'NSE',
    product: 'MIS',
    side: role === 'ENTRY' ? 'BUY' : 'SELL',
    attribution_state: 'manual',
  }
}

const TCS_FILLS = [
  tcsFill(1, 'ENTRY', 20),
  tcsFill(2, 'ENTRY', 21),
  tcsFill(3, 'ENTRY', 40),
  tcsFill(4, 'EXIT', 10),
  tcsFill(5, 'EXIT', 20),
  tcsFill(6, 'EXIT', 30),
  tcsFill(7, 'EXIT', 40),
]

describe('UI review: header, heading, reasons, expandable closed trades', () => {
  it('states the nominal period and the clamp to the start of ledger history', async () => {
    summaryFn.mockResolvedValue(
      summary({
        period: {
          period: '7d',
          start: '2026-11-18',
          end: '2026-11-19',
          reliable_since: '2026-11-18',
        },
      })
    )
    renderIt({ period: '7d' })
    const header = await screen.findByTestId('discretionary-period-header')
    expect(header).toHaveTextContent(
      'Last 7 days: 13-19 Nov 2026. Showing 18-19 Nov: ledger history starts 18 Nov.'
    )
  })

  it('heads the metric cards with the selected range and the closed-position count', async () => {
    summaryFn.mockResolvedValue(
      summary({
        sample: {
          completed_trades: 3,
          trading_days: 2,
          min_trades: 30,
          min_days: 10,
          sufficient: false,
        },
      })
    )
    renderIt()
    const heading = await screen.findByTestId('selected-period-heading')
    expect(
      within(heading).getByRole('heading', { name: 'Selected Period Performance' })
    ).toBeInTheDocument()
    expect(heading).toHaveTextContent('19 Oct - 17 Nov 2026 · 3 closed positions')
  })

  it('falls back to the metrics count for the heading on an older server', async () => {
    renderIt()
    expect(await screen.findByTestId('selected-period-heading')).toHaveTextContent(
      '2 closed positions'
    )
  })

  it('shows the plain-language reason of each unresolved fill', async () => {
    unresolvedFn.mockResolvedValue({
      unresolved: [
        {
          id: 1,
          symbol: 'TCS',
          order_id: 'O-1',
          attribution_state: 'unresolved',
          reason: 'before_coverage',
          coverage_from: '2026-11-19',
        },
        { id: 2, symbol: 'INFY', attribution_state: 'unresolved', reason: 'within_grace' },
        { id: 3, symbol: 'SBIN', attribution_state: 'unresolved' },
      ],
      unresolved_count: 3,
      orphans: [],
      orphans_count: 0,
    })
    renderIt()
    await userEvent.click(await screen.findByRole('button', { name: 'Unresolved fills' }))
    expect(
      await screen.findByText('Fill dated before attribution coverage began (19 Nov)')
    ).toBeInTheDocument()
    expect(screen.getByText(/Inside the safety window after the fill/)).toBeInTheDocument()
    // an older server sends no reason: the state text stays
    expect(
      screen.getByText('Not yet attributed to a strategy or to you, so it is left out of P&L')
    ).toBeInTheDocument()
  })

  it('shows entry and exit fill counts from the server and the holding duration', async () => {
    summaryFn.mockResolvedValue(
      summary({
        trades: [
          trade({ symbol: 'TCS', entry_fills: 3, exit_fills: 4, legs: 4, hold_minutes: 140 }),
        ],
      })
    )
    renderIt()
    const row = await screen.findByRole('button', { name: /Show fills of TCS/ })
    expect(within(row).getByTestId('entry-fills')).toHaveTextContent('3')
    expect(within(row).getByTestId('exit-fills')).toHaveTextContent('4')
    expect(row).toHaveTextContent('2h 20m')
    expect(fillsFn).not.toHaveBeenCalled()
  })

  it('expands a row in place with its fills, fetched once, and collapses it', async () => {
    summaryFn.mockResolvedValue(summary({ trades: [trade({ symbol: 'TCS', legs: 4 })] }))
    fillsFn.mockResolvedValue({ build_id: 'b1', trade: trade(), fills: TCS_FILLS, linked_fills: 7 })
    renderIt()
    const row = await screen.findByRole('button', { name: /Show fills of TCS/ })
    // an older server sends no counts: a dash until expanded
    expect(within(row).getByTestId('entry-fills')).toHaveTextContent('—')
    const chevron = screen.getByRole('button', { name: /Show entry and exit fills of TCS/ })
    expect(chevron).toHaveAttribute('aria-expanded', 'false')
    await userEvent.click(chevron)
    expect(chevron).toHaveAttribute('aria-expanded', 'true')
    const detail = await screen.findByTestId('closed-trade-fills')
    expect(await within(detail).findByText('ORD-ENTRY-1')).toBeInTheDocument()
    expect(within(detail).getByText('ORD-EXIT-7')).toBeInTheDocument()
    expect(within(detail).getAllByText('Entry')).toHaveLength(3)
    expect(within(detail).getAllByText('Exit')).toHaveLength(4)
    expect(within(detail).getAllByText('Rs 1.50')).toHaveLength(7)
    expect(within(row).getByTestId('entry-fills')).toHaveTextContent('3')
    expect(within(row).getByTestId('exit-fills')).toHaveTextContent('4')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(fillsFn).toHaveBeenCalledTimes(1)
    expect(fillsFn).toHaveBeenCalledWith(11, 'b1')
    await userEvent.click(chevron)
    expect(chevron).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTestId('closed-trade-fills')).not.toBeInTheDocument()
    await userEvent.click(chevron)
    expect(await screen.findByTestId('closed-trade-fills')).toBeInTheDocument()
    expect(fillsFn).toHaveBeenCalledTimes(1) // cached: same key as the dialog
  })

  it('expands from the keyboard without opening the dialog', async () => {
    fillsFn.mockResolvedValue({ build_id: 'b1', trade: trade(), fills: TCS_FILLS, linked_fills: 7 })
    renderIt()
    const chevron = await screen.findByRole('button', {
      name: /Show entry and exit fills of RELIANCE/,
    })
    chevron.focus()
    await userEvent.keyboard('{Enter}')
    expect(chevron).toHaveAttribute('aria-expanded', 'true')
    expect(await screen.findByTestId('closed-trade-fills')).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await userEvent.keyboard(' ')
    expect(chevron).toHaveAttribute('aria-expanded', 'false')
  })

  it('shows a changed build inside the expanded area, with Refresh', async () => {
    fillsFn.mockRejectedValue(new DiscretionaryApiError('The data changed', 'build_changed', 409))
    renderIt()
    await userEvent.click(
      await screen.findByRole('button', { name: /Show entry and exit fills of RELIANCE/ })
    )
    const detail = await screen.findByTestId('closed-trade-fills')
    expect(await within(detail).findByText('Data changed')).toBeInTheDocument()
    const calls = summaryFn.mock.calls.length
    await userEvent.click(within(detail).getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(summaryFn.mock.calls.length).toBeGreaterThan(calls))
  })

  it('still opens the dialog from the row and from Details', async () => {
    fillsFn.mockResolvedValue({ build_id: 'b1', trade: trade(), fills: TCS_FILLS, linked_fills: 7 })
    renderIt()
    await userEvent.click(
      await screen.findByRole('button', { name: /Show entry and exit fills of RELIANCE/ })
    )
    const detail = await screen.findByTestId('closed-trade-fills')
    await userEvent.click(await within(detail).findByRole('button', { name: 'Details' }))
    const dialog = await screen.findByRole('dialog')
    expect(await within(dialog).findByText('ORD-ENTRY-1')).toBeInTheDocument()
    expect(fillsFn).toHaveBeenCalledTimes(1) // the dialog reuses the cached answer
  })
})
