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
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
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
  closeDateCount,
  DISCRETIONARY_POLL_MS,
  drawdownReconciliation,
  excursionIndex,
  filterChoices,
  holdingRows,
  isEmptyBook,
  NET_EQUITY_CAPTION,
  netEquityDaily,
  netEquityPerTrade,
  netEquitySeries,
  periodProfile,
  summarizeUnrealized,
  symbolBars,
  syncSummary,
  totalCard,
  unclassifiedReasonsText,
  unclassifiedScopeNote,
  unresolvedWarningText,
} from '@/lib/discretionaryPnlView'
import { fmtDate } from '@/lib/strategyPnlView'
import { CalendarTable, CategorySelector, PointsByInstrumentTable } from './DiscretionaryBlocks'
import { NetBarChart } from './DiscretionaryCharts'
import { DiscretionaryEquityChart } from './DiscretionaryEquityChart'
import { DiscretionaryGroups } from './DiscretionaryGroups'
import { DailyPnlChart, DataQualityStrip, HowYouTradeCard } from './DiscretionaryInsights'
import { DiscretionaryHeader, TodayLine } from './DiscretionarySummary'
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
  const positionCurve = useMemo(() => netEquityPerTrade(summary?.trades ?? []), [summary?.trades])
  const dayCurve = useMemo(() => netEquityDaily(netEquity.points), [netEquity])
  const profile = useMemo(
    () =>
      periodProfile({
        period,
        closeDates: closeDateCount(summary),
        months: summary?.monthly?.length ?? 0,
      }),
    [period, summary]
  )
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
  const warnings = actionableWarnings(summary?.warnings, summary?.unresolved_in_range)
  const sync = syncSummary(summary?.sync_health)
  const metrics = summary?.metrics
  // Executions exist but none is attributable yet (all await attribution): a specific message
  // instead of the generic empty state, and the Unresolved fills list opens.
  const awaiting = awaitingAttribution(summary)
  const sessionRange = summary
    ? { start: summary.period.start, end: summary.period.end }
    : undefined
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

  const todayNode = summary?.today ? (
    // the current session, whatever the period; same build as everything below
    <TodayLine today={summary.today} range={sessionRange} formatCurrency={formatCurrency}>
      <TodayPerformance
        today={summary.today}
        formatCurrency={formatCurrency}
        onSelectClosed={(id) => {
          if (summary.build_id) setSelectedTrade({ id, buildId: summary.build_id })
        }}
      />
    </TodayLine>
  ) : null
  const populated = !!summary && !awaiting && !isEmptyBook(summary) && !!metrics && !!unrealized

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
    const curveMode = profile.perPositionCurve ? 'position' : 'day'
    const curve = curveMode === 'position' ? positionCurve : dayCurve
    const shown = summary.trades_shown ?? summary.trades.length
    const reconciliation = drawdownReconciliation({
      curve,
      mode: curveMode,
      tradesShown: shown,
      tradesTotal: summary.trades_total,
      risk: summary.risk,
      formatCurrency,
    })
    const equityBasis =
      curveMode === 'position'
        ? `Net realised P&L after estimated charges, per closed position by close time; excludes unrealized P&L. Based on ${positionCurve.used} completed trade(s)${
            positionCurve.skipped > 0
              ? `; ${positionCurve.skipped} without a net P&L (no charge estimate) are left out`
              : ''
          }${
            summary.trades_truncated > 0
              ? `; only the newest ${summary.trades.length} of ${summary.trades_total} trades are included`
              : ''
          }.`
        : `${NET_EQUITY_CAPTION}. Based on ${netEquity.used} completed trade(s)${
            netEquity.skipped > 0
              ? `; ${netEquity.skipped} without a net P&L (no charge estimate) are left out`
              : ''
          }${
            summary.trades_truncated > 0
              ? `; only the newest ${summary.trades.length} of ${summary.trades_total} trades are included`
              : ''
          }.`

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
        <DiscretionaryHeader
          summary={summary}
          formatCurrency={formatCurrency}
          chargesHint={chargesHint}
        >
          {todayNode}
        </DiscretionaryHeader>
        {profile.note && (
          <p className="text-xs text-muted-foreground" data-testid="profile-note">
            {profile.note}
          </p>
        )}

        {(summary.trades.length > 0 || (summary.by_period?.daily ?? []).length > 0) && (
          <Card data-testid="equity-card">
            <CardHeader>
              <CardTitle>
                {profile.profile === 'session' ? 'Session progression' : 'Cumulative net P&L'}
              </CardTitle>
            </CardHeader>
            <CardContent>
              {curve.points.length > 0 ? (
                <DiscretionaryEquityChart
                  curve={curve}
                  mode={curveMode}
                  reconciliation={reconciliation}
                  basis={equityBasis}
                  isDarkMode={isDarkMode}
                  formatCurrency={formatCurrency}
                />
              ) : (
                <>
                  <p className="text-sm text-muted-foreground" data-testid="net-equity-empty">
                    No completed trade with a net P&amp;L yet, so there is no equity curve.
                  </p>
                  <p
                    className="mt-2 text-xs text-muted-foreground"
                    data-testid="net-equity-caption"
                  >
                    {equityBasis}
                  </p>
                </>
              )}
              {profile.showDailyBars && (summary.by_period?.daily ?? []).length > 0 && (
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

        <DiscretionaryGroups
          summary={summary}
          unrealized={unrealized}
          total={total}
          excursions={excursionsQuery.data}
          formatCurrency={formatCurrency}
          onSelectTrade={(t) => openTrade(t.id, t.build_id)}
        />

        <h2 className="pt-2 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          How you trade
        </h2>
        {summary.breakdowns && (
          <HowYouTradeCard
            breakdowns={summary.breakdowns}
            sample={summary.sample}
            formatCurrency={formatCurrency}
          />
        )}

        {(profile.showMonthly || profile.showWeekday) && (summary.monthly || summary.weekday) && (
          <Card data-testid="calendar-card">
            <CardHeader>
              <CardTitle>
                {profile.showMonthly ? 'Net P&L by month and weekday' : 'Net P&L by weekday'}
              </CardTitle>
              <p className="text-xs text-muted-foreground">
                Each position counts once, on the date of its final close. * means some positions
                have no charge estimate, so charges and net are not complete.
              </p>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
                {profile.showMonthly && summary.monthly && (
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
                {profile.showWeekday && summary.weekday && (
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

        {profile.showByPeriodTabs && (
          <Card data-testid="period-card">
            <CardHeader>
              <CardTitle>P&amp;L by period</CardTitle>
            </CardHeader>
            <CardContent>
              <Tabs defaultValue="daily">
                <TabsList>
                  {profile.byPeriodTabs.map((g) => (
                    <TabsTrigger key={g} value={g}>
                      {g === 'daily' ? 'Daily' : g === 'weekly' ? 'Weekly' : 'Monthly'}
                    </TabsTrigger>
                  ))}
                </TabsList>
                {profile.byPeriodTabs.map((g) => (
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
        )}

        {profile.showBreakdownTables && (
          <details
            open={!profile.collapseSecondary}
            className="rounded-lg border"
            data-testid="more-breakdowns"
          >
            <summary className="cursor-pointer px-4 py-3 text-sm font-medium">
              More breakdowns: by symbol, long vs short, product, intraday vs overnight
            </summary>
            <div className="border-t p-4">
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
            </div>
          </details>
        )}

        <h2 className="pt-2 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          Trade quality
        </h2>
        {summary.points_by_instrument && (
          <Card data-testid="points-card" id="discretionary-points" className="scroll-mt-4">
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

        <Card data-testid="excursion-card" id="discretionary-excursion" className="scroll-mt-4">
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

        <Card id="discretionary-ledger" className="scroll-mt-4" data-testid="ledger-card">
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
            <p className="text-amber-700 dark:text-amber-500 [.analyzer_&]:text-amber-500">
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

      {!populated && todayNode}

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
