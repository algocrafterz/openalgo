import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { TradeRow, TradesPage } from '@/api/backtestResults'
import { TooltipProvider } from '@/components/ui/tooltip'
import { TradesDrilldown } from './TradesDrilldown'

const mocks = vi.hoisted(() => ({ trades: vi.fn(), lineage: vi.fn() }))

vi.mock('@/api/backtestResults', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/backtestResults')>()),
  backtestResultsApi: { trades: mocks.trades, lineage: mocks.lineage },
}))

function row(i: number, extra: Partial<TradeRow> = {}): TradeRow {
  return {
    trade_id: `T${i}`,
    symbol: i % 2 ? 'SBIN' : 'INFY',
    side: 'long',
    entry_time: `2024-01-0${i}T09:20:00`,
    exit_time: `2024-01-0${i}T10:05:00`,
    entry_price: 100 + i,
    exit_price: 101 + i,
    exit_reason: 'target',
    gross_r: 1,
    net_r: 0.8,
    gross_bps: 20,
    net_bps: 15.5,
    cost_bps: 4.5,
    cost_brokerage_bps: 1.25,
    ...extra,
  }
}

function page(rows: TradeRow[], over: Partial<TradesPage> = {}) {
  return {
    status: 'success',
    page: { total: rows.length, limit: 50, offset: 0, subset: 'all', rows, ...over },
  }
}

const LINEAGE = {
  status: 'success',
  lineage: {
    trade_id: 'T1',
    signal: { signal_id: 'S-1', time: '2024-01-01T09:15:00', price: 99.5 },
    order: { order_id: 'O-1', time: '2024-01-01T09:20:00', status: 'filled' },
    fills: [
      { fill_id: 'F-1', kind: 'entry', time: '2024-01-01T09:20:00', price: 101 },
      { fill_id: 'F-2', kind: 'exit', time: '2024-01-01T10:05:00', price: 102 },
    ],
    position_id: 'P-1',
  },
}

function renderIt(props: Partial<React.ComponentProps<typeof TradesDrilldown>> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <TradesDrilldown ideaId="IDEA-1" runId="run-1" hasTrades hasLedgers {...props} />
      </TooltipProvider>
    </QueryClientProvider>
  )
}

const lastQuery = () => mocks.trades.mock.calls[mocks.trades.mock.calls.length - 1][2]

