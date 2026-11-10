import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ENGINE_BLOCKS } from '@/components/backtest-results/fixtures/engineBlocks'
import { makeCard, makeSwingDetail, makeSwingRun } from '@/test/backtestResultsFixtures'
import BacktestResultsDetail from './BacktestResultsDetail'

const mocks = vi.hoisted(() => ({ idea: vi.fn(), run: vi.fn(), list: vi.fn(), compare: vi.fn() }))

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

const IDEA = makeCard({ id: 'IDEA-028', title: 'Lowest realised volatility' })

function renderPage(runExtra: object, view = '') {
  mocks.idea.mockResolvedValue({
    status: 'success',
    idea: IDEA,
    runs: [{ ...makeSwingRun(), ...runExtra }],
  })
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
}

describe('Backtest results detail: engine 1.6 blocks in the detail views', () => {
  beforeEach(() => vi.clearAllMocks())

  it('shows coverage and its warnings, with the per-symbol table, under Validation details', async () => {
    renderPage(ENGINE_BLOCKS, 'validation')
    const sheet = await screen.findByTestId('sheet-validation')
    expect(await within(sheet).findByTestId('coverage-chip')).toHaveTextContent(
      'Material gaps (1% to 5%): results may be affected'
    )
    expect(within(sheet).getByTestId('coverage-disclosure')).toHaveTextContent(
      'Some trading sessions are missing from the stored history'
    )
    expect(within(sheet).getByTestId('execution-warning-note')).toBeInTheDocument()
    expect(
      within(within(sheet).getByTestId('section-coverage')).getByTestId('coverage-row-BBB')
    ).toBeInTheDocument()
  })

  it('shows the execution warnings and the known limits under Costs and assumptions', async () => {
    renderPage(ENGINE_BLOCKS, 'assumptions')
    const sheet = await screen.findByTestId('sheet-assumptions')
    expect(await within(sheet).findByTestId('execution-warnings')).toHaveTextContent(
      'Upper bound if all had exited at the target'
    )
    expect(within(sheet).getByTestId('known-limitations')).toBeInTheDocument()
  })

  it('reads not recorded for this run when the record has none of the blocks', async () => {
    renderPage({}, 'validation')
    const sheet = await screen.findByTestId('sheet-validation')
    expect(await within(sheet).findByTestId('coverage-sentence')).toHaveTextContent(
      'not recorded for this run'
    )
    expect(within(sheet).queryByTestId('coverage-chip')).not.toBeInTheDocument()
    expect(within(sheet).getByTestId('session-coverage')).toHaveTextContent(
      'not recorded for this run'
    )
  })

  it('says execution warnings were not recorded, in Costs and assumptions', async () => {
    renderPage({}, 'assumptions')
    const sheet = await screen.findByTestId('sheet-assumptions')
    expect(await within(sheet).findByTestId('execution-warnings')).toHaveTextContent(
      'not recorded for this run'
    )
    expect(within(sheet).getByTestId('known-limitations')).toBeInTheDocument()
  })
})
