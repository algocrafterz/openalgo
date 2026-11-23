import type { DiscretionaryToday, DiscretionarySummary as Summary } from '@/api/discretionaryPnl'
import {
  cleanMoney,
  fmtWinRate,
  hasNoClosedActivity,
  type WinRateParts,
  weaknessLine,
  winRateParts,
} from '@/lib/discretionaryPnlView'
import { cn } from '@/lib/utils'
import { JumpLink } from './DiscretionaryInsights'

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
 * The first screen of the page: three large figures (net P&L, completed trades, win rate). The
 * other statistics (gross, charges, profit factor, expectancy, averages, largest winner / loser,
 * maximum drawdown) are in Trading statistics, so each figure has one home. Every value comes
 * from the summary the rest of the page uses; nothing is computed here.
 */
export function DiscretionaryHeader({
  summary,
  formatCurrency,
  chargesHint,
  children,
}: {
  summary: Summary
  formatCurrency: Money
  chargesHint: string
  children?: React.ReactNode
}) {
  const m = summary.metrics
  const noTrades = m.trades_count === 0
  const noMoney = hasNoClosedActivity(summary)
  const net = summary.realized_net_of_charges
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
          hint={noMoney ? 'no trade was closed in this date range' : chargesHint}
        />
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

      {children}
    </section>
  )
}

/**
 * Today's session as one line under the summary. When the selected range is not just today's
 * session it says what today looks like; either way the full Today figures are one click away
 * (children), so nothing is dropped.
 */
export function TodayLine({
  today,
  range,
  formatCurrency,
  children,
}: {
  today: DiscretionaryToday
  range: { start: string; end: string } | undefined
  formatCurrency: Money
  children: React.ReactNode
}) {
  const same = !!range && range.start === today.session_date && range.end === today.session_date
  return (
    <details className="rounded-md border" data-testid="today-line">
      <summary className="cursor-pointer px-3 py-2 text-sm">
        {same ? (
          <span>
            Today&apos;s session ({today.session_date}) is the range shown above. Show today&apos;s
            figures and closed trades.
          </span>
        ) : (
          <span>
            Today ({today.session_date}):{' '}
            {today.completed_positions === 0 && today.exit_legs === 0
              ? 'no closed trades'
              : `net ${formatCurrency(today.net)} after estimated charges, ${today.completed_positions} closed`}
            , {today.open_positions} open. Show today&apos;s figures and closed trades.
          </span>
        )}
      </summary>
      <div className="border-t p-3">{children}</div>
    </details>
  )
}

/**
 * Trading insights: the one thing to look at, taken from numbers the server already sends. The
 * net P&L, trade count and win rate are in the summary cards, so they are not repeated here.
 * Nothing is rendered when there is nothing to add. Factual only: no inference about discipline.
 */
export function TradingInsights({
  summary,
  formatCurrency,
}: {
  summary: Summary
  formatCurrency: Money
}) {
  const weakness = weaknessLine(summary, formatCurrency)
  if (!weakness) return null
  return (
    <section
      className="rounded-lg border-l-4 border-l-muted-foreground bg-muted/40 px-4 py-3"
      data-testid="trading-insights"
      aria-label="Trading insights"
    >
      <h2 className="mb-1 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
        Trading insights
      </h2>
      <p className="text-sm" data-testid="summary-weakness">
        Needs attention: {weakness}
      </p>
    </section>
  )
}
