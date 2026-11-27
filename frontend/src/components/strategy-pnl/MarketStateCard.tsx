import { useQuery } from '@tanstack/react-query'
import { Compass, Loader2 } from 'lucide-react'
import {
  type DateRange,
  type MarketStateDimension,
  type MarketStateDiscretionaryScope,
  type MarketStateResponse,
  type MarketStateRow,
  type PerformancePeriod,
  strategyPnlApi,
} from '@/api/strategyPnl'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { EmptyState } from '@/components/ui/empty-state'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import type { StrategyFilter } from '@/lib/strategyFilter'
import { cn } from '@/lib/utils'

export const MARKET_STATE_POLL_MS = 60_000

export const MARKET_STATE_FOOTNOTE =
  'Classified from NIFTY 50 daily bars, using the previous completed trading day. Applies to all strategies regardless of traded index. Range means neither Uptrend nor Downtrend (a residual class, not a confirmed range-bound market). Basis: gross or net as labelled.'

const SMALL_SAMPLE_FALLBACK = 'Small sample: do not draw conclusions'

const REASON_TEXT: Record<string, string> = {
  no_bars: 'NIFTY daily data is not available',
  no_prior_bar: 'no NIFTY daily data before the trade date',
  stale_bar: 'NIFTY daily data is missing for the days before the trade',
  insufficient_history: 'not enough NIFTY history yet',
  bad_date: 'the trade date could not be read',
}

const EM_DASH = '\u2014'

function pnlClass(value: number | null): string {
  if (value === null) return ''
  return value > 0 ? 'text-green-600' : value < 0 ? 'text-red-600' : ''
}

function netUnavailable(row: MarketStateRow): string {
  return `Net unavailable: ${row.net_coverage.costed} of ${row.net_coverage.total} trades costed`
}

function StateRow({
  row,
  formatCurrency,
  smallSampleText,
}: {
  row: MarketStateRow
  formatCurrency: (value: number) => string
  smallSampleText: string
}) {
  const empty = row.trades === 0
  const incomplete = !empty && row.net_coverage.costed < row.net_coverage.total
  const netCell = (value: string | null, colour?: string) => {
    if (empty) return EM_DASH
    if (incomplete) {
      return (
        <span title={netUnavailable(row)}>
          {EM_DASH}
          <span className="sr-only">{netUnavailable(row)}</span>
        </span>
      )
    }
    return <span className={colour}>{value ?? EM_DASH}</span>
  }
  return (
    <TableRow data-testid={`market-state-row-${row.name}`}>
      <TableCell className="font-medium">
        <div className="flex flex-wrap items-center gap-2">
          <span>{row.name}</span>
          {row.small_sample && !empty && (
            <Badge variant="outline" title={smallSampleText}>
              Small sample
            </Badge>
          )}
        </div>
      </TableCell>
      <TableCell className="text-right font-mono">{row.trades}</TableCell>
      <TableCell className="text-right font-mono">
        {empty || row.win_rate === null ? EM_DASH : `${row.win_rate.toFixed(1)}%`}
      </TableCell>
      <TableCell className={cn('text-right font-mono', pnlClass(row.gross_pnl))}>
        {empty || row.gross_pnl === null ? EM_DASH : formatCurrency(row.gross_pnl)}
      </TableCell>
      <TableCell className={cn('text-right font-mono', !incomplete && pnlClass(row.net_pnl))}>
        {netCell(row.net_pnl === null ? null : formatCurrency(row.net_pnl))}
        {incomplete && (
          <div className="text-[11px] font-sans text-muted-foreground">
            {row.net_coverage.costed} of {row.net_coverage.total} costed
          </div>
        )}
      </TableCell>
      <TableCell
        className={cn('text-right font-mono', !incomplete && pnlClass(row.avg_net_per_trade))}
      >
        {netCell(row.avg_net_per_trade === null ? null : formatCurrency(row.avg_net_per_trade))}
      </TableCell>
      <TableCell className="text-right font-mono">
        {netCell(row.profit_factor === null ? null : row.profit_factor.toFixed(2))}
      </TableCell>
    </TableRow>
  )
}

