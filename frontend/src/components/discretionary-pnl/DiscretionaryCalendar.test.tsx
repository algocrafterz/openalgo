import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { PeriodRow } from '@/api/discretionaryPnl'
import {
  breakdownRow,
  consistency,
  riskBlock,
  summary,
  tradingBreakdowns,
} from '@/test/discretionaryFixtures'
import { render, screen, userEvent, waitFor, within } from '@/test/test-utils'

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
  EquityCanvas: ({ points, mode }: { points: unknown[]; mode: string }) => (
    <div data-testid="equity-chart" data-mode={mode}>
      {points.length} points
    </div>
  ),
}))

const { DiscretionaryPerformance } = await import('./DiscretionaryPerformance')
const { todayIst } = await import('@/lib/strategyPnlView')

const money = (v: number) => `Rs ${v.toFixed(2)}`
// The calendar loads after the report, so a loaded machine needs more than the 1 s default.
const SLOW = { timeout: 5000 }
const TODAY = todayIst()
const MONTH = TODAY.slice(0, 7)

function row(date: string, net: number, extra: Partial<PeriodRow> = {}): PeriodRow {
  return {
    period: date,
    label: date,
    start: date,
    end: date,
    trades: 2,
    wins: net > 0 ? 1 : 0,
    losses: net > 0 ? 1 : 2,
    realized_gross: net + 5,
    charges: 5,
    charges_unavailable_trades: 0,
    net_of_charges: net,
    net_charges_complete: true,
    ...extra,
  }
}

/** A summary whose daily rows and period are for the requested custom range. */
function answer(range: { start: string; end: string } | undefined) {
  const base = summary()
  const start = range?.start ?? `${MONTH}-01`
  const end = range?.end ?? TODAY
  const rows = [row(`${MONTH}-02`, 100), row(`${MONTH}-03`, -40)].filter(
    (r) => r.period >= start && r.period <= end
  )
  return {
    ...base,
    period: { ...base.period, period: 'custom', start, end, reliable_since: '2020-01-01' },
    today: undefined,
    by_period: { daily: rows, weekly: [], monthly: [] },
    consistency: consistency(),
    risk: riskBlock(),
  }
}

function Harness({ initial }: { initial?: { start: string; end: string } }) {
  const [range, setRange] = useState(initial)
  return (
    <DiscretionaryPerformance
      period={range ? 'custom' : '30d'}
      range={range}
      rangeReady
      formatCurrency={money}
      isDarkMode={false}
      onSelectDate={(d) => setRange({ start: d, end: d })}
    />
  )
}

function renderIt(initial?: { start: string; end: string }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <Harness initial={initial} />
    </QueryClientProvider>
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  summaryFn.mockImplementation((_p: unknown, range: { start: string; end: string } | undefined) =>
    Promise.resolve(answer(range))
  )
})

