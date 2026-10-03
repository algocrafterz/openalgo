import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { BacktestListResponse, LatestRun } from '@/api/backtestResults'
import { makeCard, makeLatest } from '@/test/backtestResultsFixtures'
import BacktestResults from './BacktestResults'

const mocks = vi.hoisted(() => ({ list: vi.fn() }))

vi.mock('@/api/backtestResults', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/backtestResults')>()),
  backtestResultsApi: { list: mocks.list },
}))

const SUMMARY = {
  total_ideas: 3,
  total_runs: 2,
  ideas_with_runs: 2,
  by_status: {},
  by_verdict: { marginal: 1, 'no-edge': 1, none: 1 },
}

const IDEAS = [
  makeCard({ id: 'IDEA-001', title: 'Alpha breakout', verdict: 'no-edge', status: 'rejected' }),
  makeCard({
    id: 'IDEA-002',
    title: 'Beta idea',
    verdict: 'none',
    status: 'inbox',
    horizon: 'swing',
    run_count: 0,
    latest_run: null,
  }),
  makeCard({
    id: 'IDEA-003',
    title: 'Gamma fade',
    verdict: 'marginal',
    status: 'live',
    latest_run: makeLatest({
      run_id: 'r3',
      date: '2026-10-01',
      metrics: { trades: 500, gross_bps: 12, net_bps: 4.5, t_stat: 2.5, hurdle_t: 1.97 },
      oos_net_bps: 3.2,
      n_trials: 4,
    }),
  }),
]

