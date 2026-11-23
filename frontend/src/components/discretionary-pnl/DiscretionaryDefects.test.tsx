/**
 * Regression tests for the Discretionary dashboard defects D1 to D5 (UI only):
 *  D1 effective dark flag (Analyze is a dark theme with no .dark class)
 *  D2 green small text passes AA in light theme
 *  D3 red small text passes AA in dark theme
 *  D4 amber caution text passes AA in light theme
 *  D5 the ledger-wide "unresolved fill(s)" backend line is not shown next to the range banner
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { NEGATIVE_TEXT, POSITIVE_TEXT, pnlTextClass } from '@/lib/discretionaryPnlView'
import { isEffectiveDark } from '@/lib/effectiveTheme'
import {
  consistency,
  costsBlock,
  dataQuality,
  emptyToday,
  excursions,
  freshSyncStamp,
  monthRows,
  pointsRow,
  riskBlock,
  summary,
  todayClosed,
  tradeManagement,
  tradingBreakdowns,
  unresolvedInRange,
  weekdayRows,
} from '@/test/discretionaryFixtures'
import { render, screen, within } from '@/test/test-utils'

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
// The canvas needs a real browser; it only shows which dark flag the chart was given.
vi.mock('./EquityCanvas', () => ({
  DRAWDOWN_COLOR: '#d97706',
  EquityCanvas: ({ isDarkMode }: { isDarkMode: boolean }) => (
    <div data-testid="equity-chart" data-dark={String(isDarkMode)} />
  ),
}))

const { DiscretionaryPerformance } = await import('./DiscretionaryPerformance')

const money = (v: number) => `Rs ${v.toFixed(2)}`

function renderIt(isDarkMode = false, period = '30d') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <DiscretionaryPerformance
        period={period}
        rangeReady
        formatCurrency={money}
        isDarkMode={isDarkMode}
      />
    </QueryClientProvider>
  )
}

function dailyRows(n: number) {
  const base = summary().by_period.daily[0]
  return Array.from({ length: n }, (_, i) => {
    const date = `2026-11-${String(i + 1).padStart(2, '0')}`
    return { ...base, period: date, label: date, start: date, end: date }
  })
}

/** Every block of the dashboard filled in: winners, losers, drawdown, a low-sample note, caveats. */
function rich(extra: Partial<ReturnType<typeof summary>> = {}) {
  const base = summary()
  return summary({
    by_period: { ...base.by_period, daily: dailyRows(12) },
    consistency: consistency(),
    risk: riskBlock(),
    breakdowns: tradingBreakdowns(),
    monthly: monthRows(),
    weekday: weekdayRows(),
    trade_management: tradeManagement(),
    costs: costsBlock(),
    points_by_instrument: [pointsRow()],
    data_quality: dataQuality(),
    sample: {
      completed_trades: 2,
      trading_days: 1,
      min_trades: 30,
      min_days: 10,
      sufficient: false,
    },
    today: emptyToday({ closed_positions: [todayClosed()], completed_positions: 1, net: 20 }),
    ...extra,
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  summaryFn.mockResolvedValue(rich())
  excursionsFn.mockResolvedValue(excursions())
})

// ------------------------------------------------------------------------------------- D1

describe('D1: effective dark flag', () => {
  const table: [string, string, boolean][] = [
    ['light', 'live', false],
    ['light', 'analyzer', true],
    ['light', 'sandbox', false],
    ['dark', 'live', true],
    ['dark', 'analyzer', true],
    ['dark', 'sandbox', true],
  ]
  for (const [mode, appMode, dark] of table) {
    it(`themeMode=${mode} appMode=${appMode} -> dark=${dark}`, () => {
      expect(isEffectiveDark(mode as 'light' | 'dark', appMode)).toBe(dark)
    })
  }

  it('hands the flag it is given to the equity chart', async () => {
    renderIt(true)
    // the Equity curve and Trade-by-trade P&L both get the flag
    const charts = await screen.findAllByTestId('equity-chart')
    for (const c of charts) expect(c).toHaveAttribute('data-dark', 'true')
  })
  it('hands the light flag to the equity chart in light mode', async () => {
    renderIt(false)
    const charts = await screen.findAllByTestId('equity-chart')
    for (const c of charts) expect(c).toHaveAttribute('data-dark', 'false')
  })
})

// ------------------------------------------------------------------------- D2, D3, D4 classes

const POS = 'text-green-700 dark:text-green-600 [.analyzer_&]:text-green-600'

/** Class tokens of an element, split on whitespace. */
const tokens = (el: Element) => (el.getAttribute('class') ?? '').split(/\s+/).filter(Boolean)

describe('D2 to D4: class strings of the shared helpers', () => {
  it('uses the darker green in light theme and keeps the old green in dark and Analyze', () => {
    expect(POSITIVE_TEXT).toBe(POS)
    expect(pnlTextClass(1)).toBe(POS)
  })
  it('keeps red-600 in light and Analyze (remapped in index.css) and lightens it in dark', () => {
    expect(NEGATIVE_TEXT).toBe('text-red-600 dark:text-red-400')
    expect(pnlTextClass(-1)).toBe('text-red-600 dark:text-red-400')
  })
  it('adds no colour for a zero figure', () => {
    expect(pnlTextClass(0)).toBe('')
  })
})

describe('D2 to D4: rendered dashboard keeps no failing small-text class', () => {
  it('has green, red and amber text, each with its theme partners', async () => {
    const { container } = renderIt()
    await screen.findByTestId('discretionary-summary')
    const all = [...container.querySelectorAll('[class]')]
    // 3xl headline figures pass at large size and keep their classes; svg icons are not text
    const small = (el: Element) => !tokens(el).includes('text-3xl') && el.tagName !== 'svg'

    const greens = all.filter(
      (e) => small(e) && tokens(e).some((t) => /^text-green-[67]00$/.test(t))
    )
    const reds = all.filter((e) => small(e) && tokens(e).includes('text-red-600'))
    const ambers = all.filter(
      (e) => small(e) && tokens(e).some((t) => /^text-amber-[567]00$/.test(t))
    )
    // not vacuous
    expect(greens.length).toBeGreaterThan(0)
    expect(reds.length).toBeGreaterThan(0)
    expect(ambers.length).toBeGreaterThan(0)

    for (const e of all.filter(small)) {
      const t = tokens(e)
      if (t.includes('text-green-600')) {
        throw new Error(`bare text-green-600 on small text: ${e.outerHTML.slice(0, 160)}`)
      }
      if (t.includes('text-green-700')) {
        expect(t).toContain('dark:text-green-600')
        expect(t).toContain('[.analyzer_&]:text-green-600')
      }
      if (t.includes('text-red-600')) expect(t).toContain('dark:text-red-400')
      if (t.includes('text-amber-600')) {
        throw new Error(`bare text-amber-600 on small text: ${e.outerHTML.slice(0, 160)}`)
      }
      if (t.some((x) => /^text-amber-[567]00$/.test(x))) {
        expect(t).toContain('text-amber-700')
        expect(t).toContain('dark:text-amber-500')
        expect(t).toContain('[.analyzer_&]:text-amber-500')
      }
    }
  })

  it('shows the sample note as plain muted text, not as an amber warning', async () => {
    renderIt()
    const note = await screen.findByTestId('discretionary-sample-note')
    const t = tokens(note)
    expect(t).toContain('text-muted-foreground')
    expect(t.some((c) => c.includes('amber'))).toBe(false)
    expect(note.textContent).not.toMatch(/low sample|indicative/i)
  })

  it('colours the drawdown and the losing days red with a dark-theme partner', async () => {
    const { container } = renderIt()
    await screen.findByTestId('discretionary-summary')
    // the drawdown now lives in Trading statistics, not in the summary header
    const stats = screen.getByTestId('discretionary-statistics')
    const red = [...stats.querySelectorAll('.text-red-600')]
    expect(red.length).toBeGreaterThan(0)
    for (const e of red) expect(tokens(e)).toContain('dark:text-red-400')
    // losing-days counter in the insights
    const losing = [...container.querySelectorAll('span')].filter(
      (e) => tokens(e).includes('text-red-600') && /losing/.test(e.textContent ?? '')
    )
    for (const e of losing) expect(tokens(e)).toContain('dark:text-red-400')
  })

  it('leaves the 3xl headline figure exactly as it was', async () => {
    summaryFn.mockResolvedValue(
      rich({ realized_net_of_charges: 120, net: 120, realized_gross: 130 })
    )
    const { container } = renderIt()
    await screen.findByTestId('discretionary-summary')
    const big = [...container.querySelectorAll('.text-3xl')].filter((e) =>
      tokens(e).some((t) => /^text-(green|red)-600$/.test(t))
    )
    for (const e of big) {
      expect(tokens(e)).not.toContain('text-green-700')
      expect(tokens(e)).not.toContain('dark:text-red-400')
    }
  })
})

// ---------------------------------------------------------------------- contrast arithmetic

/** Tailwind v4 theme values (node_modules/tailwindcss/theme.css) and the index.css tokens. */
type Oklch = [number, number, number]
const COLOURS: Record<string, Oklch> = {
  'green-700': [0.527, 0.154, 150.069],
  'green-600': [0.627, 0.194, 149.214],
  'red-600': [0.577, 0.245, 27.325],
  'red-400': [0.704, 0.191, 22.216],
  'amber-700': [0.555, 0.163, 48.998],
  'amber-500': [0.769, 0.188, 70.08],
  'analyzer-red': [0.7, 0.15, 25], // index.css: .analyzer .text-red-600 / .dark\:text-red-400
}
const BACKGROUNDS: Record<string, Oklch> = {
  white: [1, 0, 0], // :root --background / --card
  'dark page': [0.145, 0, 0], // .dark --background  (#0a0a0a)
  'dark card': [0.205, 0, 0], // .dark --card        (#171717)
  'analyzer page': [0.16, 0.02, 280], // .analyzer --background
  'analyzer card': [0.2, 0.025, 280], // .analyzer --card
  'sandbox page': [0.97, 0.03, 85], // .sandbox --background
  'sandbox card': [0.99, 0.02, 85], // .sandbox --card
}

function oklchToRgb([L, C, h]: Oklch): [number, number, number] {
  const a = C * Math.cos((h * Math.PI) / 180)
  const b = C * Math.sin((h * Math.PI) / 180)
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
  const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
  const s = (L - 0.0894841775 * a - 1.291485548 * b) ** 3
  const lin = [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ]
  const enc = (x: number) => {
    const c = Math.min(1, Math.max(0, x))
    return Math.round(255 * (c <= 0.0031308 ? 12.92 * c : 1.055 * c ** (1 / 2.4) - 0.055))
  }
  return [enc(lin[0]), enc(lin[1]), enc(lin[2])]
}
function luminance(rgb: number[]): number {
  const f = (v: number) =>
    v / 255 <= 0.03928 ? v / 255 / 12.92 : ((v / 255 + 0.055) / 1.055) ** 2.4
  return 0.2126 * f(rgb[0]) + 0.7152 * f(rgb[1]) + 0.0722 * f(rgb[2])
}
function ratio(fg: string, bg: string): number {
  const x = luminance(oklchToRgb(COLOURS[fg]))
  const y = luminance(oklchToRgb(BACKGROUNDS[bg]))
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05)
}

