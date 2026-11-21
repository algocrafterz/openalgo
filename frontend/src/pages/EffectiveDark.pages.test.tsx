/**
 * Defect D1: the Analyze theme (.analyzer) is a dark palette that REPLACES the .dark class, while
 * the stored light/dark mode stays 'light'. Pages that paint a canvas chart from the stored mode
 * alone drew #333 axis text on a dark card. The pages must follow the effective appearance.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@/test/test-utils'

const status = vi.fn()
const chart = vi.hoisted(() => ({ opts: [] as { layout?: { textColor?: string } }[] }))

vi.mock('@/api/discretionaryPnl', () => ({
  discretionaryPnlApi: { status: () => status() },
}))
vi.mock('@/api/strategyPnl', () => ({
  strategyPnlApi: {
    compare: vi.fn().mockResolvedValue({
      status: 'success',
      mode: 'live',
      strategies: [],
      all_strategies: [],
    }),
    daily: vi.fn().mockResolvedValue({ status: 'success', mode: 'live', trades_count: 0 }),
    exportTrades: vi.fn(),
    excursions: vi.fn().mockResolvedValue({ status: 'success' }),
    listReports: vi.fn().mockResolvedValue([]),
  },
  strategyReportUrl: (f: string) => f,
}))
vi.mock('@/hooks/useOrderEventRefresh', () => ({ useOrderEventRefresh: vi.fn() }))
// The page hands its dark flag to the discretionary view, which hands it to the equity chart.
vi.mock('@/components/discretionary-pnl/DiscretionaryPerformance', () => ({
  DiscretionaryPerformance: ({ isDarkMode }: { isDarkMode: boolean }) => (
    <div data-testid="discretionary-view" data-dark={String(isDarkMode)} />
  ),
}))
vi.mock('@/components/strategy-pnl/EquityDrawdownChart', () => ({
  EquityDrawdownChart: () => null,
}))
vi.mock('@/components/strategy-pnl/StrategyReports', () => ({
  StrategyReports: () => <div data-testid="strategy-reports" />,
}))
vi.mock('@/components/DataHealthBanner', () => ({ DataHealthBanner: () => null }))
vi.mock('@/components/socket/SocketProvider', () => ({
  useSocketContext: () => ({
    socket: { on: () => {}, off: () => {} },
    playAlertSound: () => {},
  }),
}))
// lightweight-charts needs a canvas: a chart whose every method is a no-op records its options.
vi.mock('lightweight-charts', () => {
  const noop: () => unknown = () => new Proxy(() => undefined, handler)
  const handler: ProxyHandler<() => undefined> = {
    get: () => noop,
    apply: () => noop(),
  }
  return {
    BaselineSeries: 'Baseline',
    ColorType: { Solid: 'solid' },
    CrosshairMode: { Normal: 0 },
    LineStyle: { Dashed: 2 },
    createChart: (_el: unknown, opts: { layout?: { textColor?: string } }) => {
      chart.opts.push(opts)
      return new Proxy(() => undefined, handler)
    },
  }
})

const { useThemeStore } = await import('@/stores/themeStore')
const { default: StrategyDailyPerformance } = await import('./StrategyDailyPerformance')
const { default: PnLTracker } = await import('./PnLTracker')

const COMBOS = [
  ['light', 'live', false],
  ['dark', 'live', true],
  ['light', 'analyzer', true],
  ['dark', 'analyzer', true],
] as const

beforeEach(() => {
  vi.clearAllMocks()
  chart.opts.length = 0
  localStorage.clear()
  status.mockResolvedValue(true)
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ status: 'error', csrf_token: 'x', message: 'n/a' }),
    })
  )
})

describe('Performance page: the equity chart gets the effective dark flag', () => {
  for (const [mode, appMode, dark] of COMBOS) {
    it(`themeMode=${mode} appMode=${appMode} -> isDarkMode=${dark}`, async () => {
      useThemeStore.setState({ mode, appMode })
      window.history.pushState({}, '', '/strategy-pnl/daily?source=discretionary&period=30d')
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      render(
        <QueryClientProvider client={client}>
          <StrategyDailyPerformance />
        </QueryClientProvider>
      )
      const view = await screen.findByTestId('discretionary-view')
      expect(view).toHaveAttribute('data-dark', String(dark))
    })
  }
})

describe('PnL Tracker: the chart text colour follows the effective appearance', () => {
  // Same defect, same store: the chart sits on the themed page and paints #333 on dark otherwise.
  for (const [mode, appMode, dark] of COMBOS) {
    it(`themeMode=${mode} appMode=${appMode} -> ${dark ? 'light' : 'dark'} axis text`, async () => {
      useThemeStore.setState({ mode, appMode })
      render(<PnLTracker />)
      await waitFor(() => expect(chart.opts.length).toBeGreaterThan(0))
      expect(chart.opts.at(-1)?.layout?.textColor).toBe(dark ? '#a6adbb' : '#333')
    })
  }
})
