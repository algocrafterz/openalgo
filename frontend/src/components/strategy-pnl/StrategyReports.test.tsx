import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@/test/test-utils'
import { StrategyReports } from './StrategyReports'

const listReports = vi.fn()

vi.mock('@/api/strategyPnl', () => ({
  strategyPnlApi: { listReports: () => listReports() },
  strategyReportUrl: (filename: string, download = false) =>
    `/api/strategy-pnl/reports/${filename}${download ? '?download=1' : ''}`,
}))

function renderIt() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <StrategyReports />
    </QueryClientProvider>
  )
}

beforeEach(() => vi.clearAllMocks())

describe('StrategyReports', () => {
  it('lists saved reports with View and Download links', async () => {
    listReports.mockResolvedValue([
      { filename: 'both-2026-11-13.html', mode: 'both', date: '2026-11-13', size: 10 },
      { filename: 'paper-2026-11-06.html', mode: 'paper', date: '2026-11-06', size: 10 },
    ])
    renderIt()
    expect(await screen.findByText('2026-11-13')).toBeInTheDocument()
    expect(screen.getByText('Paper and live')).toBeInTheDocument()
    const views = screen.getAllByRole('link', { name: /view/i })
    expect(views[0]).toHaveAttribute('href', '/api/strategy-pnl/reports/both-2026-11-13.html')
    expect(views[0]).toHaveAttribute('target', '_blank')
    const downloads = screen.getAllByRole('link', { name: /download/i })
    expect(downloads[1]).toHaveAttribute(
      'href',
      '/api/strategy-pnl/reports/paper-2026-11-06.html?download=1'
    )
  })

  it('says so when no report has been saved', async () => {
    listReports.mockResolvedValue([])
    renderIt()
    expect(await screen.findByText('No reports have been saved yet.')).toBeInTheDocument()
  })

  it('shows the error instead of an empty list', async () => {
    listReports.mockRejectedValue(new Error('Could not list saved reports.'))
    renderIt()
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent('Could not list saved reports.')
    )
    expect(screen.queryByText('No reports have been saved yet.')).not.toBeInTheDocument()
  })
})
