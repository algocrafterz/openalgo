import type { DiscretionarySummary as Summary, TradeRef } from '@/api/discretionaryPnl'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  chargesInsightHint,
  fmtAvgByCount,
  fmtNetMoney,
  fmtPayoffRatio,
  fmtProfitFactor,
  fmtStamp,
  hasNoClosedActivity,
  largestLossHint,
  NET_DEFINITION_DETAIL,
  netDefinitionText,
  pnlTextClass,
} from '@/lib/discretionaryPnlView'
import { cn } from '@/lib/utils'
import { SampleNote, TradeButton, Unavailable } from './DiscretionaryInsights'

type Money = (value: number) => string

type Tone = 'positive' | 'negative' | 'neutral'

const BAR_TONE: Record<Tone, string> = {
  positive: 'border-l-green-600',
  negative: 'border-l-red-600',
  neutral: 'border-l-border',
}

function toneOf(value: number | null | undefined): Tone {
  if (value == null || !Number.isFinite(value) || value === 0) return 'neutral'
  return value > 0 ? 'positive' : 'negative'
}

/** One statistic: a label with a tooltip, one figure, and a coloured edge for good or bad. */
function Stat({
  label,
  tooltip,
  tone = 'neutral',
  hint,
  testId,
  size = 'support',
  children,
}: {
  label: string
  tooltip: string
  tone?: Tone
  hint?: React.ReactNode
  testId?: string
  /** primary: the figures that decide whether the way you trade works; support: the rest. */
  size?: 'primary' | 'support'
  children: React.ReactNode
}) {
  const primary = size === 'primary'
  return (
    <div
      className={cn(
        'min-w-0 rounded-md border border-l-4',
        primary ? 'p-4 shadow-sm' : 'p-2.5',
        BAR_TONE[tone]
      )}
      data-testid={testId}
      data-tone={tone}
      data-size={size}
      title={tooltip}
    >
      <div
        className={cn(
          'cursor-help text-muted-foreground',
          primary ? 'text-sm font-medium' : 'text-xs'
        )}
      >
        {label}
      </div>
      <div
        className={cn(
          'mt-1 break-words font-mono font-semibold',
          primary ? 'text-2xl' : 'text-base'
        )}
      >
        {children}
      </div>
      {hint && <div className="mt-0.5 text-[11px] text-muted-foreground">{hint}</div>}
    </div>
  )
}

function Largest({
  trade,
  formatCurrency,
  none,
  onSelect,
}: {
  trade: TradeRef | null | undefined
  formatCurrency: Money
  none: string
  onSelect?: (trade: TradeRef) => void
}) {
  if (!trade) return <Unavailable reason={none} />
  if (onSelect)
    return <TradeButton trade={trade} formatCurrency={formatCurrency} onSelect={onSelect} />
  const amount = trade.net_pnl ?? trade.realized_pnl
  return (
    <span className={pnlTextClass(amount)}>
      {formatCurrency(amount)}
      <span className="block break-words font-sans text-[11px] font-normal text-muted-foreground">
        {trade.symbol} {trade.trade_date}
        {trade.net_pnl != null ? ' (net)' : ' (gross)'}
      </span>
    </span>
  )
}

/**
 * The second row under the headline summary: net expectancy, profit factor and the maximum
 * drawdown. The labels, tooltips, hints, sample sizes and the n/a and dash rules are the ones the
 * Trading statistics card used before they moved here; nothing is computed differently.
 */