describe('contrast of the chosen colours (WCAG AA, 4.5:1 for small text)', () => {
  it('reads the backgrounds the way the browser did (sanity of the conversion)', () => {
    expect(oklchToRgb(BACKGROUNDS.white)).toEqual([255, 255, 255])
    expect(oklchToRgb(BACKGROUNDS['dark page'])).toEqual([10, 10, 10])
    expect(oklchToRgb(BACKGROUNDS['dark card'])).toEqual([23, 23, 23])
    expect(oklchToRgb(COLOURS['green-600'])).toEqual([0, 166, 62])
    expect(ratio('green-600', 'white')).toBeCloseTo(3.22, 2) // the defect, as measured
    expect(ratio('red-600', 'dark page')).toBeCloseTo(4.15, 2)
    expect(ratio('red-600', 'dark card')).toBeCloseTo(3.76, 2)
  })

  const PASS: [string, string][] = [
    // light theme
    ['green-700', 'white'],
    ['red-600', 'white'],
    ['amber-700', 'white'],
    // standard dark theme
    ['green-600', 'dark page'],
    ['green-600', 'dark card'],
    ['red-400', 'dark page'],
    ['red-400', 'dark card'],
    ['amber-500', 'dark page'],
    ['amber-500', 'dark card'],
    // Analyze theme: green and amber use their own partner; red is the index.css remap
    ['green-600', 'analyzer page'],
    ['green-600', 'analyzer card'],
    ['amber-500', 'analyzer page'],
    ['amber-500', 'analyzer card'],
    ['analyzer-red', 'analyzer page'],
    ['analyzer-red', 'analyzer card'],
    // sandbox theme is light: green-700 and amber-700 pass on both of its backgrounds
    ['green-700', 'sandbox page'],
    ['green-700', 'sandbox card'],
    ['amber-700', 'sandbox page'],
    ['amber-700', 'sandbox card'],
  ]
  for (const [fg, bg] of PASS) {
    it(`${fg} on ${bg} is at least 4.5:1`, () => {
      expect(ratio(fg, bg)).toBeGreaterThanOrEqual(4.5)
    })
  }

  it('the colours that were replaced did fail where they were used', () => {
    expect(ratio('green-600', 'white')).toBeLessThan(4.5)
    expect(ratio('red-600', 'dark card')).toBeLessThan(4.5)
    expect(ratio('amber-700', 'white')).toBeGreaterThanOrEqual(4.5)
    expect(ratio('amber-500', 'white')).toBeLessThan(4.5) // so amber-500 is dark/Analyze only
    expect(ratio('red-400', 'white')).toBeLessThan(4.5) // so red-400 is dark only
  })
})

