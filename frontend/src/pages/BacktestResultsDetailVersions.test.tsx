import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Link, MemoryRouter, Route, Routes, useLocation } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { BacktestIdeaResponse, RunRecord, StrategyVersion } from '@/api/backtestResults'
import { makeRun } from '@/test/backtestResultsFixtures'
import BacktestResultsDetail from './BacktestResultsDetail'

const mocks = vi.hoisted(() => ({
  idea: vi.fn(),
  run: vi.fn(),
  compare: vi.fn(),
}))

vi.mock('@/api/backtestResults', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/backtestResults')>()),
  backtestResultsApi: {
    idea: mocks.idea,
    compare: mocks.compare,
    run: mocks.run,
  },
}))

vi.mock('lightweight-charts', () => ({
  ColorType: { Solid: 'solid' },
  LineSeries: 'line',
  createChart: () => ({
    addSeries: () => ({ setData: vi.fn() }),
    timeScale: () => ({ fitContent: vi.fn() }),
    applyOptions: vi.fn(),
    remove: vi.fn(),
  }),
}))

const IDEA = {
  id: 'IDEA-012',
  title: 'EMA9 x VWAP crossover',
  status: 'rejected',
  verdict: 'no-edge' as const,
  horizon: 'intraday',
  summary: 'Momentum entry.',
}

const v = (version: string, parent: string | null, note: string): StrategyVersion => ({
  version,
  strategy_fingerprint: `fp-${version}`,
  parent,
  created_ist: '2026-09-01T10:30:00+05:30',
  note,
})

const RUNS: RunRecord[] = [
  makeRun({ run_id: 'run-a', date: '2026-09-01', strategy_version: 'v1', run_verdict: 'NO_EDGE' }),
  makeRun({ run_id: 'run-b', date: '2026-09-05', strategy_version: 'v2', run_verdict: 'EDGE' }),
]

function Where() {
  return (
    <>
      <div data-testid="where">{useLocation().pathname}</div>
      <Link to="/backtest-results/IDEA-099">go related</Link>
    </>
  )
}

function renderPage(over: Partial<BacktestIdeaResponse> = {}, _unused?: undefined, view = '') {
  mocks.idea.mockResolvedValue({
    status: 'success',
    idea: IDEA,
    runs: RUNS,
    versions: [v('v1', null, 'first cut'), v('v2', 'v1', 'tighter stop')],
    version_pointers: { current_version: 'v2', promoted_version: 'v1', promoted_run_id: 'run-a' },
    ...over,
  })
  mocks.run.mockResolvedValue({ status: 'error', message: 'unavailable' })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/backtest-results/IDEA-012${view ? `?view=${view}` : ''}`]}>
        <Routes>
          <Route
            path="/backtest-results/:ideaId"
            element={
              <>
                <BacktestResultsDetail />
                <Where />
              </>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

describe('BacktestResultsDetail versions', () => {
  beforeEach(() => {
    for (const m of Object.values(mocks)) m.mockReset()
  })

  it('renders the version tree with badges from the pointers', async () => {
    renderPage(undefined, undefined, 'history')
    const tree = await screen.findByTestId('version-tree')
    expect(within(tree).getByText('first cut')).toBeInTheDocument()
    expect(within(tree).getByTestId('badge-current')).toBeInTheDocument()
    expect(within(tree).getByTestId('badge-promoted')).toHaveTextContent('run-a')
  })

  it('selecting a run in the tree selects it for the page', async () => {
    renderPage(undefined, undefined, 'history')
    const tree = await screen.findByTestId('version-tree')
    await waitFor(() => expect(screen.getByText(/Showing run run-b/)).toBeInTheDocument())
    await userEvent.click(within(tree).getByRole('button', { name: 'run-a' }))
    expect(screen.getByText(/Showing run run-a/)).toBeInTheDocument()
    expect(within(tree).getByRole('button', { name: 'run-a' })).toHaveAttribute(
      'aria-current',
      'true'
    )
  })

  it('groups the run history by version', async () => {
    renderPage(undefined, undefined, 'history')
    await screen.findByTestId('version-tree')
    expect(await screen.findAllByTestId('version-group')).toHaveLength(2)
  })

  it('falls back to a flat history and a note when versions is null', async () => {
    renderPage({ versions: null, version_pointers: undefined }, undefined, 'history')
    expect(await screen.findByText('No version history recorded for this idea')).toBeInTheDocument()
    expect(screen.queryByTestId('version-group')).not.toBeInTheDocument()
    expect(screen.getByText('Run history (2)')).toBeInTheDocument()
  })

  it('shows the versions_error notice, not the plain no-history note', async () => {
    renderPage(
      { versions: null, versions_error: 'versions.yaml could not be read' },
      undefined,
      'history'
    )
    const tree = await screen.findByTestId('version-tree')
    expect(within(tree).getByText('versions.yaml could not be read')).toBeInTheDocument()
    expect(within(tree).queryByText('No version history recorded for this idea')).toBeNull()
  })

  it('does not carry the selected run over to the next idea', async () => {
    renderPage(undefined, undefined, 'history')
    const tree = await screen.findByTestId('version-tree')
    await waitFor(() => expect(screen.getByText(/Showing run run-b/)).toBeInTheDocument())
    await userEvent.click(within(tree).getByRole('button', { name: 'run-a' }))
    expect(screen.getByText(/Showing run run-a/)).toBeInTheDocument()
    await userEvent.keyboard('{Escape}')
    await userEvent.click(await screen.findByRole('link', { name: 'go related' }))
    expect(screen.getByTestId('where')).toHaveTextContent('/backtest-results/IDEA-099')
    await waitFor(() => expect(screen.queryByText(/Showing run run-a/)).toBeNull())
  })
})
