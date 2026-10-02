import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  BacktestCompareResponse,
  BacktestIdeaResponse,
  RunDetail,
  RunRecord,
} from '@/api/backtestResults'
import { makeRun } from '@/test/backtestResultsFixtures'
import BacktestResultsDetail from './BacktestResultsDetail'

const mocks = vi.hoisted(() => ({
  idea: vi.fn(),
  run: vi.fn(),
  compare: vi.fn(),
  createChart: vi.fn(),
  setData: vi.fn(),
}))

vi.mock('@/api/backtestResults', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/backtestResults')>()),
  backtestResultsApi: { idea: mocks.idea, compare: mocks.compare, run: mocks.run },
}))

vi.mock('lightweight-charts', () => ({
  ColorType: { Solid: 'solid' },
  LineSeries: 'line',
  createChart: (...args: unknown[]) => {
    mocks.createChart(...args)
    return {
      addSeries: () => ({ setData: mocks.setData }),
      timeScale: () => ({ fitContent: vi.fn() }),
      applyOptions: vi.fn(),
      remove: vi.fn(),
    }
  },
}))

const IDEA = {
  id: 'IDEA-012',
  title: 'EMA9 x VWAP crossover',
  status: 'rejected',
  verdict: 'no-edge' as const,
  category: 'intraday-momentum',
  horizon: 'intraday',
  summary: 'Momentum entry on the 9 EMA crossing VWAP.',
  result: 'Cross follows through less than random.',
  revisit_when: 'Only as a mean-reversion variant.',
}

const RUN_V1 = makeRun()
const RUN_V2 = makeRun({
  run_id: '2026-10-05-x-v2',
  date: '2026-10-05',
  parent_run_id: '2026-09-30-x-v1',
  change_note: 'tighter stop',
  n_trials: 2,
  metrics: { ...RUN_V1.metrics, net_bps: 2.5 },
})

function makeDetail(over: Partial<RunDetail> = {}): RunDetail {
  return {
    run_id: RUN_V1.run_id,
    schema: 3,
    captured: true,
    strategy_version: 'v3',
    parent_version: 'v2',
    branch_label: null,
    change_classes: ['parameter_change'],
    verdict: {
      run_verdict: 'EDGE',
      primary_failed_gate: null,
      reasons: [
        {
          text: 'Net edge clears the hurdle',
          gate: 'G2.1',
          status: 'PASS',
          evidence_ref: 'evaluation:G2.1',
        },
      ],
      caps_applied: [],
    },
    promotion: {
      stages: {
        backtest: { decision: 'READY_FOR_NEXT_STAGE', gates: [], reasons: [] },
        paper: { decision: 'NOT_READY', gates: [], reasons: ['not run yet'] },
        live: { decision: 'BLOCKED', gates: [], reasons: [] },
      },
      next_stage: 'paper',
    },
    next_action: { action: 'Start paper incubation', reason: 'Backtest gates cleared', gate: null },
    spec: { name: 'Frozen spec name', entry: ['frozen entry rule'] },
    fingerprints: null,
    scope: null,
    reproducibility: null,
    assumptions: null,
    missing_capabilities: [],
    manifest_sha256: null,
    created_ist: null,
    ...over,
  }
}

function ideaResponse(runs: RunRecord[]): BacktestIdeaResponse {
  return { status: 'success', idea: IDEA, runs }
}

