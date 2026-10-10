import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { BacktestIdeaResponse, RunDetail, RunRecord } from '@/api/backtestResults'
import REAL_SPEC from '@/components/backtest-results/fixtures/idea029-v4-frozen-spec.json'
import { makeRun } from '@/test/backtestResultsFixtures'
import BacktestResultsDetail from './BacktestResultsDetail'

/**
 * The page-facing items of the Definition of Done (BACKTESTS-UI-TECHNICAL-REQUIREMENTS.md
 * section 4): 1 to 3, 11 to 18 and 20. Items 4 to 10 and 19 are data behaviour and are covered by
 * test/test_backtests_definition_of_done.py. Each test names the item it covers and asserts what
 * a user sees, using the real IDEA-029 v4 frozen spec.
 *
 * Item 20 is only partly covered. The tests below show that a user can name the verdict, follow a
 * reason to the gate row that failed it and read the next action. The next-action card does not
 * itself name or link its gate (see the todo at the end), and no test can prove a person
 * understands the advice.
 */

const mocks = vi.hoisted(() => ({ idea: vi.fn(), run: vi.fn(), compare: vi.fn() }))

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

const HEX = 'abcdef0123456789'.repeat(4)
const EVALUATION = {
  version: 1,
  dimensions: [
    {
      name: 'edge',
      rows: [
        {
          gate: 'G1.2',
          title: 'Net expectancy at 1.0x cost',
          status: 'fail',
          hard: true,
          threshold: '>= +0.05R per trade',
          value: -0.2241,
          evidence: 'net expectancy -0.224R over 26144 trades',
        },
        {
          gate: 'G5.1',
          title: 'Monte Carlo',
          status: 'not_evaluated',
          hard: false,
          threshold: 'bootstrap band',
          value: null,
          evidence: 'no Monte Carlo evidence attached',
        },
      ],
    },
  ],
  summary: { counts: { fail: 1, not_evaluated: 1 }, hard_failed: ['G1.2'] },
} as unknown as NonNullable<RunRecord['evaluation']>
const RUN = makeRun({ evaluation: EVALUATION })

const DETAIL = {
  run_id: RUN.run_id,
  schema: 3,
  captured: true,
  strategy_version: 'v1',
  parent_version: null,
  branch_label: null,
  change_classes: ['RESEARCH_RERUN'],
  verdict: {
    run_verdict: 'NO_EDGE',
    primary_failed_gate: 'G1.2',
    reasons: [
      {
        text: 'A hard requirement was not met: Net expectancy at 1.0x cost',
        gate: 'G1.2',
        status: 'FAIL',
        evidence_ref: 'evaluation:G1.2',
      },
    ],
    caps_applied: [],
  },
  promotion: {
    stages: {
      backtest: {
        decision: 'NOT_READY',
        gates: [
          { gate: 'G1.2', status: 'FAIL' },
          { gate: 'G5.1', status: 'NOT_RUN' },
        ],
        reasons: ['G1.2 failed'],
      },
      paper: { decision: 'BLOCKED', gates: [{ gate: 'G7.1', status: 'NOT_RUN' }], reasons: [] },
      live: { decision: 'BLOCKED', gates: [{ gate: 'G9.1', status: 'NOT_RUN' }], reasons: [] },
    },
    next_stage: null,
  },
  next_action: {
    action: 'Reject or redesign the entry',
    reason: 'Net expectancy is negative after costs',
    gate: 'G1.2',
  },
  spec: REAL_SPEC,
  fingerprints: {
    rules: 'r'.repeat(64),
    params: 'p'.repeat(64),
    strategy: 's'.repeat(64),
    execution: 'e'.repeat(64),
    cost: 'c'.repeat(64),
    universe: 'u'.repeat(64),
    profile: 'f'.repeat(64),
    dataset: HEX,
  },
  scope: {
    market: 'NSE',
    universe: 'current_fno',
    symbol_count: 208,
    style: 'intraday',
    direction: 'both',
    timeframe: '5m',
    product: 'MIS',
    period: { start: '2021-01-01', end: '2026-09-25' },
    strategy_window: '09:20-14:15',
    exchange_session: 'NSE cash 09:15-15:30 (derived calendar)',
    broker_cutoff: '15:15',
    force_flat: '15:00',
    capital_per_trade: 100000,
    risk_per_trade_pct: 1,
    portfolio_mode: 'single_position',
    portfolio_scope: 'per_symbol',
    slippage_bps_per_side: 2.5,
    cost_bps_round_trip: 11.55,
    profile: { name: 'flattrade-nse-cash', sha256: HEX },
    dataset: {
      id: 'nse-intraday-5m-20261006-001',
      source: 'angel',
      interval: '5m',
      data_as_of: '2026-10-06',
      fingerprint: HEX,
    },
  },
  reproducibility: {
    reproducible: true,
    code_dirty: false,
    dirty_paths: [],
    code_commit: '2d74411f6',
    engine_version: '1.2',
    checklist: { strategy_version: true, code_commit: true, dataset: true },
    last_attempt: null,
  },
  assumptions: null,
  missing_capabilities: [],
  manifest_sha256: HEX,
  created_ist: '2026-10-09T10:00:00+05:30',
} as unknown as RunDetail

