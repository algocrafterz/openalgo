import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Gauge, Loader2 } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { type DiscretionaryFilters, discretionaryPnlApi } from '@/api/discretionaryPnl'
import type { PerformancePeriod } from '@/api/strategyPnl'
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
import { EquityDrawdownChart } from '@/components/strategy-pnl/EquityDrawdownChart'
import { MetricCard } from '@/components/strategy-pnl/MetricCard'
import { useOrderEventRefresh } from '@/hooks/useOrderEventRefresh'
import {
  actionableWarnings,
  DISCRETIONARY_POLL_MS,
  DRAWDOWN_BASIS,
  EQUITY_CAPTION,
  filterChoices,
  fmtProfitFactor,
  fmtWinRate,
  holdingRows,
  isEmptyBook,
  netDefinitionText,
  ratioCell,
  summarizeUnrealized,
  syncSummary,
  totalCard,
  withheldSentence,
} from '@/lib/discretionaryPnlView'
import { fmtDate, fmtDuration, pnlClass } from '@/lib/strategyPnlView'
import { cn } from '@/lib/utils'
import {
  BreakdownTable,
  ClosedTradesTable,
  OpenPositionsTable,
  PeriodTable,
} from './DiscretionaryTables'
import { type SelectedTrade, TradeFillsDialog } from './TradeFillsDialog'
import { UnresolvedSection } from './UnresolvedSection'

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
 * from the fill ledger. Everything here is gross or "net of estimated charges"; ratios that need
 * a capital basis are withheld by the server and shown as '--'.
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
    ],
    queryFn: () => discretionaryPnlApi.summary(period, range, queryFilters, quotes),
    enabled: rangeReady,
    refetchInterval: DISCRETIONARY_POLL_MS,
    placeholderData: keepPreviousData,
    retry: false,
  })
  const summary = summaryQuery.data

  // Fills arrive on order events; the poll covers a missed event.
  // This component is the only place that invalidates these queries on order events (the page
  // above does not), so one event causes one refetch.
  const refresh = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['discretionary-pnl'] })
  }, [queryClient])
  useOrderEventRefresh(refresh, { events: ['order_event', 'analyzer_update'] })

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
  const withheld = summary?.withheld ?? {}
  const metrics = summary?.metrics
  // No completed trade (e.g. only a partial exit): the ratios have nothing to rate.
  const noTrades = (metrics?.trades_count ?? 0) === 0
  const sharpe = ratioCell('sharpe_ratio', metrics?.sharpe_ratio, withheld)
  const sortino = ratioCell('sortino_ratio', metrics?.sortino_ratio, withheld)
  const returnPct = ratioCell(
    'return_pct',
    metrics?.return_pct,
    withheld,
    (v) => `${v.toFixed(2)}%`
  )
  const withheldNote = withheldSentence(withheld)

  const toolbar = (
    <div className="flex flex-wrap items-center gap-2" data-testid="discretionary-filters">
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

    body = empty ? (
      <EmptyState
        icon={Gauge}
        title={
          filtersActive
            ? 'No discretionary trades match these filters'
            : 'No discretionary trades yet'
        }
        description={
          filtersActive
            ? 'Clear the filters or widen the period.'
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
          <MetricCard label="Gross profit" value={formatCurrency(metrics.gross_profit)} />
          <MetricCard label="Gross loss" value={formatCurrency(metrics.gross_loss)} />
          <MetricCard label="Win rate" value={noTrades ? '—' : fmtWinRate(metrics.win_rate)} />
          <MetricCard
            label="Profit factor"
            value={
              noTrades
                ? '—'
                : fmtProfitFactor(
                    metrics.profit_factor,
                    metrics.winning_trades,
                    metrics.losing_trades
                  )
            }
          />
          <MetricCard
            label="Completed trades"
            value={String(metrics.trades_count)}
            hint={`${metrics.winning_trades} wins / ${metrics.losing_trades} losses`}
          />
          <MetricCard
            label="Avg win (gross)"
            value={noTrades ? '—' : formatCurrency(metrics.avg_win)}
          />
          <MetricCard
            label="Avg loss (gross)"
            value={noTrades ? '—' : formatCurrency(metrics.avg_loss)}
          />
          <MetricCard
            label="Expectancy"
            value={noTrades ? '—' : formatCurrency(metrics.expectancy)}
            hint="gross, per trade"
          />
          <MetricCard
            label="Max drawdown"
            value={formatCurrency(Math.abs(metrics.max_drawdown ?? 0))}
            negative
            hint={`basis: ${DRAWDOWN_BASIS}`}
          />
          <MetricCard label="Sharpe" value={sharpe.value} />
          <MetricCard label="Sortino" value={sortino.value} />
          <MetricCard label="Return %" value={returnPct.value} />
        </div>
        {withheldNote && <p className="text-xs text-muted-foreground">{withheldNote}</p>}
        {summary.net_definition && (
          <p className="text-xs text-muted-foreground">
            {netDefinitionText(summary.net_definition)}
          </p>
        )}

        {(metrics.equity_curve ?? []).length > 0 && (
          <Card>
            <CardHeader>
              <CardTitle>Equity Curve &amp; Drawdown</CardTitle>
            </CardHeader>
            <CardContent>
              <EquityDrawdownChart
                equityCurve={metrics.equity_curve ?? []}
                equityCurveNet={[]}
                isDarkMode={isDarkMode}
                formatCurrency={formatCurrency}
              />
              <p className="mt-2 text-xs text-muted-foreground">{EQUITY_CAPTION}</p>
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

        <Card>
          <CardHeader>
            <CardTitle>Breakdowns</CardTitle>
          </CardHeader>
          <CardContent>
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

        <Card>
          <CardHeader>
            <CardTitle>Closed trades</CardTitle>
          </CardHeader>
          <CardContent>
            {summary.trades.length === 0 ? (
              <p className="text-sm text-muted-foreground">No closed trades in this range.</p>
            ) : (
              <ClosedTradesTable
                trades={summary.trades}
                formatCurrency={formatCurrency}
                onSelect={(id) => {
                  if (summary.build_id) setSelectedTrade({ id, buildId: summary.build_id })
                }}
              />
            )}
            {summary.trades_truncated > 0 && (
              <p className="mt-2 text-xs text-muted-foreground">
                Showing the newest {summary.trades.length} of {summary.trades_total} trades. Totals
                above cover all of them.
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
      {summary && !sync.problem && (
        <p className="text-xs text-muted-foreground" data-testid="discretionary-sync-line">
          {sync.text}
        </p>
      )}

      {body}

      {summary && <UnresolvedSection filters={queryFilters} />}

      <TradeFillsDialog
        selected={selectedTrade}
        onClose={() => setSelectedTrade(null)}
        formatCurrency={formatCurrency}
      />
    </div>
  )
}
