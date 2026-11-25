import { Clock } from 'lucide-react'
import type { DiscretionaryToday } from '@/api/discretionaryPnl'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  attributionCountsLine,
  pendingAttributionLines,
  pnlTextClass,
  todayUnrealized,
} from '@/lib/discretionaryPnlView'
import { cn } from '@/lib/utils'

type Money = (value: number) => string

/**
 * Today's session as one line: net after estimated charges, closed and open positions (or that
 * nothing was closed). The exit-fill count, partially closed positions, gross, unrealised and the
 * win / loss split are hint text under it. Unresolved fills are listed and never counted. The
 * closed trades themselves are in the Closed trades table; "Show today's trades" makes today the
 * report date when it is not already. Actual data only, from the server's `today` block.
 */
export function TodayLine({
  today,
  formatCurrency,
  onShowToday,
  provisionalShown,
  historicalFills = 0,
}: {
  today: DiscretionaryToday
  formatCurrency: Money
  /** Makes today the report date; omitted when the page cannot, or today already is. */
  onShowToday?: () => void
  /** True when the server sent provisional executions: 'no closed trades' then reads 'no confirmed closed trades'. */
  provisionalShown?: boolean
  /** Provisional fills outside the attribution window (from the server's counts); 0 omits the clause. */
  historicalFills?: number
}) {
  const unrealized = todayUnrealized(today)
  const pendingBase = pendingAttributionLines(today.pending_attribution)
  const counts = attributionCountsLine(today.pending_attribution?.grace_count ?? 0, historicalFills)
  // the counts line is added only when it tells the two groups apart (historical fills exist)
  const pending = historicalFills > 0 && counts ? [counts, ...pendingBase] : pendingBase
  const executionsPending =
    (today.pending_attribution?.grace_count ?? 0) > 0 ||
    (today.pending_attribution?.other_today ?? 0) > 0 ||
    !!provisionalShown
  const m = today.metrics ?? {}
  const nothingClosed = today.completed_positions === 0 && today.exit_legs === 0
  const priced = unrealized.kind === 'complete' || unrealized.kind === 'partial'
  const unrealizedText = priced
    ? `unrealised ${formatCurrency(unrealized.value)}${
        unrealized.kind === 'partial' ? ` (${unrealized.unpriced} position(s) unpriced)` : ''
      }`
    : unrealized.kind === 'not_requested'
      ? 'unrealised: turn on Fetch live prices'
      : unrealized.kind === 'none' && today.open_positions > 0
        ? 'unrealised: no usable price'
        : null
  const hint = [
    nothingClosed ? null : `gross ${formatCurrency(today.realized_gross)}`,
    `${today.exit_legs} exit fills (incl. partial exits)`,
    `${today.partially_closed_positions} partially closed`,
    nothingClosed ? null : `${m.winning_trades ?? 0} wins / ${m.losing_trades ?? 0} losses`,
    today.charges_complete && !nothingClosed ? `charges ${formatCurrency(today.charges)}` : null,
    unrealizedText,
  ]
    .filter(Boolean)
    .join('; ')

  return (
    <div className="space-y-1 rounded-md border px-3 py-2" data-testid="today-line">
      <p className="flex flex-wrap items-baseline gap-x-1.5 text-sm" data-testid="today-summary">
        <span>Today ({today.session_date}): </span>
        {nothingClosed ? (
          <span>
            {executionsPending ? 'no confirmed closed trades' : 'no closed trades'},{' '}
            {today.open_positions} open.
          </span>
        ) : (
          <span>
            net{' '}
            <span className={cn('font-mono', pnlTextClass(today.net))} data-testid="today-net">
              {formatCurrency(today.net)}
            </span>{' '}
            after estimated charges, {today.completed_positions} closed, {today.open_positions}{' '}
            open.
          </span>
        )}
        {!today.charges_complete && (
          <Badge
            variant="outline"
            className="text-[11px] border-amber-500/50 text-amber-700 dark:text-amber-500 [.analyzer_&]:text-amber-500"
            title={`${today.charges_unavailable_legs} exit fills have no charge estimate: counted as 0`}
            data-testid="today-charges-incomplete"
          >
            incomplete
          </Badge>
        )}
        {unrealized.badge && priced && (
          <Badge
            variant="outline"
            className="text-[11px]"
            title={unrealized.badge.title}
            data-testid="today-price-badge"
          >
            {unrealized.badge.label}
          </Badge>
        )}
        {onShowToday && (
          <Button
            type="button"
            variant="link"
            size="sm"
            className="h-auto p-0 text-xs"
            onClick={onShowToday}
          >
            Show today&apos;s trades
          </Button>
        )}
      </p>
      <p className="text-xs text-muted-foreground" data-testid="today-hint">
        {hint}
      </p>
      {pending.length > 0 && (
        <div className="flex gap-2 text-xs" data-testid="today-pending">
          <Clock className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          <div className="space-y-0.5">
            {pending.map((line) => (
              <p key={line}>{line}</p>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
