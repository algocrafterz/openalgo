import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ProvisionalOpenPosition } from '@/api/discretionaryPnl'
import {
  attributionCountsLine,
  openPositionProvisionalNote,
  provisionalCounts,
} from '@/lib/discretionaryPnlView'
import { emptyToday, position, provisional, summary } from '@/test/discretionaryFixtures'
import { render, screen } from '@/test/test-utils'
import { DiscretionaryPerformance } from './DiscretionaryPerformance'
import { OpenPositionsTable } from './DiscretionaryTables'
import { TodayLine } from './TodayPerformance'

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
      unresolved: vi.fn().mockResolvedValue({
        unresolved: [],
        unresolved_count: 0,
        orphans: [],
        orphans_count: 0,
      }),
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

const money = (v: number) => `Rs ${v.toFixed(2)}`

function provOpen(extra: Partial<ProvisionalOpenPosition> = {}): ProvisionalOpenPosition {
  return {
    broker: 'flattrade',
    account: 'FT1',
    symbol: 'TCS',
    exchange: 'NSE',
    product: 'CNC',
    direction: 'LONG',
    open_qty: 20,
    avg_entry: 3010,
    opened_at: '2026-11-17 10:00:00',
    realized_so_far: null,
    opening_unknown: false,
    ...extra,
  }
}

const graceFill = (id: number) => ({
  id,
  broker: 'flattrade',
  account: 'FT1',
  symbol: 'TCS',
  exchange: 'NSE',
  product: 'CNC',
  side: 'BUY',
  qty: 1,
  price: 1,
  fill_time: '2026-11-17 10:50:00',
  attributable_from: '2026-11-17 11:20:00',
  due: false,
  affects_open_position: false,
})

describe('open position provisional note', () => {
  it('notes provisional quantity of the same instrument and direction, without combining them', () => {
    const p = provisional({ open_positions: [provOpen()] })
    expect(openPositionProvisionalNote(position({ open_qty: 25 }), p)).toBe(
      '25 confirmed qty + 20 provisional qty awaiting attribution.'
    )
    render(
      <OpenPositionsTable
        positions={[position({ open_qty: 25 })]}
        formatCurrency={money}
        provisional={p}
      />
    )
    expect(screen.getByTestId('open-provisional-note')).toHaveTextContent(
      '25 confirmed qty + 20 provisional qty awaiting attribution.'
    )
  })

  it('shows no note for a different direction, product, account or when nothing is provisional', () => {
    const base = position({ open_qty: 25 })
    for (const other of [
      provOpen({ direction: 'SHORT' }),
      provOpen({ product: 'MIS' }),
      provOpen({ account: 'OTHER' }),
      provOpen({ symbol: 'INFY' }),
    ]) {
      expect(openPositionProvisionalNote(base, provisional({ open_positions: [other] }))).toBeNull()
    }
    expect(openPositionProvisionalNote(base, null)).toBeNull()
    render(
      <OpenPositionsTable
        positions={[base]}
        formatCurrency={money}
        provisional={provisional({ open_positions: [provOpen({ direction: 'SHORT' })] })}
      />
    )
    expect(screen.queryByTestId('open-provisional-note')).not.toBeInTheDocument()
  })
})

describe('awaiting and historical counts', () => {
  const pendingToday = emptyToday({
    pending_attribution: {
      ...emptyToday().pending_attribution,
      grace_count: 2,
      grace_fills: [graceFill(8), graceFill(9)],
    },
  })

  it('computes both counts from the payload and omits the historical clause at zero', () => {
    expect(provisionalCounts(provisional({ fills: 7 }), pendingToday.pending_attribution)).toEqual({
      awaiting: 2,
      historical: 5,
    })
    expect(
      provisionalCounts(provisional({ fills: 2 }), pendingToday.pending_attribution).historical
    ).toBe(0)
    expect(attributionCountsLine(2, 5)).toBe(
      '2 execution(s) awaiting attribution; 5 historical fill(s) outside the 30-minute window.'
    )
    expect(attributionCountsLine(2, 0)).toBe('2 execution(s) awaiting attribution.')
    expect(attributionCountsLine(0, 0)).toBeNull()
  })

  it('Today block distinguishes the groups and has no link to a panel; no clause without historical fills', () => {
    const { unmount } = render(
      <TodayLine today={pendingToday} formatCurrency={money} provisionalShown historicalFills={5} />
    )
    const pending = screen.getByTestId('today-pending')
    expect(pending).toHaveTextContent(
      '2 execution(s) awaiting attribution; 5 historical fill(s) outside the 30-minute window.'
    )
    expect(screen.queryByTestId('today-provisional-link')).not.toBeInTheDocument()
    unmount()
    render(
      <TodayLine today={pendingToday} formatCurrency={money} provisionalShown historicalFills={0} />
    )
    expect(screen.getByTestId('today-pending')).not.toHaveTextContent('historical')
    expect(screen.queryByTestId('today-provisional-link')).not.toBeInTheDocument()
  })
})

describe('no confirmed closed trades wording', () => {
  it('says confirmed when executions are pending or provisional, plain otherwise', () => {
    const pending = emptyToday({
      pending_attribution: {
        ...emptyToday().pending_attribution,
        grace_count: 1,
        grace_fills: [graceFill(1)],
      },
    })
    const { unmount } = render(<TodayLine today={pending} formatCurrency={money} />)
    expect(screen.getByTestId('today-summary')).toHaveTextContent(
      'no confirmed closed trades, 0 open.'
    )
    unmount()
    const { unmount: u2 } = render(
      <TodayLine today={emptyToday()} formatCurrency={money} provisionalShown />
    )
    expect(screen.getByTestId('today-summary')).toHaveTextContent(
      'no confirmed closed trades, 0 open.'
    )
    u2()
    render(<TodayLine today={emptyToday()} formatCurrency={money} />)
    expect(screen.getByTestId('today-summary')).toHaveTextContent('no closed trades, 0 open.')
    expect(screen.getByTestId('today-summary')).not.toHaveTextContent('confirmed')
  })
})

describe('report date wording', () => {
  beforeEach(() => summaryFn.mockReset())

  function renderPage(period: '1d' | '30d') {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return render(
      <QueryClientProvider client={client}>
        <DiscretionaryPerformance
          period={period}
          rangeReady
          formatCurrency={money}
          isDarkMode={false}
        />
      </QueryClientProvider>
    )
  }

  it('1d says latest day with closed trades, and market hours sit by the live prices switch', async () => {
    const s = summary()
    summaryFn.mockResolvedValue({
      ...s,
      period: { ...s.period, start: '2026-11-17', end: '2026-11-17' },
      market_hours_source: 'calendar',
    })
    renderPage('1d')
    expect(await screen.findByText(/Latest day with closed trades:/)).toBeInTheDocument()
    expect(screen.queryByText(/Report date:/)).not.toBeInTheDocument()
    const hours = screen.getByTestId('market-hours-source')
    expect(hours).toHaveTextContent('(market hours from calendar)')
    expect(screen.getByText(/Latest day with closed trades:/).closest('p')).not.toHaveTextContent(
      'market hours'
    )
    expect(hours.parentElement).toContainElement(screen.getByLabelText('Fetch live prices'))
  })

  it('a multi-day period keeps Report period', async () => {
    const s = summary()
    summaryFn.mockResolvedValue({
      ...s,
      period: { ...s.period, start: '2026-11-01', end: '2026-11-17' },
    })
    renderPage('30d')
    expect(await screen.findByText('Report period:')).toBeInTheDocument()
    expect(screen.queryByText(/Latest day with closed trades:/)).not.toBeInTheDocument()
  })
})
