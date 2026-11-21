import type { DiscretionarySummary, ExcursionsResponse, TradeRef } from '@/api/discretionaryPnl'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  fmtDistance,
  fmtNetMoney,
  fmtPct,
  netDefinitionText,
  pnlTextClass,
  type TotalCard,
  type UnrealizedSummary,
} from '@/lib/discretionaryPnlView'
import { fmtDuration } from '@/lib/strategyPnlView'
import { cn } from '@/lib/utils'
import { TradeManagementStrip } from './DiscretionaryBlocks'
import { JumpLink, RiskStrip, SampleNote, StripTile } from './DiscretionaryInsights'

type Money = (value: number) => string

/**
 * The four groups under the summary: Performance, Risk and consistency, Trade management and
 * costs, Trade quality. Two columns from the md breakpoint, one column below it.
 */
export function DiscretionaryGroups({
  summary,
  unrealized,
  total,
  excursions,
  formatCurrency,
  onSelectTrade,
}: {
  summary: DiscretionarySummary
  unrealized: UnrealizedSummary
  total: TotalCard
  excursions: ExcursionsResponse | undefined
  formatCurrency: Money
  onSelectTrade: (trade: TradeRef) => void
}) {
  const m = summary.metrics
  const noTrades = m.trades_count === 0
  const priced = unrealized.kind === 'complete' || unrealized.kind === 'partial'
  const unrealizedHint =
    unrealized.kind === 'none'
      ? 'no open positions'
      : unrealized.kind === 'not_requested'
        ? 'Not requested: turn on Fetch live prices'
        : unrealized.kind === 'partial'
          ? `${unrealized.unpriced} position(s) unpriced or not listed`
          : undefined
  const points = summary.points_by_instrument
  const w = excursions?.aggregates.winners
  const l = excursions?.aggregates.losers

  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2" data-testid="discretionary-groups">
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
            <StripTile label={total.label} hint={total.hint}>
              <span className={pnlTextClass(total.value)}>{formatCurrency(total.value)}</span>
            </StripTile>
            <StripTile label="Total won (net)" hint="winning trades, after charges">
              {formatCurrency(m.gross_profit)}
            </StripTile>
            <StripTile label="Total lost (net)" hint="losing trades, after charges">
              {formatCurrency(m.gross_loss)}
            </StripTile>
            <StripTile label="Avg win (net)">
              {noTrades ? '—' : fmtNetMoney(m.avg_win, formatCurrency)}
            </StripTile>
            <StripTile label="Avg loss (net)">
              {noTrades ? '—' : fmtNetMoney(m.avg_loss, formatCurrency)}
            </StripTile>
          </div>
          <SampleNote sample={summary.sample} />
          {(m.net_unavailable_trades ?? 0) > 0 && (
            <p
              className="text-xs text-amber-700 dark:text-amber-500 [.analyzer_&]:text-amber-500"
              data-testid="net-unavailable-note"
            >
              {m.net_unavailable_trades} completed trade(s) have no net P&amp;L (no charge estimate)
              and are left out of win rate, profit factor, expectancy and the averages.
            </p>
          )}
          {summary.net_definition && (
            <p className="text-xs text-muted-foreground">
              {netDefinitionText(summary.net_definition)}
            </p>
          )}
        </CardContent>
      </Card>

      {summary.consistency ? (
        <RiskStrip
          compact
          consistency={summary.consistency}
          risk={summary.risk}
          formatCurrency={formatCurrency}
          onSelectTrade={onSelectTrade}
        />
      ) : (
        <Card data-testid="group-risk-missing" className="gap-3 py-4">
          <CardHeader className="pb-2">
            <CardTitle className="text-base">Risk and consistency</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">Not sent by the server.</p>
          </CardContent>
        </Card>
      )}

      {(summary.trade_management || summary.costs || summary.hold) && (
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

      <Card data-testid="group-trade-quality" className="gap-3 py-4">
        <CardHeader className="pb-2">
          <CardTitle className="text-base">Trade quality</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2 text-sm">
          <p>
            <span className="text-muted-foreground">Instruments traded: </span>
            <span className="font-mono">{points ? points.length : '—'}</span>{' '}
            <JumpLink to="discretionary-points">Points by instrument</JumpLink>
          </p>
          <p data-testid="quality-excursions">
            <span className="text-muted-foreground">Average excursion in points per unit: </span>
            {w && l ? (
              <span className="font-mono">
                winners MAE {w.mae_points.count > 0 ? fmtDistance(w.mae_points.average) : '—'} / MFE{' '}
                {w.mfe_points.count > 0 ? fmtDistance(w.mfe_points.average) : '—'}; losers MAE{' '}
                {l.mae_points.count > 0 ? fmtDistance(l.mae_points.average) : '—'} / MFE{' '}
                {l.mfe_points.count > 0 ? fmtDistance(l.mfe_points.average) : '—'}
              </span>
            ) : (
              <span className="font-mono">—</span>
            )}{' '}
            <JumpLink to="discretionary-excursion">MAE / MFE</JumpLink>
          </p>
          <p data-testid="quality-capture">
            <span className="text-muted-foreground">Profit capture of winners: </span>
            <span className="font-mono">
              {w && w.profit_capture.count > 0 && w.profit_capture.average != null
                ? fmtPct(w.profit_capture.average * 100)
                : '—'}
            </span>
          </p>
        </CardContent>
      </Card>
    </div>
  )
}