export function KeyFigures({
  summary,
  formatCurrency,
}: {
  summary: Summary
  formatCurrency: Money
}) {
  const m = summary.metrics
  const noTrades = m.trades_count === 0
  const noMoney = hasNoClosedActivity(summary)
  const risk = summary.risk
  // Only the net drawdown by position close is shown. A server without it sends no drawdown
  // tile at all: the gross daily figure is a different basis and a missing value is never 0.
  const drawdownTone: Tone = risk && risk.max_drawdown > 0 ? 'negative' : 'neutral'
  const pf = m.profit_factor
  const wins = m.winning_trades
  const losses = m.losing_trades
  // No position has a net P&L: every net figure is unknown (a dash), not "n/a".
  const hasNetBasis = (m.net_basis_trades ?? m.trades_count) > 0 && !m.count_unavailable_reason
  const pfTone: Tone =
    pf == null
      ? wins > 0 && losses === 0
        ? 'positive'
        : 'neutral'
      : wins === 0 && losses === 0
        ? 'neutral'
        : pf > 1
          ? 'positive'
          : pf < 1
            ? 'negative'
            : 'neutral'
  return (
    <div
      className={cn(
        'grid grid-cols-1 gap-3',
        risk && !noMoney ? 'sm:grid-cols-3' : 'sm:grid-cols-2'
      )}
      data-testid="summary-key-stats"
    >
      <Stat
        size="support"
        label="Expectancy (net)"
        tooltip="The average net result of one completed trade, after estimated charges."
        tone={noTrades ? 'neutral' : toneOf(m.expectancy)}
        hint="net, per trade"
      >
        {noTrades ? (
          '—'
        ) : (
          <span className={pnlTextClass(m.expectancy ?? 0)}>
            {fmtNetMoney(m.expectancy, formatCurrency)}
          </span>
        )}
      </Stat>
      <Stat
        size="support"
        label="Profit factor (net)"
        tooltip="Total won divided by total lost, after estimated charges. Above 1.00 means winners outweigh losers."
        tone={pfTone}
        hint={
          noMoney
            ? 'total won / total lost'
            : `won ${formatCurrency(m.gross_profit)} / lost ${formatCurrency(m.gross_loss)} (net, completed trades)`
        }
      >
        {noTrades
          ? '—'
          : fmtProfitFactor(pf, wins, losses, !m.count_unavailable_reason && hasNetBasis)}
      </Stat>
      {risk && !noMoney && (
        <Stat
          size="support"
          label="Max drawdown (net)"
          tooltip="The largest fall of your cumulative net realised P&L (after estimated charges, in close order) from its highest point so far, in rupees."
          testId="summary-drawdown"
          tone={drawdownTone}
          hint={
            <>
              {`over ${risk.positions} trade${risk.positions === 1 ? '' : 's'}${risk.max_drawdown_at ? `, at ${fmtStamp(risk.max_drawdown_at)}` : ''}`}
              <span className="block" data-testid="summary-current-drawdown">
                {risk.current_drawdown > 0
                  ? `now ${formatCurrency(risk.current_drawdown)} below the highest point (net, by close time)`
                  : 'now at its highest point (net, by close time)'}
              </span>
            </>
          }
        >
          <span className={risk.max_drawdown > 0 ? 'text-red-600 dark:text-red-400' : undefined}>
            {risk.max_drawdown > 0 ? '-' : ''}
            {formatCurrency(Math.abs(risk.max_drawdown))}
          </span>
        </Stat>
      )}
    </div>
  )
}

/**
 * Trading statistics: the one home of gross P&L, estimated charges, average winning / losing
 * trade, payoff ratio and largest winner / loser. Net P&L, the trade count and the win rate are
 * the summary cards; net expectancy, profit factor (with total won and lost) and the maximum
 * drawdown (with the current one) are the row under them (KeyFigures), so each figure is shown
 * once.
 * Every value comes from the summary the rest of the page uses; nothing is computed here.
 */
