import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { BacktestIdeaResponse, EvaluationBlock, RegimesBlock } from '@/api/backtestResults'
import { makeRun } from '@/test/backtestResultsFixtures'
import BacktestResultsDetail from './BacktestResultsDetail'

const mocks = vi.hoisted(() => ({ idea: vi.fn(), compare: vi.fn(), run: vi.fn() }))

vi.mock('@/api/backtestResults', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/backtestResults')>()),
  backtestResultsApi: { idea: mocks.idea, compare: mocks.compare, run: mocks.run },
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
  id: 'IDEA-023',
  title: 'Gap RSI swing',
  status: 'rejected',
  verdict: 'no-edge' as const,
  horizon: 'swing',
  summary: 'Gap entry with RSI filter.',
}

const EVALUATION: EvaluationBlock = {
  version: 1,
  dimensions: [
    {
      name: 'edge',
      rows: [
        {
          gate: 'G2.2',
          title: 'Significance',
          status: 'fail',
          hard: true,
          value: 1.2,
          threshold: 't >= 2.5',
          evidence: 't 1.2',
        },
      ],
    },
  ],
  summary: { counts: { fail: 1 }, hard_failed: ['G2.2'] },
}

const REGIMES: RegimesBlock = {
  version: 1,
  trend: [
    {
      regime: 'up',
      trades: 410,
      net_r: 0.4,
      total_r: 100,
      win_rate: 0.2,
      max_dd_r: 9,
      low_sample: false,
    },
  ],
  vol: [],
}

function renderPage(response: BacktestIdeaResponse) {
  mocks.idea.mockReturnValue(Promise.resolve(response))
  mocks.run.mockRejectedValue(new Error('run detail not mocked'))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/backtest-results/IDEA-023']}>
        <Routes>
          <Route path="/backtest-results/:ideaId" element={<BacktestResultsDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

describe('BacktestResultsDetail additions', () => {
  beforeEach(() => vi.clearAllMocks())

  it('shows the export links for the selected run', async () => {
    renderPage({ status: 'success', idea: IDEA, runs: [makeRun()] })
    const menu = await screen.findByTestId('export-2026-09-30-x-v1')
    expect(within(menu).getAllByRole('link')[0]).toHaveAttribute(
      'href',
      '/api/backtest-results/IDEA-023/runs/2026-09-30-x-v1/export?format=json'
    )
  })

  it('renders evaluation, regimes, spec, notes and rejection basis when present', async () => {
    renderPage({
      status: 'success',
      idea: { ...IDEA, rejection_basis: 'no-gross-edge' },
      runs: [makeRun({ evaluation: EVALUATION, regimes: REGIMES })],
      spec: { name: 'Gap RSI', entry: ['Gap down 2 percent at the open'] },
      research_notes: 'Decision log entry',
    })
    expect(await screen.findByTestId('evaluation')).toBeInTheDocument()
    expect(screen.getByTestId('regimes')).toBeInTheDocument()
    expect(screen.getByTestId('rejection-basis')).toHaveTextContent('no-gross-edge')
    expect(await screen.findByTestId('strategy-definition')).toHaveTextContent('Gap down 2 percent')
    expect(screen.getByTestId('research-notes')).toHaveTextContent('Decision log entry')
  })

  it('shows why a blocked idea cannot run yet, with no run recorded', async () => {
    renderPage({
      status: 'success',
      idea: {
        ...IDEA,
        status: 'blocked',
        research_state: 'blocked',
        block_reason: 'missing-published-intraday-dataset',
      },
      runs: [],
      spec: null,
    })
    expect(await screen.findByTestId('block-reason')).toHaveTextContent(
      'blocked: missing-published-intraday-dataset'
    )
    expect(screen.queryByTestId('rejection-basis')).not.toBeInTheDocument()
  })

  it('degrades for an old record with none of the new blocks', async () => {
    renderPage({ status: 'success', idea: IDEA, runs: [makeRun()], spec: null })
    await waitFor(() => expect(screen.getByTestId('run-tearsheet')).toBeInTheDocument())
    expect(screen.queryByTestId('evaluation')).not.toBeInTheDocument()
    expect(screen.queryByTestId('regimes')).not.toBeInTheDocument()
    expect(await screen.findByText('Run detail unavailable')).toBeInTheDocument()
    expect(screen.queryByTestId('rejection-basis')).not.toBeInTheDocument()
    expect(screen.queryByTestId('research-notes')).not.toBeInTheDocument()
    expect(screen.getByTestId('no-spec')).toBeInTheDocument()
  })

  it('offers trades export only when the run has a stored per-trade file', async () => {
    renderPage({ status: 'success', idea: IDEA, runs: [makeRun({ trades_export: true })] })
    expect(await screen.findByText('Trades CSV')).toBeInTheDocument()
  })

  it('offers export for both runs in the compare view', async () => {
    const v1 = makeRun()
    const v2 = makeRun({ run_id: '2026-10-05-x-v2', date: '2026-10-05' })
    mocks.compare.mockReturnValue(
      Promise.resolve({
        status: 'success',
        idea_id: 'IDEA-023',
        run_a: { run_id: v1.run_id, date: v1.date, code_version: 'a' },
        run_b: { run_id: v2.run_id, date: v2.date, code_version: 'b' },
        metric_deltas: {},
        comparable: true,
        warnings: [],
        info: [],
      })
    )
    renderPage({ status: 'success', idea: IDEA, runs: [v1, v2] })
    const body = await screen.findByTestId('compare-body')
    expect(within(body).getByTestId('export-2026-09-30-x-v1')).toBeInTheDocument()
    expect(within(body).getByTestId('export-2026-10-05-x-v2')).toBeInTheDocument()
  })
})
