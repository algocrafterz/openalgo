import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, within } from '@testing-library/react'
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

const SECTIONS = ['decision', 'gates', 'performance', 'evidence', 'definition', 'history']

async function renderPage() {
  mocks.idea.mockResolvedValue({ status: 'success', idea: IDEA, runs: [RUN] })
  mocks.run.mockResolvedValue({ status: 'success', run: makeSwingDetail() })
  mocks.list.mockResolvedValue({ status: 'success', ideas: [] })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/backtest-results/IDEA-028']}>
        <Routes>
          <Route path="/backtest-results/:ideaId" element={<BacktestResultsDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  )
  return screen.findByTestId('decision-brief', {}, { timeout: 5000 })
}

describe('BacktestResultsDetail decision-first view', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.location.hash = ''
  })

  it('answers the four questions on the first screen', async () => {
    const brief = await renderPage()
    for (const q of [
      'Should I pursue this strategy?',
      'How did it perform?',
      'Why?',
      'What should I do next?',
    ]) {
      expect(within(brief).getByText(q)).toBeVisible()
    }
    expect(screen.getByTestId('brief-decision')).toHaveTextContent('Do not pursue this version.')
    expect(screen.getByTestId('brief-figures')).toHaveTextContent('+10.6%')
    expect(within(brief).getByTestId('portfolio-chart')).toBeVisible()
    expect(screen.getByTestId('brief-action')).toHaveTextContent('Do not continue this version.')
  })

  it('collapses every technical section by default', async () => {
    await renderPage()
    for (const id of SECTIONS) {
      expect(screen.getByTestId(`section-${id}`)).not.toHaveAttribute('open')
    }
    // The system statuses and the technical record are in the details, not on the first screen.
    expect(screen.getByTestId('summary-verdict')).not.toBeVisible()
    expect(screen.getByTestId('researcher-conclusion')).not.toBeVisible()
    expect(screen.queryByText(/^Conclusion:/)).not.toBeInTheDocument()
  })

  it('shows no gate ids or statistics in the visible decision', async () => {
    const brief = await renderPage()
    expect(brief.textContent).not.toMatch(/\bG\d\.\d\b/)
    expect(brief.textContent).not.toMatch(/t-stat|\bt -?\d/)
  })

  it('keeps decision-critical warnings visible', async () => {
    await renderPage()
    const caveats = screen.getByTestId('brief-caveats')
    expect(caveats).toBeVisible()
    expect(caveats).toHaveTextContent('A test that was not run is not a pass.')
    expect(caveats).toHaveTextContent('survivorship bias')
    expect(screen.getByTestId('brief-supporting')).toHaveTextContent(/not decisive/)
  })

  it('expands the full reasoning on request', async () => {
    await renderPage()
    const section = screen.getByTestId('section-decision')
    await userEvent.click(within(section).getByText('How this decision was reached'))
    expect(section).toHaveAttribute('open')
    expect(screen.getByTestId('summary-verdict')).toBeVisible()
    expect(screen.getByTestId('summary-failed')).toHaveTextContent('G3.2')
    expect(screen.getByTestId('researcher-conclusion')).toHaveTextContent('G2.2 t 5.66')
    expect(screen.getByTestId('conclusion-source')).toHaveTextContent(
      `Recorded for the run shown (${RUN.run_id}).`
    )
  })

  it('opens the right evidence when a finding is followed', async () => {
    await renderPage()
    const link = within(screen.getByTestId('brief-findings')).getAllByText('details')[1]
    expect(link).toHaveAttribute('href', '#gate-G3.2')
    act(() => {
      window.location.hash = '#gate-G3.2'
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
    expect(screen.getByTestId('section-performance')).toHaveAttribute('open')
    expect(screen.getByTestId('section-gates')).not.toHaveAttribute('open')

    act(() => {
      window.location.hash = '#random-entry'
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
    expect(screen.getByTestId('section-decision')).toHaveAttribute('open')
  })
})