function StateTable({
  title,
  rows,
  formatCurrency,
  smallSampleText,
}: {
  title: string
  rows: MarketStateRow[]
  formatCurrency: (value: number) => string
  smallSampleText: string
}) {
  return (
    <div className="space-y-2">
      <h4 className="text-sm font-semibold">{title}</h4>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>{title === 'Trend' ? 'Trend' : 'Volatility'}</TableHead>
            <TableHead className="text-right">Trades</TableHead>
            <TableHead className="text-right">Win rate (gross)</TableHead>
            <TableHead className="text-right">Gross P&amp;L</TableHead>
            <TableHead className="text-right">Net P&amp;L</TableHead>
            <TableHead className="text-right">Avg net/trade</TableHead>
            <TableHead className="text-right">Profit factor (net)</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row) => (
            <StateRow
              key={row.name}
              row={row}
              formatCurrency={formatCurrency}
              smallSampleText={smallSampleText}
            />
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

function unclassifiedLine(data: MarketStateResponse, dimension: MarketStateDimension) {
  const block = data.unclassified?.[dimension]
  if (!block || block.trades === 0) return null
  const reasons = Object.entries(block.reasons)
    .map(([code, count]) => `${count} ${REASON_TEXT[code] ?? 'not classified'}`)
    .join('; ')
  const label = dimension === 'trend' ? 'trend' : 'volatility'
  return `${block.trades} of ${data.trades_count ?? 0} trades have no ${label} label (${reasons}).`
}

/** Read-only view of closed trades grouped by the NIFTY 50 daily market state. */
export function MarketStateCard({
  source,
  period,
  range,
  filter,
  clientMode,
  discretionary,
  enabled = true,
  formatCurrency,
}: {
  source: 'strategy' | 'discretionary'
  period: PerformancePeriod
  range?: DateRange
  filter?: StrategyFilter
  clientMode: 'live' | 'analyze'
  discretionary?: MarketStateDiscretionaryScope
  enabled?: boolean
  formatCurrency: (value: number) => string
}) {
  const query = useQuery({
    queryKey: [
      'strategy-market-state',
      source,
      source === 'strategy' ? clientMode : 'live',
      source === 'strategy' ? filter : null,
      period,
      range?.start,
      range?.end,
      source === 'discretionary' ? discretionary : null,
    ],
    queryFn: () => strategyPnlApi.marketState(source, period, range, filter, discretionary),
    enabled,
    refetchInterval: MARKET_STATE_POLL_MS,
  })
  const data = query.data
  const smallSampleText = data?.methodology?.small_sample_text ?? SMALL_SAMPLE_FALLBACK

  let body: React.ReactNode
  if (query.isLoading) {
    body = (
      <output className="flex items-center justify-center py-8">
        <Loader2 className="h-6 w-6 animate-spin" />
      </output>
    )
  } else if (query.isError || !data || data.status === 'error') {
    body = (
      <p className="py-6 text-center text-sm text-muted-foreground" role="alert">
        {data?.message ?? 'Could not load the market state analysis. Please try again.'}
      </p>
    )
  } else if ((data.trades_count ?? 0) === 0) {
    body = (
      <EmptyState
        icon={Compass}
        title="No closed trades to classify"
        description="Market state results appear once there are closed trades in the selected period."
      />
    )
  } else {
    const trendNote = unclassifiedLine(data, 'trend')
    const volatilityNote = unclassifiedLine(data, 'volatility')
    body = (
      <div className="space-y-6">
        {data.data_status?.state === 'no_data' && (
          <p className="text-sm text-muted-foreground">
            NIFTY daily data could not be read, so every trade is Unclassified.
          </p>
        )}
        <StateTable
          title="Trend"
          rows={data.trend ?? []}
          formatCurrency={formatCurrency}
          smallSampleText={smallSampleText}
        />
        {trendNote && <p className="text-xs text-muted-foreground">{trendNote}</p>}
        <StateTable
          title="Volatility"
          rows={data.volatility ?? []}
          formatCurrency={formatCurrency}
          smallSampleText={smallSampleText}
        />
        {volatilityNote && <p className="text-xs text-muted-foreground">{volatilityNote}</p>}
        <p className="text-xs text-muted-foreground">
          {smallSampleText} applies to any row marked Small sample (fewer than{' '}
          {data.methodology?.small_sample_trades ?? 30} trades).
        </p>
      </div>
    )
  }

  return (
    <Card data-testid="market-state-card">
      <CardHeader>
        <CardTitle>Market State Analysis</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {body}
        <p className="text-xs text-muted-foreground" data-testid="market-state-footnote">
          {MARKET_STATE_FOOTNOTE}
        </p>
      </CardContent>
    </Card>
  )
}