// ------------------------------------------------------------------------------------- D5

const LEDGER = '5 unresolved fill(s) are excluded from P&L until attributed.'
const CHARGES =
  '1 trade(s) have no charge estimate (instrument not modelled); net P&L counts their charges as 0 and is not complete.'
const ORPHANS =
  '2 fill(s) could not be placed (opening not in the ledger or invalid) and are excluded.'

const HEALTHY_DQ = {
  awaiting_attribution: 0,
  not_placed: 0,
  prices: { requested: true, open_positions: 1, statuses: {}, missing: 0, stale: 0 },
}
const NO_GAPS = {
  last_successful_sync: freshSyncStamp(),
  runs_by_day: {},
  days_without_sync: [],
}

describe('D5: one plain-language data warning', () => {
  it('shows nothing, and no server warning list, when no problem count is above 0', async () => {
    summaryFn.mockResolvedValue(
      rich({
        unresolved_in_range: unresolvedInRange({ unresolved_fills: 0, orphan_fills: 0, total: 0 }),
        data_quality: dataQuality(HEALTHY_DQ),
        sync_health: NO_GAPS,
        warnings: [LEDGER, CHARGES, ORPHANS],
      })
    )
    renderIt(false, '1d')
    await screen.findByTestId('discretionary-summary')
    expect(screen.queryByTestId('data-warning')).not.toBeInTheDocument()
    expect(screen.queryByTestId('discretionary-warnings')).not.toBeInTheDocument()
    expect(screen.queryByTestId('unresolved-warning')).not.toBeInTheDocument()
    expect(screen.queryByTestId('data-quality')).not.toBeInTheDocument()
    expect(screen.queryByText(/ledger sync|unresolved fill\(s\)/i)).not.toBeInTheDocument()
  })

  it('tells the trader when the last sync with the broker did not complete, and has no details button', async () => {
    summaryFn.mockResolvedValue(
      rich({
        unresolved_in_range: unresolvedInRange({ unresolved_fills: 0, orphan_fills: 0, total: 0 }),
        data_quality: dataQuality(HEALTHY_DQ),
        sync_health: {
          last_successful_sync: null,
          runs_by_day: { '2026-11-17': { error: 2 } },
          days_without_sync: [],
        },
        warnings: [],
      })
    )
    renderIt(false, '1d')
    const banner = await screen.findByTestId('data-warning')
    expect(banner).toHaveTextContent(
      'Your last sync with the broker did not complete. Recent trades may be missing. Use Refresh to try again.'
    )
    expect(banner.textContent).not.toMatch(/error|status|HTTP|broker_error|no_auth/i)
    expect(within(banner).queryByRole('button', { name: 'View details' })).not.toBeInTheDocument()
  })

  it('shows nothing when the newest sync of the day completed', async () => {
    summaryFn.mockResolvedValue(
      rich({
        unresolved_in_range: unresolvedInRange({ unresolved_fills: 0, orphan_fills: 0, total: 0 }),
        data_quality: dataQuality(HEALTHY_DQ),
        sync_health: {
          last_successful_sync: '2026-11-17 10:00',
          runs_by_day: { '2026-11-16': { error: 1 }, '2026-11-17': { ok: 3, error: 1 } },
          days_without_sync: [],
        },
        warnings: [],
      })
    )
    renderIt(false, '1d')
    await screen.findByTestId('discretionary-summary')
    expect(screen.queryByTestId('data-warning')).not.toBeInTheDocument()
  })

  it('shows one quiet note with the count and a details button when fills are left out', async () => {
    summaryFn.mockResolvedValue(
      rich({
        unresolved_in_range: unresolvedInRange({ unresolved_fills: 5, orphan_fills: 0, total: 5 }),
        data_quality: dataQuality(HEALTHY_DQ),
        sync_health: NO_GAPS,
        warnings: [CHARGES, LEDGER],
      })
    )
    renderIt(false, '7d')
    const note = await screen.findByTestId('unmatched-fills-note')
    expect(note).toHaveTextContent(
      '5 fills in this period are not counted, so P&L and trade count may be incomplete.'
    )
    expect(within(note).getByRole('button', { name: 'View details' })).toBeInTheDocument()
    expect(note).not.toHaveTextContent(/attribut|ledger|sync|reconcil|collected/i)
    // a coverage note, not the amber warning banner
    expect(screen.queryByTestId('data-warning')).not.toBeInTheDocument()
    expect(screen.queryByTestId('discretionary-warnings')).not.toBeInTheDocument()
  })

  it('warns about missing prices only when prices were requested', async () => {
    const missing = { ...HEALTHY_DQ.prices, missing: 2 }
    summaryFn.mockResolvedValue(
      rich({
        unresolved_in_range: unresolvedInRange({ unresolved_fills: 0, orphan_fills: 0, total: 0 }),
        data_quality: dataQuality({ ...HEALTHY_DQ, prices: missing }),
        sync_health: NO_GAPS,
      })
    )
    renderIt(false, '7d')
    const banner = await screen.findByTestId('data-warning')
    expect(banner).toHaveTextContent(
      '2 open positions have no current price, so open P&L may be incomplete.'
    )
    expect(within(banner).queryByRole('button', { name: 'View details' })).not.toBeInTheDocument()
  })

  it('shows no price warning when prices were not requested', async () => {
    summaryFn.mockResolvedValue(
      rich({
        unresolved_in_range: unresolvedInRange({ unresolved_fills: 0, orphan_fills: 0, total: 0 }),
        data_quality: dataQuality({
          ...HEALTHY_DQ,
          prices: { requested: false, open_positions: 1, statuses: {}, missing: 0, stale: 0 },
        }),
        sync_health: NO_GAPS,
      })
    )
    renderIt(false, '7d')
    await screen.findByTestId('discretionary-summary')
    expect(screen.queryByTestId('data-warning')).not.toBeInTheDocument()
  })
})

