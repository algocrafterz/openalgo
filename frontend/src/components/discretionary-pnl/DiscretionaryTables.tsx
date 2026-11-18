import type {
  BreakdownRow,
  DiscretionaryPosition,
  DiscretionaryTrade,
  PeriodRow,
} from '@/api/discretionaryPnl'
import { Badge } from '@/components/ui/badge'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import {
  breakdownLabel,
  chargesBadge,
  netIncomplete,
  fmtPrice,
  fmtProfitFactor,
  fmtQty,
  fmtWinRate,
  type PriceBadge,
  priceBadge,
} from '@/lib/discretionaryPnlView'
import { fmtDuration, pnlClass } from '@/lib/strategyPnlView'
import { cn } from '@/lib/utils'

type Money = (value: number) => string

/** Marks a net figure that leaves out charges it could not estimate. */
function IncompleteMark({ count }: { count: number }) {
  return (
    <span
      className="ml-1 text-xs text-amber-600"
      title={`${count} trade(s) have no charge estimate`}
    >
      incomplete
    </span>
  )
}

const TONE_CLASS: Record<PriceBadge['tone'], string> = {
  ok: 'border-green-600/40 text-green-600',
  warn: 'border-amber-500/50 text-amber-600',
  bad: 'border-red-600/40 text-red-600',
  muted: 'text-muted-foreground',
}

export function PriceStatusBadge({
  status,
  ageSeconds,
}: {
  status: string
  ageSeconds?: number | null
}) {
  const badge = priceBadge(status, ageSeconds)
  return (
    <Badge variant="outline" className={TONE_CLASS[badge.tone]} title={badge.title}>
      {badge.label}
    </Badge>
  )
}

