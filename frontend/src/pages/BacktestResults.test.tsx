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

const ok = (ideas = IDEAS): BacktestListResponse => ({
  status: 'success',
  available: true,
  ideas,
  summary: {
    total_ideas: ideas.length,
    total_runs: 2,
    ideas_with_runs: 2,
    by_status: {},
    by_verdict: {},
  },
})

function renderAt(url: string, response: BacktestListResponse = ok()) {
  mocks.list.mockReturnValue(Promise.resolve(response))
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
          <Route path="/backtest-results/:ideaId" element={<div>detail page</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

const renderPage = (response?: BacktestListResponse) => renderAt('/backtest-results', response)

/** Titles of the strategies on the page: tested ones first, then the collapsed untested ones. */
const titles = () => screen.getAllByRole('link').map((a) => a.textContent)

describe('BacktestResults dashboard', () => {
  beforeEach(() => {
    mocks.list.mockReset()
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

  it('shows the server error message', async () => {
    renderPage({ status: 'error', message: 'Research registry is invalid' })
    expect(await screen.findByRole('alert')).toHaveTextContent('Research registry is invalid')
  })

  it('says so when backtest data is not available on this server', async () => {
    renderPage({ status: 'success', available: false, ideas: [] })
    expect(
      await screen.findByText('Backtest research data is not available on this server.')
    ).toBeInTheDocument()
  })

  it('says so when no ideas are registered', async () => {
    renderPage(ok([]))
    expect(await screen.findByText('No ideas are registered yet.')).toBeInTheDocument()
  })

  it('has no summary sentence, totals or extra controls', async () => {
    renderPage()
    await screen.findByText('Gamma fade')
    expect(screen.queryByTestId('summary-line')).toBeNull()
    expect(screen.queryByTestId('summary-tiles')).toBeNull()
    expect(screen.queryByTestId('more-columns')).toBeNull()
    expect(screen.queryByLabelText('Group by category')).toBeNull()
    expect(screen.queryByRole('button', { name: /^Net bps/ })).toBeNull()
    expect(screen.queryByText(/not predictions/i)).toBeNull()
  })

  it('puts tested strategies in the table and the rest in one collapsed line', async () => {
    renderPage()
    await screen.findByText('Gamma fade')
    const untested = screen.getByTestId('untested-ideas')
    expect(untested).toHaveTextContent('1 not tested yet')
    expect(untested).not.toHaveAttribute('open')
    expect(within(untested).getByText('Beta idea')).toBeInTheDocument()
    expect(
      within(screen.getByTestId('catalogue-row-IDEA-003')).getByText('Gamma fade')
    ).toBeVisible()
  })

  it('orders by evidence: runs first, then assessment, never by return', async () => {
    renderPage()
    await screen.findByText('Gamma fade')
    expect(titles()).toEqual(['Gamma fade', 'Alpha breakout', 'Beta idea'])
  })

  it('shows the assessment words a trader reads, not the research verdict words', async () => {
    renderPage()
    const row = (await screen.findByText('Gamma fade')).closest('tr') as HTMLElement
    expect(within(row).getByTestId('plain-verdict-chip')).toHaveTextContent('Borderline')
    const weak = screen.getByText('Alpha breakout').closest('tr') as HTMLElement
    expect(within(weak).getByTestId('plain-verdict-chip')).toHaveTextContent('Weak')
  })

  it('uses the calculated run verdict, falling back to the derived idea verdict', async () => {
    renderPage(
      ok([
        makeCard({
          id: 'IDEA-020',
          title: 'Calculated',
          verdict: 'no-edge',
          latest_run: makeLatest({ run_verdict: 'EDGE' }),
        }),
        makeCard({
          id: 'IDEA-021',
          title: 'Derived',
          verdict: 'marginal',
          latest_run: makeLatest(),
        }),
      ])
    )
    const calculated = (await screen.findByText('Calculated')).closest('tr') as HTMLElement
    expect(within(calculated).getByTestId('plain-verdict-chip')).toHaveTextContent('Promising')
    const derived = screen.getByText('Derived').closest('tr') as HTMLElement
    expect(within(derived).getByTestId('plain-verdict-chip')).toHaveTextContent('Borderline')
  })

  it('shows Not available, never zero, for a run with no trades', async () => {
    renderPage(
      ok([
        makeCard({
          id: 'IDEA-009',
          title: 'Empty run',
          latest_run: makeLatest({ metrics: { trades: 0, gross_bps: 0, net_bps: 0 } }),
        }),
      ])
    )
    const row = (await screen.findByText('Empty run')).closest('tr') as HTMLElement
    expect(within(row).getAllByText('Not available').length).toBeGreaterThanOrEqual(2)
    expect(within(row).queryByText('0.00')).toBeNull()
    expect(within(row).queryByText('+0.00')).toBeNull()
  })

  it('filters by assessment, using the words a trader reads', async () => {
    renderPage()
    await screen.findByText('Gamma fade')
    const select = screen.getByLabelText('Assessment')
    expect(
      within(select)
        .getAllByRole('option')
        .map((o) => o.textContent)
    ).toEqual(['All', 'Promising', 'Borderline', 'Needs more data', 'Weak'])
    await userEvent.selectOptions(select, 'marginal')
    expect(titles()).toEqual(['Gamma fade'])
    await userEvent.click(screen.getByRole('button', { name: 'Clear' }))
    expect(titles()).toHaveLength(3)
  })

  it('searches by title and by category, and says so when nothing matches', async () => {
    renderPage(
      ok([
        ...IDEAS,
        makeCard({
          id: 'IDEA-004',
          title: 'Plain name',
          strategy_category: 'Mean Reversion',
          latest_run: makeLatest(),
        }),
      ])
    )
    await screen.findByText('Gamma fade')
    const search = screen.getByLabelText('Search strategies')
    await userEvent.type(search, 'gamma')
    expect(titles()).toEqual(['Gamma fade'])
    await userEvent.clear(search)
    await userEvent.type(search, 'mean rev')
    expect(titles()).toEqual(['Plain name'])
    await userEvent.clear(search)
    await userEvent.type(search, 'no-such-thing')
    expect(screen.getByText('No strategies match these filters.')).toBeInTheDocument()
  })

  it('filters at once but writes the search to the address after a pause', async () => {
    renderPage()
    await screen.findByText('Gamma fade')
    await userEvent.type(screen.getByLabelText('Search strategies'), 'gamma')
    expect(titles()).toEqual(['Gamma fade'])
    expect(screen.getByTestId('search').textContent).not.toContain('q=')
    await waitFor(() => expect(screen.getByTestId('search')).toHaveTextContent('q=gamma'))
  })

  it('restores the assessment filter from the address', async () => {
    renderAt('/backtest-results?verdict=marginal')
    await screen.findByText('Gamma fade')
    expect(titles()).toEqual(['Gamma fade'])
    expect(screen.getByLabelText('Assessment')).toHaveValue('marginal')
  })

  it('ignores a stale value in the address and shows every strategy', async () => {
    renderAt('/backtest-results?verdict=bogus&cat=Gone&tag=NoSuchTag')
    await screen.findByText('Gamma fade')
    expect(titles()).toHaveLength(3)
    expect(screen.getByLabelText('Assessment')).toHaveValue('all')
  })

  it('opens the report of a strategy when its row is clicked', async () => {
    renderPage()
    const row = (await screen.findByText('Gamma fade')).closest('tr') as HTMLElement
    await userEvent.click(row)
    await waitFor(() => expect(screen.getByText('detail page')).toBeInTheDocument())
  })
})

describe('response from a server that predates the coverage fields', () => {
  // An app server started before the backend gained period, years, data_age_days, net_bps_cap10
  // and baseline_excess_bps still answers with the older shape. The page must degrade, not crash.
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

  beforeEach(() => {
    mocks.list.mockReset()
  })

  it('renders the row instead of throwing, and still shows the numbers it has', async () => {
    renderPage(ok([olderShape()]))
    const row = (await screen.findByText('Older shape')).closest('tr') as HTMLElement
    expect(within(row).getByText('1200')).toBeInTheDocument()
    expect(screen.queryByText(/something went wrong/i)).not.toBeInTheDocument()
  })
})
