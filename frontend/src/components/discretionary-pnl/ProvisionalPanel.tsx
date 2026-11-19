import { ChevronDown, ChevronRight, Hourglass } from 'lucide-react'
import { Fragment, useState } from 'react'
import type { ProvisionalBlock, ProvisionalTrade } from '@/api/discretionaryPnl'
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
  fmtClock,
  fmtPrice,
  fmtProfitFactor,
  fmtQty,
  fmtStamp,
  fmtWinRate,
  PROVISIONAL_EXPLANATION,
  PROVISIONAL_TITLE,
  sampleNote,
} from '@/lib/discretionaryPnlView'
import { fmtDuration, pnlClass } from '@/lib/strategyPnlView'
import { cn } from '@/lib/utils'

type Money = (value: number) => string

const COLUMNS = 16

function ProvisionalRow({ t, formatCurrency }: { t: ProvisionalTrade; formatCurrency: Money }) {
  const [open, setOpen] = useState(false)
  const toggle = () => setOpen((v) => !v)
  return (
    <Fragment>
      <TableRow
        className="cursor-pointer"
        role="button"
        tabIndex={0}
        aria-expanded={open}
        aria-label={`Show the fills of the provisional ${t.symbol} trade`}
        onClick={toggle}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            toggle()
          }
        }}
        data-testid="provisional-row"
      >
        <TableCell className="font-mono" title={t.closed_at}>
          <span className="inline-flex items-center gap-1">
            {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
            {t.trade_date}
          </span>
        </TableCell>
        <TableCell>{t.broker}</TableCell>
        <TableCell>{t.account || '—'}</TableCell>
        <TableCell className="font-medium">{t.symbol}</TableCell>
        <TableCell>
          <Badge
            variant="outline"
            className={t.direction === 'LONG' ? 'text-green-600' : 'text-red-600'}
          >
            {t.direction === 'LONG' ? 'Long' : 'Short'}
          </Badge>
        </TableCell>
        <TableCell>{t.product}</TableCell>
        <TableCell className="text-right font-mono">{fmtPrice(t.entry_price)}</TableCell>
        <TableCell className="text-right font-mono">{fmtPrice(t.exit_price)}</TableCell>
        <TableCell className="text-right font-mono">
          {fmtQty(t.closed_quantity)}
          {t.legs > 1 && (
            <span className="ml-1 text-xs text-muted-foreground">({t.legs} exits)</span>
          )}
        </TableCell>
        <TableCell className="whitespace-nowrap">{fmtClock(t.opened_at)}</TableCell>
        <TableCell className="whitespace-nowrap">{fmtClock(t.closed_at)}</TableCell>
        <TableCell className={cn('text-right font-mono', pnlClass(t.realized_pnl))}>
          {formatCurrency(t.realized_pnl)}
        </TableCell>
        <TableCell className="text-right">
          {t.charges == null ? (
            <span className="text-amber-600">unavailable</span>
          ) : (
            <span className="font-mono text-muted-foreground">
              {formatCurrency(t.charges)} <span className="font-sans text-[11px]">estimated</span>
            </span>
          )}
        </TableCell>
        <TableCell className={cn('text-right font-mono', pnlClass(t.net_pnl ?? 0))}>
          {t.net_pnl == null ? '—' : formatCurrency(t.net_pnl)}
        </TableCell>
        <TableCell className="text-right font-mono">{fmtDuration(t.hold_minutes)}</TableCell>
        <TableCell>
          <div className="flex flex-wrap gap-1">
            <Badge variant="outline" className="border-amber-500/50 text-amber-600">
              Provisional
            </Badge>
            {t.opening_unknown && (
              <Badge
                variant="outline"
                className="border-amber-500/50 text-amber-600"
                title="A fill of this instrument was refused: its opening is not among these fills."
              >
                opening unknown
              </Badge>
            )}
          </div>
        </TableCell>
      </TableRow>
      {open && (
        <TableRow data-testid="provisional-fills">
          <TableCell colSpan={COLUMNS} className="bg-muted/40">
            <div className="text-xs font-medium mb-1">Broker fills of this trade (unresolved)</div>
            <div className="max-w-2xl">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Role</TableHead>
                    <TableHead>Side</TableHead>
                    <TableHead className="text-right">Qty</TableHead>
                    <TableHead className="text-right">Price</TableHead>
                    <TableHead>Time</TableHead>
                    <TableHead className="text-right">Est. charge</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {t.fills.map((f) => (
                    <TableRow key={`${f.fill_ref}-${f.role}`}>
                      <TableCell>{f.role === 'ENTRY' ? 'Entry' : 'Exit'}</TableCell>
                      <TableCell>{f.side}</TableCell>
                      <TableCell className="text-right font-mono">{fmtQty(f.qty_used)}</TableCell>
                      <TableCell className="text-right font-mono">{fmtPrice(f.price)}</TableCell>
                      <TableCell className="font-mono">{f.fill_time ?? '—'}</TableCell>
                      <TableCell className="text-right font-mono">
                        {f.charge_allocated == null ? '—' : formatCurrency(f.charge_allocated)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          </TableCell>
        </TableRow>
      )}
    </Fragment>
  )
}

/**
 * Unresolved executions replayed by the server as if they were manual (same engine, estimated
 * charges). Clearly separate from everything above: none of it is in any total.
 */
export function ProvisionalPanel({
  provisional: p,
  formatCurrency,
}: {
  provisional: ProvisionalBlock
  formatCurrency: Money
}) {
  if (!p || p.fills === 0) return null
  const m = p.metrics
  const none = m.trades === 0
  const note = sampleNote(p.sample)
  return (
    <Card className="border-dashed border-amber-500/60" data-testid="provisional-panel">
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-base">
          <Hourglass className="h-4 w-4 text-amber-600" />
          {PROVISIONAL_TITLE}
        </CardTitle>
        <p className="text-sm text-muted-foreground">{PROVISIONAL_EXPLANATION}</p>
      </CardHeader>
      <CardContent className="space-y-4">
        <div
          className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-8"
          data-testid="provisional-metrics"
        >
          <MetricCard
            label="Provisional trades"
            value={String(m.trades)}
            hint={`${p.fills} unresolved fill(s)`}
          />
          <MetricCard
            label="Gross P&L"
            value={formatCurrency(m.realized_gross)}
            positive={m.realized_gross > 0}
            negative={m.realized_gross < 0}
          />
          <MetricCard
            label="Est. charges"
            value={formatCurrency(m.charges)}
            hint={m.net_charges_complete ? 'estimated' : 'incomplete: some unavailable'}
          />
          <MetricCard
            label="Net P&L (est.)"
            value={formatCurrency(m.net_of_charges)}
            positive={m.net_of_charges > 0}
            negative={m.net_of_charges < 0}
          />
          <MetricCard
            label="Win rate"
            value={none ? '—' : fmtWinRate(m.win_rate)}
            hint={`${m.wins} wins / ${m.losses} losses`}
          />
          <MetricCard
            label="Profit factor"
            value={
              none || !p.sample?.sufficient
                ? '—'
                : fmtProfitFactor(m.profit_factor, m.wins, m.losses)
            }
            hint={
              !none && !p.sample?.sufficient
                ? `needs ${p.sample?.min_trades ?? 30} trades over ${p.sample?.min_days ?? 10} days`
                : undefined
            }
          />
          <MetricCard
            label="Avg win / loss"
            value={none ? '—' : `${formatCurrency(m.avg_win)} / ${formatCurrency(m.avg_loss)}`}
          />
          <MetricCard
            label="Expectancy"
            value={none ? '—' : formatCurrency(m.expectancy)}
            hint="gross, per trade"
          />
        </div>
        {note && !none && (
          <p className={cn('text-xs', note.low ? 'text-amber-600' : 'text-muted-foreground')}>
            {note.text}
          </p>
        )}

        {p.trades.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No provisional round trip is complete yet.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <Table data-testid="provisional-table">
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
                  <TableHead>Entry time</TableHead>
                  <TableHead>Final exit</TableHead>
                  <TableHead className="text-right">Gross P&amp;L</TableHead>
                  <TableHead className="text-right">Charges</TableHead>
                  <TableHead className="text-right">Net P&amp;L</TableHead>
                  <TableHead className="text-right">Held</TableHead>
                  <TableHead>Status</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {p.trades.map((t) => (
                  <ProvisionalRow
                    key={`${t.account}-${t.id}`}
                    t={t}
                    formatCurrency={formatCurrency}
                  />
                ))}
              </TableBody>
            </Table>
          </div>
        )}

        {p.open_positions.length > 0 && (
          <div data-testid="provisional-open">
            <div className="text-sm font-medium mb-1">Provisional open positions</div>
            <p className="text-xs text-muted-foreground mb-1">
              Only the unresolved fills are replayed here: an unresolved exit of a position you
              already hold appears as its own position until it is classified.
            </p>
            <ul className="text-sm space-y-0.5">
              {p.open_positions.map((o) => (
                <li key={`${o.broker}-${o.account}-${o.exchange}-${o.symbol}-${o.product}`}>
                  {o.symbol} {o.exchange} {o.product} {o.direction === 'LONG' ? 'Long' : 'Short'}{' '}
                  {fmtQty(o.open_qty)} @ {fmtPrice(o.avg_entry)} since {fmtStamp(o.opened_at)} (
                  {o.account || '—'})
                  {o.opening_unknown && (
                    <span className="ml-1 text-amber-600">opening unknown</span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
        {p.orphans.length > 0 && (
          <p className="text-xs text-amber-600" data-testid="provisional-orphans">
            {p.orphans.length} unresolved fill(s) could not be placed: their opening is not among
            these fills.
          </p>
        )}
      </CardContent>
    </Card>
  )
}
