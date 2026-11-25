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
  classificationLine,
  closeDateCount,
  DISCRETIONARY_POLL_MS,
  drawdownReconciliation,
  excursionIndex,
  filterChoices,
  hasProvisionalData,
  isEmptyBook,
  ledgerIsEmpty,
  MONTHLY_TABLE_DETAIL,
  MONTHLY_TABLE_NOTE,
  materialDataWarning,
  monthlyTotalDiffers,
  monthTableVisible,
  NET_EQUITY_CAPTION,
  NO_CLOSED_TRADES_TEXT,
  netEquityDaily,
  netEquityPerTrade,
  netEquitySeries,
  pendingAttributionLines,
  periodProfile,
  provisionalCounts,
  rangeEndsBeforeReliableStart,
  reliableStartNotice,
  requestedStartDate,
  summarizeUnrealized,
  totalCard,
  UNMATCHED_FILLS_REASON,
  unclassifiedReasonsText,
  unclassifiedScopeNote,
  unmatchedFillCount,
  unmatchedFillsNote,
} from '@/lib/discretionaryPnlView'
import { fmtDate, todayIst } from '@/lib/strategyPnlView'
import { CalendarTable, CategorySelector, PointsByInstrumentTable } from './DiscretionaryBlocks'
import { DiscretionaryBreakdowns } from './DiscretionaryBreakdowns'
import { JumpLink, RiskStrip } from './DiscretionaryInsights'
import { DiscretionaryStatistics } from './DiscretionaryStatistics'
import { DiscretionaryHeader } from './DiscretionarySummary'
import { ClosedTradesTable, OpenPositionsTable, PeriodTable } from './DiscretionaryTables'
import { EquityCurveCard } from './EquityCurveCard'
import { ExcursionPanel } from './ExcursionPanel'
import { hasIntradayData, IntradayPanel } from './IntradayPanel'
import { PnlCalendar } from './PnlCalendar'
import { TodayLine } from './TodayPerformance'
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
  const unmatched = summary ? unmatchedFillCount(summary) : 0
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
  const unmatchedNode =
    summary && unmatched > 0 && !(beforeReliable && isEmptyBook(summary)) ? (
      <p
        className="text-xs text-muted-foreground"
        data-testid="unmatched-fills-note"
        title={UNMATCHED_FILLS_REASON}
      >
        {unmatchedFillsNote(unmatched)}
        {(summary.unresolved_in_range?.total ?? 0) > 0 && (
          <>
            {' '}
            <button
              type="button"
              className="underline underline-offset-2 hover:text-foreground"
              onClick={() => setUnresolvedOpen(true)}
            >
              View details
            </button>
          </>
        )}
      </p>
    ) : null
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
        {summary?.market_hours_source && (
          <span className="text-xs text-muted-foreground" data-testid="market-hours-source">
            (market hours from {summary.market_hours_source})
          </span>
        )}
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
  const provisionalShown = hasProvisionalData(summary?.provisional)
  const todayNode = summary?.today ? (
    // the current session, whatever the period; same build as everything below
    <TodayLine
      today={summary.today}
      formatCurrency={formatCurrency}
      provisionalShown={provisionalShown}
      historicalFills={
        provisionalCounts(summary.provisional, summary.today.pending_attribution).historical
      }
      onShowToday={
        onSelectDate && !todayIsRange
          ? () => onSelectDate(summary.today?.session_date ?? todayIst())
          : undefined
      }
    />
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
    // Positions and win rate per month appear only in this table, so it stays (a table, no chart).
    const monthByPosition =
      profile.byPeriodTabs.includes('monthly') && monthTableVisible(summary.monthly)
    const curveMode = profile.perPositionCurve ? 'position' : 'day'
    const shown = summary.trades_shown ?? summary.trades.length
    const dailyRows = summary.by_period?.daily ?? []
    const truncatedNote =
      summary.trades_truncated > 0
        ? `; only the newest ${summary.trades.length} of ${summary.trades_total} are included`
        : ''
    const skippedNote = (n: number) => (n > 0 ? `; ${n} left out (no charge estimate)` : '')
    const positionBasis = `Net P&L after estimated charges. ${positionCurve.used} completed trade(s)${skippedNote(positionCurve.skipped)}${truncatedNote}.`
    const dayBasis = `${NET_EQUITY_CAPTION}. ${netEquity.used} completed trade(s)${skippedNote(netEquity.skipped)}${truncatedNote}.`

    // One cumulative-net curve: per closed trade for 7 or fewer close dates, per day beyond that.
    const hasAnyCurveData = summary.trades.length > 0 || dailyRows.length > 0
    const equityCurveNode = hasAnyCurveData ? (
      <EquityCurveCard
        summary={summary}
        filters={queryFilters}
        category={category}
        today={summary.today?.session_date ?? todayIst()}
        fallback={{ curve: dayCurve, basis: dayBasis }}
        position={
          curveMode === 'position'
            ? {
                curve: positionCurve,
                basis: positionBasis,
                reconciliation: drawdownReconciliation({
                  curve: positionCurve,
                  mode: 'position',
                  tradesShown: shown,
                  tradesTotal: summary.trades_total,
                  risk: summary.risk,
                  formatCurrency,
                }),
              }
            : null
        }
        formatCurrency={formatCurrency}
        isDarkMode={isDarkMode}
      />
    ) : null

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
            Select View details above to list them.
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
            : 'Trades appear here after your broker trades sync. If you traded today, check your broker login.'
        }
      />
    ) : (
      <>
        <DiscretionaryHeader
          summary={summary}
          formatCurrency={formatCurrency}
          chargesHint={chargesHint}
          unrealized={unrealized}
          total={total}
          coverageNote={unmatchedNode}
        >
          {
            // The range already is today's session, or today has nothing to add: no second panel.
            !todayIsRange && todayHasActivity ? todayNode : null
          }
        </DiscretionaryHeader>
        {profile.note && (
          <p className="text-xs text-muted-foreground" data-testid="profile-note">
            {profile.note}
          </p>
        )}

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

        {vis.tabs.length > 0 && (
          <DiscretionaryBreakdowns
            summary={summary}
            tabs={vis.tabs}
            formatCurrency={formatCurrency}
          />
        )}

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
                Showing the newest {summary.trades_shown ?? summary.trades.length} of{' '}
                {summary.trades_total} trades. Totals and CSV cover all.
              </p>
            )}
          </CardContent>
        </Card>

        {summary.positions.length === 0 ? (
          <p className="px-1 text-sm text-muted-foreground" data-testid="open-positions-card">
            No open positions.
          </p>
        ) : (
          <Card data-testid="open-positions-card">
            <CardHeader>
              <CardTitle>Open positions</CardTitle>
            </CardHeader>
            <CardContent>
              <OpenPositionsTable
                positions={summary.positions}
                formatCurrency={formatCurrency}
                provisional={summary.provisional}
              />
              {summary.positions_truncated > 0 && (
                <p className="mt-2 text-xs text-muted-foreground">
                  Showing {summary.positions.length} of {summary.positions_total} positions.
                </p>
              )}
            </CardContent>
          </Card>
        )}

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
                risk, consistency, periods, instruments, MAE / MFE
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
            {(summary.consistency || summary.trade_management || summary.costs || summary.hold) && (
              <RiskStrip
                consistency={summary.consistency}
                risk={summary.risk}
                management={summary.trade_management}
                costs={summary.costs}
                hold={summary.hold}
                formatCurrency={formatCurrency}
              />
            )}

            {profile.showByPeriodTabs && (
              <Card data-testid="period-card">
                <CardHeader>
                  <CardTitle>P&amp;L by period</CardTitle>
                </CardHeader>
                <CardContent>
                  <Tabs defaultValue={profile.byPeriodTabs[0]}>
                    <TabsList className="h-auto flex-wrap justify-start">
                      {profile.byPeriodTabs.map((g) => (
                        <TabsTrigger key={g} value={g}>
                          {g === 'weekly' ? 'Weekly' : 'Monthly'}
                        </TabsTrigger>
                      ))}
                      {monthByPosition && (
                        <TabsTrigger value="month_positions">
                          Monthly (by position close)
                        </TabsTrigger>
                      )}
                    </TabsList>
                    {profile.byPeriodTabs.map((g) => (
                      <TabsContent key={g} value={g}>
                        <PeriodTable
                          rows={summary.by_period?.[g] ?? []}
                          formatCurrency={formatCurrency}
                        />
                      </TabsContent>
                    ))}
                    {monthByPosition && (
                      <TabsContent value="month_positions" className="space-y-2">
                        <CalendarTable
                          rows={summary.monthly ?? []}
                          first="Month"
                          label={(r) => r.period ?? r.key}
                          formatCurrency={formatCurrency}
                        />
                        <p className="text-xs text-muted-foreground">
                          Counted once, on the final close date. *: some charges are not estimated.
                        </p>
                        {monthlyTotalDiffers(summary.monthly, summary.realized_net_of_charges) && (
                          <p
                            className="text-xs text-muted-foreground"
                            data-testid="monthly-table-note"
                            title={MONTHLY_TABLE_DETAIL}
                          >
                            {MONTHLY_TABLE_NOTE}
                          </p>
                        )}
                      </TabsContent>
                    )}
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
          <p title={summary.classification.note}>
            Intraday: closed in its opening session. Swing: held past it.
          </p>
        </div>
      )}

      {summary && (
        <p className="text-sm">
          {summary.period.start !== summary.period.end
            ? 'Report period'
            : period === '1d'
              ? 'Latest day with closed trades'
              : 'Report date'}
          :{' '}
          <span className="font-medium">
            {summary.period.start === summary.period.end
              ? fmtDate(summary.period.start)
              : `${fmtDate(summary.period.start)} - ${fmtDate(summary.period.end)}`}
          </span>
          {summaryQuery.isPlaceholderData && (
            <span className="text-muted-foreground"> updating...</span>
          )}
        </p>
      )}

      {summary &&
        period === '1d' &&
        summary.today?.session_date &&
        summary.period.start === summary.period.end &&
        summary.today.session_date !== summary.period.end && (
          <p className="text-xs text-muted-foreground" data-testid="date-presentation-note">
            {hasIntradayData(summary.intraday)
              ? `Today intraday (${fmtDate(summary.today.session_date)}) is shown below. Cards, charts and statistics above cover ${fmtDate(summary.period.end)}.`
              : `Cards, charts and statistics cover ${fmtDate(summary.period.end)}, not today (${fmtDate(summary.today.session_date)}).`}
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
        </output>
      )}

      {!populated && unmatchedNode}

      {!populated && todayNode}

      {period === '1d' && summary && (
        <IntradayPanel intraday={summary.intraday} formatCurrency={formatCurrency} />
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
