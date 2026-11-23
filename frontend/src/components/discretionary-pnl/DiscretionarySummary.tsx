import type { DiscretionarySummary as Summary } from '@/api/discretionaryPnl'
import { Badge } from '@/components/ui/badge'
import {
  cleanMoney,
  fmtWinRate,
  hasNoClosedActivity,
  pnlTextClass,
  type TotalCard,
  type UnrealizedSummary,
  type WinRateParts,
  winRateParts,
} from '@/lib/discretionaryPnlView'
import { cn } from '@/lib/utils'
import { JumpLink } from './DiscretionaryInsights'
import { KeyFigures } from './DiscretionaryStatistics'

type Money = (value: number) => string

function Primary({
  label,
  value,
  hint,
  tone,
  testId,
  children,
}: {
  label: string
  value: string
  hint?: string
  tone?: 'profit' | 'loss'
  testId: string
  children?: React.ReactNode
}) {
  return (
    <div className="min-w-0 rounded-lg border bg-card p-4" data-testid={testId}>
      <div className="text-sm text-muted-foreground">{label}</div>
      <div
        className={cn(
          'mt-1 break-words font-mono text-3xl font-bold',
          tone === 'profit' && 'text-green-600',
          tone === 'loss' && 'text-red-600'
        )}
      >
        {value}
      </div>
      {hint && <div className="mt-1 text-xs text-muted-foreground">{hint}</div>}
      {children}
    </div>
  )
}

/** Wins, losses, break-even and sample size behind the win rate, . */
function WinRateDetail({ parts }: { parts: WinRateParts }) {
  return (
    <div className="mt-1 space-y-0.5 text-xs" data-testid="win-rate-detail">
      <div className="text-muted-foreground" data-testid="win-rate-counts">
        {parts.wins} wins / {parts.losses} losses
        {parts.breakEven != null ? ` / ${parts.breakEven} break-even` : ''}
        {parts.sample != null ? ` of ${parts.sample} trades` : ''}
      </div>
    </div>
  )
}

/**
 * Unrealized P&L and the total with it, under the net tile, only when there is an open position.
 * Not priced (live prices not requested) says so; a partly priced book says how many are
 * unpriced. Never a zero stand-in.
 */
function UnrealizedLine({
  unrealized,
  total,
  priced,
  noMoney,
  partialExits,
  formatCurrency,
}: {
  unrealized: UnrealizedSummary
  total: TotalCard
  priced: boolean
  noMoney: boolean
  partialExits: number
  formatCurrency: Money
}) {
  if (unrealized.kind === 'none') return null
  return (
    <div className="mt-1 space-y-0.5 text-xs" data-testid="summary-unrealized">
      {priced ? (
        <>
          <div>
            <span className="text-muted-foreground">Unrealized: </span>
            <span className={cn('font-mono', pnlTextClass(unrealized.value))}>
              {formatCurrency(unrealized.value)}
            </span>
            {unrealized.kind === 'partial' && (
              <span className="text-muted-foreground">
                {' '}
                (
                {unrealized.unpriced > 0
                  ? `${unrealized.unpriced} position(s) unpriced`
                  : 'some positions not shown'}
                )
              </span>
            )}
            {unrealized.badge && (
              <Badge
                variant="outline"
                className="ml-1.5 text-[11px]"
                title={unrealized.badge.title}
                data-testid="unrealized-price-badge"
              >
                {unrealized.badge.label}
              </Badge>
            )}
          </div>
          {!noMoney && (
            <div data-testid="summary-total">
              <span className="text-muted-foreground">{total.label}: </span>
              <span className={cn('font-mono', pnlTextClass(total.value))}>
                {formatCurrency(total.value)}
              </span>
            </div>
          )}
        </>
      ) : (
        <div className="text-muted-foreground" title="The net above is realized only.">
          Unrealized: not requested. Turn on Fetch live prices.
        </div>
      )}
      {partialExits > 0 && (
        <div className="text-muted-foreground" data-testid="summary-partial-exits">
          Net includes exits already made on {partialExits} open position
          {partialExits === 1 ? '' : 's'}.
        </div>
      )}
    </div>
  )
}

/**
 * The first screen of the page: three large figures (net P&L, completed trades, win rate) and a
 * compact row under them (net expectancy, profit factor, maximum drawdown). The other statistics
 * (gross, charges, averages, largest winner / loser) are in Trading statistics, so each figure has
 * one home. Every value comes
 * from the summary the rest of the page uses; nothing is computed here.
 */
export function DiscretionaryHeader({
  summary,
  formatCurrency,
  chargesHint,
  unrealized,
  total,
  coverageNote,
  children,
}: {
  summary: Summary
  formatCurrency: Money
  chargesHint: string
  unrealized: UnrealizedSummary
  total: TotalCard
  /** Quiet coverage note shown directly under the key figures. */
  coverageNote?: React.ReactNode
  children?: React.ReactNode
}) {
  const m = summary.metrics
  const noTrades = m.trades_count === 0
  const noMoney = hasNoClosedActivity(summary)
  const net = summary.realized_net_of_charges
  const priced = unrealized.kind === 'complete' || unrealized.kind === 'partial'
  // Exits already made on positions that are still open are in this net (and in the calendar).
  const partialExits = (summary.positions ?? []).filter(
    (p) => p.realized_so_far != null && p.realized_so_far !== 0
  ).length
  const netHint = noMoney ? 'no trade was closed in this date range' : chargesHint
  return (
    <section className="space-y-3" data-testid="discretionary-summary" aria-label="Summary">
      <p className="text-xs">
        <JumpLink to="discretionary-ledger">Jump to closed trades</JumpLink>
      </p>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <Primary
          testId="summary-net"
          label="Net realized (after est. charges)"
          value={noMoney ? '—' : formatCurrency(net)}
          tone={
            noMoney
              ? undefined
              : cleanMoney(net) > 0
                ? 'profit'
                : cleanMoney(net) < 0
                  ? 'loss'
                  : undefined
          }
          hint={netHint}
        >
          <UnrealizedLine
            unrealized={unrealized}
            total={total}
            priced={priced}
            noMoney={noMoney}
            partialExits={partialExits}
            formatCurrency={formatCurrency}
          />
        </Primary>
        <Primary
          testId="summary-count"
          label="Completed trades"
          value={String(m.trades_count)}
          hint={noTrades ? undefined : `${m.winning_trades} wins / ${m.losing_trades} losses`}
        />
        <Primary
          testId="summary-win-rate"
          label="Win rate (net)"
          value={noTrades ? '—' : fmtWinRate(m.win_rate)}
          hint={
            noTrades
              ? 'no completed trades in this date range'
              : m.count_unavailable_reason
                ? 'no completed trade has a net P&L'
                : 'a win is a profit after estimated charges'
          }
        >
          {!noTrades && !m.count_unavailable_reason && <WinRateDetail parts={winRateParts(m)} />}
        </Primary>
      </div>

      <KeyFigures summary={summary} formatCurrency={formatCurrency} />

      {coverageNote}

      {children}
    </section>
  )
}
