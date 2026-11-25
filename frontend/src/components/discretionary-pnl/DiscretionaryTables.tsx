import { ChevronDown, ChevronRight } from 'lucide-react'
import { Fragment, useMemo, useState } from 'react'
import type {
  BreakdownRow,
  DiscretionaryPosition,
  DiscretionaryTrade,
  ExcursionPosition,
  PeriodRow,
  ProvisionalBlock,
} from '@/api/discretionaryPnl'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
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
  CATEGORY_LABEL,
  categoryReasonText,
  chargesBadge,
  fmtCapture,
  fmtDistance,
  fmtPoints,
  fmtPositionAge,
  fmtPrice,
  fmtProfitFactor,
  fmtQty,
  fmtStamp,
  fmtWinRate,
  netIncomplete,
  openPositionProvisionalNote,
  type PriceBadge,
  pnlTextClass,
  priceBadge,
} from '@/lib/discretionaryPnlView'
import { fmtDuration } from '@/lib/strategyPnlView'
import { cn } from '@/lib/utils'

type Money = (value: number) => string

/** Marks a net figure that leaves out charges it could not estimate. */
function IncompleteMark({ count }: { count: number }) {
  return (
    <span
      className="ml-1 text-xs text-amber-700 dark:text-amber-500 [.analyzer_&]:text-amber-500"
      title={`${count} trade(s) have no charge estimate`}
    >
      incomplete
    </span>
  )
}

const TONE_CLASS: Record<PriceBadge['tone'], string> = {
  ok: 'border-green-600/40 text-green-700 dark:text-green-600 [.analyzer_&]:text-green-600',
  warn: 'border-amber-500/50 text-amber-700 dark:text-amber-500 [.analyzer_&]:text-amber-500',
  bad: 'border-red-600/40 text-red-600 dark:text-red-400',
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
            <TableCell className={cn('text-right font-mono', pnlTextClass(row.realized_gross))}>
              {formatCurrency(row.realized_gross)}
            </TableCell>
            <TableCell className="text-right font-mono text-muted-foreground">
              {formatCurrency(row.charges)}
            </TableCell>
            <TableCell className={cn('text-right font-mono', pnlTextClass(row.net_of_charges))}>
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
  title?: string
  firstColumn: string
  rows: BreakdownRow[]
  formatCurrency: Money
}) {
  return (
    <div className="min-w-0 overflow-x-auto">
      {title && <div className="text-sm font-medium mb-2">{title}</div>}
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
                    : fmtProfitFactor(
                        row.profit_factor,
                        row.wins,
                        row.losses,
                        !row.stats_unavailable_reason
                      )}
                </TableCell>
                <TableCell className="text-right font-mono">
                  {fmtDuration(row.avg_hold_minutes)}
                </TableCell>
                <TableCell className={cn('text-right font-mono', pnlTextClass(row.realized_gross))}>
                  {formatCurrency(row.realized_gross)}
                </TableCell>
                <TableCell className={cn('text-right font-mono', pnlTextClass(row.net_of_charges))}>
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
    <Badge
      variant="outline"
      className={
        direction === 'LONG'
          ? 'text-green-700 dark:text-green-600 [.analyzer_&]:text-green-600'
          : 'text-red-600 dark:text-red-400'
      }
    >
      {direction === 'LONG' ? 'Long' : 'Short'}
    </Badge>
  )
}

/** Category of a position; an unclassified one carries its reason as a tooltip. */
function CategoryBadge({ trade }: { trade: DiscretionaryTrade }) {
  const category = trade.category
  if (!category) return <span className="text-muted-foreground">—</span>
  const unclassified = category === 'unclassified'
  return (
    <Badge
      variant="outline"
      className={
        unclassified
          ? 'border-amber-500/50 text-amber-700 dark:text-amber-500 [.analyzer_&]:text-amber-500'
          : 'text-muted-foreground'
      }
      title={
        unclassified ? `Unclassified: ${categoryReasonText(trade.category_reason)}` : undefined
      }
      data-testid="category-badge"
    >
      {CATEGORY_LABEL[category] ?? category}
    </Badge>
  )
}

/** What the MFE amount is: a market measurement, never realisable profit. */
export const MFE_TOOLTIP =
  'largest favourable price move while open, from 1-minute bars (a lower bound); price move x quantity open at that moment; a market measurement, not achievable profit'

/** What the MAE amount is: the move against you times the quantity open at that moment. */
export const MAE_TOOLTIP =
  'largest adverse price move while open, from 1-minute bars (a lower bound); price move x quantity open at that moment'

