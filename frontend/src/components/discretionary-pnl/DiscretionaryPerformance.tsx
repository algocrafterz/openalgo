import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ChevronDown, ChevronRight, Download, Gauge, Loader2 } from 'lucide-react'
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
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
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
  awaitingAttribution,
  awaitingAttributionText,
  breakdownVisibility,
  calendarBars,
  calendarVisibility,
  classificationLine,
  closeDateCount,
  DISCRETIONARY_POLL_MS,
  drawdownReconciliation,
  excursionIndex,
  filterChoices,
  holdingRows,
  isEmptyBook,
  ledgerIsEmpty,
  MONTHLY_TABLE_NOTE,
  materialDataWarning,
  monthlyTotalDiffers,
  NET_EQUITY_CAPTION,
  NO_CLOSED_TRADES_TEXT,
  netEquityDaily,
  netEquityFromDailyRows,
  netEquityPerTrade,
  netEquitySeries,
  pendingAttributionLines,
  periodProfile,
  profileNoteText,
  rangeEndsBeforeReliableStart,
  reliableStartNotice,
  requestedStartDate,
  summarizeUnrealized,
  symbolBars,
  totalCard,
  unclassifiedReasonsText,
  unclassifiedScopeNote,
} from '@/lib/discretionaryPnlView'
import { fmtDate, todayIst } from '@/lib/strategyPnlView'
import { cn } from '@/lib/utils'
import { CalendarTable, CategorySelector, PointsByInstrumentTable } from './DiscretionaryBlocks'
import { NetBarChart } from './DiscretionaryCharts'
import { DiscretionaryEquityChart } from './DiscretionaryEquityChart'
import { DiscretionaryGroups } from './DiscretionaryGroups'
import { HowYouTradeCard, JumpLink } from './DiscretionaryInsights'
import { DiscretionaryStatistics } from './DiscretionaryStatistics'
import { DiscretionaryHeader, TodayLine, TradingInsights } from './DiscretionarySummary'
import {
  BreakdownTable,
  ClosedTradesTable,
  OpenPositionsTable,
  PeriodTable,
} from './DiscretionaryTables'
import { EquityCurveCard } from './EquityCurveCard'
import { ExcursionPanel } from './ExcursionPanel'
import { PnlCalendar } from './PnlCalendar'
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
  onSelectDate,
}: {
  period: PerformancePeriod
  range?: { start: string; end: string }
  rangeReady: boolean
  formatCurrency: (value: number) => string
  isDarkMode: boolean
  /** Makes a calendar day the report date; the calendar is shown only when this is given. */
  onSelectDate?: (date: string) => void
}) {
  const queryClient = useQueryClient()
  const [filters, setFilters] = useState<FilterState>(NO_FILTERS)
  const [symbolText, setSymbolText] = useState('')
  const [quotes, setQuotes] = useState(false)
  const [category, setCategory] = useState<DiscretionaryCategory>('all')
  const [unresolvedOpen, setUnresolvedOpen] = useState(false)
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const [breakdownsOpen, setBreakdownsOpen] = useState(true)
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
  const dataWarning = summary ? materialDataWarning(summary) : null
  const reliableNotice = summary
    ? reliableStartNotice(
        requestedStartDate(period, range, summary.today?.session_date ?? todayIst()),
        summary.period.reliable_since,
        fmtDate
      )
    : null
  const metrics = summary?.metrics
  // Executions exist but none is attributable yet (all await attribution): a specific message
  // instead of the generic empty state, and the Unresolved fills list opens.
  const beforeReliable = rangeEndsBeforeReliableStart(period, range, summary)
  const awaiting = beforeReliable ? null : awaitingAttribution(summary)
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

  const today = summary?.today
  const todayIsRange =
    !!today &&
    !!sessionRange &&
    sessionRange.start === today.session_date &&
    sessionRange.end === today.session_date
  const todayHasActivity =
    !!today &&
    (today.completed_positions > 0 ||
      today.open_positions > 0 ||
      today.partially_closed_positions > 0 ||
      Math.abs(today.realized_gross) > 0 ||
      pendingAttributionLines(today.pending_attribution).length > 0)
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
        : 'after estimated charges'
    const vis = breakdownVisibility(summary)
    const curveMode = profile.perPositionCurve ? 'position' : 'day'
    const shown = summary.trades_shown ?? summary.trades.length
    const dailyRows = summary.by_period?.daily ?? []
    // Equity Curve: the running sum of the server's daily realised net rows. It needs two days;
    // one session is covered by Trade-by-trade P&L. Older servers without daily rows fall back
    // to the end-of-day curve built from the trades.
    const rowCurve = netEquityFromDailyRows(dailyRows, [], summary.realized_net_of_charges)
    const hasRowCurve = rowCurve.points.length >= 2
    const showDayCurve = hasRowCurve || curveMode === 'day'
    const truncatedNote =
      summary.trades_truncated > 0
        ? `; only the newest ${summary.trades.length} of ${summary.trades_total} trades are included`
        : ''
    const skippedNote = (n: number) =>
      n > 0 ? `; ${n} without a net P&L (no charge estimate) are left out` : ''
    const positionBasis = `Net realised P&L after estimated charges, per completed trade by close time; excludes unrealized P&L. Based on ${positionCurve.used} completed trade(s)${skippedNote(positionCurve.skipped)}${truncatedNote}.`
    const dayBasis = `${NET_EQUITY_CAPTION}. Based on ${netEquity.used} completed trade(s)${skippedNote(netEquity.skipped)}${truncatedNote}.`

    const tradeCard = () => {
      const curve = positionCurve
      const reconciliation = drawdownReconciliation({
        curve,
        mode: 'position',
        tradesShown: shown,
        tradesTotal: summary.trades_total,
        risk: summary.risk,
        formatCurrency,
      })
      return (
        <Card data-testid="equity-card" key="position">
          <CardHeader>
            <CardTitle>Trade-by-trade P&amp;L</CardTitle>
          </CardHeader>
          <CardContent>
            {curve.points.length > 0 ? (
              <DiscretionaryEquityChart
                curve={curve}
                mode="position"
                reconciliation={reconciliation}
                basis={positionBasis}
                isDarkMode={isDarkMode}
                formatCurrency={formatCurrency}
              />
            ) : (
              <>
                <p className="text-sm text-muted-foreground" data-testid="net-equity-empty">
                  No completed trade with a net P&amp;L yet, so there is nothing to chart.
                </p>
                <p className="mt-2 text-xs text-muted-foreground" data-testid="net-equity-caption">
                  {positionBasis}
                </p>
              </>
            )}
          </CardContent>
        </Card>
      )
    }
    const hasAnyCurveData = summary.trades.length > 0 || dailyRows.length > 0
    const equityCurveNode =
      hasAnyCurveData && showDayCurve ? (
        <EquityCurveCard
          summary={summary}
          filters={queryFilters}
          category={category}
          today={summary.today?.session_date ?? todayIst()}
          fallback={{ curve: dayCurve, basis: dayBasis }}
          showDailyBars={profile.showDailyBars}
          formatCurrency={formatCurrency}
          isDarkMode={isDarkMode}
        />
      ) : null
    const tradeCurveNode = hasAnyCurveData && curveMode === 'position' ? tradeCard() : null

    // A section with nothing to compare is not rendered, so no heading or shell is left empty.
    const calVis = calendarVisibility(summary, profile)
    const hasBreakdowns =
      (!!summary.breakdowns && vis.patterns.length > 0) ||
      calVis.any ||
      (profile.showBreakdownTables && !vis.noneShown)

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
    ) : empty &&
      (beforeReliable ||
        (!ledgerIsEmpty(summary, summary.today?.session_date ?? todayIst()) &&
          !filtersActive &&
          category === 'all')) ? (
      <p
        className="py-8 text-center text-sm text-muted-foreground"
        data-testid="discretionary-empty-range"
      >
        {NO_CLOSED_TRADES_TEXT}
      </p>
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
            : 'Closed trades and open positions appear here once your broker trades have been collected. If you traded today and see nothing, check that you are logged in to your broker.'
        }
      />
    ) : (
      <>
        <DiscretionaryHeader
          summary={summary}
          formatCurrency={formatCurrency}
          chargesHint={chargesHint}
        >
          {
            // The range already is today's session, or today has nothing to add: no second panel.
            !todayIsRange && todayHasActivity ? todayNode : null
          }
        </DiscretionaryHeader>
        {profileNoteText(profile.note, profile.showBreakdownTables && !vis.noneShown) && (
          <p className="text-xs text-muted-foreground" data-testid="profile-note">
            {profileNoteText(profile.note, profile.showBreakdownTables && !vis.noneShown)}
          </p>
        )}

        <DiscretionaryGroups
          summary={summary}
          unrealized={unrealized}
          total={total}
          formatCurrency={formatCurrency}
          part="performance"
        />

        {onSelectDate && (
          <PnlCalendar
            anchorDate={summary.period.end}
            selectedRange={sessionRange}
            today={summary.today?.session_date ?? todayIst()}
            filters={queryFilters}
            category={category}
            reliableSince={summary.period.reliable_since}
            formatCurrency={formatCurrency}
            onSelectDate={onSelectDate}
          />
        )}

        {equityCurveNode}

        <DiscretionaryStatistics
          summary={summary}
          formatCurrency={formatCurrency}
          onSelectTrade={(t) => openTrade(t.id, t.build_id)}
        />

        {hasBreakdowns && (
          <Collapsible
            open={breakdownsOpen}
            onOpenChange={setBreakdownsOpen}
            className="rounded-lg border"
            data-testid="performance-breakdowns"
          >
            <CollapsibleTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                className="h-auto w-full justify-start gap-2 whitespace-normal px-4 py-3 text-left text-sm font-semibold"
                data-testid="performance-breakdowns-toggle"
              >
                {breakdownsOpen ? (
                  <ChevronDown className="size-4" />
                ) : (
                  <ChevronRight className="size-4" />
                )}
                Performance breakdowns
                <span className="min-w-0 break-words font-normal text-muted-foreground">
                  symbol, direction, product, weekday, time of day
                </span>
              </Button>
            </CollapsibleTrigger>
            <CollapsibleContent className="space-y-6 border-t p-4">
              {summary.breakdowns && vis.patterns.length > 0 && (
                <HowYouTradeCard
                  keys={vis.patterns}
                  breakdowns={summary.breakdowns}
                  formatCurrency={formatCurrency}
                />
              )}

              {calVis.any && (
                <Card data-testid="calendar-card">
                  <CardHeader>
                    <CardTitle>
                      {calVis.monthly && calVis.weekday
                        ? 'Net P&L by month and weekday'
                        : calVis.monthly
                          ? 'Net P&L by month'
                          : 'Net P&L by weekday'}
                    </CardTitle>
                    <p className="text-xs text-muted-foreground">
                      Each position counts once, on the date of its final close.
                      {calVis.weekday && vis.patterns.includes('weekday')
                        ? ' The weekday table here is by close date; the Weekday tab under How you trade is by entry day.'
                        : ''}{' '}
                      * means some positions have no charge estimate, so charges and net are not
                      complete.
                    </p>
                  </CardHeader>
                  <CardContent>
                    <div
                      className={cn(
                        'grid grid-cols-1 gap-6',
                        calVis.monthly && calVis.weekday && 'xl:grid-cols-2'
                      )}
                    >
                      {calVis.monthly && (
                        <div className="min-w-0 space-y-3">
                          <NetBarChart
                            title="Net P&L by month"
                            bars={calendarBars(summary.monthly)}
                            formatCurrency={formatCurrency}
                            samples="position(s)"
                            emptyText="No completed trades in this range."
                            testId="monthly-chart"
                          />
                          <CalendarTable
                            rows={summary.monthly ?? []}
                            first="Month"
                            label={(r) => r.period ?? r.key}
                            formatCurrency={formatCurrency}
                          />
                          {monthlyTotalDiffers(
                            summary.monthly,
                            summary.realized_net_of_charges
                          ) && (
                            <p
                              className="text-xs text-muted-foreground"
                              data-testid="monthly-table-note"
                            >
                              {MONTHLY_TABLE_NOTE}
                            </p>
                          )}
                        </div>
                      )}
                      {calVis.weekday && (
                        <div className="min-w-0 space-y-3">
                          <NetBarChart
                            title="Net P&L by weekday (close date)"
                            bars={calendarBars(summary.weekday)}
                            formatCurrency={formatCurrency}
                            samples="position(s)"
                            emptyText="No completed trades in this range."
                            testId="weekday-chart"
                          />
                          <CalendarTable
                            rows={summary.weekday ?? []}
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

              {profile.showBreakdownTables && !vis.noneShown && (
                <details
                  open={!profile.collapseSecondary}
                  className="rounded-lg border"
                  data-testid="more-breakdowns"
                >
                  <summary className="cursor-pointer px-4 py-3 text-sm font-medium">
                    More breakdowns
                  </summary>
                  <div className="border-t p-4">
                    {vis.symbol && (
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
                    )}
                    <div className="grid grid-cols-1 gap-6 2xl:grid-cols-2">
                      {vis.symbol && (
                        <BreakdownTable
                          title="By symbol"
                          firstColumn="Symbol"
                          rows={summary.by_symbol ?? []}
                          formatCurrency={formatCurrency}
                        />
                      )}
                      {vis.side && (
                        <BreakdownTable
                          title="Long vs Short"
                          firstColumn="Side"
                          rows={summary.by_side ?? []}
                          formatCurrency={formatCurrency}
                        />
                      )}
                      {vis.product && (
                        <BreakdownTable
                          title="By product"
                          firstColumn="Product"
                          rows={summary.by_product ?? []}
                          formatCurrency={formatCurrency}
                        />
                      )}
                      {vis.holding && (
                        <BreakdownTable
                          title="Intraday vs overnight"
                          firstColumn="Holding"
                          rows={holdingRows(summary.by_holding)}
                          formatCurrency={formatCurrency}
                        />
                      )}
                    </div>
                  </div>
                </details>
              )}
            </CollapsibleContent>
          </Collapsible>
        )}

        {tradeCurveNode}

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
              <p className="text-sm text-muted-foreground">{NO_CLOSED_TRADES_TEXT}</p>
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

        <Card data-testid="open-positions-card">
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

        <TradingInsights summary={summary} formatCurrency={formatCurrency} />

        <Collapsible
          open={advancedOpen}
          onOpenChange={setAdvancedOpen}
          className="rounded-lg border"
          data-testid="advanced-analysis"
        >
          <CollapsibleTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              className="h-auto w-full justify-start gap-2 whitespace-normal px-4 py-3 text-left text-sm font-semibold"
              data-testid="advanced-analysis-toggle"
            >
              {advancedOpen ? (
                <ChevronDown className="size-4" />
              ) : (
                <ChevronRight className="size-4" />
              )}
              Advanced analysis
              <span className="min-w-0 break-words font-normal text-muted-foreground">
                risk and consistency, trade management and costs, P&L by period, points by
                instrument, MAE / MFE
              </span>
            </Button>
          </CollapsibleTrigger>
          <p className="px-4 pb-3 text-xs text-muted-foreground" data-testid="advanced-jump-links">
            Jump to:{' '}
            {vis.points && (
              <>
                <JumpLink to="discretionary-points" beforeJump={() => setAdvancedOpen(true)}>
                  Points by instrument
                </JumpLink>
                {' · '}
              </>
            )}
            <JumpLink to="discretionary-excursion" beforeJump={() => setAdvancedOpen(true)}>
              MAE / MFE
            </JumpLink>
          </p>
          <CollapsibleContent className="space-y-6 border-t p-4">
            <DiscretionaryGroups
              summary={summary}
              unrealized={unrealized}
              total={total}
              formatCurrency={formatCurrency}
              part="analysis"
            />

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

            {summary.points_by_instrument && vis.points && (
              <Card data-testid="points-card" id="discretionary-points" className="scroll-mt-4">
                <CardHeader>
                  <CardTitle>Points by instrument</CardTitle>
                  <p className="text-xs text-muted-foreground">
                    Points per unit (not per lot or rupee), one row per symbol and exchange. Points
                    are never added across instruments.
                  </p>
                </CardHeader>
                <CardContent>
                  <PointsByInstrumentTable rows={summary.points_by_instrument} />
                </CardContent>
              </Card>
            )}

            <Card data-testid="excursion-card" id="discretionary-excursion" className="scroll-mt-4">
              <CardHeader>
                <CardTitle>MAE / MFE (from 1-minute price bars)</CardTitle>
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
          </CollapsibleContent>
        </Collapsible>
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
          {summary.period.start === summary.period.end ? 'Report date' : 'Report period'}:{' '}
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

      {reliableNotice && (
        <p
          className="text-sm text-amber-700 dark:text-amber-500 [.analyzer_&]:text-amber-500"
          data-testid="reliable-start-notice"
        >
          {reliableNotice}
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

      {summary && dataWarning && !(beforeReliable && isEmptyBook(summary)) && (
        <output
          className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm"
          data-testid="data-warning"
        >
          <AlertTriangle className="h-4 w-4 shrink-0 text-amber-600" />
          <span className="min-w-0 flex-1">{dataWarning}</span>
          {(summary.unresolved_in_range?.total ?? 0) > 0 && (
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => setUnresolvedOpen(true)}
            >
              View details
            </Button>
          )}
        </output>
      )}

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
