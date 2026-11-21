import { Clock } from 'lucide-react'
import type { DiscretionaryToday, TodayClosedPosition } from '@/api/discretionaryPnl'
import { MetricCard } from '@/components/strategy-pnl/MetricCard'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import {
  chargesBadge,
  fmtClock,
  fmtPrice,
  fmtQty,
  fmtStamp,
  pendingAttributionLines,
  pnlTextClass,
  todayUnrealized,
} from '@/lib/discretionaryPnlView'
import { fmtDate } from '@/lib/strategyPnlView'
import { cn } from '@/lib/utils'

type Money = (value: number) => string

/** Entry time: the clock for a same-session entry, the full stamp for a carried position. */
function entryTime(row: TodayClosedPosition, session: string): string {
  return row.opened_session && row.opened_session !== session
    ? fmtStamp(row.opened_at)
    : fmtClock(row.opened_at)
}

export function TodayClosedTable({
  rows,
  session,
  formatCurrency,
  onSelect,
}: {
  rows: TodayClosedPosition[]
  session: string
  formatCurrency: Money
  onSelect?: (id: number) => void
}) {
  if (rows.length === 0) {
    return (
      <p className="text-sm text-muted-foreground" data-testid="today-closed-empty">
        No positions closed today yet
      </p>
    )
  }
  return (
    <div className="overflow-x-auto">
      <Table data-testid="today-closed-table">
        <TableHeader>
          <TableRow>
            <TableHead>Symbol</TableHead>
            <TableHead>Direction</TableHead>
            <TableHead className="text-right">Total qty</TableHead>
            <TableHead className="text-right">Entry price</TableHead>
            <TableHead className="text-right">Exit price</TableHead>
            <TableHead>Entry time</TableHead>
            <TableHead>Final exit</TableHead>
            <TableHead className="text-right">Gross P&amp;L</TableHead>
            <TableHead className="text-right">Est. charges</TableHead>
            <TableHead className="text-right">Net P&amp;L</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Account</TableHead>
            <TableHead>Broker</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((r) => {
            const charges = chargesBadge(r)
            return (
              <TableRow
                key={r.id}
                className={cn(onSelect && 'cursor-pointer')}
                onClick={onSelect ? () => onSelect(r.id) : undefined}
                data-testid="today-closed-row"
              >
                <TableCell className="font-medium">
                  {r.symbol}
                  <span className="ml-1 text-xs text-muted-foreground">
                    {r.exchange} {r.product}
                  </span>
                </TableCell>
                <TableCell>{r.direction === 'LONG' ? 'Long' : 'Short'}</TableCell>
                <TableCell className="text-right font-mono">
                  {fmtQty(r.total_quantity)}
                  {r.legs > 1 && (
                    <span className="ml-1 text-xs text-muted-foreground">
                      ({r.legs} exit fills)
                    </span>
                  )}
                </TableCell>
                <TableCell className="text-right font-mono">{fmtPrice(r.entry_price)}</TableCell>
                <TableCell className="text-right font-mono">{fmtPrice(r.exit_price)}</TableCell>
                <TableCell className="whitespace-nowrap">{entryTime(r, session)}</TableCell>
                <TableCell className="whitespace-nowrap">{fmtClock(r.closed_at)}</TableCell>
                <TableCell className={cn('text-right font-mono', pnlTextClass(r.gross_pnl))}>
                  {formatCurrency(r.gross_pnl)}
                </TableCell>
                <TableCell className="text-right font-mono" title={charges.title}>
                  {r.charges == null ? (
                    <span className="text-amber-700 dark:text-amber-500 [.analyzer_&]:text-amber-500">
                      unavailable
                    </span>
                  ) : (
                    formatCurrency(r.charges)
                  )}
                </TableCell>
                <TableCell className={cn('text-right font-mono', pnlTextClass(r.net_pnl ?? 0))}>
                  {r.net_pnl == null ? '—' : formatCurrency(r.net_pnl)}
                </TableCell>
                <TableCell>
                  <Badge variant="outline">{r.status}</Badge>
                </TableCell>
                <TableCell>{r.account || '—'}</TableCell>
                <TableCell>{r.broker}</TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    </div>
  )
}

/**
 * "Today's Performance": the current trading session from the same rebuilt book as the rest of
 * the page (the server's `today` block). Actual data only: unresolved fills, including those
 * inside the grace window, are listed in the note and never counted.
 */
export function TodayPerformance({
  today,
  formatCurrency,
  onSelectClosed,
}: {
  today: DiscretionaryToday
  formatCurrency: Money
  onSelectClosed?: (id: number) => void
}) {
  const unrealized = todayUnrealized(today)
  const pending = pendingAttributionLines(today.pending_attribution)
  const m = today.metrics ?? {}
  const unrealizedValue =
    unrealized.kind === 'complete' || unrealized.kind === 'partial'
      ? formatCurrency(unrealized.value)
      : '—'
  const unrealizedHint =
    unrealized.kind === 'none'
      ? today.open_positions === 0
        ? 'no open positions'
        : 'no usable price'
      : unrealized.kind === 'not_requested'
        ? 'turn on Fetch live prices'
        : unrealized.kind === 'partial'
          ? `${unrealized.unpriced} position(s) unpriced`
          : undefined
  const carried = today.closed_positions.some(
    (r) => Math.abs(r.gross_pnl - r.realized_today) > 1e-6
  )

  return (
    <Card data-testid="discretionary-today">
      <CardHeader className="pb-3">
        <CardTitle className="flex flex-wrap items-baseline gap-2">
          Today&apos;s Performance
          <span className="text-sm font-normal text-muted-foreground">
            session {fmtDate(today.session_date)}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
          <MetricCard
            label="Realised (gross)"
            value={formatCurrency(today.realized_gross)}
            positive={today.realized_gross > 0}
            negative={today.realized_gross < 0}
            hint={`${today.exit_legs} exit fills (incl. partial exits)`}
          />
          <Card data-testid="today-net">
            <CardContent className="py-4">
              <div className="text-xs text-muted-foreground">Net after est. charges</div>
              <div
                className={cn(
                  'text-xl font-bold font-mono mt-1 break-words',
                  pnlTextClass(today.net)
                )}
              >
                {formatCurrency(today.net)}
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-1.5">
                {today.charges_complete ? (
                  <span className="text-[11px] text-muted-foreground">
                    after {formatCurrency(today.charges)} charges
                  </span>
                ) : (
                  <Badge
                    variant="outline"
                    className="text-[11px] border-amber-500/50 text-amber-700 dark:text-amber-500 [.analyzer_&]:text-amber-500"
                    title={`${today.charges_unavailable_legs} exit fills have no charge estimate: counted as 0`}
                    data-testid="today-charges-incomplete"
                  >
                    incomplete
                  </Badge>
                )}
              </div>
            </CardContent>
          </Card>
          <Card data-testid="today-unrealized">
            <CardContent className="py-4">
              <div className="text-xs text-muted-foreground">Unrealised</div>
              <div
                className={cn(
                  'text-xl font-bold font-mono mt-1 break-words',
                  (unrealized.kind === 'complete' || unrealized.kind === 'partial') &&
                    pnlTextClass(unrealized.value)
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
                    data-testid="today-price-badge"
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
            label="Completed positions"
            value={String(today.completed_positions)}
            hint={`${m.winning_trades ?? 0} wins / ${m.losing_trades ?? 0} losses`}
          />
          <MetricCard label="Open positions" value={String(today.open_positions)} />
          <MetricCard
            label="Partially closed"
            value={String(today.partially_closed_positions)}
            hint="open, with exits counted in realised"
          />
        </div>

        {pending.length > 0 && (
          <div
            className="flex gap-2 rounded-md border border-dashed p-3 text-sm"
            data-testid="today-pending"
          >
            <Clock className="h-4 w-4 mt-0.5 shrink-0 text-muted-foreground" />
            <div className="space-y-1">
              {pending.map((line) => (
                <p key={line}>{line}</p>
              ))}
            </div>
          </div>
        )}

        <div>
          <div className="text-sm font-medium mb-2">Today&apos;s closed positions</div>
          <TodayClosedTable
            rows={today.closed_positions}
            session={today.session_date}
            formatCurrency={formatCurrency}
            onSelect={onSelectClosed}
          />
          {carried && (
            <p className="mt-2 text-xs text-muted-foreground">
              Gross P&amp;L is the whole position; exits made on earlier days are in those days'
              realised figures, not in today's.
            </p>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
