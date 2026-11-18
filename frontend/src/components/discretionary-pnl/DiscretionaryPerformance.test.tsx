import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { DiscretionaryApiError } from '@/api/discretionaryPnl'
import { breakdownRow, emptySummary, position, summary, trade } from '@/test/discretionaryFixtures'
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
vi.mock('@/components/strategy-pnl/EquityDrawdownChart', () => ({
  EquityDrawdownChart: ({ equityCurve }: { equityCurve: unknown[] }) => (
    <div data-testid="equity-chart">{equityCurve.length} points</div>
  ),
}))

const { DiscretionaryPerformance } = await import('./DiscretionaryPerformance')

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