/** MAE or MFE of a closed row: points per unit, a dash (with the reason) when not available. */
function ExcursionCell({
  position,
  field,
  loaded,
  formatCurrency,
}: {
  position: ExcursionPosition | undefined
  field: 'mae' | 'mfe'
  loaded: boolean
  formatCurrency: Money
}) {
  const points = position?.status === 'available' ? position[`${field}_points`] : null
  if (points == null) {
    const reason = position
      ? (position.reason_text ?? 'not available')
      : loaded
        ? 'not available for this position'
        : 'MAE / MFE is not loaded yet'
    return (
      <span className="text-muted-foreground" title={reason} data-testid={`${field}-unavailable`}>
        —
      </span>
    )
  }
  const amount = position?.[`${field}_amount`]
  const title =
    field === 'mfe'
      ? `${amount != null ? `${formatCurrency(amount)}: ` : ''}${MFE_TOOLTIP}`
      : amount != null
        ? `${formatCurrency(amount)}: ${MAE_TOOLTIP}`
        : undefined
  return (
    <span title={title} data-testid={`${field}-value`}>
      {fmtDistance(points)}
    </span>
  )
}

/**
 * Capture (a percent) or giveback (points) of a closed row; a dash with the reason when the
 * position is not counted (not a net winner, no candle coverage, ...), never 0.
 */
function CaptureValue({
  position,
  field,
}: {
  position: ExcursionPosition | undefined
  field: 'capture' | 'giveback'
}) {
  const value = field === 'capture' ? position?.profit_capture : position?.giveback_points
  if (value == null) {
    const reason = position?.capture_reason_text ?? 'not available for this position'
    return (
      <span className="text-muted-foreground" title={reason} data-testid={`${field}-unavailable`}>
        —
      </span>
    )
  }
  return (
    <span data-testid={`${field}-value`}>
      {field === 'capture' ? fmtCapture(value) : fmtDistance(value)}
    </span>
  )
}

/** Entry and exit time in one cell; the exit shows its date only when it is another day. */
function timeSpan(opened: string, closed: string): string {
  const o = fmtStamp(opened)
  const c = fmtStamp(closed)
  return o.slice(0, 10) === c.slice(0, 10) ? `${o} → ${c.slice(11)}` : `${o} → ${c}`
}

/** Trade numbers by opening time within the rows shown (1 = opened first); frontend only. */
export function tradeNumbers(trades: DiscretionaryTrade[]): Map<number, number> {
  const order = [...trades].sort((a, b) =>
    a.opened_at < b.opened_at ? -1 : a.opened_at > b.opened_at ? 1 : a.id - b.id
  )
  return new Map(order.map((t, i) => [t.id, i + 1]))
}

const DETAIL_COLSPAN = 11

function DetailItem({
  label,
  title,
  children,
}: {
  label: string
  title?: string
  children: React.ReactNode
}) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted-foreground" title={title}>
        {label}
      </dt>
      <dd className="font-mono text-sm tabular-nums">{children}</dd>
    </div>
  )
}

/**
 * Completed trades (a position from flat to flat), newest first, in eleven columns. The
 * disclosure button of the first cell opens a detail row with the secondary figures (points,
 * MAE / MFE, category, product, broker, account, reliability notes) and the button that opens
 * the fill lifecycle. MAE / MFE come from the lazy excursions call, joined by position id; the
 * table scrolls sideways inside its card.
 */