describe('TradesDrilldown', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.trades.mockResolvedValue(page([row(1), row(2)], { total: 120 }))
    mocks.lineage.mockResolvedValue(LINEAGE)
  })

  it('lists trades with the required columns', async () => {
    renderIt()
    const table = await screen.findByRole('table')
    for (const name of [
      'Trade id',
      'Symbol',
      'Side',
      'Entry time',
      'Exit time',
      'Entry price',
      'Exit price',
      'Exit reason',
      'Net R',
      'Net bps',
      'Cost bps',
    ]) {
      expect(within(table).getByRole('columnheader', { name })).toBeInTheDocument()
    }
    expect(within(table).getByText('T1')).toBeInTheDocument()
    expect(within(table).getByText('INFY')).toBeInTheDocument()
    expect(mocks.trades).toHaveBeenCalledWith('IDEA-1', 'run-1', {
      subset: 'all',
      limit: 50,
      offset: 0,
    })
  })

  it('starts on the initial subset', async () => {
    renderIt({ initialSubset: 'oos' })
    await screen.findByRole('table')
    expect(lastQuery().subset).toBe('oos')
    expect(screen.getByLabelText('Subset')).toHaveValue('oos')
  })

  it('changes subset, symbol, reason and page size through the API query', async () => {
    const user = userEvent.setup()
    renderIt()
    await screen.findByRole('table')
    await user.selectOptions(screen.getByLabelText('Subset'), 'losers')
    await waitFor(() => expect(lastQuery().subset).toBe('losers'))
    await user.type(screen.getByLabelText('Symbol (exact)'), 'sbin')
    await waitFor(() => expect(lastQuery().symbol).toBe('SBIN'))
    await user.type(screen.getByLabelText('Exit reason (exact)'), 'stop')
    await waitFor(() => expect(lastQuery().reason).toBe('stop'))
    await user.selectOptions(screen.getByLabelText('Page size'), '100')
    await waitFor(() => expect(lastQuery().limit).toBe(100))
    expect(lastQuery().offset).toBe(0)
  })

  it('pages forward and back and resets to the first page on a filter change', async () => {
    const user = userEvent.setup()
    renderIt()
    await screen.findByRole('table')
    expect(screen.getByTestId('trades-range')).toHaveTextContent('1-2 of 120')
    expect(screen.getByRole('button', { name: 'Previous page' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Next page' }))
    await waitFor(() => expect(lastQuery().offset).toBe(50))
    await user.click(screen.getByRole('button', { name: 'Previous page' }))
    // Page one is served from the query cache, so no new request is expected.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Previous page' })).toBeDisabled()
    )
    await user.click(screen.getByRole('button', { name: 'Next page' }))
    await waitFor(() => expect(lastQuery().offset).toBe(50))
    await user.selectOptions(screen.getByLabelText('Subset'), 'oos')
    await waitFor(() => expect(lastQuery().offset).toBe(0))
  })

  it('sorts by a header button, toggles direction and exposes aria-sort', async () => {
    const user = userEvent.setup()
    renderIt()
    await screen.findByRole('table')
    const head = screen.getByRole('columnheader', { name: 'Net R' })
    expect(head).toHaveAttribute('aria-sort', 'none')
    await user.click(within(head).getByRole('button', { name: 'Net R' }))
    await waitFor(() => expect(lastQuery()).toMatchObject({ sort: 'net_r', desc: false }))
    expect(screen.getByRole('columnheader', { name: 'Net R' })).toHaveAttribute(
      'aria-sort',
      'ascending'
    )
    await user.click(
      within(screen.getByRole('columnheader', { name: 'Net R' })).getByRole('button')
    )
    await waitFor(() => expect(lastQuery()).toMatchObject({ sort: 'net_r', desc: true }))
    expect(screen.getByRole('columnheader', { name: 'Net R' })).toHaveAttribute(
      'aria-sort',
      'descending'
    )
  })

  it('shows the backend message for an empty page', async () => {
    mocks.trades.mockResolvedValue({
      ...page([], { total: 0 }),
      message: 'No out-of-sample trades in this run',
    })
    renderIt()
    expect(await screen.findByText('No out-of-sample trades in this run')).toBeInTheDocument()
  })

  it('falls back to a default empty message', async () => {
    mocks.trades.mockResolvedValue(page([], { total: 0 }))
    renderIt()
    expect(await screen.findByText('No trades in this subset')).toBeInTheDocument()
  })

  it('explains exact matching when filters yield nothing', async () => {
    const user = userEvent.setup()
    renderIt()
    await screen.findByRole('table')
    mocks.trades.mockResolvedValue(page([], { total: 0 }))
    await user.type(screen.getByLabelText('Symbol (exact)'), 'zzz')
    expect(
      await screen.findByText(
        'No trades match these filters (symbol and exit reason are exact matches)'
      )
    ).toBeInTheDocument()
  })

  it('never shows previous-subset rows as the new subset while loading', async () => {
    const user = userEvent.setup()
    renderIt()
    await screen.findByRole('table')
    let release: (v: unknown) => void = () => {}
    mocks.trades.mockReturnValue(
      new Promise((res) => {
        release = res
      })
    )
    await user.selectOptions(screen.getByLabelText('Subset'), 'losers')
    const busy = await screen.findByTestId('trades-body')
    expect(busy).toHaveAttribute('aria-busy', 'true')
    expect(busy).toHaveTextContent('Loading')
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.queryByText('T1')).not.toBeInTheDocument()
    release(page([row(7)], { subset: 'losers' }))
    expect(await screen.findByText('T7')).toBeInTheDocument()
    expect(screen.getByTestId('trades-body')).toHaveAttribute('aria-busy', 'false')
  })

  it('sends the new filter with offset 0, never the old offset', async () => {
    const user = userEvent.setup()
    renderIt()
    await screen.findByRole('table')
    await user.click(screen.getByRole('button', { name: 'Next page' }))
    await waitFor(() => expect(lastQuery().offset).toBe(50))
    mocks.trades.mockClear()
    await user.selectOptions(screen.getByLabelText('Subset'), 'oos')
    await waitFor(() => expect(mocks.trades).toHaveBeenCalled())
    for (const call of mocks.trades.mock.calls) {
      expect(call[2].offset).toBe(0)
    }
  })

  it('has no row-level click handler and shows a sort indicator', async () => {
    const user = userEvent.setup()
    renderIt()
    await screen.findByRole('table')
    expect(screen.getByTestId('trade-T1').className).not.toMatch(/cursor-pointer/)
    await user.click(screen.getByRole('button', { name: 'Net R' }))
    expect(await screen.findByTestId('sort-indicator')).toHaveTextContent('asc')
  })

  it('focuses the lineage heading on open and returns focus to the trigger on close', async () => {
    const user = userEvent.setup()
    renderIt()
    await screen.findByRole('table')
    const trigger = screen.getByRole('button', { name: 'Open lineage for T1' })
    await user.click(trigger)
    const heading = await screen.findByRole('heading', { name: /Trade T1/ })
    expect(heading).toHaveFocus()
    await user.click(screen.getByRole('button', { name: 'Close lineage' }))
    expect(screen.getByRole('button', { name: 'Open lineage for T1' })).toHaveFocus()
  })

  it('focuses the drill-down heading when it opens', async () => {
    renderIt()
    expect(await screen.findByRole('heading', { name: /Trades/ })).toHaveFocus()
  })

  it('puts fills without a kind or id match in Other fills and ignores non-cost numerics', async () => {
    const user = userEvent.setup()
    mocks.trades.mockResolvedValue(
      page([row(1, { cost_stt_bps: 2.5, holding_bars: 7, gross_r: 1 })])
    )
    mocks.lineage.mockResolvedValue({
      status: 'success',
      lineage: {
        ...LINEAGE.lineage,
        fills: [
          { fill_id: 'F-9', time: 't', price: 1 },
          { fill_id: 'F-2', kind: 'exit', price: 102 },
        ],
      },
    })
    renderIt()
    await screen.findByRole('table')
    await user.click(screen.getByRole('button', { name: 'Open lineage for T1' }))
    const panel = await screen.findByTestId('lineage-panel')
    await within(panel).findByRole('list', { name: 'Lineage chain' })
    expect(within(panel).getByTestId('other-fills')).toHaveTextContent('F-9')
    expect(within(panel).getByTestId('trade-costs')).toHaveTextContent('cost_stt_bps')
    expect(within(panel).getByTestId('trade-costs')).not.toHaveTextContent('holding_bars')
  })

  it('tolerates a lineage with no fills array', async () => {
    const user = userEvent.setup()
    mocks.lineage.mockResolvedValue({
      status: 'success',
      lineage: { trade_id: 'T1', signal: null, order: null, position_id: null },
    })
    renderIt()
    await screen.findByRole('table')
    await user.click(screen.getByRole('button', { name: 'Open lineage for T1' }))
    expect(await screen.findByRole('list', { name: 'Lineage chain' })).toBeInTheDocument()
  })

  it('shows an error when the request fails', async () => {
    mocks.trades.mockRejectedValue(new Error('boom'))
    renderIt()
    expect(await screen.findByRole('alert')).toHaveTextContent(/Could not load trades/)
  })

  it('does not call the API and says so when the run has no trades', () => {
    renderIt({ hasTrades: false })
    expect(screen.getByText('Trades were not recorded for this run')).toBeInTheDocument()
    expect(mocks.trades).not.toHaveBeenCalled()
  })

  it('opens the lineage chain from a keyboard-operable row button', async () => {
    const user = userEvent.setup()
    renderIt()
    await screen.findByRole('table')
    const open = screen.getByRole('button', { name: 'Open lineage for T1' })
    open.focus()
    await user.keyboard('{Enter}')
    const panel = await screen.findByTestId('lineage-panel')
    await waitFor(() => expect(mocks.lineage).toHaveBeenCalledWith('IDEA-1', 'run-1', 'T1'))
    const steps = await within(panel).findAllByRole('listitem')
    expect(steps.map((s) => s.getAttribute('data-step'))).toEqual([
      'signal',
      'order',
      'entry_fill',
      'exit_fill',
      'position',
    ])
    expect(panel).toHaveTextContent('S-1')
    expect(panel).toHaveTextContent('2024-01-01T09:15:00')
    expect(panel).toHaveTextContent('O-1')
    expect(panel).toHaveTextContent('F-1')
    expect(panel).toHaveTextContent('F-2')
    expect(panel).toHaveTextContent('102')
    expect(panel).toHaveTextContent('P-1')
    expect(panel).toHaveTextContent('Exit reason')
    expect(panel).toHaveTextContent('target')
    expect(within(panel).getByTestId('trade-costs')).toHaveTextContent('cost_brokerage_bps')
    expect(within(panel).getByTestId('trade-costs')).toHaveTextContent('1.25')
  })

  it('shows trade fields but no chain when the run has no ledgers', async () => {
    const user = userEvent.setup()
    renderIt({ hasLedgers: false })
    await screen.findByRole('table')
    await user.click(screen.getByRole('button', { name: 'Open lineage for T1' }))
    const panel = await screen.findByTestId('lineage-panel')
    expect(panel).toHaveTextContent('Lineage ledgers were not recorded for this run')
    expect(panel).toHaveTextContent('target')
    expect(panel).toHaveTextContent('101')
    expect(mocks.lineage).not.toHaveBeenCalled()
  })

  it('shows lineage errors and closes the panel', async () => {
    const user = userEvent.setup()
    mocks.lineage.mockRejectedValue(new Error('nope'))
    renderIt()
    await screen.findByRole('table')
    await user.click(screen.getByRole('button', { name: 'Open lineage for T1' }))
    expect(await screen.findByText('Could not load the lineage for this trade')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Close lineage' }))
    expect(screen.queryByTestId('lineage-panel')).not.toBeInTheDocument()
  })

  it('calls onClose', async () => {
    const onClose = vi.fn()
    const user = userEvent.setup()
    renderIt({ onClose })
    await user.click(screen.getByRole('button', { name: 'Close trades' }))
    expect(onClose).toHaveBeenCalled()
  })

  it('shows the subset note as a caption when present and hides it otherwise', async () => {
    mocks.trades.mockResolvedValue(
      page([row(1)], { subset_note: 'entry before oos_start; purge and embargo not applied' })
    )
    renderIt()
    expect(await screen.findByTestId('subset-note')).toHaveTextContent(
      'entry before oos_start; purge and embargo not applied'
    )
  })

  it('omits the subset note when null', async () => {
    mocks.trades.mockResolvedValue(page([row(1)], { subset_note: null }))
    renderIt()
    await screen.findByRole('table')
    expect(screen.queryByTestId('subset-note')).not.toBeInTheDocument()
  })

  it('lists missing lineage parts and does not present the chain as complete', async () => {
    const user = userEvent.setup()
    mocks.lineage.mockResolvedValue({
      status: 'success',
      lineage: { ...LINEAGE.lineage, missing: ['order', 'exit fill'] },
    })
    renderIt()
    await screen.findByRole('table')
    await user.click(screen.getByRole('button', { name: 'Open lineage for T1' }))
    const note = await screen.findByTestId('lineage-missing')
    expect(note).toHaveTextContent('Not available for this trade: order, exit fill')
  })

  it('shows no missing note for a complete chain', async () => {
    const user = userEvent.setup()
    mocks.lineage.mockResolvedValue({
      status: 'success',
      lineage: { ...LINEAGE.lineage, missing: [] },
    })
    renderIt()
    await screen.findByRole('table')
    await user.click(screen.getByRole('button', { name: 'Open lineage for T1' }))
    await screen.findByRole('list', { name: 'Lineage chain' })
    expect(screen.queryByTestId('lineage-missing')).not.toBeInTheDocument()
  })
})