function renderPage(
  response: BacktestIdeaResponse,
  detail: RunDetail | Error = makeDetail({ captured: false, verdict: null })
) {
  mocks.idea.mockReturnValue(Promise.resolve(response))
  if (detail instanceof Error) {
    mocks.run.mockRejectedValue(detail)
  } else {
    mocks.run.mockResolvedValue({ status: 'success', run: detail })
  }
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

const compareOk = (over: Partial<BacktestCompareResponse> = {}): BacktestCompareResponse => ({
  status: 'success',
  idea_id: 'IDEA-012',
  run_a: { run_id: RUN_V1.run_id, date: RUN_V1.date, code_version: 'abc' },
  run_b: { run_id: RUN_V2.run_id, date: RUN_V2.date, code_version: 'def' },
  metric_deltas: {
    net_bps: { a: -1.3, b: 2.5, delta: 3.8 },
    max_dd_r: { a: 12.5, b: 10.5, delta: -2 },
    sharpe: { a: 0.4, b: 0.3, delta: -0.1 },
    trades: { a: 1200, b: 1300, delta: 100 },
    win_rate: { a: 0.41, b: 0.43, delta: 0.02 },
    t_stat: { a: 1.9, b: null, delta: null },
    profit_factor: { a: 1, b: 1, delta: 0 },
  },
  params: {
    changed: { sl_atr: { a: 1.5, b: 1 } },
    added: { vol_filter: true },
    removed: { old_flag: 'x' },
  },
  n_trials: { a: 1, b: 2 },
  comparable: true,
  warnings: [],
  info: ['code version differs'],
  equity_r: { a: [['2026-01-02', 1]], b: [['2026-01-02', 2]] },
  ...over,
})

const deltaRow = (label: string) => {
  const body = screen.getByTestId('compare-body')
  const row = within(body)
    .getAllByRole('row')
    .find((r) => r.textContent?.startsWith(label))
  if (!row) throw new Error(`no delta row ${label}`)
  return row
}

describe('BacktestResultsDetail tearsheet', () => {
  beforeEach(() => {
    mocks.idea.mockReset()
    mocks.run.mockReset()
    mocks.compare.mockReset()
    mocks.setData.mockReset()
    mocks.createChart.mockReset()
  })

  it('shows the idea header with conclusion and revisit condition', async () => {
    renderPage(ideaResponse([RUN_V1]))
    expect(
      await screen.findByRole('heading', { name: /IDEA-012: EMA9 x VWAP/ })
    ).toBeInTheDocument()
    expect(screen.getByText(/Cross follows through less than random/)).toBeInTheDocument()
    expect(screen.getByText(/Only as a mean-reversion variant/)).toBeInTheDocument()
    expect(screen.getByText('no edge')).toBeInTheDocument()
  })

  it('leads with net bps after costs and shows gross beside it', async () => {
    renderPage(ideaResponse([RUN_V1]))
    const tearsheet = await screen.findByTestId('run-tearsheet')
    const label = within(tearsheet).getByText('Net bps per trade (after costs)')
    const card = label.parentElement as HTMLElement
    expect(within(card).getByText('-1.30')).toHaveClass('text-red-600')
    expect(within(card).getByText(/Gross \+8\.10/)).toBeInTheDocument()
    expect(within(tearsheet).getByText('41.0%')).toBeInTheDocument()
    expect(within(tearsheet).getByText('12.5R')).toBeInTheDocument()
    expect(within(tearsheet).getAllByText('1.90 vs 1.97').length).toBeGreaterThan(0)
  })

  it('renders n/a, never zero, when the run has no trades', async () => {
    const empty = makeRun({
      metrics: { trades: 0, gross_bps: 0, net_bps: 0 },
      cost_sensitivity: [{ round_trip_bps: 0, net_bps: 0 }],
      in_sample: undefined,
      out_of_sample: undefined,
      split: undefined,
    })
    renderPage(ideaResponse([empty]))
    const tearsheet = await screen.findByTestId('run-tearsheet')
    expect(within(tearsheet).getAllByText('n/a').length).toBeGreaterThanOrEqual(6)
    expect(within(tearsheet).queryByText('0.00')).toBeNull()
    expect(within(tearsheet).queryByText('+0.00')).toBeNull()
  })

  it('plots equity and drawdown series', async () => {
    renderPage(ideaResponse([RUN_V1]))
    await screen.findByTestId('run-tearsheet')
    expect(screen.getAllByTestId('line-chart')).toHaveLength(2)
    await waitFor(() => expect(mocks.createChart).toHaveBeenCalledTimes(2))
    expect(mocks.setData).toHaveBeenCalled()
  })

  it('says when a run has no series', async () => {
    renderPage(ideaResponse([makeRun({ series: undefined })]))
    await screen.findByTestId('run-tearsheet')
    expect(screen.getAllByText('No series recorded for this run')).toHaveLength(2)
    expect(screen.getByText('No monthly breakdown recorded')).toBeInTheDocument()
  })

  it('shows the monthly grid with signed values', async () => {
    renderPage(ideaResponse([RUN_V1]))
    const grid = await screen.findByRole('table', { name: 'Total net R per month' })
    expect(within(grid).getByText('+0.5')).toBeInTheDocument()
    expect(within(grid).getByText('-1.2')).toBeInTheDocument()
  })

  it('states clearly when there is no in-sample / out-of-sample split', async () => {
    renderPage(ideaResponse([RUN_V1]))
    expect(
      await screen.findByText(/No in-sample \/ out-of-sample split was recorded/)
    ).toBeInTheDocument()
  })

  it('shows both halves of a split side by side', async () => {
    const split = makeRun({
      split: { type: 'oos', oos_start: '2025-01-01' },
      in_sample: { trades: 900, gross_bps: 9, net_bps: 1.2, win_rate: 0.45, t_stat: 2.1 },
      out_of_sample: { trades: 300, gross_bps: 2, net_bps: -4, win_rate: 0.38 },
    })
    renderPage(ideaResponse([split]))
    expect(await screen.findByText('In-sample')).toBeInTheDocument()
    expect(screen.getByText('Out-of-sample')).toBeInTheDocument()
    expect(screen.getByText(/Out-of-sample starts 2025-01-01/)).toBeInTheDocument()
    expect(screen.getByText('-4.00')).toBeInTheDocument()
  })

  it('shows cost sensitivity with break-even cost', async () => {
    renderPage(ideaResponse([RUN_V1]))
    expect(await screen.findByText('9.4 (modelled)')).toBeInTheDocument()
    expect(screen.getByText('8.10 bps')).toBeInTheDocument()
  })

  it('says so when no cost sensitivity was recorded', async () => {
    renderPage(ideaResponse([makeRun({ cost_sensitivity: undefined })]))
    expect(await screen.findByText('No cost sensitivity recorded')).toBeInTheDocument()
  })

  it('lists assumptions, params, validation and notes', async () => {
    const run = makeRun({
      validation: ['known-answer suite passed'],
      notes: 'Ran on full universe.',
    })
    renderPage(ideaResponse([run]))
    // Once in the coverage strip at the top and once in the assumptions block.
    expect(await screen.findAllByText('fno-208')).toHaveLength(2)
    expect(screen.getByText('2021-01-01 to 2026-09-25')).toBeInTheDocument()
    expect(screen.getByText('zerodha-mis')).toBeInTheDocument()
    expect(screen.getByText('or_minutes')).toBeInTheDocument()
    expect(screen.getByText('known-answer suite passed')).toBeInTheDocument()
    expect(screen.getByText('Ran on full universe.')).toBeInTheDocument()
  })

  it('shows None recorded when a run has no params', async () => {
    renderPage(ideaResponse([makeRun({ params: {} })]))
    expect(await screen.findByText('None recorded')).toBeInTheDocument()
  })

  it('flags fewer than a year of trading days as extrapolated', async () => {
    const short = makeRun({ metrics: { ...RUN_V1.metrics, days: 100 } })
    renderPage(ideaResponse([short]))
    expect(await screen.findByText(/Only 100 trading days/)).toBeInTheDocument()
  })

  it('always shows the caveat and no pass/fail wording', async () => {
    renderPage(ideaResponse([RUN_V1]))
    const caveat = await screen.findByTestId('caveat')
    expect(caveat).toHaveTextContent(/overstate live performance/)
    expect(caveat).toHaveTextContent(/No metric on this page is a threshold/)
    expect(screen.queryByText(/\bpass(ed)?\b/i)).toBeNull()
  })
})

describe('BacktestResultsDetail run history', () => {
  beforeEach(() => {
    mocks.idea.mockReset()
    mocks.run.mockReset()
    mocks.compare.mockReset()
    mocks.compare.mockResolvedValue(compareOk())
  })

  it('lists runs newest first with lineage and change notes', async () => {
    renderPage(ideaResponse([RUN_V1, RUN_V2]))
    const heading = await screen.findByText('Run history (2)')
    const card = heading.closest('[data-slot="card"]') as HTMLElement
    const rows = within(card).getAllByRole('row').slice(1)
    expect(rows[0]).toHaveTextContent('2026-10-05')
    expect(rows[0]).toHaveTextContent('2026-09-30-x-v1')
    expect(rows[0]).toHaveTextContent('tighter stop')
    expect(rows[1]).toHaveTextContent('first run')
  })

  it('defaults to the latest run and switches when another date is picked', async () => {
    renderPage(ideaResponse([RUN_V1, RUN_V2]))
    expect(await screen.findByText(/Showing run 2026-10-05-x-v2/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '2026-09-30' }))
    expect(screen.getByText(/Showing run 2026-09-30-x-v1/)).toBeInTheDocument()
  })

  it('labels a promoted run in text', async () => {
    renderPage(ideaResponse([makeRun({ promoted: true })]))
    expect(await screen.findByText(/promoted to forward test/)).toBeInTheDocument()
  })

  it('shows a single-run message instead of compare controls', async () => {
    renderPage(ideaResponse([RUN_V1]))
    expect(await screen.findByText(/Only one run is recorded/)).toBeInTheDocument()
    expect(screen.queryByLabelText('Run A')).toBeNull()
    expect(mocks.compare).not.toHaveBeenCalled()
  })

  it('shows an empty state for an idea with no runs', async () => {
    renderPage(ideaResponse([]))
    expect(await screen.findByTestId('no-runs')).toHaveTextContent(
      /No backtest run has been recorded/
    )
  })

  it('shows the error and a way back', async () => {
    renderPage({ status: 'error', message: 'Idea IDEA-012 not found' })
    expect(await screen.findByRole('alert')).toHaveTextContent('Idea IDEA-012 not found')
    expect(screen.getByRole('link', { name: 'Back to all backtests' })).toHaveAttribute(
      'href',
      '/backtest-results'
    )
  })

  it('shows loading skeletons first', () => {
    mocks.idea.mockReturnValue(new Promise(() => {}))
    mocks.run.mockReturnValue(new Promise(() => {}))
    const client = new QueryClient()
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/backtest-results/IDEA-012']}>
          <Routes>
            <Route path="/backtest-results/:ideaId" element={<BacktestResultsDetail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    )
    expect(screen.getByTestId('loading')).toBeInTheDocument()
  })
})