export function ClosedTradesTable({
  trades,
  formatCurrency,
  onSelect,
  excursions,
  excursionsLoaded = false,
}: {
  trades: DiscretionaryTrade[]
  formatCurrency: Money
  onSelect: (id: number) => void
  excursions?: Map<number, ExcursionPosition>
  excursionsLoaded?: boolean
}) {
  const [expanded, setExpanded] = useState<Set<number>>(() => new Set())
  const numbers = useMemo(() => tradeNumbers(trades), [trades])
  const toggle = (id: number) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Trade #</TableHead>
          <TableHead>Symbol</TableHead>
          <TableHead>Direction</TableHead>
          <TableHead>Entry → exit</TableHead>
          <TableHead className="text-right">Held</TableHead>
          <TableHead className="text-right">Quantity</TableHead>
          <TableHead className="text-right">Avg entry</TableHead>
          <TableHead className="text-right">Avg exit</TableHead>
          <TableHead className="text-right">Gross P&amp;L</TableHead>
          <TableHead className="text-right">Charges (est.)</TableHead>
          <TableHead className="text-right">Net P&amp;L</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {trades.map((t) => {
          const badge = chargesBadge(t)
          const ex = excursions?.get(t.id)
          const isOpen = expanded.has(t.id)
          const n = numbers.get(t.id)
          const detailId = `closed-trade-detail-${t.id}`
          return (
            <Fragment key={t.id}>
              <TableRow data-testid="closed-trade-row">
                <TableCell className="whitespace-nowrap font-mono tabular-nums">
                  <button
                    type="button"
                    className="inline-flex items-center gap-1 rounded px-1 py-0.5 hover:bg-muted focus-visible:outline focus-visible:outline-2"
                    aria-expanded={isOpen}
                    aria-controls={detailId}
                    aria-label={`${isOpen ? 'Hide' : 'Show'} details of trade ${n}, ${t.symbol} ${t.direction === 'LONG' ? 'long' : 'short'}`}
                    onClick={() => toggle(t.id)}
                    data-testid="trade-expander"
                  >
                    {isOpen ? (
                      <ChevronDown className="size-4" aria-hidden="true" />
                    ) : (
                      <ChevronRight className="size-4" aria-hidden="true" />
                    )}
                    {n}
                  </button>
                </TableCell>
                <TableCell className="whitespace-nowrap font-medium">
                  {t.symbol}
                  <span className="ml-1 text-xs font-normal text-muted-foreground">
                    {t.exchange}
                  </span>
                  {t.overnight && (
                    <span className="ml-1 text-xs text-muted-foreground" title="Held overnight">
                      overnight
                    </span>
                  )}
                  {t.opening_unknown && (
                    <Badge
                      variant="outline"
                      className="ml-1 border-amber-500/50 text-[10px] text-amber-700 dark:text-amber-500 [.analyzer_&]:text-amber-500"
                      title="The fill that opened this position is not in the ledger, so the entry price may be off."
                    >
                      opening unknown
                    </Badge>
                  )}
                  {t.has_orphan_fills && (
                    <Badge
                      variant="outline"
                      className="ml-1 border-amber-500/50 text-[10px] text-amber-700 dark:text-amber-500 [.analyzer_&]:text-amber-500"
                      title={`${t.orphan_fills ?? 'Some'} fill(s) of this position could not be placed and are not in its figures.`}
                    >
                      unplaced fills
                    </Badge>
                  )}
                </TableCell>
                <TableCell>
                  <DirectionBadge direction={t.direction} />
                </TableCell>
                <TableCell
                  className="whitespace-nowrap font-mono tabular-nums"
                  title={`${t.opened_at} to ${t.closed_at}`}
                >
                  {timeSpan(t.opened_at, t.closed_at)}
                </TableCell>
                <TableCell className="whitespace-nowrap text-right font-mono tabular-nums">
                  {fmtDuration(t.hold_minutes)}
                </TableCell>
                <TableCell
                  className="whitespace-nowrap text-right font-mono tabular-nums"
                  title={`${fmtQty(t.closed_quantity)} bought and ${fmtQty(t.closed_quantity)} sold over the trade`}
                  data-testid="trade-quantity"
                >
                  {fmtQty(t.closed_quantity)}
                </TableCell>
                <TableCell className="text-right font-mono tabular-nums">
                  {fmtPrice(t.entry_price)}
                </TableCell>
                <TableCell className="text-right font-mono tabular-nums">
                  {fmtPrice(t.exit_price)}
                </TableCell>
                <TableCell
                  className={cn('text-right font-mono tabular-nums', pnlTextClass(t.realized_pnl))}
                >
                  {formatCurrency(t.realized_pnl)}
                </TableCell>
                <TableCell className="text-right">
                  <div className="flex flex-col items-end gap-0.5">
                    {t.charges != null && (
                      <span className="font-mono tabular-nums text-muted-foreground">
                        {formatCurrency(t.charges)}
                      </span>
                    )}
                    {badge.tone === 'warn' && (
                      <Badge
                        variant="outline"
                        className="border-amber-500/50 text-amber-700 dark:text-amber-500 [.analyzer_&]:text-amber-500"
                        title={badge.title}
                      >
                        {badge.label}
                      </Badge>
                    )}
                  </div>
                </TableCell>
                <TableCell
                  className={cn('text-right font-mono tabular-nums', pnlTextClass(t.net_pnl ?? 0))}
                >
                  {t.net_pnl == null ? '—' : formatCurrency(t.net_pnl)}
                </TableCell>
              </TableRow>
              {isOpen && (
                <TableRow id={detailId} data-testid="closed-trade-detail" className="bg-muted/30">
                  <TableCell colSpan={DETAIL_COLSPAN}>
                    <dl className="grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-4 lg:grid-cols-6">
                      <DetailItem
                        label="Points / unit"
                        title="Exit minus entry for a long, entry minus exit for a short, per unit"
                      >
                        <span className={pnlTextClass(t.points_per_unit ?? 0)}>
                          {fmtPoints(t.points_per_unit)}
                        </span>
                      </DetailItem>
                      <DetailItem label="MAE" title={MAE_TOOLTIP}>
                        <ExcursionCell
                          position={ex}
                          field="mae"
                          loaded={excursionsLoaded}
                          formatCurrency={formatCurrency}
                        />
                      </DetailItem>
                      <DetailItem label="MFE" title={MFE_TOOLTIP}>
                        <ExcursionCell
                          position={ex}
                          field="mfe"
                          loaded={excursionsLoaded}
                          formatCurrency={formatCurrency}
                        />
                      </DetailItem>
                      <DetailItem
                        label="Capture"
                        title="Realised points / MFE, gross points on the average entry in force at each exit. Net winners with candle coverage only."
                      >
                        <CaptureValue position={ex} field="capture" />
                      </DetailItem>
                      <DetailItem label="Giveback" title="MFE minus realised points, per unit">
                        <CaptureValue position={ex} field="giveback" />
                      </DetailItem>
                      <DetailItem label="Category">
                        <CategoryBadge trade={t} />
                      </DetailItem>
                      <DetailItem label="Product">{t.product}</DetailItem>
                      <DetailItem label="Broker">{t.broker}</DetailItem>
                      <DetailItem label="Account">{t.account || '—'}</DetailItem>
                      <DetailItem label="Charges" title={badge.title}>
                        {t.charges != null ? formatCurrency(t.charges) : '—'}{' '}
                        <span className="font-sans text-xs text-muted-foreground">
                          {badge.label}
                        </span>
                      </DetailItem>
                    </dl>
                    {(t.opening_unknown || t.has_orphan_fills) && (
                      <p className="mt-2 text-xs text-amber-700 dark:text-amber-500 [.analyzer_&]:text-amber-500">
                        {t.opening_unknown &&
                          'Opening fill not in the ledger; entry price may be off. '}
                        {t.has_orphan_fills &&
                          `${t.orphan_fills ?? 'Some'} fill(s) could not be placed and are not counted.`}
                      </p>
                    )}
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="mt-2"
                      onClick={() => onSelect(t.id)}
                      aria-label={`Show fills and lifecycle of trade ${n}, ${t.symbol} closed ${t.closed_at}`}
                    >
                      Show fills and lifecycle
                    </Button>
                  </TableCell>
                </TableRow>
              )}
            </Fragment>
          )
        })}
      </TableBody>
    </Table>
  )
}

