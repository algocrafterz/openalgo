import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  MONTHLY_TABLE_DETAIL,
  MONTHLY_TABLE_NOTE,
  monthlyTotalDiffers,
  reliableStartNotice,
  requestedStartDate,
} from '@/lib/discretionaryPnlView'
import { monthRows, summary, weekdayRows } from '@/test/discretionaryFixtures'
import { render, screen, userEvent } from '@/test/test-utils'

const summaryFn = vi.fn()

vi.mock('@/api/discretionaryPnl', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/discretionaryPnl')>()
  return {
    ...actual,
    discretionaryPnlApi: {
      status: vi.fn(),
      summary: (...a: unknown[]) => summaryFn(...a),
      excursions: vi.fn().mockResolvedValue(null),
      tradeFills: vi.fn(),
      unresolved: vi.fn(),
    },
  }
})
vi.mock('@/hooks/useOrderEventRefresh', () => ({ useOrderEventRefresh: vi.fn() }))
vi.mock('@/components/socket/SocketProvider', () => ({
  useSocketContext: () => ({
    socket: { on: () => {}, off: () => {} },
    playAlertSound: () => {},
  }),
}))
vi.mock('./EquityCanvas', () => ({
  DRAWDOWN_COLOR: '#d97706',
  EquityCanvas: () => <div data-testid="equity-chart" />,
}))

const { DiscretionaryPerformance } = await import('./DiscretionaryPerformance')

const money = (v: number) => `Rs ${v.toFixed(2)}`
const RANGE = { start: '2026-11-01', end: '2026-11-20' }

function renderCustom(range = RANGE) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <DiscretionaryPerformance
        period="custom"
        range={range}
        rangeReady
        formatCurrency={money}
        isDarkMode={false}
      />
    </QueryClientProvider>
  )
}

function answer(reliableSince: string | undefined) {
  const base = summary()
  return summary({ period: { ...base.period, reliable_since: reliableSince } })
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('reliable start notice', () => {
  it('is shown when the requested start is before the first reliable date', async () => {
    summaryFn.mockResolvedValue(answer('2026-11-10'))
    renderCustom()
    expect(await screen.findByTestId('reliable-start-notice')).toHaveTextContent(
      'Your ledger is reliable from 10 Nov 2026. Earlier dates are not included.'
    )
  })

  it('is not shown when the requested start is the first reliable date', async () => {
    summaryFn.mockResolvedValue(answer('2026-11-01'))
    renderCustom()
    await screen.findByTestId('discretionary-performance')
    await screen.findByText(/Report (period|date):/)
    expect(screen.queryByTestId('reliable-start-notice')).not.toBeInTheDocument()
  })

  it('is not shown when the requested start is after the first reliable date', async () => {
    summaryFn.mockResolvedValue(answer('2026-10-01'))
    renderCustom()
    await screen.findByText(/Report (period|date):/)
    expect(screen.queryByTestId('reliable-start-notice')).not.toBeInTheDocument()
  })

  it('is not shown when the server gives no first reliable date', async () => {
    summaryFn.mockResolvedValue(answer(undefined))
    renderCustom()
    await screen.findByText(/Report (period|date):/)
    expect(screen.queryByTestId('reliable-start-notice')).not.toBeInTheDocument()
  })

  it('works out the requested start of a rolling window and the year', () => {
    expect(requestedStartDate('30d', undefined, '2026-11-23')).toBe('2026-10-25')
    expect(requestedStartDate('7d', undefined, '2026-11-23')).toBe('2026-11-17')
    expect(requestedStartDate('ytd', undefined, '2026-11-23')).toBe('2026-01-01')
    expect(requestedStartDate('custom', RANGE, '2026-11-23')).toBe('2026-11-01')
    expect(requestedStartDate('custom', undefined, '2026-11-23')).toBeNull()
    expect(requestedStartDate('1d', undefined, '2026-11-23')).toBeNull()
    expect(requestedStartDate('all', undefined, '2026-11-23')).toBeNull()
  })

  it('compares dates as text and ignores a missing value', () => {
    expect(reliableStartNotice('2026-10-25', '2026-11-10')).toBe(
      'Your ledger is reliable from 2026-11-10. Earlier dates are not included.'
    )
    expect(reliableStartNotice('2026-11-10', '2026-11-10')).toBeNull()
    expect(reliableStartNotice(null, '2026-11-10')).toBeNull()
    expect(reliableStartNotice('2026-10-25', undefined)).toBeNull()
  })
})

describe('monthly table note', () => {
  // month rows add up to 460 - 150 = 310
  function withMonths(net: number) {
    return summary({
      monthly: monthRows(),
      weekday: weekdayRows(),
      realized_net_of_charges: net,
      by_period: {
        ...summary().by_period,
        daily: Array.from({ length: 10 }, (_, i) => {
          const date = `2026-11-${String(i + 1).padStart(2, '0')}`
          return {
            ...summary().by_period.daily[0],
            period: date,
            label: date,
            start: date,
            end: date,
          }
        }),
      },
    })
  }

  async function openMonths() {
    const toggle = await screen.findByTestId('advanced-analysis-toggle')
    if (toggle.getAttribute('aria-expanded') !== 'true') await userEvent.click(toggle)
    await userEvent.click(await screen.findByRole('tab', { name: 'Monthly (by position close)' }))
  }

  it('is shown under the table when the table total differs from the net above', async () => {
    summaryFn.mockResolvedValue(withMonths(400))
    renderCustom()
    await openMonths()
    const note = await screen.findByTestId('monthly-table-note')
    expect(note).toHaveTextContent(MONTHLY_TABLE_NOTE)
    expect(MONTHLY_TABLE_NOTE).toContain('final close')
    // the partial-exit detail moved to the tooltip of the note
    expect(note).toHaveAttribute('title', MONTHLY_TABLE_DETAIL)
    expect(MONTHLY_TABLE_DETAIL).toContain('Partial exits')
  })

  it('is not shown when the table total equals the net above', async () => {
    summaryFn.mockResolvedValue(withMonths(310))
    renderCustom()
    await openMonths()
    expect(screen.queryByTestId('monthly-table-note')).not.toBeInTheDocument()
  })

  it('compares within 0.01 and needs both figures', () => {
    const rows = monthRows()
    expect(monthlyTotalDiffers(rows, 310)).toBe(false)
    expect(monthlyTotalDiffers(rows, 310.01)).toBe(false)
    expect(monthlyTotalDiffers(rows, 310.02)).toBe(true)
    expect(monthlyTotalDiffers(rows, null)).toBe(false)
    expect(monthlyTotalDiffers([], 5)).toBe(false)
    expect(monthlyTotalDiffers(undefined, 5)).toBe(false)
  })
})
