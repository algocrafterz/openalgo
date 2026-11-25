import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  IntradayBlock,
  IntradayPosition,
  IntradayTotals,
  IntradayUnmatched,
} from '@/api/discretionaryPnl'
import { emptyToday, provisional, summary } from '@/test/discretionaryFixtures'
import { render, screen, userEvent, within } from '@/test/test-utils'
import { DiscretionaryPerformance } from './DiscretionaryPerformance'
import { IntradayPanel } from './IntradayPanel'

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

function pos(extra: Partial<IntradayPosition> = {}): IntradayPosition {
  return {
    broker: 'flattrade',
    account: 'FT1',
    exchange: 'NSE',
    symbol: 'TCS',
    product: 'MIS',
    direction: 'LONG',
    quantity: 45,
    avg_entry: 2510.2222,
    opened_at: '2026-11-25 10:00:42',
    opened_session: '2026-11-25',
    status: 'partially_reconciled',
    confirmed_fills: 1,
    pending_fills: 2,
    realized_so_far_confirmed: 0,
    realized_so_far_provisional: 0,
    ltp: 2503,
    price_status: 'live',
    unrealized: -325,
    unrealized_reason: null,
    opening_unknown: false,
    not_checked_against_broker: true,
    ...extra,
  }
}

function totals(extra: Partial<IntradayTotals> = {}): IntradayTotals {
  return {
    confirmed_realized: 0,
    provisional_realized: 0,
    unrealized: -325,
    priced_positions: 1,
    unpriced_positions: 0,
    confirmed_charges: 0,
    estimated_charges: 0,
    charges_unavailable_legs: 0,
    total_gross: -325,
    total_net: -325,
    total_complete: true,
    partial_reason: null,
    basis: 'gross, before charges; net is gross minus realized-leg charges',
    confirmed_legs: 0,
    provisional_legs: 0,
    open_positions: 1,
    confirmed_fills: 1,
    pending_fills: 2,
    ...extra,
  }
}

function block(extra: Partial<IntradayBlock> = {}): IntradayBlock {
  return {
    session_date: '2026-11-25',
    definition: 'd',
    basis: 'gross, before charges; charges shown separately',
    not_checked_against_broker: true,
    quotes_requested: true,
    positions: [pos()],
    closed: [],
    unmatched: [],
    totals: totals(),
    ...extra,
  }
}

const unmatched = (extra: Partial<IntradayUnmatched> = {}): IntradayUnmatched => ({
  fill_ref: 4,
  broker: 'flattrade',
  account: 'FT1',
  exchange: 'NSE',
  symbol: 'TCS',
  product: 'MIS',
  side: 'SELL',
  qty: 30,
  price: 2395.2,
  fill_time: '2026-11-18 10:16:38',
  session_date: '2026-11-18',
  reason: 'earlier_session_unresolved',
  reason_text:
    "An earlier-session execution still awaits attribution. It is not added to today's position.",
  today: false,
  ...extra,
})