/** One row per day, week or month of closes. */
export function PeriodTable({
  rows,
  formatCurrency,
  emptyText = 'No closed trades in this range',
}: {
  rows: PeriodRow[]
  formatCurrency: Money
  emptyText?: string
}) {
  if (rows.length === 0) return <p className="text-sm text-muted-foreground">{emptyText}</p>
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Period</TableHead>
          <TableHead className="text-right">Trades</TableHead>
          <TableHead className="text-right">Wins / Losses</TableHead>
          <TableHead className="text-right">Gross P&amp;L</TableHead>
          <TableHead className="text-right">Est. charges</TableHead>
          <TableHead className="text-right">Net of charges</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) => (
          <TableRow key={row.period}>
            <TableCell className="font-medium" title={`${row.start} to ${row.end}`}>
              {row.label}
            </TableCell>
            <TableCell className="text-right font-mono">{row.trades}</TableCell>
            <TableCell className="text-right font-mono">
              {row.wins} / {row.losses}
            </TableCell>
            <TableCell className={cn('text-right font-mono', pnlClass(row.realized_gross))}>
              {formatCurrency(row.realized_gross)}
            </TableCell>
            <TableCell className="text-right font-mono text-muted-foreground">
              {formatCurrency(row.charges)}
            </TableCell>
            <TableCell className={cn('text-right font-mono', pnlClass(row.net_of_charges))}>
              {formatCurrency(row.net_of_charges)}
              {netIncomplete(row) && <IncompleteMark count={row.charges_unavailable_trades} />}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

/** Stats per group: symbol, side, product or holding bucket. */
export function BreakdownTable({
  title,
  firstColumn,
  rows,
  formatCurrency,
}: {
  title: string
  firstColumn: string
  rows: BreakdownRow[]
  formatCurrency: Money
}) {
  return (
    <div className="min-w-0 overflow-x-auto">
      <div className="text-sm font-medium mb-2">{title}</div>
      {rows.length === 0 ? (
        <div className="text-sm text-muted-foreground">None in this period</div>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{firstColumn}</TableHead>
              <TableHead className="text-right">Trades</TableHead>
              <TableHead className="text-right">Win rate</TableHead>
              <TableHead className="text-right">Profit factor</TableHead>
              <TableHead className="text-right">Avg hold</TableHead>
              <TableHead className="text-right">Gross P&amp;L</TableHead>
              <TableHead className="text-right">Net of charges</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row) => (
              <TableRow key={row.key}>
                <TableCell className="font-medium">{breakdownLabel(row.key ?? '')}</TableCell>
                <TableCell className="text-right font-mono">{row.trades}</TableCell>
                <TableCell className="text-right font-mono">
                  {row.trades === 0 ? '—' : fmtWinRate(row.win_rate)}
                </TableCell>
                <TableCell className="text-right font-mono">
                  {row.trades === 0
                    ? '—'
                    : fmtProfitFactor(row.profit_factor, row.wins, row.losses)}
                </TableCell>
                <TableCell className="text-right font-mono">
                  {fmtDuration(row.avg_hold_minutes)}
                </TableCell>
                <TableCell className={cn('text-right font-mono', pnlClass(row.realized_gross))}>
                  {formatCurrency(row.realized_gross)}
                </TableCell>
                <TableCell className={cn('text-right font-mono', pnlClass(row.net_of_charges))}>
                  {formatCurrency(row.net_of_charges)}
                  {netIncomplete(row) && <IncompleteMark count={row.charges_unavailable_trades} />}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  )
}

function DirectionBadge({ direction }: { direction: 'LONG' | 'SHORT' }) {
  return (
    <Badge variant="outline" className={direction === 'LONG' ? 'text-green-600' : 'text-red-600'}>
      {direction === 'LONG' ? 'Long' : 'Short'}
    </Badge>
  )
}

/**
 * Completed trades (a position from flat to flat), newest first. A row opens the fills of all
 * its legs; a trade closed in parts shows a legs badge.
 */
export function ClosedTradesTable({
  trades,
  formatCurrency,
  onSelect,
}: {
  trades: DiscretionaryTrade[]
  formatCurrency: Money
  onSelect: (id: number) => void
}) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Date</TableHead>
          <TableHead>Broker</TableHead>
          <TableHead>Account</TableHead>
          <TableHead>Symbol</TableHead>
          <TableHead>Direction</TableHead>
          <TableHead>Product</TableHead>
          <TableHead className="text-right">Entry</TableHead>
          <TableHead className="text-right">Exit</TableHead>
          <TableHead className="text-right">Qty</TableHead>
          <TableHead className="text-right">Gross P&amp;L</TableHead>
          <TableHead className="text-right">Charges</TableHead>
          <TableHead className="text-right">Net P&amp;L</TableHead>
          <TableHead className="text-right">Held</TableHead>
          <TableHead>Status</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {trades.map((t) => {
          const badge = chargesBadge(t)
          return (
            <TableRow
              key={t.id}
              className="cursor-pointer"
              role="button"
              tabIndex={0}
              aria-label={`Show fills of ${t.symbol} closed ${t.closed_at}`}
              onClick={() => onSelect(t.id)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  onSelect(t.id)
                }
              }}
            >
              <TableCell className="font-mono" title={t.closed_at}>
                {t.trade_date}
              </TableCell>
              <TableCell>{t.broker}</TableCell>
              <TableCell>{t.account || '—'}</TableCell>
              <TableCell className="font-medium">
                {t.symbol}
                {t.overnight && (
                  <span className="ml-1 text-xs text-muted-foreground" title="Held overnight">
                    overnight
                  </span>
                )}
              </TableCell>
              <TableCell>
                <DirectionBadge direction={t.direction} />
              </TableCell>
              <TableCell>{t.product}</TableCell>
              <TableCell className="text-right font-mono">{fmtPrice(t.entry_price)}</TableCell>
              <TableCell className="text-right font-mono">{fmtPrice(t.exit_price)}</TableCell>
              <TableCell className="text-right font-mono">
                {fmtQty(t.closed_quantity)}
                {t.legs > 1 && (
                  <Badge
                    variant="outline"
                    className="ml-1 text-[10px] text-muted-foreground"
                    title={`Closed in ${t.legs} parts (partial exits)`}
                  >
                    {t.legs} legs
                  </Badge>
                )}
              </TableCell>
              <TableCell className={cn('text-right font-mono', pnlClass(t.realized_pnl))}>
                {formatCurrency(t.realized_pnl)}
              </TableCell>
              <TableCell className="text-right">
                <div className="flex flex-col items-end gap-0.5">
                  {t.charges != null && (
                    <span className="font-mono text-muted-foreground">
                      {formatCurrency(t.charges)}
                    </span>
                  )}
                  <Badge
                    variant="outline"
                    className={
                      badge.tone === 'warn'
                        ? 'border-amber-500/50 text-amber-600'
                        : 'text-muted-foreground'
                    }
                    title={badge.title}
                  >
                    {badge.label}
                  </Badge>
                </div>
              </TableCell>
              <TableCell className={cn('text-right font-mono', pnlClass(t.net_pnl ?? 0))}>
                {t.net_pnl == null ? '—' : formatCurrency(t.net_pnl)}
              </TableCell>
              <TableCell className="text-right font-mono">{fmtDuration(t.hold_minutes)}</TableCell>
              <TableCell>
                <Badge variant="secondary">Closed</Badge>
              </TableCell>
            </TableRow>
          )
        })}
      </TableBody>
    </Table>
  )
}

