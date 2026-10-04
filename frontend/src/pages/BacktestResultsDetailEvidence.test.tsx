import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { RunDetail } from '@/api/backtestResults'
import { makeRun } from '@/test/backtestResultsFixtures'
import BacktestResultsDetail from './BacktestResultsDetail'

const mocks = vi.hoisted(() => ({
  idea: vi.fn(),
  run: vi.fn(),
  compare: vi.fn(),
  trades: vi.fn(),
  lineage: vi.fn(),
}))

vi.mock('@/api/backtestResults', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/backtestResults')>()),
  backtestResultsApi: {
    idea: mocks.idea,
    compare: mocks.compare,
    run: mocks.run,
    trades: mocks.trades,
    lineage: mocks.lineage,
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

const RUN = makeRun({
  evaluation: {
    version: 1,
    dimensions: [
      {
        name: 'oos',
        rows: [
          {
            gate: 'G3.1',
            title: 'OOS holds',
            status: 'fail',
            hard: false,
            value: 1,
            threshold: 't',
            evidence: 'e',
          },
        ],
      },
    ],
    summary: { counts: { fail: 1 }, hard_failed: [] },
  },
})

function detail(over: Partial<RunDetail> = {}): RunDetail {
  return {
    run_id: RUN.run_id,
    schema: 3,
    captured: true,
    strategy_version: 'v1',
    parent_version: null,
    branch_label: null,
    change_classes: [],
    verdict: null,
    promotion: null,
    next_action: null,
    spec: null,
    fingerprints: null,
    scope: null,
    reproducibility: null,
    assumptions: null,
    missing_capabilities: [],
    manifest_sha256: null,
    created_ist: null,
    validity: { ok: true, failures: [], checks_run: ['data_quality', 'config'] },
    data_quality: {
      schema: 1,
      status: 'PASS',
      affects_validity: false,
      thresholds: {},
      items: [],
      summary: {},
      dropped: {},
      policy: { corporate_actions: 'ca', closing_auction: 'auction', timestamps: 'ist' },
    },
    trials: { material: 1, external: 0, total: 1, source: 'trials.yaml' },
    ledgers: {
      signals: 10,
      accepted: 5,
      excluded_by_reason: { WARMUP: 5 },
      orders: 5,
      filled: 5,
      dropped: 0,
      fills: 10,
      trades: 5,
      reconcile_problems: 0,
      problems: [],
    },
    cost_components: {
      trades: 5,
      components: [{ key: 'stt', label: 'STT', mean_bps: 2 }],
      modeled_cost_bps_mean: 2,
      engine_cost_bps_mean: 9,
    },
    has_trades: true,
    has_ledgers: true,
    ...over,
  }
}

function renderPage(d: RunDetail) {
  mocks.idea.mockResolvedValue({ status: 'success', idea: IDEA, runs: [RUN] })
  mocks.run.mockResolvedValue({ status: 'success', run: d })
  mocks.trades.mockResolvedValue({
    status: 'success',
    page: { total: 0, limit: 50, offset: 0, subset: 'oos', rows: [] },
  })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/backtest-results/IDEA-012']}>
        <Routes>
          <Route path="/backtest-results/:ideaId" element={<BacktestResultsDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

describe('BacktestResultsDetail evidence blocks', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders every evidence block for a captured run', async () => {
    renderPage(detail())
    expect(await screen.findByTestId('integrity-checks')).toBeInTheDocument()
    expect(screen.getByTestId('data-quality')).toBeInTheDocument()
    expect(screen.getByTestId('ledger-summary')).toBeInTheDocument()
    expect(screen.getByTestId('trials-block')).toBeInTheDocument()
    expect(screen.getByTestId('cost-decomposition')).toBeInTheDocument()
    expect(screen.getByTestId('cost-before')).toHaveTextContent('8.10')
    expect(screen.getByTestId('cost-after')).toHaveTextContent('-1.30')
    expect(screen.getByTestId('trades-section')).toBeInTheDocument()
    expect(screen.queryByTestId('trades-drilldown')).not.toBeInTheDocument()
  })

  it('shows the not-captured texts for a legacy run', async () => {
    renderPage(
      detail({
        captured: false,
        validity: null,
        data_quality: null,
        trials: null,
        ledgers: null,
        cost_components: null,
        has_trades: false,
        has_ledgers: false,
      })
    )
    expect(await screen.findByText('Validity checks were not recorded for this run')).toBeVisible()
    expect(screen.getByText('Not captured for this run')).toBeInTheDocument()
    expect(screen.getByText('No lineage ledgers for this run')).toBeInTheDocument()
    expect(screen.getByText('Not recorded')).toBeInTheDocument()
    expect(screen.getByText('Cost components were not recorded')).toBeInTheDocument()
  })

  it('opens the drill-down from the Trades entry point', async () => {
    const user = userEvent.setup()
    renderPage(detail())
    await user.click(await screen.findByRole('button', { name: 'Open trades' }))
    expect(await screen.findByTestId('trades-drilldown')).toBeInTheDocument()
    await waitFor(() =>
      expect(mocks.trades).toHaveBeenCalledWith(
        'IDEA-012',
        RUN.run_id,
        expect.objectContaining({ subset: 'all' })
      )
    )
  })

  it('opens the drill-down on the mapped subset from a gate and scrolls to it', async () => {
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    const user = userEvent.setup()
    renderPage(detail())
    await screen.findByTestId('integrity-checks')
    const gate = await screen.findByTestId('gate-G3.1')
    await user.click(within(gate).getByRole('button', { name: 'Show trades' }))
    expect(await screen.findByTestId('trades-drilldown')).toBeInTheDocument()
    expect(screen.getByLabelText('Subset')).toHaveValue('oos')
    expect(scroll).toHaveBeenCalled()
    expect(screen.getByTestId('trades-section')).toHaveAttribute('id', 'trades')
  })

  it('offers no gate button when the run has no trades', async () => {
    renderPage(detail({ has_trades: false }))
    await screen.findByTestId('integrity-checks')
    expect(screen.queryByRole('button', { name: 'Show trades' })).not.toBeInTheDocument()
  })

  it('resets the drill-down when the run changes', async () => {
    const user = userEvent.setup()
    const run2 = makeRun({ run_id: '2026-10-05-x-v2', date: '2026-10-05' })
    mocks.idea.mockResolvedValue({ status: 'success', idea: IDEA, runs: [RUN, run2] })
    mocks.run.mockImplementation((_i: string, runId: string) =>
      Promise.resolve({ status: 'success', run: detail({ run_id: runId }) })
    )
    mocks.trades.mockResolvedValue({
      status: 'success',
      page: { total: 0, limit: 50, offset: 0, subset: 'all', rows: [] },
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/backtest-results/IDEA-012']}>
          <Routes>
            <Route path="/backtest-results/:ideaId" element={<BacktestResultsDetail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    )
    await user.click(await screen.findByRole('button', { name: 'Open trades' }))
    expect(await screen.findByTestId('trades-drilldown')).toBeInTheDocument()
    const pick = await screen.findAllByRole('button', { name: /2026-09-30/ })
    await user.click(pick[0])
    await waitFor(() => expect(screen.queryByTestId('trades-drilldown')).not.toBeInTheDocument())
  })
})