describe('IntradayPanel', () => {
  it('shows the combined position, status, price status and the broker caveat', () => {
    render(<IntradayPanel intraday={block()} formatCurrency={money} />)
    expect(
      screen.getByRole('heading', { name: /^Today intraday \(25 Nov 2026\)$/ })
    ).toBeInTheDocument()
    const row = screen.getByTestId('intraday-position-row')
    expect(row).toHaveTextContent('TCS')
    expect(row).toHaveTextContent('45')
    expect(row).toHaveTextContent('2510.22')
    expect(within(row).getByTestId('intraday-status-partially_reconciled')).toHaveTextContent(
      'Partially reconciled'
    )
    expect(row).toHaveTextContent('Live')
    expect(row).toHaveTextContent('1 confirmed, 2 pending')
    expect(row).toHaveTextContent('not checked against broker')
    expect(screen.getByTestId('intraday-position-unrealized')).toHaveTextContent('Rs -325.00')
    expect(screen.getByTestId('intraday-explanation')).toHaveTextContent(
      'Provisional figures can change'
    )
  })

  it.each([
    ['confirmed', 'Confirmed'],
    ['provisional', 'Provisional'],
    ['partially_reconciled', 'Partially reconciled'],
  ] as const)('labels a %s position', (status, label) => {
    render(
      <IntradayPanel intraday={block({ positions: [pos({ status })] })} formatCurrency={money} />
    )
    expect(screen.getByTestId(`intraday-status-${status}`)).toHaveTextContent(label)
  })

  it('shows a missing price as n/a, never as zero, and marks the total partial', () => {
    const b = block({
      positions: [
        pos({
          unrealized: null,
          ltp: null,
          price_status: 'unavailable',
          unrealized_reason: 'No price is available.',
        }),
      ],
      totals: totals({
        unrealized: null,
        priced_positions: 0,
        unpriced_positions: 1,
        total_gross: 0,
        total_complete: false,
        partial_reason: '1 open position(s) have no reliable price and are left out.',
      }),
    })
    render(<IntradayPanel intraday={b} formatCurrency={money} />)
    expect(screen.getByTestId('intraday-position-unrealized')).toHaveTextContent('n/a')
    expect(screen.getByTestId('intraday-position-unrealized')).not.toHaveTextContent('0.00')
    expect(screen.getByTestId('intraday-unrealized')).toHaveTextContent('n/a')
    expect(screen.getByText(/Total intraday \(partial\)/)).toBeInTheDocument()
    // nothing priced and nothing closed: no total, not a zero
    expect(screen.getByTestId('intraday-total')).toHaveTextContent('n/a')
    expect(screen.getByTestId('intraday-basis')).not.toHaveTextContent('Net of charges')
    expect(screen.getByText(/excludes 1 unpriced/)).toBeInTheDocument()
    expect(screen.getByTestId('intraday-partial')).toHaveTextContent('no reliable price')
  })

  it('keeps a partial total that rests on a closed leg as a figure', () => {
    const b = block({
      positions: [pos({ unrealized: null, ltp: null, price_status: 'unavailable' })],
      totals: totals({
        unrealized: null,
        unpriced_positions: 1,
        priced_positions: 0,
        confirmed_realized: 40,
        confirmed_legs: 1,
        total_gross: 40,
        total_net: 35,
        total_complete: false,
        partial_reason: '1 open position(s) have no reliable price and are left out.',
      }),
    })
    render(<IntradayPanel intraday={b} formatCurrency={money} />)
    expect(screen.getByTestId('intraday-total')).toHaveTextContent('Rs 40.00')
    expect(screen.getByText(/Total intraday \(partial\)/)).toBeInTheDocument()
  })

  it('shows confirmed and provisional realized and estimated charges separately', () => {
    const b = block({
      positions: [],
      closed: [
        {
          broker: 'flattrade',
          account: 'FT1',
          exchange: 'NSE',
          symbol: 'SBIN',
          product: 'MIS',
          direction: 'LONG',
          quantity: 10,
          entry_price: 100,
          exit_price: 102,
          opened_at: '2026-11-25 09:30:00',
          closed_at: '2026-11-25 09:40:00',
          realized_gross: 20,
          charges: 3.5,
          charges_estimated: true,
          status: 'provisional',
          confirmed_fills: 1,
          pending_fills: 1,
        },
      ],
      totals: totals({
        confirmed_realized: 40,
        provisional_realized: 20,
        estimated_charges: 3.5,
        confirmed_charges: 5,
        total_gross: 40 + 20 - 325,
        provisional_legs: 1,
        confirmed_legs: 1,
        unrealized: -325,
      }),
    })
    render(<IntradayPanel intraday={b} formatCurrency={money} />)
    expect(screen.getByTestId('intraday-confirmed-realized')).toHaveTextContent('Rs 40.00')
    expect(screen.getByTestId('intraday-provisional-realized')).toHaveTextContent('Rs 20.00')
    expect(screen.getByTestId('intraday-estimated-charges')).toHaveTextContent('Rs 3.50')
    const row = screen.getByTestId('intraday-closed-row')
    expect(within(row).getByTestId('intraday-status-provisional')).toBeInTheDocument()
    expect(row).toHaveTextContent('est. charges')
    expect(screen.getByTestId('intraday-basis')).toHaveTextContent('gross, before charges')
    expect(screen.getByTestId('intraday-basis')).toHaveTextContent(
      '1 confirmed and 1 provisional closed leg(s)'
    )
  })

  it('lists a today unmatched execution separately from the positions', () => {
    const today = unmatched({
      reason: 'orphan_sell',
      reason_text: 'No opening trade is known for this sale, so it is not matched to a position.',
      today: true,
      session_date: '2026-11-25',
      fill_time: '2026-11-25 10:16:38',
    })
    render(<IntradayPanel intraday={block({ unmatched: [today] })} formatCurrency={money} />)
    const list = screen.getByTestId('intraday-unmatched')
    expect(list).toHaveTextContent('Not matched to a position (1)')
    expect(within(list).getByTestId('intraday-unmatched-row')).toHaveTextContent('No opening trade')
    expect(screen.queryByTestId('intraday-earlier')).not.toBeInTheDocument()
    expect(screen.getAllByTestId('intraday-position-row')).toHaveLength(1)
  })

  it('puts earlier-session executions in a collapsed block, as information only', async () => {
    const user = userEvent.setup()
    render(
      <IntradayPanel
        intraday={block({ unmatched: [unmatched(), unmatched({ fill_ref: 5 })] })}
        formatCurrency={money}
      />
    )
    const box = screen.getByTestId('intraday-earlier') as HTMLDetailsElement
    expect(box.open).toBe(false)
    expect(screen.getByTestId('intraday-earlier-toggle')).toHaveTextContent(
      "Not included in today's position (2 earlier-session executions)"
    )
    // collapsed by default: executions and note are not visible
    for (const row of within(box).getAllByTestId('intraday-earlier-row')) {
      expect(row).not.toBeVisible()
    }
    expect(screen.getByTestId('intraday-earlier-note')).not.toBeVisible()

    await user.click(screen.getByTestId('intraday-earlier-toggle'))
    expect(box.open).toBe(true)
    expect(screen.getByTestId('intraday-earlier-note')).toBeVisible()
    expect(screen.getByTestId('intraday-earlier-note')).toHaveTextContent(
      "Earlier session, not part of today's position"
    )
    expect(screen.getByTestId('intraday-earlier-note')).toHaveTextContent(
      "do not change today's quantity, entry price, profit or loss"
    )
    const rows = within(box).getAllByTestId('intraday-earlier-row')
    expect(rows).toHaveLength(2)
    for (const row of rows) expect(row).toBeVisible()
    // not mixed into the "not matched" list, and no alarm wording
    expect(screen.queryByTestId('intraday-unmatched')).not.toBeInTheDocument()
    expect(box.textContent ?? '').not.toMatch(/error|warning|exposure|problem|failed/i)
  })

  it('keeps today unmatched items visible while earlier-session items are collapsed', () => {
    const today = unmatched({
      fill_ref: 9,
      reason: 'orphan_sell',
      reason_text: 'No opening trade is known for this sale, so it is not matched to a position.',
      today: true,
      session_date: '2026-11-25',
      fill_time: '2026-11-25 10:16:38',
    })
    render(
      <IntradayPanel intraday={block({ unmatched: [today, unmatched()] })} formatCurrency={money} />
    )
    expect(screen.getByTestId('intraday-unmatched')).toBeVisible()
    expect(screen.getByTestId('intraday-unmatched-row')).toBeVisible()
    expect(screen.getByTestId('intraday-earlier-toggle')).toHaveTextContent(
      '(1 earlier-session execution)'
    )
    expect(screen.getByTestId('intraday-earlier-row')).not.toBeVisible()
  })

  it('names the session date in the heading', () => {
    render(<IntradayPanel intraday={block()} formatCurrency={money} />)
    expect(screen.getByTestId('intraday-heading')).toHaveTextContent('Today intraday (25 Nov 2026)')
  })

  it.each([
    ['unavailable', 'Price not available'],
    ['stale', 'Price is out of date'],
    ['cached', 'Price is out of date'],
    ['not_requested', 'Prices not fetched'],
  ])('shows a plain reason, not a code, when the price is %s', (status, text) => {
    const b = block({
      positions: [
        pos({
          unrealized: null,
          ltp: status === 'stale' ? 2500 : null,
          price_status: status as IntradayPosition['price_status'],
          unrealized_reason: 'x',
        }),
      ],
    })
    render(<IntradayPanel intraday={b} formatCurrency={money} />)
    const cell = screen.getByTestId('intraday-position-unrealized')
    expect(cell).toHaveTextContent('n/a')
    expect(screen.getByTestId('intraday-position-no-price')).toHaveTextContent(text)
    expect(cell.textContent ?? '').not.toMatch(/\b0\.00\b|unavailable|not_requested|stale|cached/)
  })

  it('shows no reason line when the position is priced', () => {
    render(<IntradayPanel intraday={block()} formatCurrency={money} />)
    expect(screen.queryByTestId('intraday-position-no-price')).not.toBeInTheDocument()
    expect(screen.getByTestId('intraday-total')).toHaveTextContent('Rs -325.00')
    expect(screen.getByText(/Total intraday:/)).toBeInTheDocument()
    expect(screen.queryByText(/partial/)).not.toBeInTheDocument()
  })

  it('renders nothing when the server sends no block or an empty one', () => {
    const { container, rerender } = render(
      <IntradayPanel intraday={undefined} formatCurrency={money} />
    )
    expect(container).toBeEmptyDOMElement()
    rerender(<IntradayPanel intraday={null} formatCurrency={money} />)
    expect(container).toBeEmptyDOMElement()
    rerender(
      <IntradayPanel
        intraday={block({ positions: [], closed: [], unmatched: [] })}
        formatCurrency={money}
      />
    )
    expect(container).toBeEmptyDOMElement()
  })
})

