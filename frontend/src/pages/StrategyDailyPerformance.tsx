import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  BaselineSeries,
  ColorType,
  createChart,
  type IChartApi,
  type ISeriesApi,
  LineSeries,
  LineStyle,
  type UTCTimestamp,
} from 'lightweight-charts'
import { AlertTriangle, Download, Gauge, Loader2, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router'
import {
  type BrokerCharges,
  type CapitalView,
  type DailyPnlPoint,
  type DateRange,
  type EquityPoint,
  type PerformancePeriod,
  type SymbolPnl,
  strategyPnlApi,
} from '@/api/strategyPnl'
import { DataHealthBanner } from '@/components/DataHealthBanner'
import { StrategyModeBadge } from '@/components/StrategyModeBadge'
import { CloseBreakdowns } from '@/components/strategy-pnl/CloseBreakdowns'
import { EdgeCheckCard } from '@/components/strategy-pnl/EdgeCheckCard'
import { HoldBuckets } from '@/components/strategy-pnl/HoldBuckets'
import { HoldStatsRow } from '@/components/strategy-pnl/HoldStats'
import { ExcursionCard, GradeBadge, InsightsPanel } from '@/components/strategy-pnl/InsightsPanel'
import { PortfolioComparison } from '@/components/strategy-pnl/PortfolioComparison'
import { MetricCard } from '@/components/strategy-pnl/MetricCard'
import { SessionSummary } from '@/components/strategy-pnl/SessionSummary'
import { SessionTrades } from '@/components/strategy-pnl/SessionTrades'
import { StrategyFilterPicker } from '@/components/strategy-pnl/StrategyFilterPicker'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { EmptyState } from '@/components/ui/empty-state'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { useDebouncedCommit } from '@/hooks/useDebouncedCommit'
import { useOrderEventRefresh } from '@/hooks/useOrderEventRefresh'
import {
  type CapitalMode,
  capitalProblem,
  clearStoredCapital,
  loadStoredCapital,
  parseCapital,
  planCapitalForMode,
  REFERENCE_CAPITAL_PER_STRATEGY,
  saveStoredCapital,
  scaleRupeeFigures,
} from '@/lib/capital'
import {
  onlyStrategy,
  parseStrategyFilter,
  type StrategyFilter,
  writeStrategyFilter,
} from '@/lib/strategyFilter'
import {
  chargesHint,
  edgeShortLabel,
  fmtDate,
  fmtDuration,
  fmtR,
  pnlClass,
  rCoverage,
  rExcludedText,
  slippageHint,
  todayIst,
} from '@/lib/strategyPnlView'
import { cn, makeFormatCurrency } from '@/lib/utils'
import { useAuthStore } from '@/stores/authStore'
import { onModeChange, useThemeStore } from '@/stores/themeStore'
import { showToast } from '@/utils/toast'

const PERIODS: { value: PerformancePeriod; label: string }[] = [
  { value: '1d', label: '1D' },
  { value: '7d', label: '7D' },
  { value: '15d', label: '15D' },
  { value: '30d', label: '30D' },
  { value: '90d', label: '90D' },
  { value: 'ytd', label: 'YTD' },
  { value: 'all', label: 'All' },
  { value: 'custom', label: 'Custom' },
]

const PERIOD_VALUES = new Set<string>(PERIODS.map((p) => p.value))
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/

/** "30 Oct 2026" or "05 Oct 2026 - 20 Oct 2026" for the range the server resolved. */
function rangeLabel(start: string | undefined, end: string | undefined): string {
  if (!start || !end) return ''
  return start === end ? fmtDate(start) : `${fmtDate(start)} - ${fmtDate(end)}`
}

function validRange(range: DateRange): boolean {
  return ISO_DATE.test(range.start) && ISO_DATE.test(range.end) && range.start <= range.end
}

const POLL_MS = 60_000

/** Colour for a charges-to-profit share: half the profit is a warning, all of it a loss. */
function chargesDragClass(pct: number | null | undefined): string {
  if (pct == null) return 'text-muted-foreground'
  if (pct >= 100) return 'text-red-600'
  if (pct >= 50) return 'text-amber-600'
  return ''
}

function fmtRatio(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return value.toFixed(2)
}

function SymbolTable({
  title,
  rows,
  formatCurrency,
}: {
  title: string
  rows: SymbolPnl[]
  formatCurrency: (value: number) => string
}) {
  return (
    <div>
      <div className="text-sm font-medium mb-2">{title}</div>
      {rows.length === 0 ? (
        <div className="text-sm text-muted-foreground">None in this period</div>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Symbol</TableHead>
              <TableHead className="text-right">Trades</TableHead>
              <TableHead className="text-right">Win Rate</TableHead>
              <TableHead className="text-right">Net P&amp;L</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((r) => (
              <TableRow key={r.symbol}>
                <TableCell className="font-medium">{r.symbol}</TableCell>
                <TableCell className="text-right font-mono">{r.trades}</TableCell>
                <TableCell className="text-right font-mono">{r.win_rate.toFixed(0)}%</TableCell>
                <TableCell className={cn('text-right font-mono', pnlClass(r.net_pnl))}>
                  {formatCurrency(r.net_pnl)}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  )
}

function BrokerChargesCard({
  rows,
  assumed,
  formatCurrency,
}: {
  rows: BrokerCharges[]
  assumed: boolean
  formatCurrency: (value: number) => string
}) {
  if (rows.length === 0) return null
  return (
    <Card>
      <CardHeader>
        <CardTitle>Charges by broker</CardTitle>
      </CardHeader>
      <CardContent className="py-0">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Broker</TableHead>
              <TableHead className="text-right">Trades</TableHead>
              <TableHead className="text-right">Gross P&amp;L</TableHead>
              <TableHead className="text-right">Est. charges</TableHead>
              <TableHead className="text-right">Charges % of profit</TableHead>
              <TableHead className="text-right">After charges</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((r) => (
              <TableRow key={r.broker}>
                <TableCell className="font-medium">
                  {r.label}
                  {r.assumed && (
                    <span className="ml-2 text-xs text-amber-600" title="Assumed fee schedule">
                      assumed rates
                    </span>
                  )}
                </TableCell>
                <TableCell className="text-right font-mono">{r.trades}</TableCell>
                <TableCell className={cn('text-right font-mono', pnlClass(r.gross))}>
                  {formatCurrency(r.gross)}
                </TableCell>
                <TableCell className="text-right font-mono text-muted-foreground">
                  {r.est_costs == null ? '—' : formatCurrency(r.est_costs)}
                </TableCell>
                <TableCell
                  className={cn('text-right font-mono', chargesDragClass(r.charges_pct_of_gross))}
                >
                  {r.charges_pct_of_gross == null ? '—' : `${r.charges_pct_of_gross.toFixed(1)}%`}
                </TableCell>
                <TableCell className={cn('text-right font-mono', pnlClass(r.net_after_costs ?? 0))}>
                  {r.net_after_costs == null ? '—' : formatCurrency(r.net_after_costs)}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        {rows
          .filter((r) => r.attention)
          .map((r) => (
            <p key={`attn-${r.broker}`} className="text-xs text-amber-600 py-1">
              {r.label}: {r.attention}
            </p>
          ))}
        <p className="text-xs text-muted-foreground py-2">
          Each broker's trades are priced on that broker's own brokerage; taxes and exchange fees
          are the same everywhere.
          {assumed && ' Rows marked "assumed rates" use a typical discount-broker schedule.'}
        </p>
      </CardContent>
    </Card>
  )
}

function CalendarHeatmap({
  dailyPnl,
  formatCurrency,
  onSelectDay,
}: {
  dailyPnl: DailyPnlPoint[]
  formatCurrency: (v: number) => string
  onSelectDay: (day: string) => void
}) {
  const { grid, maxAbs } = useMemo(() => {
    if (dailyPnl.length === 0) return { grid: [] as (DailyPnlPoint | null)[][], maxAbs: 1 }
    const maxAbsPnl = Math.max(1, ...dailyPnl.map((d) => Math.abs(d.pnl)))
    const firstDate = new Date(`${dailyPnl[0].date}T00:00:00`)
    // Monday-first week, so the grid reads left-to-right by calendar week.
    const startOffset = (firstDate.getDay() + 6) % 7
    const cells: (DailyPnlPoint | null)[] = Array(startOffset).fill(null)
    cells.push(...dailyPnl)
    while (cells.length % 7 !== 0) cells.push(null)
    const weeks: (DailyPnlPoint | null)[][] = []
    for (let w = 0; w < cells.length / 7; w++) {
      weeks.push(cells.slice(w * 7, w * 7 + 7))
    }
    return { grid: weeks, maxAbs: maxAbsPnl }
  }, [dailyPnl])

  if (grid.length === 0) {
    return <p className="text-sm text-muted-foreground py-3">No daily history in this range yet.</p>
  }

  return (
    <div className="flex gap-1 overflow-x-auto pb-2">
      {grid.map((week, wi) => (
        <div key={`week-${wi}`} className="flex flex-col gap-1">
          {week.map((day, di) => {
            if (!day) {
              return <div key={`empty-${wi}-${di}`} className="h-3.5 w-3.5" />
            }
            const intensity = Math.min(1, Math.abs(day.pnl) / maxAbs)
            const bg =
              day.pnl > 0
                ? `rgba(34, 197, 94, ${0.15 + intensity * 0.65})`
                : day.pnl < 0
                  ? `rgba(239, 68, 68, ${0.15 + intensity * 0.65})`
                  : 'rgba(148, 163, 184, 0.2)'
            return (
              <button
                type="button"
                key={day.date}
                title={`${day.date}: ${formatCurrency(day.pnl)}`}
                aria-label={`Open ${day.date}, P&L ${formatCurrency(day.pnl)}`}
                className="h-3.5 w-3.5 rounded-sm p-0 border-0 cursor-pointer focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring"
                style={{ backgroundColor: bg }}
                onClick={() => onSelectDay(day.date)}
              />
            )
          })}
        </div>
      ))}
    </div>
  )
}

const NET_LINE_COLOR = '#6366f1'

function EquityDrawdownChart({
  equityCurve,
  equityCurveNet,
  isDarkMode,
  formatCurrency,
}: {
  equityCurve: EquityPoint[]
  equityCurveNet: EquityPoint[]
  isDarkMode: boolean
  formatCurrency: (v: number) => string
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const equitySeriesRef = useRef<ISeriesApi<'Baseline'> | null>(null)
  const drawdownSeriesRef = useRef<ISeriesApi<'Baseline'> | null>(null)
  const netSeriesRef = useRef<ISeriesApi<'Line'> | null>(null)
  // Bumped each time the chart is rebuilt (theme or currency change), so the
  // data effect below reloads the new, empty series.
  const [chartVersion, setChartVersion] = useState(0)

  useEffect(() => {
    if (!containerRef.current) return
    const container = containerRef.current
    if (chartRef.current) {
      chartRef.current.remove()
      chartRef.current = null
    }

    const chart = createChart(container, {
      width: container.offsetWidth,
      height: 400,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: isDarkMode ? '#a6adbb' : '#333',
        panes: {
          enableResize: true,
          separatorColor: isDarkMode ? 'rgba(166, 173, 187, 0.2)' : 'rgba(0, 0, 0, 0.2)',
        },
      },
      grid: {
        vertLines: { visible: false },
        horzLines: {
          color: isDarkMode ? 'rgba(166, 173, 187, 0.1)' : 'rgba(0, 0, 0, 0.08)',
        },
      },
      rightPriceScale: {
        borderColor: isDarkMode ? 'rgba(166, 173, 187, 0.2)' : 'rgba(0, 0, 0, 0.2)',
        scaleMargins: { top: 0.1, bottom: 0.1 },
      },
      timeScale: {
        borderColor: isDarkMode ? 'rgba(166, 173, 187, 0.2)' : 'rgba(0, 0, 0, 0.2)',
        timeVisible: false,
      },
    })

    const equitySeries = chart.addSeries(
      BaselineSeries,
      {
        baseValue: { type: 'price', price: 0 },
        topLineColor: '#22c55e',
        topFillColor1: 'rgba(34, 197, 94, 0.28)',
        topFillColor2: 'rgba(34, 197, 94, 0.02)',
        bottomLineColor: '#ef4444',
        bottomFillColor1: 'rgba(239, 68, 68, 0.02)',
        bottomFillColor2: 'rgba(239, 68, 68, 0.28)',
        lineWidth: 2,
        priceScaleId: 'right',
        priceFormat: { type: 'custom', formatter: (p: number) => formatCurrency(p) },
      },
      0
    )

    const netSeries = chart.addSeries(
      LineSeries,
      {
        color: NET_LINE_COLOR,
        lineWidth: 2,
        lineStyle: LineStyle.Dashed,
        priceScaleId: 'right',
        lastValueVisible: false,
        priceLineVisible: false,
        priceFormat: { type: 'custom', formatter: (p: number) => formatCurrency(p) },
      },
      0
    )

    const drawdownSeries = chart.addSeries(
      BaselineSeries,
      {
        baseValue: { type: 'price', price: 0 },
        topLineColor: '#eab308',
        topFillColor1: 'rgba(234, 179, 8, 0)',
        topFillColor2: 'rgba(234, 179, 8, 0)',
        bottomLineColor: '#eab308',
        bottomFillColor1: 'rgba(234, 179, 8, 0.04)',
        bottomFillColor2: 'rgba(234, 179, 8, 0.30)',
        lineWidth: 2,
        priceScaleId: 'right',
        priceFormat: { type: 'custom', formatter: (p: number) => formatCurrency(p) },
      },
      1
    )

    const panes = chart.panes()
    if (panes.length > 1) {
      panes[0].setHeight(300)
      panes[1].setHeight(100)
    }

    chartRef.current = chart
    equitySeriesRef.current = equitySeries
    drawdownSeriesRef.current = drawdownSeries
    netSeriesRef.current = netSeries
    setChartVersion((v) => v + 1)

    const handleResize = () => {
      chart.applyOptions({ width: container.offsetWidth })
    }
    window.addEventListener('resize', handleResize)

    return () => {
      window.removeEventListener('resize', handleResize)
      chart.remove()
      chartRef.current = null
    }
  }, [isDarkMode, formatCurrency])

  useEffect(() => {
    if (chartVersion === 0 || !equitySeriesRef.current || !drawdownSeriesRef.current) return
    const toTime = (d: string) =>
      Math.floor(new Date(`${d}T00:00:00Z`).getTime() / 1000) as UTCTimestamp

    const equityData = equityCurve.map((p) => ({ time: toTime(p.date), value: p.value }))
    let runningMax = Number.NEGATIVE_INFINITY
    const drawdownData = equityCurve.map((p) => {
      runningMax = Math.max(runningMax, p.value)
      return { time: toTime(p.date), value: p.value - runningMax }
    })

    if (equityData.length > 0) {
      equitySeriesRef.current.setData(equityData)
      drawdownSeriesRef.current.setData(drawdownData)
      netSeriesRef.current?.setData(
        equityCurveNet.map((p) => ({ time: toTime(p.date), value: p.value }))
      )
      chartRef.current?.timeScale().fitContent()
    }
  }, [equityCurve, equityCurveNet, chartVersion])

  return <div ref={containerRef} className="relative" style={{ height: '400px' }} />
}

function EquityLegend({ coverage }: { coverage: { covered: number; total: number } | undefined }) {
  const partial = !!coverage && coverage.covered < coverage.total
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 mt-2 text-xs text-muted-foreground">
      <span className="flex items-center gap-1">
        <span className="inline-block h-0.5 w-4 bg-green-500" />
        Equity
      </span>
      <span className="flex items-center gap-1">
        <span
          className="inline-block w-4 border-t-2 border-dashed"
          style={{ borderColor: NET_LINE_COLOR }}
        />
        After est. charges
      </span>
      {partial && (
        <span>
          charges estimated for {coverage.covered} of {coverage.total} trades
        </span>
      )}
    </div>
  )
}

function fmtPct(value: number | null): string {
  return value === null ? '—' : `${value.toFixed(2)}%`
}

/** Scaled what-if rupee figures and percent-of-total-capital figures for a capital per strategy. */
function CapitalViewBlock({
  view,
  formatCurrency,
}: {
  view: CapitalView
  formatCurrency: (value: number) => string
}) {
  const inr = (v: number) => v.toLocaleString('en-IN')
  const total = view.total_capital ?? view.capital * view.strategies
  const basis = `of Rs ${inr(total)}`
  const tone = (v: number | null) => ({
    positive: v !== null && v > 0,
    negative: v !== null && v < 0,
  })
  const money = (v: number | null) => (v === null ? '—' : formatCurrency(v))
  const s = view.scaled
  const scaleText = view.scale === null ? '' : ` (${Number(view.scale.toPrecision(4))}x)`
  return (
    <div className="space-y-2">
      <h3 className="text-sm font-medium">
        If you had allocated Rs {inr(view.capital)} per strategy ({view.strategies}{' '}
        {view.strategies === 1 ? 'strategy' : 'strategies'}, Rs {inr(total)} in total)
      </h3>
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
        <MetricCard label="Net P&L (scaled)" value={money(s.net_profit)} {...tone(s.net_profit)} />
        {s.net_after_costs !== null && (
          <MetricCard
            label="After est. charges (scaled)"
            value={money(s.net_after_costs)}
            {...tone(s.net_after_costs)}
          />
        )}
        <MetricCard
          label="Max drawdown (scaled)"
          value={money(s.max_drawdown === null ? null : Math.abs(s.max_drawdown))}
        />
        <MetricCard label="Best day (scaled)" value={money(s.best_day)} {...tone(s.best_day)} />
        <MetricCard label="Worst day (scaled)" value={money(s.worst_day)} {...tone(s.worst_day)} />
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
        <MetricCard
          label="Return (gross)"
          value={fmtPct(view.return_pct)}
          hint={basis}
          {...tone(view.return_pct)}
        />
        <MetricCard
          label="Return (net of est. charges)"
          value={fmtPct(view.return_net_pct)}
          hint={basis}
          {...tone(view.return_net_pct)}
        />
        <MetricCard
          label="Max drawdown (% of capital)"
          value={fmtPct(view.max_drawdown_pct_of_capital)}
          hint={basis}
          {...tone(view.max_drawdown_pct_of_capital)}
        />
        <MetricCard
          label="Best day"
          value={fmtPct(view.best_day_pct)}
          hint={basis}
          {...tone(view.best_day_pct)}
        />
        <MetricCard
          label="Worst day"
          value={fmtPct(view.worst_day_pct)}
          hint={basis}
          {...tone(view.worst_day_pct)}
        />
      </div>
      <p className="text-xs font-medium text-amber-600 dark:text-amber-400">
        Every rupee figure on this page (cards, tables, charts, trades) is scaled to this amount.
        Quantities and prices are as recorded, and the CSV export is not scaled.
      </p>
      <p className="text-xs text-muted-foreground">
        This scales the recorded results as if each strategy had been traded with Rs{' '}
        {inr(view.capital)} instead of Rs {inr(view.reference_capital)}
        {scaleText}. It is a straight-line estimate: real quantities, lot sizes, charges and
        slippage would not scale exactly. Percentages do not change with the amount. Recorded P&L is
        unchanged.
      </p>
    </div>
  )
}

export default function StrategyDailyPerformance() {
  const { user } = useAuthStore()
  const formatCurrency = useMemo(() => makeFormatCurrency(user?.broker), [user?.broker])
  const { mode: themeMode } = useThemeStore()
  const isDarkMode = themeMode === 'dark'
  const queryClient = useQueryClient()

  // Period, range and strategy live in the URL, so a refresh or a shared link
  // reopens the same view.
  const [searchParams, setSearchParams] = useSearchParams()
  const rawPeriod = searchParams.get('period') ?? '7d'
  const period = (PERIOD_VALUES.has(rawPeriod) ? rawPeriod : '7d') as PerformancePeriod
  const filter = useMemo(() => parseStrategyFilter(searchParams), [searchParams])
  const appliedRange = useMemo<DateRange>(
    () => ({ start: searchParams.get('start') ?? '', end: searchParams.get('end') ?? '' }),
    [searchParams]
  )
  const [draftRange, setDraftRange] = useState<DateRange>(appliedRange)
  // Back/forward navigation changes the URL range; keep the pickers in step.
  useEffect(() => setDraftRange(appliedRange), [appliedRange])
  const isCustom = period === 'custom'
  const rangeReady = !isCustom || validRange(appliedRange)
  const range = isCustom ? appliedRange : undefined

  const updateParams = useCallback(
    (changes: Record<string, string | null>) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          for (const [key, value] of Object.entries(changes)) {
            if (value === null) next.delete(key)
            else next.set(key, value)
          }
          return next
        },
        { replace: true }
      )
    },
    [setSearchParams]
  )

  const setPeriod = (value: PerformancePeriod) => {
    if (value !== 'custom') {
      updateParams({ period: value, start: null, end: null })
      return
    }
    // Seed the picker with the last 7 days so Custom shows data at once.
    const end = todayIst()
    const start = new Date(`${end}T00:00:00`)
    start.setDate(start.getDate() - 6)
    const seeded = appliedRange.start
      ? appliedRange
      : { start: new Intl.DateTimeFormat('en-CA').format(start), end }
    setDraftRange(seeded)
    updateParams({ period: value, start: seeded.start, end: seeded.end })
  }

  const openDay = (day: string) => {
    setDraftRange({ start: day, end: day })
    updateParams({ period: 'custom', start: day, end: day })
  }

  const [exporting, setExporting] = useState(false)
  const exportCsv = async () => {
    setExporting(true)
    try {
      const { blob, filename } = await strategyPnlApi.exportTrades(filter, period, range)
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = filename
      document.body.appendChild(link)
      link.click()
      link.remove()
      URL.revokeObjectURL(url)
    } catch (error) {
      showToast.error(error instanceof Error ? error.message : 'Could not export the trades.')
    } finally {
      setExporting(false)
    }
  }

  const setFilter = useCallback(
    (next: StrategyFilter) => {
      setSearchParams((prev) => writeStrategyFilter(prev, next), { replace: true })
    },
    [setSearchParams]
  )

  // Every strategy seen on this page in the current mode, kept while the filter
  // hides some of them, so an excluded one can be added back.
  const [knownNames, setKnownNames] = useState<string[]>([])
  const knownMode = useRef<string | undefined>(undefined)
  const nothingSelected = filter.none === true
  const queriesEnabled = rangeReady && !nothingSelected

  // Capital for the percent-of-capital view. The URL is the source of truth and
  // each mode remembers its own value, so a Live capital never shows in Sandbox.
  const urlCapital = searchParams.get('capital')
  const capital = useMemo(() => parseCapital(urlCapital), [urlCapital])
  const [capitalText, setCapitalText] = useState(urlCapital ?? '')
  // The mode comes from the app, which is settled before this page renders.
  const appMode = useThemeStore((state) => state.appMode)
  const clientMode: CapitalMode = appMode === 'analyzer' ? 'analyze' : 'live'
  // The mode whose capital is now in the URL. Until the URL shows it, the report
  // is not requested, so no request carries another mode's capital.
  const [capitalApplied, setCapitalApplied] = useState<
    { mode: CapitalMode; url: string | null; settled: boolean } | undefined
  >(undefined)
  const appliedMode = useRef<CapitalMode | undefined>(undefined)
  // True while typed text has not been committed, so unmount can still keep it.
  const capitalDirty = useRef(false)
  const capitalTextRef = useRef(capitalText)
  capitalTextRef.current = capitalText
  const clientModeRef = useRef(clientMode)
  clientModeRef.current = clientMode
  // Shown only when a commit finds the text unusable, never while typing.
  const [capitalMessage, setCapitalMessage] = useState<string | null>(null)

  // Writes the typed capital to the URL and storage. Runs when typing pauses,
  // on blur and on Enter; a cleared field runs it at once.
  const commitCapital = (text: string) => {
    const value = parseCapital(text)
    capitalDirty.current = false
    if (text.trim() === '') {
      setCapitalMessage(null)
      clearStoredCapital(clientMode)
      updateParams({ capital: null })
    } else if (value === null) {
      // Not sent, and not kept in the URL: the report shows no percentages.
      setCapitalMessage(capitalProblem(text))
      updateParams({ capital: null })
    } else {
      setCapitalMessage(null)
      saveStoredCapital(clientMode, value)
      updateParams({ capital: String(value) })
    }
  }
  const capitalCommit = useDebouncedCommit(commitCapital, 500)
  const { cancel: cancelCapitalCommit, isPending: capitalCommitPending } = capitalCommit

  // First load and mode switches: apply the mode's capital to the URL before
  // any report is requested. A pending edit from the other mode is dropped.
  useEffect(() => {
    if (appliedMode.current === clientMode) return
    const first = appliedMode.current === undefined
    appliedMode.current = clientMode
    cancelCapitalCommit()
    capitalDirty.current = false
    setCapitalMessage(null)
    const plan = planCapitalForMode({
      first,
      urlCapital,
      stored: loadStoredCapital(clientMode),
    })
    if (plan.remember !== null) saveStoredCapital(clientMode, plan.remember)
    if (plan.url === 'keep') {
      setCapitalApplied({ mode: clientMode, url: urlCapital, settled: true })
      return
    }
    setCapitalText(plan.url ?? '')
    updateParams({ capital: plan.url })
    setCapitalApplied({ mode: clientMode, url: plan.url, settled: plan.url === urlCapital })
  }, [clientMode, urlCapital, updateParams, cancelCapitalCommit])

  // The URL has caught up with the capital applied for this mode.
  useEffect(() => {
    if (capitalApplied && !capitalApplied.settled && capitalApplied.url === urlCapital) {
      setCapitalApplied({ ...capitalApplied, settled: true })
    }
  }, [capitalApplied, urlCapital])
  const capitalReady = capitalApplied?.mode === clientMode && capitalApplied.settled

  // Back/forward changes the URL; keep the field in step, but never over text
  // the user is still typing.
  useEffect(() => {
    setCapitalText((prev) => {
      if (capitalCommitPending()) return prev
      if (parseCapital(prev) === parseCapital(urlCapital)) return prev
      setCapitalMessage(null)
      return urlCapital ?? ''
    })
  }, [urlCapital, capitalCommitPending])

  // Leaving the page keeps a valid pending capital in storage. No URL write, so
  // it cannot fight the navigation, and no network call.
  useEffect(
    () => () => {
      if (!capitalDirty.current) return
      const value = parseCapital(capitalTextRef.current)
      if (value !== null) saveStoredCapital(clientModeRef.current, value)
    },
    []
  )

  const onCapitalInput = (text: string) => {
    setCapitalText(text)
    setCapitalMessage(null)
    capitalDirty.current = true
    if (text.trim() === '') capitalCommit.flush(text)
    else capitalCommit.schedule(text)
  }

  // Blur and Enter commit only when there is something to commit.
  const commitCapitalNow = () => {
    const text = capitalText
    const differs = parseCapital(text) !== capital
    const unreported = text.trim() !== '' && parseCapital(text) === null && capitalMessage === null
    if (capitalCommit.isPending() || differs || unreported) capitalCommit.flush(text)
  }

  const applyDraftRange = () => {
    if (validRange(draftRange)) updateParams({ start: draftRange.start, end: draftRange.end })
  }

  // Closed trades are only pushed on fill events, so a missed event (socket
  // down, app restarted) would leave the page stale until reload. A slow poll
  // also picks up fills the server-side reconcile recovers from the broker.
  // TanStack Query pauses this while the tab is hidden.
  const compareQuery = useQuery({
    queryKey: ['strategy-daily-compare', filter, period, range?.start, range?.end],
    // With nothing selected this is one unfiltered request that only fills the
    // picker's strategy list; its rows are never shown.
    queryFn: () => strategyPnlApi.compare(period, range, nothingSelected ? undefined : filter),
    enabled: rangeReady,
    refetchInterval: nothingSelected ? false : POLL_MS,
  })

  const dailyQuery = useQuery({
    queryKey: ['strategy-daily', clientMode, filter, period, range?.start, range?.end, capital],
    queryFn: () => strategyPnlApi.daily(filter, period, range, capital),
    enabled: queriesEnabled && capitalReady,
    refetchInterval: POLL_MS,
  })

  const refresh = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['strategy-daily-compare'] })
    queryClient.invalidateQueries({ queryKey: ['strategy-daily'] })
  }, [queryClient])
  // A mode switch changes the daily query key, which is what fetches the new
  // mode's report once its capital is applied. Refreshing it here would send
  // the old key out with the old mode's capital.
  const refreshCompare = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['strategy-daily-compare'] })
  }, [queryClient])

  useOrderEventRefresh(refresh, { events: ['order_event', 'analyzer_update'] })

  useEffect(() => {
    const unsubscribe = onModeChange(() => refreshCompare())
    return () => unsubscribe()
  }, [refreshCompare])

  // With a capital entered, every rupee figure on the page is shown scaled to it.
  // The factor comes from the daily report; before it arrives, from the typed capital.
  const scaleFactor =
    capital === null
      ? null
      : (dailyQuery.data?.capital_view?.scale ?? capital / REFERENCE_CAPITAL_PER_STRATEGY)
  const scaledCompare = useMemo(
    () => scaleRupeeFigures(compareQuery.data, scaleFactor),
    [compareQuery.data, scaleFactor]
  )
  const strategies = scaledCompare?.strategies ?? []
  const listedNames = compareQuery.data?.all_strategies
  const listedMode = compareQuery.data?.mode
  useEffect(() => {
    if (!listedNames) return
    const modeChanged = knownMode.current !== undefined && knownMode.current !== listedMode
    knownMode.current = listedMode
    setKnownNames((prev) => {
      const base = modeChanged ? [] : prev
      const merged = [...new Set([...base, ...listedNames])].sort()
      return merged.length === prev.length && !modeChanged ? prev : merged
    })
  }, [listedNames, listedMode])
  const strategyNames = useMemo(
    () => [...new Set([...knownNames, ...filter.include, ...filter.exclude])].sort(),
    [knownNames, filter]
  )
  const daily = useMemo(
    () => scaleRupeeFigures(dailyQuery.data, scaleFactor),
    [dailyQuery.data, scaleFactor]
  )
  const dailyMode = daily?.mode
  // The response mode is only a hint. If it differs from the app's mode (another
  // device may have switched), ask the server for its mode, as AuthSync does. A
  // stale response cannot change the app mode: only the server's answer can, and
  // when it agrees with the app nothing changes. One ask per mismatched response
  // mode; the guard is released once the response matches the app's mode again,
  // so a later switch on another device is reconciled too.
  const reconciledMode = useRef<string | undefined>(undefined)
  useEffect(() => {
    if (!dailyMode) return
    if (dailyMode === clientMode) {
      reconciledMode.current = undefined
      return
    }
    if (reconciledMode.current === dailyMode) return
    reconciledMode.current = dailyMode
    void useThemeStore.getState().syncAppMode()
  }, [dailyMode, clientMode])
  const includedChips = daily?.included ?? []
  const excludedChips = daily?.excluded ?? filter.exclude
  const dailyError = daily?.status === 'error' ? daily.message : null
  const isFetching = compareQuery.isFetching || dailyQuery.isFetching
  // One resolved day (1D, or a one-day custom range) shows the trade list
  // instead of ratios that mean nothing over a single session.
  const isToday = !!daily?.start_date && daily.start_date === daily.end_date
  const shownRange = rangeLabel(daily?.start_date, daily?.end_date)
  const isLastSession = period === '1d' && !!daily?.end_date && daily.end_date !== todayIst()
  const draftValid = validRange(draftRange)
  const bySymbol = daily?.by_symbol ?? []

  return (
    <div className="space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-3xl font-bold tracking-tight">Strategy Daily Performance</h1>
            <StrategyModeBadge mode={daily?.mode} />
          </div>
          <p className="text-muted-foreground">
            Day-by-day strategy performance with professional trade evaluation metrics - win rate,
            profit factor, expectancy in R, estimated charges, Sharpe/Sortino/Calmar, drawdown and
            streaks. 1D shows the last traded session's trades instead of ratios. Live and Analyze
            mode are tracked separately.
          </p>
          {shownRange && (
            <p className="text-sm mt-1">
              Showing <span className="font-medium">{shownRange}</span>
              {isLastSession && (
                <span className="text-muted-foreground"> (last traded session)</span>
              )}
            </p>
          )}
          {!nothingSelected && (includedChips.length > 0 || excludedChips.length > 0) && (
            <div className="flex flex-wrap items-center gap-1.5 mt-2">
              {includedChips.map((name) => (
                <span key={`in-${name}`} className="rounded-full border px-2 py-0.5 text-xs">
                  {name}
                </span>
              ))}
              {excludedChips.map((name) => (
                <span
                  key={`out-${name}`}
                  className="rounded-full border border-red-600/40 px-2 py-0.5 text-xs text-red-600"
                >
                  Excluded: {name}
                </span>
              ))}
            </div>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <StrategyFilterPicker filter={filter} names={strategyNames} onChange={setFilter} />
          <div className="flex flex-col">
            <Input
              type="text"
              inputMode="decimal"
              aria-label="Capital per strategy (Rs)"
              placeholder="Capital per strategy (Rs)"
              className="w-[150px]"
              value={capitalText}
              onChange={(e) => onCapitalInput(e.target.value)}
              onBlur={commitCapitalNow}
              onKeyDown={(e) => {
                if (e.key === 'Enter') commitCapitalNow()
              }}
              aria-invalid={capitalMessage !== null}
            />
            {capitalMessage && (
              <span className="text-xs text-red-600 mt-0.5">{capitalMessage}</span>
            )}
          </div>
          <Select value={period} onValueChange={(v) => setPeriod(v as PerformancePeriod)}>
            <SelectTrigger className="w-[100px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {PERIODS.map((p) => (
                <SelectItem key={p.value} value={p.value}>
                  {p.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {isCustom && (
            <form
              className="flex items-center gap-2"
              onSubmit={(e) => {
                e.preventDefault()
                applyDraftRange()
              }}
            >
              <Input
                type="date"
                aria-label="Start date"
                className="w-[150px]"
                value={draftRange.start}
                max={draftRange.end || todayIst()}
                onChange={(e) => setDraftRange((r) => ({ ...r, start: e.target.value }))}
                aria-invalid={!draftValid}
              />
              <span className="text-muted-foreground text-sm">to</span>
              <Input
                type="date"
                aria-label="End date"
                className="w-[150px]"
                value={draftRange.end}
                min={draftRange.start || undefined}
                max={todayIst()}
                onChange={(e) => setDraftRange((r) => ({ ...r, end: e.target.value }))}
                aria-invalid={!draftValid}
              />
              <Button
                type="submit"
                size="sm"
                disabled={
                  !draftValid ||
                  (draftRange.start === appliedRange.start && draftRange.end === appliedRange.end)
                }
              >
                Apply
              </Button>
            </form>
          )}
          <Button variant="outline" size="sm" onClick={refresh} disabled={isFetching}>
            <RefreshCw className={cn('h-4 w-4 mr-2', isFetching && 'animate-spin')} />
            Refresh
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={exportCsv}
            disabled={!queriesEnabled || exporting}
          >
            <Download className={cn('h-4 w-4 mr-2', exporting && 'animate-pulse')} />
            Export CSV
          </Button>
        </div>
      </div>

      <DataHealthBanner health={daily?.data_health ?? compareQuery.data?.data_health} />

      <div className="flex items-start gap-2 rounded-lg border border-muted-foreground/20 bg-muted/40 p-3 text-sm text-muted-foreground">
        <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
        <span>
          History starts from the day this page was deployed - trades closed before that are not
          reconstructed. Rupee metrics stay on each strategy's own realized P&L. Sharpe and
          Sortino are calculated on daily returns after charges and do not change with capital;
          Calmar is still calculated on rupee daily P&L.
          Percentage-of-capital figures appear only when you enter a capital per strategy. Est.
          charges use the India intraday schedule (the placing broker's own brokerage, plus STT,
          exchange fees, stamp duty, GST), count each entry order once however many exit legs
          follow, and exclude slippage. "% of profit" is estimated charges divided by gross profit:
          100% means charges took all of it. R is shown only for trades whose signal stop could be
          identified.
        </span>
      </div>

      {!rangeReady ? (
        <div className="text-center py-12 text-muted-foreground">
          Choose a start and end date, then press Apply.
        </div>
      ) : nothingSelected ? (
        <div className="text-center py-12 text-muted-foreground">Select at least one strategy.</div>
      ) : dailyQuery.isLoading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="h-8 w-8 animate-spin" />
        </div>
      ) : dailyError ? (
        <div className="text-center py-12 text-muted-foreground">{dailyError}</div>
      ) : !daily || daily.trades_count === 0 ? (
        <EmptyState
          icon={Gauge}
          title={`No closed ${daily?.mode === 'live' ? 'live' : 'Analyze-mode'} trades in this range`}
          description="Metrics appear once a strategy has at least one closed (exited) trade in the selected period. Live and Analyze trades are never combined: switch mode to see the other."
        />
      ) : (
        <>
          {daily.capital_view && (
            <CapitalViewBlock view={daily.capital_view} formatCurrency={formatCurrency} />
          )}
          {isToday ? (
            <SessionSummary daily={daily} formatCurrency={formatCurrency} />
          ) : (
            <div className="grid grid-cols-2 sm:grid-cols-5 gap-3">
              <MetricCard
                label="Net P&L (gross)"
                value={formatCurrency(daily.net_profit ?? 0)}
                positive={(daily.net_profit ?? 0) > 0}
                negative={(daily.net_profit ?? 0) < 0}
              />
              <MetricCard
                label="After est. charges"
                value={daily.net_after_costs == null ? '—' : formatCurrency(daily.net_after_costs)}
                positive={(daily.net_after_costs ?? 0) > 0}
                negative={(daily.net_after_costs ?? 0) < 0}
                hint={chargesHint(daily, formatCurrency)}
              />
              <MetricCard
                label="Entry slippage"
                value={daily.entry_slippage == null ? '—' : formatCurrency(daily.entry_slippage)}
                hint={
                  daily.entry_slippage_bps == null
                    ? slippageHint(daily)
                    : `${daily.entry_slippage_bps.toFixed(1)} bps, ${slippageHint(daily)}`
                }
              />
              <MetricCard label="Win Rate" value={`${(daily.win_rate ?? 0).toFixed(1)}%`} />
              <MetricCard label="Profit Factor" value={fmtRatio(daily.profit_factor)} />
              <MetricCard label="Trades" value={String(daily.trades_count ?? 0)} />
              <MetricCard label="Expectancy" value={formatCurrency(daily.expectancy ?? 0)} />
              <MetricCard
                label="Expectancy (R, after charges)"
                value={fmtR(daily.expectancy_net_r)}
                positive={(daily.expectancy_net_r ?? 0) > 0}
                negative={(daily.expectancy_net_r ?? 0) < 0}
                hint={`${rCoverage(daily) ?? ''}, before charges ${fmtR(daily.expectancy_r)}`}
              />
              <MetricCard
                label="Sharpe"
                value={fmtRatio(daily.sharpe_ratio)}
                hint={daily.sharpe_ratio == null ? 'not enough sessions' : 'after charges, daily'}
              />
              <MetricCard
                label="Sortino"
                value={fmtRatio(daily.sortino_ratio)}
                hint={daily.sortino_ratio == null ? 'not enough sessions' : 'after charges, daily'}
              />
              <MetricCard
                label="Max Drawdown (before charges)"
                value={formatCurrency(Math.abs(daily.max_drawdown ?? 0))}
                negative
              />
            </div>
          )}

          {!isToday && daily.ratio_warning && (
            <p className="text-xs text-muted-foreground">{daily.ratio_warning}</p>
          )}
          {!isToday && daily.ratio_capital != null && (
            <p className="text-xs text-muted-foreground">
              Sharpe and Sortino use each trading session's profit after estimated charges, divided
              by an assumed allocated capital of {formatCurrency(daily.ratio_capital)} (your amount
              per strategy times the strategies selected, not capital actually deployed). Weekends
              and exchange holidays are not counted.
            </p>
          )}
          {!isToday && rExcludedText(daily.r_excluded) && (
            <p className="text-xs text-muted-foreground">{rExcludedText(daily.r_excluded)}.</p>
          )}

          {!isToday && <EdgeCheckCard daily={daily} formatCurrency={formatCurrency} />}

          {daily.hold_stats && <HoldStatsRow stats={daily.hold_stats} />}

          {!isToday && daily.insights && (
            <InsightsPanel insights={daily.insights} formatCurrency={formatCurrency} />
          )}
          {!isToday && (
            <ExcursionCard
              strategy={filter}
              period={period}
              range={range}
              enabled={queriesEnabled && capitalReady}
            />
          )}

          <BrokerChargesCard
            rows={daily.by_broker ?? []}
            assumed={daily.fee_basis_assumed ?? false}
            formatCurrency={formatCurrency}
          />

          {isToday ? (
            <SessionTrades
              trades={daily.trades ?? []}
              sessionDate={daily.start_date}
              excursionCovered={daily.excursion_covered ?? 0}
              excursionTotal={daily.excursion_total ?? 0}
              formatCurrency={formatCurrency}
            />
          ) : (
            <>
              <Card>
                <CardHeader>
                  <CardTitle>Equity Curve &amp; Drawdown</CardTitle>
                </CardHeader>
                <CardContent>
                  <EquityDrawdownChart
                    equityCurve={daily.equity_curve ?? []}
                    equityCurveNet={daily.equity_curve_net ?? []}
                    isDarkMode={isDarkMode}
                    formatCurrency={formatCurrency}
                  />
                  <EquityLegend coverage={daily.equity_net_cost_coverage} />
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle>Daily P&amp;L</CardTitle>
                </CardHeader>
                <CardContent>
                  <CalendarHeatmap
                    dailyPnl={daily.daily_pnl ?? []}
                    formatCurrency={formatCurrency}
                    onSelectDay={openDay}
                  />
                  <div className="flex flex-wrap gap-4 mt-2 text-xs text-muted-foreground">
                    <span>Best day: {formatCurrency(daily.best_day ?? 0)}</span>
                    <span>Worst day: {formatCurrency(daily.worst_day ?? 0)}</span>
                    <span>Best win streak: {daily.best_win_streak ?? 0} days</span>
                    <span>Worst loss streak: {daily.best_loss_streak ?? 0} days</span>
                  </div>
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle>P&amp;L by Symbol</CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="grid gap-6 md:grid-cols-2">
                    <SymbolTable
                      title="Top contributors"
                      rows={bySymbol.filter((r) => r.net_pnl > 0).slice(0, 5)}
                      formatCurrency={formatCurrency}
                    />
                    <SymbolTable
                      title="Biggest detractors"
                      rows={bySymbol
                        .filter((r) => r.net_pnl < 0)
                        .slice(-5)
                        .reverse()}
                      formatCurrency={formatCurrency}
                    />
                  </div>
                </CardContent>
              </Card>
            </>
          )}

          <HoldBuckets rows={daily.by_hold_bucket ?? []} formatCurrency={formatCurrency} />

          <CloseBreakdowns
            byEntryHour={daily.by_entry_hour ?? []}
            byHour={daily.by_hour ?? []}
            byWeekday={daily.by_weekday ?? []}
            formatCurrency={formatCurrency}
          />
        </>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Strategy Comparison</CardTitle>
        </CardHeader>
        <CardContent className="py-0">
          {nothingSelected ? (
            <div className="text-center py-12 text-muted-foreground">
              Select at least one strategy.
            </div>
          ) : compareQuery.isLoading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="h-8 w-8 animate-spin" />
            </div>
          ) : strategies.length === 0 ? (
            <EmptyState
              icon={Gauge}
              title="No strategies with closed trades yet"
              description="A strategy appears here once it has at least one closed trade in the selected period."
            />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Strategy</TableHead>
                  <TableHead className="text-right">Trades</TableHead>
                  <TableHead className="text-right">Win Rate</TableHead>
                  <TableHead className="text-right">Profit Factor</TableHead>
                  <TableHead className="text-right">Net P&L</TableHead>
                  <TableHead className="text-right">Charges</TableHead>
                  <TableHead className="text-right">Charges % of profit</TableHead>
                  <TableHead className="text-right">After charges</TableHead>
                  <TableHead className="text-right">Expectancy</TableHead>
                  <TableHead
                    className="text-right"
                    title="Average result per trade after estimated charges"
                  >
                    Net/trade
                  </TableHead>
                  <TableHead
                    className="text-right"
                    title="Whether the average per trade is unlikely to be luck (needs 30+ trades)"
                  >
                    Edge
                  </TableHead>
                  <TableHead className="text-right">Avg Hold</TableHead>
                  {!isToday && <TableHead className="text-right">Sharpe</TableHead>}
                  {!isToday && <TableHead className="text-right">Max Drawdown (before charges)</TableHead>}
                  {!isToday && <TableHead className="text-right">Gross PF</TableHead>}
                  {!isToday && <TableHead className="text-right">Net PF</TableHead>}
                  {!isToday && <TableHead className="text-right">Avg R (net)</TableHead>}
                  {!isToday && <TableHead className="text-right">Return on capital</TableHead>}
                  {!isToday && <TableHead className="text-right">Drawdown % (net)</TableHead>}
                  {!isToday && <TableHead className="text-right">Recovery</TableHead>}
                  {!isToday && <TableHead className="text-right">Best day share</TableHead>}
                  {!isToday && <TableHead className="text-right">Grade</TableHead>}
                </TableRow>
              </TableHeader>
              <TableBody>
                {strategies.map((row) => (
                  <TableRow
                    key={row.strategy}
                    className={cn(
                      'cursor-pointer',
                      filter.include.length === 1 &&
                        filter.include[0] === row.strategy &&
                        'bg-muted/50'
                    )}
                    onClick={() => setFilter(onlyStrategy(row.strategy))}
                  >
                    <TableCell className="font-medium">{row.strategy}</TableCell>
                    <TableCell className="text-right font-mono">{row.trades_count}</TableCell>
                    <TableCell className="text-right font-mono">
                      {row.win_rate.toFixed(1)}%
                    </TableCell>
                    <TableCell className="text-right font-mono">
                      {fmtRatio(row.profit_factor)}
                    </TableCell>
                    <TableCell
                      className={cn(
                        'text-right font-mono',
                        row.net_profit > 0
                          ? 'text-green-600'
                          : row.net_profit < 0
                            ? 'text-red-600'
                            : ''
                      )}
                    >
                      {formatCurrency(row.net_profit)}
                    </TableCell>
                    <TableCell className="text-right font-mono text-muted-foreground">
                      {row.est_costs == null ? '—' : formatCurrency(row.est_costs)}
                    </TableCell>
                    <TableCell
                      className={cn(
                        'text-right font-mono',
                        chargesDragClass(row.charges_pct_of_gross)
                      )}
                    >
                      {row.charges_pct_of_gross == null
                        ? '—'
                        : `${row.charges_pct_of_gross.toFixed(1)}%`}
                    </TableCell>
                    <TableCell
                      className={cn(
                        'text-right font-mono',
                        (row.net_after_costs ?? 0) > 0
                          ? 'text-green-600'
                          : (row.net_after_costs ?? 0) < 0
                            ? 'text-red-600'
                            : ''
                      )}
                    >
                      {row.net_after_costs == null ? '—' : formatCurrency(row.net_after_costs)}
                    </TableCell>
                    <TableCell className="text-right font-mono">
                      {formatCurrency(row.expectancy)}
                    </TableCell>
                    <TableCell
                      className={cn('text-right font-mono', pnlClass(row.net_expectancy ?? 0))}
                    >
                      {row.net_expectancy == null ? '—' : formatCurrency(row.net_expectancy)}
                    </TableCell>
                    <TableCell className="text-right">{edgeShortLabel(row.edge_verdict)}</TableCell>
                    <TableCell className="text-right font-mono">
                      {fmtDuration(row.avg_hold_min)}
                    </TableCell>
                    {!isToday && (
                      <TableCell className="text-right font-mono">
                        {fmtRatio(row.sharpe_ratio)}
                      </TableCell>
                    )}
                    {!isToday && (
                      <TableCell className="text-right font-mono text-red-600">
                        {formatCurrency(Math.abs(row.max_drawdown))}
                      </TableCell>
                    )}
                    {!isToday && (
                      <>
                        <TableCell className="text-right font-mono">
                          {fmtRatio(row.gross_profit_factor)}
                        </TableCell>
                        <TableCell className="text-right font-mono">
                          {fmtRatio(row.net_profit_factor)}
                        </TableCell>
                        <TableCell className="text-right font-mono">
                          {row.expectancy_net_r == null ? '—' : fmtR(row.expectancy_net_r)}
                        </TableCell>
                        <TableCell className="text-right font-mono">
                          {row.return_on_capital_pct == null
                            ? '—'
                            : `${row.return_on_capital_pct.toFixed(2)}%`}
                        </TableCell>
                        <TableCell className="text-right font-mono">
                          {row.max_drawdown_pct == null ? '—' : `${row.max_drawdown_pct.toFixed(2)}%`}
                        </TableCell>
                        <TableCell className="text-right font-mono">
                          {fmtRatio(row.recovery_factor)}
                        </TableCell>
                        <TableCell className="text-right font-mono">
                          {row.top1_day_share_pct == null ? '—' : `${row.top1_day_share_pct.toFixed(1)}%`}
                        </TableCell>
                        <TableCell className="text-right" title={row.overall_note ?? undefined}>
                          <GradeBadge grade={row.overall_grade} preliminary={row.preliminary_grade} />
                        </TableCell>
                      </>
                    )}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
          {!isToday && !nothingSelected && compareQuery.data && strategies.length > 0 && (
            <PortfolioComparison data={compareQuery.data} formatCurrency={formatCurrency} />
          )}
        </CardContent>
      </Card>
    </div>
  )
}