describe('data warning: cases from earlier server warnings', () => {
  const healthy = () =>
    rich({
      unresolved_in_range: unresolvedInRange({ unresolved_fills: 0, orphan_fills: 0, total: 0 }),
      data_quality: dataQuality(HEALTHY_DQ),
      sync_health: NO_GAPS,
      positions: [],
      trades: [],
    })
  const istDay = (offset: number) =>
    new Date(Date.now() + 5.5 * 3600 * 1000 - offset * 86400000).toISOString().slice(0, 10)
  const WORDS = /ledger|attribut|sync|reconcil|fills collected|Risk card|\blegs?\b/i

  it('stays absent on a healthy report', async () => {
    summaryFn.mockResolvedValue(healthy())
    renderIt(false, '7d')
    await screen.findByTestId('discretionary-summary')
    expect(screen.queryByTestId('data-warning')).not.toBeInTheDocument()
  })

  it('warns about an intraday position left open from an earlier day', async () => {
    const base = healthy()
    summaryFn.mockResolvedValue({
      ...base,
      positions: [
        {
          ...summary().positions[0],
          product: 'MIS',
          opened_at: `${istDay(3)}T10:00:00`,
          opening_unknown: false,
        },
      ],
      period: { ...base.period, end: istDay(0) },
    } as never)
    renderIt(false, '7d')
    const banner = await screen.findByTestId('data-warning')
    expect(banner).toHaveTextContent('still open from an earlier day')
    expect(banner.textContent).not.toMatch(WORDS)
    expect(screen.getAllByTestId('data-warning')).toHaveLength(1)
  })

  it('warns about a trade with no recorded entry', async () => {
    const base = healthy()
    const trade = summary().trades[0]
    summaryFn.mockResolvedValue({
      ...base,
      trades: [{ ...trade, opening_unknown: true }],
      trades_total: 1,
      trades_shown: 1,
    } as never)
    renderIt(false, '7d')
    const banner = await screen.findByTestId('data-warning')
    expect(banner).toHaveTextContent('no recorded entry')
    expect(banner.textContent).not.toMatch(WORDS)
  })

  it('warns when the last collection is older than the selected range reaches', async () => {
    const base = healthy()
    const day = new Date(Date.now() + 5.5 * 3600 * 1000).getUTCDay()
    if (day === 0 || day === 6) return // the rule applies on weekdays only
    summaryFn.mockResolvedValue({
      ...base,
      sync_health: { ...NO_GAPS, last_successful_sync: `${istDay(6)} 09:30` },
      period: { ...base.period, end: istDay(0) },
    } as never)
    renderIt(false, '7d')
    const banner = await screen.findByTestId('data-warning')
    expect(banner).toHaveTextContent(`Trades after ${istDay(6)} may be missing`)
    expect(banner.textContent).not.toMatch(WORDS)
  })
})
