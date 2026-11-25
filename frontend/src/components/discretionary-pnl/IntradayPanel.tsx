import type { IntradayBlock, IntradayStatus } from '@/api/discretionaryPnl'
import { Badge } from '@/components/ui/badge'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { fmtPrice, fmtQty, fmtStamp, pnlTextClass, priceBadge } from '@/lib/discretionaryPnlView'
import { fmtDate } from '@/lib/strategyPnlView'
import { cn } from '@/lib/utils'

type Money = (value: number) => string

export const INTRADAY_PANEL_ID = 'discretionary-intraday'
export const NOT_CHECKED_TEXT = 'not checked against broker'

const STATUS_LABEL: Record<IntradayStatus, string> = {
  confirmed: 'Confirmed',
  partially_reconciled: 'Partially reconciled',
  provisional: 'Provisional',
}

const STATUS_TITLE: Record<IntradayStatus, string> = {
  confirmed: 'Every execution behind this row is attributed to you.',
  partially_reconciled:
    'Some executions are attributed to you and some still await attribution. The row can change.',
  provisional: 'Every execution behind this row still awaits attribution. The row can change.',
}

export const EARLIER_SESSION_NOTE = "Earlier session, not part of today's position"

/** What a trader reads when a position has no usable price (never a status code). */
export function noPriceText(status: string | null | undefined): string {
  switch (status) {
    case 'stale':
    case 'cached':
      return 'Price is out of date'
    case 'not_requested':
      return 'Prices not fetched'
    default:
      return 'Price not available'
  }
}

const WARN_CLASS =
  'border-amber-500/50 text-amber-700 dark:text-amber-500 [.analyzer_&]:text-amber-500'

function StatusBadge({ status }: { status: IntradayStatus }) {
  return (
    <Badge
      variant="outline"
      className={cn('text-[10px]', status !== 'confirmed' && WARN_CLASS)}
      title={STATUS_TITLE[status]}
      data-testid={`intraday-status-${status}`}
    >
      {STATUS_LABEL[status]}
    </Badge>
  )
}

/** True when the block has something to show (a position, a closed leg or an unmatched fill). */
export function hasIntradayData(block: IntradayBlock | null | undefined): block is IntradayBlock {
  return (
    !!block && (block.positions.length > 0 || block.closed.length > 0 || block.unmatched.length > 0)
  )
}

/**
 * Today's confirmed and pending executions replayed together as one position per instrument.
 * Read only: nothing here is added to any confirmed total, metric, chart or history row, and
 * nothing is checked against the broker. Shown for the current (1D) view only.
 */
