import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { RunRecord } from '@/api/backtestResults'
import { backtestResultsApi } from '@/api/backtestResults'
import { CompareView } from './CompareView'

vi.mock('./InfoTip', () => ({ InfoTip: ({ term }: { term: string }) => <span>{term}</span> }))
vi.mock('./LineChart', () => ({ LineChart: ({ title }: { title: string }) => <div>{title}</div> }))
vi.mock('./ExportMenu', () => ({ ExportMenu: () => null }))
vi.mock('@/api/backtestResults', async (orig) => {
  const mod = await orig<typeof import('@/api/backtestResults')>()
  return { ...mod, backtestResultsApi: { ...mod.backtestResultsApi, compare: vi.fn() } }
})

const run = (id: string, date: string) => ({ run_id: id, date }) as unknown as RunRecord

function renderIt() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <CompareView ideaId="idea-1" runs={[run('R1', '2024-01-01'), run('R2', '2024-02-01')]} />
    </QueryClientProvider>
  )
}

describe('CompareView legacy fallback', () => {
  beforeEach(() => vi.clearAllMocks())

  it('keeps legacy content and shows comparison_note when comparison is null', async () => {
    vi.mocked(backtestResultsApi.compare).mockResolvedValue({
      status: 'success',
      run_a: { run_id: 'R1', date: '2024-01-01', code_version: 'x' },
      run_b: { run_id: 'R2', date: '2024-02-01', code_version: 'y' },
      metric_deltas: { trades: { a: 1, b: 2, delta: 1 } },
      comparison: null,
      comparison_note: 'Detailed comparison needs both runs to have a run folder',
    })
    renderIt()
    await waitFor(() => expect(screen.getByTestId('compare-body')).toBeInTheDocument())
    expect(screen.getByText(/needs both runs to have a run folder/)).toBeInTheDocument()
    expect(screen.getByText('Trades')).toBeInTheDocument()
    const table = screen.getByRole('table')
    expect(table.textContent ?? '').not.toMatch(/improved|worse|better|assessment/i)
    expect(table.innerHTML).not.toMatch(/green|red-/)
    expect(screen.getByText('+1')).toBeInTheDocument()
    expect(screen.queryByTestId('compare-detail')).not.toBeInTheDocument()
  })

  it('shows an explicit error when comparison is present but a run is missing', async () => {
    vi.mocked(backtestResultsApi.compare).mockResolvedValue({
      status: 'success',
      run_b: { run_id: 'R2', date: '2024-02-01', code_version: 'y' },
      metric_deltas: { trades: { a: 1, b: 2, delta: 1 } },
      comparison: { schema: 1 } as never,
    })
    renderIt()
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/could not be shown/i))
    expect(screen.queryByText('Trades')).not.toBeInTheDocument()
  })

  it('resets the selection when the runs change', async () => {
    vi.mocked(backtestResultsApi.compare).mockResolvedValue({
      status: 'success',
      comparison: null,
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const ui = (id: string, runs: RunRecord[]) => (
      <QueryClientProvider client={qc}>
        <CompareView ideaId={id} runs={runs} />
      </QueryClientProvider>
    )
    const { rerender } = render(ui('i1', [run('R1', 'd1'), run('R2', 'd2')]))
    rerender(ui('i2', [run('X1', 'd3'), run('X2', 'd4')]))
    const selects = screen.getAllByRole('combobox') as HTMLSelectElement[]
    expect(selects.map((x) => x.value)).toEqual(['X1', 'X2'])
  })
})