describe('BacktestResultsDetail compare', () => {
  beforeEach(() => {
    mocks.idea.mockReset()
    mocks.run.mockReset()
    mocks.compare.mockReset()
    mocks.compare.mockResolvedValue(compareOk())
  })

  it('compares previous against latest by default', async () => {
    renderPage(ideaResponse([RUN_V1, RUN_V2]))
    await screen.findByTestId('compare-body')
    expect(mocks.compare).toHaveBeenCalledWith('IDEA-012', RUN_V1.run_id, RUN_V2.run_id)
  })

  it('shows neutral deltas with no verdict wording or green/red colour', async () => {
    renderPage(ideaResponse([RUN_V1, RUN_V2]))
    await screen.findByTestId('compare-body')
    const body = screen.getByTestId('compare-body')
    expect(body.textContent ?? '').not.toMatch(/improved|worse|assessment/i)
    expect(body.innerHTML).not.toMatch(/text-green-|text-red-/)
    expect(deltaRow('Net bps per trade')).toHaveTextContent(/[+-]\d/)
  })

  it('does not judge trade counts and formats win rate in points', async () => {
    renderPage(ideaResponse([RUN_V1, RUN_V2]))
    await screen.findByTestId('compare-body')
    const trades = deltaRow('Trades')
    expect(trades).toHaveTextContent('+100')
    expect(within(trades).queryByText('improved')).toBeNull()
    expect(within(trades).queryByText('worse')).toBeNull()
    const win = deltaRow('Win rate')
    expect(win).toHaveTextContent('41.0%')
    expect(win).toHaveTextContent('+2.0 pts')
  })

  it('shows parameter changes, additions, removals and trial counts', async () => {
    renderPage(ideaResponse([RUN_V1, RUN_V2]))
    await screen.findByTestId('compare-body')
    expect(screen.getByText('changed sl_atr: 1.5 to 1')).toBeInTheDocument()
    expect(screen.getByText('added vol_filter: true')).toBeInTheDocument()
    expect(screen.getByText('removed old_flag (was x)')).toBeInTheDocument()
    expect(screen.getByText(/run A 1, run B 2/)).toBeInTheDocument()
    expect(screen.getByText('code version differs')).toBeInTheDocument()
  })

  it('shows identical parameters when nothing changed', async () => {
    mocks.compare.mockResolvedValue(compareOk({ params: { changed: {}, added: {}, removed: {} } }))
    renderPage(ideaResponse([RUN_V1, RUN_V2]))
    expect(await screen.findByText('Parameters are identical')).toBeInTheDocument()
  })

  it('shows no warning banner for like-for-like runs', async () => {
    renderPage(ideaResponse([RUN_V1, RUN_V2]))
    await screen.findByTestId('compare-body')
    expect(screen.queryByText(/not like-for-like/)).toBeNull()
  })

  it('warns when the runs are not like-for-like', async () => {
    mocks.compare.mockResolvedValue(
      compareOk({ comparable: false, warnings: ['universe differs: fno-208 vs fno-40'] })
    )
    renderPage(ideaResponse([RUN_V1, RUN_V2]))
    const banner = await screen.findByText(/not like-for-like/)
    const alert = banner.closest('[role="alert"]') as HTMLElement
    expect(alert).toHaveTextContent('universe differs: fno-208 vs fno-40')
  })

  it('shows the comparison error', async () => {
    mocks.compare.mockResolvedValue({ status: 'error', message: 'Run not found: x' })
    renderPage(ideaResponse([RUN_V1, RUN_V2]))
    expect(await screen.findByText('Run not found: x')).toBeInTheDocument()
  })

  it('refetches when a different run is chosen and refuses the same run twice', async () => {
    const RUN_V3 = makeRun({ run_id: '2026-10-09-x-v3', date: '2026-10-09' })
    renderPage(ideaResponse([RUN_V1, RUN_V2, RUN_V3]))
    await screen.findByTestId('compare-body')
    await userEvent.selectOptions(screen.getByLabelText('Run A'), RUN_V1.run_id)
    await waitFor(() =>
      expect(mocks.compare).toHaveBeenLastCalledWith('IDEA-012', RUN_V1.run_id, RUN_V3.run_id)
    )
    const calls = mocks.compare.mock.calls.length
    await userEvent.selectOptions(screen.getByLabelText('Run B'), RUN_V1.run_id)
    expect(await screen.findByText('Pick two different runs to compare.')).toBeInTheDocument()
    expect(mocks.compare.mock.calls.length).toBe(calls)
  })

  it('overlays both runs on one chart with a legend', async () => {
    renderPage(ideaResponse([RUN_V1, RUN_V2]))
    await screen.findByTestId('compare-body')
    expect(screen.getByText('A: 2026-09-30')).toBeInTheDocument()
    expect(screen.getByText('B: 2026-10-05')).toBeInTheDocument()
  })
})