export function IntradayPanel({
  intraday,
  formatCurrency,
}: {
  intraday: IntradayBlock | null | undefined
  formatCurrency: Money
}) {
  if (!hasIntradayData(intraday)) return null
  const t = intraday.totals
  // Nothing was priced and no leg has closed: the total would be a sum of nothing, not zero.
  const noTotal =
    !t.total_complete && t.unrealized === null && t.confirmed_legs + t.provisional_legs === 0
  const earlier = intraday.unmatched.filter((u) => u.reason === 'earlier_session_unresolved')
  const unmatchedToday = intraday.unmatched.filter((u) => u.reason !== 'earlier_session_unresolved')
  const money = (v: number | null | undefined) =>
    v === null || v === undefined ? 'n/a' : formatCurrency(v)
  return (
    <section
      id={INTRADAY_PANEL_ID}
      className="space-y-3 rounded-md border px-3 py-3"
      data-testid="intraday-panel"
    >
      <div className="space-y-1">
        <h3 className="text-sm font-medium" data-testid="intraday-heading">
          Today intraday ({fmtDate(intraday.session_date)})
        </h3>
        <p className="text-xs text-muted-foreground" data-testid="intraday-explanation">
          Today&apos;s confirmed executions and today&apos;s executions awaiting attribution,
          combined into one position per instrument. Provisional figures can change when an
          execution is attributed. Positions are {NOT_CHECKED_TEXT}.
        </p>
      </div>

      <div
        className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs sm:grid-cols-3"
        data-testid="intraday-totals"
      >
        <TotalCell
          label="Confirmed realized"
          testId="intraday-confirmed-realized"
          value={t.confirmed_realized}
          money={money}
        />
        <TotalCell
          label="Provisional realized"
          testId="intraday-provisional-realized"
          value={t.provisional_realized}
          money={money}
        />
        <TotalCell
          label="Unrealized"
          testId="intraday-unrealized"
          value={t.unrealized}
          money={money}
          suffix={
            t.unpriced_positions > 0
              ? `${t.priced_positions} of ${t.open_positions} priced`
              : undefined
          }
        />
        <TotalCell
          label="Estimated charges (provisional legs)"
          testId="intraday-estimated-charges"
          value={t.estimated_charges}
          money={money}
          plain
        />
        <TotalCell
          label="Confirmed charges"
          testId="intraday-confirmed-charges"
          value={t.confirmed_charges}
          money={money}
          plain
        />
        <TotalCell
          label={t.total_complete ? 'Total intraday' : 'Total intraday (partial)'}
          testId="intraday-total"
          value={noTotal ? null : t.total_gross}
          money={money}
          strong
          suffix={t.total_complete ? undefined : `excludes ${t.unpriced_positions} unpriced`}
        />
      </div>
      <p className="text-xs text-muted-foreground" data-testid="intraday-basis">
        Basis: {t.basis}. {t.confirmed_legs} confirmed and {t.provisional_legs} provisional closed
        leg(s), {t.open_positions} open position(s), {t.confirmed_fills} confirmed and{' '}
        {t.pending_fills} pending execution(s).
        {t.total_net !== null && !noTotal && (
          <>
            {' '}
            Net of charges: <span className="font-mono">{money(t.total_net)}</span>.
          </>
        )}
      </p>
      {!t.total_complete && t.partial_reason && (
        <p className={cn('text-xs', WARN_CLASS)} data-testid="intraday-partial">
          {t.partial_reason}
        </p>
      )}

      {intraday.positions.length > 0 && (
        <div className="space-y-1" data-testid="intraday-positions">
          <p className="text-xs font-medium">Open positions today</p>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Symbol</TableHead>
                <TableHead>Side</TableHead>
                <TableHead className="text-right">Qty</TableHead>
                <TableHead className="text-right">Avg entry</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Last price</TableHead>
                <TableHead className="text-right">Unrealized</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {intraday.positions.map((p) => {
                const badge = priceBadge(p.price_status)
                return (
                  <TableRow
                    key={`${p.broker}|${p.account}|${p.exchange}|${p.symbol}|${p.product}|${p.opened_at}`}
                    data-testid="intraday-position-row"
                  >
                    <TableCell>
                      <span className="font-medium">{p.symbol}</span>{' '}
                      <span className="text-xs text-muted-foreground">
                        {p.exchange} {p.product}
                      </span>
                      <div className="text-[10px] text-muted-foreground">
                        {p.confirmed_fills} confirmed, {p.pending_fills} pending -{' '}
                        {NOT_CHECKED_TEXT}
                      </div>
                    </TableCell>
                    <TableCell>{p.direction === 'LONG' ? 'Long' : 'Short'}</TableCell>
                    <TableCell className="text-right font-mono">{fmtQty(p.quantity)}</TableCell>
                    <TableCell className="text-right font-mono">{fmtPrice(p.avg_entry)}</TableCell>
                    <TableCell>
                      <StatusBadge status={p.status} />
                    </TableCell>
                    <TableCell className="text-right font-mono">
                      {p.ltp === null ? 'n/a' : fmtPrice(p.ltp)}
                      <div className="text-[10px] text-muted-foreground" title={badge.title}>
                        {badge.label}
                      </div>
                    </TableCell>
                    <TableCell
                      className={cn(
                        'text-right font-mono',
                        p.unrealized !== null && pnlTextClass(p.unrealized)
                      )}
                      title={p.unrealized_reason ?? undefined}
                      data-testid="intraday-position-unrealized"
                    >
                      {p.unrealized === null ? 'n/a' : formatCurrency(p.unrealized)}
                      {p.unrealized === null && (
                        <div
                          className="text-[10px] font-sans text-muted-foreground"
                          data-testid="intraday-position-no-price"
                        >
                          {noPriceText(p.price_status)}
                        </div>
                      )}
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        </div>
      )}

      {intraday.closed.length > 0 && (
        <div className="space-y-1" data-testid="intraday-closed">
          <p className="text-xs font-medium">Closed today</p>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Symbol</TableHead>
                <TableHead>Side</TableHead>
                <TableHead className="text-right">Qty</TableHead>
                <TableHead className="text-right">Entry</TableHead>
                <TableHead className="text-right">Exit</TableHead>
                <TableHead>Closed</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Realized (gross)</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {intraday.closed.map((c) => (
                <TableRow
                  key={`${c.broker}|${c.account}|${c.exchange}|${c.symbol}|${c.product}|${c.closed_at}|${c.quantity}`}
                  data-testid="intraday-closed-row"
                >
                  <TableCell>
                    <span className="font-medium">{c.symbol}</span>{' '}
                    <span className="text-xs text-muted-foreground">{c.product}</span>
                  </TableCell>
                  <TableCell>{c.direction === 'LONG' ? 'Long' : 'Short'}</TableCell>
                  <TableCell className="text-right font-mono">{fmtQty(c.quantity)}</TableCell>
                  <TableCell className="text-right font-mono">{fmtPrice(c.entry_price)}</TableCell>
                  <TableCell className="text-right font-mono">{fmtPrice(c.exit_price)}</TableCell>
                  <TableCell className="text-xs">{fmtStamp(c.closed_at)}</TableCell>
                  <TableCell>
                    <StatusBadge status={c.status} />
                  </TableCell>
                  <TableCell className={cn('text-right font-mono', pnlTextClass(c.realized_gross))}>
                    {formatCurrency(c.realized_gross)}
                    {c.charges !== null && (
                      <div className="text-[10px] text-muted-foreground">
                        {c.charges_estimated ? 'est. charges' : 'charges'}{' '}
                        {formatCurrency(c.charges)}
                      </div>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      {unmatchedToday.length > 0 && (
        <div className="space-y-1" data-testid="intraday-unmatched">
          <p className="text-xs font-medium">Not matched to a position ({unmatchedToday.length})</p>
          <p className="text-xs text-muted-foreground">
            These executions are not part of the positions above and are not guessed.
          </p>
          <ul className="space-y-0.5 text-xs">
            {unmatchedToday.map((u) => (
              <li key={`${u.fill_ref}|${u.reason}`} data-testid="intraday-unmatched-row">
                <span className="font-medium">{u.symbol}</span> {u.side} {fmtQty(u.qty)} @{' '}
                {fmtPrice(u.price)}, {fmtStamp(u.fill_time)}: {u.reason_text}
              </li>
            ))}
          </ul>
        </div>
      )}

      {earlier.length > 0 && (
        <details className="text-xs" data-testid="intraday-earlier">
          <summary
            className="cursor-pointer font-medium text-muted-foreground"
            data-testid="intraday-earlier-toggle"
          >
            Not included in today&apos;s position ({earlier.length} earlier-session{' '}
            {earlier.length === 1 ? 'execution' : 'executions'})
          </summary>
          <div className="mt-1 space-y-1">
            <p className="text-xs text-muted-foreground" data-testid="intraday-earlier-note">
              {EARLIER_SESSION_NOTE}. For information only: these executions do not change
              today&apos;s quantity, entry price, profit or loss.
            </p>
            <ul className="space-y-0.5 text-xs text-muted-foreground">
              {earlier.map((u) => (
                <li key={`${u.fill_ref}|${u.reason}`} data-testid="intraday-earlier-row">
                  <span className="font-medium">{u.symbol}</span> {u.product} {u.side}{' '}
                  {fmtQty(u.qty)} @ {fmtPrice(u.price)}, {fmtStamp(u.fill_time)}
                </li>
              ))}
            </ul>
          </div>
        </details>
      )}
    </section>
  )
}

function TotalCell({
  label,
  value,
  money,
  testId,
  suffix,
  plain,
  strong,
}: {
  label: string
  value: number | null
  money: (v: number | null | undefined) => string
  testId: string
  suffix?: string
  plain?: boolean
  strong?: boolean
}) {
  return (
    <div>
      <span className="text-muted-foreground">{label}: </span>
      <span
        className={cn(
          'font-mono',
          strong && 'font-semibold',
          !plain && value !== null && pnlTextClass(value)
        )}
        data-testid={testId}
      >
        {money(value)}
      </span>
      {suffix && <span className="text-muted-foreground"> ({suffix})</span>}
    </div>
  )
}