const IDEA = {
  id: 'IDEA-029',
  title: 'VWAP 2SD range mean reversion',
  status: 'rejected',
  verdict: 'no-edge' as const,
  horizon: 'intraday',
  summary: 's',
  result: 'r',
  revisit_when: 'never',
}

function renderPage(detail: RunDetail = DETAIL) {
  const response: BacktestIdeaResponse = { status: 'success', idea: IDEA, runs: [RUN] }
  mocks.idea.mockResolvedValue(response)
  mocks.run.mockResolvedValue({ status: 'success', run: detail })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/backtest-results/IDEA-029']}>
        <Routes>
          <Route path="/backtest-results/:ideaId" element={<BacktestResultsDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
}

describe('Definition of Done: what the page shows', () => {
  beforeEach(() => {
    mocks.idea.mockReset()
    mocks.run.mockReset()
    mocks.compare.mockReset()
  })

  it('1: the exact strategy version, parent and fingerprints used by the run', async () => {
    renderPage()
    const meta = await screen.findByTestId('frozen-meta')
    expect(meta).toHaveTextContent('Version v1')
    expect(meta).toHaveTextContent('Parent none')
    expect(meta.querySelectorAll('[title]').length).toBeGreaterThanOrEqual(3)
    expect(screen.getByTestId('strategy-definition')).toHaveTextContent(
      'Long stop = extreme - 0.20 x ATR14_5m'
    )
  })

  it('2: the complete backtest scope, with session, cutoff and force-flat as separate lines', async () => {
    renderPage()
    const scope = await screen.findByTestId('backtest-scope')
    for (const text of [
      'current_fno',
      '5m',
      '2021-01-01',
      '2026-09-25',
      '09:20-14:15',
      'NSE cash 09:15-15:30',
      '15:15',
      '15:00',
      'flattrade-nse-cash',
    ]) {
      expect(scope).toHaveTextContent(text)
    }
  })

  it('3: the dataset and its fingerprint', async () => {
    renderPage()
    const scope = await screen.findByTestId('backtest-scope')
    expect(scope).toHaveTextContent('nse-intraday-5m-20261006-001')
    expect(scope).toHaveTextContent('2026-10-06')
    expect(scope.querySelector(`[title="${HEX}"]`)).not.toBeNull()
  })

  it('11 and 12: the verdict and its explicit reasons', async () => {
    renderPage()
    const verdict = await screen.findByTestId('run-verdict')
    expect(within(verdict).getByTestId('verdict-badge')).toHaveTextContent(/no edge/i)
    expect(within(verdict).getByTestId('verdict-reasons')).toHaveTextContent(
      'Net expectancy at 1.0x cost'
    )
  })

  it('13 and 14: stage-aware promotion with failed and not-run checks visible', async () => {
    renderPage()
    const gate = await screen.findByTestId('promotion-gate')
    expect(within(gate).getByTestId('gate-stage-backtest')).toHaveTextContent(/not ready/i)
    expect(within(gate).getByTestId('gate-stage-paper')).toHaveTextContent(/blocked/i)
    expect(within(gate).getByTestId('gate-stage-live')).toHaveTextContent(/blocked/i)
    expect(within(gate).getByTestId('promotion-gate-G1.2')).toHaveTextContent(/fail/i)
    expect(within(gate).getByTestId('promotion-gate-G5.1')).toHaveTextContent(/not run/i)
  })

  it('15: the next research action and its reason', async () => {
    renderPage()
    const action = await screen.findByTestId('next-action')
    expect(action).toHaveTextContent('Reject or redesign the entry')
    expect(action).toHaveTextContent('Net expectancy is negative after costs')
  })

  it('16: a verdict reason links to the gate row that backs it', async () => {
    renderPage()
    const reasons = await screen.findByTestId('verdict-reasons')
    const link = within(reasons).getAllByRole('link')[0]
    expect(link.getAttribute('href')).toMatch(/G1\.2/)
  })

  it('17: reproducibility status is visible', async () => {
    renderPage()
    const repro = await screen.findByTestId('repro-state')
    expect(repro).toHaveTextContent('Reproducible: Yes')
  })

  it('17: a dirty run is shown as not reproducible and not eligible', async () => {
    renderPage({
      ...DETAIL,
      reproducibility: {
        ...(DETAIL.reproducibility as NonNullable<RunDetail['reproducibility']>),
        reproducible: false,
        code_dirty: true,
        dirty_paths: ['signal_engine/backtest/x.py'],
      },
    } as RunDetail)
    const repro = await screen.findByTestId('repro-state')
    expect(repro).toHaveTextContent('Reproducible: No')
    expect(repro).toHaveTextContent('signal_engine/backtest/x.py')
    expect(screen.getByTestId('repro-not-eligible')).toBeInTheDocument()
  })

  it('18: the glossary is reachable by a button and does not show until opened', async () => {
    renderPage()
    await screen.findByTestId('run-verdict')
    expect(screen.queryByRole('dialog')).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: /glossary/i }))
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
  })

  it('20: the decision, its reasons and the next step are on the page without reading metrics', async () => {
    renderPage()
    const panel = await screen.findByTestId('decision-panel')
    expect(within(panel).getByTestId('verdict-badge')).toBeInTheDocument()
    expect(within(panel).getByTestId('verdict-reasons')).toBeInTheDocument()
    expect(within(panel).getByTestId('next-action')).toBeInTheDocument()
    expect(within(panel).getByTestId('promotion-summary')).toBeInTheDocument()
  })

  it('20: a user can follow the failed reason to the gate row with its threshold and measured value', async () => {
    renderPage()
    const reasons = await screen.findByTestId('verdict-reasons')
    const href = within(reasons).getAllByRole('link')[0].getAttribute('href') ?? ''
    expect(href).toBe('#gate-G1.2')
    const target = document.getElementById(href.slice(1))
    expect(target).not.toBeNull()
    expect(target).toHaveTextContent('Net expectancy at 1.0x cost')
    expect(target).toHaveTextContent('>= +0.05R per trade')
    expect(target).toHaveTextContent('-0.22')
    expect(target).toHaveTextContent(/fail/i)
  })

  it('20: the unevaluated gate is shown as not run, not as a pass', async () => {
    renderPage()
    const row = await screen.findByTestId('promotion-gate-G5.1')
    expect(row).toHaveTextContent(/not run/i)
    expect(row).not.toHaveTextContent(/pass/i)
  })

  it('20: a marginal run reads differently: no hard failure, the missing gate is the reason and the action', async () => {
    const marginal = {
      ...DETAIL,
      verdict: {
        run_verdict: 'MARGINAL',
        primary_failed_gate: null,
        reasons: [
          {
            text: 'A promotion requirement is not evaluated: Monte Carlo',
            gate: 'G5.1',
            status: 'NOT_RUN',
            evidence_ref: 'evaluation:G5.1',
          },
        ],
        caps_applied: [],
      },
      next_action: {
        action: 'Attach Monte Carlo evidence',
        reason: 'The edge cannot be called until G5.1 is evaluated',
        gate: 'G5.1',
      },
    } as unknown as RunDetail
    renderPage(marginal)
    const panel = await screen.findByTestId('decision-panel')
    expect(within(panel).getByTestId('verdict-badge')).toHaveTextContent(/marginal/i)
    expect(within(panel).getByTestId('verdict-reasons')).toHaveTextContent('Monte Carlo')
    expect(within(panel).getByTestId('verdict-reasons')).not.toHaveTextContent(/hard requirement/i)
    expect(within(panel).getByTestId('next-action')).toHaveTextContent(
      'Attach Monte Carlo evidence'
    )
    expect(within(panel).getByTestId('next-action')).not.toHaveTextContent('Reject or redesign')
  })

  it('20: the decision comes before the evidence on the page', async () => {
    renderPage()
    const panel = await screen.findByTestId('decision-panel')
    const scope = await screen.findByTestId('backtest-scope')
    expect(panel.compareDocumentPosition(scope) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it.todo(
    '20: the next-action card names or links the gate it is about (it shows action and reason only)'
  )
})
