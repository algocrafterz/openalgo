import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  BaselineSeries,
  ColorType,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from 'lightweight-charts'
import { AlertTriangle, Gauge, Loader2, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  type DailyPnlPoint,
  type EquityPoint,
  type PerformancePeriod,
  strategyPnlApi,
} from '@/api/strategyPnl'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { EmptyState } from '@/components/ui/empty-state'
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
import { useOrderEventRefresh } from '@/hooks/useOrderEventRefresh'
import { cn, makeFormatCurrency } from '@/lib/utils'
import { useAuthStore } from '@/stores/authStore'
import { onModeChange, useThemeStore } from '@/stores/themeStore'

const PERIODS: { value: PerformancePeriod; label: string }[] = [
  { value: '7d', label: '7D' },
  { value: '30d', label: '30D' },
  { value: '90d', label: '90D' },
  { value: 'ytd', label: 'YTD' },
  { value: 'all', label: 'All' },
]

const PORTFOLIO_VALUE = '__portfolio__'

function fmtRatio(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return value.toFixed(2)
}

function MetricCard({
  label,
  value,
  positive,
  negative,
}: {
  label: string
  value: string
  positive?: boolean
  negative?: boolean
}) {
  return (
    <Card>
      <CardContent className="py-4">
        <div className="text-xs text-muted-foreground">{label}</div>
        <div
          className={cn(
            'text-xl font-bold font-mono mt-1',
            positive && 'text-green-600',
            negative && 'text-red-600'
          )}
        >
          {value}
        </div>
      </CardContent>
    </Card>
  )
}

function CalendarHeatmap({
  dailyPnl,
  formatCurrency,
}: {
  dailyPnl: DailyPnlPoint[]
  formatCurrency: (v: number) => string
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
              <div
                key={day.date}
                title={`${day.date}: ${formatCurrency(day.pnl)}`}
                className="h-3.5 w-3.5 rounded-sm"
                style={{ backgroundColor: bg }}
              />
            )
          })}
        </div>
      ))}
    </div>
  )
}

function EquityDrawdownChart({
  equityCurve,
  isDarkMode,
  formatCurrency,
}: {
  equityCurve: EquityPoint[]
  isDarkMode: boolean
  formatCurrency: (v: number) => string
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const equitySeriesRef = useRef<ISeriesApi<'Baseline'> | null>(null)
  const drawdownSeriesRef = useRef<ISeriesApi<'Baseline'> | null>(null)

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
    if (!equitySeriesRef.current || !drawdownSeriesRef.current) return
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
      chartRef.current?.timeScale().fitContent()
    }
  }, [equityCurve])

  return <div ref={containerRef} className="relative" style={{ height: '400px' }} />
}