describe('intraday section on the page', () => {
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

  it('is shown for the current (1d) view, and no provisional panel or pointer is rendered', async () => {
    summaryFn.mockResolvedValue({
      ...summary(),
      intraday: block(),
      provisional: provisional(),
    })
    renderPage('1d')
    expect(await screen.findByTestId('intraday-panel')).toBeInTheDocument()
    expect(screen.getByTestId('intraday-total')).toHaveTextContent('Rs -325.00')
    expect(screen.queryByTestId('provisional-panel')).not.toBeInTheDocument()
    expect(screen.queryByTestId('provisional-intraday-pointer')).not.toBeInTheDocument()
  })

  it('is hidden for a history period', async () => {
    summaryFn.mockResolvedValue({ ...summary(), intraday: block() })
    renderPage('30d')
    expect(await screen.findByText('Report period:')).toBeInTheDocument()
    expect(screen.queryByTestId('intraday-panel')).not.toBeInTheDocument()
  })

  it('leaves the confirmed totals untouched', async () => {
    const s = summary()
    summaryFn.mockResolvedValue({ ...s, intraday: block() })
    renderPage('1d')
    await screen.findByTestId('intraday-panel')
    expect(screen.getByTestId('intraday-confirmed-realized')).toHaveTextContent('Rs 0.00')
    // the page's own confirmed realized figure is still the server's
    expect(summaryFn).toHaveBeenCalledTimes(1)
  })

  describe('date presentation', () => {
    const closedOn = (day: string) => ({
      ...summary(),
      period: { ...summary().period, period: '1d', start: day, end: day },
    })

    it('says which dates the cards and the intraday section cover when they differ', async () => {
      summaryFn.mockResolvedValue({
        ...closedOn('2026-11-23'),
        today: emptyToday({ session_date: '2026-11-25' }),
        intraday: block(),
      })
      renderPage('1d')
      const note = await screen.findByTestId('date-presentation-note')
      expect(note).toHaveTextContent('Today intraday (25 Nov 2026) is shown below.')
      expect(note).toHaveTextContent('Cards, charts and statistics above cover 23 Nov 2026.')
      expect(screen.getByText('Latest day with closed trades:')).toBeInTheDocument()
    })

    it('still names the dates, without claiming anything about intraday, when there is no block', async () => {
      summaryFn.mockResolvedValue({
        ...closedOn('2026-11-23'),
        today: emptyToday({ session_date: '2026-11-25' }),
        intraday: null,
      })
      renderPage('1d')
      const note = await screen.findByTestId('date-presentation-note')
      expect(note).toHaveTextContent(
        'Cards, charts and statistics cover 23 Nov 2026, not today (25 Nov 2026).'
      )
      expect(note).not.toHaveTextContent('shown below')
    })

    it('adds nothing when the closed-trade day is today', async () => {
      summaryFn.mockResolvedValue({
        ...closedOn('2026-11-25'),
        today: emptyToday({ session_date: '2026-11-25' }),
        intraday: block(),
      })
      renderPage('1d')
      await screen.findByTestId('intraday-panel')
      expect(screen.queryByTestId('date-presentation-note')).not.toBeInTheDocument()
    })

    it('adds nothing for a history period', async () => {
      summaryFn.mockResolvedValue({
        ...closedOn('2026-11-23'),
        today: emptyToday({ session_date: '2026-11-25' }),
        intraday: block(),
      })
      renderPage('30d')
      await screen.findByText(/Report (period|date):/)
      expect(screen.queryByTestId('date-presentation-note')).not.toBeInTheDocument()
    })
  })
})