describe('P&L calendar on the page', () => {
  it('shows the current month with daily net, counts and month totals that add up', async () => {
    renderIt()
    const day2 = await screen.findByTestId(`calendar-day-${MONTH}-02`, {}, SLOW)
    expect(day2).toHaveTextContent('Rs 100.00')
    expect(day2).toHaveTextContent('2 trades')
    expect(day2).toHaveTextContent('1W 1L')
    expect(day2).toHaveAttribute('data-status', 'profit')
    expect(screen.getByTestId(`calendar-day-${MONTH}-03`)).toHaveAttribute('data-status', 'loss')
    expect(screen.getByTestId(`calendar-day-${MONTH}-04`)).toHaveAttribute('data-status', 'none')
    const totals = screen.getByTestId('calendar-month-totals')
    // 100 + (-40) = 60: the month total is the sum of the daily values
    expect(within(totals).getByTestId('calendar-total-net')).toHaveTextContent('Rs 60.00')
    expect(within(totals).getByTestId('calendar-trading-days')).toHaveTextContent('2')
    expect(within(totals).getByTestId('calendar-profitable-days')).toHaveTextContent('1')
    expect(within(totals).getByTestId('calendar-losing-days')).toHaveTextContent('1')
    // the month is requested with the existing summary call: custom range, no live prices
    const calls = summaryFn.mock.calls.filter((c) => c[1]?.start === `${MONTH}-01`)
    expect(calls.length).toBeGreaterThan(0)
    expect(calls[0][0]).toBe('custom')
    expect(calls[0][1]).toEqual({ start: `${MONTH}-01`, end: TODAY })
    expect(calls[0][3]).toBe(false)
  })

  it('moves to the previous month and back, and cannot pass the current month', async () => {
    renderIt()
    await screen.findByTestId('calendar-month-totals', {}, SLOW)
    const title = screen.getByTestId('calendar-month')
    const here = title.textContent
    expect(screen.getByRole('button', { name: 'Next month' })).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: 'Previous month' }))
    await waitFor(() => expect(screen.getByTestId('calendar-month').textContent).not.toBe(here))
    const [y, m] = MONTH.split('-').map(Number)
    const prev = `${m === 1 ? y - 1 : y}-${String(m === 1 ? 12 : m - 1).padStart(2, '0')}`
    await waitFor(() =>
      expect(summaryFn.mock.calls.some((c) => c[1]?.start === `${prev}-01`)).toBe(true)
    )
    const prevCall = summaryFn.mock.calls.find((c) => c[1]?.start === `${prev}-01`)
    // a past month is requested whole
    expect(prevCall?.[1].end.slice(0, 7)).toBe(prev)
    await userEvent.click(screen.getByRole('button', { name: 'Next month' }))
    await waitFor(() => expect(screen.getByTestId('calendar-month').textContent).toBe(here))
  })

  it('opens the existing daily report of the day that is selected', async () => {
    renderIt()
    const day = `${MONTH}-03`
    await userEvent.click(await screen.findByTestId(`calendar-day-${day}`, {}, SLOW))
    await waitFor(() =>
      expect(
        summaryFn.mock.calls.some(
          (c) => c[0] === 'custom' && c[1]?.start === day && c[1]?.end === day
        )
      ).toBe(true)
    )
    // the report of that date is on screen, and the day is the selected one
    expect(await screen.findByText(/Report date:/)).toBeInTheDocument()
    expect(screen.getByTestId(`calendar-day-${day}`)).toHaveAttribute('aria-pressed', 'true')
  })

  it('keeps the broker, account and symbol filters on the month it requests', async () => {
    renderIt()
    await screen.findByTestId('calendar-month-totals', {}, SLOW)
    await userEvent.click(screen.getByRole('combobox', { name: 'Broker' }))
    await userEvent.click(await screen.findByRole('option', { name: 'zerodha' }))
    await waitFor(() =>
      expect(
        summaryFn.mock.calls.some(
          (c) => c[1]?.start === `${MONTH}-01` && c[2]?.broker === 'zerodha'
        )
      ).toBe(true)
    )
  })
})

describe('breakdown visibility on the page', () => {
  it('hides the breakdowns section when nothing has two categories to compare', async () => {
    summaryFn.mockResolvedValue({
      ...answer(undefined),
      by_symbol: [breakdownRow('RELIANCE')],
      by_side: [breakdownRow('LONG', { trades: 5 })],
      by_product: [breakdownRow('MIS')],
      breakdowns: tradingBreakdowns({
        time_of_day: [breakdownRow('09:15-10:00')],
        hold_bucket: [breakdownRow('15-60m')],
      }),
      monthly: null,
      weekday: null,
    })
    renderIt()
    await screen.findByTestId('discretionary-statistics')
    expect(screen.queryByTestId('performance-breakdowns')).not.toBeInTheDocument()
    expect(screen.queryByTestId('more-breakdowns')).not.toBeInTheDocument()
  })

  it('shows direction and product when each has more than one category', async () => {
    summaryFn.mockResolvedValue({
      ...answer(undefined),
      by_symbol: [breakdownRow('RELIANCE'), breakdownRow('INFY')],
      by_side: [breakdownRow('LONG', { trades: 5 }), breakdownRow('SHORT', { trades: 4 })],
      by_product: [breakdownRow('MIS'), breakdownRow('NRML')],
    })
    renderIt()
    const section = await screen.findByTestId('performance-breakdowns')
    expect(within(section).getByText('Long vs Short')).toBeInTheDocument()
    expect(within(section).getByText('By product')).toBeInTheDocument()
    expect(within(section).getByText('By symbol')).toBeInTheDocument()
  })

  it('keeps the trade-by-trade chart for a one-day report', async () => {
    renderIt({ start: TODAY, end: TODAY })
    expect(await screen.findByText('Trade-by-trade P&L')).toBeInTheDocument()
  })
})