export function DiscretionaryStatistics({
  summary,
  formatCurrency,
  onSelectTrade,
}: {
  summary: Summary
  formatCurrency: Money
  /** Opens the fills of the largest winner / loser. */
  onSelectTrade?: (trade: TradeRef) => void
}) {
  const m = summary.metrics
  const noTrades = m.trades_count === 0
  const noMoney = hasNoClosedActivity(summary)
  const c = summary.consistency
  const charges = summary.costs?.total_charges ?? summary.charges
  const gross = summary.realized_gross

  const wins = m.winning_trades
  const losses = m.losing_trades
  // No position has a net P&L: every net figure is unknown (a dash), not "n/a".
  const hasNetBasis = (m.net_basis_trades ?? m.trades_count) > 0 && !m.count_unavailable_reason
  const largestWin = c?.largest_win
  const largestLoss = c?.largest_loss

  return (
    <Card data-testid="discretionary-statistics" className="gap-3 py-4">
      <CardHeader className="pb-0">
        <CardTitle className="text-base">Trading statistics</CardTitle>
        <p className="text-xs text-muted-foreground">Hover a figure for its meaning.</p>
      </CardHeader>
      <CardContent className="space-y-3">
        <div
          className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-7"
          data-testid="stats-supporting"
        >
          <Stat
            label="Gross realized"
            tooltip="Realised profit and loss before charges."
            tone={noMoney ? 'neutral' : toneOf(gross)}
            hint="before charges"
          >
            {noMoney ? '—' : <span className={pnlTextClass(gross)}>{formatCurrency(gross)}</span>}
          </Stat>
          <Stat
            label="Charges (est.)"
            tooltip="Brokerage, taxes and exchange fees, estimated for the completed trades. They reduce your net P&L."
            testId="summary-charges"
            hint={[
              summary.charges_unavailable_trades > 0
                ? `${summary.charges_unavailable_trades} trade(s) without an estimate`
                : 'estimated',
              chargesInsightHint(summary, formatCurrency),
            ]
              .filter(Boolean)
              .join('; ')}
          >
            {noMoney ? '—' : formatCurrency(charges)}
          </Stat>
          <Stat
            label="Avg win (net)"
            tooltip="The average net profit of a winning trade, after estimated charges. n/a when there is no winning trade."
            tone={noTrades || wins === 0 ? 'neutral' : 'positive'}
            testId="stat-avg-win"
          >
            <span className={wins > 0 ? pnlTextClass(m.avg_win ?? 0) : undefined}>
              {noTrades || !hasNetBasis ? '—' : fmtAvgByCount(m.avg_win, wins, formatCurrency)}
            </span>
          </Stat>
          <Stat
            label="Avg loss (net)"
            tooltip="The average net loss of a losing trade, after estimated charges. n/a when there is no losing trade."
            tone={noTrades || losses === 0 ? 'neutral' : 'negative'}
            testId="stat-avg-loss"
          >
            <span className={losses > 0 ? pnlTextClass(m.avg_loss ?? 0) : undefined}>
              {noTrades || !hasNetBasis ? '—' : fmtAvgByCount(m.avg_loss, losses, formatCurrency)}
            </span>
          </Stat>
          <Stat
            label="Payoff ratio (net)"
            tooltip="Average winning trade divided by average losing trade, after estimated charges. n/a unless there is at least one win and one loss."
            testId="stat-payoff"
            hint="avg win / avg loss"
          >
            {noTrades || !hasNetBasis ? '—' : fmtPayoffRatio(m.avg_win, m.avg_loss, wins, losses)}
          </Stat>
          <Stat
            label="Largest winner"
            tooltip="Your best completed trade in this period. Select it to see its fills."
            testId="summary-largest-win"
            tone={largestWin ? 'positive' : 'neutral'}
          >
            <Largest
              trade={largestWin}
              formatCurrency={formatCurrency}
              none="no winning trade"
              onSelect={onSelectTrade}
            />
          </Stat>
          <Stat
            label="Largest loser"
            tooltip="Your worst completed trade in this period. Select it to see its fills."
            testId="summary-largest-loss"
            tone={largestLoss ? 'negative' : 'neutral'}
            hint={largestLossHint(summary, formatCurrency) ?? undefined}
          >
            <Largest
              trade={largestLoss}
              formatCurrency={formatCurrency}
              none="no losing trade"
              onSelect={onSelectTrade}
            />
          </Stat>
        </div>

        <SampleNote sample={summary.sample} />
        {(m.net_unavailable_trades ?? 0) > 0 && (
          <p
            className="text-xs text-amber-700 dark:text-amber-500 [.analyzer_&]:text-amber-500"
            data-testid="net-unavailable-note"
          >
            {m.net_unavailable_trades} completed trade(s) have no charge estimate and are left out
            of net statistics.
          </p>
        )}
        {summary.net_definition && (
          <p className="text-xs text-muted-foreground" title={NET_DEFINITION_DETAIL}>
            {netDefinitionText(summary.net_definition)}
          </p>
        )}
      </CardContent>
    </Card>
  )
}
