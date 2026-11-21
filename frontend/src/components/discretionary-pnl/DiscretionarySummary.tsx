import type {
  DiscretionaryToday,
  DiscretionarySummary as Summary,
  TradeRef,
} from '@/api/discretionaryPnl'
import {
  cleanMoney,
  DRAWDOWN_BASIS,
  fmtNetMoney,
  fmtProfitFactor,
  fmtStamp,
  fmtWinRate,
  verdictLine,
  weaknessLine,
} from '@/lib/discretionaryPnlView'
import { cn } from '@/lib/utils'
import { JumpLink, StripTile, Unavailable } from './DiscretionaryInsights'

type Money = (value: number) => string

function Primary({
  label,
  value,
  hint,
  tone,
  testId,
}: {
  label: string
  value: string
  hint?: string
  tone?: 'profit' | 'loss'
  testId: string
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
    </div>
  )
}

function Largest({
  trade,
  formatCurrency,
  none,
}: {
  trade: TradeRef | null | undefined
  formatCurrency: Money
  none: string
}) {
  if (!trade) return <Unavailable reason={none} />
  const amount = trade.net_pnl ?? trade.realized_pnl
  return (
    <span
      className={
        amount > 0
          ? 'text-green-700 dark:text-green-600 [.analyzer_&]:text-green-600'
          : amount < 0
            ? 'text-red-600 dark:text-red-400'
            : undefined
      }
    >
      {formatCurrency(amount)}
      <span className="block break-words font-sans text-[11px] font-normal text-muted-foreground">
        {trade.symbol} {trade.trade_date}
        {trade.net_pnl != null ? ' (net)' : ' (gross)'}
      </span>
    </span>
  )
}

/**
 * The first screen of the page: one verdict sentence, the single biggest weakness, three large
 * figures (net P&L, completed trades, win rate) and one dense row of supporting figures. Every
 * value comes from the summary the rest of the page uses; nothing is computed here.
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
  const net = summary.realized_net_of_charges
  const verdict = verdictLine(summary, formatCurrency)
  const weakness = weaknessLine(summary, formatCurrency)
  const c = summary.consistency
  const risk = summary.risk
  const charges = summary.costs?.total_charges ?? summary.charges
  const gross = summary.realized_gross

  let drawdown: React.ReactNode
  let drawdownHint: string | undefined
  if (risk) {
    drawdown = (
      <span className={risk.max_drawdown > 0 ? 'text-red-600 dark:text-red-400' : undefined}>
        {risk.max_drawdown > 0 ? '-' : ''}
        {formatCurrency(Math.abs(risk.max_drawdown))}
      </span>
    )
    drawdownHint = risk.max_drawdown_at
      ? `at ${fmtStamp(risk.max_drawdown_at)}`
      : 'net, from the Risk card'
  } else if (c?.max_drawdown_unavailable_reason) {
    // one trading day has no peak to fall from: say so instead of showing 0
    drawdown = '—'
    drawdownHint = `${c.max_drawdown_unavailable_reason}; basis: ${DRAWDOWN_BASIS}`
  } else {
    drawdown = (
      <span className="text-red-600 dark:text-red-400">
        {formatCurrency(Math.abs(m.max_drawdown ?? 0))}
      </span>
    )
    drawdownHint = `basis: ${DRAWDOWN_BASIS}`
  }

  return (
    <section className="space-y-3" data-testid="discretionary-summary" aria-label="Summary">
      <div
        className={cn(
          'rounded-lg border-l-4 bg-muted/40 px-4 py-3',
          verdict.tone === 'profit' && 'border-l-green-600',
          verdict.tone === 'loss' && 'border-l-red-600',
          (verdict.tone === 'flat' || verdict.tone === 'none') && 'border-l-muted-foreground'
        )}
      >
        <p className="text-base font-medium" data-testid="summary-verdict">
          {verdict.text}
        </p>
        {weakness && (
          <p className="mt-1 text-sm text-muted-foreground" data-testid="summary-weakness">
            Biggest weakness: {weakness}
          </p>
        )}
        <p className="mt-1 text-xs">
          <JumpLink to="discretionary-ledger">Jump to closed positions</JumpLink>
        </p>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <Primary
          testId="summary-net"
          label="Net realized (after est. charges)"
          value={formatCurrency(net)}
          tone={cleanMoney(net) > 0 ? 'profit' : cleanMoney(net) < 0 ? 'loss' : undefined}
          hint={chargesHint}
        />
        <Primary
          testId="summary-count"
          label="Completed trades"
          value={String(m.trades_count)}
          hint={`${m.winning_trades} wins / ${m.losing_trades} losses`}
        />
        <Primary
          testId="summary-win-rate"
          label="Win rate (net)"
          value={noTrades ? '—' : fmtWinRate(m.win_rate)}
          hint={
            m.count_unavailable_reason
              ? 'no completed trade has a net P&L'
              : 'a win is a profit after charges'
          }
        />
      </div>

      <div
        className="grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-7"
        data-testid="summary-support"
      >
        <StripTile label="Profit factor (net)" hint="total won / total lost">
          {noTrades ? '—' : fmtProfitFactor(m.profit_factor, m.winning_trades, m.losing_trades)}
        </StripTile>
        <StripTile label="Expectancy (net)" hint="net, per trade">
          {noTrades ? '—' : fmtNetMoney(m.expectancy, formatCurrency)}
        </StripTile>
        <StripTile
          label={risk ? 'Max drawdown (net)' : 'Max drawdown'}
          testId="summary-drawdown"
          hint={drawdownHint}
        >
          {drawdown}
        </StripTile>
        <StripTile label="Gross realized" hint="before charges">
          <span
            className={
              gross > 0
                ? 'text-green-700 dark:text-green-600 [.analyzer_&]:text-green-600'
                : gross < 0
                  ? 'text-red-600 dark:text-red-400'
                  : undefined
            }
          >
            {formatCurrency(gross)}
          </span>
        </StripTile>
        <StripTile
          label="Charges (est.)"
          testId="summary-charges"
          hint={
            summary.charges_unavailable_trades > 0
              ? `${summary.charges_unavailable_trades} trade(s) without an estimate`
              : 'estimated'
          }
        >
          {formatCurrency(charges)}
        </StripTile>
        <StripTile label="Largest winner" testId="summary-largest-win">
          <Largest trade={c?.largest_win} formatCurrency={formatCurrency} none="no winning trade" />
        </StripTile>
        <StripTile label="Largest loser" testId="summary-largest-loss">
          <Largest trade={c?.largest_loss} formatCurrency={formatCurrency} none="no losing trade" />
        </StripTile>
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
            figures and closed positions.
          </span>
        ) : (
          <span>
            Today ({today.session_date}): net {formatCurrency(today.net)} after estimated charges,{' '}
            {today.completed_positions} closed, {today.open_positions} open. Show today&apos;s
            figures and closed positions.
          </span>
        )}
      </summary>
      <div className="border-t p-3">{children}</div>
    </details>
  )
}
