import type { DiscretionarySummary } from '@/api/discretionaryPnl'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  hasNoClosedActivity,
  pnlTextClass,
  type TotalCard,
  type UnrealizedSummary,
} from '@/lib/discretionaryPnlView'
import { fmtDuration } from '@/lib/strategyPnlView'
import { cn } from '@/lib/utils'
import { TradeManagementStrip } from './DiscretionaryBlocks'
import { RiskStrip, StripTile } from './DiscretionaryInsights'

type Money = (value: number) => string

/**
 * The groups under the summary: Performance (shown with the summary), then Risk and consistency
 * and Trade management and costs (in Advanced analysis). MAE / MFE and profit capture live in the
 * MAE / MFE card only. Two columns from the md breakpoint, one column below it.
 */
export function DiscretionaryGroups({
  summary,
  unrealized,
  total,
  formatCurrency,
  part = 'all',
}: {
  summary: DiscretionarySummary
  unrealized: UnrealizedSummary
  total: TotalCard
  formatCurrency: Money
  /** performance: the Performance card only; analysis: the others; all: both. */
  part?: 'all' | 'performance' | 'analysis'
}) {
  const m = summary.metrics
  const noMoney = hasNoClosedActivity(summary)
  const priced = unrealized.kind === 'complete' || unrealized.kind === 'partial'
  const unrealizedHint =
    unrealized.kind === 'none'
      ? 'no open positions'
      : unrealized.kind === 'not_requested'
        ? 'Not requested: turn on Fetch live prices'
        : unrealized.kind === 'partial'
          ? `${unrealized.unpriced} position(s) unpriced or not listed`
          : undefined
  const showPerformance = part !== 'analysis'
  const showAnalysis = part !== 'performance'

  return (
    <div
      className="grid grid-cols-1 gap-4 md:grid-cols-2"
      data-testid={part === 'all' ? 'discretionary-groups' : `discretionary-groups-${part}`}
    >
      {showPerformance && (
        <Card data-testid="group-performance" className="gap-3 py-4">
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Performance</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            <div className="grid grid-cols-2 gap-2">
              <div className="min-w-0 rounded-md border p-3">
                <div className="text-xs text-muted-foreground">Unrealized</div>
                <div
                  className={cn(
                    'mt-1 break-words font-mono text-sm font-semibold',
                    priced && pnlTextClass(unrealized.value)
                  )}
                >
                  {priced ? formatCurrency(unrealized.value) : '—'}
                </div>
                <div className="mt-0.5 flex flex-wrap items-center gap-1.5">
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
              </div>
              {noMoney && !priced ? (
                <StripTile label="Total" hint="no trade was closed in this date range">
                  —
                </StripTile>
              ) : (
                <StripTile label={total.label} hint={total.hint}>
                  <span className={pnlTextClass(total.value)}>{formatCurrency(total.value)}</span>
                </StripTile>
              )}
              <StripTile
                label="Total won (net)"
                hint={noMoney ? 'no closed trades' : 'winning trades, after estimated charges'}
              >
                {noMoney ? '—' : formatCurrency(m.gross_profit)}
              </StripTile>
              <StripTile
                label="Total lost (net)"
                hint={noMoney ? 'no closed trades' : 'losing trades, after estimated charges'}
              >
                {noMoney ? '—' : formatCurrency(m.gross_loss)}
              </StripTile>
            </div>
          </CardContent>
        </Card>
      )}

      {showAnalysis && summary.consistency && (
        <RiskStrip
          compact
          consistency={summary.consistency}
          risk={summary.risk}
          formatCurrency={formatCurrency}
        />
      )}

      {showAnalysis && (summary.trade_management || summary.costs || summary.hold) && (
        <Card data-testid="group-trade-management" className="gap-3 py-4">
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Trade management and costs</CardTitle>
          </CardHeader>
          <CardContent>
            <TradeManagementStrip
              compact
              management={summary.trade_management}
              costs={summary.costs}
              formatCurrency={formatCurrency}
            />
            {!summary.trade_management && summary.hold && (
              // an older server without the trade-management block: the hold figures it has
              <div className="mt-3" data-testid="hold-durations">
                <div className="mb-2 text-sm font-medium">Average holding duration</div>
                <div className="grid grid-cols-3 gap-2">
                  {(
                    [
                      ['Overall', summary.hold.overall],
                      ['Winners', summary.hold.winners],
                      ['Losers', summary.hold.losers],
                    ] as const
                  ).map(([label, stat]) => (
                    <StripTile
                      key={label}
                      label={label}
                      hint={
                        stat && stat.count > 0
                          ? `median ${fmtDuration(stat.median_minutes)}, ${stat.count} trade(s)`
                          : undefined
                      }
                    >
                      {fmtDuration(stat?.mean_minutes)}
                    </StripTile>
                  ))}
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  )
}