describe('BacktestResultsDetail data coverage and reality checks', () => {
  beforeEach(() => {
    mocks.idea.mockReset()
    mocks.run.mockReset()
  })

  it('puts the data coverage above the run history so the window is read before the numbers', async () => {
    renderPage(ideaResponse([RUN_V1]))
    const coverage = await screen.findByTestId('data-coverage')
    const history = screen.getByText(/Run history/i)
    expect(
      coverage.compareDocumentPosition(history) & Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy()
  })

  it('shows the reality checks right after the headline numbers', async () => {
    renderPage(ideaResponse([RUN_V1]))
    const checks = await screen.findByTestId('reality-checks')
    const chart = screen.getByText(/Equity and drawdown/i)
    expect(checks.compareDocumentPosition(chart) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('shows the coverage of the run being viewed, not only the latest', async () => {
    const older = makeRun({ run_id: 'r-old', date: '2026-09-01', years: 4.2, data_age_days: 40 })
    const latest = makeRun({ run_id: 'r-new', date: '2026-09-30', years: 10, data_age_days: 0 })
    renderPage(ideaResponse([older, latest]))
    expect(await screen.findByTestId('data-coverage')).toHaveTextContent('10.0 years')
    await userEvent.click(screen.getByRole('button', { name: /2026-09-01/ }))
    expect(screen.getByTestId('data-coverage')).toHaveTextContent('4.2 years')
    expect(screen.getByTestId('data-coverage')).toHaveTextContent('40 days old')
  })
})

describe('BacktestResultsDetail decision-first layout', () => {
  beforeEach(() => {
    mocks.idea.mockReset()
    mocks.run.mockReset()
    mocks.compare.mockReset()
  })

  const withIdea = (over: Record<string, unknown>): BacktestIdeaResponse => ({
    status: 'success',
    idea: { ...IDEA, ...over } as BacktestIdeaResponse['idea'],
    runs: [RUN_V1],
  })

  it('shows the calculated verdict from the run detail, not the legacy idea verdict', async () => {
    renderPage(withIdea({ verdict: 'no-edge' }), makeDetail())
    const panel = await screen.findByTestId('decision-panel')
    expect(await within(panel).findByTestId('verdict-badge')).toHaveTextContent('Edge')
    expect(within(panel).queryByTestId('legacy-note')).toBeNull()
    expect(mocks.run).toHaveBeenCalledWith('IDEA-012', RUN_V1.run_id)
    expect(within(panel).getByTestId('next-action')).toHaveTextContent('Start paper incubation')
  })

  it('labels the research state separately from the run verdict', async () => {
    renderPage(withIdea({ research_state: 'promising', stage: 'backtest' }), makeDetail())
    const state = await screen.findByTestId('research-state')
    expect(state).toHaveTextContent('Research state')
    expect(state).toHaveTextContent('Promising')
    expect(screen.getByTestId('run-verdict')).not.toContainElement(state)
  })

  it('shows strategy details from the frozen spec when run detail is present', async () => {
    renderPage(
      { ...withIdea({}), spec: { name: 'Current spec name', entry: ['current rule'] } },
      makeDetail()
    )
    expect(await screen.findByText(/frozen entry rule/)).toBeInTheDocument()
    expect(screen.queryByText(/current rule/)).toBeNull()
  })

  it('shows run identity with version and classification chips', async () => {
    renderPage(
      withIdea({
        strategy_category: 'Mean Reversion',
        classification: {
          strategy_category: 'Mean Reversion',
          strategy_tags: ['vwap'],
          trading_style: 'Intraday',
          direction: null,
          timeframe: '5m',
          market: 'NSE Equities',
        },
      }),
      makeDetail()
    )
    const identity = await screen.findByTestId('run-identity')
    expect(identity).toHaveTextContent('v3')
    expect(identity).toHaveTextContent(RUN_V1.run_id)
    expect(within(identity).getByTestId('classification')).toHaveTextContent('Mean Reversion')
  })

  it('still renders the existing blocks and a note when the run detail request fails', async () => {
    renderPage(withIdea({}), new Error('boom'))
    expect(await screen.findByTestId('run-tearsheet')).toBeInTheDocument()
    expect(await screen.findByText('Run detail unavailable')).toBeInTheDocument()
    expect(screen.getByTestId('data-coverage')).toBeInTheDocument()
    expect(screen.queryByTestId('decision-panel')).toBeNull()
  })

  it('shows placeholders while run detail loads without blanking the page', async () => {
    mocks.idea.mockResolvedValue({ status: 'success', idea: IDEA, runs: [RUN_V1] })
    mocks.run.mockReturnValue(new Promise(() => {}))
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
    expect(await screen.findByTestId('run-detail-loading')).toBeInTheDocument()
    expect(screen.getByTestId('run-tearsheet')).toBeInTheDocument()
  })

  it('shows the legacy note for a run that was not captured', async () => {
    renderPage(
      withIdea({ verdict: 'marginal' }),
      makeDetail({ captured: false, verdict: null, spec: null })
    )
    expect(await screen.findByTestId('legacy-note')).toBeInTheDocument()
    expect(screen.getByTestId('legacy-verdict')).toHaveTextContent(/marginal/i)
  })

  it('has a glossary button and the drawer is closed by default', async () => {
    renderPage(withIdea({}), makeDetail())
    await screen.findByTestId('decision-panel')
    expect(screen.getByRole('button', { name: /glossary/i })).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('renders the promotion gate and the gate anchors the reasons link to', async () => {
    renderPage(withIdea({}), makeDetail())
    expect(await screen.findByTestId('promotion-gate')).toBeInTheDocument()
  })
})

describe('BacktestResultsDetail verdict provenance', () => {
  beforeEach(() => {
    mocks.idea.mockReset()
    mocks.run.mockReset()
    mocks.compare.mockReset()
  })

  const twoRuns = (): BacktestIdeaResponse => ({
    status: 'success',
    idea: { ...IDEA, verdict: 'edge' } as BacktestIdeaResponse['idea'],
    runs: [RUN_V1, RUN_V2],
  })

  it('labels the idea verdict as the latest run verdict while detail is loading', async () => {
    mocks.idea.mockResolvedValue(twoRuns())
    mocks.run.mockImplementation(() => new Promise(() => {}))
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
    expect(await screen.findByText('Latest run verdict')).toBeInTheDocument()
    expect(screen.getByText('edge')).toBeInTheDocument()
  })

  it('shows no idea verdict in the header for an older run whose detail failed', async () => {
    renderPage(twoRuns(), new Error('boom'))
    await userEvent.click(await screen.findByRole('button', { name: '2026-09-30' }))
    await screen.findByTestId('run-detail-unavailable')
    expect(screen.queryByText('Latest run verdict')).toBeNull()
    expect(screen.queryByText('edge')).toBeNull()
  })

  it('does not show the latest verdict as the recorded verdict of an older legacy run', async () => {
    renderPage(twoRuns(), makeDetail({ captured: false, verdict: null, spec: null }))
    await userEvent.click(await screen.findByRole('button', { name: '2026-09-30' }))
    await screen.findByTestId('legacy-note')
    expect(screen.queryByTestId('legacy-verdict')).toBeNull()
    expect(screen.getByTestId('decision-panel')).toHaveTextContent(
      'No calculated verdict was recorded for this run'
    )
  })

  it('takes the identity chips from the frozen scope of a captured run', async () => {
    renderPage(
      {
        status: 'success',
        idea: {
          ...IDEA,
          classification: {
            strategy_category: 'c',
            strategy_tags: [],
            trading_style: 'IDEA-STYLE',
            direction: 'IDEA-DIR',
            timeframe: 'IDEA-TF',
            market: 'IDEA-MKT',
          },
        } as BacktestIdeaResponse['idea'],
        runs: [RUN_V1],
      },
      makeDetail({
        scope: { style: 'FROZEN-STYLE', market: 'FROZEN-MKT' } as unknown as RunDetail['scope'],
      })
    )
    const chips = await screen.findByTestId('classification')
    expect(chips).toHaveTextContent('FROZEN-STYLE')
    expect(chips).toHaveTextContent('FROZEN-MKT')
    expect(chips).not.toHaveTextContent('IDEA-STYLE')
    expect(chips).not.toHaveTextContent('IDEA-TF')
  })

  it('shows a not captured note when the captured run has no frozen spec', async () => {
    renderPage(
      { ...ideaResponse([RUN_V1]), spec: { name: 'Idea spec', entry: 'CURRENT RULE' } } as never,
      makeDetail({ spec: null })
    )
    expect(await screen.findByTestId('frozen-spec-missing')).toBeInTheDocument()
    expect(screen.queryByText(/CURRENT RULE/)).toBeNull()
  })
})
