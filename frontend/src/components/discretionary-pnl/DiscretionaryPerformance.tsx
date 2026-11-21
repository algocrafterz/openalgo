import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Download, Gauge, Loader2 } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  type DiscretionaryCategory,
  type DiscretionaryFilters,
  discretionaryPnlApi,
  exportCsvUrl,
} from '@/api/discretionaryPnl'
import type { PerformancePeriod } from '@/api/strategyPnl'
import { EquityDrawdownChart } from '@/components/strategy-pnl/EquityDrawdownChart'
import { MetricCard } from '@/components/strategy-pnl/MetricCard'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
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
import { Switch } from '@/components/ui/switch'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useOrderEventRefresh } from '@/hooks/useOrderEventRefresh'
import {
  actionableWarnings,
  awaitingAttribution,
  awaitingAttributionText,
  calendarBars,
  classificationLine,
  DISCRETIONARY_POLL_MS,
  DRAWDOWN_BASIS,
  excursionIndex,
  filterChoices,
  fmtNetMoney,
  fmtProfitFactor,
  fmtWinRate,
  holdingRows,
  isEmptyBook,
  NET_EQUITY_CAPTION,
  netDefinitionText,
  netEquitySeries,
  summarizeUnrealized,
  symbolBars,
  syncSummary,
  totalCard,
  unclassifiedReasonsText,
  unclassifiedScopeNote,
  unresolvedWarningText,
} from '@/lib/discretionaryPnlView'
import { fmtDate, fmtDuration, pnlClass } from '@/lib/strategyPnlView'
import { cn } from '@/lib/utils'
import {
  CalendarTable,
  CategorySelector,
  PointsByInstrumentTable,
  TradeManagementStrip,
} from './DiscretionaryBlocks'
import { NetBarChart } from './DiscretionaryCharts'
import {
  DailyPnlChart,
  DataQualityStrip,
  HowYouTradeCard,
  RiskStrip,
  SampleNote,
} from './DiscretionaryInsights'
import {
  BreakdownTable,
  ClosedTradesTable,
  OpenPositionsTable,
  PeriodTable,
} from './DiscretionaryTables'
import { ExcursionPanel } from './ExcursionPanel'
import { TodayPerformance } from './TodayPerformance'
import { type SelectedTrade, TradeFillsDialog } from './TradeFillsDialog'
import { UnresolvedDialog } from './UnresolvedDialog'
import { useLedgerFillRefresh } from './useLedgerFillRefresh'

const ALL = 'all'

interface FilterState {
  broker: string
  account: string
  symbol: string
}

const NO_FILTERS: FilterState = { broker: '', account: '', symbol: '' }

function toQueryFilters(f: FilterState): DiscretionaryFilters {
  return {
    broker: f.broker || undefined,
    account: f.account || undefined,
    symbol: f.symbol || undefined,
  }
}

function errorText(error: unknown): string {
  const e = error as { code?: string; message?: string }
  if (e?.code === 'feature_disabled') return 'The Discretionary source is turned off on the server.'
  return e?.message || 'Could not load the discretionary P&L.'
}

