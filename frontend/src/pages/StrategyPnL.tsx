import { type UseQueryResult, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Gauge,
  Loader2,
  Pause,
  Radio,
  RefreshCw,
  TrendingDown,
  TrendingUp,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router'
import {
  type StrategyDailyPerformance,
  type StrategyLeg,
  type StrategyPnl,
  strategyPnlApi,
} from '@/api/strategyPnl'
import { DataHealthBanner } from '@/components/DataHealthBanner'
import { StrategyModeBadge } from '@/components/StrategyModeBadge'
import { HoldStatsRow } from '@/components/strategy-pnl/HoldStats'
import { SessionSummary } from '@/components/strategy-pnl/SessionSummary'
import { SessionTrades } from '@/components/strategy-pnl/SessionTrades'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { EmptyState } from '@/components/ui/empty-state'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { useLivePrice } from '@/hooks/useLivePrice'
import { useOrderEventRefresh } from '@/hooks/useOrderEventRefresh'
import { applyLivePrices, legPriceKey, openLegItems, visibleLegs } from '@/lib/strategyPnlLive'
import { fmtDate, todayIst } from '@/lib/strategyPnlView'
import { cn, makeFormatCurrency } from '@/lib/utils'
import { useAuthStore } from '@/stores/authStore'
import { onModeChange } from '@/stores/themeStore'

// Realized P&L and quantity change on fills (order events); unrealized P&L
// moves continuously with the last traded price, which fires no event - a
// 30s poll covers that. TanStack Query v5 pauses refetchInterval while the
// tab is hidden by default (refetchIntervalInBackground defaults to false),
// so this never polls a backgrounded tab.
const POLL_MS = 30_000

function PnlText({ value, formatted }: { value: number; formatted: string }) {
  return (
    <span
      className={cn('font-mono', value > 0 ? 'text-green-600' : value < 0 ? 'text-red-600' : '')}
    >
      {formatted}
    </span>
  )
}

function LegsTable({
  legs,
  formatCurrency,
}: {
  legs: StrategyLeg[]
  formatCurrency: (v: number) => string
}) {
  const [showFlat, setShowFlat] = useState(false)
  if (legs.length === 0) {
    return <p className="text-sm text-muted-foreground py-3">No legs recorded for this strategy.</p>
  }
  const shown = visibleLegs(legs, showFlat)
  const hiddenCount = legs.length - shown.length
  return (
    <>
      {(hiddenCount > 0 || showFlat) && (
        <div className="flex items-center justify-between pb-2 text-xs text-muted-foreground">
          <span>
            {showFlat
              ? `Showing all ${legs.length} legs`
              : `${hiddenCount} flat leg(s) with no P&L today hidden`}
          </span>
          <Button variant="ghost" size="sm" onClick={() => setShowFlat((v) => !v)}>
            {showFlat ? 'Hide flat legs' : 'Show flat legs'}
          </Button>
        </div>
      )}
      {shown.length === 0 ? (
        <p className="text-sm text-muted-foreground py-3">No open or active legs today.</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Symbol</TableHead>
              <TableHead>Exchange</TableHead>
              <TableHead>Product</TableHead>
              <TableHead className="text-right">Qty</TableHead>
              <TableHead className="text-right">Avg Price</TableHead>
              <TableHead className="text-right">LTP</TableHead>
              <TableHead className="text-right">Today Realized</TableHead>
              <TableHead className="text-right">Unrealized</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {shown.map((leg) => (
              <TableRow key={`${leg.symbol}-${leg.exchange}-${leg.product}`}>
                <TableCell className="font-medium">{leg.symbol}</TableCell>
                <TableCell>
                  <Badge variant="outline">{leg.exchange}</Badge>
                </TableCell>
                <TableCell>
                  <Badge variant="secondary">{leg.product}</Badge>
                </TableCell>
                <TableCell className="text-right font-mono">{leg.quantity}</TableCell>
                <TableCell className="text-right font-mono">
                  {formatCurrency(leg.average_price)}
                </TableCell>
                <TableCell className="text-right font-mono">
                  {leg.ltp === null ? '-' : formatCurrency(leg.ltp)}
                </TableCell>
                <TableCell className="text-right">
                  <PnlText
                    value={leg.today_realized}
                    formatted={formatCurrency(leg.today_realized)}
                  />
                </TableCell>
                <TableCell className="text-right">
                  <PnlText value={leg.unrealized} formatted={formatCurrency(leg.unrealized)} />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </>
  )
}

function Stat({
  label,
  value,
  formatCurrency,
  bold = false,
}: {
  label: string
  value: number
  formatCurrency: (v: number) => string
  bold?: boolean
}) {
  const positive = value > 0
  const negative = value < 0
  return (
    <div>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div
        className={cn(
          'font-mono flex items-center justify-end gap-1',
          bold && 'font-semibold',
          positive && 'text-green-600',
          negative && 'text-red-600'
        )}
      >
        {positive && <TrendingUp className="h-3 w-3" />}
        {negative && <TrendingDown className="h-3 w-3" />}
        {formatCurrency(value)}
      </div>
    </div>
  )
}

function StrategyRow({
  strategy,
  isOpen,
  onToggle,
  formatCurrency,
}: {
  strategy: StrategyPnl
  isOpen: boolean
  onToggle: () => void
  formatCurrency: (v: number) => string
}) {
  return (
    <div className="py-4 first:pt-6 last:pb-6">
      <button
        type="button"
        className="w-full flex items-center gap-3 text-left"
        onClick={onToggle}
        aria-expanded={isOpen}
      >
        {isOpen ? (
          <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
        )}
        <div className="flex-1 min-w-0">
          <div className="font-semibold truncate">{strategy.strategy}</div>
          <div className="text-xs text-muted-foreground">
            Open qty: {strategy.open_quantity}
            {strategy.unpriced_legs > 0 && (
              <span className="ml-2 text-yellow-600">{strategy.unpriced_legs} leg(s) unpriced</span>
            )}
          </div>
        </div>
        <div className="grid grid-cols-3 gap-x-6 gap-y-1 text-right text-sm">
          <Stat
            label="Today Realized"
            value={strategy.today_realized}
            formatCurrency={formatCurrency}
          />
          <Stat label="Unrealized" value={strategy.unrealized} formatCurrency={formatCurrency} />
          <Stat
            label="Today Total"
            value={strategy.today_total}
            formatCurrency={formatCurrency}
            bold
          />
        </div>
      </button>
      {isOpen && (
        <div className="mt-4 pl-7 overflow-x-auto">
          <LegsTable legs={strategy.legs} formatCurrency={formatCurrency} />
        </div>
      )}
    </div>
  )
}

function PortfolioTotal({
  strategies,
  formatCurrency,
}: {
  strategies: StrategyPnl[]
  formatCurrency: (v: number) => string
}) {
  const totals = useMemo(
    () =>
      strategies.reduce(
        (acc, s) => ({
          today_realized: acc.today_realized + s.today_realized,
          unrealized: acc.unrealized + s.unrealized,
          today_total: acc.today_total + s.today_total,
        }),
        { today_realized: 0, unrealized: 0, today_total: 0 }
      ),
    [strategies]
  )

  return (
    <Card>
      <CardContent className="py-4">
        <div className="flex items-center justify-between gap-4">
          <div className="font-semibold">All strategies combined</div>
          <div className="grid grid-cols-3 gap-x-6 gap-y-1 text-right text-sm">
            <Stat
              label="Today Realized"
              value={totals.today_realized}
              formatCurrency={formatCurrency}
            />
            <Stat label="Unrealized" value={totals.unrealized} formatCurrency={formatCurrency} />
            <Stat
              label="Today Total"
              value={totals.today_total}
              formatCurrency={formatCurrency}
              bold
            />
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

function StrategyList({
  strategies,
  expanded,
  toggleExpanded,
  formatCurrency,
  keyPrefix,
}: {
  strategies: StrategyPnl[]
  expanded: Set<string>
  toggleExpanded: (key: string) => void
  formatCurrency: (v: number) => string
  keyPrefix: string
}) {
  return (
    <div className="divide-y">
      {strategies.map((strategy) => {
        const key = `${keyPrefix}:${strategy.strategy}`
        return (
          <StrategyRow
            key={key}
            strategy={strategy}
            isOpen={expanded.has(key)}
            onToggle={() => toggleExpanded(key)}
            formatCurrency={formatCurrency}
          />
        )
      })}
    </div>
  )
}

function SessionDetail({
  query,
  formatCurrency,
}: {
  query: UseQueryResult<StrategyDailyPerformance>
  formatCurrency: (value: number) => string
}) {
  const daily = query.data
  const day = daily?.start_date
  const heading = day
    ? `Session - ${fmtDate(day)}${day === todayIst() ? '' : ' (last traded session)'}`
    : 'Session trades'
  const error = daily?.status === 'error' ? daily.message : null
  const empty = !query.isLoading && !error && (!daily || (daily.trades_count ?? 0) === 0)
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-lg font-semibold">{heading}</h2>
        <Link
          to="/strategy-pnl/daily?period=1d"
          className="text-sm text-primary underline-offset-4 hover:underline"
        >
          Open in Performance
        </Link>
      </div>
      {query.isLoading ? (
        <div className="flex items-center justify-center py-8">
          <Loader2 className="h-6 w-6 animate-spin" />
        </div>
      ) : error ? (
        <div className="text-center py-8 text-muted-foreground">{error}</div>
      ) : empty || !daily ? (
        <div className="text-center py-8 text-muted-foreground">
          No closed trades yet. Trades appear here once a strategy exits a position.
        </div>
      ) : (
        <>
          <SessionSummary daily={daily} formatCurrency={formatCurrency} />
          {daily.hold_stats && <HoldStatsRow stats={daily.hold_stats} />}
          <SessionTrades
            trades={daily.trades ?? []}
            sessionDate={daily.start_date}
            excursionCovered={daily.excursion_covered ?? 0}
            excursionTotal={daily.excursion_total ?? 0}
            formatCurrency={formatCurrency}
          />
        </>
      )}
    </div>
  )
}

export default function StrategyPnL() {
  const { user } = useAuthStore()
  const formatCurrency = useMemo(() => makeFormatCurrency(user?.broker), [user?.broker])
  const queryClient = useQueryClient()
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  const { data, isLoading, isFetching, error } = useQuery({
    queryKey: ['strategy-pnl'],
    queryFn: () => strategyPnlApi.get(),
    refetchInterval: POLL_MS,
    // Focusing the window refetches only when the data is older than half a poll, so a tab that
    // was just polled does not send a second, overlapping read.
    staleTime: POLL_MS / 2,
    refetchOnWindowFocus: true,
  })

  // Portfolio session detail: 1d is today when something traded today, else the
  // last traded session.
  const sessionQuery = useQuery({
    queryKey: ['strategy-pnl-session'],
    queryFn: () => strategyPnlApi.daily(null, '1d'),
    refetchInterval: POLL_MS,
    staleTime: POLL_MS / 2,
    refetchOnWindowFocus: true,
  })

  const refresh = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['strategy-pnl'] })
    queryClient.invalidateQueries({ queryKey: ['strategy-pnl-session'] })
  }, [queryClient])

  // Fills change realized P&L and open quantity; refetch on the same order
  // events OrderBook/TradeBook already listen for.
  useOrderEventRefresh(refresh, { events: ['order_event', 'analyzer_update'] })

  // Refresh when the user toggles Live/Analyze mode, matching OrderBook/TradeBook.
  useEffect(() => {
    const unsubscribe = onModeChange(() => refresh())
    return () => unsubscribe()
  }, [refresh])

  const toggleExpanded = (key: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(key)) {
        next.delete(key)
      } else {
        next.add(key)
      }
      return next
    })
  }

  const serverStrategies = useMemo(
    () => (data?.status === 'success' ? (data.strategies ?? []) : []),
    [data]
  )

  // Same live-price pipeline as the Positions page: WebSocket LTP when the
  // market is open, MultiQuotes as fallback, paused while the tab is hidden.
  // Only unrealized P&L is re-marked here; realized figures come from fills and
  // the server refresh above.
  const legItems = useMemo(() => openLegItems(serverStrategies), [serverStrategies])
  const {
    data: pricedLegs,
    isLive,
    isPaused,
  } = useLivePrice(legItems, {
    enabled: legItems.length > 0,
    useMultiQuotesFallback: true,
    staleThreshold: 5000,
    multiQuotesRefreshInterval: 30000,
    pauseWhenHidden: true,
  })
  const strategies = useMemo(() => {
    const prices = new Map<string, number>()
    for (const item of pricedLegs) {
      if (item.ltp) prices.set(legPriceKey(item.exchange, item.symbol), item.ltp)
    }
    return applyLivePrices(serverStrategies, prices)
  }, [serverStrategies, pricedLegs])
  const fetchError =
    data?.status === 'error' ? data.message : error ? 'Failed to load strategy P&L' : null

  return (
    <div className="space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-3xl font-bold tracking-tight">Strategy P&L</h1>
            <StrategyModeBadge mode={data?.mode} />
            {isPaused ? (
              <Badge
                variant="outline"
                className="bg-amber-500/10 text-amber-600 border-amber-500/30 gap-1"
              >
                <Pause className="h-3 w-3" />
                Paused
              </Badge>
            ) : isLive ? (
              <Badge
                variant="outline"
                className="bg-emerald-500/10 text-emerald-600 border-emerald-500/30 gap-1"
              >
                <Radio className="h-3 w-3 animate-pulse" />
                Streaming
              </Badge>
            ) : null}
          </div>
          <p className="text-muted-foreground">
            Today's session only: realized and unrealized P&L per strategy for trades filled in the
            current trading session. Nothing from earlier sessions is shown here; see the
            Performance page for history. Live and Sandbox (Analyze mode) trades are tracked
            separately.
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={refresh} disabled={isFetching}>
          <RefreshCw className={cn('h-4 w-4 mr-2', isFetching && 'animate-spin')} />
          Refresh
        </Button>
      </div>

      <DataHealthBanner health={data?.data_health} />

      {strategies.length > 0 && (
        <div className="flex items-start gap-2 rounded-lg border border-muted-foreground/20 bg-muted/40 p-3 text-sm text-muted-foreground">
          <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
          <span>
            P&L only updates after a fill is reported by the broker or sandbox engine - a strategy
            that has not traded yet, or whose broker has no order-update feed configured, will show
            as flat rather than being a sign something is broken.
          </span>
        </div>
      )}

      {strategies.length > 0 && (
        <PortfolioTotal strategies={strategies} formatCurrency={formatCurrency} />
      )}

      <Card>
        <CardContent className="py-0">
          {isLoading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="h-8 w-8 animate-spin" />
            </div>
          ) : fetchError ? (
            <div className="text-center py-12 text-muted-foreground">{fetchError}</div>
          ) : strategies.length === 0 ? (
            <EmptyState
              icon={Gauge}
              title="No strategy activity in today's session yet"
              description="Orders placed with a strategy tag will appear here once they are placed and filled today."
            />
          ) : (
            <StrategyList
              strategies={strategies}
              expanded={expanded}
              toggleExpanded={toggleExpanded}
              formatCurrency={formatCurrency}
              keyPrefix="current"
            />
          )}
        </CardContent>
      </Card>

      <SessionDetail query={sessionQuery} formatCurrency={formatCurrency} />
    </div>
  )
}
