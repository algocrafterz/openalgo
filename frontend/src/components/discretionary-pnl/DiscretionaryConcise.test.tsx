import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  awaitingAttributionText,
  BREAKDOWN_TABS,
  DAILY_CHART_CAPTION,
  EQUITY_CAPTION_DETAIL,
  equityCurveBasis,
  MONTHLY_TABLE_DETAIL,
  MONTHLY_TABLE_NOTE,
  materialDataWarning,
  NET_DEFINITION_DETAIL,
  NET_EQUITY_CAPTION,
  netDefinitionText,
  periodProfile,
  reliableStartNotice,
  sampleNote,
  UNMATCHED_FILLS_REASON,
  unmatchedFillsNote,
} from '@/lib/discretionaryPnlView'
import { dataQuality, excursions, summary, unresolvedInRange } from '@/test/discretionaryFixtures'
import { render, screen, userEvent, within } from '@/test/test-utils'
import { CALENDAR_DETAIL } from './PnlCalendar'

const summaryFn = vi.fn()
const excursionsFn = vi.fn()

vi.mock('@/api/discretionaryPnl', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/discretionaryPnl')>()
  return {
    ...actual,
    discretionaryPnlApi: {
      status: vi.fn(),
      summary: (...a: unknown[]) => summaryFn(...a),
      excursions: (...a: unknown[]) => excursionsFn(...a),
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

const { DiscretionaryPerformance } = await import('./DiscretionaryPerformance')

const money = (v: number) => `Rs ${v.toFixed(2)}`
const words = (t: string | null | undefined) => (t ?? '').trim().split(/\s+/).filter(Boolean).length
const MAX_CAPTION_WORDS = 14

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <DiscretionaryPerformance
        period="custom"
        range={{ start: '2026-11-01', end: '2026-11-20' }}
        rangeReady
        formatCurrency={money}
        isDarkMode={false}
        onSelectDate={() => {}}
      />
    </QueryClientProvider>
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  excursionsFn.mockResolvedValue(excursions())
})

describe('unmatched fills note', () => {
  it('uses the singular for one fill and the plural otherwise', () => {
    expect(unmatchedFillsNote(1)).toBe(
      '1 fill in this period is not counted, so P&L and trade count may be incomplete.'
    )
    expect(unmatchedFillsNote(5)).toBe(
      '5 fills in this period are not counted, so P&L and trade count may be incomplete.'
    )
  })

  it('keeps the technical reason in the tooltip text, not in the line', () => {
    expect(UNMATCHED_FILLS_REASON).toContain('could not be matched to a manual trade')
    expect(unmatchedFillsNote(3)).not.toMatch(/matched|attribut|ledger/i)
  })

  it('shows under the headline figures, with the reason as a tooltip', async () => {
    summaryFn.mockResolvedValue(summary({ unresolved_in_range: unresolvedInRange() }))
    renderPage()
    const note = await screen.findByTestId('unmatched-fills-note')
    expect(note).toHaveTextContent('3 fills in this period are not counted')
    expect(note).toHaveAttribute('title', UNMATCHED_FILLS_REASON)
    expect(screen.queryByTestId('data-warning')).not.toBeInTheDocument()
    const headline = screen.getByTestId('summary-net')
    // after the headline figures, before the calendar
    expect(headline.compareDocumentPosition(note) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    const calendar = await screen.findByTestId('pnl-calendar')
    expect(note.compareDocumentPosition(calendar) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('shows the singular for one fill', async () => {
    summaryFn.mockResolvedValue(
      summary({
        unresolved_in_range: unresolvedInRange({ unresolved_fills: 1, orphan_fills: 0, total: 1 }),
      })
    )
    renderPage()
    expect(await screen.findByTestId('unmatched-fills-note')).toHaveTextContent(
      '1 fill in this period is not counted'
    )
  })

  it('is hidden when the range has no unmatched fills', async () => {
    summaryFn.mockResolvedValue(
      summary({
        unresolved_in_range: unresolvedInRange({ unresolved_fills: 0, orphan_fills: 0, total: 0 }),
      })
    )
    renderPage()
    await screen.findByTestId('summary-net')
    expect(screen.queryByTestId('unmatched-fills-note')).not.toBeInTheDocument()
  })

  it('still opens the details list', async () => {
    summaryFn.mockResolvedValue(summary({ unresolved_in_range: unresolvedInRange() }))
    renderPage()
    const note = await screen.findByTestId('unmatched-fills-note')
    await userEvent.click(within(note).getByRole('button', { name: 'View details' }))
    expect(await screen.findByTestId('unresolved-dialog')).toBeInTheDocument()
  })
})

describe('coverage and limitation messages are kept', () => {
  it('keeps the key words of every message on the preserve list', async () => {
    summaryFn.mockResolvedValue(
      summary({
        unresolved_in_range: unresolvedInRange(),
        charges_unavailable_trades: 2,
        sample: { completed_trades: 12, trading_days: 3 } as never,
        period: { ...summary().period, reliable_since: '2026-11-10' },
      })
    )
    renderPage()
    const net = await screen.findByTestId('summary-net')
    expect(net).toHaveTextContent('est. charges')
    expect(net).toHaveTextContent('without a charge estimate')
    expect(screen.getByTestId('unmatched-fills-note')).toHaveTextContent('not counted')
    expect(screen.getByTestId('reliable-start-notice')).toHaveTextContent('reliable from')
    expect(await screen.findByTestId('calendar-legend')).toHaveTextContent('estimated charges')
    expect(screen.getByTestId('net-equity-caption')).toHaveTextContent('estimated charges')
    expect(screen.getByTestId('net-equity-caption')).toHaveTextContent(/\d+ completed trade/)
    expect(screen.getByTestId('discretionary-sample-note')).toHaveTextContent(
      /\d+ completed trades? over \d+ trading days?/
    )
  })

  it('keeps the MAE / MFE coverage count line', async () => {
    summaryFn.mockResolvedValue(summary())
    renderPage()
    await userEvent.click(await screen.findByTestId('advanced-analysis-toggle'))
    expect(await screen.findByTestId('excursions-coverage')).toHaveTextContent(
      /\d+ of \d+ completed positions with 1-minute price data/
    )
  })

  it('keeps the sync failure and the other material warnings', () => {
    const failed = materialDataWarning({
      unresolved_in_range: null,
      data_quality: dataQuality({ awaiting_attribution: 0, not_placed: 0 }),
      sync_health: {
        last_successful_sync: null,
        runs_by_day: { '2026-11-17': { error: 2 } },
        days_without_sync: [],
      },
    } as never)
    expect(failed).toContain('sync')
    expect(failed).toContain('did not complete')
  })

  it('keeps the reliable-start notice words', () => {
    const text = reliableStartNotice('2026-11-01', '2026-11-10', (d) => d)
    expect(text).toContain('reliable from')
  })

  it('keeps the incomplete-charges disclosure in the badge tooltip', () => {
    expect(netDefinitionText('x')).toContain('estimated charges')
    expect(awaitingAttributionText(5, 5)).toContain('not counted')
  })
})

describe('captions are short and the details moved to tooltips', () => {
  it('keeps every exported caption constant within the word limit', () => {
    const captions: Record<string, string> = {
      NET_EQUITY_CAPTION,
      DAILY_CHART_CAPTION,
      MONTHLY_TABLE_NOTE,
      netDefinition: netDefinitionText('x'),
      awaiting: awaitingAttributionText(5, 5),
      basis: equityCurveBasis({ used: 9, incompleteDays: 0 }),
      sample: sampleNote({ completed_trades: 12, trading_days: 3 } as never)?.text ?? '',
      session: periodProfile({ period: '1d', closeDates: 1, months: 1 }).note ?? '',
      short: periodProfile({ period: '30d', closeDates: 3, months: 1 }).note ?? '',
      month: periodProfile({ period: '30d', closeDates: 10, months: 1 }).note ?? '',
    }
    for (const o of BREAKDOWN_TABS) captions[`tab-${o.key}`] = o.note
    for (const [name, text] of Object.entries(captions)) {
      expect(words(text), `${name}: ${text}`).toBeLessThanOrEqual(MAX_CAPTION_WORDS)
    }
  })

  it('keeps the rendered captions of the dashboard within the word limit', async () => {
    summaryFn.mockResolvedValue(
      summary({ sample: { completed_trades: 12, trading_days: 3 } as never })
    )
    renderPage()
    await screen.findByTestId('calendar-month-totals')
    const ids = [
      'calendar-caption',
      'equity-axis-note',
      'net-equity-caption',
      'discretionary-sample-note',
    ]
    for (const id of ids) {
      const el = screen.getByTestId(id)
      expect(words(el.textContent), `${id}: ${el.textContent}`).toBeLessThanOrEqual(
        MAX_CAPTION_WORDS
      )
    }
  })

  it('moves the calendar details to the tooltip and keeps the legend meanings', async () => {
    summaryFn.mockResolvedValue(summary())
    renderPage()
    const legend = await screen.findByTestId('calendar-legend')
    expect(screen.getByTestId('calendar-caption')).toHaveTextContent(
      'Net P&L by close day, after estimated charges.'
    )
    expect(legend).toHaveTextContent('Grey: no trades.')
    expect(legend).toHaveTextContent('Dashed amber: data may be missing.')
    expect(legend).toHaveAttribute('title', CALENDAR_DETAIL)
    expect(CALENDAR_DETAIL).toContain('Open positions are not included')
    expect(CALENDAR_DETAIL).toContain('Select a day to open its report')
    expect(legend).not.toHaveTextContent('Select a day')
  })

  it('carries the moved details in tooltips', async () => {
    summaryFn.mockResolvedValue(summary())
    renderPage()
    expect(await screen.findByTestId('net-equity-caption')).toHaveAttribute(
      'title',
      EQUITY_CAPTION_DETAIL
    )
    expect(EQUITY_CAPTION_DETAIL).toContain('Unrealized P&L is not included')
    expect(EQUITY_CAPTION_DETAIL).toContain('not an account balance or a return on capital')
    expect(NET_DEFINITION_DETAIL).toContain('measured on net P&L')
    expect(MONTHLY_TABLE_DETAIL).toContain('Partial exits')
    for (const o of BREAKDOWN_TABS.filter((t) => t.table === 'how')) {
      expect(o.detail, o.key).toBeTruthy()
    }
  })
})