function renderPage(response: BacktestListResponse | Promise<BacktestListResponse>) {
  mocks.list.mockReturnValue(Promise.resolve(response))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/backtest-results']}>
        <Routes>
          <Route path="/backtest-results" element={<BacktestResults />} />
          <Route path="/backtest-results/:ideaId" element={<div>detail page</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

const ok = (ideas = IDEAS): BacktestListResponse => ({
  status: 'success',
  available: true,
  ideas,
  summary: SUMMARY,
})

const ids = () => screen.getAllByRole('link').map((a) => a.textContent)

describe('BacktestResults leaderboard', () => {
  beforeEach(() => {
    mocks.list.mockReset()
  })

  it('states that results are historical evidence, not predictions', async () => {
    renderPage(ok())
    expect(await screen.findByText(/not predictions/i)).toBeInTheDocument()
  })

  it('shows loading skeletons before data arrives', () => {
    mocks.list.mockImplementation(() => new Promise(() => {}))
    const client = new QueryClient()
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <BacktestResults />
        </MemoryRouter>
      </QueryClientProvider>
    )
    expect(screen.getByTestId('loading')).toBeInTheDocument()
  })

  it('orders by evidence by default: runs first, then verdict, not by return', async () => {
    renderPage(ok())
    await screen.findByText('IDEA-003')
    expect(ids()).toEqual(['IDEA-003', 'IDEA-001', 'IDEA-002'])
  })

  it('shows summary tiles', async () => {
    renderPage(ok())
    const tiles = await screen.findByTestId('summary-tiles')
    expect(within(tiles).getByText('Total runs').nextSibling).toHaveTextContent('2')
    expect(within(tiles).getByText('Marginal').nextSibling).toHaveTextContent('1')
  })

  it('marks an idea without runs as No run recorded', async () => {
    renderPage(ok())
    const row = (await screen.findByText('IDEA-002')).closest('tr') as HTMLElement
    expect(within(row).getByText('No run recorded')).toBeInTheDocument()
  })

  it('renders n/a, never zero, for a run with no trades', async () => {
    const empty = makeCard({
      id: 'IDEA-009',
      latest_run: makeLatest({
        metrics: { trades: 0, gross_bps: 0, net_bps: 0 },
        oos_net_bps: null,
      }),
    })
    renderPage(ok([empty]))
    const row = (await screen.findByText('IDEA-009')).closest('tr') as HTMLElement
    expect(within(row).getAllByText('n/a').length).toBeGreaterThanOrEqual(4)
    expect(within(row).queryByText('0.00')).toBeNull()
    expect(within(row).queryByText('+0.00')).toBeNull()
  })

  it('shows signed net bps and t against its hurdle', async () => {
    renderPage(ok())
    const row = (await screen.findByText('IDEA-003')).closest('tr') as HTMLElement
    expect(within(row).getByText('+4.50')).toHaveClass('text-green-600')
    expect(within(row).getByText('2.50 vs 2.50')).toBeInTheDocument()
    const negative = (await screen.findByText('IDEA-001')).closest('tr') as HTMLElement
    expect(within(negative).getByText('-1.30')).toHaveClass('text-red-600')
  })

  it('filters by verdict', async () => {
    renderPage(ok())
    await screen.findByText('IDEA-001')
    await userEvent.selectOptions(screen.getByLabelText('Verdict'), 'marginal')
    expect(ids()).toEqual(['IDEA-003'])
  })

  it('filters by status and horizon', async () => {
    renderPage(ok())
    await screen.findByText('IDEA-001')
    await userEvent.selectOptions(screen.getByLabelText('Status'), 'inbox')
    expect(ids()).toEqual(['IDEA-002'])
    await userEvent.selectOptions(screen.getByLabelText('Status'), 'all')
    await userEvent.selectOptions(screen.getByLabelText('Horizon'), 'swing')
    expect(ids()).toEqual(['IDEA-002'])
  })

  it('searches by title and shows a message when nothing matches', async () => {
    renderPage(ok())
    await screen.findByText('IDEA-001')
    await userEvent.type(screen.getByLabelText('Search'), 'gamma')
    expect(ids()).toEqual(['IDEA-003'])
    await userEvent.clear(screen.getByLabelText('Search'))
    await userEvent.type(screen.getByLabelText('Search'), 'no-such-thing')
    expect(screen.getByText('No ideas match these filters.')).toBeInTheDocument()
  })

  it('sorts by net bps on header click and resets to evidence order', async () => {
    renderPage(ok())
    await screen.findByText('IDEA-001')
    await userEvent.click(screen.getByRole('button', { name: /^Net bps/ }))
    expect(ids()).toEqual(['IDEA-003', 'IDEA-001', 'IDEA-002'])
    expect(screen.getByRole('columnheader', { name: /Net bps/ })).toHaveAttribute(
      'aria-sort',
      'descending'
    )
    await userEvent.click(screen.getByRole('button', { name: /^Net bps/ }))
    expect(ids()).toEqual(['IDEA-001', 'IDEA-003', 'IDEA-002'])
    await userEvent.click(screen.getByRole('button', { name: 'Reset to evidence order' }))
    expect(ids()).toEqual(['IDEA-003', 'IDEA-001', 'IDEA-002'])
  })

  it('sorts a text column ascending first', async () => {
    renderPage(ok())
    await screen.findByText('IDEA-001')
    await userEvent.click(screen.getByRole('button', { name: /^Idea/ }))
    expect(ids()).toEqual(['IDEA-001', 'IDEA-002', 'IDEA-003'])
  })

  it('opens the detail page when a row is clicked', async () => {
    renderPage(ok())
    const row = (await screen.findByText('Gamma fade')).closest('tr') as HTMLElement
    await userEvent.click(row)
    await waitFor(() => expect(screen.getByText('detail page')).toBeInTheDocument())
  })

  it('shows the server error message', async () => {
    renderPage({ status: 'error', message: 'Research registry is invalid' })
    expect(await screen.findByRole('alert')).toHaveTextContent('Research registry is invalid')
  })

  it('says so when research data is not available', async () => {
    renderPage({ status: 'success', available: false, ideas: [], summary: undefined })
    expect(await screen.findByText(/not available on this server/i)).toBeInTheDocument()
  })

  it('shows an empty state when no ideas exist', async () => {
    renderPage({ status: 'success', available: true, ideas: [], summary: SUMMARY })
    expect(await screen.findByText('No ideas are registered yet.')).toBeInTheDocument()
  })
})

describe('coverage and reality columns', () => {
  const withReality = makeCard({
    id: 'IDEA-004',
    title: 'Pullback swing',
    latest_run: makeLatest({
      run_id: 'r4',
      years: 7.8,
      data_age_days: 3,
      period: { start: '2019-12-03', end: '2026-09-25' },
      net_bps_cap10: 112.1,
      baseline_excess_bps: -28.8,
    }),
  })

  it('shows how far back each idea was tested and how old the data is', async () => {
    renderPage(ok([withReality]))
    const row = (await screen.findByText('IDEA-004')).closest('tr') as HTMLElement
    expect(within(row).getByText(/7\.8 years/)).toBeInTheDocument()
    expect(within(row).getByText(/2026-09-25/)).toBeInTheDocument()
    expect(within(row).getByText(/3 days old/)).toBeInTheDocument()
  })

  it('shows net at ten slots and the excess over random entry, signed', async () => {
    renderPage(ok([withReality]))
    const row = (await screen.findByText('IDEA-004')).closest('tr') as HTMLElement
    expect(within(row).getByText(/net at 10 slots \+112\.10/)).toBeInTheDocument()
    expect(within(row).getByText('-28.80')).toBeInTheDocument()
  })

  it('renders n/a where the run has no cap or baseline data', async () => {
    renderPage(ok([makeCard({ id: 'IDEA-005', title: 'Plain', latest_run: makeLatest() })]))
    const row = (await screen.findByText('IDEA-005')).closest('tr') as HTMLElement
    expect(within(row).getAllByText('n/a').length).toBeGreaterThanOrEqual(2)
  })

  it('labels the new columns', async () => {
    renderPage(ok([withReality]))
    await screen.findByText('IDEA-004')
    expect(screen.getByText('Why')).toBeInTheDocument()
    expect(screen.getByText('t vs bar')).toBeInTheDocument()
    expect(screen.getByText('vs random entry')).toBeInTheDocument()
  })
})

describe('response from a server that predates the coverage fields', () => {
  // An app server started before the backend gained period, years, data_age_days, net_bps_cap10
  // and baseline_excess_bps still answers with the older shape. The page must degrade to n/a,
  // not crash the whole route.
  function olderShape() {
    const latest = makeLatest() as Partial<LatestRun>
    delete latest.period
    delete latest.data
    delete latest.years
    delete latest.data_age_days
    delete latest.net_bps_cap10
    delete latest.baseline_excess_bps
    return makeCard({ id: 'IDEA-009', title: 'Older shape', latest_run: latest as LatestRun })
  }

  it('renders the row with n/a instead of throwing', async () => {
    renderPage(ok([olderShape()]))
    const row = (await screen.findByText('IDEA-009')).closest('tr') as HTMLElement
    expect(within(row).getAllByText('n/a').length).toBeGreaterThanOrEqual(2)
    expect(screen.queryByText(/something went wrong/i)).not.toBeInTheDocument()
  })

  it('still shows the numbers it does have', async () => {
    renderPage(ok([olderShape()]))
    const row = (await screen.findByText('IDEA-009')).closest('tr') as HTMLElement
    expect(within(row).getByText('1200')).toBeInTheDocument()
  })
})

describe('classification line', () => {
  const CLS = {
    strategy_category: 'Mean Reversion',
    strategy_tags: ['vwap'],
    trading_style: 'Intraday',
    direction: 'Long',
    timeframe: '5m',
    market: 'NSE Equities',
  }

  it('shows category, style, market and timeframe under the strategy name', async () => {
    renderPage(ok([makeCard({ id: 'IDEA-020', title: 'VWAP fade', classification: CLS })]))
    expect(await screen.findByTestId('classification-line')).toHaveTextContent(
      'Mean Reversion · Intraday · NSE Equities · 5m'
    )
  })

  it('omits missing parts', async () => {
    renderPage(
      ok([
        makeCard({
          classification: { ...CLS, trading_style: null, market: null },
        }),
      ])
    )
    expect(await screen.findByTestId('classification-line')).toHaveTextContent(
      /^Mean Reversion · 5m$/
    )
  })

  it('shows nothing when the idea has no classification', async () => {
    renderPage(ok([makeCard({ id: 'IDEA-021' })]))
    await screen.findByText('IDEA-021')
    expect(screen.queryByTestId('classification-line')).toBeNull()
  })
})

describe('BacktestResults verdict column', () => {
  beforeEach(() => {
    mocks.list.mockReset()
  })

  const calc = [
    makeCard({
      id: 'IDEA-020',
      verdict: 'no-edge',
      latest_run: makeLatest({ run_verdict: 'EDGE' }),
    }),
    makeCard({ id: 'IDEA-021', verdict: 'marginal', latest_run: makeLatest() }),
  ]

  it('labels the column as the latest run verdict', async () => {
    renderPage(ok(calc))
    await screen.findByText('IDEA-020')
    expect(screen.getByRole('button', { name: /Verdict \(latest run\)/ })).toBeInTheDocument()
  })

  it('shows the calculated run verdict, falling back to the derived idea verdict', async () => {
    renderPage(ok(calc))
    const withCalc = (await screen.findByText('IDEA-020')).closest('tr') as HTMLElement
    expect(within(withCalc).getByText('edge')).toBeInTheDocument()
    expect(within(withCalc).queryByText('no edge')).toBeNull()
    const legacy = (await screen.findByText('IDEA-021')).closest('tr') as HTMLElement
    expect(within(legacy).getByText('marginal')).toBeInTheDocument()
  })

  it('filters on the shown value', async () => {
    renderPage(ok(calc))
    await screen.findByText('IDEA-020')
    await userEvent.selectOptions(screen.getByLabelText('Verdict'), 'edge')
    expect(ids()).toEqual(['IDEA-020'])
  })
})

const cls = (over: Record<string, string | null> = {}) => ({
  strategy_category: 'Mean Reversion',
  strategy_tags: [],
  trading_style: 'Intraday',
  direction: 'Long',
  timeframe: '5m',
  market: 'NSE Equities',
  ...over,
})

const CLASSIFIED = [
  makeCard({
    id: 'C-1',
    title: 'VWAP fade',
    strategy_category: 'Mean Reversion',
    strategy_tags: ['VWAP'],
    research_state: 'active',
    classification: cls(),
    latest_run: makeLatest({
      date: '2026-10-01',
      metrics: { trades: 100, gross_bps: 1, net_bps: 9, t_stat: 3, hurdle_t: 2 },
    }),
  }),
  makeCard({
    id: 'C-2',
    title: 'Range break',
    strategy_category: 'Breakout',
    strategy_tags: ['Pullback'],
    research_state: 'promising',
    classification: cls({
      strategy_category: 'Breakout',
      trading_style: 'Swing',
      market: 'NSE F&O',
    }),
    latest_run: makeLatest({
      date: '2026-09-01',
      metrics: { trades: 100, gross_bps: 1, net_bps: 2, t_stat: 1, hurdle_t: 2 },
    }),
  }),
  makeCard({
    id: 'C-3',
    title: 'RSI fade',
    strategy_category: 'Mean Reversion',
    strategy_tags: ['vwap'],
    classification: cls(),
    latest_run: makeLatest({
      date: '2026-08-01',
      metrics: { trades: 100, gross_bps: 1, net_bps: 5, t_stat: 2, hurdle_t: 2 },
    }),
  }),
  makeCard({ id: 'C-4', title: 'Plain idea', latest_run: null, run_count: 0 }),
]

describe('BacktestResults classification filters', () => {
  beforeEach(() => mocks.list.mockReset())

  it('shows facet options with counts and filters by category', async () => {
    renderPage(ok(CLASSIFIED))
    await screen.findByText('C-1')
    const select = screen.getByLabelText('Category')
    expect(within(select).getByRole('option', { name: 'Mean Reversion (2)' })).toBeInTheDocument()
    expect(within(select).getByRole('option', { name: 'Other (1)' })).toBeInTheDocument()
    await userEvent.selectOptions(select, 'Other')
    expect(ids()).toEqual(['C-4'])
  })

  it('filters by style and market', async () => {
    renderPage(ok(CLASSIFIED))
    await screen.findByText('C-1')
    await userEvent.selectOptions(screen.getByLabelText('Trading style'), 'Swing')
    expect(ids()).toEqual(['C-2'])
    await userEvent.selectOptions(screen.getByLabelText('Trading style'), 'all')
    await userEvent.selectOptions(screen.getByLabelText('Market'), 'NSE Equities')
    expect(ids()).toEqual(['C-1', 'C-3'])
  })

  it('tags need all selected', async () => {
    renderPage(ok(CLASSIFIED))
    await screen.findByText('C-1')
    await userEvent.click(screen.getByRole('button', { name: 'VWAP (2)' }))
    expect(ids()).toEqual(['C-1', 'C-3'])
    await userEvent.click(screen.getByRole('button', { name: 'Pullback (0)' }))
    expect(screen.getByText('No ideas match these filters.')).toBeInTheDocument()
    expect(screen.getByText('Has all selected tags')).toBeInTheDocument()
  })

  it('search matches category and combines with filters, shows count and clears', async () => {
    renderPage(ok(CLASSIFIED))
    await screen.findByText('C-1')
    await userEvent.type(screen.getByLabelText('Search'), 'mean rev')
    expect(ids()).toEqual(['C-1', 'C-3'])
    expect(screen.getByTestId('results-count')).toHaveTextContent('2 of 4 strategies')
    await userEvent.selectOptions(screen.getByLabelText('Research state'), 'active')
    expect(ids()).toEqual(['C-1'])
    await userEvent.click(screen.getByRole('button', { name: 'Clear search' }))
    expect(screen.getByLabelText('Search')).toHaveValue('')
    expect(ids()).toEqual(['C-1'])
  })

  it('restores filters from the URL', async () => {
    mocks.list.mockReturnValue(Promise.resolve(ok(CLASSIFIED)))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/backtest-results?cat=Breakout&tag=Pullback']}>
          <Routes>
            <Route path="/backtest-results" element={<BacktestResults />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    )
    await screen.findByText('C-2')
    expect(ids()).toEqual(['C-2'])
    expect(screen.getByLabelText('Category')).toHaveValue('Breakout')
  })

  it('shows classification chips on the row', async () => {
    renderPage(ok(CLASSIFIED))
    const row = (await screen.findByText('C-2')).closest('tr') as HTMLElement
    const chips = within(row).getByTestId('classification-chips')
    expect(chips).toHaveTextContent('Breakout')
    expect(chips).toHaveTextContent('Swing')
    expect(chips).toHaveTextContent('NSE F&O')
    expect(chips).toHaveTextContent('5m')
  })

  it('groups by category with headings and keeps sort within groups', async () => {
    renderPage(ok(CLASSIFIED))
    await screen.findByText('C-1')
    await userEvent.click(screen.getByLabelText('Group by category'))
    const headings = screen.getAllByTestId('category-heading').map((h) => h.textContent)
    expect(headings).toEqual(['Breakout (1)', 'Mean Reversion (2)', 'Other (1)'])
    expect(ids()).toEqual(['C-2', 'C-1', 'C-3', 'C-4'])
  })
})

function renderAt(url: string, ideas = CLASSIFIED) {
  mocks.list.mockReturnValue(Promise.resolve(ok(ideas)))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const Where = () => <div data-testid="search">{useLocation().search}</div>
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[url]}>
        <Routes>
          <Route
            path="/backtest-results"
            element={
              <>
                <BacktestResults />
                <Where />
              </>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

describe('BacktestResults stale url filters and counts', () => {
  beforeEach(() => mocks.list.mockReset())

  it('ignores an unknown category in the URL and shows every idea with All', async () => {
    renderAt('/backtest-results?cat=Bogus&tag=NoSuchTag')
    await screen.findByText('C-1')
    expect(ids()).toHaveLength(4)
    expect(screen.getByLabelText('Category')).toHaveValue('all')
    expect(screen.queryByTestId('active-filters')).toBeNull()
  })

  it('a valid URL value filters, and an active filters summary clears everything', async () => {
    renderAt('/backtest-results?cat=Breakout')
    await screen.findByText('C-2')
    expect(ids()).toEqual(['C-2'])
    const summary = screen.getByTestId('active-filters')
    expect(summary).toHaveTextContent('Breakout')
    await userEvent.click(within(summary).getByRole('button', { name: 'Clear all filters' }))
    expect(ids()).toHaveLength(4)
    expect(screen.getByLabelText('Category')).toHaveValue('all')
    expect(screen.queryByTestId('active-filters')).toBeNull()
  })

  it('facet and tag counts follow the other filters and equal the resulting rows', async () => {
    renderAt('/backtest-results')
    await screen.findByText('C-1')
    await userEvent.selectOptions(screen.getByLabelText('Trading style'), 'Swing')
    const market = screen.getByLabelText('Market')
    const option = within(market).getByRole('option', { name: /^NSE F&O/ })
    const shown = Number(/\((\d+)\)/.exec(option.textContent ?? '')?.[1])
    await userEvent.selectOptions(market, 'NSE F&O')
    expect(ids()).toHaveLength(shown)
    expect(screen.getByTestId('results-count')).toHaveTextContent(`${shown} of 4`)
    expect(within(market).getByRole('option', { name: 'NSE Equities (0)' })).toBeInTheDocument()
  })

  it('a tag containing a comma round trips through the URL', async () => {
    const tagged = [makeCard({ id: 'T-1', title: 'Comma tag', strategy_tags: ['a,b'] })]
    renderAt('/backtest-results?tag=a%2Cb', tagged)
    await screen.findByText('T-1')
    expect(screen.getByRole('button', { name: 'a,b (1)' })).toHaveAttribute('aria-pressed', 'true')
    expect(ids()).toEqual(['T-1'])
  })

  it('filters the list at once but writes the search to the URL after a pause', async () => {
    renderAt('/backtest-results')
    await screen.findByText('C-1')
    await userEvent.type(screen.getByLabelText('Search'), 'mean')
    expect(screen.getByLabelText('Search')).toHaveValue('mean')
    expect(ids()).toEqual(['C-1', 'C-3'])
    expect(screen.getByTestId('search')).toHaveTextContent('')
    expect(screen.getByTestId('search').textContent).not.toContain('q=')
    await waitFor(() => expect(screen.getByTestId('search')).toHaveTextContent('q=mean'))
  })

  it('names the clear search button by its visible text', async () => {
    renderAt('/backtest-results')
    await screen.findByText('C-1')
    const btn = screen.getByRole('button', { name: 'Clear search' })
    expect(btn).toHaveTextContent('Clear search')
    expect(btn).not.toHaveAttribute('aria-label')
  })
})