export default function StrategyDailyPerformance() {
  const { user } = useAuthStore()
  const formatCurrency = useMemo(() => makeFormatCurrency(user?.broker), [user?.broker])
  const { mode: themeMode } = useThemeStore()
  const isDarkMode = themeMode === 'dark'
  const queryClient = useQueryClient()

  const [period, setPeriod] = useState<PerformancePeriod>('30d')
  const [selectedStrategy, setSelectedStrategy] = useState<string>(PORTFOLIO_VALUE)
  const strategyParam = selectedStrategy === PORTFOLIO_VALUE ? null : selectedStrategy

  const compareQuery = useQuery({
    queryKey: ['strategy-daily-compare', period],
    queryFn: () => strategyPnlApi.compare(period),
  })

  const dailyQuery = useQuery({
    queryKey: ['strategy-daily', strategyParam, period],
    queryFn: () => strategyPnlApi.daily(strategyParam, period),
  })

  const refresh = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['strategy-daily-compare'] })
    queryClient.invalidateQueries({ queryKey: ['strategy-daily'] })
  }, [queryClient])

  useOrderEventRefresh(refresh, { events: ['order_event', 'analyzer_update'] })

  useEffect(() => {
    const unsubscribe = onModeChange(() => refresh())
    return () => unsubscribe()
  }, [refresh])

  const strategies = compareQuery.data?.strategies ?? []
  const daily = dailyQuery.data
  const dailyError = daily?.status === 'error' ? daily.message : null
  const isFetching = compareQuery.isFetching || dailyQuery.isFetching

  return (
    <div className="space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">Strategy Daily Performance</h1>
          <p className="text-muted-foreground">
            Day-by-day strategy performance with professional trade evaluation metrics - win rate,
            profit factor, Sharpe/Sortino/Calmar, drawdown and streaks. Live and Analyze mode are
            tracked separately.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Select value={selectedStrategy} onValueChange={setSelectedStrategy}>
            <SelectTrigger className="w-[220px]">
              <SelectValue placeholder="Portfolio (all strategies)" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={PORTFOLIO_VALUE}>Portfolio (all strategies)</SelectItem>
              {strategies.map((s) => (
                <SelectItem key={s.strategy} value={s.strategy}>
                  {s.strategy}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
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
          <Button variant="outline" size="sm" onClick={refresh} disabled={isFetching}>
            <RefreshCw className={cn('h-4 w-4 mr-2', isFetching && 'animate-spin')} />
            Refresh
          </Button>
        </div>
      </div>

      <div className="flex items-start gap-2 rounded-lg border border-muted-foreground/20 bg-muted/40 p-3 text-sm text-muted-foreground">
        <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
        <span>
          History starts from the day this page was deployed - trades closed before that are not
          reconstructed. Metrics are computed in rupees on each strategy's own realized P&L, not a
          %-of-capital return (no isolated capital is tracked per strategy).
        </span>
      </div>

      {dailyQuery.isLoading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="h-8 w-8 animate-spin" />
        </div>
      ) : dailyError ? (
        <div className="text-center py-12 text-muted-foreground">{dailyError}</div>
      ) : !daily || daily.trades_count === 0 ? (
        <EmptyState
          icon={Gauge}
          title="No closed trades in this range"
          description="Metrics appear once a strategy has at least one closed (exited) trade in the selected period."
        />
      ) : (
        <>
          <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-8 gap-3">
            <MetricCard
              label="Net P&L"
              value={formatCurrency(daily.net_profit ?? 0)}
              positive={(daily.net_profit ?? 0) > 0}
              negative={(daily.net_profit ?? 0) < 0}
            />
            <MetricCard label="Win Rate" value={`${(daily.win_rate ?? 0).toFixed(1)}%`} />
            <MetricCard label="Profit Factor" value={fmtRatio(daily.profit_factor)} />
            <MetricCard label="Expectancy" value={formatCurrency(daily.expectancy ?? 0)} />
            <MetricCard label="Sharpe" value={fmtRatio(daily.sharpe_ratio)} />
            <MetricCard label="Sortino" value={fmtRatio(daily.sortino_ratio)} />
            <MetricCard
              label="Max Drawdown"
              value={formatCurrency(Math.abs(daily.max_drawdown ?? 0))}
              negative
            />
            <MetricCard label="Trades" value={String(daily.trades_count ?? 0)} />
          </div>

          <Card>
            <CardHeader>
              <CardTitle>Equity Curve &amp; Drawdown</CardTitle>
            </CardHeader>
            <CardContent>
              <EquityDrawdownChart
                equityCurve={daily.equity_curve ?? []}
                isDarkMode={isDarkMode}
                formatCurrency={formatCurrency}
              />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Daily P&amp;L</CardTitle>
            </CardHeader>
            <CardContent>
              <CalendarHeatmap dailyPnl={daily.daily_pnl ?? []} formatCurrency={formatCurrency} />
              <div className="flex flex-wrap gap-4 mt-2 text-xs text-muted-foreground">
                <span>Best day: {formatCurrency(daily.best_day ?? 0)}</span>
                <span>Worst day: {formatCurrency(daily.worst_day ?? 0)}</span>
                <span>Best win streak: {daily.best_win_streak ?? 0} days</span>
                <span>Worst loss streak: {daily.best_loss_streak ?? 0} days</span>
              </div>
            </CardContent>
          </Card>
        </>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Strategy Comparison</CardTitle>
        </CardHeader>
        <CardContent className="py-0">
          {compareQuery.isLoading ? (
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
                  <TableHead className="text-right">Expectancy</TableHead>
                  <TableHead className="text-right">Sharpe</TableHead>
                  <TableHead className="text-right">Max Drawdown</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {strategies.map((row) => (
                  <TableRow
                    key={row.strategy}
                    className={cn(
                      'cursor-pointer',
                      selectedStrategy === row.strategy && 'bg-muted/50'
                    )}
                    onClick={() => setSelectedStrategy(row.strategy)}
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
                    <TableCell className="text-right font-mono">
                      {formatCurrency(row.expectancy)}
                    </TableCell>
                    <TableCell className="text-right font-mono">
                      {fmtRatio(row.sharpe_ratio)}
                    </TableCell>
                    <TableCell className="text-right font-mono text-red-600">
                      {formatCurrency(Math.abs(row.max_drawdown))}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