/** Open positions with the price behind each unrealized figure. */
export function OpenPositionsTable({
  positions,
  formatCurrency,
}: {
  positions: DiscretionaryPosition[]
  formatCurrency: Money
}) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Opened</TableHead>
          <TableHead>Broker</TableHead>
          <TableHead>Account</TableHead>
          <TableHead>Symbol</TableHead>
          <TableHead>Direction</TableHead>
          <TableHead>Product</TableHead>
          <TableHead className="text-right">Qty</TableHead>
          <TableHead className="text-right">Avg entry</TableHead>
          <TableHead className="text-right">LTP</TableHead>
          <TableHead>Price</TableHead>
          <TableHead className="text-right">Realized so far</TableHead>
          <TableHead className="text-right">Unrealized</TableHead>
          <TableHead>Status</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {positions.map((p, i) => (
          <TableRow key={`${p.broker}-${p.account}-${p.exchange}-${p.symbol}-${p.product}-${i}`}>
            <TableCell className="font-mono">
              {p.opened_at ? p.opened_at.slice(0, 10) : '—'}
            </TableCell>
            <TableCell>{p.broker}</TableCell>
            <TableCell>{p.account || '—'}</TableCell>
            <TableCell className="font-medium">{p.symbol}</TableCell>
            <TableCell>
              <DirectionBadge direction={p.direction} />
            </TableCell>
            <TableCell>{p.product}</TableCell>
            <TableCell className="text-right font-mono">{fmtQty(p.open_qty)}</TableCell>
            <TableCell className="text-right font-mono">{fmtPrice(p.avg_entry)}</TableCell>
            <TableCell className="text-right font-mono">{fmtPrice(p.ltp)}</TableCell>
            <TableCell>
              <PriceStatusBadge status={p.price_status} ageSeconds={p.age_s} />
            </TableCell>
            <TableCell
              className={cn('text-right font-mono', pnlClass(p.realized_so_far ?? 0))}
              title="Gross P&L of the partial exits of this position, already in realized"
            >
              {p.realized_so_far ? formatCurrency(p.realized_so_far) : '—'}
            </TableCell>
            <TableCell className={cn('text-right font-mono', pnlClass(p.unrealized ?? 0))}>
              {p.unrealized == null ? '—' : formatCurrency(p.unrealized)}
            </TableCell>
            <TableCell>
              <div className="flex flex-wrap gap-1">
                <Badge variant="secondary">Open</Badge>
                {p.opening_unknown && (
                  <Badge
                    variant="outline"
                    className="border-amber-500/50 text-amber-600"
                    title="The fill that opened this position is not in the ledger, so the entry price may be off."
                  >
                    opening position unknown
                  </Badge>
                )}
              </div>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}
