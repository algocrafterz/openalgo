import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { makeCard, makeSwingDetail, makeSwingRun } from '@/test/backtestResultsFixtures'
import BacktestResultsDetail from './BacktestResultsDetail'

const mocks = vi.hoisted(() => ({
  idea: vi.fn(),
  run: vi.fn(),
  list: vi.fn(),
  compare: vi.fn(),
}))

vi.mock('@/api/backtestResults', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/backtestResults')>()),
  backtestResultsApi: {
    idea: mocks.idea,
    run: mocks.run,
    list: mocks.list,
    compare: mocks.compare,
  },
}))

vi.mock('@/components/backtest-results/LineChart', () => ({
  LineChart: ({ title }: { title: string }) => <div>{title}</div>,
}))

const RUN = makeSwingRun()
const IDEA = {
  ...makeCard({
    id: 'IDEA-028',
    title: 'Lowest realised volatility',
    research_state: 'rejected',
    rejection_basis: 'not-robust',
    revisit_when: 'Only a new, pre-specified hypothesis, recorded as a separate idea.',
  }),
  result: `MARGINAL (run ${RUN.run_id}): G2.2 t 5.66 (pass); G3.2 out-of-sample FAIL.`,
}

async function renderPage(view = '') {
  mocks.idea.mockResolvedValue({ status: 'success', idea: IDEA, runs: [RUN] })
  mocks.run.mockResolvedValue({ status: 'success', run: makeSwingDetail() })
  mocks.list.mockResolvedValue({ status: 'success', ideas: [] })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/backtest-results/IDEA-028${view ? `?view=${view}` : ''}`]}>
        <Routes>
          <Route path="/backtest-results/:ideaId" element={<BacktestResultsDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
  // The page's own first block is ready once the run detail (the next step) has loaded.
  await screen.findByTestId('next-step', {}, { timeout: 5000 })
}

const before = (a: HTMLElement, b: HTMLElement) =>
  !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)

describe('BacktestResultsDetail first screen', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.location.hash = ''
  })

  it('leads with the verdict, the reasons, the next step and the six figures', async () => {
    await renderPage()
    const result = screen.getByTestId('result-card')
    expect(within(result).getByTestId('plain-verdict-chip')).toHaveTextContent('Borderline')
    expect(within(result).getByTestId('next-step')).toHaveTextContent(
      'Do not continue this version.'
    )
    const keys = within(result).getByTestId('key-metrics')
    for (const id of ['net-pnl', 'return', 'drawdown', 'profit-factor', 'trades', 'avg-trade']) {
      expect(within(keys).getByTestId(`kpi-${id}`)).toBeVisible()
    }
    expect(screen.getByTestId('kpi-return')).toHaveTextContent('+161.6%')
    // The total is never shown without how long it took.
    expect(screen.getByTestId('kpi-return')).toHaveTextContent('+10.6% a year')
    expect(within(result).getByTestId('why-list')).toBeVisible()
  })

  it('shows no gate ids or statistics terms on the first screen', async () => {
    await renderPage()
    const result = screen.getByTestId('result-card')
    expect(result.textContent).not.toMatch(/\bG\d\.\d\b/)
    expect(result.textContent).not.toMatch(/t-stat|\bt -?\d/)
  })

  it('orders the page: result, chart, statistics, then the links to the detail', async () => {
    await renderPage()
    const order = [
      screen.getByTestId('result-card'),
      screen.getByTestId('run-tearsheet-chart'),
      screen.getByTestId('section-performance'),
      screen.getByTestId('report-links'),
    ]
    for (let i = 0; i < order.length - 1; i++) expect(before(order[i], order[i + 1])).toBe(true)
    expect(within(order[1]).getByTestId('portfolio-chart')).toBeVisible()
    expect(within(order[2]).queryByTestId('portfolio-chart')).toBeNull()
  })

  it('has a statistics table and keeps monthly returns and breakdowns collapsed', async () => {
    await renderPage()
    const stats = screen.getByTestId('statistics-table')
    expect(within(stats).getByTestId('stat-win-rate')).toBeVisible()
    expect(within(stats).getByTestId('stat-drawdown')).toHaveTextContent('-23.0%')
    expect(screen.getByTestId('section-monthly')).not.toHaveAttribute('open')
    expect(screen.getByTestId('section-diagnostics')).not.toHaveAttribute('open')
  })

  it('offers the detail links and opens none of them', async () => {
    await renderPage()
    const links = screen.getByTestId('report-links')
    for (const name of [
      'Costs and assumptions',
      'Strategy rules',
      'Validation details',
      'Run history and compare',
      'Research notes',
    ]) {
      expect(within(links).getByRole('button', { name })).toBeInTheDocument()
    }
    expect(within(links).getByRole('button', { name: /^All \d+ trades$/ })).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByTestId('decision-brief')).toBeNull()
  })

  it('opens a detail view from its link, and closes it again', async () => {
    await renderPage()
    await userEvent.click(screen.getByRole('button', { name: 'Validation details' }))
    expect(await screen.findByTestId('decision-brief')).toBeVisible()
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByTestId('decision-brief')).toBeNull())
  })
})

describe('BacktestResultsDetail validation details', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.location.hash = ''
  })

  it('answers the questions in plain words, without repeating the key figures', async () => {
    await renderPage('validation')
    const brief = await screen.findByTestId('decision-brief')
    for (const q of ['Should I pursue this strategy?', 'Why?', 'What should I do next?']) {
      expect(within(brief).getByText(q)).toBeVisible()
    }
    expect(within(brief).queryByText('How did it perform?')).toBeNull()
    expect(screen.getByTestId('brief-decision')).toHaveTextContent('Do not continue this version.')
    expect(screen.getByTestId('brief-basis')).toBeVisible()
    expect(screen.getByTestId('brief-action')).toHaveTextContent('Do not continue this version.')
    expect(brief.textContent).not.toMatch(/\bG\d\.\d\b/)
  })

  it('keeps decision-critical warnings visible', async () => {
    await renderPage('validation')
    const caveats = await screen.findByTestId('brief-caveats')
    expect(caveats).toBeVisible()
    expect(caveats).toHaveTextContent('A test that was not run is not a pass.')
    expect(caveats).toHaveTextContent('survivorship bias')
    expect(screen.getByTestId('brief-supporting')).toHaveTextContent(/not decisive/)
  })

  it('shows the full reasoning and the evidence a finding links to', async () => {
    await renderPage('validation')
    await screen.findByTestId('decision-brief')
    expect(screen.getByTestId('summary-failed')).toHaveTextContent('G3.2')
    const link = within(screen.getByTestId('brief-findings')).getAllByText('details')[1]
    expect(link).toHaveAttribute('href', '#gate-G3.2')
    // The target of the link is inside Audit, which is closed until something opens it.
    expect(document.getElementById('gate-G3.2')).not.toBeNull()
    const audit = screen.getByTestId('section-audit')
    expect(audit).not.toHaveAttribute('open')
    await userEvent.click(link)
    await waitFor(() => expect(audit).toHaveAttribute('open'))
    expect(screen.getByTestId('summary-verdict')).toBeVisible()
    expect(screen.getByTestId('section-tests')).toBeVisible()
  })

  it('answers first, then the checks, then a closed Audit and closed readiness', async () => {
    await renderPage('validation')
    const brief = await screen.findByTestId('decision-brief')
    const checks = screen.getByTestId('validation-checks')
    const audit = screen.getByTestId('section-audit')
    const readiness = screen.getByTestId('section-readiness')
    for (const [a, b] of [
      [brief, checks],
      [checks, audit],
      [audit, readiness],
    ]) {
      expect(before(a, b)).toBe(true)
    }
    expect(audit).not.toHaveAttribute('open')
    expect(readiness).not.toHaveAttribute('open')
    // Every individual gate stays in the page, inside Audit.
    expect(within(audit).getByTestId('promotion-gate')).toBeInTheDocument()
    expect(within(readiness).getByTestId('readiness-gate')).toBeInTheDocument()
    expect(within(checks).queryByTestId('promotion-gate')).toBeNull()
  })

  it('summarises the recorded gate results by question, worst result first in each group', async () => {
    await renderPage('validation')
    const checks = await screen.findByTestId('validation-checks')
    // The fixture records stability (G3.x), statistical (G2.2) and robustness (G4.x) gates only,
    // so the other groups are left out rather than shown empty.
    expect(
      within(checks)
        .getAllByRole('listitem')
        .map((li) => li.dataset.testid)
    ).toEqual(['check-random', 'check-oos', 'check-robustness', 'check-sample'])
    // The run records a random-entry baseline but no gate row for it: it is shown as recorded.
    expect(within(checks).getByTestId('check-state-random')).toHaveTextContent('Reported only')
    expect(within(checks).getByTestId('check-random')).toHaveTextContent(
      'Random entries did better'
    )
    expect(within(checks).getByTestId('check-state-oos')).toHaveTextContent('Failed')
    // G3.3 is re-scored by an evaluator correction in the fixture, so one gate fails, not two.
    expect(within(checks).getByTestId('check-oos')).toHaveTextContent(
      '1 failed: Out-of-sample result'
    )
    // Not run is shown as not run, never as a pass.
    expect(within(checks).getByTestId('check-state-robustness')).toHaveTextContent(
      'Not fully checked'
    )
    expect(within(checks).getByTestId('check-state-sample')).toHaveTextContent('Passed')
    expect(within(checks).queryByTestId('check-edge')).toBeNull()
  })
})

describe('BacktestResultsDetail other detail views', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.location.hash = ''
  })

  it('keeps the costs and the full assumptions panel in Costs and assumptions', async () => {
    await renderPage('assumptions')
    expect(await screen.findByTestId('assumptions-confidence')).toBeVisible()
    expect(screen.getByTestId('slippage-line')).toBeVisible()
  })

  it('keeps the rules, scope and reproducibility in Strategy rules', async () => {
    await renderPage('rules')
    expect(await screen.findByTestId('backtest-scope')).toBeVisible()
  })

  it("keeps the researcher's record in Research notes, in its original wording", async () => {
    await renderPage('notes')
    expect(await screen.findByTestId('researcher-conclusion')).toHaveTextContent('G2.2 t 5.66')
    expect(screen.getByTestId('recorded-status')).toHaveTextContent(
      `Recorded status: ${IDEA.status} (not-robust)`
    )
    expect(screen.getByTestId('conclusion-source')).toHaveTextContent(
      `Recorded for the run shown (${RUN.run_id}).`
    )
    expect(screen.getByTestId('rejection-basis')).toBeVisible()
  })
})