/** Open positions with the price behind each unrealized figure and their age. */
export function OpenPositionsTable({
  positions,
  formatCurrency,
  nowMs,
  provisional,
}: {
  positions: DiscretionaryPosition[]
  formatCurrency: Money
  /** Provisional executions: a row with provisional quantity in the same instrument gets a note. */
  provisional?: ProvisionalBlock | null
  /** Clock for the Age column (tests pin it); defaults to now. */
  nowMs?: number
}) {
  const now = nowMs ?? Date.now()
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Opened</TableHead>
          <TableHead>Age</TableHead>
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
            <TableCell
              className="font-mono whitespace-nowrap"
              title={p.opened_at ? `opened ${p.opened_at} IST` : undefined}
            >
              {fmtPositionAge(p.opened_at, now)}
            </TableCell>
            <TableCell>{p.broker}</TableCell>
            <TableCell>{p.account || '—'}</TableCell>
            <TableCell className="font-medium">
              {p.symbol}
              {openPositionProvisionalNote(p, provisional) && (
                <p
                  className="text-xs font-normal text-amber-700 dark:text-amber-500 [.analyzer_&]:text-amber-500"
                  data-testid="open-provisional-note"
                >
                  {openPositionProvisionalNote(p, provisional)}
                </p>
              )}
            </TableCell>
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
              className={cn('text-right font-mono', pnlTextClass(p.realized_so_far ?? 0))}
              title="Gross P&L of the partial exits of this position, already in realized"
            >
              {p.realized_so_far ? formatCurrency(p.realized_so_far) : '—'}
            </TableCell>
            <TableCell className={cn('text-right font-mono', pnlTextClass(p.unrealized ?? 0))}>
              {p.unrealized == null ? '—' : formatCurrency(p.unrealized)}
            </TableCell>
            <TableCell>
              <div className="flex flex-wrap gap-1">
                <Badge variant="secondary">Open</Badge>
                {p.opening_unknown && (
                  <Badge
                    variant="outline"
                    className="border-amber-500/50 text-amber-700 dark:text-amber-500 [.analyzer_&]:text-amber-500"
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