function ChoiceSelect({
  label,
  value,
  options,
  allLabel,
  onChange,
}: {
  label: string
  value: string
  options: string[]
  allLabel: string
  onChange: (next: string) => void
}) {
  // A selected value that is no longer in the data stays selectable, so the filter can be seen.
  const shown = value && !options.includes(value) ? [...options, value] : options
  return (
    <Select value={value || ALL} onValueChange={(v) => onChange(v === ALL ? '' : v)}>
      <SelectTrigger className="w-[150px]" aria-label={label}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={ALL}>{allLabel}</SelectItem>
        {shown.map((o) => (
          <SelectItem key={o} value={o}>
            {o}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

/**
 * The Discretionary view of the Performance page: trades placed by hand at the broker, rebuilt
 * from the fill ledger. Win rate, profit factor, expectancy and the averages are net of estimated
 * charges; money cards say gross or net. There is no capital basis, so no Sharpe, Sortino or
 * return percentage is shown.
 */
export function DiscretionaryPerformance({
  period,
  range,
  rangeReady,
  formatCurrency,
  isDarkMode,
}: {
  period: PerformancePeriod
  range?: { start: string; end: string }
  rangeReady: boolean
  formatCurrency: (value: number) => string
  isDarkMode: boolean
}) {
  const queryClient = useQueryClient()
  const [filters, setFilters] = useState<FilterState>(NO_FILTERS)
  const [symbolText, setSymbolText] = useState('')
  const [quotes, setQuotes] = useState(false)
  const [category, setCategory] = useState<DiscretionaryCategory>('all')
  const [unresolvedOpen, setUnresolvedOpen] = useState(false)
  const [selectedTrade, setSelectedTrade] = useState<SelectedTrade | null>(null)
  const [knownBrokers, setKnownBrokers] = useState<string[]>([])
  const [knownAccounts, setKnownAccounts] = useState<string[]>([])
  const queryFilters = useMemo(() => toQueryFilters(filters), [filters])

  const summaryQuery = useQuery({
    queryKey: [
      'discretionary-pnl',
      'summary',
      queryFilters,
      period,
      range?.start,
      range?.end,
      quotes,
      category,
    ],
    queryFn: () => discretionaryPnlApi.summary(period, range, queryFilters, quotes, category),
    enabled: rangeReady,
    refetchInterval: DISCRETIONARY_POLL_MS,
    placeholderData: keepPreviousData,
    retry: false,
  })
  const summary = summaryQuery.data

  // MAE / MFE reads price bars, so it loads on its own, after the summary, and never blocks it.
  const excursionsQuery = useQuery({
    queryKey: [
      'discretionary-pnl',
      'excursions',
      queryFilters,
      period,
      range?.start,
      range?.end,
      category,
    ],
    queryFn: () => discretionaryPnlApi.excursions(period, range, queryFilters, category),
    enabled: rangeReady && !!summary && summary.trades_total > 0,
    refetchInterval: DISCRETIONARY_POLL_MS,
    retry: false,
  })
  const excursionMap = useMemo(
    () => excursionIndex(excursionsQuery.data, summary?.build_id),
    [excursionsQuery.data, summary?.build_id]
  )
  const netEquity = useMemo(() => netEquitySeries(summary?.trades ?? []), [summary?.trades])
  const exportHref = exportCsvUrl(period, range, queryFilters, category)

  // Fills arrive on order events; the poll covers a missed event.
  // This component is the only place that invalidates these queries on order events (the page
  // above does not), so one event causes one refetch.
  const refresh = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['discretionary-pnl'] })
  }, [queryClient])
  useOrderEventRefresh(refresh, { events: ['order_event', 'analyzer_update'] })
  // A fill made at the broker reaches the ledger through a debounced server sync, so the
  // order_update socket event refetches twice, later (see LEDGER_FILL_REFRESH_MS).
  useLedgerFillRefresh(refresh)

  // Keep every broker/account seen, so picking one does not remove the others from the list.
  const choices = useMemo(() => filterChoices(summary), [summary])
  useEffect(() => {
    setKnownBrokers((prev) => [...new Set([...prev, ...choices.brokers])].sort())
    setKnownAccounts((prev) => [...new Set([...prev, ...choices.accounts])].sort())
  }, [choices])

  const commitSymbol = () => {
    const next = symbolText.trim()
    if (next !== filters.symbol) setFilters((f) => ({ ...f, symbol: next }))
  }
  const filtersActive = !!(filters.broker || filters.account || filters.symbol)
  const clearFilters = () => {
    setFilters(NO_FILTERS)
    setSymbolText('')
  }

  const unrealized = useMemo(() => (summary ? summarizeUnrealized(summary) : null), [summary])
  const warnings = actionableWarnings(summary?.warnings)
  const sync = syncSummary(summary?.sync_health)
  const metrics = summary?.metrics
  // No completed trade (e.g. only a partial exit): the ratios have nothing to rate.
  const noTrades = (metrics?.trades_count ?? 0) === 0
  // Executions exist but none is attributable yet (all await attribution): a specific message
  // instead of the generic empty state, and the Unresolved fills list opens.
  const awaiting = awaitingAttribution(summary)
  const openTrade = (id: number, buildId?: string | null) => {
    const bid = buildId ?? summary?.build_id
    if (bid) setSelectedTrade({ id, buildId: bid })
  }

  const toolbar = (
    <div className="flex flex-wrap items-center gap-2" data-testid="discretionary-filters">
      <CategorySelector value={category} onChange={setCategory} />
      <ChoiceSelect
        label="Broker"
        value={filters.broker}
        options={knownBrokers}
        allLabel="All brokers"
        onChange={(broker) => setFilters((f) => ({ ...f, broker }))}
      />
      <ChoiceSelect
        label="Account"
        value={filters.account}
        options={knownAccounts}
        allLabel="All accounts"
        onChange={(account) => setFilters((f) => ({ ...f, account }))}
      />
      <Input
        type="text"
        aria-label="Symbol"
        placeholder="Symbol"
        className="w-[150px]"
        value={symbolText}
        onChange={(e) => setSymbolText(e.target.value)}
        onBlur={commitSymbol}
        onKeyDown={(e) => {
          if (e.key === 'Enter') commitSymbol()
        }}
      />
      {filtersActive && (
        <Button type="button" variant="ghost" size="sm" onClick={clearFilters}>
          Clear filters
        </Button>
      )}
      <div className="flex items-center gap-2 sm:ml-auto">
        <Switch
          id="discretionary-quotes"
          checked={quotes}
          onCheckedChange={setQuotes}
          aria-label="Fetch live prices"
        />
        <label htmlFor="discretionary-quotes" className="text-sm cursor-pointer">
          Fetch live prices
        </label>
      </div>
    </div>
  )

  let body: React.ReactNode
  if (!rangeReady) {
    body = (
      <div className="text-center py-12 text-muted-foreground">
        Choose a start and end date, then press Apply.
      </div>
    )
  } else if (summaryQuery.isLoading) {
    body = (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="h-8 w-8 animate-spin" />
      </div>
    )
  } else if (summaryQuery.error && !summary) {
    body = (
      <Alert variant="destructive">
        <AlertTriangle className="h-4 w-4" />
        <AlertTitle>Could not load the discretionary P&amp;L</AlertTitle>
        <AlertDescription>
          <p>{errorText(summaryQuery.error)}</p>
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="mt-2"
            onClick={() => summaryQuery.refetch()}
          >
            Retry
          </Button>
        </AlertDescription>
      </Alert>
    )
  } else if (!summary || !metrics || !unrealized) {
    body = null
  } else {
    const net = summary.realized_net_of_charges
    const total = totalCard(net, unrealized, summary.charges_unavailable_trades)
    const empty = isEmptyBook(summary)
    const chargesHint =
      summary.charges_unavailable_trades > 0
        ? `${summary.charges_unavailable_trades} trade(s) without a charge estimate: not complete`
        : `after ${formatCurrency(summary.charges)} estimated charges`
    const unrealizedValue =
      unrealized.kind === 'complete' || unrealized.kind === 'partial'
        ? formatCurrency(unrealized.value)
        : '—'
    const unrealizedHint =
      unrealized.kind === 'none'
        ? 'no open positions'
        : unrealized.kind === 'not_requested'
          ? 'Not requested: turn on Fetch live prices'
          : unrealized.kind === 'partial'
            ? `${unrealized.unpriced} position(s) unpriced or not listed`
            : undefined

    body = awaiting ? (
      <div
        className="flex flex-col items-center justify-center py-12 text-center"
        data-testid="discretionary-awaiting-attribution"
      >
        <Gauge className="h-12 w-12 text-muted-foreground/50 mb-4" />
        <p className="text-base font-medium max-w-2xl">
          {awaitingAttributionText(awaiting.collected, awaiting.unresolved)}
        </p>
        {(summary.unresolved_in_range?.total ?? 0) > 0 && (
          <p className="text-sm text-muted-foreground mt-1">
            Use View details in the warning above to list them.
          </p>
        )}
      </div>
    ) : empty ? (
      <EmptyState
        icon={Gauge}
        title={
          filtersActive || category !== 'all'
            ? 'No discretionary trades match these filters'
            : 'No discretionary trades yet'
        }
        description={
          filtersActive || category !== 'all'
            ? 'Clear the filters, choose All trades or widen the period.'
            : 'Closed trades and open positions appear here once the broker fills have been synced into the ledger. The ledger sync has to be running for this view to fill.'
        }
      />
    ) : (
      <>
        <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-5 gap-3">
          <MetricCard
            label="Net realized (after est. charges)"
            value={formatCurrency(net)}
            positive={net > 0}
            negative={net < 0}
            hint={chargesHint}
          />
          <MetricCard
            label="Gross realized"
            value={formatCurrency(summary.realized_gross)}
            positive={summary.realized_gross > 0}
            negative={summary.realized_gross < 0}
            hint="before charges"
          />
          <Card>
            <CardContent className="py-4">
              <div className="text-xs text-muted-foreground">Unrealized</div>
              <div
                className={cn(
                  'text-xl font-bold font-mono mt-1 break-words',
                  (unrealized.kind === 'complete' || unrealized.kind === 'partial') &&
                    pnlClass(unrealized.value)
                )}
              >
                {unrealizedValue}
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-1.5">
                {unrealized.badge && (
                  <Badge
                    variant="outline"
                    className="text-[11px]"
                    title={unrealized.badge.title}
                    data-testid="unrealized-price-badge"
                  >
                    {unrealized.badge.label}
                  </Badge>
                )}
                {unrealizedHint && (
                  <span className="text-[11px] text-muted-foreground">{unrealizedHint}</span>
                )}
              </div>
            </CardContent>
          </Card>
          <MetricCard
            label={total.label}
            value={formatCurrency(total.value)}
            positive={total.value > 0}
            negative={total.value < 0}
            hint={total.hint}
          />
          <MetricCard
            label="Total won (net)"
            value={formatCurrency(metrics.gross_profit)}
            hint="winning trades, after charges"
          />
          <MetricCard
            label="Total lost (net)"
            value={formatCurrency(metrics.gross_loss)}
            hint="losing trades, after charges"
          />
          <MetricCard
            label="Win rate (net)"
            value={noTrades ? '—' : fmtWinRate(metrics.win_rate)}
            hint={
              metrics.count_unavailable_reason
                ? 'no completed trade has a net P&L'
                : 'a win is a profit after charges'
            }
          />
          <MetricCard
            label="Profit factor (net)"
            value={
              noTrades
                ? '—'
                : fmtProfitFactor(
                    metrics.profit_factor,
                    metrics.winning_trades,
                    metrics.losing_trades
                  )
            }
            hint="total won / total lost"
          />
          <MetricCard
            label="Completed trades"
            value={String(metrics.trades_count)}
            hint={`${metrics.winning_trades} wins / ${metrics.losing_trades} losses`}
          />
          <MetricCard
            label="Avg win (net)"
            value={noTrades ? '—' : fmtNetMoney(metrics.avg_win, formatCurrency)}
          />
          <MetricCard
            label="Avg loss (net)"
            value={noTrades ? '—' : fmtNetMoney(metrics.avg_loss, formatCurrency)}
          />
          <MetricCard
            label="Expectancy (net)"
            value={noTrades ? '—' : fmtNetMoney(metrics.expectancy, formatCurrency)}
            hint="net, per trade"
          />
          {!summary.risk &&
            (summary.consistency?.max_drawdown_unavailable_reason ? (
              // one trading day has no peak to fall from: say so instead of showing 0
              <MetricCard
                label="Max drawdown"
                value="—"
                hint={`${summary.consistency.max_drawdown_unavailable_reason}; basis: ${DRAWDOWN_BASIS}`}
              />
            ) : (
              <MetricCard
                label="Max drawdown"
                value={formatCurrency(Math.abs(metrics.max_drawdown ?? 0))}
                negative
                hint={`basis: ${DRAWDOWN_BASIS}`}
              />
            ))}
        </div>
        <SampleNote sample={summary.sample} />
        {(metrics.net_unavailable_trades ?? 0) > 0 && (
          <p className="text-xs text-amber-600" data-testid="net-unavailable-note">
            {metrics.net_unavailable_trades} completed trade(s) have no net P&amp;L (no charge
            estimate) and are left out of win rate, profit factor, expectancy and the averages.
          </p>
        )}
        {summary.net_definition && (
          <p className="text-xs text-muted-foreground">
            {netDefinitionText(summary.net_definition)}
          </p>
        )}

        {(summary.trades.length > 0 || (summary.by_period?.daily ?? []).length > 0) && (
          <Card>
            <CardHeader>
              <CardTitle>Equity Curve &amp; Drawdown (net)</CardTitle>
            </CardHeader>
            <CardContent>
              {netEquity.points.length > 0 ? (
                <EquityDrawdownChart
                  equityCurve={netEquity.points}
                  equityCurveNet={[]}
                  isDarkMode={isDarkMode}
                  formatCurrency={formatCurrency}
                />
              ) : (
                <p className="text-sm text-muted-foreground" data-testid="net-equity-empty">
                  No completed trade with a net P&amp;L yet, so there is no equity curve.
                </p>
              )}
              <p className="mt-2 text-xs text-muted-foreground" data-testid="net-equity-caption">
                {NET_EQUITY_CAPTION}. Based on {netEquity.used} completed trade(s)
                {netEquity.skipped > 0 &&
                  `; ${netEquity.skipped} without a net P&L (no charge estimate) are left out`}
                {summary.trades_truncated > 0 &&
                  `; only the newest ${summary.trades.length} of ${summary.trades_total} trades are included`}
                .
              </p>
              {(summary.by_period?.daily ?? []).length > 0 && (
                <div className="mt-6">
                  <DailyPnlChart
                    rows={summary.by_period?.daily ?? []}
                    formatCurrency={formatCurrency}
                  />
                </div>
              )}
            </CardContent>
          </Card>
        )}

        {summary.consistency && (
          <RiskStrip
            consistency={summary.consistency}
            risk={summary.risk}
            formatCurrency={formatCurrency}
            onSelectTrade={(t) => openTrade(t.id, t.build_id)}
          />
        )}

        {(summary.trade_management || summary.costs) && (
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">Trade management and costs</CardTitle>
            </CardHeader>
            <CardContent>
              <TradeManagementStrip
                management={summary.trade_management}
                costs={summary.costs}
                formatCurrency={formatCurrency}
              />
            </CardContent>
          </Card>
        )}

        {(summary.monthly || summary.weekday) && (
          <Card data-testid="calendar-card">
            <CardHeader>
              <CardTitle>Net P&amp;L by month and weekday</CardTitle>
              <p className="text-xs text-muted-foreground">
                Each position counts once, on the date of its final close. * means some positions
                have no charge estimate, so charges and net are not complete.
              </p>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
                {summary.monthly && (
                  <div className="min-w-0 space-y-3">
                    <NetBarChart
                      title="Net P&L by month"
                      bars={calendarBars(summary.monthly)}
                      formatCurrency={formatCurrency}
                      samples="position(s)"
                      emptyText="No closed positions in this range."
                      testId="monthly-chart"
                    />
                    <CalendarTable
                      rows={summary.monthly}
                      first="Month"
                      label={(r) => r.period ?? r.key}
                      formatCurrency={formatCurrency}
                    />
                  </div>
                )}
                {summary.weekday && (
                  <div className="min-w-0 space-y-3">
                    <NetBarChart
                      title="Net P&L by weekday (close date)"
                      bars={calendarBars(summary.weekday)}
                      formatCurrency={formatCurrency}
                      samples="position(s)"
                      emptyText="No closed positions in this range."
                      testId="weekday-chart"
                    />
                    <CalendarTable
                      rows={summary.weekday}
                      first="Weekday"
                      label={(r) => r.weekday ?? r.key}
                      formatCurrency={formatCurrency}
                    />
                  </div>
                )}
              </div>
            </CardContent>
          </Card>
        )}

        <Card>
          <CardHeader>
            <CardTitle>P&amp;L by period</CardTitle>
          </CardHeader>
          <CardContent>
            <Tabs defaultValue="daily">
              <TabsList>
                <TabsTrigger value="daily">Daily</TabsTrigger>
                <TabsTrigger value="weekly">Weekly</TabsTrigger>
                <TabsTrigger value="monthly">Monthly</TabsTrigger>
              </TabsList>
              {(['daily', 'weekly', 'monthly'] as const).map((g) => (
                <TabsContent key={g} value={g}>
                  <PeriodTable
                    rows={summary.by_period?.[g] ?? []}
                    formatCurrency={formatCurrency}
                  />
                </TabsContent>
              ))}
            </Tabs>
          </CardContent>
        </Card>

        {summary.points_by_instrument && (
          <Card data-testid="points-card">
            <CardHeader>
              <CardTitle>Points by instrument</CardTitle>
              <p className="text-xs text-muted-foreground">
                Points per unit (not per lot or rupee), one row per symbol and exchange. Points are
                never added across instruments.
              </p>
            </CardHeader>
            <CardContent>
              <PointsByInstrumentTable rows={summary.points_by_instrument} />
            </CardContent>
          </Card>
        )}

        <Card>
          <CardHeader>
            <CardTitle>Breakdowns</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="mb-6">
              <NetBarChart
                title="Net P&L by symbol"
                bars={symbolBars(summary.by_symbol)}
                formatCurrency={formatCurrency}
                samples="trade(s)"
                emptyText="No closed trades in this range."
                testId="symbol-chart"
              />
            </div>
            <div className="grid grid-cols-1 gap-6 2xl:grid-cols-2">
              <BreakdownTable
                title="By symbol"
                firstColumn="Symbol"
                rows={summary.by_symbol ?? []}
                formatCurrency={formatCurrency}
              />
              <BreakdownTable
                title="Long vs Short"
                firstColumn="Side"
                rows={summary.by_side ?? []}
                formatCurrency={formatCurrency}
              />
              <BreakdownTable
                title="By product"
                firstColumn="Product"
                rows={summary.by_product ?? []}
                formatCurrency={formatCurrency}
              />
              <BreakdownTable
                title="Intraday vs overnight"
                firstColumn="Holding"
                rows={holdingRows(summary.by_holding)}
                formatCurrency={formatCurrency}
              />
            </div>
            <div className="mt-6" data-testid="hold-durations">
              <div className="text-sm font-medium mb-2">Average holding duration</div>
              <div className="grid grid-cols-3 gap-3">
                {(
                  [
                    ['Overall', summary.hold?.overall],
                    ['Winners', summary.hold?.winners],
                    ['Losers', summary.hold?.losers],
                  ] as const
                ).map(([label, stat]) => (
                  <MetricCard
                    key={label}
                    label={label}
                    value={fmtDuration(stat?.mean_minutes)}
                    hint={
                      stat && stat.count > 0
                        ? `median ${fmtDuration(stat.median_minutes)}, ${stat.count} trade(s)`
                        : undefined
                    }
                  />
                ))}
              </div>
            </div>
          </CardContent>
        </Card>

        {summary.breakdowns && (
          <HowYouTradeCard
            breakdowns={summary.breakdowns}
            sample={summary.sample}
            formatCurrency={formatCurrency}
          />
        )}

        <Card data-testid="excursion-card">
          <CardHeader>
            <CardTitle>MAE / MFE</CardTitle>
          </CardHeader>
          <CardContent>
            <ExcursionPanel
              data={excursionsQuery.data}
              loading={excursionsQuery.isLoading}
              failed={excursionsQuery.isError}
              onRetry={() => excursionsQuery.refetch()}
              formatCurrency={formatCurrency}
            />
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2">
            <CardTitle>Closed trades</CardTitle>
            <Button asChild variant="outline" size="sm">
              <a href={exportHref} download data-testid="export-csv">
                <Download className="mr-1 h-4 w-4" />
                Export CSV
              </a>
            </Button>
          </CardHeader>
          <CardContent>
            {summary.trades.length === 0 ? (
              <p className="text-sm text-muted-foreground">No closed trades in this range.</p>
            ) : (
              <ClosedTradesTable
                trades={summary.trades}
                formatCurrency={formatCurrency}
                onSelect={(id) => openTrade(id)}
                excursions={excursionMap}
                excursionsLoaded={!!excursionsQuery.data}
              />
            )}
            {summary.trades_truncated > 0 && (
              <p className="mt-2 text-xs text-muted-foreground" data-testid="trades-truncated">
                Showing {summary.trades_shown ?? summary.trades.length} of {summary.trades_total}{' '}
                trades (the newest). Totals above and the CSV export cover all of them.
              </p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Open positions</CardTitle>
          </CardHeader>
          <CardContent>
            {summary.positions.length === 0 ? (
              <p className="text-sm text-muted-foreground">No open positions.</p>
            ) : (
              <OpenPositionsTable positions={summary.positions} formatCurrency={formatCurrency} />
            )}
            {summary.positions_truncated > 0 && (
              <p className="mt-2 text-xs text-muted-foreground">
                Showing {summary.positions.length} of {summary.positions_total} positions.
              </p>
            )}
          </CardContent>
        </Card>
      </>
    )
  }

  return (
    <div className="space-y-6" data-testid="discretionary-performance">
      {toolbar}

      {summary?.classification && (
        <div className="space-y-0.5 text-xs text-muted-foreground" data-testid="classification">
          <p>
            {classificationLine(summary.classification)}{' '}
            {summary.classification.closed.unclassified > 0 && (
              <span
                className="cursor-help underline decoration-dotted"
                title={unclassifiedReasonsText(summary.classification.closed.unclassified_reasons)}
                data-testid="unclassified-count"
              >
                Why unclassified?
              </span>
            )}
          </p>
          {unclassifiedScopeNote(summary.classification, category) && (
            <p className="text-amber-600">
              {unclassifiedScopeNote(summary.classification, category)}
            </p>
          )}
          <p>{summary.classification.note}</p>
        </div>
      )}

      {summary && (
        <p className="text-sm">
          Showing{' '}
          <span className="font-medium">
            {summary.period.start === summary.period.end
              ? fmtDate(summary.period.start)
              : `${fmtDate(summary.period.start)} - ${fmtDate(summary.period.end)}`}
          </span>
          {summary.market_hours_source && (
            <span className="text-muted-foreground">
              {' '}
              (market hours from {summary.market_hours_source})
            </span>
          )}
          {summaryQuery.isPlaceholderData && (
            <span className="text-muted-foreground"> updating...</span>
          )}
        </p>
      )}

      {summary && summaryQuery.isRefetchError && (
        <Alert variant="warning" data-testid="discretionary-refresh-failed">
          <AlertTriangle className="h-4 w-4" />
          <AlertTitle>Last refresh failed, showing earlier data</AlertTitle>
          <AlertDescription>
            <p>{errorText(summaryQuery.error)}</p>
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="mt-2"
              onClick={() => summaryQuery.refetch()}
            >
              Retry
            </Button>
          </AlertDescription>
        </Alert>
      )}

      {summary && unresolvedWarningText(summary.unresolved_in_range) && (
        <output
          className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm"
          data-testid="unresolved-warning"
        >
          <AlertTriangle className="h-4 w-4 shrink-0 text-amber-600" />
          <span className="min-w-0 flex-1">
            {unresolvedWarningText(summary.unresolved_in_range)}
          </span>
          <Button type="button" size="sm" variant="outline" onClick={() => setUnresolvedOpen(true)}>
            View details
          </Button>
        </output>
      )}

      {summary && (warnings.length > 0 || sync.problem) && (
        <Alert variant="warning" data-testid="discretionary-warnings">
          <AlertTriangle className="h-4 w-4" />
          <AlertTitle>Check before you rely on these figures</AlertTitle>
          <AlertDescription>
            <ul className="list-disc pl-5 space-y-0.5">
              {sync.problem && <li>{sync.text}</li>}
              {warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      )}
      {summary && !sync.problem && !summary.data_quality && (
        // with the data-quality panel the same line is shown inside it
        <p className="text-xs text-muted-foreground" data-testid="discretionary-sync-line">
          {sync.text}
        </p>
      )}

      {summary?.data_quality && <DataQualityStrip dq={summary.data_quality} />}

      {summary?.today && (
        // the current session, whatever the period; same build as everything below
        <TodayPerformance
          today={summary.today}
          formatCurrency={formatCurrency}
          onSelectClosed={(id) => {
            if (summary.build_id) setSelectedTrade({ id, buildId: summary.build_id })
          }}
        />
      )}

      {body}

      <UnresolvedDialog
        open={unresolvedOpen}
        onClose={() => setUnresolvedOpen(false)}
        filters={queryFilters}
      />

      <TradeFillsDialog
        selected={selectedTrade}
        onClose={() => setSelectedTrade(null)}
        formatCurrency={formatCurrency}
      />
    </div>
  )
}
